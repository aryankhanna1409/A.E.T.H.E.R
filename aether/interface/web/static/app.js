// AETHER web app. Plain ES modules, no build step, no network beyond 127.0.0.1.
import { markdown, esc } from "./md.js";
import { GraphView } from "./graph.js";
import { Inspector } from "./inspector.js";
import { renderDag } from "./dag.js";
import { HUD } from "./hud.js";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const h = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };
const icon = (name) => `<svg><use href="#i-${name}"/></svg>`;
const store = {
  get(k, d) { try { const v = localStorage.getItem("aether." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("aether." + k, JSON.stringify(v)); } catch {} },
};
const ago = (t) => {
  const s = Date.now() / 1000 - t;
  if (s < 60) return "just now"; if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`; return new Date(t * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
};

async function api(path, opts = {}) {
  const init = { ...opts, headers: { ...(opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {}), ...(opts.headers || {}) } };
  if (init.body && typeof init.body === "object" && !(init.body instanceof FormData)) init.body = JSON.stringify(init.body);
  const r = await fetch(path, init);
  if (!r.ok) { let msg = r.statusText; try { msg = (await r.json()).detail || msg; } catch {} throw new Error(msg); }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r;
}

function toast(title, body = "", kind = "", ms = 6000) {
  const t = h(`<div class="toast ${kind}"><b>${esc(title)}</b><button class="icon-btn" aria-label="Dismiss">✕</button>${body ? `<p>${esc(body)}</p>` : ""}</div>`);
  $("button", t).onclick = () => t.remove();
  $("#toasts").appendChild(t);
  if (ms) setTimeout(() => t.remove(), ms);
}

// ------------------------------------------------------------------ app state
const S = {
  view: "chat", status: null, busy: false, session: null,
  showMind: store.get("showMind", true), showConvos: store.get("showConvos", true),
  autoSpeak: store.get("autoSpeak", false), theme: store.get("theme", "dark"),
  lastState: null, pendingId: null, graph: null,
};
document.documentElement.dataset.theme = S.theme;

const VIEWS = { chat: "Chat", today: "Today", memory: "Memory", knowledge: "Knowledge", goals: "Mission control", lab: "Lab", self: "Self", activity: "Activity", settings: "Settings" };

function go(view) {
  if (!VIEWS[view]) return;
  if (S.graph && view !== "knowledge") { S.graph.destroy(); S.graph = null; }
  clearInterval(S.poll); clearInterval(S.play);
  S.view = view;
  $$("#rail button").forEach(b => b.classList.toggle("active", b.dataset.view === view));
  $("#view-title").textContent = VIEWS[view];
  const main = $("#main");
  main.replaceChildren();
  if (view === "chat") main.appendChild(Chat.el());
  else { const page = h(`<div class="page"></div>`); main.appendChild(page); RENDER[view](page).catch(e => page.replaceChildren(h(`<div class="empty">Couldn't load this view: ${esc(e.message)}</div>`))); }
  if (location.hash !== "#" + view) history.replaceState(null, "", "#" + view);
}
$$("#rail button").forEach(b => b.onclick = () => go(b.dataset.view));

// ------------------------------------------------------------------ status, stop, approvals, notifications
async function refreshStatus() {
  try {
    const s = S.status = await api("/api/status");
    const pills = [
      [s.model_loaded ? "ok" : "bad", s.model_loaded ? "Model ready" : "No model", s.model_loaded ? "" : s.problem],
      ["", s.embeddings.includes("BM25") ? "Keyword search" : "Hybrid search", s.embeddings],
      ["", `${s.memories} memories · ${s.facts} facts`, ""],
      [s.network ? "ok" : "warn", s.network ? "Online research" : "Offline", ""],
    ];
    $("#status-pills").innerHTML = pills.map(([k, t, tip]) => `<span class="pill ${k}" title="${esc(tip)}"><span class="dot"></span>${esc(t)}</span>`).join("")
      + `<span class="pill" title="Memories and chats are encrypted at rest">${icon("lock")}Encrypted</span>`;
    const stop = $("#stop");
    stop.classList.toggle("on", s.emergency_stop);
    $("span", stop).textContent = s.emergency_stop ? "Stopped · click to resume" : "Emergency stop";
  } catch (e) { $("#status-pills").innerHTML = `<span class="pill bad"><span class="dot"></span>Server offline</span>`; }
}
$("#stop").onclick = async () => {
  const on = !$("#stop").classList.contains("on");
  await api(`/api/stop?engaged=${on}`, { method: "POST" });
  toast(on ? "Emergency stop engaged" : "Actions re-enabled", on ? "Every computer action is blocked until you resume." : "", on ? "error" : "");
  refreshStatus();
};

async function pollPending() {
  try {
    const items = await api("/api/pending");
    if (items.length && S.pendingId !== items[0].id) {
      S.pendingId = items[0].id;
      $("#approval-text").textContent = items[0].prompt;
      const inj = /prompt injection/i.test(items[0].prompt);
      $("#approval-note").textContent = inj
        ? "Something AETHER just read contained instructions aimed at it. Allow only if YOU asked for this."
        : "Nothing happens unless you allow it. This request expires after two minutes.";
      $("#approval").hidden = false; $("#allow").focus();
      if (document.hidden) notify("AETHER needs your approval", items[0].prompt.split("\n")[0]);
    } else if (!items.length && S.pendingId) { $("#approval").hidden = true; S.pendingId = null; }
  } catch {}
}
async function answerPending(allow) {
  $("#approval").hidden = true;
  if (S.pendingId) await api(`/api/pending/${S.pendingId}?allow=${allow}`, { method: "POST" }).catch(() => {});
  S.pendingId = null;
}
$("#allow").onclick = () => answerPending(true); $("#deny").onclick = () => answerPending(false);

function notify(title, body) {
  if (window.Notification && Notification.permission === "granted") new Notification(title, { body });
}
async function pollNotifications() {
  try {
    const r = await api("/api/notifications");
    for (const n of r.items) { toast(n.title, n.body, "proactive", 15000); if (document.hidden) notify("AETHER · " + n.title, n.body); }
  } catch {}
}

// ------------------------------------------------------------------ voice helpers
let audioEl = null;
async function speak(text) {
  try {
    const r = await api("/api/speak", { method: "POST", body: { text } });
    const url = URL.createObjectURL(await r.blob());
    if (audioEl) audioEl.pause();
    audioEl = new Audio(url); audioEl.play();
  } catch (e) { toast("Voice is not set up", e.message + "\nSet [voice].tts_voice_path (scripts/get_model.py voice).", "error"); }
}

// ------------------------------------------------------------------ chat
const PIPE = [
  ["perceive", "Perceive", "mood and topic"], ["interpret", "Interpret", "what you want"], ["retrieve", "Retrieve", "memory, documents, web"],
  ["reason", "Reason", "facts and rules"], ["act", "Act", "tools, if needed"], ["generate", "Write", "the answer"], ["verify", "Verify", "against known facts"],
];

const Chat = {
  root: null,
  el() { if (!this.root) this.build(); this.loadConvos(); return this.root; },
  build() {
    const r = this.root = h(`<div class="chat">
      <aside class="convos">
        <div class="convos-head"><button class="primary" id="new-chat">${icon("plus")}New chat</button></div>
        <input class="field convos-search" id="convo-q" placeholder="Search conversations">
        <div class="convo-list" id="convo-list"></div>
      </aside>
      <section class="stage">
        <div class="stage-bar">
          <button class="icon-btn" id="toggle-convos" title="Conversations">${icon("chat")}</button>
          <button class="icon-btn" id="toggle-mind" title="Show how AETHER is thinking">${icon("panel")}</button>
        </div>
        <div id="log"></div>
        <div class="composer-wrap">
          <div class="composer" id="composer">
            <div class="attach" id="attach" hidden></div>
            <textarea id="t" rows="1" placeholder="Talk to AETHER…"></textarea>
            <div class="bar">
              <button class="icon-btn" id="img-btn" title="Attach an image">${icon("image")}</button>
              <button class="icon-btn" id="mic" title="Hold to talk (or press and release)">${icon("mic")}</button>
              <input type="file" id="img-in" accept="image/*" hidden>
              <span class="hint">Enter to send · Shift+Enter new line · Ctrl+K commands</span>
              <button class="send" id="send" title="Send">${icon("send")}</button>
            </div>
          </div>
        </div>
      </section>
      <aside class="mind" id="mind"></aside>
    </div>`);
    this.log = $("#log", r); this.t = $("#t", r); this.mind = $("#mind", r);
    this.applyLayout();
    $("#toggle-mind", r).onclick = () => { S.showMind = !S.showMind; store.set("showMind", S.showMind); this.applyLayout(); };
    $("#toggle-convos", r).onclick = () => { S.showConvos = !S.showConvos; store.set("showConvos", S.showConvos); this.applyLayout(); };
    $("#new-chat", r).onclick = () => this.newChat();
    $("#convo-q", r).oninput = (e) => this.loadConvos(e.target.value);
    this.t.addEventListener("input", () => this.autosize());
    this.t.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); this.send(); } });
    $("#send", r).onclick = () => this.send();
    $("#img-btn", r).onclick = () => $("#img-in", r).click();
    $("#img-in", r).onchange = e => e.target.files[0] && this.attach(e.target.files[0]);
    const comp = $("#composer", r);
    comp.addEventListener("dragover", e => { e.preventDefault(); comp.classList.add("drag"); });
    comp.addEventListener("dragleave", () => comp.classList.remove("drag"));
    comp.addEventListener("drop", e => { e.preventDefault(); comp.classList.remove("drag"); const f = [...e.dataTransfer.files].find(f => f.type.startsWith("image/")); if (f) this.attach(f); });
    this.t.addEventListener("paste", e => { const f = [...(e.clipboardData?.files || [])].find(f => f.type.startsWith("image/")); if (f) { e.preventDefault(); this.attach(f); } });
    this.setupMic($("#mic", r));
    this.log.addEventListener("click", e => {
      const c = e.target.closest("[data-copy]");
      if (c) { navigator.clipboard.writeText(c.parentElement.querySelector("code").textContent); c.textContent = "copied"; setTimeout(() => c.textContent = "copy", 1200); }
    });
    this.renderMind(null);
    this.hero();
  },
  applyLayout() {
    this.root.classList.toggle("no-mind", !S.showMind); this.root.classList.toggle("no-convos", !S.showConvos);
    $("#toggle-mind", this.root).classList.toggle("on", S.showMind); $("#toggle-convos", this.root).classList.toggle("on", S.showConvos);
  },
  autosize() { this.t.style.height = "auto"; this.t.style.height = Math.min(220, this.t.scrollHeight) + "px"; },
  async hero() {
    const hour = new Date().getHours();
    const greet = hour < 5 ? "Burning the midnight oil" : hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
    const el = h(`<div class="hero">
      <div class="reactor" id="reactor"><i class="r1"></i><i class="r2"></i><i class="r3"></i><i class="r4"></i></div>
      <h1>${greet}.</h1><p>Everything stays on this PC. What are we doing?</p>
      <div class="hero-brief" id="hero-brief"></div>
      <div class="suggest">
        <button data-p="Give me a quick plan for today based on my reminders and goals."><b>Plan my day</b><span>reminders, goals, priorities</span></button>
        <button data-p="What do you know about me so far? Be specific and tell me how you learned each thing."><b>What do you know about me?</b><span>memories and facts, with sources</span></button>
        <button data-p="I'm feeling stressed and could use a moment."><b>I need a moment</b><span>companion mode</span></button>
        <button data-p="Research the latest on solid-state batteries and give me a cited brief."><b>Research something</b><span>web + your documents, cited</span></button>
        <button data-p="Summarize the most important notes in my documents folder."><b>Summarize my notes</b><span>answers from your files</span></button>
        <button data-p="Optimize my week: essay:3:5:Wed; gym:1:3:Sun; slides:2:4:Fri; reading:2:2:Sun"><b>Optimize my week</b><span>metaheuristic scheduling</span></button>
      </div></div>`);
    $$(".suggest button", el).forEach(b => b.onclick = () => { this.t.value = b.dataset.p; this.send(); });
    this.log.replaceChildren(el);
    try {
      const b = await api("/api/brief");
      const lines = b.text.split("\n").slice(1).join("\n");
      if (lines && !/Nothing scheduled/.test(lines)) $("#hero-brief", el).textContent = lines;
    } catch {}
  },
  async loadConvos(q = "") {
    const list = this.root && $("#convo-list", this.root); if (!list) return;
    try {
      if (q.trim()) {
        const hits = await api("/api/search?q=" + encodeURIComponent(q));
        list.innerHTML = `<div class="convo-group">Matches</div>` + (hits.conversations.map(c => `<div class="convo" data-s="${esc(c.session)}"><span class="t" title="${esc(c.snippet)}">${esc(c.title || c.snippet)}</span></div>`).join("") || `<div class="empty">No matches</div>`);
      } else {
        const { current, items } = await api("/api/conversations");
        S.session = current;
        const groups = { Pinned: [], Today: [], "This week": [], Older: [] };
        const now = Date.now() / 1000;
        for (const c of items) (c.pinned ? groups.Pinned : now - c.updated < 86400 ? groups.Today : now - c.updated < 604800 ? groups["This week"] : groups.Older).push(c);
        list.innerHTML = Object.entries(groups).filter(([, v]) => v.length).map(([g, v]) => `<div class="convo-group">${g}</div>` + v.map(c =>
          `<div class="convo ${c.session === current ? "active" : ""}" data-s="${esc(c.session)}"><span class="t">${esc(c.title)}</span>
            <span class="acts"><button data-pin="${c.pinned ? 0 : 1}" title="${c.pinned ? "Unpin" : "Pin"}">${icon("pin")}</button><button data-del title="Delete">${icon("trash")}</button></span></div>`).join("")).join("")
          || `<div class="empty">Your conversations will appear here.</div>`;
      }
      $$(".convo", list).forEach(el => el.onclick = async (e) => {
        const s = el.dataset.s;
        if (e.target.closest("[data-del]")) { if (confirm("Delete this conversation? Memories learned from it stay.")) { await api("/api/conversations/" + s, { method: "DELETE" }); if (s === S.session) this.hero(); this.loadConvos(); } return; }
        const pin = e.target.closest("[data-pin]");
        if (pin) { await api("/api/conversations/" + s, { method: "PATCH", body: { pinned: pin.dataset.pin === "1" } }); this.loadConvos(); return; }
        this.open(s);
      });
    } catch {}
  },
  async newChat() { await api("/api/conversations", { method: "POST" }); this.hero(); this.renderMind(null); this.loadConvos(); this.t.focus(); },
  async open(session) {
    const { turns } = await api("/api/conversations/" + session);
    this.log.replaceChildren();
    for (const t of turns) t.role === "user" ? this.addYou(t.text) : this.addAI(t.text, { final: true, t: t.t });
    this.renderMind(null); this.loadConvos(); this.log.scrollTop = 1e9;
  },
  clearHero() { if ($(".hero", this.log)) this.log.replaceChildren(); },
  addYou(text, img) {
    this.clearHero();
    const el = h(`<div class="msg you"><div class="bubble"></div></div>`);
    $(".bubble", el).textContent = text;
    if (img) $(".bubble", el).prepend(h(`<img src="${img}" style="max-width:220px;border-radius:10px;display:block;margin-bottom:6px">`));
    this.log.appendChild(el); this.log.scrollTop = 1e9;
  },
  addAI(text = "", { final = false, t } = {}) {
    const el = h(`<div class="msg ai"><div class="who"><span class="mini-orb"></span>AETHER</div><div class="tools"></div><div class="body md"></div><div class="notices"></div>
      <div class="foot"><button class="icon-btn" data-a="copy" title="Copy">${icon("copy")}</button><button class="icon-btn" data-a="speak" title="Read aloud">${icon("speak")}</button>
      <button class="icon-btn" data-a="inspect" title="Inspect this turn: timeline, retrieval, proofs, verification" hidden>${icon("scan")}</button>
      <button class="icon-btn" data-a="up" title="Good answer">${icon("up")}</button><button class="icon-btn" data-a="down" title="Bad answer">${icon("down")}</button><span class="meta"></span></div></div>`);
    el._text = text;
    $(".body", el).innerHTML = text ? markdown(text) : `<div class="thinking-line"><span class="dots"><span></span><span></span><span></span></span><span class="label">Thinking</span></div>`;
    if (t) $(".meta", el).textContent = new Date(t * 1000).toLocaleString(undefined, { hour: "2-digit", minute: "2-digit", day: "numeric", month: "short" });
    $(".foot", el).onclick = async e => {
      const a = e.target.closest("[data-a]")?.dataset.a; if (!a) return;
      if (a === "copy") { navigator.clipboard.writeText(el._text); toast("Copied"); }
      if (a === "speak") speak(el._text);
      if (a === "inspect") Inspector.open(el._turn);
      if (a === "up" || a === "down") {
        await api("/api/feedback", { method: "POST", body: { value: a === "up" ? 1 : -1, turn_id: el._turn || null } });
        $$('[data-a="up"],[data-a="down"]', el).forEach(b => b.classList.toggle("on", b.dataset.a === a));
        toast(a === "up" ? "Thanks, noted" : "Noted. It counts toward self-improvement.");
      }
    };
    if (!final) $(".foot", el).style.display = "none";
    this.log.appendChild(el); this.log.scrollTop = 1e9;
    return el;
  },
  attach(file) {
    this.image = file;
    const url = URL.createObjectURL(file);
    const a = $("#attach", this.root); a.hidden = false;
    a.innerHTML = `<div class="thumb"><img src="${url}" alt=""><button title="Remove">✕</button></div>`;
    $("button", a).onclick = () => { this.image = null; a.hidden = true; a.innerHTML = ""; };
    this.imageUrl = url; this.t.placeholder = "Ask something about the image…"; this.t.focus();
  },
  setBusy(b) {
    S.busy = b; document.body.classList.toggle("busy", b);
    $("#send", this.root).disabled = b; $("#reactor")?.classList.toggle("busy", b);
  },
  async send() {
    const text = this.t.value.trim();
    if (S.busy || (!text && !this.image)) return;
    this.t.value = ""; this.autosize();
    if (this.image) return this.sendImage(text || "What is in this image?");
    this.addYou(text);
    const el = this.addAI();
    const body = $(".body", el), label = $(".label", el);
    this.setBusy(true);
    const live = { stages: {}, tools: [], route: [] };
    this.renderMind(null, live);
    let acc = "", raf = 0;
    const paint = () => {
      raf = 0; body.innerHTML = markdown(acc);
      let tail = body; while (tail.lastElementChild && !/^(BR|SPAN|BUTTON)$/.test(tail.lastElementChild.tagName)) tail = tail.lastElementChild;
      tail.insertAdjacentHTML("beforeend", `<span class="caret"></span>`); this.stick();
    };
    try {
      await streamChat(text, (kind, d) => {
        if (kind === "stage") {
          live.stages[d.name] = d; if (!d.done && label) label.textContent = d.label + "…";
          if (d.name === "retrieve" && d.done) live.route = d.routes;
          this.renderMind(null, live);
        } else if (kind === "tool") {
          if (d.running) { $(".tools", el).appendChild(h(`<span class="chip accent" data-tool="${esc(d.tool)}">⚙ ${esc(d.tool)}…</span>`)); live.stages.act = { name: "act", label: `Using ${d.tool}` }; }
          else { const c = $$(`.tools [data-tool="${CSS.escape(d.tool)}"]`, el).pop(); if (c) { c.className = "chip " + (d.ok ? "good" : "bad"); c.textContent = `${d.ok ? "✓" : "✕"} ${d.tool}${d.decision === "declined" ? " (declined)" : d.decision === "deny" ? " (blocked)" : ""}`; c.title = d.preview || ""; }
                 live.tools.push(d); live.stages.act = { name: "act", done: true }; }
          this.renderMind(null, live);
        } else if (kind === "token") { acc += d.text; if (!raf) raf = requestAnimationFrame(paint); }
        else if (kind === "replace") { acc = d.text; }
        else if (kind === "done") {
          cancelAnimationFrame(raf); el._text = d.text; el._turn = d.turn_id;
          if (d.turn_id != null) $('[data-a="inspect"]', el).hidden = false;
          body.innerHTML = markdown(d.text);
          const n = $(".notices", el);
          d.notices.forEach(x => n.appendChild(h(`<div class="notice ${/^Security/.test(x) ? "sec" : ""}">${esc(x)}</div>`)));
          $(".foot", el).style.display = ""; $(".meta", el).textContent = `${(d.state.latency_ms / 1000).toFixed(1)} s · ${d.state.route.join(" + ") || "direct"}`;
          S.lastState = d.state; this.renderMind(d.state, live);
          if (S.autoSpeak) speak(d.text);
        } else if (kind === "error") { body.innerHTML = `<div class="notice sec">${esc(d.message)}</div>`; }
      });
    } catch (e) { body.innerHTML = `<div class="notice sec">${esc(e.message)}</div>`; }
    this.setBusy(false); this.stick(); this.loadConvos(); refreshStatus();
  },
  stick() { const l = this.log; if (l.scrollHeight - l.scrollTop - l.clientHeight < 240) l.scrollTop = 1e9; },
  async sendImage(question) {
    const url = this.imageUrl, file = this.image;
    this.image = null; $("#attach", this.root).hidden = true; $("#attach", this.root).innerHTML = ""; this.t.placeholder = "Talk to AETHER…";
    this.addYou(question, url);
    const el = this.addAI(); $(".label", el).textContent = "Looking at the image (this runs on the CPU, give it a moment)…";
    this.setBusy(true);
    try {
      const fd = new FormData(); fd.append("file", file); fd.append("question", question);
      const r = await api("/api/image", { method: "POST", body: fd });
      el._text = r.text; $(".body", el).innerHTML = markdown(r.text); $(".foot", el).style.display = "";
    } catch (e) { $(".body", el).innerHTML = `<div class="notice sec">${esc(e.message)}. Set [vision].model_path and mmproj_path (scripts/get_model.py vision).</div>`; }
    this.setBusy(false);
  },
  setupMic(btn) {
    let rec = null, chunks = [], started = 0;
    const start = async () => {
      if (rec || S.busy) return;
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        rec = new MediaRecorder(stream); chunks = []; started = Date.now();
        rec.ondataavailable = e => chunks.push(e.data);
        rec.onstop = async () => {
          stream.getTracks().forEach(t => t.stop()); btn.classList.remove("rec"); $("#reactor")?.classList.remove("listen");
          const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" }); rec = null;
          if (blob.size < 2000) return;
          try {
            const fd = new FormData(); fd.append("file", blob, "speech.webm");
            const r = await api("/api/transcribe", { method: "POST", body: fd });
            if (r.text) { this.t.value = r.text; this.send(); } else toast("I didn't catch that");
          } catch (e) { toast("Speech-to-text is not available", e.message + "\nInstall the voice extra: pip install -e .[voice]", "error"); }
        };
        rec.start(); btn.classList.add("rec"); $("#reactor")?.classList.add("listen");
      } catch (e) { toast("Microphone blocked", "Allow microphone access for 127.0.0.1 in the browser.", "error"); }
    };
    const stop = () => { if (rec && rec.state === "recording") rec.stop(); };
    btn.addEventListener("pointerdown", e => { e.preventDefault(); rec ? stop() : start(); });
    btn.addEventListener("pointerup", () => { if (rec && Date.now() - started > 600) stop(); });
    this.toggleMic = () => (rec ? stop() : start());
  },
  renderMind(state, live) {
    const m = this.mind; if (!m) return;
    const st = live?.stages || {};
    const steps = PIPE.map(([k, name, sub]) => {
      const s = st[k]; const cls = s ? (s.done ? "done" : "run") : (state ? "done" : "");
      let detail = sub;
      if (s?.done && k === "interpret") detail = `${s.intent} · ${Math.round(s.confidence * 100)}% sure`;
      if (s?.done && k === "retrieve") detail = s.routes.length ? s.routes.join(" + ") + (s.documents ? ` · ${s.documents} doc chunks` : "") + (s.memories ? ` · ${s.memories} memories` : "") : "no lookup needed";
      if (s?.done && k === "reason") detail = `${s.facts} facts in play` + (s.contradictions?.length ? ` · ${s.contradictions.length} conflict` : "");
      if (k === "act" && live?.tools.length) detail = live.tools.map(t => `${t.ok ? "✓" : "✕"} ${t.tool}`).join(", ");
      if (k === "act" && !s && (state || st.generate)) return `<div class="step"><span class="node"></span><div><b>${name}</b><small>no tools needed</small></div></div>`;
      if (s?.done && k === "verify") detail = s.revised ? "corrected a conflict with known facts" : "consistent";
      if (s?.done && k === "perceive") detail = `${s.wellness} · ${s.topic}`;
      return `<div class="step ${cls}"><span class="node"></span><div><b>${name}</b><small>${esc(detail)}</small></div></div>`;
    }).join("");
    const list = (title, xs, cls = "") => xs && xs.length ? `<h4>${title}</h4><div class="list ${cls}">${xs.slice(0, 12).map(x => `<div>${esc(x)}</div>`).join("")}</div>` : "";
    const s = state;
    m.innerHTML = `<h4>Thinking pipeline</h4><div class="pipeline">${steps}</div>` + (s ? `
      <h4>This turn</h4>
      <div class="kv stack"><span>Goal</span><span>${esc(s.current_goal || "–")}</span></div>
      <div class="kv"><span>Topic</span><span>${esc(s.active_topic)}</span></div>
      <div class="kv"><span>Mood read</span><span>${esc(s.wellness)}</span></div>
      <div class="kv"><span>Time</span><span>${(s.latency_ms / 1000).toFixed(1)} s</span></div>
      <div class="kv"><span>Confidence</span><span>${Math.round(s.confidence * 100)}%</span></div><div class="gauge"><i style="width:${Math.round(s.confidence * 100)}%"></i></div>`
      + list("Facts used", s.known_facts) + list("Contradictions", s.contradictions, "bad") + list("Uncertain", s.uncertain_hypotheses) + list("Tool calls", s.tool_log)
      : (live ? "" : `<p class="muted" style="font-size:13px;margin-top:16px">Send a message to watch each stage: what AETHER understood, where it looked, which facts and rules it used, and whether it had to correct itself.</p>`));
  },
};

