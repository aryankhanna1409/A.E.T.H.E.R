"""Incremental indexing of authorized folders: new and changed files are
(re)indexed, deleted files are removed from memory. Runs on demand or in a
background thread."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from ..core.logging import log

INDEXABLE = {".txt", ".md", ".py", ".json", ".csv", ".toml", ".yaml", ".yml", ".html", ".js", ".ts", ".tsx",
             ".java", ".c", ".cpp", ".h", ".rs", ".go", ".tex", ".rst", ".pdf", ".docx"}


class FolderWatcher:
    def __init__(self, store, allowed: Callable[[str], bool], reader: Callable[[Path], str],
                 on_new_text: Callable[[str, str], None] | None = None):
        self.store, self.allowed, self.reader, self.on_new_text = store, allowed, reader, on_new_text
        with store.lock:
            store.db.execute("CREATE TABLE IF NOT EXISTS indexed_files (path TEXT PRIMARY KEY, mtime REAL, chunks INTEGER)")
            store.db.commit()
        self._stop = threading.Event()
        self.folders: list[str] = []

    def scan(self, folder: str) -> dict[str, int]:
        stats = {"added": 0, "updated": 0, "removed": 0, "skipped": 0}
        root = Path(folder).expanduser().resolve()
        known = {r[0]: r[1] for r in self.store.db.execute(
            "SELECT path, mtime FROM indexed_files WHERE path LIKE ?", (str(root) + "%",)).fetchall()}
        seen = set()
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in INDEXABLE:
                continue
            sp = str(p)
            if not self.allowed(sp):
                stats["skipped"] += 1
                continue
            seen.add(sp)
            mtime = p.stat().st_mtime
            if known.get(sp) == mtime:
                continue
            try:
                text = self.reader(p)
            except Exception as e:
                log.warning("cannot read %s: %s", p, e)
                continue
            if sp in known:
                self.store.delete_document(sp)
                stats["updated"] += 1
            else:
                stats["added"] += 1
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
        return stats

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
