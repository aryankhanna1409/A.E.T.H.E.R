"""A fingerprint of the code on disk, so a process started before a `git pull` can tell it is out of date.

The tray compares its own fingerprint with the running server's: after an update the old server (and the old
voice listener and Nova) are replaced instead of quietly running yesterday's code.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]   # the aether/ package folder
MARK = "Mark XXVIII"   # the release name (version 28)
VERSION = "28.0.0"


@lru_cache(maxsize=1)
def code_version() -> str:
    """Short hash of every .py and web file's path, size and modification time. Computed once per process."""
    h = hashlib.sha1()
    for p in sorted(PACKAGE.rglob("*")):
        if p.suffix in {".py", ".js", ".html", ".css"} and "__pycache__" not in p.parts:
            st = p.stat()
            h.update(f"{p.relative_to(PACKAGE).as_posix()}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:12]
