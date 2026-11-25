"""Whole-drive indexing: system and junk folders skipped, incremental re-scans, polite to chat."""
import logging
import time

from aether.retrieval.watcher import index_roots, skipped_path


def make_drive(root):
    files = {
        "Users/aryan/Documents/notes.md": "Robotics exam plan for RSD",
        "Users/aryan/Desktop/todo.txt": "Buy coffee beans",
        "Projects/sar/readme.md": "Search and rescue rover",
        "$Recycle.Bin/S-1-5-21/$IDRXI3W.pdf": "junk",
        "Windows/System32/notes.txt": "system",
        "Program Files/App/readme.txt": "program",
        "Users/aryan/AppData/Local/x.txt": "cache",
        "Projects/sar/.venv/lib/site.txt": "venv",
        "Projects/web/node_modules/pkg/readme.md": "dependency",
        "Projects/sar/.git/HEAD.txt": "git",
        "Users/aryan/Documents/~$draft.docx": "office lock file",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def watcher(a):
    w = a.watcher()
    w.allowed = lambda p: True   # the test folders stand in for a drive the user allowed
    return w


def indexed(a):
    return sorted(r[0] for r in a.store.db.execute("SELECT path FROM indexed_files"))


def test_skips_system_and_junk_folders(make_aether, tmp_path):
    a = make_aether()
    drive = make_drive(tmp_path / "C")
    stats = watcher(a).scan(str(drive))
    names = [p.replace("\\", "/").split("/C/")[1] for p in indexed(a)]
    assert names == ["Projects/sar/readme.md", "Users/aryan/Desktop/todo.txt", "Users/aryan/Documents/notes.md"]
    assert stats["added"] == 3


def test_rescan_is_incremental(make_aether, tmp_path):
    a = make_aether()
    drive = make_drive(tmp_path / "C")
    w = watcher(a)
    w.scan(str(drive))
    reads = []
    w.reader = lambda p: reads.append(p.name) or p.read_text()
    assert w.scan(str(drive)) == {"added": 0, "updated": 0, "removed": 0, "skipped": 0} and reads == []
    note = drive / "Users/aryan/Desktop/todo.txt"
    note.write_text("Buy coffee beans and oat milk")
    import os
    os.utime(note, (time.time() + 5, time.time() + 5))
    assert w.scan(str(drive))["updated"] == 1 and reads == ["todo.txt"]


def test_prune_forgets_files_already_indexed_from_junk_folders(make_aether, tmp_path):
    a = make_aether()
    w = watcher(a)
    junk = tmp_path / "C" / "$Recycle.Bin" / "old.txt"
    keep = tmp_path / "C" / "Docs" / "keep.txt"
    for p in (junk, keep):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("hello world text")
        n = a.store.add_document(p.read_text(), str(p))
        a.store.db.execute("INSERT INTO indexed_files VALUES (?,?,?)", (str(p), p.stat().st_mtime, n))
    assert w.prune([str(tmp_path / "C")]) == 1
    assert indexed(a) == [str(keep)]


def test_skipped_path_and_roots():
    assert skipped_path("C:/$Recycle.Bin/x/a.pdf".replace("/", "\\")) or skipped_path("/C/$Recycle.Bin/x/a.pdf")
    assert skipped_path("/C/Users/a/AppData/Roaming/x.txt") and not skipped_path("/C/Users/a/Documents/x.txt")
    assert index_roots(["C:/"], []) == ["C:/"] and index_roots(["C:/"], ["D:/Work"]) == ["D:/Work"]


def test_indexer_waits_while_a_reply_is_being_made(make_aether, tmp_path):
    a = make_aether()
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "a.txt").write_text("some text to index")
    w = watcher(a)
    a._active_turns = 1
    started = time.time()
    import threading
    t = threading.Thread(target=w.scan, args=(str(tmp_path / "d"),))
    t.start()
    time.sleep(0.6)
    assert indexed(a) == []          # still waiting for the reply to finish
    a._active_turns = 0
    t.join(5)
    assert len(indexed(a)) == 1 and time.time() - started >= 0.5


def test_background_start_returns_at_once(make_aether, tmp_path):
    a = make_aether()
    make_drive(tmp_path / "C")
    w = watcher(a)
    t0 = time.time()
    w.start([str(tmp_path / "C")], interval_s=3600)
    assert time.time() - t0 < 0.5
    for _ in range(100):
        if len(indexed(a)) == 3:
            break
        time.sleep(0.05)
    w.stop()
    assert len(indexed(a)) == 3


def test_pypdf_warnings_are_quiet():
    import aether.core.logging  # noqa: F401
    assert logging.getLogger("pypdf").getEffectiveLevel() >= logging.ERROR
