// Memory map renderer: a glowing force-directed graph of everything AETHER knows, centred on you.
// Canvas 2D, no libraries. Additive glow sprites, community gravity, animated camera, flowing links on selection.
export const AREA_COLORS = {
  identity: "#ffd23f", people: "#ff2fb3", health: "#ff3352", study: "#a46bff", career: "#ff8a1f",
  projects: "#2fe0ff", goals: "#ffb000", prefs: "#ff7ad9", assistant: "#7dff9a", life: "#ff5f8f", docs: "#4d8dff",
};
const COMMUNITY = ["#ff2fb3", "#ffd23f", "#ff3352", "#a46bff", "#2fe0ff", "#ff8a1f", "#7dff9a", "#ff7ad9", "#4d8dff", "#ffb000", "#c6ff3d", "#ff5f8f"];
const LINK_LEN = { area: 120, fact: 70, derived: 80, mention: 55, similar: 75, about: 90 };

export class Atlas {
  constructor(canvas, { onSelect, onHover } = {}) {
    this.c = canvas; this.ctx = canvas.getContext("2d");
    this.onSelect = onSelect || (() => {}); this.onHover = onHover || (() => {});
    this.nodes = []; this.links = []; this.byId = new Map(); this.adj = new Map();
    this.view = { x: 0, y: 0, k: .8 }; this.target = null;
    this.sel = null; this.hover = null; this.dim = null; this.alpha = 1; this.running = true;
    this.mode = "areas"; this.hiddenTypes = new Set(); this.showInferred = true; this.until = null; this.t = 0;
    this.sprites = new Map(); this.stars = Array.from({ length: 160 }, () => [Math.random(), Math.random(), Math.random()]);
    this._bind(); this._resize(); this._loop = this._loop.bind(this); requestAnimationFrame(this._loop);
    this.ro = new ResizeObserver(() => this._resize()); this.ro.observe(canvas.parentElement);
  }
  destroy() { this.running = false; this.ro.disconnect(); }

  // -------------------------------------------------------------- data
  setData(data) {
    this.data = data;
    const comArea = new Map(data.communities.map(c => [c.id, c.area]));
    const old = new Map(this.nodes.map(n => [n.id, n]));
    const areas = data.stats.areas || [];
    const ang = new Map(areas.map((a, i) => [a.key, i / Math.max(1, areas.length) * Math.PI * 2]));
    this.nodes = data.nodes.map((n, i) => {
      const o = old.get(n.id);
      const area = n.type === "category" ? n.kind : n.category || comArea.get(n.community) || "life";
      const a = (ang.get(area) ?? i * 2.4) + (Math.random() - .5) * .8, rr = n.id === "e:user" ? 0 : n.type === "category" ? 170 : 240 + Math.random() * 160;
      const r = n.id === "e:user" ? 17 : n.type === "category" ? 11 : n.type === "goal" ? 6.5 : n.type === "document" ? 6
        : n.type === "entity" ? 3.5 + 11 * Math.sqrt(n.pr) : 2.6 + 4 * Math.sqrt(n.pr);
      return { ...n, area, r, x: o?.x ?? Math.cos(a) * rr, y: o?.y ?? Math.sin(a) * rr, vx: 0, vy: 0, fixed: n.id === "e:user" };
    });
    this.byId = new Map(this.nodes.map(n => [n.id, n]));
    this.links = data.links.map(l => ({ ...l, a: this.byId.get(l.s), b: this.byId.get(l.t) })).filter(l => l.a && l.b);
    this.adj = new Map(this.nodes.map(n => [n.id, new Set()]));
    for (const l of this.links) { this.adj.get(l.s).add(l.t); this.adj.get(l.t).add(l.s); }
    this.god = new Set((data.insights.god_nodes || []).map(g => g.id));
    this.alpha = 1;
  }
  color(n) {
    if (n.id === "e:user") return "#fff1c2";
    if (this.mode === "communities" && n.type !== "category") return COMMUNITY[(n.community >= 0 ? n.community : 0) % COMMUNITY.length];
    return AREA_COLORS[n.area] || "#ff2fb3";
  }
  visible(n) {
    if (this.hiddenTypes.has(n.type)) return false;
    if (this.until && n.created && n.created > this.until) return false;
    return true;
  }
  linkVisible(l) {
    if (!this.showInferred && (l.kind === "similar")) return false;
    return this.visible(l.a) && this.visible(l.b);
  }