async function streamChat(text, on, { voice = false } = {}) {
  const r = await fetch("/api/chat/stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, voice }) });
  if (!r.ok || !r.body) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  const reader = r.body.getReader(), dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
      const ev = /^event: (.*)$/m.exec(chunk)?.[1], data = /^data: (.*)$/m.exec(chunk)?.[1];
      if (ev && data) on(ev, JSON.parse(data));
    }
  }
}

// ------------------------------------------------------------------ pages
const RENDER = {};

RENDER.today = async (p) => {
  const [b, props, m] = await Promise.all([api("/api/brief"), api("/api/proposals"), api("/api/metrics")]);
  const waiting = props.filter(x => x.status === "proposed");
  const hour = new Date().getHours();
  p.innerHTML = `<div class="page-head"><div class="grow"><h1>${hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening"}</h1>
      <p>${new Date().toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" })}</p></div>
      <button class="primary" id="plan">Plan my day with AETHER</button></div>
    <div class="grid three" style="margin-bottom:16px">
      <div class="card stat"><b>${b.reminders.length}</b><span>open reminders</span></div>
      <div class="card stat"><b>${b.goals.filter(g => ["running", "pending"].includes(g.status)).length}</b><span>goals in progress</span></div>
      <div class="card stat"><b>${m.turns || 0}</b><span>messages this week</span></div>
      <div class="card stat"><b>${waiting.length}</b><span>improvements waiting for you</span></div>
    </div>
    <div class="card" id="sched" style="margin-bottom:16px"></div>
    <div class="grid two">
      <div class="card"><h3><span class="grow">Briefing</span><button class="ghost" id="hear">☀ Hear it</button></h3><div class="md" id="brief-text">${markdown(b.text)}</div></div>
      <div class="card"><h3><span class="grow">Reminders</span></h3><div id="rem"></div>
        <div class="row" style="margin-top:12px"><input class="field" id="rem-text" placeholder="Remind me to…" style="flex:2"><input class="field" id="rem-when" placeholder="when (friday, 2026-12-01)" style="flex:1"><button class="ghost" id="rem-add">Add</button></div></div>
      <div class="card"><h3>Goals</h3>${b.goals.map(g => `<div class="item"><div class="grow">${esc(g.text)}<div class="meta">#${g.id}</div></div><span class="chip ${g.status === "done" ? "good" : g.status === "failed" ? "bad" : "accent"}">${g.status}</span></div>`).join("") || `<div class="empty">No goals yet. Ask AETHER to work on something in the background.</div>`}</div>
      <div class="card"><h3>Self-improvement</h3>${waiting.map(x => `<div class="item"><div class="grow">${esc(x.title)}<div class="meta">${esc(x.rationale)}</div></div></div>`).join("") || `<div class="empty">Nothing waiting for your approval.</div>`}
        ${waiting.length ? `<button class="ghost" onclick="location.hash='self'">Review in Self</button>` : ""}</div>
      <div class="card span2" id="inbox"></div>
      <div class="card span2" id="routines"></div>
    </div>`;
  Routines.render($("#routines", p));
  Schedule.render($("#sched", p));
  Inbox.render($("#inbox", p));
  $("#hear", p).onclick = () => { HUD.open(); HUD.ask("brief me"); };
  api("/api/briefing").then(x => { const t = $("#brief-text", p); if (t) t.innerHTML = markdown(x.text); }).catch(() => {});  // adds weather and news when online
  const rem = $("#rem", p);
  rem.innerHTML = b.reminders.map(r => `<div class="item"><div class="grow">${esc(r.text)}<div class="meta">due ${esc(r.due)}</div></div><div class="actions"><button class="ghost" data-r="${r.id}" data-s="done">Done</button><button class="icon-btn" data-r="${r.id}" data-s="dismissed" title="Dismiss">✕</button></div></div>`).join("") || `<div class="empty">Nothing due. Say "remind me to…" in chat or add one here.</div>`;
  $$("[data-r]", rem).forEach(x => x.onclick = async () => { await api(`/api/reminders/${x.dataset.r}?status=${x.dataset.s}`, { method: "POST" }); go("today"); });
  $("#rem-add", p).onclick = async () => {
    const text = $("#rem-text", p).value.trim(); if (!text) return;
    try { const r = await api("/api/reminders", { method: "POST", body: { text, when: $("#rem-when", p).value.trim() || "tomorrow" } }); toast("Reminder set", `${r.text} · ${r.due}`); } catch (e) { toast("Couldn't add", e.message, "error"); }
    go("today");
  };
  $("#plan", p).onclick = () => { go("chat"); Chat.t.value = "Give me a quick plan for today based on my reminders and goals."; Chat.send(); };
};

// Schedule: the local calendar (.ics files / Outlook on this PC) as a day strip with free time and a now line.
const Schedule = {
  async render(box, when = store.get("schedWhen", "today")) {
    const c = await api(`/api/calendar?when=${encodeURIComponent(when)}`);
    const hm = s => { const [h, m] = s.split(":").map(Number); return h * 60 + m; };
    const mins = iso => { const d = new Date(iso); return d.getHours() * 60 + d.getMinutes(); };
    const t = iso => iso.slice(11, 16);
    const oneDay = new Date(c.end) - new Date(c.start) <= 864e5;
    const a = hm(c.day_start), z = hm(c.day_end), span = z - a, pct = m => ((Math.min(z, Math.max(a, m)) - a) / span) * 100;
    const timed = c.events.filter(e => !e.all_day), allDay = c.events.filter(e => e.all_day);
    const now = new Date(), nowM = now.getHours() * 60 + now.getMinutes();
    const isToday = c.start.slice(0, 10) === new Date(now - now.getTimezoneOffset() * 6e4).toISOString().slice(0, 10);
    const hours = []; for (let h = Math.ceil(a / 60); h * 60 <= z; h += 2) hours.push(h);
    const strip = oneDay ? `<div class="day-strip">
        ${hours.map(h => `<span class="tick" style="left:${pct(h * 60)}%"><i>${String(h).padStart(2, "0")}</i></span>`).join("")}
        ${c.free.map(([f, u]) => `<div class="free" style="left:${pct(mins(f))}%;width:${pct(mins(u)) - pct(mins(f))}%" title="free ${t(f)}–${t(u)}"></div>`).join("")}
        ${timed.map((e, i) => `<div class="ev" style="left:${pct(mins(e.start))}%;width:${Math.max(1.2, pct(mins(e.end)) - pct(mins(e.start)))}%;top:${9 + (i % 2) * 28}px" title="${esc(e.title)} ${t(e.start)}–${t(e.end)}${e.location ? " · " + esc(e.location) : ""}"><b>${esc(e.title)}</b></div>`).join("")}
        ${isToday && nowM > a && nowM < z ? `<div class="now" style="left:${pct(nowM)}%"></div>` : ""}</div>` : "";
    const list = c.events.map(e => `<div class="item"><div class="grow">${esc(e.title)}<div class="meta">${oneDay ? "" : new Date(e.start).toLocaleDateString(undefined, { weekday: "short", day: "numeric" }) + " · "}${e.all_day ? "all day" : `${t(e.start)}–${t(e.end)}`}${e.location ? " · " + esc(e.location) : ""}</div></div><span class="chip">${esc(e.source.replace(/\.ics$/, ""))}</span></div>`).join("");
    box.innerHTML = `<h3><span class="grow">Schedule · ${esc(c.label)}</span>
        <span class="tabs small" id="sw">${["today", "tomorrow", "this week"].map(w => `<button data-w="${w}" class="${w === when ? "active" : ""}">${w[0].toUpperCase() + w.slice(1)}</button>`).join("")}</span></h3>
      ${allDay.length ? `<div class="row" style="margin-bottom:8px">${allDay.map(e => `<span class="chip accent">${esc(e.title)}</span>`).join("")}</div>` : ""}
      ${c.files.length || c.outlook ? `${strip}${c.free.length ? `<div class="muted free-line">Free ${c.free.map(([f, u]) => `${t(f)}–${t(u)}`).join(", ")}</div>` : ""}${list ? `<div class="sched-list">${list}</div>` : `<div class="empty">Nothing on the calendar ${esc(c.label)}.</div>`}`
        : `<div class="empty">No calendar yet. Export your calendar as <b>.ics</b> (Outlook, Google Calendar, a timetable) into <code>${esc(c.folder)}</code>, or turn on Outlook in settings. It's read on this PC only.</div>`}
      <div class="row" style="margin-top:12px"><input class="field" id="ev-title" placeholder="Add an event…" style="flex:2"><input class="field" id="ev-when" placeholder="when (friday 3pm)" style="flex:1"><button class="ghost" id="ev-add">Add</button></div>`;
    $$("#sw button", box).forEach(b => b.onclick = () => { store.set("schedWhen", b.dataset.w); this.render(box, b.dataset.w); });
    $("#ev-add", box).onclick = async () => {
      const title = $("#ev-title", box).value.trim(); if (!title) return;
      try {
        const r = await api("/api/calendar", { method: "POST", body: { title, when: $("#ev-when", box).value.trim() || "today 9am" } });
        toast("Added to your calendar", `${r.title} · ${r.start.replace("T", " ")}${r.clash.length ? ` · overlaps ${r.clash.map(x => x.title).join(", ")}` : ""}`, r.clash.length ? "error" : "");
      } catch (e) { toast("Couldn't add", e.message, "error"); }
      this.render(box, when);
    };
  },
};

// Inbox: mail already on this PC (.eml / .mbox / Outlook), read-only. "Draft reply" asks AETHER in chat.
const Inbox = {
  async render(box, q = "") {
    const m = await api(`/api/mail?days=3&q=${encodeURIComponent(q)}`);
    const rows = m.items.map(x => `<div class="item mail"><div class="grow"><div class="mail-top">${x.vip ? `<span class="chip accent">★</span>` : ""}<b>${esc(x.sender)}</b><span class="muted">${ago(new Date(x.date).getTime() / 1000)}</span></div>
        <div>${esc(x.subject || "(no subject)")}</div><div class="meta mail-snip">${esc(x.snippet.slice(0, 160))}</div></div>
        <div class="actions"><button class="ghost" data-reply="${esc(x.id)}">Draft reply</button></div></div>`).join("");
    box.innerHTML = `<h3><span class="grow">Inbox</span><input class="field" id="mail-q" placeholder="Search mail…" value="${esc(q)}" style="max-width:220px"></h3>
      ${m.files || m.outlook ? rows || `<div class="empty">${q ? "No mail matches." : "No new mail in the last 3 days."}</div>`
        : `<div class="empty">No mail connected. Put <b>.eml</b> files or an <b>.mbox</b> archive in <code>${esc(m.folder)}</code>, or turn on Outlook in settings. Read-only, and AETHER never sends mail.</div>`}`;
    const input = $("#mail-q", box);
    input.onkeydown = e => { if (e.key === "Enter") this.render(box, input.value.trim()); };
    $$("[data-reply]", box).forEach(b => b.onclick = () => {
      const x = m.items.find(i => i.id === b.dataset.reply);
      go("chat"); Chat.t.value = `Draft a short reply to ${x.sender} <${x.address}> about "${x.subject}". Use the draft_email tool.`; Chat.send();
    });
  },
};

// Routines: one-line voice macros ("focus mode"). Saying the name runs the steps; every step is still policy-checked.
const Routines = {
  async render(box) {
    const { items, tools } = await api("/api/routines");
    box.innerHTML = `<h3><span class="grow">Routines</span><span class="r-hint">Say the name in chat or by voice, e.g. “focus mode”</span></h3>
      <div class="routines">${items.map(r => `<div class="routine"><div class="r-head"><b>${esc(r.name)}</b><span class="muted">${r.runs ? `ran ${r.runs}×` : "never run"}</span></div>
        ${r.description ? `<div class="muted" style="font-size:12.5px">${esc(r.description)}</div>` : ""}<pre class="r-steps">${esc(r.steps)}</pre>
        <div class="row"><button class="primary" data-run="${esc(r.name)}">Run</button><button class="ghost" data-edit="${esc(r.name)}">Edit</button><button class="icon-btn" data-del="${esc(r.name)}" title="Delete">${icon("trash")}</button></div></div>`).join("")
        || `<div class="empty">No routines yet. Make one below: a name and one tool per line.</div>`}</div>
      <details class="r-new" ${items.length ? "" : "open"}><summary>New routine</summary>
        <div class="row" style="margin-top:10px"><input class="field" id="r-name" placeholder="name, e.g. focus mode" style="flex:1"><input class="field" id="r-desc" placeholder="what it's for (optional)" style="flex:2"></div>
        <textarea class="field mono" id="r-steps" rows="4" style="width:100%;margin-top:8px;font-size:12.5px" placeholder="app_open name=spotify&#10;remind text=&quot;stand up and stretch&quot; when=&quot;in 50 minutes&quot;"></textarea>
        <div class="row" style="margin-top:8px"><button class="primary" id="r-save">Save routine</button><span class="muted" style="font-size:12px">Tools: ${tools.filter(t => !["run_routine", "start_goal"].includes(t)).map(esc).join(", ")}</span></div></details>`;
    $$("[data-run]", box).forEach(b => b.onclick = async () => {
      b.disabled = true; b.textContent = "Running…";
      try { const r = await api(`/api/routines/${encodeURIComponent(b.dataset.run)}/run`, { method: "POST" }); const ok = r.results.every(x => x.ok); toast(ok ? "Routine done" : "Routine stopped", r.text.replace(/\*\*/g, ""), ok ? "" : "error", 10000); }
      catch (e) { toast("Couldn't run", e.message, "error"); }
      this.render(box);
    });
    $$("[data-del]", box).forEach(b => b.onclick = async () => { await api(`/api/routines/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" }); this.render(box); });
    $$("[data-edit]", box).forEach(b => b.onclick = () => { const r = items.find(x => x.name === b.dataset.edit); $("#r-name", box).value = r.name; $("#r-desc", box).value = r.description || ""; $("#r-steps", box).value = r.steps; $(".r-new", box).open = true; $("#r-steps", box).focus(); });
    $("#r-save", box).onclick = async () => {
      try { const r = await api("/api/routines", { method: "POST", body: { name: $("#r-name", box).value, description: $("#r-desc", box).value, steps: $("#r-steps", box).value } }); toast("Routine saved", `Say “${r.name}” to run it.`); this.render(box); }
      catch (e) { toast("Not saved", e.message, "error", 10000); }
    };
  },
};

