"""Turn a streaming answer into speakable sentences as soon as each one is complete.

Speaking sentence by sentence while the model is still writing makes the gap
between "you stop talking" and "AETHER starts talking" roughly the time to the
first sentence, instead of the time to the whole answer. Code blocks and tables
are skipped (they are on screen); very short fragments are merged so the voice
does not stutter.
"""
from __future__ import annotations

import re

from .tts import speakable

# A sentence ends at . ! ? or … followed by whitespace (so 3.5 and e.g.x stay intact), or at a blank line.
# A list item ("2. Gym") is not a sentence end, but the line break before the next item is.
# Titles and common abbreviations ("Prof. Rao", "e.g. this") are not sentence ends either.
ABBR = r"(?<!\bMr)(?<!\bMs)(?<!\bDr)(?<!\bSt)(?<!\bSr)(?<!\bJr)(?<!\bvs)(?<!\bMrs)(?<!\bProf)(?<!\be\.g)(?<!\bi\.e)(?<!\betc)"
END = re.compile(r"(?<!\n\d)(?<!\n\d\d)(?<!^\d)(?:" + ABBR + r"\.|[!?…])[\"')\]]*\s+|\n\s*\n|\n(?=\s*(?:[-*•]|\d+[.)])\s)")
TABLE_LINE = re.compile(r"^\s*\|.*$", re.M)


class SentenceChunker:
    def __init__(self, min_chars: int = 24):
        self.min_chars = min_chars
        self.buf = ""
        self.in_code = False

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out: list[str] = []
        while True:
            if self.in_code:
                i = self.buf.find("```")
                if i < 0:
                    self.buf = self.buf[-2:]  # keep a possible split fence
                    return out
                end = self.buf.find("\n", i + 3)
                if end < 0:
                    return out  # wait for the rest of the closing fence line
                self.buf, self.in_code = self.buf[end + 1:], False
                continue
            i = self.buf.find("```")
            if i >= 0:
                out += self._sentences(self.buf[:i], final=True)
                self.buf, self.in_code = self.buf[i + 3:], True
                continue
            out += self._sentences(None, final=False)
            return out

    def flush(self) -> list[str]:
        if self.in_code:
            self.buf, self.in_code = "", False
            return []
        out = self._sentences(self.buf, final=True)
        self.buf = ""
        return out

    def _sentences(self, text: str | None, final: bool) -> list[str]:
        """Split complete sentences off `text` (or off the buffer when text is None)."""
        src = self.buf if text is None else text
        out, start = [], 0
        for m in END.finditer(src):
            piece = src[start:m.end()]
            if len(piece.strip()) < self.min_chars:
                continue  # too short on its own: merge with what follows
            spoken = clean(piece)
            if spoken:
                out.append(spoken)
            start = m.end()
        rest = src[start:]
        if final:
            spoken = clean(rest)
            if spoken:
                out.append(spoken)
            rest = ""
        if text is None:
            self.buf = rest
        return out


def clean(text: str) -> str:
    return re.sub(r"\s+([.,!?;:])", r"\1", speakable(TABLE_LINE.sub(" ", text)))
