"""Memory map: everything AETHER knows about you as one graph you can explore, question and fix.

The Knowledge view shows facts. The memory map joins every memory net into one graph:

* **entities** from the knowledge graph (people, places, projects, things, and you),
* **memories** (episodic, semantic, preference, procedural, ...), linked to the entities they mention,
* **documents** you've indexed, one node per file, linked to what they talk about,
* **facts** and **derived facts** (with their proofs) as edges between entities,
* **inferred** links between memories and documents that say similar things (TF-IDF cosine, no model needed).

Then it does real graph analysis, all in code and explainable:

* **Communities** (Louvain modularity), each named after its most central members: the "areas" of your life.
* **Centrality**: PageRank and betweenness. The highest are the *god nodes* that hold your world together;
  *bridges* are the ones that connect several communities.
* **Surprising connections**: links across communities between nodes that rarely meet.
* **Suggested links**: entity pairs that share a lot of context but have no fact between them (Adamic-Adar);
  accept one and it becomes a fact.
* **Health**: near-duplicate memories (merge), orphans nothing links to, weak inferred facts, topics with
  memories but no facts.

It's cached until a memory or a fact changes, so opening the map is instant after the first build.
"""
from __future__ import annotations

import math
import random
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np

STOP = set("""a an the and or but so if then than to of for with in on at by from into onto about as is are was were be
been being am do does did done have has had having i me my mine you your yours he him his she her hers it its we us
our they them their this that these those there here what which who whom whose when where why how all any both each
few more most other some such no nor not only own same too very can could will would should may might must shall
just also really get got go going went like one two s t don didn doesn isn wasn aren weren ll ve re d m o y
yes yeah okay ok please thanks thank user assistant aether nova said says say""".split())
WORD = re.compile(r"[a-z][a-z0-9']{2,}")
PERSON_P = {"sister", "brother", "mother", "father", "mom", "dad", "friend", "best_friend", "knows", "colleague",
            "partner", "wife", "husband", "girlfriend", "boyfriend", "son", "daughter", "cousin", "aunt", "uncle",
            "mentor", "boss", "manager", "roommate", "teacher", "professor", "classmate", "grandmother",
            "grandfather", "parent", "child", "sibling", "coworker", "teammate", "advisor"}
PERSON_S = {"birthday", "works_at", "studies_at", "lives_in", "age", "phone", "email"}
PLACE_P = {"lives_in", "located_in", "born_in", "from", "based_in", "moved_to", "visited", "city", "country"}
PROJECT_P = {"works_on", "building", "studying", "learning", "project", "goal", "preparing_for", "course"}
KIND_WORDS = {"person": "person", "people": "person", "city": "place", "country": "place", "place": "place",
              "project": "project", "course": "project", "exam": "project", "goal": "project"}

# Areas of your life. Every memory lands in one, and every area hangs off you, so the map is organised around you.
# "{name}" is your name from the config.
CATEGORIES = {
    "identity": "Who {name} is", "people": "People", "health": "Health & Grind 75", "study": "Study plan & learning",
    "career": "Career", "projects": "Projects", "goals": "Goals", "prefs": "{name}'s preferences",
    "assistant": "How AETHER should work", "life": "Life & moments", "docs": "Documents",
}
_PREF = re.compile(r"\b(?:prefers?|likes?|loves?|enjoys?|hates?|dislikes?|wants?|doesn'?t\s+(?:like|want)|favou?rite|"
                   r"rather|always|never)\b", re.I)
_ASSIST = re.compile(r"\b(?:aether|nova|answers?|repl(?:y|ies)|explain|code|commits?|pull\s+requests?|credit|cloud|"
                     r"ollama|online|api\s+keys?|documents?|responses?|tone|format|step[\s-]by[\s-]step|claude)\b", re.I)
_CAT_RULES = [
    ("people", r"\b(?:father|mother|mom|dad|sister|brother|sibling|classmates?|teammates?|friends?|girlfriend|boyfriend|"
               r"cousin|family|guide|co-guide|parents?|aunt|uncle|grand\w+|roommate)\b"),
    ("health", r"\b(?:75\s*hard|protein|gym|workouts?|weight|\d+(?:\.\d)?\s*kg|bulk(?:ing)?|sleep|diet|calories?|"
               r"marathon|running|grind|water|creatine|rest\s+days?|habits?|meals?|breakfast|lift(?:ing)?|cardio)\b"),
    ("career", r"\b(?:intern(?:ship)?|fde|placement|linkedin|job|career|resume|cv|north\s+stars|offer|hired|"
               r"recruit\w*|interview)\b"),
    ("study", r"\b(?:neetcode|leetcode|courses?|deeplearning|udemy|week\s+\d|study|studies|exams?|certifi\w+|aws|"
              r"cgpa|semester|subjects?|b\.?tech|diploma|lectures?|syllabus|college|university|labs?|assignments?)\b"),
    ("projects", r"\b(?:aether|projects?|robot\w*|research|github|portfolio|thesis|paper|journal|prototype|hackathon|"
                 r"build(?:ing)?|app)\b"),
    ("identity", r"\b(?:name|lives?|age|born|laptop|windows|tattoo|called|years\s+old)\b"),
]
_CAT_RX = [(c, re.compile(rx, re.I)) for c, rx in _CAT_RULES]