RENDER.memory = async (p, tab = store.get("memTab", "memories")) => {
  p.innerHTML = `<div class="page-head"><div class="grow"><h1>Memory</h1><p>Everything AETHER remembers, likes and has noticed. All of it can be corrected or deleted.</p></div>
    <div class="tabs" id="mt">${["memories", "preferences", "patterns"].map(t => `<button data-t="${t}" class="${t === tab ? "active" : ""}">${t[0].toUpperCase() + t.slice(1)}</button>`).join("")}</div></div><div id="mb"></div>`;
  $$("#mt button", p).forEach(b => b.onclick = () => { store.set("memTab", b.dataset.t); RENDER.memory(p, b.dataset.t); });
  const mb = $("#mb", p);
  if (tab === "memories") {
    const layers = ["all", "semantic", "episodic", "procedural", "document", "observation", "prospective"];
    mb.innerHTML = `<div class="row" style="margin-bottom:14px"><input class="field" id="mq" placeholder="Search memories (hybrid search)" style="flex:1;min-width:220px">
      <div class="tabs" id="ml">${layers.map((l, i) => `<button data-l="${l}" class="${i ? "" : "active"}">${l}</button>`).join("")}</div></div><div class="card" id="mlist"></div>`;
    const show = async (layer, q) => {
      const items = q ? (await api("/api/search?q=" + encodeURIComponent(q))).memories : await api("/api/memories" + (layer && layer !== "all" ? "?layer=" + layer : ""));
      $("#mlist", mb).innerHTML = items.map(m => `<div class="item"><div class="grow">${esc(m.text.slice(0, 400))}
        <div class="meta"><span class="chip">${esc(m.layer)}</span> ${m.source ? `· ${esc(m.source)}` : ""} ${m.confidence != null ? `· conf ${m.confidence.toFixed(2)}` : ""} ${m.score ? `· match ${m.score}` : ""} ${m.meta?.path || m.path ? `· ${esc(m.meta?.path || m.path)}` : ""}</div></div>
        <div class="actions"><button class="icon-btn" data-f="${m.id}" title="Forget">${icon("trash")}</button></div></div>`).join("") || `<div class="empty">Nothing here yet.</div>`;
      $$("[data-f]", mb).forEach(b => b.onclick = async () => { await api("/api/memories/" + b.dataset.f, { method: "DELETE" }); b.closest(".item").remove(); toast("Forgotten"); });
    };
    let layer = "all", timer;
    $$("#ml button", mb).forEach(b => b.onclick = () => { $$("#ml button", mb).forEach(x => x.classList.toggle("active", x === b)); layer = b.dataset.l; show(layer, ""); $("#mq", mb).value = ""; });
    $("#mq", mb).oninput = e => { clearTimeout(timer); timer = setTimeout(() => show(layer, e.target.value.trim()), 250); };
    show("all", "");
  } else if (tab === "preferences") {
    const [prefs, tl] = await Promise.all([api("/api/preferences"), api("/api/preferences/timeline")]);
    mb.innerHTML = `<div class="grid two"><div class="card"><h3>How AETHER talks to you</h3>
      <div class="row" style="margin-bottom:8px"><input class="field" id="np" placeholder="Add your own rule, e.g. Use metric units" style="flex:1"><button class="ghost" id="addp">Add</button></div>
      ${prefs.map(x => `<div class="item"><button class="switch ${x.active ? "on" : ""}" data-k="${esc(x.key)}" data-a="${x.active ? "off" : "on"}" title="${x.active ? "Disable" : "Enable"}"></button>
        <div class="grow">${esc(x.instruction)}<div class="meta">${esc(x.source)} · confidence ${x.confidence.toFixed(2)}</div></div>
        <div class="actions"><button class="icon-btn" data-k="${esc(x.key)}" data-a="delete" title="Delete">${icon("trash")}</button></div></div>`).join("") || `<div class="empty">Tell AETHER what you like or hate about its replies ("stop the long intros").</div>`}</div>
      <div class="card"><h3>Timeline</h3>${tl.slice(0, 40).map(e => `<div class="item"><div class="grow">${esc(e.key)}<div class="meta">${esc(e.change)} · ${ago(e.t)}</div></div></div>`).join("") || `<div class="empty">No changes yet.</div>`}</div></div>`;
    $("#addp", mb).onclick = async () => { const v = $("#np", mb).value.trim(); if (!v) return; await api("/api/preferences", { method: "POST", body: { key: "", action: "add", value: v } }); RENDER.memory(p, tab); };
    $$("[data-k]", mb).forEach(b => b.onclick = async () => { await api("/api/preferences", { method: "POST", body: { key: b.dataset.k, action: b.dataset.a } }); RENDER.memory(p, tab); });
  } else {
    const obs = await api("/api/observations");
    mb.innerHTML = `<div class="card"><h3><span class="grow">Patterns AETHER noticed</span><button class="ghost" id="an">Look for new patterns</button></h3>
      <p class="muted" style="margin-top:-4px">These are guesses, never facts. AETHER only uses the ones you confirm, and only to help you, never to pressure you.</p>
      ${obs.map(o => `<div class="item"><div class="grow">${esc(o.text)}<div class="meta"><span class="chip ${o.status === "confirmed" ? "good" : o.status === "rejected" ? "bad" : "warn"}">${o.status}</span> · ${esc(o.kind)} · conf ${o.confidence.toFixed(2)} · ${esc((o.evidence || []).slice(-1)[0] || "")}</div></div>
        <div class="actions"><button class="ghost" data-o="${o.id}" data-s="confirmed">That's right</button><button class="ghost" data-o="${o.id}" data-s="rejected">Not true</button><button class="icon-btn" data-o="${o.id}" data-s="delete">${icon("trash")}</button></div></div>`).join("") || `<div class="empty">No patterns yet. They need a few weeks of conversations.</div>`}</div>`;
    $("#an", mb).onclick = async () => { const r = await api("/api/observations/analyse", { method: "POST" }); toast(r.proposed.length ? `${r.proposed.length} new pattern(s)` : "No new patterns"); RENDER.memory(p, tab); };
    $$("[data-o]", mb).forEach(b => b.onclick = async () => { await api(`/api/observations/${b.dataset.o}?status=${b.dataset.s}`, { method: "POST" }); RENDER.memory(p, tab); });
  }
};

