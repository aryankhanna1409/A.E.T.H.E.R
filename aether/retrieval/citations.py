"""Citation validation: every [web n] / [doc n] / [memory n] in an answer must
point at a source that was actually provided. Fabricated citations are
removed and reported."""
from __future__ import annotations

import re

CITE = re.compile(r"\[(web|doc|memory|source)\s*(\d+)\]", re.I)


def validate(answer: str, available: dict[str, int]) -> tuple[str, list[str]]:
    """available maps kind -> number of sources given, e.g. {'web': 5, 'doc': 2}."""
    bad: list[str] = []

    def check(m: re.Match) -> str:
        kind, n = m.group(1).lower(), int(m.group(2))
        if 1 <= n <= available.get(kind, 0):
            return m.group(0)
        bad.append(m.group(0))
        return ""
    cleaned = CITE.sub(check, answer)
    cleaned = re.sub(r"[ \t]+([.,;:])", r"\1", cleaned)
    return cleaned, bad


def used(answer: str) -> set[str]:
    return {f"{k.lower()} {n}" for k, n in CITE.findall(answer)}