  // -------------------------------------------------------------- camera
  fit(pad = 80) {
    const vis = this.nodes.filter(n => this.visible(n)); if (!vis.length) return;
    const xs = vis.map(n => n.x), ys = vis.map(n => n.y);
    const w = Math.max(...xs) - Math.min(...xs) + pad * 2, hh = Math.max(...ys) - Math.min(...ys) + pad * 2;
    const k = Math.max(.15, Math.min(1.4, Math.min(this.w / w, this.h / hh)));
    this.target = { k, x: -(Math.max(...xs) + Math.min(...xs)) / 2 * k, y: -(Math.max(...ys) + Math.min(...ys)) / 2 * k };
  }
  focus(id, k = 1.5) {
    const n = this.byId.get(id); if (!n) return;
    this.sel = n; this.target = { k, x: -n.x * k, y: -n.y * k }; this.onSelect(n);
  }
  isolate(ids) { this.dim = ids ? new Set(ids) : null; }

  // -------------------------------------------------------------- physics
  _tick() {
    const N = this.nodes, a = this.alpha;
    if (a < .004) return;
    const cent = new Map();
    for (const n of N) {
      const c = cent.get(n.community) || { x: 0, y: 0, k: 0 }; c.x += n.x; c.y += n.y; c.k++; cent.set(n.community, c);
    }
    for (let i = 0; i < N.length; i++) {
      const p = N[i];
      for (let j = i + 1; j < N.length; j++) {
        const q = N[j]; let dx = p.x - q.x, dy = p.y - q.y; let d2 = dx * dx + dy * dy;
        if (d2 > 160000) continue; if (d2 < 1) { dx = Math.random() - .5; dy = Math.random() - .5; d2 = 1; }
        const f = (p.type === "category" || q.type === "category" ? 2600 : 900) * a / d2;
        p.vx += dx * f; p.vy += dy * f; q.vx -= dx * f; q.vy -= dy * f;
      }
    }
    for (const l of this.links) {
      const p = l.a, q = l.b, dx = q.x - p.x, dy = q.y - p.y, d = Math.hypot(dx, dy) || 1;
      const len = LINK_LEN[l.kind] || 70, s = (l.kind === "area" ? .02 : .05) * Math.min(2, l.w) * a * (d - len) / d;
      p.vx += dx * s; p.vy += dy * s; q.vx -= dx * s; q.vy -= dy * s;
    }
    for (const n of N) {
      const c = cent.get(n.community);
      if (c && c.k > 1) { n.vx += (c.x / c.k - n.x) * .006 * a; n.vy += (c.y / c.k - n.y) * .006 * a; }
      n.vx -= n.x * .0016 * a; n.vy -= n.y * .0016 * a;
      if (n.fixed || n === this.drag) { n.vx = n.vy = 0; if (n.id === "e:user" && n !== this.drag) { n.x *= .9; n.y *= .9; } continue; }
      n.vx *= .82; n.vy *= .82; n.x += n.vx; n.y += n.vy;
    }
    this.alpha *= .992;
  }