RENDER.knowledge = async (p) => {
  p.className = ""; p.style.height = "100%";
  p.innerHTML = `<div class="kg"><div class="kg-canvas"><canvas id="kgc"></canvas>
      <div class="kg-head"><input class="field" id="kgq" placeholder="Find an entity" style="max-width:240px"><span class="chip" id="kgn"></span><span class="grow"></span>
        <button class="ghost kg-tool" id="wi-btn" title="Explore a hypothetical without changing memory">∴ What if…</button>
        <button class="ghost kg-tool" id="tt-btn" title="Replay how AETHER's beliefs changed">◷ Time travel</button></div>
      <div class="wi-panel" id="wi" hidden>
        <div class="wi-title">Counterfactual reasoning <span class="muted">· real memory is never touched</span></div>
        <div class="row"><input class="field mono" id="wi-q" placeholder="kabir parent_of zoe; !user likes coffee" style="flex:1;font-size:12.5px"><button class="primary" id="wi-run">Run</button></div>
        <div class="wi-hint muted">One assumption per <b>;</b> as <span class="mono">subject predicate object</span>. Prefix <span class="mono">!</span> to assume something is false.</div>
        <div id="wi-out"></div></div>
      <div class="tt-bar" id="tt" hidden>
        <button class="tt-play" id="tt-play" title="Play">▶</button>
        <div class="tt-track"><input type="range" id="tt-r" min="0" max="0" value="0"><div class="tt-ticks" id="tt-ticks"></div></div>
        <div class="tt-info"><b id="tt-when">now</b><span id="tt-what"></span></div></div>
      <div class="kg-legend" id="kg-legend"><span><i style="background:var(--line2)"></i>stated fact</span><span><i style="border-top:2px dashed var(--accent2)"></i>derived by rules</span></div></div>
    <aside class="kg-side" id="kgs"></aside></div>`;
  let [g, rules] = await Promise.all([api("/api/graph"), api("/api/rules")]);
  const live = g;
  const key = l => `${l.source}|${l.label}|${l.target}`;
  const count = () => { $("#kgn", p).textContent = `${g.nodes.length} entities · ${g.links.length} links`; };
  count();
  const side = $("#kgs", p);
  const showSide = async (node) => {
    const rel = node ? g.links.filter(l => l.source === node.id || l.target === node.id) : [];
    side.innerHTML = (node ? `<h3 style="font:600 20px var(--display);margin:0 0 4px">${esc(node.id)}</h3><p class="muted" style="margin:0 0 14px">${rel.length} relations</p>
      ${rel.map(l => `<div class="item"><div class="grow">${esc(l.source)} <span class="chip ${l.mark === "gain" ? "good" : l.mark === "lose" ? "bad" : l.kind === "derived" ? "violet" : "accent"}">${esc(l.label.replace(/_/g, " "))}</span> ${esc(l.target)}
        <div class="meta">${l.mark === "gain" ? "would follow" : l.mark === "lose" ? "would no longer hold" : l.mark === "assume" ? "assumed" : l.kind === "derived" ? `<a href="#" data-why="${esc(l.source)}|${esc(l.label)}|${esc(l.target)}">why?</a>` : "stated"}</div><pre class="out" hidden></pre></div>
        ${l.id && !l.mark && g === live ? `<div class="actions"><button class="icon-btn" data-x="${l.id}" title="Retract">${icon("trash")}</button></div>` : ""}</div>`).join("")}
      <h4 class="side-h">Belief history</h4><div class="hist" id="hist"><div class="muted" style="font-size:12.5px">Loading…</div></div>` : `<h3 style="font:600 18px var(--display);margin:0 0 6px">Knowledge graph</h3>
      <p class="muted" style="margin:0 0 12px">Facts you've told AETHER, plus what its rules derive from them. Click a node. Try <b>What if…</b> to reason about hypotheticals, or <b>Time travel</b> to replay how its beliefs changed.</p>`)
      + `<div class="card" style="margin-top:18px;padding:14px"><h3>Ask with logic</h3><div class="row"><input class="field" id="lq" placeholder="parent_of(?p, riya) &amp; lives_in(?p, ?c)" style="flex:1"><button class="ghost" id="lqb">Ask</button></div><pre class="out" id="lqo" hidden></pre></div>
      <div class="card" style="margin-top:14px;padding:14px"><h3>Your rules</h3>${rules.yours.map(r => `<div class="item"><div class="grow mono" style="font-size:12px">${esc(r)}</div><div class="actions"><button class="icon-btn" data-rr="${esc(r)}">${icon("trash")}</button></div></div>`).join("") || `<div class="muted" style="font-size:13px">None yet.</div>`}
        <div class="row" style="margin-top:8px"><input class="field mono" id="nr" placeholder="works_at(?x,?o) &amp; located_in(?o,?c) -> works_in(?x,?c)" style="flex:1;font-size:12px"><button class="ghost" id="nrb">Add</button></div>
        <details style="margin-top:10px"><summary class="muted" style="cursor:pointer;font-size:12px">${rules.all.length} built-in rules</summary>${rules.all.map(r => `<div class="mono muted" style="font-size:11.5px;padding:3px 0">${esc(r)}</div>`).join("")}</details></div>`;
    $$("[data-why]", side).forEach(a => a.onclick = async (e) => { e.preventDefault(); const [s, pp, o] = a.dataset.why.split("|"); const r = await api(`/api/why?s=${encodeURIComponent(s)}&p=${encodeURIComponent(pp)}&o=${encodeURIComponent(o)}`); const pre = a.closest(".grow").querySelector("pre"); pre.hidden = false; pre.textContent = r.proof.join("\n"); });
    $$("[data-x]", side).forEach(b => b.onclick = async () => { await api("/api/facts/" + b.dataset.x, { method: "DELETE" }); toast("Fact retracted"); RENDER.knowledge(p); });
    $$("[data-rr]", side).forEach(b => b.onclick = async () => { await api("/api/rules", { method: "DELETE", body: { text: b.dataset.rr } }); RENDER.knowledge(p); });
    $("#lqb", side).onclick = async () => { const o = $("#lqo", side); o.hidden = false; try { const r = await api("/api/ask", { method: "POST", body: { text: $("#lq", side).value } }); o.textContent = r.bindings.length ? r.bindings.map(b => JSON.stringify(b)).join("\n") : "No matches."; } catch (e) { o.textContent = e.message; } };
    $("#nrb", side).onclick = async () => { try { const r = await api("/api/rules", { method: "POST", body: { text: $("#nr", side).value } }); toast("Rule added", `${r.derived} new facts derived` + (r.violations.length ? `, ${r.violations.length} constraint violations` : "")); RENDER.knowledge(p); } catch (e) { toast("Rule rejected", e.message, "error"); } };
    if (node) {
      const { events } = await api("/api/kg/history?entity=" + encodeURIComponent(node.id)).catch(() => ({ events: [] }));
      const box = $("#hist", side); if (!box) return;
      const when = t => new Date(t * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
      box.innerHTML = events.filter(e => !e.derived).reverse().map(e => `<div class="hist-row ${e.retired ? "gone" : ""}"><span class="hist-dot"></span><div>
          <div>${esc(e.s)} <b>${esc(e.p.replace(/_/g, " "))}</b> ${esc(e.o)}</div>
          <div class="meta">learned ${when(e.learned)} · ${esc(e.source.replace(/_/g, " "))}${e.retired ? ` · <span class="bad">replaced ${when(e.retired)}</span>` : ""}</div></div></div>`).join("")
        || `<div class="muted" style="font-size:12.5px">No stated facts about ${esc(node.id)} yet.</div>`;
    }
  };
  S.graph = new GraphView($("#kgc", p), { onSelect: showSide });
  S.graph.setData(g); showSide(null);
  $("#kgq", p).oninput = e => { S.graph.filter = e.target.value; const n = g.nodes.find(n => e.target.value && n.id.toLowerCase().includes(e.target.value.toLowerCase())); if (n && e.target.value.length > 2) S.graph.focus(n.id); };
  if (!g.nodes.length) side.insertAdjacentHTML("afterbegin", `<div class="notice">The graph is empty. Tell AETHER facts like "My sister Riya lives in Pune" and they appear here.</div>`);
  const show = (data, opts) => { g = data; S.graph.setData(data, opts); count(); };

  // ---- what if
  const wi = $("#wi", p);
  const resetMarks = () => show(live, { keepView: true });
  $("#wi-btn", p).onclick = () => { wi.hidden = !wi.hidden; $("#wi-btn", p).classList.toggle("on", !wi.hidden); if (wi.hidden) { resetMarks(); $("#wi-out", p).innerHTML = ""; } else { closeTT(); $("#wi-q", p).focus(); } };
  $("#wi-q", p).onkeydown = e => { if (e.key === "Enter") $("#wi-run", p).click(); };
  $("#wi-run", p).onclick = async () => {
    const out = $("#wi-out", p);
    let r;
    try { r = await api("/api/whatif", { method: "POST", body: { text: $("#wi-q", p).value } }); }
    catch (e) { out.innerHTML = `<div class="notice sec">${esc(e.message)}</div>`; return; }
    const split = f => { const [s, pp, ...o] = f.split(" "); return { source: s, label: pp, target: o.join(" ") }; };
    const lost = new Set([...r.lost.map(x => x.fact), ...r.overridden, ...r.retracted].map(f => key(split(f))));
    const links = live.links.map(l => ({ ...l, mark: lost.has(key(l)) ? "lose" : undefined }));
    const add = (f, kind, mark) => { const l = split(f); if (!links.some(x => key(x) === key(l))) links.push({ ...l, kind, mark, id: null }); else links.find(x => key(x) === key(l)).mark = mark; };
    r.assumptions.forEach(f => add(f, "fact", "assume"));
    r.gained.forEach(x => add(x.fact, "derived", "gain"));
    const nodes = new Map(live.nodes.map(n => [n.id, { ...n }]));
    for (const l of links) for (const id of [l.source, l.target]) {
      if (!nodes.has(id)) nodes.set(id, { id, degree: 1, user: id === "user", mark: l.mark || "assume" });
      else if (l.mark && l.mark !== "lose") nodes.get(id).mark ||= l.mark;
    }
    show({ nodes: [...nodes.values()], links }, { keepView: true });
    const li = (cls, sym, f, why = "") => `<div class="wi-row ${cls}"><span>${sym}</span><div>${esc(f)}${why ? `<div class="meta">${why}</div>` : ""}</div></div>`;
    out.innerHTML = `<div class="wi-res">
      ${r.overridden.map(f => li("lose", "⇄", f, "would be replaced")).join("")}
      ${r.retracted.map(f => li("lose", "✕", f, "assumed false")).join("")}
      ${r.gained.map(x => li("gain", "+", x.fact, "because " + x.because.map(esc).join(" &amp; "))).join("")}
      ${r.lost.map(x => li("lose", "−", x.fact, "no longer follows")).join("")}
      ${r.new_violations.map(v => li("lose", "!", v, "constraint broken")).join("")}
      ${r.resolved_violations.map(v => li("gain", "✓", v, "constraint resolved")).join("")}
      ${!(r.gained.length || r.lost.length || r.overridden.length || r.new_violations.length || r.retracted.length) ? `<div class="muted" style="font-size:12.5px">Nothing else changes: no rule depends on this.</div>` : ""}
      </div><button class="ghost" id="wi-clear" style="margin-top:8px">Clear</button>`;
    $("#wi-clear", p).onclick = () => { out.innerHTML = ""; resetMarks(); };
  };

  // ---- time travel
  const tt = $("#tt", p), range = $("#tt-r", p);
  let times = [], evs = [], prevKeys = new Set(live.links.map(key)), req = 0;
  const closeTT = () => { tt.hidden = true; $("#tt-btn", p).classList.remove("on"); clearInterval(S.play); $("#tt-play", p).textContent = "▶"; $("#kg-legend", p).hidden = false; };
  const at = async (i) => {
    const mine = ++req, isNow = i >= times.length - 1;
    const data = await api(isNow ? "/api/graph" : `/api/graph?at=${times[i] + 1e-3}`);
    if (mine !== req) return;
    const keys = new Set(data.links.map(key));
    data.links.forEach(l => { if (!prevKeys.has(key(l))) l.mark = "new"; });
    prevKeys = keys;
    show(data, { keepView: true });
    const t = times[i];
    $("#tt-when", p).textContent = isNow ? "Now" : new Date(t * 1000).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const here = evs.filter(e => Math.abs(e.t - t) < 1e-6);
    $("#tt-what", p).innerHTML = here.map(e => `<span class="${e.kind}">${e.kind === "learned" ? "+ learned" : "− replaced"} <b>${esc(e.f)}</b></span>`).join("") || (isNow ? "Current beliefs" : "");
  };
  $("#tt-btn", p).onclick = async () => {
    if (!tt.hidden) { closeTT(); show(live, { keepView: true }); return; }
    wi.hidden = true; $("#wi-btn", p).classList.remove("on");
    const { events } = await api("/api/kg/history?limit=2000");
    evs = events.filter(e => !e.derived).flatMap(e => [{ t: e.learned, kind: "learned", f: `${e.s} ${e.p.replace(/_/g, " ")} ${e.o}` },
      ...(e.retired ? [{ t: e.retired, kind: "retired", f: `${e.s} ${e.p.replace(/_/g, " ")} ${e.o}` }] : [])]);
    times = [...new Set(evs.map(e => e.t))].sort((a, b) => a - b);
    if (!times.length) { toast("Nothing to replay yet", "Tell AETHER a few facts first."); return; }
    tt.hidden = false; $("#tt-btn", p).classList.add("on"); $("#kg-legend", p).hidden = true;
    range.max = times.length - 1; range.value = times.length - 1;
    const span = Math.max(times[times.length - 1] - times[0], 1);
    $("#tt-ticks", p).innerHTML = times.map(t => `<i style="left:${((t - times[0]) / span) * 100}%"></i>`).join("");
    prevKeys = new Set(live.links.map(key)); at(times.length - 1);
  };
  range.oninput = () => at(+range.value);
  $("#tt-play", p).onclick = () => {
    if (S.play) { clearInterval(S.play); S.play = 0; $("#tt-play", p).textContent = "▶"; return; }
    $("#tt-play", p).textContent = "❚❚";
    let i = +range.value >= times.length - 1 ? 0 : +range.value;
    prevKeys = new Set();
    const tick = () => { range.value = i; at(i); if (++i >= times.length) { clearInterval(S.play); S.play = 0; $("#tt-play", p).textContent = "▶"; } };
    tick(); S.play = setInterval(tick, 900);
  };
};

RENDER.goals = async (p, sel) => {
  const { goals, progress } = await api("/api/goals");
  p.innerHTML = `<div class="page-head"><div class="grow"><h1>Mission control</h1><p>Multi-step goals AETHER runs in the background: an executive plans a task graph, specialist agents run it, a verifier checks every result.</p></div></div>
    <div class="card" style="margin-bottom:16px"><div class="row"><input class="field" id="ng" placeholder="e.g. Research three laptops under ₹80k for ML and compare them in a table" style="flex:1"><button class="primary" id="ngb">Start goal</button></div>
      <p class="muted" style="font-size:12px;margin:8px 0 0">Starting a goal asks for approval. Every action inside it still goes through the policy engine.</p></div>
    <div class="card mc" id="mc" hidden><h3><span class="grow" id="mc-title"></span><span class="chip" id="mc-status"></span><button class="ghost" id="gc" hidden>Cancel</button></h3><div class="dag-scroll" id="dag"></div></div>
    <div class="grid two"><div class="card"><h3>All goals</h3>${goals.map(g => `<div class="item goal-item" style="cursor:pointer" data-g="${g.id}"><div class="grow">${esc(g.text)}<div class="meta">#${g.id}</div></div><span class="chip ${g.status === "done" ? "good" : g.status === "failed" ? "bad" : "accent"}">${g.status}</span></div>`).join("") || `<div class="empty">No goals yet.</div>`}</div>
    <div class="card" id="gd"><h3>Live log</h3><div id="glog">${progress.slice().reverse().map(x => `<div class="item"><div class="grow mono" style="font-size:12px">${esc(x)}</div></div>`).join("") || `<div class="empty">Select a goal to see its task graph.</div>`}</div></div></div>`;
  $("#ngb", p).onclick = async () => { const v = $("#ng", p).value.trim(); if (!v) return; try { const r = await api("/api/goals", { method: "POST", body: { text: v } }); toast("Goal queued", r.message); RENDER.goals(p); } catch (e) { toast("Couldn't start", e.message, "error"); } };
  const detail = async (gid) => {
    if (!p.isConnected) { clearInterval(S.poll); return; }
    const [g, lg] = await Promise.all([api("/api/goals/" + gid), api("/api/goals")]);
    const running = ["running", "pending"].includes(g.status);
    $$(".goal-item", p).forEach(x => x.classList.toggle("on", x.dataset.g == gid));
    $("#mc", p).hidden = false; $("#mc-title", p).textContent = `#${g.id} · ${g.text}`;
    $("#mc-status", p).textContent = g.status; $("#mc-status", p).className = "chip " + (g.status === "done" ? "good" : g.status === "failed" ? "bad" : "accent");
    const gc = $("#gc", p); gc.hidden = !running; gc.onclick = async () => { await api(`/api/goals/${gid}/cancel`, { method: "POST" }); toast("Cancelling"); };
    if (g.tasks.length) renderDag($("#dag", p), g.tasks); else $("#dag", p).innerHTML = `<div class="empty">${running ? "The executive is planning the task graph…" : "No tasks were planned."}</div>`;
    $("#gd", p).innerHTML = `<h3>Tasks</h3>${g.tasks.map(t => `<div class="task ${t.status}"><span class="s"></span><div><b style="font-weight:600">${esc(t.description)}</b><div class="meta muted" style="font-size:12px">${t.agent} · ${t.status} · attempt ${t.attempts}/${t.max_attempts}${t.success_criteria ? " · done when: " + esc(t.success_criteria) : ""}</div>
        ${t.result ? `<details><summary class="muted" style="font-size:12px;cursor:pointer">result</summary><div class="md" style="font-size:13px">${markdown(t.result.slice(0, 3000))}</div></details>` : ""}</div></div>`).join("")}
      ${g.report ? `<details style="margin-top:10px"><summary class="muted" style="cursor:pointer">Report</summary><pre class="out">${esc(g.report)}</pre></details>` : ""}
      ${lg.progress.length ? `<h3 style="margin-top:14px">Live log</h3>${lg.progress.slice(-8).reverse().map(x => `<div class="mono muted" style="font-size:11.5px;padding:2px 0">${esc(x)}</div>`).join("")}` : ""}`;
    clearInterval(S.poll);
    if (running) S.poll = setInterval(() => detail(gid), 1500);
  };
  $$("[data-g]", p).forEach(x => x.onclick = () => detail(x.dataset.g));
  const auto = sel || goals.find(g => ["running", "pending"].includes(g.status))?.id || goals[0]?.id;
  if (auto) detail(auto);
};

