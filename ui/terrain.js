// Dot terrain background: a perspective field of dots that drifts like slow
// swell and lifts (tinted amber) under the pointer. Same style as the
// ai-vance.de footer, recoloured for the white theme.
(function () {
  "use strict";
  const canvas = document.getElementById("terrain");
  if (!canvas || !canvas.getContext) return;
  const ctx = canvas.getContext("2d");

  const ROWS = 34;          // depth rows
  const RADIUS = 150;       // pointer influence radius (px)
  const INK = [17, 18, 22];       // --ink
  const AMBER = [201, 119, 31];   // --amber
  const reduced = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;

  let w = 0, h = 0, cols = 0, trail = null;
  let px = -1, py = -1, active = 0, raf = null;

  function resize() {
    const r = canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = Math.max(1, Math.round(r.width));
    h = Math.max(1, Math.round(r.height));
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    cols = Math.min(150, Math.max(28, Math.round(w / (w > 1700 ? 17 : 14))));
    trail = new Float32Array(cols * ROWS);
  }

  function wave(x, z, t) {
    return Math.sin(x * 2.3 + z * 1.9 - t * 32e-5) * 0.72
         + Math.sin(x * 1.1 - z * 3.1 + t * 19e-5) * 0.46
         + Math.sin(x * 5.6 + z * 0.8 + t * 44e-5) * 0.17;
  }

  function draw(t) {
    ctx.clearRect(0, 0, w, h);
    const top = h * 0.12, span = h - top;
    for (let row = 0; row < ROWS; row++) {
      const z = row / (ROWS - 1);
      const depth = z * z * 0.93 + z * 0.07;         // perspective spacing
      const baseY = top + span * depth;
      const spread = 0.52 + 1.06 * depth;            // rows widen toward the viewer
      const size = 0.45 + depth * 1.35;
      const alpha = 0.05 + depth * 0.3;
      for (let col = 0; col < cols; col++) {
        const u = col / (cols - 1) - 0.5;
        const x = w * 0.5 + u * w * spread;
        if (x < -8 || x > w + 8) continue;
        let y = baseY - wave(u * 2, z, t) * depth * h * 0.17;

        let lift = 0;
        if (active > 0) {
          const dx = x - px, dy = y - py, d = Math.sqrt(dx * dx + dy * dy);
          if (d < RADIUS) lift = (1 - d / RADIUS) * active;
        }
        const i = row * cols + col;
        let tr = trail[i] * 0.965;                    // lingering wake
        if (lift * 0.45 > tr) tr = lift * 0.45;
        trail[i] = tr;
        const v = Math.max(lift, tr);
        if (v > 0) y -= v * v * 20;

        const a = alpha + v * 0.5, r = size + v * 1.6;
        if (v > 0.02) {
          const k = Math.min(1, v * v * 2.4);
          ctx.fillStyle = "rgba(" + Math.round(INK[0] + (AMBER[0] - INK[0]) * k) + ","
            + Math.round(INK[1] + (AMBER[1] - INK[1]) * k) + ","
            + Math.round(INK[2] + (AMBER[2] - INK[2]) * k) + "," + a.toFixed(3) + ")";
        } else {
          ctx.fillStyle = "rgba(17,18,22," + a.toFixed(3) + ")";
        }
        if (r < 1.2) ctx.fillRect(x - r, y - r, r * 2, r * 2);
        else { ctx.beginPath(); ctx.arc(x, y, r, 0, 6.2832); ctx.fill(); }
      }
    }
  }

  function loop(t) { draw(t); raf = requestAnimationFrame(loop); }
  function start() { if (!reduced && raf === null && !document.hidden) raf = requestAnimationFrame(loop); }
  function stop() { if (raf !== null) { cancelAnimationFrame(raf); raf = null; } }

  resize();
  draw(0);
  addEventListener("resize", () => { resize(); if (reduced) draw(0); }, { passive: true });
  document.addEventListener("visibilitychange", () => (document.hidden ? stop() : start()));
  start();

  if (!reduced && matchMedia("(hover: hover) and (pointer: fine)").matches) {
    addEventListener("pointermove", (e) => {
      const r = canvas.getBoundingClientRect();
      const x = e.clientX - r.left, y = e.clientY - r.top;
      if (x < -RADIUS || x > r.width + RADIUS || y < -RADIUS || y > r.height + RADIUS) { active = 0; return; }
      px = x; py = y; active = 1;
    }, { passive: true });
    document.addEventListener("pointerleave", () => { active = 0; }, { passive: true });
  }
})();
