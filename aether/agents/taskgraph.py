"""Persistent task graph: goals split into tasks with dependencies, status,
attempts, checkpoints and results, all in SQLite so work survives restarts."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field

STATUSES = {"pending", "running", "done", "failed", "cancelled", "blocked", "replaced"}


@dataclass
class Task:
    id: int
    goal_id: int
    agent: str
    description: str
    depends_on: list[int] = field(default_factory=list)
    success_criteria: str = ""
    status: str = "pending"
    attempts: int = 0
    max_attempts: int = 2
    result: str = ""
    checkpoint: dict = field(default_factory=dict)


class TaskGraph:
    def __init__(self, db: sqlite3.Connection, lock: threading.Lock):
        self.db, self.lock = db, lock
        with lock:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS goals (id INTEGER PRIMARY KEY, text TEXT, status TEXT, created REAL,
                                              finished REAL, report TEXT);
            CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, goal_id INTEGER, agent TEXT, description TEXT,
                depends_on TEXT, success_criteria TEXT, status TEXT, attempts INTEGER, max_attempts INTEGER,
                result TEXT, checkpoint TEXT, updated REAL);
            """)
            db.commit()

    def new_goal(self, text: str) -> int:
        with self.lock:
            gid = self.db.execute("INSERT INTO goals(text,status,created) VALUES (?, 'planning', ?)",
                                  (text, time.time())).lastrowid
            self.db.commit()
        return gid

    def add_task(self, goal_id: int, agent: str, description: str, depends_on: list[int],
                 success_criteria: str = "", max_attempts: int = 2) -> int:
        with self.lock:
            tid = self.db.execute(
                "INSERT INTO tasks(goal_id,agent,description,depends_on,success_criteria,status,attempts,max_attempts,"
                "result,checkpoint,updated) VALUES (?,?,?,?,?,'pending',0,?,'','{}',?)",
                (goal_id, agent, description, json.dumps(depends_on), success_criteria, max_attempts, time.time())).lastrowid
            self.db.commit()
        return tid

    def tasks(self, goal_id: int) -> list[Task]:
        rows = self.db.execute("SELECT id,goal_id,agent,description,depends_on,success_criteria,status,attempts,"
                               "max_attempts,result,checkpoint FROM tasks WHERE goal_id=? ORDER BY id", (goal_id,)).fetchall()
        return [Task(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5], r[6], r[7], r[8], r[9], json.loads(r[10] or "{}"))
                for r in rows]

    def update(self, task: Task) -> None:
        with self.lock:
            self.db.execute("UPDATE tasks SET status=?, attempts=?, result=?, checkpoint=?, updated=? WHERE id=?",
                            (task.status, task.attempts, task.result, json.dumps(task.checkpoint), time.time(), task.id))
            self.db.commit()

    def set_goal(self, goal_id: int, status: str, report: str | None = None) -> None:
        with self.lock:
            self.db.execute("UPDATE goals SET status=?, report=COALESCE(?, report), finished=? WHERE id=?",
                            (status, report, time.time() if status in {"done", "failed", "cancelled"} else None, goal_id))
            self.db.commit()

    def goal(self, goal_id: int) -> dict:
        r = self.db.execute("SELECT id,text,status,report FROM goals WHERE id=?", (goal_id,)).fetchone()
        return {"id": r[0], "text": r[1], "status": r[2], "report": r[3]} if r else {}

    def goals(self, limit: int = 20) -> list[dict]:
        rows = self.db.execute("SELECT id,text,status FROM goals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r[0], "text": r[1], "status": r[2]} for r in rows]

    def ready(self, goal_id: int) -> list[Task]:
        ts = self.tasks(goal_id)
        done = {t.id for t in ts if t.status == "done"}
        return [t for t in ts if t.status == "pending" and all(d in done for d in t.depends_on)]

    @staticmethod
    def has_cycle(edges: dict[int, list[int]]) -> bool:
        state: dict[int, int] = {}

        def visit(n: int) -> bool:
            if state.get(n) == 1:
                return True
            if state.get(n) == 2:
                return False
            state[n] = 1
            if any(visit(m) for m in edges.get(n, [])):
                return True
            state[n] = 2
            return False
        return any(visit(n) for n in edges)