// ------------------------------------------------------------------ command palette
const COMMANDS = [
  ["New chat", "Start a fresh conversation", "plus", () => { go("chat"); Chat.newChat(); }],
  ...Object.entries(VIEWS).map(([k, v]) => [`Go to ${v}`, "", { chat: "chat", today: "sun", memory: "brain", knowledge: "graph", goals: "target", lab: "flask", self: "pulse", activity: "shield", settings: "gear" }[k], () => go(k)]),
  ["HUD mode", "Full-screen voice interface (Ctrl+Shift+H)", "orb", () => HUD.open()],
  ["Morning briefing", "Weather, calendar, what's due, email and headlines, read aloud", "sun", () => { HUD.open(); HUD.ask("brief me"); }],
  ["What's on my screen?", "Look at the screen with the local vision model (asks first)", "scan", () => { go("chat"); Chat.t.value = "Look at my screen. What's on it, and is there anything I should know?"; Chat.send(); }],
  ["Inspect the last turn", "Glass-box trace: timeline, retrieval, proofs, verification", "scan", () => Inspector.open()],
  ["Toggle thinking panel", "Show or hide the live pipeline", "panel", () => { S.showMind = !S.showMind; store.set("showMind", S.showMind); Chat.root && Chat.applyLayout(); }],
  ["Talk", "Push to talk (click the mic again to stop)", "mic", () => { go("chat"); Chat.toggleMic?.(); }],
  ["Toggle theme", "Light or dark", "sun", () => { S.theme = S.theme === "light" ? "dark" : "light"; store.set("theme", S.theme); document.documentElement.dataset.theme = S.theme; }],
  ["Emergency stop", "Block every computer action", "stop", () => $("#stop").click()],
  ["Undo last file change", "", "trash", async () => toast((await api("/api/undo", { method: "POST" })).message)],
  ["Look for new patterns about me", "", "brain", async () => { const r = await api("/api/observations/analyse", { method: "POST" }); toast(r.proposed.length ? `${r.proposed.length} new pattern(s)` : "No new patterns"); }],
];
const Palette = {
  items: [], sel: 0, timer: 0,
  open() { $("#palette").hidden = false; const q = $("#palette-q"); q.value = ""; q.focus(); this.update(""); },
  close() { $("#palette").hidden = true; },
  async update(q) {
    const ql = q.toLowerCase();
    let items = COMMANDS.filter(([t, s]) => !ql || (t + " " + s).toLowerCase().includes(ql)).map(([t, s, ic, fn]) => ({ group: "Commands", t, s, ic, fn }));
    if (ql.length > 1 && !q.startsWith(">")) items.push({ group: "Chat", t: `Ask AETHER: “${q}”`, s: "", ic: "send", fn: () => { go("chat"); Chat.t.value = q; Chat.send(); } });
    this.items = items; this.sel = 0; this.draw();
    clearTimeout(this.timer);
    if (ql.length > 2) this.timer = setTimeout(async () => {
      try {
        const r = await api("/api/search?q=" + encodeURIComponent(q));
        const more = [
          ...r.memories.slice(0, 5).map(m => ({ group: "Memories", t: m.text, s: m.layer, ic: "brain", fn: () => go("memory") })),
          ...r.facts.slice(0, 5).map(f => ({ group: "Facts", t: `${f.s} ${f.p.replace(/_/g, " ")} ${f.o}`, s: "", ic: "graph", fn: () => { go("knowledge"); setTimeout(() => S.graph?.focus(f.s), 400); } })),
          ...r.conversations.slice(0, 6).map(c => ({ group: "Conversations", t: c.snippet, s: c.title, ic: "chat", fn: () => { go("chat"); Chat.open(c.session); } })),
        ];
        if ($("#palette-q").value === q) { this.items = items.concat(more); this.draw(); }
      } catch {}
    }, 180);
  },
  draw() {
    let g = "";
    $("#palette-list").innerHTML = this.items.map((it, i) => { const head = it.group !== g ? `<div class="pal-group">${(g = it.group)}</div>` : ""; return head + `<div class="pal-item ${i === this.sel ? "sel" : ""}" data-i="${i}">${icon(it.ic)}<span class="t">${esc(it.t)}</span><span class="s">${esc(it.s)}</span></div>`; }).join("") || `<div class="empty">No results</div>`;
    $$(".pal-item").forEach(el => { el.onclick = () => this.run(+el.dataset.i); el.onmousemove = () => { if (this.sel !== +el.dataset.i) { this.sel = +el.dataset.i; this.draw(); } }; });
    $(".pal-item.sel")?.scrollIntoView({ block: "nearest" });
  },
  run(i) { const it = this.items[i]; this.close(); it?.fn(); },
};
$("#palette-btn").onclick = () => Palette.open();
$("#palette-q").oninput = e => Palette.update(e.target.value);
$("#palette-q").onkeydown = e => {
  if (e.key === "ArrowDown") { e.preventDefault(); Palette.sel = Math.min(Palette.items.length - 1, Palette.sel + 1); Palette.draw(); }
  if (e.key === "ArrowUp") { e.preventDefault(); Palette.sel = Math.max(0, Palette.sel - 1); Palette.draw(); }
  if (e.key === "Enter") { e.preventDefault(); Palette.run(Palette.sel); }
};
$("#palette").onclick = e => { if (e.target.id === "palette") Palette.close(); };

