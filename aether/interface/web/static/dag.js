// Mission control: the live task graph of an autonomous goal, laid out in dependency layers.
// HTML cards for the tasks, an SVG underneath for the edges. Edges into a running task animate.
import { esc } from "./md.js";

const W = 230, H = 78, GX = 70, GY = 22;
const AGENT_ICON = { research: "⌕", reasoning: "∴", coding: "</>", operator: "⚙", memory: "◎", verification: "✓" };

export function renderDag(host, tasks) {
  const live = tasks.filter(t => t.status !== "replaced");
  const byId = new Map(live.map(t => [t.id, t]));
  const depth = new Map();
  const d = (t, seen = new Set()) => {
    if (depth.has(t.id)) return depth.get(t.id);
    if (seen.has(t.id)) return 0;
    seen.add(t.id);
    const v = Math.max(-1, ...t.depends_on.filter(x => byId.has(x)).map(x => d(byId.get(x), seen))) + 1;
    depth.set(t.id, v); return v;
  };
  live.forEach(t => d(t));
  const layers = [];
  for (const t of live) (layers[depth.get(t.id)] ||= []).push(t);
  const pos = new Map();
  const tallest = Math.max(1, ...layers.map(l => l?.length || 0));
  layers.forEach((layer, x) => layer?.forEach((t, y) => {
    const offset = (tallest - layer.length) * (H + GY) / 2;
    pos.set(t.id, { x: 24 + x * (W + GX), y: 20 + offset + y * (H + GY) });
  }));
  const width = 48 + layers.length * (W + GX) - GX, height = 40 + tallest * (H + GY) - GY;
  const edges = [];
  for (const t of live) for (const dep of t.depends_on) {
    const a = pos.get(dep), b = pos.get(t.id); if (!a || !b) continue;
    const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2, mx = (x1 + x2) / 2;
    const src = byId.get(dep);
    const cls = t.status === "running" ? "flow" : src.status === "done" ? "done" : "";
    edges.push(`<path class="${cls}" d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2 - 6},${y2}" marker-end="url(#dag-arrow)"/>`);
  }
  const done = live.filter(t => t.status === "done").length;
  host.innerHTML = `<div class="dag-meter"><i style="width:${live.length ? (done / live.length) * 100 : 0}%"></i></div>
    <div class="dag" style="width:${width}px;height:${height}px">
      <svg width="${width}" height="${height}" class="dag-edges"><defs><marker id="dag-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="currentColor"/></marker></defs>${edges.join("")}</svg>
      ${live.map(t => { const p = pos.get(t.id); return `<div class="dag-node ${t.status}" style="left:${p.x}px;top:${p.y}px;width:${W}px;height:${H}px" title="${esc(t.success_criteria || "")}">
        <div class="dag-top"><span class="dag-agent">${AGENT_ICON[t.agent] || "•"} ${esc(t.agent)}</span><span class="dag-status">${t.status === "running" ? `<span class="spin"></span>` : ""}${esc(t.status)}${t.attempts > 1 ? ` · try ${t.attempts}` : ""}</span></div>
        <div class="dag-desc">${esc(t.description)}</div></div>`; }).join("")}
    </div>`;
}
