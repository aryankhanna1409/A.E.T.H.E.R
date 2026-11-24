"""Computer control for Windows 11, behind the policy engine.

- Apps: only those listed in [security].apps can be opened.
- Shell: allowlisted programs only, no shell interpreter, scrubbed environment,
  working folder inside authorized roots, timeout, dangerous patterns refused.
  The policy engine always asks you first and shows the exact command.
- Files: move and delete (to AETHER's own trash) with an undo journal that
  also covers create and modify.
- Screen: screenshot + local OCR, only if screen_access is on.
- UI automation: typing and clicking in a named window via pywinauto, only if
  ui_automation is on.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..core.config import SecurityConfig
from .registry import Tool

DANGEROUS = re.compile(
    r"(rm\s+-rf|del\s+/[sq]|rmdir\s+/s|format\s+[a-z]:|reg\s+(add|delete)|bcdedit|diskpart|cipher\s+/w|"
    r"-encodedcommand|-enc\s|invoke-expression|iex\s|curl\s.*\|\s*(sh|bash)|wget\s.*\|\s*(sh|bash)|"
    r"shutdown|takeown|icacls|net\s+user|schtasks|--force\s+push|push\s+--force|git\s+push\s+-f)", re.I)
SAFE_ENV_KEYS = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
                 "PATHEXT", "COMSPEC", "LANG", "HOME", "PYTHONIOENCODING", "NUMBER_OF_PROCESSORS", "APPDATA",
                 "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"}


class Journal:
    """Undo journal for file operations."""

    def __init__(self, db, lock, trash: Path):
        self.db, self.lock, self.trash = db, lock, trash
        trash.mkdir(parents=True, exist_ok=True)
        with lock:
            db.execute("CREATE TABLE IF NOT EXISTS file_journal (id INTEGER PRIMARY KEY, op TEXT, src TEXT, dst TEXT, "
                       "backup TEXT, t REAL, undone INTEGER DEFAULT 0)")
            db.commit()

    def log(self, op: str, src: str, dst: str = "", backup: str = "") -> None:
        with self.lock:
            self.db.execute("INSERT INTO file_journal(op,src,dst,backup,t) VALUES (?,?,?,?,?)",
                            (op, src, dst, backup, time.time()))
            self.db.commit()

    def undo_last(self) -> str:
        row = self.db.execute("SELECT id,op,src,dst,backup FROM file_journal WHERE undone=0 ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return "Nothing to undo."
        jid, op, src, dst, backup = row
        if op == "create":
            Path(src).replace(self.trash / f"{int(time.time())}_{Path(src).name}")
            msg = f"Removed {src} (moved to AETHER trash)."
        elif op == "modify":
            shutil.copy2(backup, src)
            msg = f"Restored the previous version of {src}."
        elif op == "move":
            Path(dst).replace(src)
            msg = f"Moved {dst} back to {src}."
        elif op == "delete":
            Path(src).parent.mkdir(parents=True, exist_ok=True)
            Path(backup).replace(src)
            msg = f"Restored {src}."
        else:
            return f"Cannot undo '{op}'."
        with self.lock:
            self.db.execute("UPDATE file_journal SET undone=1 WHERE id=?", (jid,))
            self.db.commit()
        return msg

    def recent(self, n: int = 20) -> list[dict]:
        rows = self.db.execute("SELECT id,op,src,dst,t,undone FROM file_journal ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(zip(("id", "op", "src", "dst", "t", "undone"), r)) for r in rows]


def computer_tools(sec: SecurityConfig, journal: Journal) -> list[Tool]:
    def app_open(name: str) -> str:
        cmd = sec.apps.get(name.lower().strip())
        if not cmd:
            return f"'{name}' is not in the allowed apps list: {', '.join(sec.apps)}"
        if sys.platform == "win32":
            os.startfile(cmd) if cmd.endswith(".exe") or Path(cmd).exists() else subprocess.Popen([cmd], shell=False)
        else:  # development on other systems
            subprocess.Popen([cmd])
        return f"Opened {name}."

    def shell(command: str, cwd: str) -> str:
        if DANGEROUS.search(command):
            raise PermissionError("This command matches a destructive pattern and is always refused.")
        if any(ch in command for ch in "|&;<>`") or "$(" in command:
            raise PermissionError("Pipes, redirection and command chaining are not allowed; run one program at a time.")
        argv = shlex.split(command, posix=(sys.platform != "win32"))
        prog = Path(argv[0]).stem.lower()
        if prog not in {p.lower() for p in sec.shell_allow}:
            raise PermissionError(f"'{prog}' is not in the shell allowlist: {', '.join(sec.shell_allow)}")
        env = {k: v for k, v in os.environ.items() if k.upper() in SAFE_ENV_KEYS}
        env["PYTHONIOENCODING"] = "utf-8"
        if prog == "dir" and sys.platform == "win32":
            argv = ["cmd", "/c", *argv]
        out = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=sec.shell_timeout_s,
                             shell=False)
        text = (out.stdout + ("\n[stderr]\n" + out.stderr if out.stderr.strip() else ""))[-8000:]
        return f"$ {command}\n(exit code {out.returncode})\n{text}"

    def fs_move(path: str, destination: str) -> str:
        src, dst = Path(path).expanduser().resolve(), Path(destination).expanduser().resolve()
        if dst.is_dir():
            dst = dst / src.name
        if dst.exists():
            raise FileExistsError(f"{dst} already exists.")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        if not dst.exists() or src.exists():
            raise IOError("Move could not be verified.")
        journal.log("move", str(src), str(dst))
        return f"Moved {src.name} to {dst} (verified; undo available)."

    def fs_delete(path: str) -> str:
        src = Path(path).expanduser().resolve()
        if src.is_dir():
            raise IsADirectoryError("Deleting folders is not supported; move it instead.")
        backup = journal.trash / f"{int(time.time() * 1000)}_{src.name}"
        shutil.move(str(src), str(backup))
        journal.log("delete", str(src), backup=str(backup))
        return f"Deleted {src.name} (kept in AETHER trash; undo available)."

    def screen_read() -> str:
        if not sec.screen_access:
            return "Screen access is off. Turn on [security].screen_access in aether.toml to allow it."
        try:
            import mss
            import numpy as np
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            return "Screen reading needs: pip install -e .[computer]"
        with mss.mss() as sct:
            img = np.array(sct.grab(sct.monitors[1]))[:, :, :3]
        result, _ = RapidOCR()(img)
        lines = [r[1] for r in (result or [])]
        return "Text on screen:\n" + "\n".join(lines[:300]) if lines else "No readable text on screen."

    def ui_type(window: str, text: str) -> str:
        if not sec.ui_automation:
            return "UI automation is off. Turn on [security].ui_automation in aether.toml to allow it."
        from pywinauto import Desktop
        win = Desktop(backend="uia").window(title_re=f".*{re.escape(window)}.*")
        win.set_focus()
        win.type_keys(text, with_spaces=True, set_foreground=True)
        return f"Typed {len(text)} characters into '{window}'."

    def ui_click(window: str, control: str) -> str:
        if not sec.ui_automation:
            return "UI automation is off. Turn on [security].ui_automation in aether.toml to allow it."
        from pywinauto import Desktop
        win = Desktop(backend="uia").window(title_re=f".*{re.escape(window)}.*")
        win.child_window(title=control).click_input()
        return f"Clicked '{control}' in '{window}'."

    return [
        Tool("app_open", "open an allowed application", {"name": "app name, e.g. " + ", ".join(list(sec.apps)[:4])},
             "app.open", app_open),
        Tool("shell", "run ONE allowlisted program in an authorized folder (always asks, shows the command)",
             {"command": "e.g. pytest -q", "cwd": "authorized folder"}, "shell", shell, "cwd"),
        Tool("fs_move", "move or rename a file (asks first, undoable)", {"path": "file", "destination": "new path or folder"},
             "fs.move", fs_move, "path", (("destination", "fs.create"),)),
        Tool("fs_delete", "delete a file into AETHER trash (asks first, undoable)", {"path": "file"},
             "fs.delete", fs_delete, "path"),
        Tool("undo_last", "undo the last file change AETHER made", {}, "fs.modify", lambda: journal.undo_last()),
        Tool("screen_read", "read the text on screen (needs screen_access on; asks first)", {}, "screen.observe",
             screen_read),
        Tool("ui_type", "type text into a window (needs ui_automation on; asks first)", {"window": "title", "text": "text"},
             "ui.automation", ui_type),
        Tool("ui_click", "click a named button in a window (needs ui_automation on; asks first)",
             {"window": "title", "control": "button text"}, "ui.automation", ui_click),
    ]


def journal_json(j: Journal) -> str:
    return json.dumps(j.recent(), indent=1)