// ------------------------------------------------------------------ keyboard
document.addEventListener("keydown", e => {
  const mod = e.ctrlKey || e.metaKey;
  if (HUD.el) return;  // the HUD owns the keyboard while it is open
  if (e.key === "Escape") { if (!$("#palette").hidden) Palette.close(); return; }
  if (mod && e.shiftKey && e.key.toLowerCase() === "h") { e.preventDefault(); HUD.open(); return; }
  if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); $("#palette").hidden ? Palette.open() : Palette.close(); }
  else if (mod && e.shiftKey && e.key.toLowerCase() === "x") { e.preventDefault(); $("#stop").click(); }
  else if (mod && e.shiftKey && e.key.toLowerCase() === "v") { e.preventDefault(); go("chat"); Chat.toggleMic?.(); }
  else if (mod && e.key.toLowerCase() === "n") { e.preventDefault(); go("chat"); Chat.newChat(); }
  else if (mod && e.key === ".") { e.preventDefault(); S.showMind = !S.showMind; store.set("showMind", S.showMind); Chat.root && Chat.applyLayout(); }
  else if (mod && e.key === ",") { e.preventDefault(); go("settings"); }
  else if (mod && /^[1-8]$/.test(e.key)) { e.preventDefault(); go(Object.keys(VIEWS)[+e.key - 1]); }
  else if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { e.preventDefault(); go("chat"); Chat.t.focus(); }
});
window.addEventListener("hashchange", () => { const v = location.hash.slice(1); if (v && v !== S.view) go(v); });

// ------------------------------------------------------------------ boot
Inspector.init(api);
HUD.init({ api, streamChat, onTurn: () => { refreshStatus(); Chat.root && Chat.loadConvos(); } });
$("#hud-btn").onclick = () => HUD.open();
if (new URLSearchParams(location.search).has("hud")) {  // the tray's global hotkey / "Morning briefing" menu item
  const brief = new URLSearchParams(location.search).get("hud") === "brief";
  history.replaceState(null, "", "/" + location.hash); HUD.open(); if (brief) HUD.ask("good morning");
}  // the tray's global hotkey
go(VIEWS[location.hash.slice(1)] ? location.hash.slice(1) : "chat");
refreshStatus(); setInterval(refreshStatus, 10000);
setInterval(pollPending, 700);
pollNotifications(); setInterval(pollNotifications, 30000);
