"""Structured logging plus the user-visible activity/audit log."""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("aether")
# pypdf warns about every damaged PDF it meets ("EOF marker not found"); AETHER just skips those files.
for _noisy in ("pypdf", "PyPDF2", "pdfminer"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)


def log_dir() -> Path:
    from .config import default_home
    return default_home() / "logs"


def ensure_streams(name: str) -> Path | None:
    """Under pythonw (no console) sys.stdout/stderr are None, and libraries that print or check isatty() crash.
    Point them at %LOCALAPPDATA%\\AETHER\\logs\\<name>.log instead. Returns the log path, or None if not needed."""
    if sys.stdout is not None and sys.stderr is not None:
        return None
    path = log_dir() / f"{name}.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 5_000_000:
            path.replace(path.with_suffix(".old.log"))
        f = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        import os
        f = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f
    return path


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class AuditLog:
    """Append-only JSONL of everything AETHER accessed or changed."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def record(self, action: str, target: str, allowed: bool, reason: str, **extra) -> None:
        entry = {"t": time.time(), "action": action, "target": target, "allowed": allowed,
                 "reason": reason, **extra}
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        log.info("audit %s %s allowed=%s (%s)", action, target, allowed, reason)

    def tail(self, n: int = 50) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(line) for line in lines]
