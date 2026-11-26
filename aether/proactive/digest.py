"""Keeping you up to date: a personal "while you were away" digest and a watchlist.

Digest
    Headlines from your feeds plus Google News searches for what you're working on (your plan's current focus,
    the interests you set, the names and terms you use most), ranked by how close each one is to those interests
    (embedding similarity when the embedding model is loaded, word overlap otherwise). The same story from three
    outlets becomes one line, and anything already shown is skipped, so it really is what's new since you looked.

Watchlist
    "Keep an eye on the AWS MLA-C01 exam", "watch for LangGraph releases", "stop watching X", "what are you
    watching?". Every few hours AETHER searches the news for each watch and tells you only when something new turns
    up, as a normal nudge (quiet hours and limits apply). Nothing runs when the network is off.

Headlines are untrusted text: anything that reads like instructions is dropped before it is shown or spoken.
"""
from __future__ import annotations

import difflib
import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime

from ..core.logging import log
from ..retrieval.lexical import tokens
from ..security import injection

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (id INTEGER PRIMARY KEY, query TEXT UNIQUE, created REAL, last_check REAL DEFAULT 0,
                                    active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS seen_news (key TEXT PRIMARY KEY, watch INTEGER, seen REAL);
"""


def story_key(title: str) -> str:
    return hashlib.sha1(re.sub(r"\W+", " ", title.lower()).strip()[:80].encode()).hexdigest()[:16]


@dataclass
class Story:
    title: str
    sources: list[str]
    link: str
    published: datetime | None
    score: float = 0.0
    why: str = ""


def cluster(headlines) -> list[Story]:
    """One story per event: headlines whose titles are near-identical (or share most words) are merged."""
    out: list[Story] = []
    for h in headlines:
        t = re.sub(r"\s+", " ", h.title).strip()
        tw = set(tokens(t))
        for s in out:
            sw = set(tokens(s.title))
            jac = len(tw & sw) / max(len(tw | sw), 1)
            if jac >= 0.6 or difflib.SequenceMatcher(a=t.lower(), b=s.title.lower()).ratio() >= 0.82:
                if h.source and h.source not in s.sources:
                    s.sources.append(h.source)
                break
        else:
            out.append(Story(t, [h.source] if h.source else [], h.link, h.published))
    return out


class Digest:
    def __init__(self, aether):
        self.a = aether
        with aether.lock:
            aether.db.executescript(SCHEMA)
            aether.db.commit()

    # ---------------------------------------------------------------- interests
    def interests(self, limit: int = 8) -> list[str]:
        """What you care about right now, most specific first: settings, plan focus, your own terms."""
        out: list[str] = list(getattr(self.a.cfg.online, "interests", []) or [])
        plans = getattr(self.a, "plans", None)
        try:
            t = plans.today() if plans else {}
            for item in (t.get("focus") or [])[:4]:
                core = re.split(r"[(:]", item)[0].strip()
                if 3 <= len(core) <= 60:
                    out.append(core)
        except Exception:
            pass
        try:
            from ..speech.profile import terms_from_memories
            out += [w for w in terms_from_memories(self.a.store, 10) if len(w) > 3][:4]
        except Exception:
            pass
        seen, uniq = set(), []
        for x in out:
            k = x.lower()
            if k not in seen:
                seen.add(k)
                uniq.append(x)
        return uniq[:limit]

    def _scorer(self, interests: list[str]):
        emb = getattr(self.a.store, "embedder", None)
        if emb is not None and interests:
            try:
                import numpy as np
                iv = np.asarray(emb.embed(interests), dtype="float32")
                iv /= np.linalg.norm(iv, axis=1, keepdims=True) + 1e-9

                def score(titles: list[str]) -> list[tuple[float, str]]:
                    tv = np.asarray(emb.embed(titles), dtype="float32")
                    tv /= np.linalg.norm(tv, axis=1, keepdims=True) + 1e-9
                    sims = tv @ iv.T
                    return [(float(r.max()), interests[int(r.argmax())]) for r in sims]
                return score
            except Exception as e:
                log.info("digest: embedding scorer unavailable (%s)", e)
        ivs = [(set(tokens(i)), i) for i in interests]

        def score(titles: list[str]) -> list[tuple[float, str]]:
            res = []
            for t in titles:
                tw = set(tokens(t))
                best = max(((len(tw & w) / max(len(w), 1), i) for w, i in ivs), default=(0.0, ""))
                res.append(best)
            return res
        return score

    # ---------------------------------------------------------------- digest
    def build(self, limit: int = 6, mark_seen: bool = True, topics: int = 3) -> list[Story]:
        online = getattr(self.a, "online", None)
        if online is None or not self.a.cfg.security.network:
            return []
        interests = self.interests()
        raw = []
        try:
            raw += online.headlines("", 30)
        except Exception as e:
            log.info("digest: feeds failed: %s", e)
        for topic in interests[:topics]:
            try:
                raw += online.headlines(topic, 8)
            except Exception as e:
                log.info("digest: topic %s failed: %s", topic, e)
        raw = [h for h in raw if not injection.scan(h.title)]
        stories = cluster(raw)
        with self.a.lock:
            seen = {r[0] for r in self.a.db.execute("SELECT key FROM seen_news WHERE watch IS NULL")}
        stories = [s for s in stories if story_key(s.title) not in seen]
        if not stories:
            return []
        for s, (sc, why) in zip(stories, self._scorer(interests)([s.title for s in stories])):
            fresh = 1.0
            if s.published:
                hours = max((datetime.now() - s.published).total_seconds() / 3600, 0)
                fresh = 0.5 ** (hours / 36)
            s.score = sc * 0.7 + 0.15 * fresh + 0.15 * min(len(s.sources), 3) / 3
            s.why = why if sc >= 0.35 else ""
        stories.sort(key=lambda s: s.score, reverse=True)
        top = stories[:limit]
        if mark_seen:
            now = time.time()
            with self.a.lock:
                self.a.db.executemany("INSERT OR IGNORE INTO seen_news(key, watch, seen) VALUES (?, NULL, ?)",
                                      [(story_key(s.title), now) for s in top])
                self.a.db.execute("DELETE FROM seen_news WHERE seen < ?", (now - 30 * 86400,))
                self.a.db.commit()
        return top

    def for_briefing(self, limit: int = 5) -> list[Story]:
        """New stories, or (asked again the same morning) the ones it showed last time."""
        new = self.build(limit)
        if new:
            self._last = (time.time(), new)
            return new
        when, last = getattr(self, "_last", (0, []))
        return last if time.time() - when < 12 * 3600 else []

    @staticmethod
    def render(stories: list[Story], spoken: bool = False) -> str:
        if not stories:
            return "Nothing new since you last looked."
        if spoken:
            return "While you were away: " + " ".join(s.title.rstrip(".") + "." for s in stories[:3])
        return "\n".join(f"- [{injection.neutralize(s.title)}]({s.link})" + (f" · {', '.join(s.sources[:3])}" if s.sources else "")
                         + (f" · _for: {s.why}_" if s.why else "") for s in stories)

    # ---------------------------------------------------------------- watchlist
    def watch(self, query: str) -> bool:
        q = query.strip().strip(".?!")[:120]
        if len(q) < 3:
            return False
        with self.a.lock:
            self.a.db.execute("INSERT INTO watches(query, created) VALUES (?, ?) ON CONFLICT(query) DO UPDATE SET active=1",
                              (q, time.time()))
            self.a.db.commit()
        return True

    def unwatch(self, query: str) -> str | None:
        rows = self.watches()
        best = max(rows, key=lambda r: difflib.SequenceMatcher(a=query.lower(), b=r[1].lower()).ratio(), default=None)
        if not best or difflib.SequenceMatcher(a=query.lower(), b=best[1].lower()).ratio() < 0.5:
            return None
        with self.a.lock:
            self.a.db.execute("UPDATE watches SET active=0 WHERE id=?", (best[0],))
            self.a.db.commit()
        return best[1]

    def watches(self) -> list[tuple[int, str, float]]:
        return [tuple(r) for r in self.a.db.execute("SELECT id, query, last_check FROM watches WHERE active=1 ORDER BY id")]

    def check_watches(self, every_hours: float = 4, force: bool = False) -> list[tuple[str, list]]:
        """[(watch, [new headlines])] for watches due a check. The first check only records what exists."""
        online = getattr(self.a, "online", None)
        if online is None or not self.a.cfg.security.network:
            return []
        out = []
        now = time.time()
        for wid, query, last in self.watches():
            if not force and now - (last or 0) < every_hours * 3600:
                continue
            try:
                hs = [h for h in online.headlines(query, 10) if not injection.scan(h.title)]
            except Exception as e:
                log.info("watch %s failed: %s", query, e)
                continue
            with self.a.lock:
                seen = {r[0] for r in self.a.db.execute("SELECT key FROM seen_news WHERE watch=?", (wid,))}
                new = [h for h in hs if story_key(h.title) not in seen]
                self.a.db.executemany("INSERT OR IGNORE INTO seen_news(key, watch, seen) VALUES (?, ?, ?)",
                                      [(story_key(h.title), wid, now) for h in hs])
                self.a.db.execute("UPDATE watches SET last_check=? WHERE id=?", (now, wid))
                self.a.db.commit()
            if new and last:
                out.append((query, new))
        return out


WATCH = re.compile(r"^(?:please\s+)?(?:keep\s+(?:an\s+)?eye\s+on|watch(?:\s+out)?\s+for|watch|track|follow|monitor|"
                   r"let\s+me\s+know\s+(?:when|if)\s+there'?s\s+news\s+(?:about|on))\s+(.{3,120})$", re.I)
UNWATCH = re.compile(r"^(?:stop|quit)\s+(?:watching|tracking|following|monitoring)\s+(.{3,120})$", re.I)
LIST = re.compile(r"^what\s+(?:are\s+you|am\s+i)\s+(?:watching|tracking|keeping\s+an\s+eye\s+on)\??$", re.I)
AWAY = re.compile(r"^(?:what\s+did\s+i\s+miss|what'?s\s+new|catch\s+me\s+up|anything\s+new|while\s+i\s+was\s+away|"
                  r"what'?s\s+happening(?:\s+in\s+the\s+world)?|news\s+for\s+me)\??$", re.I)


def handle(d: Digest, text: str, spoken: bool = False) -> str | None:
    t = text.strip().rstrip(".!")
    if m := UNWATCH.match(t):
        q = d.unwatch(m.group(1))
        return f"Stopped watching {q}." if q else "I wasn't watching anything like that."
    if LIST.match(t):
        ws = d.watches()
        return ("I'm keeping an eye on: " + "; ".join(w[1] for w in ws) + ".") if ws else \
            "Nothing yet. Say \"keep an eye on …\" and I'll tell you when there's news."
    if (m := WATCH.match(t)) and not re.search(r"\b(?:movie|film|show|series|video|youtube)\b", t, re.I):
        q = re.sub(r"^(?:the|any)\s+", "", m.group(1).strip(), flags=re.I)
        if d.watch(q):
            return f"I'll keep an eye on {q} and tell you when something new comes up." + \
                ("" if d.a.cfg.security.network else " (Network is off right now, so I'll start once it's on.)")
    if AWAY.match(t):
        if not d.a.cfg.security.network:
            return "I'm offline right now, so I can't check the news."
        return Digest.render(d.build(), spoken)
    return None
