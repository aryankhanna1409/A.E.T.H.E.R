// Force-directed knowledge-graph view on a canvas. No libraries.
// Drag nodes, drag the background to pan, scroll to zoom, click to select.
export class GraphView {
  constructor(canvas, { onSelect } = {}) {
    this.c = canvas; this.ctx = canvas.getContext("2d"); this.onSelect = onSelect || (() => {});
    this.nodes = []; this.links = []; this.view = { x: 0, y: 0, k: 1 }; this.sel = null; this.hover = null;
    this.filter = ""; this.alpha = 1; this.running = true;
    this._bind(); this._resize(); this._loop = this._loop.bind(this); requestAnimationFrame(this._loop);
    this.ro = new ResizeObserver(() => this._resize()); this.ro.observe(canvas.parentElement);
  }
  destroy() { this.running = false; this.ro.disconnect(); }
  colors() {
    const cs = getComputedStyle(document.documentElement);
    return { accent: cs.getPropertyValue("--accent").trim(), accent2: cs.getPropertyValue("--accent2").trim(),
             text: cs.getPropertyValue("--text").trim(), muted: cs.getPropertyValue("--muted").trim(),
             line: cs.getPropertyValue("--line2").trim(), bg: cs.getPropertyValue("--bg").trim(), good: cs.getPropertyValue("--good").trim(),
             bad: cs.getPropertyValue("--bad").trim(), warn: cs.getPropertyValue("--warn").trim() };
  }
  setData({ nodes, links }, { keepView = false } = {}) {
    const old = new Map(this.nodes.map(n => [n.id, n]));
    this.nodes = nodes.map((n, i) => {
      const o = old.get(n.id);
      const a = i * 2.399963, r = 30 + 14 * Math.sqrt(i);
      return { ...n, x: o?.x ?? Math.cos(a) * r, y: o?.y ?? Math.sin(a) * r, vx: 0, vy: 0, r: 5 + Math.min(14, Math.sqrt(n.degree) * 3) };
    });
    const idx = new Map(this.nodes.map(n => [n.id, n]));
    if (keepView) for (const l of links) {  // spawn newcomers next to a neighbour that is already on screen
      for (const [a, b] of [[l.source, l.target], [l.target, l.source]]) {
        if (!old.has(a) && old.has(b) && idx.get(a)) { const o = old.get(b), n = idx.get(a), t = Math.random() * 6.28; n.x = o.x + Math.cos(t) * 60; n.y = o.y + Math.sin(t) * 60; old.set(a, n); }
      }
    }
    this.links = links.map(l => ({ ...l, s: idx.get(l.source), t: idx.get(l.target) })).filter(l => l.s && l.t);
    if (keepView) { this.alpha = Math.max(this.alpha, .35); } else { this.alpha = 1; this.fitted = false; }
  }
  fit() {
    if (!this.nodes.length) return;
    const xs = this.nodes.map(n => n.x), ys = this.nodes.map(n => n.y);
    const w = Math.max(...xs) - Math.min(...xs) + 160, hh = Math.max(...ys) - Math.min(...ys) + 120;
    const k = Math.max(.3, Math.min(1.15, Math.min(this.w / w, this.h / hh)));
    this.view = { k, x: -(Math.max(...xs) + Math.min(...xs)) / 2 * k, y: -(Math.max(...ys) + Math.min(...ys)) / 2 * k };
  }
  focus(id) { const n = this.nodes.find(n => n.id === id); if (n) { this.sel = n; this.view.x = -n.x * this.view.k; this.view.y = -n.y * this.view.k; this.onSelect(n); } }
  _resize() {
    const r = this.c.parentElement.getBoundingClientRect(), d = devicePixelRatio || 1;
    this.w = r.width; this.h = r.height; this.c.width = r.width * d; this.c.height = r.height * d; this.ctx.setTransform(d, 0, 0, d, 0, 0);
  }
  _toWorld(px, py) { return { x: (px - this.w / 2 - this.view.x) / this.view.k, y: (py - this.h / 2 - this.view.y) / this.view.k }; }
  _hit(px, py) {
    const p = this._toWorld(px, py);
    for (let i = this.nodes.length - 1; i >= 0; i--) { const n = this.nodes[i]; if ((n.x - p.x) ** 2 + (n.y - p.y) ** 2 < (n.r + 4) ** 2) return n; }
    return null;
  }
  _bind() {
    let drag = null, pan = null, moved = false;
    this.c.addEventListener("pointerdown", e => {
      const n = this._hit(e.offsetX, e.offsetY); moved = false; this.c.setPointerCapture(e.pointerId);
      if (n) { drag = n; n.fixed = true; } else pan = { x: e.offsetX - this.view.x, y: e.offsetY - this.view.y };
    });
    this.c.addEventListener("pointermove", e => {
      if (drag) { const p = this._toWorld(e.offsetX, e.offsetY); drag.x = p.x; drag.y = p.y; this.alpha = Math.max(this.alpha, .3); moved = true; }
      else if (pan) { this.view.x = e.offsetX - pan.x; this.view.y = e.offsetY - pan.y; moved = true; }
      else { this.hover = this._hit(e.offsetX, e.offsetY); this.c.style.cursor = this.hover ? "pointer" : "grab"; }
    });
    this.c.addEventListener("pointerup", e => {
      if (drag) { drag.fixed = false; if (!moved) { this.sel = drag; this.onSelect(drag); } }
      else if (!moved) { this.sel = null; this.onSelect(null); }
      drag = pan = null;
    });
    this.c.addEventListener("wheel", e => {
      e.preventDefault();
      const k = Math.min(4, Math.max(.2, this.view.k * Math.exp(-e.deltaY * .0015)));
      const mx = e.offsetX - this.w / 2, my = e.offsetY - this.h / 2;
      this.view.x = mx - (mx - this.view.x) * k / this.view.k; this.view.y = my - (my - this.view.y) * k / this.view.k; this.view.k = k;
    }, { passive: false });
  }
  _step() {
    if (this.alpha < .005) return;
    const N = this.nodes, a = this.alpha;
    for (let i = 0; i < N.length; i++) for (let j = i + 1; j < N.length; j++) {
      const p = N[i], q = N[j]; let dx = q.x - p.x, dy = q.y - p.y, d2 = dx * dx + dy * dy || .01;
      if (d2 > 250000) continue;
      const f = 2600 / d2 * a, d = Math.sqrt(d2); dx /= d; dy /= d;
      p.vx -= dx * f; p.vy -= dy * f; q.vx += dx * f; q.vy += dy * f;
    }
    for (const l of this.links) {
      const dx = l.t.x - l.s.x, dy = l.t.y - l.s.y, d = Math.sqrt(dx * dx + dy * dy) || 1, f = (d - 130) * .02 * a;
      l.s.vx += dx / d * f; l.s.vy += dy / d * f; l.t.vx -= dx / d * f; l.t.vy -= dy / d * f;
    }
    for (const n of N) {
      n.vx -= n.x * .002 * a; n.vy -= n.y * .002 * a;
      if (!n.fixed) { n.x += n.vx; n.y += n.vy; }
      n.vx *= .6; n.vy *= .6;
    }
    this.alpha *= .985;
    if (!this.fitted && this.alpha < .08) { this.fitted = true; this.fit(); }
  }
  _loop() {
    if (!this.running) return;
    this._step(); this._draw(); requestAnimationFrame(this._loop);
  }
  _draw() {
    const g = this.ctx, C = this.colors(), f = this.filter.toLowerCase();
    g.clearRect(0, 0, this.w, this.h);
    g.save(); g.translate(this.w / 2 + this.view.x, this.h / 2 + this.view.y); g.scale(this.view.k, this.view.k);
    const near = new Set();
    const focus = this.sel || this.hover;
    if (focus) for (const l of this.links) if (l.s === focus || l.t === focus) { near.add(l.s); near.add(l.t); }
    for (const l of this.links) {
      const lit = focus && (l.s === focus || l.t === focus);
      g.globalAlpha = focus ? (lit ? .95 : .12) : .5;
      g.strokeStyle = l.kind === "derived" ? C.accent2 : C.line; if (lit) g.strokeStyle = l.kind === "derived" ? C.accent2 : C.accent;
      g.lineWidth = lit ? 1.6 : 1; g.setLineDash(l.kind === "derived" ? [4, 4] : []);
      g.beginPath(); g.moveTo(l.s.x, l.s.y); g.lineTo(l.t.x, l.t.y); g.stroke();
      if (lit || (this.view.k > 1.4 && !focus)) {
        g.setLineDash([]); g.globalAlpha = lit ? 1 : .6; g.fillStyle = C.muted; g.font = "10px Segoe UI, system-ui";
        g.textAlign = "center"; g.fillText(l.label.replace(/_/g, " "), (l.s.x + l.t.x) / 2, (l.s.y + l.t.y) / 2 - 4);
      }
    }
    g.setLineDash([]);
    // Marked links: what-if gains/losses, assumptions, and beliefs that just changed during time travel.
    const pulse = .55 + .45 * Math.sin(performance.now() / 260);
    for (const l of this.links) {
      if (!l.mark) continue;
      const col = { gain: C.good, lose: C.bad, assume: C.warn, new: C.accent }[l.mark];
      g.save(); g.globalAlpha = l.mark === "lose" ? .9 : .6 + .4 * pulse; g.strokeStyle = col; g.lineWidth = 2.6;
      g.shadowColor = col; g.shadowBlur = 14 * pulse; g.setLineDash(l.mark === "lose" ? [3, 5] : l.mark === "gain" ? [7, 4] : []);
      g.beginPath(); g.moveTo(l.s.x, l.s.y); g.lineTo(l.t.x, l.t.y); g.stroke(); g.restore();
      g.globalAlpha = 1; g.fillStyle = col; g.font = "600 10.5px Segoe UI, system-ui"; g.textAlign = "center";
      g.fillText((l.mark === "lose" ? "✕ " : l.mark === "gain" ? "+ " : "") + l.label.replace(/_/g, " "), (l.s.x + l.t.x) / 2, (l.s.y + l.t.y) / 2 - 5);
    }
    for (const n of this.nodes) {
      const match = f && n.id.toLowerCase().includes(f);
      const dim = (focus && !near.has(n) && n !== focus) || (f && !match);
      g.globalAlpha = dim ? .2 : 1;
      g.beginPath(); g.arc(n.x, n.y, n.r, 0, Math.PI * 2);
      g.fillStyle = n.user ? C.accent : n === this.sel ? C.accent : C.bg; g.fill();
      g.lineWidth = n === focus || match ? 2.5 : 1.5; g.strokeStyle = n.user ? C.accent : match ? C.good : C.accent; g.stroke();
      if (n.mark) { g.save(); g.globalAlpha = .5 + .5 * pulse; g.strokeStyle = { gain: C.good, lose: C.bad, assume: C.warn, new: C.accent }[n.mark]; g.lineWidth = 2;
        g.beginPath(); g.arc(n.x, n.y, n.r + 5 + 3 * pulse, 0, Math.PI * 2); g.stroke(); g.restore(); }
      if (n === this.sel || n.user) { g.globalAlpha = dim ? .1 : .25; g.beginPath(); g.arc(n.x, n.y, n.r + 6, 0, Math.PI * 2); g.strokeStyle = C.accent; g.stroke(); }
      g.globalAlpha = dim ? .25 : 1; g.fillStyle = C.text; g.font = `${n === focus ? 600 : 500} 12px Segoe UI, system-ui`;
      g.textAlign = "center"; g.fillText(n.id, n.x, n.y + n.r + 14);
    }
    g.restore();
  }
}