  // -------------------------------------------------------------- drawing
  sprite(col) {
    let s = this.sprites.get(col); if (s) return s;
    s = document.createElement("canvas"); s.width = s.height = 64; const g = s.getContext("2d");
    const gr = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    gr.addColorStop(0, "#ffffff"); gr.addColorStop(.12, col); gr.addColorStop(.35, col + "88"); gr.addColorStop(1, col + "00");
    g.fillStyle = gr; g.fillRect(0, 0, 64, 64); this.sprites.set(col, s); return s;
  }
  _draw() {
    const g = this.ctx, v = this.view, W = this.w, H = this.h; this.t += 1;
    g.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    // starfield + HUD rings around you
    g.globalCompositeOperation = "source-over";
    for (const [x, y, z] of this.stars) { g.fillStyle = `rgba(255,${180 + z * 60},${220},${.15 + z * .35})`; g.fillRect(((x * W + v.x * z * .1) % W + W) % W, ((y * H + v.y * z * .1) % H + H) % H, z * 1.6, z * 1.6); }
    g.save(); g.translate(W / 2 + v.x, H / 2 + v.y); g.scale(v.k, v.k);
    const you = this.byId.get("e:user");
    if (you) {
      g.strokeStyle = "rgba(255,47,179,.10)"; g.lineWidth = 1 / v.k;
      for (const r of [170, 340, 520]) { g.beginPath(); g.arc(you.x, you.y, r, 0, Math.PI * 2); g.stroke(); }
      g.save(); g.translate(you.x, you.y); g.rotate(this.t / 400); g.strokeStyle = "rgba(255,210,63,.22)"; g.setLineDash([4 / v.k, 18 / v.k]);
      g.beginPath(); g.arc(0, 0, 250, 0, Math.PI * 2); g.stroke(); g.restore(); g.setLineDash([]);
    }
    const selN = this.sel ? this.adj.get(this.sel.id) : null;
    const lit = n => !this.dim || this.dim.has(n.id);
    // links
    g.globalCompositeOperation = "lighter";
    for (const l of this.links) {
      if (!this.linkVisible(l)) continue;
      const on = this.sel && (l.a === this.sel || l.b === this.sel);
      const faded = (this.dim && !(lit(l.a) && lit(l.b))) || (this.sel && !on);
      const col = this.color(l.a.type === "category" || l.a.id === "e:user" ? l.b : l.a);
      g.strokeStyle = col; g.globalAlpha = on ? .85 : faded ? .025 : l.kind === "area" ? .10 : l.kind === "similar" ? .10 : .22;
      g.lineWidth = (on ? 1.8 : l.kind === "fact" ? 1.1 : .7) / Math.sqrt(v.k);
      if (l.kind === "derived" || l.kind === "similar") g.setLineDash([5 / v.k, 5 / v.k]);
      g.beginPath(); g.moveTo(l.a.x, l.a.y); g.lineTo(l.b.x, l.b.y); g.stroke(); g.setLineDash([]);
      if (on) {  // energy flowing along the selected node's links
        for (let k = 0; k < 2; k++) {
          const f = ((this.t / 90 + k / 2) % 1), x = l.a.x + (l.b.x - l.a.x) * f, y = l.a.y + (l.b.y - l.a.y) * f;
          g.globalAlpha = .9; g.drawImage(this.sprite(col), x - 6 / v.k, y - 6 / v.k, 12 / v.k, 12 / v.k);
        }
      }
    }
    // nodes: glow sprite (additive) then core
    for (const n of this.nodes) {
      if (!this.visible(n)) continue;
      const faded = (this.dim && !lit(n)) || (this.sel && n !== this.sel && !(selN && selN.has(n.id)));
      const col = this.color(n), pulse = n.id === "e:user" ? 1 + .08 * Math.sin(this.t / 25) : this.god.has(n.id) ? 1 + .12 * Math.sin(this.t / 30 + n.x) : 1;
      const R = n.r * pulse * (n === this.hover ? 1.25 : 1);
      g.globalAlpha = faded ? .08 : n.type === "memory" ? .55 : .9;
      const gs = R * (n.type === "category" || n.id === "e:user" ? 7 : 4.5);
      g.drawImage(this.sprite(col), n.x - gs / 2, n.y - gs / 2, gs, gs);
    }
    g.globalCompositeOperation = "source-over";
    for (const n of this.nodes) {
      if (!this.visible(n)) continue;
      const faded = (this.dim && !lit(n)) || (this.sel && n !== this.sel && !(selN && selN.has(n.id)));
      g.globalAlpha = faded ? .15 : 1;
      g.fillStyle = n.id === "e:user" ? "#fff8e1" : n.type === "memory" ? this.color(n) : "#fff";
      g.beginPath(); g.arc(n.x, n.y, Math.max(1.2, n.r * (n.type === "memory" ? .55 : .42)), 0, Math.PI * 2); g.fill();
      if (n.type === "category" || n.id === "e:user" || n.type === "goal") {
        g.strokeStyle = this.color(n); g.lineWidth = 1.4 / v.k; g.beginPath();
        if (n.type === "goal") { const s = n.r * .9; g.moveTo(n.x, n.y - s); g.lineTo(n.x + s, n.y); g.lineTo(n.x, n.y + s); g.lineTo(n.x - s, n.y); g.closePath(); }
        else g.arc(n.x, n.y, n.r * 1.25, 0, Math.PI * 2);
        g.stroke();
      }
      if (n === this.sel) { g.strokeStyle = "#fff"; g.lineWidth = 2 / v.k; g.beginPath(); g.arc(n.x, n.y, n.r * 1.9 + 2 * Math.sin(this.t / 12), 0, Math.PI * 2); g.stroke(); }
    }
    // labels
    g.textAlign = "center"; g.textBaseline = "top";
    for (const n of this.nodes) {
      if (!this.visible(n)) continue;
      const near = this.sel && (n === this.sel || (selN && selN.has(n.id)));
      const show = n.id === "e:user" || n.type === "category" || n === this.hover || near || this.god.has(n.id)
        || (n.type === "entity" && n.r * v.k > 7) || (n.type === "goal" && v.k > 1.1) || (v.k > 2.2 && n.type !== "memory");
      if (!show || (this.dim && !lit(n) && !near)) continue;
      const big = n.id === "e:user" || n.type === "category";
      const fs = (big ? 13 : 11) / Math.min(1.6, v.k) * (n.id === "e:user" ? 1.25 : 1);
      g.font = `${big ? 700 : 500} ${fs}px "Segoe UI Variable Display","Segoe UI",system-ui,sans-serif`;
      const txt = n.label.length > 42 ? n.label.slice(0, 40) + "…" : n.label;
      g.globalAlpha = 1; g.lineWidth = 3 / v.k; g.strokeStyle = "rgba(6,1,10,.85)"; g.strokeText(big ? txt.toUpperCase() : txt, n.x, n.y + n.r * 1.5 + 2);
      g.fillStyle = big ? this.color(n) : "#fbe9ff"; g.fillText(big ? txt.toUpperCase() : txt, n.x, n.y + n.r * 1.5 + 2);
    }
    g.restore(); g.globalAlpha = 1;
  }
  _loop() {
    if (!this.running) return;
    if (this.target) {
      const v = this.view, t = this.target, e = .14;
      v.x += (t.x - v.x) * e; v.y += (t.y - v.y) * e; v.k += (t.k - v.k) * e;
      if (Math.abs(t.k - v.k) < .002 && Math.abs(t.x - v.x) < .5 && Math.abs(t.y - v.y) < .5) this.target = null;
    }
    this._tick(); this._tick(); this._draw();
    if (!this.fitted && this.alpha < .35) { this.fitted = true; this.fit(); }
    requestAnimationFrame(this._loop);
  }

