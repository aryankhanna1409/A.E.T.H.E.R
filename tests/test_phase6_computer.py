import sys
from pathlib import Path

import pytest

from aether.neurosymbolic.policy import Decision


def call(a, tool, **args):
    return a.tools.call(tool, args)


def test_move_checks_both_paths_and_undo(make_aether, cfg):
    out = Path(cfg.security.allowed_write_roots[0])
    docs = Path(cfg.security.allowed_read_roots[0])
    (out / "a.txt").write_text("hi")
    a = make_aether()
    assert call(a, "fs_move", path=str(out / "a.txt"), destination=str(docs / "a.txt")).decision == "deny"
    r = call(a, "fs_move", path=str(out / "a.txt"), destination=str(out / "sub" / "b.txt"))
    assert r.ok and (out / "sub" / "b.txt").exists()
    assert "back" in call(a, "undo_last").output and (out / "a.txt").exists()


def test_delete_goes_to_trash_and_can_be_undone(make_aether, cfg):
    out = Path(cfg.security.allowed_write_roots[0])
    f = out / "old.md"
    f.write_text("keep me")
    a = make_aether()
    assert call(a, "fs_delete", path=str(f)).ok and not f.exists()
    call(a, "undo_last")
    assert f.read_text() == "keep me"


def test_create_and_modify_are_journaled(make_aether, cfg):
    out = Path(cfg.security.allowed_write_roots[0])
    a = make_aether()
    call(a, "fs_create", path=str(out / "n.md"), content="v1")
    call(a, "fs_modify", path=str(out / "n.md"), content="v2")
    call(a, "undo_last")
    assert (out / "n.md").read_text() == "v1"
    call(a, "undo_last")
    assert not (out / "n.md").exists()


def test_shell_sandbox(make_aether, cfg):
    out = cfg.security.allowed_write_roots[0]
    cfg.security.shell_allow = ["python", "python3"]
    asked = []
    a = make_aether(confirm=lambda p: asked.append(p) or True)
    exe = Path(sys.executable).stem
    if exe not in cfg.security.shell_allow:
        cfg.security.shell_allow.append(exe)
    r = call(a, "shell", command=f'{sys.executable} -c "print(6*7)"', cwd=out)
    assert r.ok and "42" in r.output and asked and "shell" in asked[0]
    assert "destructive" in call(a, "shell", command="git push -f", cwd=out).output
    assert "not allowed" in call(a, "shell", command="python -c 1 && whoami", cwd=out).output
    assert "allowlist" in call(a, "shell", command="powershell Get-Process", cwd=out).output
    assert call(a, "shell", command="python -V", cwd=cfg.security.allowed_read_roots[0]).decision == "deny"


def test_screen_and_ui_are_off_by_default(make_aether):
    a = make_aether()
    assert "off" in call(a, "screen_read").output
    assert "off" in call(a, "ui_type", window="Notepad", text="hi").output


def test_apps_allowlist(make_aether, cfg):
    cfg.security.open_any_app = False   # allowlist only
    a = make_aether()
    assert "not in the allowed apps" in call(a, "app_open", name="regedit").output
    assert a.policy.evaluate("app.open").decision == Decision.CONFIRM