def category(text: str, layer: str = "", meta: dict | None = None) -> str:
    """Which area of your life a memory belongs to (a seed pack can say so directly)."""
    c = (meta or {}).get("category")
    if c in CATEGORIES:
        return c
    if layer == "document":
        return "docs"
    if layer == "preference" or (meta or {}).get("kind") == "preference" or _PREF.search(text):
        return "assistant" if _ASSIST.search(text) else "prefs"
    for c, rx in _CAT_RX:
        if rx.search(text):
            return c
    return "life"


@dataclass
class Graph:
    nodes: dict = field(default_factory=dict)                     # id -> node dict
    adj: dict = field(default_factory=lambda: defaultdict(dict))  # id -> {id: weight}
    edges: list = field(default_factory=list)                     # dicts

    def add_edge(self, a: str, b: str, w: float, **kw) -> None:
        if a == b or a not in self.nodes or b not in self.nodes:
            return
        self.adj[a][b] = self.adj[a].get(b, 0.0) + w
        self.adj[b][a] = self.adj[b].get(a, 0.0) + w
        self.edges.append({"s": a, "t": b, "w": round(w, 3), **kw})

    def degree(self, n: str) -> int:
        return len(self.adj.get(n, {}))


# ------------------------------------------------------------------ analytics (pure functions, tested directly)
def louvain(adj: dict, seed: int = 0) -> dict:
    """Community detection by modularity (Blondel et al. 2008): local moves, then aggregate, until stable."""
    rng = random.Random(seed)
    nodes = list(adj)
    member = {n: n for n in nodes}                     # original node -> current super-node
    g = {n: dict(adj[n]) for n in nodes}
    while True:
        m2 = sum(sum(nb.values()) for nb in g.values())
        if m2 == 0:
            break
        k = {n: sum(nb.values()) for n, nb in g.items()}
        comm = {n: n for n in g}
        tot = dict(k)
        moved, improved = True, False
        order = list(g)
        while moved:
            moved = False
            rng.shuffle(order)
            for n in order:
                c0 = comm[n]
                links = defaultdict(float)
                for nb, w in g[n].items():
                    if nb != n:
                        links[comm[nb]] += w
                tot[c0] -= k[n]
                best, gain = c0, links.get(c0, 0.0) - tot[c0] * k[n] / m2
                for c, w in links.items():
                    gn = w - tot[c] * k[n] / m2
                    if gn > gain + 1e-12:
                        best, gain = c, gn
                tot[best] += k[n]
                if best != c0:
                    comm[n] = best
                    moved = improved = True
        if not improved:
            break
        for n in member:
            member[n] = comm[member[n]]
        ng: dict = defaultdict(lambda: defaultdict(float))
        for n, nb in g.items():
            for b, w in nb.items():
                ng[comm[n]][comm[b]] += w
        g = {n: dict(nb) for n, nb in ng.items()}
        for n in set(comm.values()):
            g.setdefault(n, {})
    labels = {c: i for i, c in enumerate(sorted(set(member.values()), key=lambda c: -sum(1 for v in member.values() if v == c)))}
    return {n: labels[c] for n, c in member.items()}


def modularity(adj: dict, comm: dict) -> float:
    m2 = sum(sum(nb.values()) for nb in adj.values())
    if not m2:
        return 0.0
    k = {n: sum(nb.values()) for n, nb in adj.items()}
    inside, tot = defaultdict(float), defaultdict(float)
    for n, nb in adj.items():
        tot[comm[n]] += k[n]
        for b, w in nb.items():
            if comm[b] == comm[n]:
                inside[comm[n]] += w
    return sum(inside[c] / m2 - (tot[c] / m2) ** 2 for c in tot)


def pagerank(adj: dict, d: float = 0.85, iters: int = 60) -> dict:
    nodes = list(adj)
    if not nodes:
        return {}
    ix = {n: i for i, n in enumerate(nodes)}
    n = len(nodes)
    out = np.array([sum(adj[a].values()) for a in nodes])
    r = np.full(n, 1.0 / n)
    src, dst, w = [], [], []
    for a in nodes:
        for b, x in adj[a].items():
            src.append(ix[a]); dst.append(ix[b]); w.append(x)
    src, dst, w = np.array(src, int), np.array(dst, int), np.array(w, float)
    for _ in range(iters):
        contrib = np.zeros(n)
        if len(src):
            np.add.at(contrib, dst, r[src] * w / out[src])
        dangling = r[out == 0].sum()
        new = (1 - d) / n + d * (contrib + dangling / n)
        if np.abs(new - r).sum() < 1e-9:
            r = new
            break
        r = new
    return {a: float(r[ix[a]]) for a in nodes}


def betweenness(adj: dict, samples: int = 96, seed: int = 0) -> dict:
    """Brandes' algorithm over unweighted shortest paths, from a sample of sources when the graph is large."""
    nodes = list(adj)
    bc = dict.fromkeys(nodes, 0.0)
    if len(nodes) < 3:
        return bc
    srcs = nodes if len(nodes) <= samples else random.Random(seed).sample(nodes, samples)
    for s in srcs:
        stack, pred = [], defaultdict(list)
        sigma = dict.fromkeys(nodes, 0); sigma[s] = 1
        dist = {s: 0}
        q = [s]
        i = 0
        while i < len(q):
            v = q[i]; i += 1
            stack.append(v)
            for w in adj[v]:
                if w not in dist:
                    dist[w] = dist[v] + 1
                    q.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    pred[w].append(v)
        delta = dict.fromkeys(nodes, 0.0)
        while stack:
            w = stack.pop()
            for v in pred[w]:
                delta[v] += sigma[v] / sigma[w] * (1 + delta[w])
            if w != s:
                bc[w] += delta[w]
    scale = len(nodes) / len(srcs)
    top = max(bc.values()) or 1.0
    return {n: v * scale / (top * scale) for n, v in bc.items()}


