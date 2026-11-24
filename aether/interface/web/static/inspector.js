// Glass-box inspector: opens the recorded trace of a turn and shows exactly what happened,
// when, and why: a waterfall of stages and model calls, what was retrieved and how it scored,
// which facts the rules derived (with their proofs), and what the verifier checked.
import { esc } from "./md.js";

const STAGE_COL = { perceive: "var(--faint)", interpret: "var(--accent2)", retrieve: "var(--accent)", reason: "var(--warn)",
                    act: "var(--good)", generate: "var(--accent)", verify: "var(--good)" };
const fmt = ms => ms >= 1000 ? (ms / 1000).toFixed(2) + " s" : Math.round(ms) + " ms";

export const Inspector = {
  api: null, el: null, cur: null,
  init(api) {
    this.api = api;
    this.el = document.createElement("div");
    this.el.id = "inspector"; this.el.className = "drawer-wrap"; this.el.hidden = true;
    this.el.innerHTML = `<div class="drawer"><div class="drawer-list"><h4>Recent turns</h4><div id="tr-list"></div></div><div class="drawer-main" id="tr-main"></div></div>`;
    this.el.onclick = e => { if (e.target === this.el) this.close(); };
    document.body.appendChild(this.el);
    document.addEventListener("keydown", e => { if (e.key === "Escape" && !this.el.hidden) this.close(); });
  },
  close() { this.el.hidden = true; },
  async open(turnId) {
    this.el.hidden = false;
    const list = await this.api("/api/traces?limit=40").catch(() => []);
    const box = this.el.querySelector("#tr-list");
    box.innerHTML = list.map(t => `<button class="tr-item" data-t="${t.turn_id}"><span>${esc(t.request || "(empty)")}</span><small>${fmt(t.total_ms)} · ${t.llm_calls} model call${t.llm_calls === 1 ? "" : "s"}</small></button>`).join("")
      || `<div class="muted" style="font-size:12.5px">Traces appear here after you chat. They're encrypted like the rest of your history.</div>`;
    box.querySelectorAll("[data-t]").forEach(b => b.onclick = () => this.show(+b.dataset.t));
    const id = turnId ?? list[0]?.turn_id;
    if (id != null) this.show(id); else this.el.querySelector("#tr-main").innerHTML = `<div class="empty">Nothing recorded yet.</div>`;
  },
  async show(id) {
    this.el.querySelectorAll(".tr-item").forEach(b => b.classList.toggle("on", +b.dataset.t === id));
    const main = this.el.querySelector("#tr-main");
    let t;
    try { t = this.cur = await this.api("/api/traces/" + id); } catch (e) { main.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    const stage = n => t.events.find(e => e.kind === "stage" && e.name === n)?.data || {};
    const total = Math.max(t.total_ms, 1);
    const bar = (start, end, col, label, extra = "") => {
      const l = (start / total) * 100, w = Math.max(.6, ((end - start) / total) * 100);
      return `<div class="wf-row"><span class="wf-label">${label}</span><div class="wf-track"><i style="left:${l}%;width:${w}%;background:${col}" title="${fmt(start)} → ${fmt(end)}">${extra}</i>
        <em style="left:calc(${Math.min(l + w, 88)}% + 6px)">${fmt(end - start)}</em></div></div>`;
    };
    const rows = [
      ...t.events.filter(e => e.kind === "stage").map(e => bar(e.start, e.end, STAGE_COL[e.name] || "var(--accent)", esc(e.name))),
      ...t.llm_calls.map(c => bar(c.start, c.end, "color-mix(in srgb, var(--accent2) 70%, transparent)", `<b class="llm">LLM</b> ${esc(c.purpose)}`,
        c.first_token_ms != null ? `<b class="ftok" style="left:${(c.first_token_ms / Math.max(c.end - c.start, 1)) * 100}%" title="first token after ${fmt(c.first_token_ms)}"></b>` : "")),
      ...t.events.filter(e => e.kind === "tool").map(e => bar(e.start, e.end + total * .004, e.data.ok ? "var(--good)" : "var(--bad)", `<b class="tool">TOOL</b> ${esc(e.name)}`)),
    ];
    const ticks = [0, .25, .5, .75, 1].map(f => `<span style="left:${f * 100}%">${fmt(total * f)}</span>`).join("");
    const interp = stage("interpret"), ret = stage("retrieve"), rea = stage("reason"), ver = stage("verify");
    const pc = ver.prompt_chars || {};
    const pcTot = (pc.system || 0) + (pc.context || 0) + (pc.history || 0) || 1;
    const seg = (k, col) => pc[k] ? `<i style="width:${(pc[k] / pcTot) * 100}%;background:${col}" title="${k}: ~${Math.round(pc[k] / 4)} tokens"></i>` : "";
    const maxScore = Math.max(...(ret.items || []).map(i => i.score), 1e-9);
    main.innerHTML = `
      <div class="tr-head"><div class="grow"><div class="tr-q">“${esc(t.request)}”</div>
        <div class="tr-stats"><span><b>${fmt(t.total_ms)}</b> end to end</span><span><b>${t.llm_calls.length}</b> model calls</span>
          <span><b>~${t.tokens_est}</b> tokens out</span><span><b>${(t.state.route || []).join(" + ") || "no lookup"}</b></span><span><b>${Math.round((t.state.confidence || 0) * 100)}%</b> confidence</span></div></div>
        <button class="ghost" id="tr-export">Export JSON</button><button class="icon-btn" id="tr-close" title="Close (Esc)">✕</button></div>
      <h4>Timeline</h4><div class="wf"><div class="wf-axis">${ticks}</div>${rows.join("")}</div>
      <div class="tr-grid">
        <section><h4>Understood as</h4>
          <div class="kv"><span>Intent</span><span class="chip violet">${esc(interp.intent || "–")}</span></div>
          <div class="kv stack"><span>Goal</span><span>${esc(interp.goal || "–")}</span></div>
          ${(interp.issues || []).length ? `<div class="tr-warn">Validator fixed: ${interp.issues.map(esc).join("; ")}</div>` : `<div class="tr-ok">Passed symbolic validation</div>`}
          <h4>Prompt budget</h4><div class="budget">${seg("system", "var(--accent2)")}${seg("context", "var(--accent)")}${seg("history", "var(--faint)")}</div>
          <div class="budget-key"><span><i style="background:var(--accent2)"></i>system ~${Math.round((pc.system || 0) / 4)}</span><span><i style="background:var(--accent)"></i>context ~${Math.round((pc.context || 0) / 4)}</span><span><i style="background:var(--faint)"></i>history ~${Math.round((pc.history || 0) / 4)}</span></div>
        </section>
        <section><h4>Retrieved ${ret.items?.length ? `(${ret.items.length})` : ""}</h4>
          ${(ret.reasons || []).map(r => `<div class="tr-reason">${esc(r)}</div>`).join("")}
          ${(ret.items || []).map(i => `<div class="ret"><div class="ret-top"><span class="chip ${i.layer === "document" ? "accent" : "violet"}">${esc(i.layer)}</span>
            <span class="score"><i style="width:${(i.score / maxScore) * 100}%"></i></span><span class="mono faint">${i.score}</span></div>
            <div class="ret-text">${esc(i.text)}</div>${i.path ? `<div class="mono faint" style="font-size:11px">${esc(i.path)}</div>` : ""}</div>`).join("")
            || `<div class="muted" style="font-size:12.5px">Nothing retrieved: the router decided the answer didn't need a lookup.</div>`}
          ${ret.web_query ? `<div class="tr-reason">Web query: <span class="mono">${esc(ret.web_query)}</span></div>` : ""}
        </section>
        <section><h4>Reasoned ${rea.derived?.length ? `(${rea.derived.length} derived)` : ""}</h4>
          ${(rea.derived || []).map(d => `<div class="proof"><div class="proof-head">∴ ${esc(d.fact)}</div>${d.because.map(b => `<div class="proof-leg">${esc(b)}</div>`).join("")}<div class="proof-rule mono">${esc(d.rule)}</div></div>`).join("")
            || `<div class="muted" style="font-size:12.5px">No rule fired for this turn.</div>`}
          ${(rea.contradictions || []).map(v => `<div class="tr-warn">Contradiction: ${esc(v)}</div>`).join("")}
        </section>
        <section><h4>Verified</h4>
          ${(ver.supported || []).map(c => `<div class="claim ok">✓ ${esc(c)}</div>`).join("")}
          ${(ver.conflicts || []).map(c => `<div class="claim bad">✕ ${esc(c)}</div>`).join("")}
          ${(ver.removed_citations || []).length ? `<div class="claim bad">Removed made-up citations: ${ver.removed_citations.map(esc).join(", ")}</div>` : ""}
          ${ver.revised ? `<div class="tr-warn">The draft contradicted known facts, so it was rewritten before you saw it.</div>` : ""}
          ${!(ver.supported || []).length && !(ver.conflicts || []).length ? `<div class="muted" style="font-size:12.5px">No checkable factual claims in this answer.</div>` : ""}
          ${(t.notices || []).map(n => `<div class="tr-reason">${esc(n)}</div>`).join("")}
        </section>
      </div>`;
    main.querySelector("#tr-close").onclick = () => this.close();
    main.querySelector("#tr-export").onclick = () => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([JSON.stringify(t, null, 2)], { type: "application/json" }));
      a.download = `aether-trace-${id}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    };
  },
};
