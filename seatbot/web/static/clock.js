// Poll /api/status every 30s, update topbar clock nodes.
(function () {
  const nodes = {
    now: document.getElementById('now-time'),
    nextAt: document.getElementById('next-relay-time'),
    nextAcc: document.getElementById('next-relay-account'),
  };
  if (!nodes.now) return;  // not on a page with a topbar clock
  function fmt(d) {
    return d.toTimeString().slice(0, 5);  // HH:MM
  }
  async function tick() {
    try {
      const r = await fetch('/api/status', { cache: 'no-store' });
      if (!r.ok) return;
      const j = await r.json();
      const now = new Date(j.now);
      nodes.now.textContent = fmt(now);
      if (j.next_relay_at) {
        // server returns ISO; parse and format as HH:MM
        const t = new Date(j.next_relay_at);
        nodes.nextAt.textContent = fmt(t);
        nodes.nextAcc.textContent = j.next_relay_account_id || '-';
      } else {
        nodes.nextAt.textContent = '--:--';
        nodes.nextAcc.textContent = '夜间静默';
      }
    } catch (e) { /* keep last value */ }
  }
  tick();
  setInterval(tick, 30000);
})();