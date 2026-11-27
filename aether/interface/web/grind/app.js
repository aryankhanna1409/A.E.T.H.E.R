/* Grind 75 - renderer (all UI). Data lives in `state` and is saved to disk by the main process. */
'use strict';

// ================================================================ utilities
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const round = (n, d = 0) => { const m = 10 ** d; return Math.round((+n || 0) * m) / m; };
const clamp = (n, a, b) => Math.max(a, Math.min(b, n));
const uid = () => Math.random().toString(36).slice(2, 10);
const DAY_MS = 86400000;

function ymd(d = new Date()) {
  const z = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}`;
}
function parseYmd(s) { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); }
function addDays(s, n) { const d = parseYmd(s); d.setDate(d.getDate() + n); return ymd(d); }
function diffDays(a, b) { return Math.round((parseYmd(a) - parseYmd(b)) / DAY_MS); }
function prettyDate(s, opts = { weekday: 'short', day: 'numeric', month: 'short' }) { return parseYmd(s).toLocaleDateString('en-IN', opts); }
function hashSeed(str) { let h = 2166136261; for (const c of str) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619); } return Math.abs(h); }
function pick(arr, seed) { return arr[hashSeed(seed) % arr.length]; }
const inr = n => '₹' + Math.round(+n || 0).toLocaleString('en-IN');

function toast(msg, ms = 2600) {
  const t = $('#toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove('show'), ms);
}

// ================================================================ state
const DEFAULT_STATE = () => ({
  version: 1,
  profile: { name: '', heightCm: 170, startDate: ymd(), startWeight: 60, targetWeight: 65 },
  targets: { kcal: 3000, protein: 140, pages: 20, waterL: 3, sleepH: 7, workoutsPerWeek: 5, weeklySpend: 2000 },
  habits: [
    { id: 'workout', label: 'Workout done', emoji: '💪', auto: false },
    { id: 'protein', label: 'Hit protein', emoji: '🥛', auto: true },
    { id: 'kcal', label: 'Hit calories', emoji: '🍛', auto: true },
    { id: 'read', label: 'Read pages', emoji: '📖', auto: true },
    { id: 'sleep', label: 'Slept 7h+', emoji: '😴', auto: true },
    { id: 'water', label: 'Water 3L', emoji: '💧', auto: true },
    { id: 'homefood', label: 'No eating out', emoji: '🏠', auto: false },
    { id: 'spend', label: 'Logged spending', emoji: '💸', auto: false },
    { id: 'journal', label: 'Journaled', emoji: '✍️', auto: true },
  ],
  days: {},
  books: [],
  goals: [],
  chat: [],
  customFoods: [],
  settings: {
    aiProvider: 'auto',
    wallpaperMode: 'mix', autoDesktop: false, theme: 'pastel',
  },
  walls: {},
});

let state = DEFAULT_STATE();
let page = 'today';
let viewDate = ymd();
let aiInfo = { aether: false };

function merge(base, saved) {
  if (!saved || typeof saved !== 'object') return base;
  const out = { ...base, ...saved };
  for (const k of ['profile', 'targets', 'settings']) out[k] = { ...base[k], ...(saved[k] || {}) };
  return out;
}

let saveTimer;
function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => window.api.save(state).catch(e => toast('Save failed: ' + e.message)), 250);
}

function day(date = viewDate) {
  if (!state.days[date]) {
    state.days[date] = { checks: {}, food: [], weight: null, workout: null, reading: [], sleepH: null, waterL: 0, spend: [], journal: '', mood: null };
  }
  return state.days[date];
}
const peekDay = date => state.days[date];

// ================================================================ challenge math
function challenge(date = ymd()) {
  const n = diffDays(date, state.profile.startDate) + 1; // 1-based day of the year-long run
  if (n < 1) return { started: false, daysUntil: 1 - n };
  const block = Math.ceil(n / 75);
  const dayInBlock = ((n - 1) % 75) + 1;
  const blockEnd = addDays(state.profile.startDate, block * 75 - 1);
  return { started: true, n, block, dayInBlock, left: 75 - dayInBlock, blockEnd };
}

function totals(date) {
  const d = peekDay(date);
  const t = { kcal: 0, protein: 0, carbs: 0, fat: 0 };
  if (!d) return t;
  for (const f of d.food) { t.kcal += +f.kcal || 0; t.protein += +f.protein || 0; t.carbs += +f.carbs || 0; t.fat += +f.fat || 0; }
  return t;
}
const pagesOn = date => (peekDay(date)?.reading || []).reduce((s, r) => s + (+r.pages || 0), 0);

function habitDone(h, date) {
  const d = peekDay(date);
  if (!d) return false;
  if (d.checks[h.id] !== undefined && !h.auto) return !!d.checks[h.id];
  const T = state.targets;
  switch (h.id) {
    case 'protein': return totals(date).protein >= T.protein;
    case 'kcal': return totals(date).kcal >= T.kcal;
    case 'read': return pagesOn(date) >= T.pages;
    case 'sleep': return (+d.sleepH || 0) >= T.sleepH;
    case 'water': return (+d.waterL || 0) >= T.waterL;
    case 'journal': return (d.journal || '').trim().length >= 30;
    case 'workout': return !!d.checks.workout || !!d.workout?.done;
    case 'spend': return !!d.checks.spend || d.spend.length > 0;
    default: return !!d.checks[h.id];
  }
}
function dayScore(date) {
  const hs = state.habits;
  const done = hs.filter(h => habitDone(h, date)).length;
  return { done, total: hs.length, pct: hs.length ? done / hs.length : 0 };
}
const GOOD_DAY = 0.7;
function streak() {
  let s = 0; let date = ymd();
  if (dayScore(date).pct < GOOD_DAY) date = addDays(date, -1); // today still in progress
  while (diffDays(date, state.profile.startDate) >= 0 && dayScore(date).pct >= GOOD_DAY) { s++; date = addDays(date, -1); }
  return s;
}

// weights sorted by date
function weights() {
  return Object.entries(state.days).filter(([, d]) => +d.weight > 0).map(([date, d]) => ({ date, w: +d.weight })).sort((a, b) => a.date.localeCompare(b.date));
}
function latestWeight() { const w = weights(); return w.length ? w[w.length - 1].w : state.profile.startWeight; }
// kg per week from a least-squares fit over the last `days` days
function weightRate(days = 21) {
  const since = addDays(ymd(), -days);
  const pts = weights().filter(p => p.date >= since);
  if (pts.length < 3) return null;
  const xs = pts.map(p => diffDays(p.date, since)), ys = pts.map(p => p.w);
  const mx = xs.reduce((a, b) => a + b) / xs.length, my = ys.reduce((a, b) => a + b) / ys.length;
  let num = 0, den = 0;
  xs.forEach((x, i) => { num += (x - mx) * (ys[i] - my); den += (x - mx) ** 2; });
  return den ? (num / den) * 7 : null;
}
function avgOver(days, fn) {
  let s = 0, n = 0;
  for (let i = 1; i <= days; i++) { const d = addDays(ymd(), -i); if (peekDay(d)) { s += fn(d); n++; } }
  return n ? s / n : null;
}
function workoutsThisWeek() {
  const today = parseYmd(ymd()); const dow = (today.getDay() + 6) % 7; // Monday = 0
  let c = 0; for (let i = 0; i <= dow; i++) if (habitDone({ id: 'workout' }, addDays(ymd(), -i))) c++;
  return c;
}
function spendThisWeek() {
  const today = parseYmd(ymd()); const dow = (today.getDay() + 6) % 7;
  let s = 0; for (let i = 0; i <= dow; i++) s += (peekDay(addDays(ymd(), -i))?.spend || []).reduce((a, x) => a + (+x.amt || 0), 0);
  return s;
}

// ================================================================ food parsing (offline)
function allFoods() { return [...state.customFoods, ...window.FOODS]; }

function matchFood(name) {
  const q = name.toLowerCase().replace(/[^a-z\s]/g, ' ').replace(/\s+/g, ' ').trim();
  if (!q) return null;
  let best = null, bestScore = 0;
  for (const f of allFoods()) {
    for (const alias of [f.n, ...(f.a || [])]) {
      const a = alias.toLowerCase();
      let score = 0;
      if (q === a) score = 100;
      else if (q.includes(a)) score = 50 + a.length;
      else if (a.includes(q) && q.length >= 3) score = 30 + q.length;
      else {
        const qw = q.split(' '), aw = a.split(' ');
        const common = qw.filter(w => w.length > 2 && aw.includes(w)).length;
        if (common) score = 10 * common;
      }
      if (score > bestScore) { bestScore = score; best = f; }
    }
  }
  return bestScore >= 10 ? best : null;
}

function parseFoodLocal(text) {
  const parts = text.split(/,|\+|\n|;|\band\b|\bwith\b/i).map(s => s.trim()).filter(Boolean);
  const items = [], unknown = [];
  for (const raw of parts) {
    let s = raw.toLowerCase();
    let qty = null;
    const num = s.match(/(\d+(?:\.\d+)?)(?:\s*\/\s*(\d+))?/);
    if (num) { qty = num[2] ? +num[1] / +num[2] : +num[1]; s = s.replace(num[0], ' '); }
    else {
      for (const [w, v] of Object.entries(window.NUMBER_WORDS)) {
        const re = new RegExp(`\\b${w}\\b`);
        if (re.test(s)) { qty = v; s = s.replace(re, ' '); break; }
      }
    }
    let unit = null;
    for (const u of Object.keys(window.UNITS).sort((a, b) => b.length - a.length)) {
      const re = new RegExp(`(^|\\s|\\d)${u}\\b`);
      if (re.test(s)) { unit = window.UNITS[u]; s = s.replace(re, ' '); break; }
    }
    s = s.replace(/\b(of|x|small|medium|large|big|cooked|homemade)\b/g, ' ').trim();
    const f = matchFood(s);
    if (!f) { unknown.push(raw); continue; }
    if (qty == null) qty = f.unit === 'g' ? 100 : 1;
    let factor; // how many of the food's base units
    if (!unit) factor = f.unit === 'g' && qty < 10 ? qty * 100 : qty; // "paneer 2" ~ 200 g
    else if (unit.canon === f.unit) factor = qty;
    else if (unit.canon === 'g') factor = (qty * unit.grams) / f.grams;
    else if (unit.grams && f.grams) factor = (qty * unit.grams) / f.grams;
    else factor = qty;
    items.push({
      name: f.n, quantity: raw,
      kcal: Math.round(f.kcal * factor), protein: round(f.p * factor, 1), carbs: round(f.c * factor, 1), fat: round(f.f * factor, 1),
    });
  }
  return { items, unknown };
}

// ================================================================ AI context
function aiContext() {
  const today = ymd();
  const c = challenge(today);
  const lines = [];
  lines.push(`Today is ${prettyDate(today, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })}. ` +
    (c.started ? `Day ${c.dayInBlock}/75 of block ${c.block} (overall day ${c.n}).` : `Challenge starts in ${c.daysUntil} days.`));
  lines.push(`Targets: ${state.targets.kcal} kcal, ${state.targets.protein} g protein, ${state.targets.pages} pages, ${state.targets.workoutsPerWeek} workouts/week, sleep ${state.targets.sleepH}h, spend under ${inr(state.targets.weeklySpend)}/week.`);
  const r = weightRate();
  lines.push(`Weight: latest ${latestWeight()} kg (start ${state.profile.startWeight}, target ${state.profile.targetWeight})` + (r != null ? `, trend ${round(r, 2)} kg/week.` : '.'));
  for (let i = 6; i >= 0; i--) {
    const d = addDays(today, -i); const dd = peekDay(d); if (!dd) continue;
    const t = totals(d), sc = dayScore(d);
    lines.push(`${d}: ${Math.round(t.kcal)} kcal, ${Math.round(t.protein)} g protein, ${pagesOn(d)} pages, workout ${habitDone({ id: 'workout' }, d) ? (dd.workout?.type || 'yes') : 'no'}, sleep ${dd.sleepH ?? '?'}h, habits ${sc.done}/${sc.total}` +
      (dd.spend.length ? `, spent ${inr(dd.spend.reduce((a, x) => a + (+x.amt || 0), 0))}` : '') + (dd.mood ? `, mood ${dd.mood}` : ''));
  }
  const reading = state.books.filter(b => b.status === 'reading').map(b => `${b.title} (${b.pagesRead}/${b.totalPages})`);
  if (reading.length) lines.push('Reading: ' + reading.join('; '));
  const goals = state.goals.filter(g => !g.done).map(g => g.title);
  if (goals.length) lines.push('Open goals: ' + goals.join('; '));
  const j = peekDay(today)?.journal; if (j) lines.push('Today\'s journal: ' + j.slice(0, 800));
  return lines.join('\n');
}

// rule-based coach used when no AI is configured, and for the Today nudges
function insights() {
  const out = [];
  const today = ymd(), t = totals(today), T = state.targets;
  const hour = new Date().getHours();
  const pLeft = T.protein - t.protein, kLeft = T.kcal - t.kcal;
  if (pLeft > 0) {
    const shakes = pLeft >= 40 ? ' A homemade shake (2 scoops whey + milk + banana + peanut butter) covers ~55 g.' : pLeft >= 20 ? ' A scoop of whey in milk or 100 g paneer closes it.' : ' A katori of curd or a handful of roasted chana will do.';
    out.push(`<b>${Math.round(pLeft)} g protein</b> to go today.${shakes}`);
  } else out.push('<b>Protein done.</b> That is the muscle-building box ticked.');
  if (kLeft > 300) out.push(`<b>${Math.round(kLeft)} kcal</b> left.${hour >= 18 ? ' Add 2 extra roti and a glass of full-cream milk tonight.' : ' Spread it over 2 more meals plus the shake.'}`);
  const r = weightRate();
  if (r != null) {
    if (r < 0.15) out.push(`Weight trend is <b>${round(r, 2)} kg/week</b>. That is slow for a bulk: add ~250 kcal a day (a shake or 1 more katori of rice and dal).`);
    else if (r > 0.6) out.push(`Weight trend is <b>${round(r, 2)} kg/week</b>, a bit fast. Trim ~150 kcal of junk and keep protein high.`);
    else out.push(`Weight trend <b>+${round(r, 2)} kg/week</b> is right in the lean-bulk zone. Keep going.`);
  } else out.push('Log your weight each morning (after the bathroom, before food) to unlock the trend.');
  const yest = addDays(today, -1);
  if (diffDays(yest, state.profile.startDate) >= 0 && dayScore(yest).pct < GOOD_DAY) out.push('<b>Yesterday slipped.</b> Never miss twice: today only needs the minimum version done.');
  const ww = workoutsThisWeek(), dow = (new Date().getDay() + 6) % 7;
  if (ww + (6 - dow) < T.workoutsPerWeek) out.push(`You can still hit at most ${ww + (6 - dow)} workouts this week. Protect every gym slot left.`);
  const sp = spendThisWeek();
  if (sp > T.weeklySpend) out.push(`Spent <b>${inr(sp)}</b> this week, over your ${inr(T.weeklySpend)} line. No-spend days until Monday.`);
  if (pagesOn(today) < T.pages) out.push(`${T.pages - pagesOn(today)} pages left to read. Phone in the other room for 25 minutes.`);
  return out;
}

// ================================================================ suggested goals ("slightly tough")
function suggestions() {
  const S = [];
  const T = state.targets;
  const w = latestWeight();
  const r = weightRate();
  const weeklyGain = r != null ? clamp(Math.max(r * 1.15, 0.3), 0.3, 0.5) : 0.4;
  const c = challenge();
  const daysLeft = c.started ? c.left : 75;
  const blockTarget = round(Math.min(state.profile.targetWeight, w + (weeklyGain * daysLeft) / 7), 1);
  S.push({ title: `Reach ${blockTarget} kg by Day 75 of this block`, why: `That is ~${round(weeklyGain, 2)} kg/week: a bit faster than ${r != null ? 'your current trend' : 'a cautious bulk'}, still mostly muscle with 150 g protein and hard training.`, metric: 'weight', target: blockTarget });
  const avgPages = avgOver(7, pagesOn);
  const pageGoal = Math.max(T.pages, Math.ceil(((avgPages || T.pages) * 1.15) / 5) * 5);
  S.push({ title: `Read ${pageGoal} pages every day this week`, why: avgPages ? `You averaged ${round(avgPages)} pages/day. +15% is a stretch you can win.` : 'Start a little above the base target to build the habit fast.', metric: 'pages', target: pageGoal });
  const avgProt = avgOver(7, d => totals(d).protein);
  S.push({ title: `Hit ${T.protein} g protein 6 days out of 7`, why: avgProt ? `Last week averaged ${round(avgProt)} g. Make the shake non-negotiable.` : 'The shake + paneer/soya at lunch + dal at dinner gets you there.', metric: 'protein', target: T.protein });
  S.push({ title: `${Math.min(6, T.workoutsPerWeek + (workoutsThisWeek() >= T.workoutsPerWeek ? 1 : 0))} gym sessions this week with one PR`, why: 'Add 2.5 kg or 1 rep to one main lift. Progressive overload is what turns food into size.', metric: 'workouts', target: T.workoutsPerWeek });
  S.push({ title: 'Three no-spend days this week', why: `Keeps you under ${inr(T.weeklySpend)} and cuts eating out without needing willpower every day.`, metric: 'spend', target: 3 });
  S.push({ title: 'Lights out by 10:30 PM on 5 nights', why: 'Gym at 6 AM needs a 10:30 bedtime for 7+ hours. Sleep is when you grow.', metric: 'sleep', target: 5 });
  S.push({ title: 'One honest conversation, one boundary held', why: 'Say no to one plan that does not fit your goals, and reach out to one person who does.', metric: 'social', target: 1 });
  return S;
}

// ================================================================ charts (inline SVG)
function lineChart(points, { width = 640, height = 220, target = null, color = 'var(--accent)', unit = '' } = {}) {
  if (points.length < 2) return '<div class="empty">Log at least two entries to see the chart.</div>';
  const pad = { l: 36, r: 12, t: 14, b: 26 };
  const ys = points.map(p => p.y).concat(target != null ? [target] : []);
  let min = Math.min(...ys), max = Math.max(...ys);
  if (max - min < 1) { min -= 0.5; max += 0.5; }
  const span = max - min; min -= span * 0.1; max += span * 0.1;
  const x0 = parseYmd(points[0].x).getTime(), x1 = parseYmd(points[points.length - 1].x).getTime();
  const X = x => pad.l + ((parseYmd(x).getTime() - x0) / Math.max(1, x1 - x0)) * (width - pad.l - pad.r);
  const Y = y => pad.t + (1 - (y - min) / (max - min)) * (height - pad.t - pad.b);
  let g = '';
  for (let i = 0; i <= 4; i++) {
    const v = min + ((max - min) * i) / 4;
    g += `<line class="grid-line" x1="${pad.l}" x2="${width - pad.r}" y1="${Y(v)}" y2="${Y(v)}"/><text x="${pad.l - 6}" y="${Y(v) + 4}" text-anchor="end">${round(v, 1)}</text>`;
  }
  const labels = [points[0], points[Math.floor(points.length / 2)], points[points.length - 1]];
  labels.forEach(p => { g += `<text x="${X(p.x)}" y="${height - 6}" text-anchor="middle">${prettyDate(p.x, { day: 'numeric', month: 'short' })}</text>`; });
  if (target != null) g += `<line x1="${pad.l}" x2="${width - pad.r}" y1="${Y(target)}" y2="${Y(target)}" stroke="var(--mint-ink)" stroke-dasharray="5 5" stroke-width="1.5"/><text x="${width - pad.r}" y="${Y(target) - 5}" text-anchor="end" style="fill:var(--mint-ink)">target ${target}${unit}</text>`;
  const path = points.map((p, i) => `${i ? 'L' : 'M'}${X(p.x).toFixed(1)},${Y(p.y).toFixed(1)}`).join(' ');
  const area = `${path} L${X(points[points.length - 1].x)},${height - pad.b} L${X(points[0].x)},${height - pad.b} Z`;
  // 7-point moving average
  const avg = points.map((p, i) => { const s = points.slice(Math.max(0, i - 6), i + 1); return { x: p.x, y: s.reduce((a, b) => a + b.y, 0) / s.length }; });
  const avgPath = avg.map((p, i) => `${i ? 'L' : 'M'}${X(p.x).toFixed(1)},${Y(p.y).toFixed(1)}`).join(' ');
  const dots = points.map(p => `<circle cx="${X(p.x)}" cy="${Y(p.y)}" r="3" fill="${color}"><title>${p.x}: ${p.y}${unit}</title></circle>`).join('');
  return `<svg class="chart" viewBox="0 0 ${width} ${height}">${g}<path d="${area}" fill="${color}" opacity=".10"/><path d="${path}" fill="none" stroke="${color}" stroke-width="1.5" opacity=".45"/><path d="${avgPath}" fill="none" stroke="${color}" stroke-width="3" stroke-linecap="round"/>${dots}</svg>`;
}

function barChart(bars, { width = 640, height = 180, target = null, color = 'var(--accent)' } = {}) {
  const pad = { l: 36, r: 8, t: 10, b: 24 };
  const max = Math.max(1, target || 0, ...bars.map(b => b.y)) * 1.1;
  const bw = (width - pad.l - pad.r) / bars.length;
  const Y = y => pad.t + (1 - y / max) * (height - pad.t - pad.b);
  let s = '';
  for (let i = 0; i <= 3; i++) { const v = (max * i) / 3; s += `<line class="grid-line" x1="${pad.l}" x2="${width - pad.r}" y1="${Y(v)}" y2="${Y(v)}"/><text x="${pad.l - 6}" y="${Y(v) + 4}" text-anchor="end">${Math.round(v)}</text>`; }
  bars.forEach((b, i) => {
    const x = pad.l + i * bw + bw * 0.18, w = bw * 0.64, y = Y(b.y);
    const hit = target != null && b.y >= target;
    s += `<rect x="${x}" y="${y}" width="${w}" height="${Math.max(0, height - pad.b - y)}" rx="5" fill="${hit ? 'var(--mint-ink)' : color}" opacity="${hit ? 0.85 : 0.65}"><title>${b.x}: ${Math.round(b.y)}</title></rect>`;
    if (i % Math.ceil(bars.length / 7) === 0) s += `<text x="${x + w / 2}" y="${height - 6}" text-anchor="middle">${prettyDate(b.x, { day: 'numeric', month: 'short' })}</text>`;
  });
  if (target != null) s += `<line x1="${pad.l}" x2="${width - pad.r}" y1="${Y(target)}" y2="${Y(target)}" stroke="var(--peach-ink)" stroke-dasharray="5 5" stroke-width="1.5"/>`;
  return `<svg class="chart" viewBox="0 0 ${width} ${height}">${s}</svg>`;
}
const lastNDays = n => Array.from({ length: n }, (_, i) => addDays(ymd(), i - n + 1));

// ================================================================ wallpaper
let wallCache = {}; // date -> { kind, dataUrl, file, credit, link }

function wallKindFor(date) {
  const mode = state.settings.wallpaperMode;
  if (state.walls[date]?.kind) return state.walls[date].kind;
  if (mode === 'dark') return 'dark';
  if (mode === 'scenery') return 'scenery';
  return hashSeed(date + 'k') % 5 < 2 ? 'dark' : 'scenery'; // ~40% black minimal, ~60% scenery
}

function renderDarkWallpaper(date, motto) {
  const W = 2560, H = 1440;
  const cv = document.createElement('canvas'); cv.width = W; cv.height = H;
  const g = cv.getContext('2d');
  g.fillStyle = '#0b0b0d'; g.fillRect(0, 0, W, H);
  // soft vignette + faint grain for a matte look
  const rad = g.createRadialGradient(W / 2, H / 2, 200, W / 2, H / 2, W * 0.7);
  rad.addColorStop(0, 'rgba(40,38,52,0.55)'); rad.addColorStop(1, 'rgba(0,0,0,0)');
  g.fillStyle = rad; g.fillRect(0, 0, W, H);
  const rnd = (() => { let s = hashSeed(date); return () => ((s = Math.imul(s ^ (s >>> 15), 2246822507) >>> 0) % 1000) / 1000; })();
  for (let i = 0; i < 9000; i++) { g.fillStyle = `rgba(255,255,255,${rnd() * 0.035})`; g.fillRect(rnd() * W, rnd() * H, 2, 2); }
  g.fillStyle = '#f2f0f7';
  g.textAlign = 'center'; g.textBaseline = 'middle';
  let size = 150;
  g.font = `800 ${size}px "Segoe UI", "Helvetica Neue", Arial, sans-serif`;
  while (g.measureText(motto).width > W * 0.72 && size > 60) { size -= 6; g.font = `800 ${size}px "Segoe UI", "Helvetica Neue", Arial, sans-serif`; }
  if ('letterSpacing' in g) g.letterSpacing = '6px';
  g.fillText(motto, W / 2, H / 2 - 20);
  const c = challenge(date);
  g.font = `600 34px "Segoe UI", "Helvetica Neue", Arial, sans-serif`;
  g.fillStyle = 'rgba(242,240,247,0.45)';
  if ('letterSpacing' in g) g.letterSpacing = '10px';
  g.fillText(c.started ? `DAY ${c.dayInBlock} / 75` : 'DAY 0', W / 2, H / 2 + size * 0.75);
  return cv.toDataURL('image/png');
}

function renderFallbackScenery(date) {
  // Offline fallback: a calm pastel gradient "landscape" so there is always a wallpaper.
  const W = 2560, H = 1440;
  const cv = document.createElement('canvas'); cv.width = W; cv.height = H;
  const g = cv.getContext('2d');
  const palettes = [['#ffd6e0', '#d9ccff', '#a8c5ff'], ['#ffe6c7', '#ffc2b3', '#b9a7e6'], ['#d6f5ec', '#a8dcd1', '#6f9fb3'], ['#fff1c9', '#ffd1a8', '#e79aa5']];
  const p = pick(palettes, date);
  const sky = g.createLinearGradient(0, 0, 0, H);
  sky.addColorStop(0, p[2]); sky.addColorStop(0.6, p[1]); sky.addColorStop(1, p[0]);
  g.fillStyle = sky; g.fillRect(0, 0, W, H);
  g.fillStyle = 'rgba(255,255,255,0.65)'; g.beginPath(); g.arc(W * 0.7, H * 0.38, 120, 0, Math.PI * 2); g.fill();
  const layers = 4;
  for (let l = 0; l < layers; l++) {
    g.fillStyle = `rgba(60,50,90,${0.12 + l * 0.12})`;
    g.beginPath(); g.moveTo(0, H);
    const base = H * (0.55 + l * 0.1), seed = hashSeed(date + l);
    for (let x = 0; x <= W; x += 40) {
      const y = base - Math.sin((x / W) * Math.PI * (2 + l) + seed) * (80 - l * 12) - Math.sin(x / 170 + seed) * 18;
      g.lineTo(x, y);
    }
    g.lineTo(W, H); g.closePath(); g.fill();
  }
  return cv.toDataURL('image/jpeg', 0.92);
}

async function loadWallpaper(date, force = false) {
  if (wallCache[date] && !force) return wallCache[date];
  const kind = wallKindFor(date);
  let w;
  if (kind === 'dark') {
    const motto = state.walls[date]?.motto || pick(window.MOTTOS, date);
    const dataUrl = renderDarkWallpaper(date, motto);
    const file = await window.api.saveGenerated(date, 'dark', dataUrl);
    w = { kind, dataUrl, file, credit: 'Generated by Grind 75', motto };
  } else {
    const r = await window.api.scenery(date, state.settings).catch(() => null);
    if (r) w = { kind, dataUrl: r.dataUrl, file: r.file, credit: r.credit, link: r.link };
    else {
      const dataUrl = renderFallbackScenery(date);
      const file = await window.api.saveGenerated(date, 'fallback', dataUrl);
      w = { kind, dataUrl, file, credit: 'Offline pastel fallback (no internet)' };
    }
  }
  wallCache[date] = w;
  return w;
}

async function maybeAutoDesktop() {
  const today = ymd();
  if (!state.settings.autoDesktop || state.walls[today]?.desktopSet) return;
  const w = await loadWallpaper(today);
  const r = await window.api.setDesktop(w.file);
  if (r.ok) { state.walls[today] = { ...(state.walls[today] || {}), desktopSet: true }; save(); }
}

// ================================================================ pages
const pages = {};

pages.today = () => {
  const today = ymd(), c = challenge(today), d = day(today), T = state.targets, t = totals(today);
  const q = pick(window.QUOTES, today + 'q');
  const sc = dayScore(today);
  const yest = addDays(today, -1);
  const missedYesterday = diffDays(yest, state.profile.startDate) >= 0 && dayScore(yest).pct < GOOD_DAY;
  const greet = new Date().getHours() < 12 ? 'Good morning' : new Date().getHours() < 17 ? 'Good afternoon' : 'Good evening';
  const spentToday = d.spend.reduce((a, x) => a + (+x.amt || 0), 0);

  return `
  <div class="hero" id="hero">
    <div class="tools">
      <button data-act="wall-swap" title="Switch between black minimal and scenery">⇄ Swap style</button>
      <button data-act="wall-desktop" title="Set as your desktop wallpaper">🖥 Set as desktop</button>
    </div>
    <div class="day">${c.started ? `Day ${c.dayInBlock}` : `${c.daysUntil} days to go`}</div>
    <div class="meta">${c.started ? `Block ${c.block} of 5 · ${c.left} days left · ends ${prettyDate(c.blockEnd)}` : 'Get ready.'}</div>
    ${c.started ? `<div class="progress-75"><div style="width:${(c.dayInBlock / 75) * 100}%"></div></div>` : ''}
    <div class="quote">“${esc(q[0])}” <em>· ${esc(q[1])}</em></div>
    <div class="motto" id="hero-motto"></div>
    <div class="credit" id="wall-credit"></div>
  </div>

  <div style="height:18px"></div>
  ${missedYesterday ? '<div class="banner warn">Yesterday was below the line. Never miss twice: today only needs the basics done, then you are back on track.</div>' : ''}

  <div class="grid g3">
    <div class="card span2">
      <div class="row between"><h2>${greet}, ${esc(state.profile.name)} · today's checklist</h2><span class="tag ${sc.pct >= 1 ? 'mint' : sc.pct >= GOOD_DAY ? 'lav' : 'peach'}">${sc.done}/${sc.total} ${sc.pct >= 1 ? 'perfect day' : sc.pct >= GOOD_DAY ? 'solid day' : 'keep going'}</span></div>
      <div class="checks">
        ${state.habits.map(h => {
          const done = habitDone(h, today);
          const hint = h.auto ? habitHint(h, today) : '';
          return `<div class="check ${done ? 'done' : ''} ${h.auto ? 'auto' : ''}" data-act="check" data-id="${h.id}" title="${h.auto ? 'Ticks itself from your logs' : 'Click to toggle'}">
            <div class="box">${done ? '✓' : ''}</div><div class="lbl">${h.emoji} ${esc(h.label)}${hint ? `<div class="hint">${esc(hint)}</div>` : ''}</div></div>`;
        }).join('')}
      </div>
      <p class="small muted" style="margin:10px 0 0">Dashed boxes tick themselves from your food, reading, sleep, water and journal logs.</p>
    </div>

    <div class="card">
      <h2>Fuel</h2>
      <div class="row" style="gap:16px">
        <div class="ring" style="--p:${clamp((t.kcal / T.kcal) * 100, 0, 100)};--c:var(--peach-ink)"><div>${Math.round(t.kcal)}<small>of ${T.kcal} kcal</small></div></div>
        <div class="ring" style="--p:${clamp((t.protein / T.protein) * 100, 0, 100)};--c:var(--mint-ink)"><div>${Math.round(t.protein)}g<small>of ${T.protein}g protein</small></div></div>
      </div>
      <div class="row" style="margin-top:14px"><button class="btn soft" data-go="food">+ Log food</button><button class="btn ghost" data-act="quick-shake">+ Shake</button></div>
      <div class="stack small" style="margin-top:16px;gap:6px">
        <div class="row between"><span class="muted">Next workout</span><b>${esc(d.workout?.done ? d.workout.type + ' ✓' : nextSplit())}</b></div>
        <div class="row between"><span class="muted">Reading</span><b>${esc(state.books.find(b => b.status === 'reading')?.title || 'add a book')}</b></div>
        <div class="row between"><span class="muted">Weight</span><b>${round(latestWeight(), 1)} → ${state.profile.targetWeight} kg</b></div>
      </div>
    </div>

    <div class="card tint-sky">
      <h2>Quick log</h2>
      <div class="stack">
        <div class="row"><span style="width:70px">⚖️ Weight</span><input type="number" step="0.1" class="w80" id="q-weight" value="${d.weight ?? ''}" placeholder="kg"><span class="muted small">kg</span></div>
        <div class="row"><span style="width:70px">😴 Sleep</span><input type="number" step="0.25" class="w80" id="q-sleep" value="${d.sleepH ?? ''}" placeholder="h"><span class="muted small">hours</span></div>
        <div class="row"><span style="width:70px">💧 Water</span><button class="btn tiny soft" data-act="water" data-v="-0.25">−</button><b>${round(d.waterL, 2)} L</b><button class="btn tiny soft" data-act="water" data-v="0.25">+</button><button class="btn tiny soft" data-act="water" data-v="0.5">+½L</button></div>
      </div>
    </div>

    <div class="card tint-butter">
      <h2>Money today · ${inr(spentToday)}</h2>
      <div class="row"><input type="number" class="w80" id="spend-amt" placeholder="₹"><input type="text" class="grow" id="spend-what" placeholder="on what?"><button class="btn tiny" data-act="spend-add">Add</button></div>
      <div style="margin-top:8px">${d.spend.map((s, i) => `<div class="list-item small"><span>${inr(s.amt)}</span><span class="muted" style="flex:1">${esc(s.what)}</span><button class="btn ghost tiny" data-act="spend-del" data-i="${i}">✕</button></div>`).join('') || '<div class="small muted">Nothing spent yet. A zero is a good number.</div>'}</div>
      <div class="small muted" style="margin-top:6px">This week ${inr(spendThisWeek())} of ${inr(T.weeklySpend)}</div>
    </div>

    <div class="card tint-lav">
      <h2>AETHER says</h2>
      ${insights().slice(0, 4).map(i => `<div class="insight">${i}</div>`).join('')}
    </div>
  </div>`;
};

function habitHint(h, date) {
  const d = peekDay(date), T = state.targets, t = totals(date);
  switch (h.id) {
    case 'protein': return `${Math.round(t.protein)} / ${T.protein} g`;
    case 'kcal': return `${Math.round(t.kcal)} / ${T.kcal}`;
    case 'read': return `${pagesOn(date)} / ${T.pages} pages`;
    case 'sleep': return d?.sleepH ? `${d.sleepH} h` : 'log below';
    case 'water': return `${round(d?.waterL || 0, 2)} / ${T.waterL} L`;
    case 'journal': return (d?.journal || '').trim().length >= 30 ? 'written' : 'a few lines in Journal';
    default: return '';
  }
}

// ---------------------------------------------------------------- food
let foodPreview = null; // { items, source, unknown }
pages.food = () => {
  const d = day(viewDate), t = totals(viewDate), T = state.targets;
  const meals = ['Breakfast', 'Pre-workout', 'Post-workout shake', 'Lunch', 'Evening snack', 'Dinner', 'Before bed'];
  const aiLabel = aiProviderLabel();
  const byMeal = meals.concat(['Other']).map(m => ({ m, items: d.food.map((f, i) => ({ ...f, i })).filter(f => (f.meal || 'Other') === m) })).filter(g => g.items.length);
  const favs = [['Shake', '1 mass shake'], ['2 roti', '2 roti'], ['Dal katori', '1 katori dal'], ['Rice katori', '1 katori rice'], ['100g paneer', '100g paneer'], ['50g soya', '50g soya chunks'], ['Curd', '1 katori curd'], ['Milk', '1 glass milk'], ['Banana', '1 banana'], ['Peanuts', '1 handful peanuts']];

  return `
  <div class="row between"><div><h1>Food & protein</h1><p class="sub">Type what you ate in plain words, like <span class="kbd">3 roti, 1 katori rajma, 200 ml curd</span>.</p></div>
  ${dateNav()}</div>
  <div class="grid g3">
    <div class="card span2">
      <h2>Add food</h2>
      <div class="row" style="align-items:flex-start">
        <textarea id="food-text" placeholder="e.g. 2 aloo paratha with curd, 1 glass milk" style="min-height:64px;flex:1"></textarea>
      </div>
      <div class="row" style="margin-top:10px">
        <select id="food-meal">${meals.map(m => `<option ${m === guessMeal() ? 'selected' : ''}>${m}</option>`).join('')}</select>
        <button class="btn" data-act="food-estimate">Estimate ${aiLabel ? `with ${aiLabel}` : '(offline)'}</button>
        <button class="btn ghost" data-act="food-offline">Use offline table</button>
      </div>
      <div class="row" style="margin-top:10px">${favs.map(([l, txt]) => `<button class="chip" data-act="food-fav" data-t="${esc(txt)}">+ ${l}</button>`).join('')}</div>
      <div id="food-preview">${foodPreviewHtml()}</div>
    </div>
    <div class="card">
      <h2>${viewDate === ymd() ? 'Today' : prettyDate(viewDate)}</h2>
      ${macroRow('Calories', t.kcal, T.kcal, 'peach', 'kcal')}
      ${macroRow('Protein', t.protein, T.protein, 'mint', 'g')}
      <div class="row small muted" style="margin-top:10px;gap:16px"><span>Carbs ${Math.round(t.carbs)} g</span><span>Fat ${Math.round(t.fat)} g</span></div>
      <div class="insight" style="margin-top:14px">${foodTip(t)}</div>
    </div>
    <div class="card span2">
      <h2>Log</h2>
      ${byMeal.length ? byMeal.map(g => `<h3 style="margin-top:12px">${esc(g.m)}</h3><table><tbody>${g.items.map(f => `<tr><td>${esc(f.name)}<div class="small muted">${esc(f.quantity)}${f.src ? ` · ${esc(f.src)}` : ''}</div></td><td class="right">${Math.round(f.kcal)} kcal</td><td class="right"><b>${round(f.protein, 1)} g</b> P</td><td class="right" style="width:40px"><button class="btn ghost tiny" data-act="food-del" data-i="${f.i}">✕</button></td></tr>`).join('')}</tbody></table>`).join('') : '<div class="empty">Nothing logged for this day yet.</div>'}
    </div>
    <div class="card">
      <h2>Last 14 days · protein</h2>
      ${barChart(lastNDays(14).map(x => ({ x, y: totals(x).protein })), { target: T.protein, color: 'var(--sky-ink)', height: 200 })}
      <h2 style="margin-top:16px">Calories</h2>
      ${barChart(lastNDays(14).map(x => ({ x, y: totals(x).kcal })), { target: T.kcal, color: 'var(--peach-ink)', height: 200 })}
    </div>
  </div>`;
};
function macroRow(label, v, target, cls, unit) {
  return `<div style="margin-top:10px"><div class="row between small"><b>${label}</b><span>${Math.round(v)} / ${target} ${unit}</span></div><div class="meter ${cls}"><div style="width:${clamp((v / target) * 100, 0, 100)}%"></div></div></div>`;
}
function foodTip(t) {
  const T = state.targets, p = T.protein - t.protein, k = T.kcal - t.kcal;
  if (p <= 0 && k <= 0) return '<b>Both targets hit.</b> That is a growing day.';
  const bits = [];
  if (p > 0) bits.push(`<b>${Math.round(p)} g protein</b> left: ${p > 45 ? 'shake + 150 g paneer or soya curry' : p > 20 ? 'one shake or 100 g paneer' : 'curd or roasted chana'}`);
  if (k > 0) bits.push(`<b>${Math.round(k)} kcal</b> left: ${k > 900 ? 'a full plate (3 roti, dal, sabzi, rice) + shake' : k > 400 ? 'an extra roti-dal round and a banana' : 'milk and a handful of peanuts'}`);
  return bits.join('<br>');
}
function guessMeal() {
  const h = new Date().getHours();
  return h < 6 ? 'Pre-workout' : h < 8 ? 'Post-workout shake' : h < 11 ? 'Breakfast' : h < 15 ? 'Lunch' : h < 19 ? 'Evening snack' : h < 22 ? 'Dinner' : 'Before bed';
}
function foodPreviewHtml() {
  if (!foodPreview) return '';
  if (foodPreview.loading) return `<div class="insight" style="margin-top:12px"><span class="spinner"></span> Estimating…</div>`;
  const it = foodPreview.items;
  const sum = it.reduce((a, x) => ({ k: a.k + (+x.kcal || 0), p: a.p + (+x.protein || 0) }), { k: 0, p: 0 });
  return `<div style="margin-top:14px">
    ${foodPreview.note ? `<div class="insight">${esc(foodPreview.note)}</div>` : ''}
    ${it.length ? `<table><thead><tr><th>Item</th><th>kcal</th><th>Protein g</th><th>Carbs g</th><th>Fat g</th><th></th></tr></thead><tbody>
    ${it.map((x, i) => `<tr data-i="${i}"><td><input type="text" data-f="name" value="${esc(x.name)}"><div class="small muted">${esc(x.quantity)}</div></td>
      <td><input type="number" data-f="kcal" value="${x.kcal}"></td><td><input type="number" step="0.1" data-f="protein" value="${x.protein}"></td>
      <td><input type="number" step="0.1" data-f="carbs" value="${x.carbs}"></td><td><input type="number" step="0.1" data-f="fat" value="${x.fat}"></td>
      <td><button class="btn ghost tiny" data-act="prev-del" data-i="${i}">✕</button></td></tr>`).join('')}
    </tbody></table>
    <div class="row between" style="margin-top:10px"><span class="muted small">Source: ${esc(foodPreview.source)} · total ${Math.round(sum.k)} kcal, ${round(sum.p, 1)} g protein. Edit any number before adding.</span>
    <div class="row"><label class="toggle small"><input type="checkbox" id="remember-food" ${foodPreview.source !== 'offline table' ? 'checked' : ''}> remember for offline use</label><button class="btn mint" data-act="food-add">Add to log</button></div></div>` : ''}
  </div>`;
}

// ---------------------------------------------------------------- body
const SPLIT = ['Back + biceps', 'Chest + shoulders + triceps', 'Quads', 'Hamstrings + calves + abs', 'Rest / walk'];
function nextSplit() {
  const done = Object.entries(state.days).filter(([, d]) => d.workout?.type && SPLIT.includes(d.workout.type)).sort((a, b) => b[0].localeCompare(a[0]));
  if (!done.length) return SPLIT[0];
  return SPLIT[(SPLIT.indexOf(done[0][1].workout.type) + 1) % SPLIT.length];
}
pages.body = () => {
  const d = day(viewDate), w = weights();
  const r = weightRate(), latest = latestWeight(), target = state.profile.targetWeight;
  const eta = r && r > 0.05 ? addDays(ymd(), Math.ceil(((target - latest) / r) * 7)) : null;
  const bmi = latest / (state.profile.heightCm / 100) ** 2;
  const recentWorkouts = Object.entries(state.days).filter(([, x]) => x.workout?.done).sort((a, b) => b[0].localeCompare(a[0])).slice(0, 12);
  const wk = d.workout || { done: false, type: nextSplit(), notes: '' };
  return `
  <div class="row between"><div><h1>Body</h1><p class="sub">Weigh in each morning; the thick line is your 7-day average, which is what matters.</p></div>${dateNav()}</div>
  <div class="grid g4">
    <div class="card tint-lav"><h3>Now</h3><div class="big">${round(latest, 1)} kg</div><div class="small">started ${state.profile.startWeight} kg · ${latest - state.profile.startWeight >= 0 ? '+' : ''}${round(latest - state.profile.startWeight, 1)} kg</div></div>
    <div class="card tint-mint"><h3>Trend</h3><div class="big">${r == null ? '–' : (r >= 0 ? '+' : '') + round(r, 2)}</div><div class="small">kg / week (last 3 weeks)</div></div>
    <div class="card tint-peach"><h3>Target</h3><div class="big">${target} kg</div><div class="small">${eta ? `at this pace: ${prettyDate(eta, { day: 'numeric', month: 'short', year: 'numeric' })}` : 'need a few weeks of data'}</div></div>
    <div class="card tint-sky"><h3>This week</h3><div class="big">${workoutsThisWeek()} / ${state.targets.workoutsPerWeek}</div><div class="small">workouts · BMI ${round(bmi, 1)}</div></div>
    <div class="card span2" style="grid-column: span 3">
      <div class="row between"><h2>Weight</h2><div class="row"><input type="number" step="0.1" class="w80" id="b-weight" value="${d.weight ?? ''}" placeholder="kg"><button class="btn tiny" data-act="b-weight">Save for ${viewDate === ymd() ? 'today' : prettyDate(viewDate)}</button></div></div>
      ${lineChart(w.map(p => ({ x: p.date, y: p.w })), { target, unit: ' kg' })}
    </div>
    <div class="card">
      <h2>Workout · ${viewDate === ymd() ? 'today' : prettyDate(viewDate)}</h2>
      <div class="stack">
        <select id="w-type">${SPLIT.concat(['Cardio', 'Arms + shoulders', 'Full body', 'Other']).map(s => `<option ${s === wk.type ? 'selected' : ''}>${s}</option>`).join('')}</select>
        <textarea id="w-notes" placeholder="e.g. Deadlift 3x5 @ 80 kg (PR), Pull-ups 4x8, Rows 3x10 @ 30">${esc(wk.notes || '')}</textarea>
        <div class="row"><button class="btn mint" data-act="w-save">${wk.done ? 'Update' : 'Mark done'}</button>${wk.done ? '<button class="btn ghost" data-act="w-undo">Undo</button>' : ''}</div>
        <div class="small muted">Up next in your rotation: <b>${nextSplit()}</b></div>
      </div>
    </div>
    <div class="card" style="grid-column: span 4">
      <h2>Recent sessions</h2>
      ${recentWorkouts.length ? recentWorkouts.map(([date, x]) => `<div class="list-item"><span class="tag lav">${prettyDate(date)}</span><b>${esc(x.workout.type)}</b><span class="muted small" style="flex:1;white-space:pre-wrap">${esc(x.workout.notes || '')}</span></div>`).join('') : '<div class="empty">No sessions logged yet. 6 AM, let\'s go.</div>'}
    </div>
  </div>`;
};

// ---------------------------------------------------------------- reading
pages.reading = () => {
  const reading = state.books.filter(b => b.status === 'reading'), done = state.books.filter(b => b.status === 'done'), queue = state.books.filter(b => b.status === 'queue');
  const d = day(viewDate);
  const totalPages = Object.values(state.days).reduce((s, x) => s + (x.reading || []).reduce((a, r) => a + (+r.pages || 0), 0), 0);
  const bookRow = b => {
    const pct = b.totalPages ? clamp((b.pagesRead / b.totalPages) * 100, 0, 100) : 0;
    return `<div class="list-item"><div style="flex:1;min-width:0"><b>${esc(b.title)}</b> <span class="muted small">${esc(b.author || '')}</span>
      <div class="meter sky" style="margin-top:6px"><div style="width:${pct}%"></div></div><div class="small muted">${b.pagesRead} / ${b.totalPages || '?'} pages</div></div>
      ${b.status === 'reading' ? `<input type="number" class="w80" placeholder="pages" data-book="${b.id}"><button class="btn tiny" data-act="read-log" data-id="${b.id}">Log</button>` : ''}
      ${b.status === 'queue' ? `<button class="btn tiny soft" data-act="book-start" data-id="${b.id}">Start</button>` : ''}
      <button class="btn ghost tiny" data-act="book-del" data-id="${b.id}">✕</button></div>`;
  };
  return `
  <div class="row between"><div><h1>Reading</h1><p class="sub">${state.targets.pages} pages a day is ~${Math.round((state.targets.pages * 75) / 250)} books per block.</p></div>${dateNav()}</div>
  <div class="grid g3">
    <div class="card tint-sky"><h3>${viewDate === ymd() ? 'Today' : prettyDate(viewDate)}</h3><div class="big">${pagesOn(viewDate)} / ${state.targets.pages}</div><div class="small">pages</div></div>
    <div class="card tint-lav"><h3>All time</h3><div class="big">${totalPages}</div><div class="small">pages · ${done.length} books finished</div></div>
    <div class="card tint-mint"><h3>7-day average</h3><div class="big">${round(avgOver(7, pagesOn) || 0)}</div><div class="small">pages a day</div></div>
    <div class="card span2">
      <h2>Currently reading</h2>
      ${reading.map(bookRow).join('') || '<div class="empty">Add a book below and press Start.</div>'}
      <h2 style="margin-top:18px">Up next</h2>
      ${queue.map(bookRow).join('') || '<div class="empty">Queue is empty.</div>'}
      <div class="row" style="margin-top:14px"><input type="text" class="grow" id="bk-title" placeholder="Title"><input type="text" id="bk-author" placeholder="Author"><input type="number" class="w80" id="bk-pages" placeholder="pages"><button class="btn" data-act="book-add">Add book</button></div>
    </div>
    <div class="card">
      <h2>Last 14 days</h2>
      ${barChart(lastNDays(14).map(x => ({ x, y: pagesOn(x) })), { target: state.targets.pages, color: 'var(--sky-ink)', height: 200 })}
      <h2 style="margin-top:16px">Finished</h2>
      ${done.map(b => `<div class="list-item small">✅ <b>${esc(b.title)}</b><span class="muted">${esc(b.author || '')}</span></div>`).join('') || '<div class="empty">Your first finished book will show here.</div>'}
    </div>
  </div>`;
};

// ---------------------------------------------------------------- goals
let aiPlan = null;
pages.goals = () => {
  const sugg = suggestions().filter(s => !state.goals.some(g => g.title === s.title && !g.done));
  const open = state.goals.filter(g => !g.done), closed = state.goals.filter(g => g.done);
  return `
  <h1>Goals</h1><p class="sub">Suggestions are tuned a notch above what your logs say you are doing now. Accept the ones you will actually chase.</p>
  <div class="grid g2">
    <div class="card">
      <h2>Your goals</h2>
      ${open.map(g => `<div class="list-item"><button class="btn tiny mint" data-act="goal-done" data-id="${g.id}">✓</button><div style="flex:1"><b>${esc(g.title)}</b>${g.deadline ? `<div class="small muted">by ${prettyDate(g.deadline)}</div>` : ''}</div><button class="btn ghost tiny" data-act="goal-del" data-id="${g.id}">✕</button></div>`).join('') || '<div class="empty">No goals yet. Accept a suggestion or write your own.</div>'}
      <div class="row" style="margin-top:12px"><input type="text" class="grow" id="goal-title" placeholder="Write your own goal"><input type="date" id="goal-date"><button class="btn" data-act="goal-add">Add</button></div>
      ${closed.length ? `<h3 style="margin-top:18px">Done</h3>${closed.slice(-8).reverse().map(g => `<div class="list-item small">🏆 ${esc(g.title)}</div>`).join('')}` : ''}
    </div>
    <div class="card tint-lav">
      <div class="row between"><h2>Suggested (slightly tough)</h2><button class="btn tiny soft" data-act="ai-plan" style="background:var(--card)">${aiProviderLabel() ? 'Ask AETHER for a plan' : 'Plan (needs AI)'}</button></div>
      ${sugg.map((s, i) => `<div class="insight"><div class="row between"><b>${esc(s.title)}</b><button class="btn tiny" data-act="goal-accept" data-i="${i}">Accept</button></div><div class="small muted" style="margin-top:4px">${esc(s.why)}</div></div>`).join('')}
    </div>
    ${aiPlan ? `<div class="card span2"><h2>AETHER's plan</h2><div style="white-space:pre-wrap">${aiPlan.loading ? '<span class="spinner"></span> Thinking…' : esc(aiPlan.text)}</div></div>` : ''}
  </div>`;
};

// ---------------------------------------------------------------- journal + chat
const PROMPTS = [
  'What did I do today that future me will thank me for?',
  'Where did I spend energy on people who do not fit my goals?',
  'What boundary did I hold, or wish I had held?',
  'One thing I learned from my reading today:',
  'What made today harder than it needed to be? What is the fix?',
  'What am I avoiding? What is the smallest first step?',
  'Who is one person worth reconnecting with, and why?',
  'Rate my effort 1-10. What would make it a 10 tomorrow?',
  'What did I want to buy or eat out today, and what did I do instead?',
];
let chatBusy = false;
pages.journal = () => {
  const d = day(viewDate);
  const prompts = [pick(PROMPTS, viewDate), pick(PROMPTS, viewDate + 'b'), pick(PROMPTS, viewDate + 'c')].filter((v, i, a) => a.indexOf(v) === i);
  const past = Object.entries(state.days).filter(([date, x]) => x.journal && date !== viewDate).sort((a, b) => b[0].localeCompare(a[0])).slice(0, 10);
  const moods = ['😞', '😐', '🙂', '😄', '🔥'];
  return `
  <div class="row between"><div><h1>Journal & Coach</h1><p class="sub">Write it down, then talk it through with AETHER, your coach.</p></div>${dateNav()}</div>
  <div class="grid g2">
    <div class="card">
      <div class="row between"><h2>${viewDate === ymd() ? 'Today' : prettyDate(viewDate)}</h2><div class="moods">${moods.map(m => `<button class="${d.mood === m ? 'on' : ''}" data-act="mood" data-m="${m}">${m}</button>`).join('')}</div></div>
      <div class="row" style="margin-bottom:8px">${prompts.map(p => `<button class="chip" data-act="prompt" data-p="${esc(p)}">${esc(p)}</button>`).join('')}</div>
      <textarea id="journal" style="min-height:260px" placeholder="How did today go?">${esc(d.journal)}</textarea>
      <div class="row between" style="margin-top:8px"><span class="small muted" id="j-saved">Saves automatically</span><button class="btn soft" data-act="j-reflect">Ask AETHER to reflect on this</button></div>
      ${past.length ? `<h3 style="margin-top:18px">Earlier entries</h3>${past.map(([date, x]) => `<div class="list-item small" style="align-items:flex-start;cursor:pointer" data-act="goto-date" data-d="${date}"><span class="tag lav">${prettyDate(date)}</span><span>${x.mood || ''}</span><span class="muted" style="flex:1">${esc(x.journal.slice(0, 140))}${x.journal.length > 140 ? '…' : ''}</span></div>`).join('')}` : ''}
    </div>
    <div class="card tint-lav" style="display:flex;flex-direction:column">
      <div class="row between"><h2>AETHER</h2><span class="small">${aiProviderLabel() ? `via ${aiProviderLabel()}` : 'offline mode'}</span></div>
      <div class="chat" id="chat" style="flex:1;background:var(--card);border-radius:14px;padding:12px;min-height:300px">
        ${state.chat.length ? state.chat.slice(-40).map(m => `<div class="bubble ${m.role}"><div class="who">${m.role === 'user' ? 'You' : 'AETHER'}</div>${esc(m.content)}</div>`).join('') : '<div class="empty">Ask anything: "what should I eat tonight to hit protein?", "I skipped the gym, help me reset", "plan my week".</div>'}
        ${chatBusy ? '<div class="bubble assistant"><span class="spinner"></span></div>' : ''}
      </div>
      <div class="row" style="margin-top:10px"><input type="text" class="grow" id="chat-in" placeholder="Talk to AETHER…"><button class="btn" data-act="chat-send" ${chatBusy ? 'disabled' : ''}>Send</button><button class="btn ghost tiny" data-act="chat-clear" title="Clear chat">Clear</button></div>
    </div>
  </div>`;
};

// ---------------------------------------------------------------- settings
pages.settings = () => {
  const S = state.settings, T = state.targets, P = state.profile;
  return `
  <h1>Settings</h1><p class="sub">Everything is stored on this laptop. Nothing leaves it except the AI requests and wallpaper downloads you turn on.</p>
  <div class="grid g2">
    <div class="card">
      <h2>AI (food estimates + coach)</h2>
      <div class="stack">
        <label class="field">Coach
          <select id="s-provider">${[['auto', 'AETHER (runs on this PC, nothing leaves it)'], ['off', 'Off (offline table + built-in coach)']].map(([v, l]) => `<option value="${v}" ${S.aiProvider === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
        <div class="small muted" id="ai-detail">${aiStatusText()}</div>
        <button class="btn soft" data-act="ai-refresh">Re-check AI</button>
      </div>
    </div>
    <div class="card">
      <h2>Wallpaper</h2>
      <div class="stack">
        <label class="field">Style
          <select id="s-wall">${[['mix', 'Mix: some days black "do the work", most days scenery'], ['dark', 'Always black minimal'], ['scenery', 'Always scenery']].map(([v, l]) => `<option value="${v}" ${S.wallpaperMode === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
        <label class="toggle"><input type="checkbox" id="s-autodesk" ${S.autoDesktop ? 'checked' : ''}> Set it as my desktop wallpaper automatically each day</label>
        <div class="small muted">Scenery comes from Wikimedia Commons "Featured pictures" (free, no key needed).</div>
      </div>
    </div>
    <div class="card">
      <h2>Targets</h2>
      <div class="grid g2">
        ${[['kcal', 'Calories / day'], ['protein', 'Protein g / day'], ['pages', 'Pages / day'], ['waterL', 'Water L / day'], ['sleepH', 'Sleep hours'], ['workoutsPerWeek', 'Workouts / week'], ['weeklySpend', 'Spend limit ₹ / week']].map(([k, l]) => `<label class="field">${l}<input type="number" step="any" data-target="${k}" value="${T[k]}"></label>`).join('')}
      </div>
    </div>
    <div class="card">
      <h2>Profile & challenge</h2>
      <div class="grid g2">
        <label class="field">Name<input type="text" data-profile="name" value="${esc(P.name)}"></label>
        <label class="field">Challenge start (Day 1)<input type="date" data-profile="startDate" value="${P.startDate}"></label>
        <label class="field">Height cm<input type="number" data-profile="heightCm" value="${P.heightCm}"></label>
        <label class="field">Start weight kg<input type="number" step="0.1" data-profile="startWeight" value="${P.startWeight}"></label>
        <label class="field">Target weight kg<input type="number" step="0.1" data-profile="targetWeight" value="${P.targetWeight}"></label>
        <label class="field">Theme<select id="s-theme"><option value="pastel" ${S.theme === 'pastel' ? 'selected' : ''}>Pastel (light)</option><option value="dusk" ${S.theme === 'dusk' ? 'selected' : ''}>Dusk (soft dark)</option></select></label>
      </div>
    </div>
    <div class="card">
      <h2>Daily checklist</h2>
      ${state.habits.map(h => `<div class="list-item"><span>${h.emoji}</span><span style="flex:1">${esc(h.label)} ${h.auto ? '<span class="tag">auto</span>' : ''}</span>${['workout', 'protein', 'kcal', 'read', 'sleep', 'water', 'journal'].includes(h.id) ? '' : `<button class="btn ghost tiny" data-act="habit-del" data-id="${h.id}">✕</button>`}</div>`).join('')}
      <div class="row" style="margin-top:10px"><input type="text" class="w80" id="h-emoji" placeholder="🙂" maxlength="4"><input type="text" class="grow" id="h-label" placeholder="New habit, e.g. 10 min stretching"><button class="btn" data-act="habit-add">Add</button></div>
    </div>
    <div class="card">
      <h2>Your data</h2>
      <p class="small muted">Saved automatically to a file in your user folder, with a daily backup copy (last 30 days kept).</p>
      <div class="row"><button class="btn soft" data-act="data-open">Open data folder</button><button class="btn soft" data-act="data-export">Export backup</button><button class="btn ghost" data-act="data-import">Import backup</button></div>
    </div>
  </div>`;
};

function aiProviderLabel() {
  return state.settings.aiProvider !== 'off' && aiInfo.aether ? 'AETHER' : '';
}
function aiStatusText() {
  return [aiInfo.aether ? '✅ AETHER\'s local model is ready' : '◻️ AETHER\'s model is not loaded (offline table + built-in coach)', '✅ Offline food table always available'].join(' · ');
}

function dateNav() {
  return `<div class="row"><button class="btn ghost" data-act="date-prev">‹</button><input type="date" id="date-pick" value="${viewDate}" max="${ymd()}"><button class="btn ghost" data-act="date-next" ${viewDate >= ymd() ? 'disabled' : ''}>›</button>${viewDate !== ymd() ? '<button class="btn tiny soft" data-act="date-today">Today</button>' : ''}</div>`;
}

// ================================================================ rendering + events
function render() {
  document.documentElement.dataset.theme = state.settings.theme === 'dusk' ? 'dusk' : 'pastel';
  const main = $('#main');
  const scroll = main.scrollTop;
  main.innerHTML = pages[page]();
  main.scrollTop = scroll;
  $$('#nav button').forEach(b => b.classList.toggle('active', b.dataset.page === page));
  const c = challenge();
  $('#brand-sub').textContent = c.started ? `Block ${c.block} · Day ${c.dayInBlock}/75` : `Starts in ${c.daysUntil} days`;
  const s = streak();
  $('#streak').textContent = s ? `🔥 ${s}-day streak` : '🌱 Start a streak today';
  $('#ai-pill').textContent = aiProviderLabel() ? `AI: ${aiProviderLabel()} connected` : 'AI: offline mode';
  if (page === 'today') paintHero();
  if (page === 'journal') { const ch = $('#chat'); if (ch) ch.scrollTop = ch.scrollHeight; }
  if (page === 'settings') paintKeyStatus();
}

async function paintHero() {
  const today = ymd();
  const w = await loadWallpaper(today).catch(() => null);
  const hero = $('#hero');
  if (!hero || !w) return;
  // The black wallpaper is wide; in the shorter hero its words would collide with the
  // day counter, so the hero draws the motto as text instead of cropping the image.
  hero.classList.toggle('dark', w.kind === 'dark');
  hero.style.backgroundImage = w.kind === 'dark' ? '' : `url("${w.dataUrl}")`;
  const m = $('#hero-motto'); if (m) m.textContent = w.kind === 'dark' ? (w.motto || 'DO THE WORK.') : '';
  const cr = $('#wall-credit');
  if (cr) { cr.textContent = w.credit || ''; cr.onclick = () => w.link && window.api.openExternal(w.link); }
}

async function paintKeyStatus() {}

async function refreshAI() {
  aiInfo = await window.api.aiStatus(state.settings).catch(() => aiInfo);
}

function go(p) { page = p; if (p === 'today') viewDate = ymd(); foodPreview = foodPreview && p === 'food' ? foodPreview : null; render(); $('#main').scrollTop = 0; }

const actions = {
  // navigation & dates
  'date-prev': () => { viewDate = addDays(viewDate, -1); render(); },
  'date-next': () => { if (viewDate < ymd()) { viewDate = addDays(viewDate, 1); render(); } },
  'date-today': () => { viewDate = ymd(); render(); },
  'goto-date': el => { viewDate = el.dataset.d; render(); },

  // today
  check: el => {
    const h = state.habits.find(x => x.id === el.dataset.id); if (!h) return;
    if (h.auto) { toast('This one ticks itself from your logs.'); return; }
    const d = day(ymd()); d.checks[h.id] = !habitDone(h, ymd());
    if (h.id === 'workout' && !d.checks.workout && d.workout) d.workout.done = false;
    save(); render();
    if (dayScore(ymd()).pct >= 1) toast('Perfect day. That is how it is done. 🔥');
  },
  water: el => { const d = day(ymd()); d.waterL = clamp(round((+d.waterL || 0) + +el.dataset.v, 2), 0, 10); save(); render(); },
  'spend-add': () => {
    const amt = +$('#spend-amt').value, what = $('#spend-what').value.trim();
    if (!amt) return toast('Enter an amount');
    const d = day(ymd()); d.spend.push({ amt, what: what || 'misc' }); d.checks.spend = true; save(); render();
  },
  'spend-del': el => { day(ymd()).spend.splice(+el.dataset.i, 1); save(); render(); },
  'quick-shake': () => { addFood([{ ...foodFrom('mass shake (homemade)'), quantity: '1 glass', meal: 'Post-workout shake', src: 'offline table' }], ymd()); toast('Shake logged: +45 g protein'); render(); },
  'wall-swap': async () => {
    const today = ymd(); const cur = wallKindFor(today);
    state.walls[today] = { ...(state.walls[today] || {}), kind: cur === 'dark' ? 'scenery' : 'dark', motto: pick(window.MOTTOS, today + Date.now()), desktopSet: false };
    delete wallCache[today]; save(); paintHero();
  },
  'wall-desktop': async () => {
    const w = await loadWallpaper(ymd());
    const r = await window.api.setDesktop(w.file);
    if (r.ok) { state.walls[ymd()] = { ...(state.walls[ymd()] || {}), desktopSet: true }; save(); toast('Desktop wallpaper set ✨'); }
    else toast('Could not set wallpaper: ' + r.error, 5000);
  },

  // food
  'food-fav': el => { const ta = $('#food-text'); ta.value = (ta.value.trim() ? ta.value.trim() + ', ' : '') + el.dataset.t; ta.focus(); },
  'food-estimate': () => estimate(true),
  'food-offline': () => estimate(false),
  'prev-del': el => { foodPreview.items.splice(+el.dataset.i, 1); $('#food-preview').innerHTML = foodPreviewHtml(); },
  'food-add': () => {
    syncPreviewEdits();
    const meal = $('#food-meal').value;
    const remember = $('#remember-food')?.checked;
    addFood(foodPreview.items.map(x => ({ ...x, meal, src: foodPreview.source })), viewDate);
    if (remember) rememberFoods(foodPreview.items);
    foodPreview = null; $('#food-text').value = ''; render(); toast('Logged ✓');
  },
  'food-del': el => { day(viewDate).food.splice(+el.dataset.i, 1); save(); render(); },

  // body
  'b-weight': () => { const v = +$('#b-weight').value; if (!v) return; day(viewDate).weight = v; save(); render(); toast('Weight saved'); },
  'w-save': () => {
    const d = day(viewDate);
    d.workout = { done: true, type: $('#w-type').value, notes: $('#w-notes').value.trim() };
    d.checks.workout = true; save(); render(); toast('Session logged. Now eat. 🍛');
  },
  'w-undo': () => { const d = day(viewDate); if (d.workout) d.workout.done = false; d.checks.workout = false; save(); render(); },

  // reading
  'book-add': () => {
    const title = $('#bk-title').value.trim(); if (!title) return toast('Add a title');
    state.books.push({ id: uid(), title, author: $('#bk-author').value.trim(), totalPages: +$('#bk-pages').value || 0, pagesRead: 0, status: state.books.some(b => b.status === 'reading') ? 'queue' : 'reading' });
    save(); render();
  },
  'book-start': el => { const b = state.books.find(x => x.id === el.dataset.id); b.status = 'reading'; save(); render(); },
  'book-del': el => { state.books = state.books.filter(x => x.id !== el.dataset.id); save(); render(); },
  'read-log': el => {
    const b = state.books.find(x => x.id === el.dataset.id);
    const n = +$(`input[data-book="${b.id}"]`).value; if (!n) return toast('How many pages?');
    day(viewDate).reading.push({ bookId: b.id, pages: n });
    b.pagesRead += n;
    if (b.totalPages && b.pagesRead >= b.totalPages) { b.status = 'done'; b.finished = viewDate; toast(`Finished "${b.title}"! 🎉`, 4000); }
    else toast(`+${n} pages`);
    save(); render();
  },

  // goals
  'goal-accept': el => {
    const s = suggestions().filter(x => !state.goals.some(g => g.title === x.title && !g.done))[+el.dataset.i];
    const c = challenge();
    state.goals.push({ id: uid(), title: s.title, metric: s.metric, target: s.target, deadline: s.metric === 'weight' && c.started ? c.blockEnd : addDays(ymd(), 7 - ((new Date().getDay() + 6) % 7) - 1), createdAt: ymd(), done: false });
    save(); render(); toast('Goal accepted. Make it happen.');
  },
  'goal-add': () => {
    const t = $('#goal-title').value.trim(); if (!t) return;
    state.goals.push({ id: uid(), title: t, deadline: $('#goal-date').value || null, createdAt: ymd(), done: false }); save(); render();
  },
  'goal-done': el => { const g = state.goals.find(x => x.id === el.dataset.id); g.done = true; g.doneAt = ymd(); save(); render(); toast('Goal crushed 🏆'); },
  'goal-del': el => { state.goals = state.goals.filter(x => x.id !== el.dataset.id); save(); render(); },
  'ai-plan': async () => {
    if (!aiProviderLabel()) return toast('AETHER\'s model is not loaded yet.', 4000);
    aiPlan = { loading: true }; render();
    const r = await window.api.chat({ context: aiContext(), effort: 'high', messages: [{ role: 'user', content: 'Look at my data and give me a concrete plan for the next 7 days: daily food structure that hits my targets with vegetarian Indian home food (with rough grams), training focus per day, reading target, and 3 slightly tough but realistic goals for the week. Use short headings and bullets.' }] }, state.settings);
    aiPlan = { text: r.ok ? r.text : `Could not reach the AI: ${r.error || r.reason}` }; render();
  },

  // journal
  mood: el => { day(viewDate).mood = el.dataset.m; save(); render(); },
  prompt: el => { const ta = $('#journal'); ta.value = (ta.value.trim() ? ta.value.trimEnd() + '\n\n' : '') + el.dataset.p + '\n'; ta.focus(); day(viewDate).journal = ta.value; save(); },
  'j-reflect': () => {
    const text = $('#journal').value.trim(); if (!text) return toast('Write something first');
    sendChat(`Here is my journal entry for ${prettyDate(viewDate)}. Reflect on it briefly, point out one pattern, and give me one specific action for tomorrow:\n\n${text}`);
  },
  'chat-send': () => { const v = $('#chat-in').value.trim(); if (v) sendChat(v); },
  'chat-clear': () => { state.chat = []; save(); render(); },

  // settings
  'key-save': async el => {
    const v = $('#' + el.dataset.in).value.trim(); if (!v) return toast('Paste the key first');
    await window.api.setSecret(el.dataset.k, v); $('#' + el.dataset.in).value = '';
    await refreshAI(); render(); toast('Key saved on this laptop');
  },
  'key-clear': async el => { await window.api.setSecret(el.dataset.k, ''); await refreshAI(); render(); toast('Key removed'); },
  'ai-refresh': async () => { await refreshAI(); render(); toast(aiProviderLabel() ? `Using ${aiProviderLabel()}` : 'No AI found, offline mode'); },
  'habit-add': () => {
    const label = $('#h-label').value.trim(); if (!label) return;
    state.habits.push({ id: uid(), label, emoji: $('#h-emoji').value.trim() || '⭐', auto: false }); save(); render();
  },
  'habit-del': el => { state.habits = state.habits.filter(h => h.id !== el.dataset.id); save(); render(); },
  'data-open': () => window.api.openDataFolder(),
  'data-export': async () => { const p = await window.api.exportData(state); if (p) toast('Exported to ' + p, 4000); },
  'data-import': async () => {
    try {
      const d = await window.api.importData(); if (!d) return;
      if (!d.days || !d.profile) return toast('That file does not look like a Grind 75 backup');
      state = merge(DEFAULT_STATE(), d); save(); render(); toast('Backup imported');
    } catch (e) { toast('Import failed: ' + e.message); }
  },
};

function foodFrom(name) {
  const f = allFoods().find(x => x.n === name);
  return { name: f.n, kcal: f.kcal, protein: f.p, carbs: f.c, fat: f.f };
}
function addFood(items, date) {
  const d = day(date);
  for (const x of items) d.food.push({ name: x.name, quantity: x.quantity || '', kcal: +x.kcal || 0, protein: +x.protein || 0, carbs: +x.carbs || 0, fat: +x.fat || 0, meal: x.meal || 'Other', src: x.src || '' });
  save();
}
function rememberFoods(items) {
  // Save AI estimates as single-serving custom foods so the same text works offline next time.
  for (const x of items) {
    const key = (x.quantity || x.name).toLowerCase().trim();
    if (!key) continue;
    state.customFoods = state.customFoods.filter(f => f.n !== key);
    state.customFoods.unshift({ n: key, a: [x.name.toLowerCase()], unit: 'piece', grams: null, kcal: +x.kcal, p: +x.protein, c: +x.carbs, f: +x.fat, custom: true });
  }
  state.customFoods = state.customFoods.slice(0, 300);
  save();
}
function syncPreviewEdits() {
  $$('#food-preview tr[data-i]').forEach(tr => {
    const it = foodPreview.items[+tr.dataset.i];
    $$('input', tr).forEach(inp => { it[inp.dataset.f] = inp.dataset.f === 'name' ? inp.value : +inp.value || 0; });
  });
}

async function estimate(useAI) {
  const text = $('#food-text').value.trim();
  if (!text) return toast('Type what you ate first');
  const local = parseFoodLocal(text);
  if (useAI && aiProviderLabel()) {
    foodPreview = { loading: true }; $('#food-preview').innerHTML = foodPreviewHtml();
    const r = await window.api.estimateFood(text, state.settings);
    if (r.ok) { foodPreview = { items: r.items, source: 'AETHER estimate' }; }
    else {
      foodPreview = { items: local.items, source: 'offline table', note: `AI unavailable (${r.error || r.reason}), used the offline table instead.` + (local.unknown.length ? ` Not recognised: ${local.unknown.join(', ')}.` : '') };
    }
  } else {
    foodPreview = { items: local.items, source: 'offline table', note: local.unknown.length ? `Not recognised: ${local.unknown.join(', ')}. Try simpler words, or let AETHER estimate it.` : '' };
  }
  $('#food-preview').innerHTML = foodPreviewHtml();
}

async function sendChat(text) {
  if (chatBusy) return;
  state.chat.push({ role: 'user', content: text, ts: Date.now() });
  chatBusy = true; save(); render();
  let reply;
  if (aiProviderLabel()) {
    const history = state.chat.slice(-16).map(m => ({ role: m.role, content: m.content }));
    const r = await window.api.chat({ context: aiContext(), messages: history }, state.settings);
    reply = r.ok ? r.text : `(I could not reach the AI: ${r.error || r.reason}. Here is what your logs say.)\n\n` + offlineReply();
  } else reply = offlineReply();
  state.chat.push({ role: 'assistant', content: reply, ts: Date.now() });
  chatBusy = false; save(); render();
}
function offlineReply() {
  const tips = insights().map(s => '• ' + s.replace(/<[^>]+>/g, ''));
  const q = pick(window.QUOTES, String(Date.now()));
  return `${tips.slice(0, 4).join('\n')}\n\n“${q[0]}” (${q[1]})\n\nLoad AETHER's model and I can talk this through properly.`;
}

// one delegated listener for clicks
document.addEventListener('click', e => {
  const nav = e.target.closest('#nav button'); if (nav) return go(nav.dataset.page);
  const g = e.target.closest('[data-go]'); if (g) return go(g.dataset.go);
  const a = e.target.closest('[data-act]'); if (a && actions[a.dataset.act]) actions[a.dataset.act](a);
});

// inputs that save on change
document.addEventListener('change', async e => {
  const el = e.target;
  if (el.id === 'date-pick' && el.value) { viewDate = el.value > ymd() ? ymd() : el.value; render(); return; }
  if (el.id === 'q-weight') { day(ymd()).weight = +el.value || null; save(); render(); return; }
  if (el.id === 'q-sleep') { day(ymd()).sleepH = +el.value || null; save(); render(); return; }
  if (el.dataset.target) { state.targets[el.dataset.target] = +el.value || 0; save(); return; }
  if (el.dataset.profile) { const k = el.dataset.profile; state.profile[k] = ['name', 'startDate'].includes(k) ? el.value : +el.value; wallCache = {}; save(); render(); return; }
  const S = state.settings;
  if (el.id === 's-provider') { S.aiProvider = el.value; save(); render(); }
  if (el.id === 's-model') { S.claudeModel = el.value; save(); }
  if (el.id === 's-ollama') { S.ollamaHost = el.value.trim() || 'http://localhost:11434'; save(); await refreshAI(); render(); }
  if (el.id === 's-ollama-model') { S.ollamaModel = el.value; save(); }
  if (el.id === 's-wall') { S.wallpaperMode = el.value; delete state.walls[ymd()]; wallCache = {}; save(); }
  if (el.id === 's-autodesk') { S.autoDesktop = el.checked; save(); if (el.checked) maybeAutoDesktop(); }
  if (el.id === 's-login') { S.openAtLogin = el.checked; save(); window.api.setOpenAtLogin?.(el.checked); }
  if (el.id === 's-theme') { S.theme = el.value; save(); render(); }
});

// journal autosave + enter-to-send
document.addEventListener('input', e => {
  if (e.target.id === 'journal') {
    day(viewDate).journal = e.target.value; save();
    const s = $('#j-saved'); if (s) s.textContent = 'Saved ✓';
  }
});
document.addEventListener('keydown', e => {
  if (e.target.id === 'chat-in' && e.key === 'Enter') { e.preventDefault(); actions['chat-send'](); }
  if (e.target.id === 'food-text' && e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); estimate(true); }
});

// ================================================================ boot
(async function boot() {
  const saved = await window.api.load();
  state = merge(DEFAULT_STATE(), saved);
  if (!saved) save();
  render();
  await refreshAI();
  render();
  maybeAutoDesktop().catch(() => {});
  // roll over at midnight if the app stays open
  let lastDay = ymd();
  setInterval(() => {
    if (ymd() !== lastDay) { lastDay = ymd(); viewDate = ymd(); wallCache = {}; render(); maybeAutoDesktop().catch(() => {}); }
  }, 60000);
})();