def adamic_adar(adj: dict, a: str, b: str) -> tuple[float, list[str]]:
    common = set(adj.get(a, {})) & set(adj.get(b, {}))
    score = sum(1 / math.log(max(2.0, len(adj[z]))) for z in common)
    return score, sorted(common, key=lambda z: len(adj[z]))


def tokens(text: str) -> list[str]:
    return [w.strip("'") for w in WORD.findall(text.lower()) if w.strip("'") not in STOP]


def tfidf(docs: list[list[str]], max_vocab: int = 6000) -> np.ndarray:
    df = Counter(t for d in docs for t in set(d))
    n = len(docs)
    vocab = [t for t, c in df.most_common() if c < max(2, 0.5 * n) or n < 4][:max_vocab]
    ix = {t: i for i, t in enumerate(vocab)}
    X = np.zeros((n, len(vocab)), dtype=np.float32)
    for r, d in enumerate(docs):
        for t, c in Counter(d).items():
            if t in ix:
                X[r, ix[t]] = (1 + math.log(c)) * math.log((1 + n) / (1 + df[t]))
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return X / norms


# ------------------------------------------------------------------ the map
class Atlas:
    MAX_MEMORIES = 700
    MAX_DOCS = 150

    def __init__(self, aether):
        self.a = aether
        self._cache: dict = {}
        with aether.lock:
            aether.db.execute("CREATE TABLE IF NOT EXISTS atlas_dismissed (s TEXT, o TEXT, PRIMARY KEY (s, o))")
            aether.db.commit()

    # -------------------------------------------------------------- cache key: anything changed?
    def _version(self) -> tuple:
        db = self.a.db
        m = db.execute("SELECT COUNT(*), MAX(updated), MAX(id) FROM memories WHERE deleted=0").fetchone()
        t = db.execute("SELECT COUNT(*), MAX(id), MAX(valid_to) FROM triples").fetchone()
        d = db.execute("SELECT COUNT(*) FROM atlas_dismissed").fetchone()
        return tuple(m) + tuple(t) + tuple(d) + (len(getattr(self.a.rules, "rules", [])),)

    def build(self, at: float | None = None, focus: str = "me") -> dict:
        focus = "all" if focus == "all" else "me"
        key = (self._version(), round(at, 1) if at else None, focus)
        hit = self._cache.get(key)
        if hit:
            self.__dict__.update(hit[1])
            return hit[0]
        t0 = time.time()
        out = self._build(at, focus)
        out["stats"]["build_ms"] = round((time.time() - t0) * 1000)
        state = {k: getattr(self, k, None) for k in ("_last", "_mentions", "_sim_of", "_dups", "_proofs", "_unrelated")}
        if len(self._cache) >= 4:
            self._cache.clear()
        self._cache[key] = (out, state)
        return out

    # -------------------------------------------------------------- graph assembly
    def _facts(self, at):
        kg = self.a.kg
        if at is None:
            return kg.all()
        return [t for t in kg.as_of(at)]

    def _entity_kinds(self, facts) -> dict:
        kind: dict[str, str] = {}
        for t in facts:
            if t.p in ("is_a", "is") and t.o in KIND_WORDS:
                kind[t.s] = KIND_WORDS[t.o]
        for t in facts:
            root = re.sub(r"^(?:has_|is_)|_of$", "", t.p)
            if t.p.endswith("_of") and (root in PERSON_P or root.split("_")[-1] in PERSON_P | {"guide"}):
                for e in (t.s, t.o):            # "Aryan classmate_of Arvind" / "Dr X thesis_guide_of Aryan"
                    if e != "user" and e not in kind:
                        kind[e] = "person"
            if t.p in ("works_on", "building", "member_of") and t.o not in kind and t.s == "user":
                kind[t.o] = "project"
            if t.p in PERSON_P and t.o not in kind:
                kind[t.o] = "person"
            if t.p in PLACE_P and t.o not in kind:
                kind[t.o] = "place"
            if t.p in PROJECT_P and t.o not in kind:
                kind[t.o] = "project"
            if t.p in PERSON_S and t.s not in kind and t.s != "user":
                kind[t.s] = "person"
        kind["user"] = "you"
        return kind

    def _aliases(self) -> set[str]:
        from ..neurosymbolic.kg import norm
        name = (getattr(self.a.cfg, "user_name", "") or "").strip()
        out = {"user", "me", "i", "myself"}
        if name:
            out |= {norm(name), norm(name.split()[0])}
        return out

    def _build(self, at: float | None, focus: str = "me") -> dict:
        a = self.a
        G = Graph()
        alias = self._aliases()
        from ..neurosymbolic.kg import Triple
        facts = [t if (t.s not in alias and t.o not in alias) else
                 Triple("user" if t.s in alias else t.s, t.p, "user" if t.o in alias else t.o, t.source, t.confidence, t.id)
                 for t in self._facts(at)]
        facts = [t for t in facts if t.s != t.o]
        kinds = self._entity_kinds(facts)
        hist = {}
        try:
            for h in a.kg.history(None, 5000):
                hist.setdefault(h["s"], h["learned"])
                hist.setdefault(h["o"], h["learned"])
        except Exception:
            pass
        for t in facts:
            for e in (t.s, t.o):
                if f"e:{e}" not in G.nodes:
                    G.nodes[f"e:{e}"] = {"id": f"e:{e}", "type": "entity", "kind": kinds.get(e, "thing"),
                                         "label": "You" if e == "user" else e.replace("_", " "),
                                         "key": e, "created": hist.get(e)}
        for t in facts:
            G.add_edge(f"e:{t.s}", f"e:{t.o}", 1.0 + t.confidence, kind="fact", label=t.p.replace("_", " "),
                       conf="stated" if t.source in ("user_stated", "user_approved") else "inferred",
                       fid=t.id, source=t.source, confidence=round(t.confidence, 2))
        proofs = {}
        try:
            if at is None:
                derived, _ = a.rules.run(persist=False)
            else:
                from ..neurosymbolic.rules import RuleEngine
                derived, _ = RuleEngine(a.kg.snapshot_at(at), [r.text for r in a.rules.rules]).run(persist=False)
            for d in derived[:300]:
                s, p, o = ("user" if x in alias else x for x in d.triple)
                if s == o:
                    continue
                for e in (s, o):
                    G.nodes.setdefault(f"e:{e}", {"id": f"e:{e}", "type": "entity", "kind": kinds.get(e, "thing"),
                                                  "label": e.replace("_", " "), "key": e, "created": None})
                G.add_edge(f"e:{s}", f"e:{o}", 0.8 * d.confidence, kind="derived", label=p.replace("_", " "),
                           conf="derived", rule=d.rule, support=[" ".join(x).replace("_", " ") for x in d.support],
                           confidence=round(d.confidence, 2))
                proofs[d.triple] = d
        except Exception:
            pass
        self._proofs = proofs

        # memories and documents
        mems = [m for m in a.store.list(None, 6000) if at is None or (m.created or 0) <= at]
        docs: dict[str, list] = defaultdict(list)
        notes = []
        for m in mems:
            if m.layer == "document":
                p = m.meta.get("path") or m.source or "document"
                if p in docs or len(docs) < self.MAX_DOCS:
                    docs[p].append(m)
            elif len(notes) < self.MAX_MEMORIES:
                notes.append(m)
        for m in notes:
            G.nodes[f"m:{m.id}"] = {"id": f"m:{m.id}", "type": "memory", "kind": m.layer, "label": _short(m.text),
                                    "mid": m.id, "created": m.created, "confidence": round(m.confidence, 2),
                                    "source": m.source}
        for p, chunks in docs.items():
            name = re.split(r"[\\/]", p)[-1] or p
            G.nodes[f"d:{p}"] = {"id": f"d:{p}", "type": "document", "kind": "document", "label": name,
                                 "path": p, "chunks": len(chunks), "created": min(c.created or 0 for c in chunks)}

        # mentions: memory/document -> entity, by name
        ents = {e["key"]: nid for nid, e in G.nodes.items() if e["type"] == "entity" and e["key"] != "user"}
        surface = sorted(((k.replace("_", " "), nid) for k, nid in ents.items() if len(k) >= 3), key=lambda x: -len(x[0]))
        pat = re.compile(r"\b(" + "|".join(re.escape(s) for s, _ in surface) + r")\b", re.I) if surface else None
        by_surface = {s.lower(): nid for s, nid in surface}
        texts: list[tuple[str, str]] = [(f"m:{m.id}", m.text) for m in notes]
        texts += [(f"d:{p}", " ".join(c.text for c in ch[:40])) for p, ch in docs.items()]
        self._mentions = defaultdict(set)
        if pat:
            for nid, text in texts:
                hits = Counter(by_surface[h.lower()] for h in pat.findall(text))
                for e, c in hits.items():
                    G.add_edge(nid, e, 0.6 + 0.2 * min(c, 3), kind="mention", conf="extracted")
                    self._mentions[e].add(nid)
        # areas of your life: every memory, document, goal and person hangs off you through one of them
        name = (getattr(a.cfg, "user_name", "") or "you").split()[0]
        G.nodes.setdefault("e:user", {"id": "e:user", "type": "entity", "kind": "you", "label": "You", "key": "user",
                                      "created": None})
        if name.lower() != "you":
            G.nodes["e:user"]["label"] = name

        def hub(c: str) -> str:
            nid = f"c:{c}"
            if nid not in G.nodes:
                G.nodes[nid] = {"id": nid, "type": "category", "kind": c, "label": CATEGORIES[c].format(name=name),
                                "key": c, "created": None}
                G.add_edge(nid, "e:user", 2.0, kind="area", conf="structure")
            return nid
        for m in notes:
            G.nodes[f"m:{m.id}"]["category"] = c = category(m.text, m.layer, m.meta)
            G.add_edge(f"m:{m.id}", hub(c), 0.9, kind="area", conf="structure")
        for p in docs:
            G.nodes[f"d:{p}"]["category"] = "docs"
            G.add_edge(f"d:{p}", hub("docs"), 0.6, kind="area", conf="structure")
        try:
            goals = [g for g in a.graph.goals(200) if at is None or (g.get("created") or 0) <= at]
        except Exception:
            goals = []
        for g in goals[:60]:
            gid = f"g:{g['id']}"
            G.nodes[gid] = {"id": gid, "type": "goal", "kind": g.get("status") or "active", "label": _short(g["text"]),
                            "gid": g["id"], "created": g.get("created"), "category": "goals"}
            G.add_edge(gid, hub("goals"), 1.2, kind="area", conf="structure")
            if pat:
                for e in {by_surface[h.lower()] for h in pat.findall(g["text"])}:
                    G.add_edge(gid, e, 0.8, kind="mention", conf="extracted")

        # similarity between memories and documents (TF-IDF cosine)
        self._dups = []
        self._sim_of = {}
        if len(texts) >= 2:
            X = tfidf([tokens(t) for _, t in texts])
            if X.shape[1]:
                S = X @ X.T
                np.fill_diagonal(S, 0)
                ids = [n for n, _ in texts]
                for i, nid in enumerate(ids):
                    order = np.argsort(-S[i])[:5]
                    self._sim_of[nid] = [(ids[j], float(S[i, j])) for j in order if S[i, j] >= 0.12]
                    for j in order[:3]:
                        s = float(S[i, j])
                        if s >= 0.22 and i < j or (s >= 0.22 and i not in np.argsort(-S[j])[:3]):
                            if ids[j] not in G.adj.get(nid, {}):
                                G.add_edge(nid, ids[j], s, kind="similar", conf="inferred", label=f"{s:.0%} similar")
                        if s >= 0.8 and i < j and nid.startswith("m:") and ids[j].startswith("m:"):
                            self._dups.append((nid, ids[j], s))
        for n in G.nodes:
            G.adj.setdefault(n, {})
        hidden = 0
        keep, todo = {"e:user"}, ["e:user"]
        while todo:             # what actually connects to you, before people and projects are filed under areas
            for b in G.adj[todo.pop()]:
                if b not in keep:
                    keep.add(b)
                    todo.append(b)
        for nid, n in list(G.nodes.items()):
            if n["type"] == "entity" and n["kind"] in ("person", "project") and (focus == "all" or nid in keep):
                G.add_edge(nid, hub("people" if n["kind"] == "person" else "projects"), 0.5, kind="area", conf="structure")
        keep |= {n for n in G.nodes if n.startswith("c:")}
        if focus == "me":       # only what's connected to you: drop demo worlds, test data and stray islands
            drop = set(G.nodes) - keep
            hidden = len(drop)
            for n in drop:
                del G.nodes[n]
                G.adj.pop(n, None)
            for n in G.adj:
                for b in [b for b in G.adj[n] if b in drop]:
                    del G.adj[n][b]
            G.edges = [e for e in G.edges if e["s"] in keep and e["t"] in keep]
            self._unrelated = drop
        else:
            self._unrelated = set()
        self._last = G
        out = self._analyse(G, facts, notes)
        out["stats"]["hidden"] = hidden
        out["stats"]["focus"] = focus
        return out

    # -------------------------------------------------------------- analysis
    def _analyse(self, G: Graph, facts, notes) -> dict:
        adj = G.adj
        comm = louvain(adj) if adj else {}
        pr = pagerank(adj)
        bt = betweenness(adj)
        prmax = max(pr.values(), default=1.0) or 1.0
        for nid, n in G.nodes.items():
            n["community"] = comm.get(nid, -1)
            n["pr"] = round(pr.get(nid, 0) / prmax, 4)
            n["bt"] = round(bt.get(nid, 0), 4)
            n["deg"] = len(adj[nid])
        # communities, named after their most central entities (or their words)
        groups = defaultdict(list)
        for nid, c in comm.items():
            groups[c].append(nid)
        communities = []
        for c, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            if len(members) < 2:
                continue
            ranked = sorted(members, key=lambda x: -pr.get(x, 0))
            hubs = [G.nodes[x]["label"] for x in ranked if G.nodes[x]["type"] == "category"]
            names = ([" & ".join(hubs[:2])] if hubs else []) or [G.nodes[x]["label"] for x in ranked if G.nodes[x]["type"] == "entity" and x != "e:user"][:3]
            if not names:
                words = Counter(w for x in members for w in tokens(G.nodes[x]["label"]))
                names = [w for w, _ in words.most_common(3)]
            types = Counter(G.nodes[x]["type"] for x in members)
            cats = Counter(G.nodes[x].get("category") for x in members if G.nodes[x].get("category"))
            internal = sum(1 for e in G.edges if comm.get(e["s"]) == c and comm.get(e["t"]) == c)
            communities.append({"id": c, "label": " · ".join(names) or f"group {c}", "size": len(members),
                                "entities": types["entity"], "memories": types["memory"], "documents": types["document"],
                                "core": ranked[:6], "area": cats.most_common(1)[0][0] if cats else None, "cohesion": round(internal / max(1, len(members) - 1), 2)})
        for e in G.edges:
            e["cross"] = comm.get(e["s"]) != comm.get(e["t"])
        q = modularity(adj, comm) if adj else 0.0

        def card(nid, **kw):
            n = G.nodes[nid]
            return {"id": nid, "label": n["label"], "type": n["type"], "kind": n["kind"], **kw}

        trivial = re.compile(r"^(?:\d[\d\s.,:/-]*|(?:19|20)\d\d|\d+\s*(?:kg|km|h|%)?)$")   # years, numbers, dates
        cand = [n for n in G.nodes if n != "e:user" and G.nodes[n]["type"] in ("entity", "document", "goal")
                and not trivial.match(G.nodes[n]["label"]) and len(adj[n]) >= 2]
        god = sorted(cand, key=lambda n: -(0.55 * bt.get(n, 0) + 0.45 * pr.get(n, 0) / prmax))[:8]
        god_nodes = [card(n, degree=len(adj[n]), why=f"{len(adj[n])} links, ties together "
                          f"{len({comm.get(b) for b in adj[n]})} area{'s' if len({comm.get(b) for b in adj[n]}) != 1 else ''}")
                     for n in god]
        bridges = []
        for n in cand:
            k = sum(adj[n].values())
            per = defaultdict(float)
            for b, w in adj[n].items():
                per[comm.get(b)] += w
            if len(per) >= 2 and len(adj[n]) >= 3:
                bridges.append((1 - sum((x / k) ** 2 for x in per.values()), n, len(per)))
        bridges = [card(n, participation=round(p, 2), areas=k) for p, n, k in sorted(bridges, reverse=True)[:6]]

        cname = {c["id"]: c["label"] for c in communities}
        surprising = []
        for e in G.edges:
            if not e["cross"] or e["kind"] in ("about", "area"):
                continue
            a_, b_ = e["s"], e["t"]
            if cname.get(comm.get(a_)) is None or cname.get(comm.get(b_)) is None:
                continue
            score = e["w"] / math.sqrt(len(adj[a_]) * len(adj[b_]))
            if G.nodes[a_]["type"] != G.nodes[b_]["type"]:
                score *= 1.3
            surprising.append((score, e))
        seen, surprise_out = set(), []
        for score, e in sorted(surprising, key=lambda x: -x[0]):
            k = frozenset((e["s"], e["t"]))
            if k in seen:
                continue
            seen.add(k)
            surprise_out.append({"s": card(e["s"]), "t": card(e["t"]), "kind": e["kind"], "label": e.get("label", ""),
                                 "score": round(score, 3),
                                 "why": f"links “{cname[comm[e['s']]]}” with “{cname[comm[e['t']]]}”"})
            if len(surprise_out) >= 8:
                break

        dismissed = {(r[0], r[1]) for r in self.a.db.execute("SELECT s, o FROM atlas_dismissed").fetchall()}
        ents = [n for n in G.nodes if G.nodes[n]["type"] == "entity" and n != "e:user"]
        sugg = []
        for i, u in enumerate(ents):
            near = set()
            for z in adj[u]:
                near |= set(adj[z])
            for v in near:
                if v <= u or v not in G.nodes or G.nodes[v]["type"] != "entity" or v == "e:user" or v in adj[u] \
                        or trivial.match(G.nodes[v]["label"]) or trivial.match(G.nodes[u]["label"]):
                    continue
                ku, kv = G.nodes[u]["key"], G.nodes[v]["key"]
                if (ku, kv) in dismissed or (kv, ku) in dismissed:
                    continue
                score, common = adamic_adar(adj, u, v)
                if len(common) >= 2 or score >= 1.2:
                    sugg.append((score, u, v, common))
        sugg.sort(key=lambda x: -x[0])
        suggestions = []
        for score, u, v, common in sugg[:10]:
            mems = sum(1 for z in common if G.nodes[z]["type"] != "entity")
            via = [G.nodes[z]["label"] for z in common if G.nodes[z]["type"] == "entity"][:3]
            why = []
            if mems:
                why.append(f"mentioned together in {mems} memor{'ies' if mems != 1 else 'y'}")
            if via:
                why.append("both linked to " + ", ".join(via))
            suggestions.append({"s": card(u), "t": card(v), "score": round(score, 2), "why": "; ".join(why),
                                "s_key": G.nodes[u]["key"], "t_key": G.nodes[v]["key"]})

        dups = [{"keep": card(b), "drop": card(a_), "similarity": round(s, 2)}
                for a_, b, s in sorted(self._dups, key=lambda x: -x[2])[:8]]
        orphans = [n for n in G.nodes if not adj[n] and G.nodes[n]["type"] == "memory"]
        weak = [{"fid": t.id, "text": f"{t.s} {t.p} {t.o}".replace("_", " "), "confidence": round(t.confidence, 2),
                 "source": t.source} for t in facts if t.confidence < 0.6 and t.source not in ("user_stated", "user_approved")][:8]
        gaps = [{"community": c["id"], "label": c["label"], "memories": c["memories"]}
                for c in communities if c["entities"] == 0 and c["memories"] >= 3][:5]

        created = [n.get("created") for n in G.nodes.values() if n.get("created")]
        nodes = []
        for nid, n in G.nodes.items():
            nodes.append({k: n[k] for k in ("id", "type", "kind", "label", "community", "pr", "bt", "deg", "category") if k in n}
                         | ({"created": n["created"]} if n.get("created") else {})
                         | ({"confidence": n["confidence"]} if "confidence" in n else {}))
        links = [{k: e[k] for k in ("s", "t", "w", "kind", "conf", "cross", "label") if k in e} for e in G.edges]
        return {
            "nodes": nodes, "links": links, "communities": communities,
            "insights": {"god_nodes": god_nodes, "bridges": bridges, "surprising": surprise_out,
                         "suggestions": suggestions, "duplicates": dups,
                         "orphans": [card(n) for n in orphans[:8]], "orphan_count": len(orphans),
                         "weak_facts": weak, "gaps": gaps},
            "stats": {"nodes": len(nodes), "links": len(links), "communities": len(communities),
                      "modularity": round(q, 3), "entities": sum(1 for n in nodes if n["type"] == "entity"),
                      "memories": sum(1 for n in nodes if n["type"] == "memory"),
                      "documents": sum(1 for n in nodes if n["type"] == "document"),
                      "goals": sum(1 for n in nodes if n["type"] == "goal"),
                      "areas": [{"id": n["id"], "key": n["kind"], "label": n["label"], "size": n["deg"]}
                                for n in nodes if n["type"] == "category"],
                      "facts": sum(1 for e in links if e["kind"] == "fact"),
                      "derived": sum(1 for e in links if e["kind"] == "derived"),
                      "inferred": sum(1 for e in links if e["kind"] == "similar"),
                      "first": min(created, default=None), "last": max(created, default=None)},
        }

    # -------------------------------------------------------------- one node, in full
    def node(self, nid: str) -> dict:
        self.build()
        G: Graph = self._last
        if nid not in G.nodes:
            raise KeyError(nid)
        n = dict(G.nodes[nid])
        adj = G.adj[nid]
        neigh = sorted(adj.items(), key=lambda kv: -kv[1])[:16]
        edges = [e for e in G.edges if nid in (e["s"], e["t"])]
        out = {"node": n, "neighbours": [{"id": b, "label": G.nodes[b]["label"], "type": G.nodes[b]["type"],
                                          "kind": G.nodes[b]["kind"], "w": round(w, 2)} for b, w in neigh]}
        if n["type"] == "entity":
            key = n["key"]
            out["facts"] = [{"fid": e.get("fid"), "s": G.nodes[e["s"]]["label"], "p": e["label"], "o": G.nodes[e["t"]]["label"],
                             "source": e.get("source"), "confidence": e.get("confidence"), "conf": e["conf"]}
                            for e in edges if e["kind"] == "fact"]
            out["proofs"] = [{"fact": f"{G.nodes[e['s']]['label']} {e['label']} {G.nodes[e['t']]['label']}",
                              "rule": e.get("rule"), "support": e.get("support", []), "confidence": e.get("confidence")}
                             for e in edges if e["kind"] == "derived"]
            mids = [int(x[2:]) for x in self._mentions.get(nid, ()) if x.startswith("m:")]
            mems = {m.id: m for m in self.a.store.list(None, 6000) if m.id in set(mids)}
            out["memories"] = [_mem(m) for m in sorted(mems.values(), key=lambda m: -(m.created or 0))[:25]]
            out["documents"] = [G.nodes[x]["label"] for x in self._mentions.get(nid, ()) if x.startswith("d:")][:10]
            tl = []
            try:
                for h in self.a.kg.history(key, 200):
                    tl.append({"t": h["learned"], "what": f"learned: {h['s']} {h['p']} {h['o']}".replace("_", " ")})
                    if h["retired"]:
                        tl.append({"t": h["retired"], "what": f"retired: {h['s']} {h['p']} {h['o']}".replace("_", " ")})
            except Exception:
                pass
            tl += [{"t": m["created"], "what": f"{m['layer']} memory: {m['text'][:90]}"} for m in out["memories"]]
            out["timeline"] = sorted((x for x in tl if x["t"]), key=lambda x: x["t"])[-40:]
        elif n["type"] == "memory":
            m = next((m for m in self.a.store.list(None, 6000) if m.id == n["mid"]), None)
            out["memory"] = _mem(m) if m else None
            out["similar"] = [{"id": b, "label": G.nodes[b]["label"], "similarity": round(s, 2)}
                              for b, s in self._sim_of.get(nid, []) if b in G.nodes]
        elif n["type"] == "category":
            mids = [int(b[2:]) for b in adj if b.startswith("m:")]
            want = set(mids)
            mems = [m for m in self.a.store.list(None, 6000) if m.id in want]
            out["memories"] = [_mem(m) for m in sorted(mems, key=lambda m: -(m.created or 0))]
        elif n["type"] == "goal":
            try:
                g = self.a.graph.goal(n["gid"])
                out["goal"] = {"text": g.get("text"), "status": g.get("status"),
                               "tasks": [{"description": t.description, "status": t.status}
                                         for t in self.a.graph.tasks(n["gid"])]}
            except Exception:
                out["goal"] = None
        else:
            out["similar"] = [{"id": b, "label": G.nodes[b]["label"], "similarity": round(s, 2)}
                              for b, s in self._sim_of.get(nid, []) if b in G.nodes]
        return out

    # -------------------------------------------------------------- fixing what AETHER knows
    def act(self, op: str, body: dict) -> dict:
        a = self.a
        if op == "link":
            s, o = str(body["s"]), str(body["o"])
            p = re.sub(r"[^a-z_]", "", str(body.get("p") or "related_to").lower().replace(" ", "_")) or "related_to"
            bad = a.kg.assert_fact(s, p, o, source="user_approved", confidence=0.85)
            return {"ok": not bad, "message": f"Linked {s.replace('_', ' ')} {p.replace('_', ' ')} {o.replace('_', ' ')}."
                    if not bad else bad[0].reason}
        if op == "dismiss":
            with a.lock:
                a.db.execute("INSERT OR IGNORE INTO atlas_dismissed VALUES (?, ?)", (str(body["s"]), str(body["o"])))
                a.db.commit()
            return {"ok": True, "message": "Okay, I won't suggest that link again."}
        if op == "merge":
            keep, drop = int(body["keep"]), int(body["drop"])
            a.store.forget(drop, hard=False)
            return {"ok": True, "message": f"Merged: kept memory {keep}, set aside the duplicate."}
        if op == "forget":
            a.store.forget(int(body["id"]), hard=False)
            return {"ok": True, "message": "Forgotten."}
        if op == "correct":
            text = str(body["text"]).strip()
            if not text or len(text) > 4000:
                raise ValueError("text")
            a.store.correct(int(body["id"]), text)
            return {"ok": True, "message": "Corrected."}
        if op == "retract":
            a.kg.retract(int(body["fid"]))
            return {"ok": True, "message": "Retracted. It stays in the belief history."}
        if op == "prune":       # retire facts that don't connect to you (they stay in the belief history)
            self._cache.clear()
            self._build(None, "me")
            unrelated = {n[2:] for n in getattr(self, "_unrelated", set()) if n.startswith("e:")}
            n = 0
            for t in a.kg.all():
                if t.s in unrelated and t.o in unrelated and t.id is not None:
                    a.kg.retract(t.id)
                    n += 1
            return {"ok": True, "message": f"Retired {n} fact{'s' if n != 1 else ''} that had nothing to do with you. "
                                           "They stay in the belief history if you ever want them back."}
        raise ValueError(op)

    # -------------------------------------------------------------- a report you can read (or hear)
    def report(self, spoken: bool = False) -> str:
        m = self.build()
        s, ins, comms = m["stats"], m["insights"], m["communities"]
        if not s["nodes"]:
            return "Your memory map is empty so far. Tell me about your people, projects and plans and it fills in."
        god = [g["label"] for g in ins["god_nodes"][:3]]
        if spoken:
            out = (f"Your memory map has {s['nodes']} nodes in {s['communities']} areas. "
                   + (f"The most central are {', '.join(god)}. " if god else ""))
            if ins["suggestions"]:
                x = ins["suggestions"][0]
                out += f"I think {x['s']['label']} and {x['t']['label']} are connected; want me to link them?"
            return out.strip()
        lines = [f"**Memory map:** {s['nodes']} nodes ({s['entities']} entities, {s['memories']} memories, "
                 f"{s['documents']} documents), {s['links']} links, {s['communities']} communities "
                 f"(modularity {s['modularity']}).", ""]
        if god:
            lines += ["**God nodes** (what holds your world together): " + ", ".join(f"{g['label']} ({g['why']})" for g in ins["god_nodes"][:5]) + "."]
        if comms:
            lines += ["", "**Areas:**"] + [f"- {c['label']}: {c['size']} nodes" for c in comms[:6]]
        if ins["surprising"]:
            lines += ["", "**Surprising connections:**"] + [f"- {x['s']['label']} ↔ {x['t']['label']} ({x['why']})" for x in ins["surprising"][:3]]
        if ins["suggestions"]:
            lines += ["", "**Links I'd suggest:**"] + [f"- {x['s']['label']} ↔ {x['t']['label']}: {x['why']}" for x in ins["suggestions"][:3]]
        fixes = []
        if ins["duplicates"]:
            fixes.append(f"{len(ins['duplicates'])} near-duplicate memories to merge")
        if ins["orphan_count"]:
            fixes.append(f"{ins['orphan_count']} memories nothing links to")
        if ins["weak_facts"]:
            fixes.append(f"{len(ins['weak_facts'])} weak inferred facts to check")
        if fixes:
            lines += ["", "**To tidy:** " + "; ".join(fixes) + "."]
        lines += ["", "_Open **Map** to explore it, accept links and fix things._"]
        return "\n".join(lines)


def _short(text: str, n: int = 64) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    return t if len(t) <= n else t[:n - 1].rsplit(" ", 1)[0] + "…"


def _mem(m) -> dict:
    return {"id": m.id, "layer": m.layer, "text": m.text, "source": m.source, "confidence": round(m.confidence, 2),
            "created": m.created, "path": m.meta.get("path", "")}


ASK = re.compile(r"^(?:(?:show|open)\s+(?:me\s+)?)?(?:my\s+|the\s+)?(?:memory\s+map|map\s+of\s+(?:my\s+)?memor(?:y|ies)|"
                 r"memory\s+report|knowledge\s+report|graph\s+report)(?:\s+report)?(?:\s+please)?$|"
                 r"^(?:map\s+my\s+memor(?:y|ies)|what\s+are\s+my\s+god\s+nodes|what\s+holds\s+my\s+(?:world|life)\s+together)$",
                 re.I)


def handle(a, text: str, spoken: bool = False) -> str | None:
    t = re.sub(r"^(?:(?:hey|ok|okay)\s+)?(?:aether|nova)[\s,]+", "", text.strip(), flags=re.I).strip(" ?.!")
    if not ASK.match(t):
        return None
    return a.atlas.report(spoken)
