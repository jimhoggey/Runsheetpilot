/* Dotted 3D thinking-orb loaders — vanilla-JS port of two states from
 * "thinking-orbs" by Jakub Antalík (github.com/Jakubantalik/thinking-orbs,
 * MIT licence). Ported because the original ships as a React component and
 * this app is framework-free vanilla JS.
 *
 *   working — particles on tilted orbits   (shown while building in PP)
 *   solving — bands scramble, click solved (shown while the AI parses)
 *
 * Faithful to the original engine: honestly 3D (rotated, depth-shaded,
 * z-sorted), plain 2D canvas arcs only — no filters, no WebGL — so it
 * renders identically in every browser AND inside the pywebview window.
 * The size-64 preset numbers below are the shipped tunings, pre-resolved
 * (count/radius multipliers already applied) so the render loop sees
 * plain constants.
 */
(function () {
  'use strict';

  function hashD(a, b) {
    const h = Math.sin(a * 12.9898 + b * 78.233) * 43758.5453;
    return h - Math.floor(h);
  }

  function makeProj(yaw, tilt, cx, cy, scale) {
    const st = Math.sin(tilt), ct = Math.cos(tilt);
    const sy = Math.sin(yaw), cyw = Math.cos(yaw);
    return function (x, y, z) {
      const x1 = x * cyw + z * sy;
      const z1 = -x * sy + z * cyw;
      const y1 = y * ct - z1 * st;
      const z2 = y * st + z1 * ct;
      return [cx + x1 * scale, cy - y1 * scale, z2];
    };
  }

  // z-sort far->near; ink value mirrored because this app is dark-themed,
  // so near dots read bright (the original's dark-substrate behaviour).
  function paint(ctx, dots, rMin) {
    dots.sort(function (a, b) { return a.z - b.z; });
    for (let i = 0; i < dots.length; i++) {
      const d = dots[i];
      const alpha = d.a === undefined ? 1 : d.a;
      if (alpha < 0.02) continue;
      const w = Math.min(1, Math.max(0, d.white));
      const g = Math.round((1 - w) * 255);
      ctx.fillStyle = 'rgba(' + g + ',' + g + ',' + g + ',' + alpha + ')';
      ctx.beginPath();
      ctx.arc(d.x, d.y, Math.max(rMin, d.r), 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function radiusScale(size, pow) { return Math.pow(size / 300, pow); }

  // ── working: particles on tilted orbits (base profile, count=1 size=1) ──
  function drawOrbits(ctx, size, t) {
    const cx = size / 2, cy = size / 2, R = (size / 2) * 0.82;
    const pt = makeProj(t * 0.12, 0.3, cx, cy, 1);
    const rs = radiusScale(size, 0.6);
    const dots = [];
    for (let orb = 0; orb < 12; orb++) {
      const h1 = hashD(orb, 1.7), h2 = hashD(orb, 5.2), h3 = hashD(orb, 8.9);
      const ro = R * (0.45 + 0.52 * h1);
      const th = h1 * 2 * Math.PI;
      const phi = Math.acos(2 * h2 - 1);
      const nx = Math.sin(phi) * Math.cos(th);
      const ny = Math.cos(phi);
      const nz = Math.sin(phi) * Math.sin(th);
      let ux = -ny, uy = nx;
      const uz = 0;
      const ul = Math.max(1e-6, Math.sqrt(ux * ux + uy * uy));
      ux /= ul; uy /= ul;
      const vx = ny * uz - nz * uy;
      const vy = nz * ux - nx * uz;
      const vz = nx * uy - ny * ux;
      const speed = (0.25 + 0.55 * h3) * (h3 > 0.5 ? 1 : -1);
      for (let k = 0; k < 40; k++) {                    // ghost path
        const a = (k / 40) * 2 * Math.PI;
        const p = pt((ux * Math.cos(a) + vx * Math.sin(a)) * ro,
                     (uy * Math.cos(a) + vy * Math.sin(a)) * ro,
                     (uz * Math.cos(a) + vz * Math.sin(a)) * ro);
        const depth = (p[2] / ro + 1) / 2;
        dots.push({ x: p[0], y: p[1], z: p[2], r: 0.9 * rs, white: 0.72,
                    a: 0.5 * (0.4 + 0.6 * depth) });
      }
      for (let m = 0; m < 3; m++) {                     // the particles
        const a = t * speed + (m / 3) * 2 * Math.PI + h2 * 6;
        const p = pt((ux * Math.cos(a) + vx * Math.sin(a)) * ro,
                     (uy * Math.cos(a) + vy * Math.sin(a)) * ro,
                     (uz * Math.cos(a) + vz * Math.sin(a)) * ro);
        const depth = (p[2] / ro + 1) / 2;
        dots.push({ x: p[0], y: p[1], z: p[2],
                    r: (1.2 + 1.6 * depth) * rs, white: 0.3 - 0.22 * depth });
      }
    }
    paint(ctx, dots, 0.3);
  }

  // ── solving: sphere bands scramble then click back solved ──────────────
  // Size-64 preset resolved: count 0.35 → latRings 9 / lonDensity 24;
  // size 1.05 → rBase .63 / rDepth 1.785 / rActive .315. moveCount stays 14.
  const MOVES = (function () {
    const moves = [];
    for (let i = 0; i < 14; i++) {
      const axis = Math.min(2, Math.floor(hashD(i, 2.3) * 3));
      const lo = -1.0 + 0.5 * Math.min(3, Math.floor(hashD(i, 5.9) * 4));
      const dir = hashD(i, 7.7) < 0.5 ? 1 : -1;
      moves.push({ axis: axis, lo: lo, hi: lo + 0.5, ang: dir * Math.PI / 2 });
    }
    return moves;
  })();

  function solveCycle(time, count, slotDur, rest) {
    const cyc = 2 * count * slotDur + rest;
    const tc = time % cyc;
    const amount = new Array(count).fill(0);
    let active = -1;
    if (tc < 2 * count * slotDur) {
      const slot = Math.floor(tc / slotDur);
      const p = (tc - slot * slotDur) / slotDur;
      const cl = Math.min(1, p / 0.7);
      const ep = 1 - Math.pow(1 - cl, 3);
      if (slot < count) {
        for (let i = 0; i < slot; i++) amount[i] = 1;
        amount[slot] = ep;
        active = slot;
      } else {
        const u = 2 * count - 1 - slot;
        for (let i = 0; i < u; i++) amount[i] = 1;
        amount[u] = 1 - ep;
        active = u;
      }
    }
    return { amount: amount, active: active };
  }

  function applyMoves(x, y, z, sc) {
    let inActive = false;
    for (let i = 0; i < MOVES.length; i++) {
      if (sc.amount[i] <= 0) continue;
      const mv = MOVES[i];
      const coord = mv.axis === 0 ? x : mv.axis === 1 ? y : z;
      if (coord < mv.lo || coord >= mv.hi) continue;
      if (i === sc.active) inActive = true;
      const a = mv.ang * sc.amount[i];
      const ca = Math.cos(a), sa = Math.sin(a);
      if (mv.axis === 0) { const y2 = y * ca - z * sa; z = y * sa + z * ca; y = y2; }
      else if (mv.axis === 1) { const x2 = x * ca + z * sa; z = -x * sa + z * ca; x = x2; }
      else { const x2 = x * ca - y * sa; y = x * sa + y * ca; x = x2; }
    }
    return [x, y, z, inActive];
  }

  function drawRubik(ctx, size, t) {
    const cx = size / 2, cy = size / 2, R = (size / 2) * 0.82;
    const pt = makeProj(t * 0.55, 0.35 + 0.1 * Math.sin(t * 0.9), cx, cy, R);
    const rs = radiusScale(size, 0.6);
    const sc = solveCycle(t, 14, 0.42, 1.2);
    const dots = [];
    for (let li = 0; li <= 9; li++) {
      const lat = -Math.PI / 2 + (li / 9) * Math.PI;
      const cosLat = Math.cos(lat), sinLat = Math.sin(lat);
      const lonCount = Math.max(1, Math.round(Math.abs(cosLat) * 24));
      for (let lj = 0; lj < lonCount; lj++) {
        const lon = (lj / lonCount) * 2 * Math.PI;
        const m = applyMoves(cosLat * Math.cos(lon), sinLat, cosLat * Math.sin(lon), sc);
        const p = pt(m[0], m[1], m[2]);
        const depth = (p[2] + 1) / 2;
        dots.push({ x: p[0], y: p[1], z: p[2],
                    r: (0.63 + 1.785 * depth + (m[3] ? 0.315 : 0)) * rs,
                    white: 0.62 - 0.54 * depth - (m[3] ? 0.14 : 0) });
      }
    }
    paint(ctx, dots, 0.3);
  }

  const STATES = {
    working: { draw: drawOrbits, speed: 1.885 },
    solving: { draw: drawRubik,  speed: 1.82 }
  };

  /** Mount an orb onto a <canvas>. Returns { stop() }. */
  function mount(canvas, state) {
    const cfg = STATES[state] || STATES.working;
    const size = 64;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(size * dpr);
    canvas.height = Math.round(size * dpr);
    canvas.style.width = size + 'px';
    canvas.style.height = size + 'px';
    const ctx = canvas.getContext('2d');
    let raf = 0, running = true;

    function frame(tSec) {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, size, size);
      cfg.draw(ctx, size, tSec);
    }

    // reduced-motion users get one static, deterministic frame
    if (window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches) {
      frame(0.6);
      return { stop: function () {} };
    }

    // Always paint one frame immediately (the original does the same) —
    // the loader must never appear as a blank square, even in contexts
    // where rAF is throttled or the document reports hidden.
    frame((performance.now() / 1000) * cfg.speed);

    function loop() {
      if (!running) return;
      if (!document.hidden) frame((performance.now() / 1000) * cfg.speed);
      raf = requestAnimationFrame(loop);
    }
    raf = requestAnimationFrame(loop);
    return { stop: function () { running = false; cancelAnimationFrame(raf); } };
  }

  window.Orb = { mount: mount };
})();
