"""The event bus: one live picture of what AETHER is doing, shared by every part of him.

The voice listener, the server, the HUD, Nova and the phone run as separate processes (so a slow model never
freezes the microphone or the screen). They meet here: anything can publish an event ("partial": what you're
saying right now, "presence": idle/listening/thinking/speaking, "stage": which part of the pipeline is running,
"reply", "notify"), and anything can follow the stream (the server serves it as Server-Sent Events at
/api/events; the voice process publishes with POST /api/events).

Events live in a small ring buffer, so a page that reconnects picks up where it left off.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass
class Event:
    seq: int
    kind: str
    data: dict
    at: float

    def json(self) -> dict:
        return {"seq": self.seq, "kind": self.kind, "data": self.data, "at": self.at}


KINDS = {"partial", "heard", "presence", "stage", "reply", "notify", "world", "device", "ack"}


class EventBus:
    def __init__(self, size: int = 400):
        self._events: deque[Event] = deque(maxlen=size)
        self._seq = 0
        self._cond = threading.Condition()

    def publish(self, kind: str, data: dict | None = None) -> Event:
        with self._cond:
            self._seq += 1
            e = Event(self._seq, kind, dict(data or {}), time.time())
            self._events.append(e)
            self._cond.notify_all()
            return e

    @property
    def seq(self) -> int:
        return self._seq

    def since(self, seq: int, kinds: set[str] | None = None) -> list[Event]:
        with self._cond:
            return [e for e in self._events if e.seq > seq and (not kinds or e.kind in kinds)]

    def wait(self, seq: int, timeout: float = 15.0, kinds: set[str] | None = None) -> list[Event]:
        """Events after ``seq``, waiting up to ``timeout`` seconds for the first one."""
        end = time.time() + timeout
        with self._cond:
            while True:
                out = [e for e in self._events if e.seq > seq and (not kinds or e.kind in kinds)]
                left = end - time.time()
                if out or left <= 0:
                    return out
                self._cond.wait(left)

    def last(self, kind: str) -> Event | None:
        with self._cond:
            return next((e for e in reversed(self._events) if e.kind == kind), None)
