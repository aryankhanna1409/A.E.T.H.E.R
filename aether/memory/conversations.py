"""Conversation list for the UI: each chat session gets a title, a time and a
searchable history. Message text stays in the encrypted `turns` table."""
from __future__ import annotations

import sqlite3
import time

SCHEMA = """CREATE TABLE IF NOT EXISTS conversations (
    session TEXT PRIMARY KEY, title TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
    pinned INTEGER DEFAULT 0)"""


def _title(text: str) -> str:
    t = " ".join(text.split())
    return (t[:48] + "…") if len(t) > 48 else t or "New conversation"


class Conversations:
    def __init__(self, db: sqlite3.Connection, lock, cipher):
        self.db, self.lock, self.cipher = db, lock, cipher
        with lock:
            db.execute(SCHEMA)
            db.commit()

    def touch(self, session: str, first_text: str) -> None:
        now = time.time()
        with self.lock:
            self.db.execute("INSERT INTO conversations(session, title, created, updated) VALUES (?,?,?,?) "
                            "ON CONFLICT(session) DO UPDATE SET updated=excluded.updated",
                            (session, self.cipher.encrypt(_title(first_text)), now, now))
            self.db.commit()

    def list(self, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT c.session, c.title, c.created, c.updated, c.pinned, "
                                   "(SELECT COUNT(*) FROM turns t WHERE t.session=c.session) FROM conversations c "
                                   "ORDER BY c.pinned DESC, c.updated DESC LIMIT ?", (limit,)).fetchall()
        return [{"session": s, "title": self.cipher.decrypt(t), "created": c, "updated": u, "pinned": bool(p),
                 "messages": n} for s, t, c, u, p, n in rows]

    def history(self, session: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT role, text, created, topic FROM turns WHERE session=? ORDER BY id",
                                   (session,)).fetchall()
        return [{"role": r, "text": self.cipher.decrypt(t), "t": c, "topic": tp} for r, t, c, tp in rows]

    def rename(self, session: str, title: str) -> None:
        with self.lock:
            self.db.execute("UPDATE conversations SET title=? WHERE session=?", (self.cipher.encrypt(_title(title)), session))
            self.db.commit()

    def pin(self, session: str, pinned: bool) -> None:
        with self.lock:
            self.db.execute("UPDATE conversations SET pinned=? WHERE session=?", (int(pinned), session))
            self.db.commit()

    def delete(self, session: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM turns WHERE session=?", (session,))
            self.db.execute("DELETE FROM conversations WHERE session=?", (session,))
            self.db.commit()

    def search(self, query: str, limit: int = 30) -> list[dict]:
        """Full-text search over decrypted history (fine for a personal assistant's volume)."""
        q = query.lower().strip()
        if not q:
            return []
        titles = {c["session"]: c["title"] for c in self.list(500)}
        with self.lock:
            rows = self.db.execute("SELECT session, role, text, created FROM turns ORDER BY id DESC LIMIT 5000").fetchall()
        out = []
        for s, r, t, c in rows:
            text = self.cipher.decrypt(t)
            i = text.lower().find(q)
            if i >= 0:
                out.append({"session": s, "title": titles.get(s, ""), "role": r, "t": c,
                            "snippet": text[max(0, i - 60): i + 100]})
                if len(out) >= limit:
                    break
        return out
