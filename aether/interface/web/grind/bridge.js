// Grind 75 runs inside AETHER: this replaces the desktop app's Electron bridge with calls to AETHER's local server.
// Everything stays on this PC. AETHER logs food, weight, sleep and workouts by voice into the same data, so the
// page picks up those changes every few seconds.
(function () {
  let rev = 0, dirty = false;
  const j = async (url, body) => {
    const r = await fetch(url, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  };
  window.api = {
    load: async () => { const s = await j('/api/grind/state'); rev = s._rev || 0; return s; },
    save: async state => { dirty = true; try { const r = await j('/api/grind/state', { state, rev }); rev = r.rev; } finally { dirty = false; } return true; },
    openDataFolder: () => j('/api/grind/open-folder', {}),
    exportData: async state => { const r = await j('/api/grind/export', { state }); return r.path; },
    importData: async () => { const r = await j('/api/grind/import-app', {}); return r.state || null; },
    secretsStatus: async () => ({}),
    setSecret: async () => true,
    aiStatus: () => j('/api/grind/ai'),
    estimateFood: text => j('/api/grind/food', { text }),
    chat: payload => j('/api/grind/chat', payload),
    scenery: date => j('/api/grind/scenery', { date }).then(r => r.ok ? r : null),
    saveGenerated: (date, kind, dataUrl) => j('/api/grind/wallpaper', { date, kind, dataUrl }).then(r => r.file),
    setDesktop: file => j('/api/grind/desktop', { file }),
    setOpenAtLogin: async () => true,
    openExternal: url => { if (/^https:\/\//.test(url)) window.open(url, '_blank', 'noopener'); },
  };
  // Pick up what AETHER logged by voice (or another open tab) while this page is open.
  setInterval(async () => {
    if (dirty || document.hidden) return;
    try {
      const r = await j('/api/grind/rev');
      if (r.rev > rev && typeof merge === 'function' && typeof render === 'function') {
        const s = await window.api.load();
        state = merge(DEFAULT_STATE(), s);   // the app's own globals (classic scripts share them)
        const a = document.activeElement;
        if (!a || !/INPUT|TEXTAREA|SELECT/.test(a.tagName)) render();
      }
    } catch (e) { /* server restarting */ }
  }, 4000);
})();
