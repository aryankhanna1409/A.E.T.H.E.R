"""Topic tracking so AETHER can switch topics on the fly and come back later.

Each topic keeps its own short history. A message that is dissimilar to the
current topic (or that uses an explicit switch phrase) opens or resumes a topic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..retrieval.lexical import sparse_cosine as cosine, vectorize

SWITCH_PHRASES = re.compile(
    r"^(anyway|btw|by the way|new topic|change of topic|switching gears|unrelated|on another note|"
    r"different question|also,)\b", re.I)
RESUME_PHRASES = re.compile(r"\b(back to|going back to|return to|as we were saying about)\b", re.I)


@dataclass
class Topic:
    id: int
    label: str
    centroid: dict[str, float] = field(default_factory=dict)
    turns: int = 0

    def absorb(self, vec: dict[str, float]) -> None:
        self.turns += 1
        w = 1 / self.turns
        keys = set(self.centroid) | set(vec)
        self.centroid = {k: (1 - w) * self.centroid.get(k, 0) + w * vec.get(k, 0) for k in keys}


class TopicTracker:
    def __init__(self, threshold: float = 0.08):
        self.threshold = threshold
        self.topics: list[Topic] = []
        self.current: Topic | None = None

    def observe(self, text: str) -> tuple[Topic, str]:
        """Returns (topic, event) where event is 'continue' | 'new' | 'resume'."""
        vec = vectorize(text)
        if self.current is None:
            return self._new(text, vec), "new"

        explicit_switch = bool(SWITCH_PHRASES.search(text.strip()))
        sim_current = cosine(vec, self.current.centroid)
        # Short follow-ups ("why?", "and then?") stay on topic.
        if not explicit_switch and (len(vec) <= 2 or sim_current >= self.threshold):
            self.current.absorb(vec)
            return self.current, "continue"

        best = max((t for t in self.topics if t is not self.current),
                   key=lambda t: cosine(vec, t.centroid), default=None)
        if best and (cosine(vec, best.centroid) >= self.threshold * 1.5 or RESUME_PHRASES.search(text)):
            self.current = best
            best.absorb(vec)
            return best, "resume"
        return self._new(text, vec), "new"

    def _new(self, text: str, vec: dict[str, float]) -> Topic:
        label = " ".join(sorted(vec, key=vec.get, reverse=True)[:3]) or text[:30]
        topic = Topic(len(self.topics) + 1, label)
        topic.absorb(vec)
        self.topics.append(topic)
        self.current = topic
        return topic


@dataclass
class WorkingMemory:
    """Short-term context: recent turns per topic, active goals, scratch variables."""
    max_turns: int = 12
    turns: dict[int, list[tuple[str, str]]] = field(default_factory=dict)
    active_goals: list[str] = field(default_factory=list)
    scratch: dict[str, str] = field(default_factory=dict)
    wellness_mode: str | None = None

    def add(self, topic_id: int, role: str, text: str) -> None:
        self.turns.setdefault(topic_id, []).append((role, text))
        self.turns[topic_id] = self.turns[topic_id][-self.max_turns:]

    def history(self, topic_id: int) -> list[tuple[str, str]]:
        return list(self.turns.get(topic_id, []))
