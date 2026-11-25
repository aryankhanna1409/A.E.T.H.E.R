"""Structured logging plus the user-visible activity/audit log."""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("aether")
# pypdf warns about every damaged PDF it meets ("EOF marker not found"); AETHER just skips those files.
for _noisy in ("pypdf", "PyPDF2", "pdfminer"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)


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
