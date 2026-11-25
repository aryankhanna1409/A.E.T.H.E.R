"""Incremental indexing of authorized folders: new and changed files are
(re)indexed, deleted files are removed from memory. Runs on demand or in a
background thread."""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from ..core.logging import log

INDEXABLE = {".txt", ".md", ".py", ".json", ".csv", ".toml", ".yaml", ".yml", ".html", ".js", ".ts", ".tsx",
             ".java", ".c", ".cpp", ".h", ".rs", ".go", ".tex", ".rst", ".pdf", ".docx"}


# Never indexed, wherever they appear: system, program, cache and hidden folders.
SKIP_DIRS = {"windows", "program files", "program files (x86)", "programdata", "appdata", "system volume information",
             "recovery", "perflogs", "msocache", "config.msi", "drivers", "node_modules", ".venv", "venv",
             "__pycache__", "site-packages", "dist-packages", ".git", ".svn", ".hg", ".idea", ".vscode", ".cache",
             ".gradle", ".m2", ".npm", ".nuget", "onedrivetemp"}
MAX_FILE_MB = 25


def skipped_dir(name: str) -> bool:
    n = name.lower()
    return n in SKIP_DIRS or n.startswith(("$", ".", "~"))


def skipped_path(path: str) -> bool:
    return any(skipped_dir(part) for part in Path(path).parts[1:-1])


def index_roots(read_roots: list[str], configured: list[str] | None = None) -> list[str]:
    """What gets indexed: [memory].index_roots if set, else every readable folder (system folders are skipped)."""
    return list(dict.fromkeys(configured or read_roots))


class FolderWatcher:
    def __init__(self, store, allowed: Callable[[str], bool], reader: Callable[[Path], str],
                 on_new_text: Callable[[str, str], None] | None = None):
        self.store, self.allowed, self.reader, self.on_new_text = store, allowed, reader, on_new_text
        with store.lock:
            store.db.execute("CREATE TABLE IF NOT EXISTS indexed_files (path TEXT PRIMARY KEY, mtime REAL, chunks INTEGER)")
            store.db.commit()
        self._stop = threading.Event()
        self.busy: Callable[[], bool] = lambda: False   # set by the app: True while a reply is being made
        self.yield_s = 0.0                               # pause per file in background scans
        self.folders: list[str] = []

    def scan(self, folder: str) -> dict[str, int]:
        stats = {"added": 0, "updated": 0, "removed": 0, "skipped": 0}
        root = Path(folder).expanduser().resolve()
        status = _Status(root.name or str(root))
        known = {r[0]: r[1] for r in self.store.db.execute(
            "SELECT path, mtime FROM indexed_files WHERE path LIKE ?", (str(root) + "%",)).fetchall()}
        seen = set()
        for p in _walk(root):
            if p.suffix.lower() not in INDEXABLE:
                continue
            sp = str(p)
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size > MAX_FILE_MB * 2 ** 20:
                continue
            if not self.allowed(sp):
                stats["skipped"] += 1
                continue
            seen.add(sp)
            mtime = st.st_mtime
            if known.get(sp) == mtime:   # unchanged since last time: nothing to do
                continue
            self._be_polite()
            try:
                text = self.reader(p)
            except Exception as e:  # damaged or locked files are normal on a real disk: not worth a warning
                log.debug("cannot read %s: %s", p, e)
                continue
            if sp in known:
                self.store.delete_document(sp)
                stats["updated"] += 1
            else:
                stats["added"] += 1
            status.update(stats["added"] + stats["updated"], p.name)
            n = self.store.add_document(text, sp)
            with self.store.lock:
                self.store.db.execute("INSERT OR REPLACE INTO indexed_files VALUES (?,?,?)", (sp, mtime, n))
                self.store.db.commit()
            if self.on_new_text:
                self.on_new_text(text, sp)
        for gone in set(known) - seen:
            self.store.delete_document(gone)
            with self.store.lock:
                self.store.db.execute("DELETE FROM indexed_files WHERE path=?", (gone,))
                self.store.db.commit()
            stats["removed"] += 1
        status.done(stats)
        return stats


    def _be_polite(self) -> None:
        """Let chat replies go first: wait while one is being made, and breathe between files."""
        waited = 0.0
        while self.busy() and waited < 120 and not self._stop.is_set():
            time.sleep(0.25)
            waited += 0.25
        if self.yield_s:
            time.sleep(self.yield_s)

    def prune(self, roots: list[str]) -> int:
        """Forget indexed files inside skipped folders (like $Recycle.Bin) or outside the indexed roots."""
        resolved = [str(Path(r).expanduser().resolve()).lower() for r in roots]
        rows = self.store.db.execute("SELECT path FROM indexed_files").fetchall()
        gone = [r[0] for r in rows
                if skipped_path(r[0]) or not any(r[0].lower().startswith(root) for root in resolved)]
        for path in gone:
            self.store.delete_document(path)
            with self.store.lock:
                self.store.db.execute("DELETE FROM indexed_files WHERE path=?", (path,))
                self.store.db.commit()
        if gone:
            log.info("Removed %d file(s) in system or skipped folders from search", len(gone))
        return len(gone)

    def start(self, folders: list[str], interval_s: int = 300) -> threading.Thread:
        """Index now and then every interval, all in the background, so the app is usable at once."""
        self.folders = folders
        self.yield_s = 0.02

        def loop():
            while not self._stop.is_set():
                for f in self.folders:
                    try:
                        self.scan(f)
                    except FileNotFoundError:
                        log.warning("Folder to index %s does not exist yet.", f)
                    except Exception as e:  # pragma: no cover
                        log.warning("index scan failed for %s: %s", f, e)
                if self._stop.wait(interval_s):
                    break

        t = threading.Thread(target=loop, daemon=True, name="aether-indexer")
        t.start()
        return t

    def watch(self, folders: list[str], interval_s: int = 300) -> threading.Thread:
        self.folders = folders

        def loop():
            while not self._stop.wait(interval_s):
                for f in self.folders:
                    try:
                        self.scan(f)
                    except Exception as e:
                        log.warning("watch scan failed for %s: %s", f, e)
        t = threading.Thread(target=loop, daemon=True, name="aether-watcher")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()



class _Status:
    """One console line that updates in place while a folder is indexed, then a one-line summary."""

    def __init__(self, name: str):
        self.name, self.shown, self.last = name, False, 0.0
        self.tty = bool(getattr(sys.stderr, "isatty", lambda: False)())

    def update(self, n: int, current: str) -> None:
        now = time.monotonic()
        if not self.tty or now - self.last < 0.2:
            return
        self.last, self.shown = now, True
        sys.stderr.write(f"\rIndexing {self.name}: {n + 1} files  {current[:40]:<40}")
        sys.stderr.flush()

    def done(self, stats: dict[str, int]) -> None:
        if self.shown:
            sys.stderr.write("\r" + " " * 90 + "\r")
            sys.stderr.flush()
        if stats["added"] or stats["updated"] or stats["removed"]:
            log.info("Indexed %s: %d new, %d changed, %d removed", self.name, stats["added"], stats["updated"],
                     stats["removed"])


def _walk(root: Path):
    """Files under root, never descending into skipped folders or following links."""
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if not skipped_dir(d)]
        for name in filenames:
            if not name.startswith(("~$", ".")):
                yield Path(dirpath) / name
