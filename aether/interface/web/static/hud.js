// HUD mode: a full-screen voice interface. The orb is drawn from real audio: the microphone's
// spectrum while you talk, AETHER's own synthesized voice while it answers (WebAudio analysers).
// Hold Space to talk; release to send. Everything stays on 127.0.0.1.
import { esc } from "./md.js";

// What the HUD shows and says: the answer without Markdown syntax, code or tables.
const plain = t => t.replace(/```[\s\S]*?(```|$)/g, " ").replace(/^\s*\|.*\|\s*$/gm, " ").replace(/^\s*>\s?/gm, "")
  .replace(/\[(web|doc|memory)\s*\d+\]/gi, "").replace(/https?:\/\/\S+/g, "a link on screen").replace(/^\s*[-•]\s+/gm, "")
  .replace(/[*_#`]/g, "").replace(/\s+/g, " ").replace(/\s+([.,!?;:])/g, "$1").trim();

// Port of aether/speech/chunker.py: complete sentences out of a token stream, code and tables skipped.
const END = /(?<!\n\d)(?<!\n\d\d)(?<!^\d)(?:(?<!\b(?:Mr|Ms|Dr|St|Sr|Jr|vs|Mrs|Prof|e\.g|i\.e|etc))\.|[!?…])["')\]]*\s+|\n\s*\n|\n(?=\s*(?:[-*•]|\d+[.)])\s)/g;
export class Chunker {
  constructor(min = 24) { this.min = min; this.buf = ""; this.code = false; }
  feed(t) {
    this.buf += t; const out = [];
    for (;;) {
      if (this.code) {
        const i = this.buf.indexOf("```"); if (i < 0) { this.buf = this.buf.slice(-2); return out; }
        const e = this.buf.indexOf("\n", i + 3); if (e < 0) return out;
        this.buf = this.buf.slice(e + 1); this.code = false; continue;
      }
      const i = this.buf.indexOf("```");
      if (i >= 0) { out.push(...this.split(this.buf.slice(0, i), true)); this.buf = this.buf.slice(i + 3); this.code = true; continue; }
      out.push(...this.split(null, false)); return out;
    }
  }
  flush() { if (this.code) { this.buf = ""; this.code = false; return []; } const o = this.split(this.buf, true); this.buf = ""; return o; }
  split(text, final) {
    const src = text ?? this.buf, out = []; let start = 0;
    for (const m of src.matchAll(END)) {
      const piece = src.slice(start, m.index + m[0].length);
      if (piece.trim().length < this.min) continue;
      const s = plain(piece); if (s) out.push(s); start = m.index + m[0].length;
    }
    let rest = src.slice(start);
    if (final) { const s = plain(rest); if (s) out.push(s); rest = ""; }
    if (text == null) this.buf = rest;
    return out;
  }
}

export const HUD = {
  deps: null, el: null, ctx: null, analyser: null, freq: null, raf: 0, mode: "idle", level: 0, spin: 0,
  rec: null, stream: null, audio: null, answer: "", t0: 0, queue: [], gen: 0, playing: false, voiceOff: false, streamDone: true,

  init(deps) { this.deps = deps; },

  open() {
    if (this.el) return;
    const el = this.el = document.createElement("div");
    el.id = "hud";
    el.innerHTML = `<canvas></canvas>
      <div class="hud-top"><div><b>AETHER</b><span>HUD</span></div><div class="hud-clock"></div><button class="hud-x" title="Exit (Esc)">Esc ✕</button></div>
      <div class="hud-bottom">
        <div class="hud-stage"><span class="hud-mode">Ready</span><span class="hud-sub">Hold <kbd>Space</kbd> to talk, or type below</span><button class="hud-brief" type="button">☀ Brief me</button></div>
        <div class="hud-ticker"><div class="hud-you"></div><div class="hud-ai"></div></div>
        <form class="hud-type"><input placeholder="Type to AETHER and press Enter" autocomplete="off"></form></div>
      <div class="hud-corner tl"></div><div class="hud-corner tr"></div><div class="hud-corner bl"></div><div class="hud-corner br"></div>`;
    document.body.appendChild(el);
    this.cv = el.querySelector("canvas"); this.g = this.cv.getContext("2d");
    el.querySelector(".hud-x").onclick = () => this.close();
    // Misheard? Click what it heard, fix it and press Enter: AETHER learns your accent from the fix.
    const you = el.querySelector(".hud-you");
    you.title = "Misheard? Click to fix it. AETHER learns from your fix.";
    you.style.cursor = "text";
    you.onclick = () => { const i = el.querySelector(".hud-type input"); i.value = you.textContent; i.focus(); i.select(); };
    el.querySelector(".hud-brief").onclick = () => this.ask("brief me");
    el.querySelector("form").onsubmit = e => { e.preventDefault(); const i = el.querySelector("input"); const v = i.value.trim(); if (v) { i.value = ""; i.blur(); this.ask(v); } };
    this.onKey = e => {
      if (e.key === "Escape") { e.preventDefault(); this.close(); return; }
      if (e.code !== "Space" || e.target.tagName === "INPUT") return;
      e.preventDefault();
      if (e.type === "keydown" && !e.repeat) this.listen();
      if (e.type === "keyup") this.stopListening();
    };
    addEventListener("keydown", this.onKey); addEventListener("keyup", this.onKey);
    this.resize = () => { const d = devicePixelRatio || 1; this.cv.width = innerWidth * d; this.cv.height = innerHeight * d; this.g.setTransform(d, 0, 0, d, 0, 0); };
    addEventListener("resize", this.resize); this.resize();
    this.ctx = new (window.AudioContext || window.webkitAudioContext)();
    this.analyser = this.ctx.createAnalyser(); this.analyser.fftSize = 256; this.analyser.smoothingTimeConstant = .78;
    this.freq = new Uint8Array(this.analyser.frequencyBinCount);
    this.t0 = performance.now(); this.voiceOff = false; this.queue = []; this.playing = false; this.streamDone = true;
    this.clock = setInterval(() => { el.querySelector(".hud-clock").textContent = new Date().toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }, 500);
    const loop = () => { this.draw(); this.raf = requestAnimationFrame(loop); }; loop();
  },

  close() {
    if (!this.el) return;
    cancelAnimationFrame(this.raf); clearInterval(this.clock);
    removeEventListener("keydown", this.onKey); removeEventListener("keyup", this.onKey); removeEventListener("resize", this.resize);
    this.stream?.getTracks().forEach(t => t.stop()); this.cancelSpeech();
    this.ctx?.close(); this.el.remove(); this.el = null; this.src = null; this.stream = null; this.rec = null;
  },

  setMode(mode, label, sub = "") {
    this.mode = mode;
    // Tell Nova (the desktop pet) what is happening, so it changes colour with the HUD.
    const state = { listening: "listening", thinking: "thinking", speaking: "speaking" }[mode] || "idle";
    fetch("/api/presence", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ state, seconds: 60 }) }).catch(() => {});
    if (!this.el) return;
    this.el.dataset.mode = mode;
    this.el.querySelector(".hud-mode").textContent = label;
    this.el.querySelector(".hud-sub").innerHTML = sub;
  },

  attach(node) { this.src?.disconnect(); this.src = node; node.connect(this.analyser); },

  async listen() {
    if (this.rec || this.mode === "thinking") return;
    this.cancelSpeech();  // barge-in: talking over AETHER stops it
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    } catch { this.setMode("idle", "Microphone blocked", "Allow microphone access for 127.0.0.1, or type below"); return; }
    if (this.ctx.state === "suspended") await this.ctx.resume();
    this.attach(this.ctx.createMediaStreamSource(this.stream));
    const chunks = [];
    this.rec = new MediaRecorder(this.stream);
    this.rec.ondataavailable = e => chunks.push(e.data);
    this.rec.onstop = async () => {
      this.stream.getTracks().forEach(t => t.stop()); this.src?.disconnect(); this.src = null;
      const blob = new Blob(chunks, { type: this.rec.mimeType || "audio/webm" }); this.rec = null;
      if (blob.size < 2000) { this.setMode("idle", "Ready", "That was too short. Hold <kbd>Space</kbd> while you speak"); return; }
      this.setMode("thinking", "Transcribing", "local speech-to-text");
      try {
        const fd = new FormData(); fd.append("file", blob, "speech.webm");
        const r = await this.deps.api("/api/transcribe", { method: "POST", body: fd });
        if (r.text) this.ask(r.text); else this.setMode("idle", "Didn't catch that", "Hold <kbd>Space</kbd> and try again");
      } catch (e) { this.setMode("idle", "Speech-to-text isn't set up", esc(e.message) + " · type below instead"); }
    };
    this.rec.start();
    this.setMode("listening", "Listening", "release <kbd>Space</kbd> to send");
  },

  stopListening() { if (this.rec && this.rec.state === "recording") this.rec.stop(); },

  async ask(text) {
    if (!this.el) return;
    this.el.querySelector(".hud-you").textContent = text;
    const ai = this.el.querySelector(".hud-ai"); ai.textContent = "";
    this.answer = "";
    this.cancelSpeech();
    this.streamDone = false; this.spoke = false;
    const chunker = new Chunker();
    let replaced = false;
    this.setMode("thinking", "Thinking", "");
    try {
      await this.deps.streamChat(text, (kind, d) => {
        if (!this.el) return;
        if (kind === "stage" && !d.done && this.mode !== "speaking") this.setMode("thinking", d.label, "");
        else if (kind === "tool" && d.running) this.setMode("thinking", `Using ${d.tool}`, "");
        else if (kind === "token") {
          this.answer += d.text; const p = plain(this.answer); ai.textContent = p.length > 200 ? "…" + p.slice(-200) : p;
          if (!replaced) chunker.feed(d.text).forEach(s => this.enqueue(s));  // speak while it is still writing
        }
        else if (kind === "replace") {
          // The verifier rewrote the draft. If only unspoken parts changed (e.g. a bad citation was removed), keep talking.
          if (plain(d.text) !== plain(this.answer)) { replaced = true; this.cancelSpeech(); }
          this.answer = d.text;
        }
        else if (kind === "done") { this.answer = d.text; const p = plain(d.text); ai.textContent = p.length > 220 ? p.slice(0, 220) + "…" : p; }
        else if (kind === "error") ai.textContent = d.message;
      }, { voice: true });
    } catch (e) { ai.textContent = e.message; }
    this.deps.onTurn?.();
    if (replaced || !this.spoke) {  // nothing streamed, or the verifier corrected the draft: say the final answer
      this.cancelSpeech();
      const c = new Chunker(); [...c.feed(this.answer), ...c.flush()].forEach(s => this.enqueue(s));
    } else chunker.flush().forEach(s => this.enqueue(s));
    this.streamDone = true;
    this.maybeIdle();
  },

  // ---- speech: sentences are synthesised in parallel and played strictly in order
  enqueue(sentence) {
    if (!this.el || this.voiceOff || !sentence) return;
    this.spoke = true;
    const gen = this.gen;
    const blob = this.browserVoice ? Promise.resolve(null)
      : this.deps.api("/api/speak", { method: "POST", body: { text: sentence } }).then(r => r.blob()).catch(() => null);
    this.queue.push({ blob, text: sentence });
    if (!this.playing) this.playLoop(gen);
  },
  cancelSpeech() {
    this.gen = (this.gen || 0) + 1; this.queue = []; this.playing = false;
    if (this.audio) { this.audio.onended = null; this.audio.pause(); this.audio = null; }
    if (window.speechSynthesis) speechSynthesis.cancel();
    if (this.src && this.mode === "speaking") { this.src.disconnect(); this.src = null; }
  },
  async playLoop(gen) {
    this.playing = true;
    while (this.queue.length && gen === this.gen && this.el) {
      const item = this.queue.shift();
      const blob = await item.blob;
      if (gen !== this.gen) return;
      if (blob) { await this.play(blob, gen); continue; }
      // no local voice (Piper) on the server: use the browser's own voice instead of going quiet
      if (!window.speechSynthesis) { this.voiceOff = true; this.queue = []; break; }
      this.browserVoice = true;
      await this.browserSay(item.text, gen);
    }
    if (gen === this.gen) { this.playing = false; this.maybeIdle(); }
  },
  async play(blob, gen) {
    const url = URL.createObjectURL(blob);
    const audio = this.audio = new Audio(url);
    if (this.ctx.state === "suspended") await this.ctx.resume();
    const node = this.ctx.createMediaElementSource(audio);
    node.connect(this.ctx.destination); this.attach(node);
    this.setMode("speaking", "Speaking", "hold <kbd>Space</kbd> to interrupt");
    await new Promise(res => { audio.onended = audio.onerror = res; audio.play().catch(res); });
    node.disconnect(); if (this.src === node) this.src = null;
    URL.revokeObjectURL(url);
  },
  browserSay(text, gen) {
    const pick = () => {
      const vs = speechSynthesis.getVoices();
      return vs.find(v => /en-GB/i.test(v.lang) && /Ryan|Thomas|George|Daniel|Male/i.test(v.name) && /Natural|Online/i.test(v.name))
        || vs.find(v => /en-GB/i.test(v.lang) && /Ryan|Thomas|George|Daniel|Male/i.test(v.name))
        || vs.find(v => /en-(GB|IN)/i.test(v.lang)) || vs.find(v => /^en/i.test(v.lang)) || null;
    };
    return new Promise(res => {
      const u = new SpeechSynthesisUtterance(text);
      u.voice = pick(); u.rate = 1.0; u.pitch = 0.95;
      u.onend = u.onerror = res;
      if (gen !== this.gen) return res();
      this.setMode("speaking", "Speaking", "hold <kbd>Space</kbd> to interrupt");
      speechSynthesis.speak(u);
    });
  },
  maybeIdle() {
    if (!this.el || this.playing || this.queue.length || !this.streamDone || this.mode === "listening") return;
    this.setMode("idle", "Ready", this.voiceOff ? "Voice output isn't set up, so I answered in text · hold <kbd>Space</kbd> to talk" : "Hold <kbd>Space</kbd> to talk");
  },

  draw() {
    const g = this.g, w = innerWidth, hgt = innerHeight, cx = w / 2, cy = hgt * .4, t = (performance.now() - this.t0) / 1000;
    const cs = getComputedStyle(document.documentElement);
    const col = { idle: cs.getPropertyValue("--accent"), listening: cs.getPropertyValue("--bad"), thinking: cs.getPropertyValue("--accent2"), speaking: cs.getPropertyValue("--accent") }[this.mode].trim();
    const live = !!this.src;
    if (live) this.analyser.getByteFrequencyData(this.freq);
    const bins = this.freq.length;
    let sum = 0; for (let i = 0; i < bins; i++) sum += live ? this.freq[i] : 0;
    const target = live ? Math.min(1, sum / bins / 90) : this.mode === "thinking" ? .25 + .12 * Math.sin(t * 5) : .06 + .03 * Math.sin(t * 1.3);
    this.level += (target - this.level) * .18;
    this.spin += this.mode === "thinking" ? .035 : .006;
    const R = Math.min(w, hgt) * .14, L = this.level;
    g.clearRect(0, 0, w, hgt);
    // grid backdrop
    g.save(); g.globalAlpha = .07; g.strokeStyle = col; g.lineWidth = 1;
    for (let x = (cx % 48); x < w; x += 48) { g.beginPath(); g.moveTo(x, 0); g.lineTo(x, hgt); g.stroke(); }
    for (let y = (cy % 48); y < hgt; y += 48) { g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke(); }
    g.restore();
    // glow
    const glow = g.createRadialGradient(cx, cy, 0, cx, cy, R * (2.6 + L));
    glow.addColorStop(0, col + "55"); glow.addColorStop(.35, col + "18"); glow.addColorStop(1, "transparent");
    g.fillStyle = glow; g.fillRect(0, 0, w, hgt);
    // radial spectrum (mirrored so it is symmetric)
    const N = 128;
    g.save(); g.translate(cx, cy); g.rotate(this.spin * .3);
    for (let i = 0; i < N; i++) {
      const k = i < N / 2 ? i : N - 1 - i;
      const v = live ? this.freq[Math.floor((k / (N / 2)) * bins * .7)] / 255 : (.15 + .1 * Math.sin(t * 2 + k * .4)) * (this.mode === "thinking" ? 1.6 : .7);
      const a = (i / N) * Math.PI * 2, r1 = R * 1.18, r2 = r1 + 4 + v * R * .9;
      g.strokeStyle = col; g.globalAlpha = .35 + v * .65; g.lineWidth = 2.2;
      g.beginPath(); g.moveTo(Math.cos(a) * r1, Math.sin(a) * r1); g.lineTo(Math.cos(a) * r2, Math.sin(a) * r2); g.stroke();
    }
    g.restore();
    // HUD rings
    const ring = (r, from, len, lw, alpha, dash = []) => { g.save(); g.translate(cx, cy); g.globalAlpha = alpha; g.strokeStyle = col; g.lineWidth = lw; g.setLineDash(dash); g.beginPath(); g.arc(0, 0, r, from, from + len); g.stroke(); g.restore(); };
    ring(R * 1.95, this.spin, Math.PI * 1.2, 1.5, .5); ring(R * 1.95, this.spin + Math.PI * 1.35, Math.PI * .4, 1.5, .25);
    ring(R * 2.15, -this.spin * 1.6, Math.PI * 2, 1, .22, [2, 10]);
    ring(R * 1.05, -this.spin * 2, Math.PI * .5, 3, .9); ring(R * 1.05, -this.spin * 2 + Math.PI, Math.PI * .5, 3, .9);
    ring(R * 1.75, this.spin * 2.4, Math.PI * .18, 4, .8);
    // core
    const core = g.createRadialGradient(cx, cy, 0, cx, cy, R * (.92 + L * .25));
    core.addColorStop(0, "#ffffff"); core.addColorStop(.18, col); core.addColorStop(.6, col + "44"); core.addColorStop(1, "transparent");
    g.fillStyle = core; g.beginPath(); g.arc(cx, cy, R * (.92 + L * .25), 0, Math.PI * 2); g.fill();
    // orbiting particles
    for (let i = 0; i < 36; i++) {
      const a = i * 2.399 + t * (.2 + (i % 5) * .05) * (this.mode === "thinking" ? 3 : 1), r = R * (1.3 + (i % 7) * .1 + L * .3);
      g.globalAlpha = .3 + (i % 3) * .2; g.fillStyle = col;
      g.beginPath(); g.arc(cx + Math.cos(a) * r, cy + Math.sin(a) * r, 1.2 + (i % 3) * .6, 0, Math.PI * 2); g.fill();
    }
    g.globalAlpha = 1;
  },
};