  // -------------------------------------------------------------- input
  _resize() {
    const r = this.c.parentElement.getBoundingClientRect(); this.dpr = devicePixelRatio || 1;
    this.w = r.width; this.h = r.height; this.c.width = r.width * this.dpr; this.c.height = r.height * this.dpr;
  }
  _world(px, py) { return { x: (px - this.w / 2 - this.view.x) / this.view.k, y: (py - this.h / 2 - this.view.y) / this.view.k }; }
  _hit(px, py) {
    const p = this._world(px, py); let best = null, bd = Infinity;
    for (const n of this.nodes) {
      if (!this.visible(n)) continue;
      const d = (n.x - p.x) ** 2 + (n.y - p.y) ** 2, r = Math.max(n.r + 3, 7 / this.view.k);
      if (d < r * r && d < bd) { best = n; bd = d; }
    }
    return best;
  }
  _bind() {
    let pan = null, moved = false;
    this.c.addEventListener("pointerdown", e => {
      const n = this._hit(e.offsetX, e.offsetY); moved = false; this.c.setPointerCapture(e.pointerId); this.target = null;
      if (n) this.drag = n; else pan = { x: e.offsetX - this.view.x, y: e.offsetY - this.view.y };
    });
    this.c.addEventListener("pointermove", e => {
      if (this.drag) { const p = this._world(e.offsetX, e.offsetY); this.drag.x = p.x; this.drag.y = p.y; this.alpha = Math.max(this.alpha, .25); moved = true; return; }
      if (pan) { this.view.x = e.offsetX - pan.x; this.view.y = e.offsetY - pan.y; moved = true; return; }
      const n = this._hit(e.offsetX, e.offsetY);
      if (n !== this.hover) { this.hover = n; this.c.style.cursor = n ? "pointer" : "grab"; this.onHover(n, e); }
    });
    this.c.addEventListener("pointerup", e => {
      if (!moved) { const n = this._hit(e.offsetX, e.offsetY); this.sel = n; this.onSelect(n); }
      if (this.drag && moved && this.drag.id !== "e:user") this.drag.fixed = true;
      this.drag = null; pan = null;
    });
    this.c.addEventListener("dblclick", e => { const n = this._hit(e.offsetX, e.offsetY); if (n) { if (n.fixed && n.id !== "e:user") n.fixed = false; this.focus(n.id, 2); } else this.fit(); });
    this.c.addEventListener("wheel", e => {
      e.preventDefault(); this.target = null;
      const k = Math.max(.08, Math.min(5, this.view.k * Math.exp(-e.deltaY * .0015)));
      const p = this._world(e.offsetX, e.offsetY);
      this.view.x = e.offsetX - this.w / 2 - p.x * k; this.view.y = e.offsetY - this.h / 2 - p.y * k; this.view.k = k;
    }, { passive: false });
  }
}
