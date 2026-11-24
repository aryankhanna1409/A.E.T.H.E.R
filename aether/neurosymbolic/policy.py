"""Deterministic policy engine. Every tool call is evaluated here *before*
execution. The LLM can propose actions; it can never override these rules.

Decisions: ALLOW (run), CONFIRM (ask the user first), DENY (refuse with a reason).
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PureWindowsPath

from ..core.config import SecurityConfig


class Decision(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


# Permission categories from the spec, lowest to highest risk.
RISK = {
    "fs.read": 1, "fs.list": 1, "fs.search": 1, "memory": 1, "kg": 1, "optimize": 1,
    "routine": 1, "calendar": 1,
    "calendar.write": 2,  # only ever AETHER's own aether.ics; asks unless you auto-approve it  # running a routine is harmless by itself: every step inside it is checked on its own
    "web.search": 2, "web.fetch": 2,
    "fs.create": 3, "fs.modify": 4, "app.open": 4, "autonomy": 3, "fs.move": 4,
    "fs.delete": 5, "shell": 5, "ui.automation": 5, "screen.observe": 4, "message.send": 5,
}
ALWAYS_CONFIRM = {"fs.delete", "shell", "ui.automation", "message.send", "fs.modify", "fs.move",
                  "screen.observe", "app.open", "autonomy"}


@dataclass
class PolicyResult:
    decision: Decision
    reason: str


class PolicyEngine:
    def __init__(self, sec: SecurityConfig):
        self.sec = sec
        self.emergency_stop = False

    def _inside(self, path: Path, roots: list[str]) -> bool:
        return any(path == Path(r) or Path(r) in path.parents for r in roots)

    def _secret(self, path: Path) -> str | None:
        parts_lower = [p.lower() for p in path.parts]
        joined = str(PureWindowsPath(path)).lower()
        for d in self.sec.denylist_dirs:
            d_low = d.lower()
            if d_low in parts_lower or d_low.replace("/", "\\") in joined:
                return d
        for g in self.sec.denylist_globs:
            if fnmatch.fnmatch(path.name.lower(), g.lower()):
                return g
        return None

    def evaluate(self, category: str, path: str | None = None, detail: str = "") -> PolicyResult:
        if self.emergency_stop:
            return PolicyResult(Decision.DENY, "Emergency stop is active. Say 'resume' to re-enable actions.")
        if category not in RISK:
            return PolicyResult(Decision.DENY, f"Unknown action category '{category}'.")
        if category.startswith("web.") and not self.sec.network:
            return PolicyResult(Decision.DENY, "Network access is turned off in settings.")
        if path is not None:
            p = Path(path).expanduser().resolve()
            hit = self._secret(p)
            if hit:
                return PolicyResult(Decision.DENY, f"{p.name} matches the secrets denylist ({hit}); never accessible.")
            write = category in {"fs.create", "fs.modify", "fs.delete", "fs.move", "shell"}
            roots = self.sec.allowed_write_roots if write else self.sec.allowed_read_roots + self.sec.allowed_write_roots
            if not self._inside(p, roots):
                kind = "write to" if write else "read"
                return PolicyResult(Decision.DENY, f"Not authorized to {kind} {p}. Allowed: {roots or 'none yet'}.")
        if category in ALWAYS_CONFIRM:
            return PolicyResult(Decision.CONFIRM, f"{category} always needs your approval. {detail}".strip())
        if category in self.sec.auto_approve or RISK[category] <= 1:
            return PolicyResult(Decision.ALLOW, "low-risk and auto-approved")
        return PolicyResult(Decision.CONFIRM, f"{category} needs approval. {detail}".strip())
