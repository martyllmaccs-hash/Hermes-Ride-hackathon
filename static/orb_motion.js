// P4: the orb moves and deforms with the motorcycle. Pure: the page feeds every DeviceMotion sample (feed), the GPS
// speed (setSpeed) and the telemetry calibration (setCal); orb.js asks for a frame() on its 20 Hz grid while the orb is
// awake and writes it as CSS custom properties on #orbm. frame() is null when nothing moved (deadband: no style write).
// No timer, no animation frame; tests/orb_motion_test.js runs synthetic rides through it.
//
// A motorcycle is not a car (10 Nov). In a turn the bike leans, and gravity + centripetal acceleration stay
// along the bike's own vertical axis: a mounted phone's accelerometer keeps reading "down", only stronger
// (|a| = g / cos(lean)). There is almost no lateral G and gravity does not roll in the screen plane, so the
// accelerometer cannot show lean. Here:
//   lean   the gyro. Roll rate about forward, integrated, and held by the turn rate while the GPS speed is fresh (3 s):
//          sin(lean) = v * yaw_rate_about_bike_down / g (telemetry.js's lean_turn idea; asin, not atan, because the
//          gyro axis is the leaned bike's down: it sees yaw * cos(lean)). Complementary filter, 1.2 s. Without a
//          speed the roll rate alone, leaking back to upright in about 8 s (gyro drift never sticks). No gyro: upright.
//   lon    braking / accelerating: the raw accelerometer along forward, a slow (30 s) baseline removed
//   bump   the accelerometer along down, high-passed (0.7 s), kicks a spring (3.6 Hz, damping 0.3: settled in < 1 s)
//   load   the accelerometer along down above 1 g (a turn, a dip): the orb is pressed flatter
//   sway   sideways accelerometer, high-passed: transients only (a pothole, a quick flick), same spring
// Axes (phone frame, x screen right, y screen top, z out of the screen):
//   up       cal.down from telemetry.js when it agrees with our own 30 s mean of the accelerometer, else that mean
//   forward  the mount guess: the screen faces the rider, so forward is the horizontal part of "into the screen" plus
//            "up the screen" (a flat phone: its top edge). cal.forward replaces the axis when it is within 45 deg of
//            the guess; its sign always comes from the guess (the sign of DeviceMotion's `acceleration` differs
//            between iOS and the W3C text, and the calibration's sign needs GPS).
// Only the SUM gravity + linear acceleration is used (the raw accelerometer), never the OS's split. gdown: true is
// iOS Safari's sign (the vector points down at rest, what telemetry.js calls gravity); false is the W3C sign.
// Output, at gain 1 (gain 0 to 2 scales it, the clamps stay), in the orb's own units.
// Round 3 (rider feedback, 10 Nov: "make it move more"): real rides peak near 0.2 g braking and 0.3-0.4 g accelerating, so the
// full-scale inputs are 0.35 g (lon_full_g, load_full_g; were 0.7 and 0.5) and the ranges about 2.5x the first ones:
// a steady 0.3 g brake now shifts the orb 12.6 % of its diameter with sx/sy 1.45 (was 2.4 % and 1.09).
//   mx, my  shift in % of the orb's diameter (CSS axes, y down): mx within 16, my within 24 (brake or accelerate alone
//           tops out at 16; load and a bump add the rest)
//   sx, sy  scale across / along the orb's up axis, 0.65 to 1.35 (brake: squash, pushed up the screen = forward;
//           accelerate: stretch, pulled back; bump: wobble; load: pressed)
//   mk      shear in degrees, within 18 (the top of the jelly stays behind in a lean, and in a sideways kick)
//   mr      rotation of the orb's light in degrees, within 45: MINUS the lean. The orb counter-leans like a spirit
//           level: the screen already leans with the bike, so a light that stays under the real sky shows the lean,
//           and it is what a floating, self-levelling thing would do. (Leaning further with the bike has no
//           reference on a round orb and would double the screen's own lean.)
//   ma      the orb's up axis on the screen in degrees (0 on a portrait mount; 90 for a landscape mount on a page that
//           did not rotate); the mount angle, not a tilt
'use strict';
const RIDE_ORB_MOTION_KEYS = ['mx', 'my', 'sx', 'sy', 'mk', 'mr', 'ma'];
const RIDE_ORB_MOTION_EPS = {mx: 0.15, my: 0.15, sx: 0.004, sy: 0.004, mk: 0.2, mr: 0.5, ma: 1.5};   // the deadband
const RIDE_ORB_MOTION_DEFAULTS = {
  gain: 1, gdown: true, screenAngle: 0,
  up_s: 30, lon_base_s: 30, lon_s: 0.15, lon_hz: 1.8, lon_damp: 0.55, lon_full_g: 0.35,
  lean_hold_s: 1.2, lean_leak_s: 8, yaw_s: 0.3, speed_fresh_ms: 3000, lean_max: 45,
  hp_s: 0.7, dead_g: 0.04, spring_hz: 3.6, spring_damp: 0.3, load_s: 0.5, load_dead_g: 0.03, load_full_g: 0.35,
};

function rideOrbMotionGain(cfg, reducedMotion) {     // hello.cfg.orb_motion -> 0 to 2 (default 1); reduced motion: 0
  if (reducedMotion) return 0;
  const raw = cfg && cfg.orb_motion, n = raw === null || raw === undefined || raw === '' ? 1 : Number(raw);
  return Number.isFinite(n) ? Math.max(0, Math.min(2, n)) : 1;
}

function RideOrbMotion(opts) {
  const o = Object.assign({}, RIDE_ORB_MOTION_DEFAULTS, opts || {});
  const G = 9.81, RAD = Math.PI / 180;
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const unit = a => { const n = Math.hypot(a[0], a[1], a[2]); return n > 1e-9 ? [a[0] / n, a[1] / n, a[2] / n] : null; };
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const flat = (v, up) => { const k = dot(v, up); return [v[0] - k * up[0], v[1] - k * up[1], v[2] - k * up[2]]; };   // horizontal part
  const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
  const dead = (x, d) => Math.abs(x) <= d ? 0 : x - Math.sign(x) * d;
  const vec = v => Array.isArray(v) && v.length === 3 && v.every(Number.isFinite);
  let s, cal = null, speed = null, speedT = -Infinity, last;

  function reset() {
    s = {n: 0, t: null, upL: null, lean: 0, yaw: 0, lonBase: 0, lonLP: 0, lon: 0, lonV: 0, vBase: 1, bz: 0, bv: 0,
         latBase: 0, lx: 0, lv: 0, load: 0, ma: 0};
    last = {mx: 0, my: 0, sx: 1, sy: 1, mk: 0, mr: 0, ma: 0};
  }
  reset();

  function axes() {              // -> {up, fwd, right} in the phone frame, from the slow mean (and the calibration)
    let up = unit(s.upL);
    if (!up) return null;
    if (cal && vec(cal.down)) {
      let c = unit(cal.down);
      if (c) { if (dot(c, up) < 0) c = c.map(v => -v); if (dot(c, up) > 0.9) up = c; }    // within ~25 deg: trust the 60 s one
    }
    let guess = unit(flat([up[0], up[1], -1], up));
    if (!guess || Math.hypot(...flat([up[0], up[1], -1], up)) < 0.05) guess = unit(flat([0, 1, 0], up)) || [0, 1, 0];
    let fwd = guess;
    if (cal && vec(cal.forward)) {
      const c = unit(flat(cal.forward, up)), d = c ? dot(c, guess) : 0;
      if (Math.abs(d) > 0.7) fwd = d < 0 ? c.map(v => -v) : c;
    }
    return {up, fwd, right: cross(fwd, up)};
  }
  function spring(pos, vel, target, hz, damp, dt) {      // semi-implicit Euler, stable for the dt we allow (<= 50 ms)
    const w = 2 * Math.PI * hz;
    vel += (w * w * (target - pos) - 2 * damp * w * vel) * dt;
    return [pos + vel * dt, vel];
  }

  function feed(m, t) {          // {g:[3], la:[3] m/s2, rot:[3] deg/s about x, y, z | null}, t ms: every motion sample
    if (!o.gain || !m || !vec(m.g)) return;
    const la = vec(m.la) ? m.la : [0, 0, 0], k = (o.gdown ? -1 : 1) / G;
    const f = [(m.g[0] + la[0]) * k, (m.g[1] + la[1]) * k, (m.g[2] + la[2]) * k];       // specific force in g: up at rest
    const gap = s.t === null ? Infinity : t - s.t;
    const dt = gap > 0 && gap <= 500 ? Math.min(gap, 50) / 1000 : 0;                   // a gap: hold, don't integrate
    s.t = t; s.n++;
    if (!s.upL) s.upL = f.slice();
    // the first second is a plain mean (a quick start), then the slow one
    const ku = Math.max(dt / o.up_s, s.n <= 60 ? 1 / s.n : 0);
    for (let i = 0; i < 3; i++) s.upL[i] += (f[i] - s.upL[i]) * Math.min(1, ku);
    const ax = axes();
    if (!ax || !dt) return;
    const fv = dot(f, ax.up), fl = dot(f, ax.fwd), fr = dot(f, ax.right);
    // lean: roll rate about forward, held by the turn rate (speed fresh) or leaking to upright
    if (vec(m.rot)) {
      const roll = dot(m.rot, ax.fwd), yaw = -dot(m.rot, ax.up);                       // deg/s; a right turn is + about down
      s.yaw += (yaw - s.yaw) * Math.min(1, dt / o.yaw_s);
      const fresh = speed !== null && t - speedT <= o.speed_fresh_ms;
      const ref = fresh ? Math.asin(clamp(speed * s.yaw * RAD / G, -0.94, 0.94)) / RAD : 0;
      s.lean = clamp(s.lean + roll * dt + (ref - s.lean) * Math.min(1, dt / (fresh ? o.lean_hold_s : o.lean_leak_s)), -70, 70);
    } else { s.lean = 0; s.yaw = 0; }
    // braking / accelerating: a jelly follows it (a slightly under-damped spring)
    s.lonBase += (fl - s.lonBase) * dt / o.lon_base_s;
    s.lonLP += (fl - s.lonBase - s.lonLP) * Math.min(1, dt / o.lon_s);
    [s.lon, s.lonV] = spring(s.lon, s.lonV, s.lonLP, o.lon_hz, o.lon_damp, dt);
    // load and bumps along down; sway sideways
    s.load += (fv - 1 - s.load) * Math.min(1, dt / o.load_s);
    s.vBase += (fv - s.vBase) * Math.min(1, dt / o.hp_s);
    [s.bz, s.bv] = spring(s.bz, s.bv, dead(fv - s.vBase, o.dead_g), o.spring_hz, o.spring_damp, dt);
    s.latBase += (fr - s.latBase) * Math.min(1, dt / o.hp_s);
    [s.lx, s.lv] = spring(s.lx, s.lv, dead(fr - s.latBase, o.dead_g), o.spring_hz, o.spring_damp, dt);
    s.bz = clamp(s.bz, -3, 3); s.lx = clamp(s.lx, -3, 3); s.lon = clamp(s.lon, -3, 3);
    // the orb's up axis on the screen (the mount angle); a flat phone keeps the last one
    const a = o.screenAngle * RAD, sr = ax.up[0] * Math.cos(a) - ax.up[1] * Math.sin(a), su = ax.up[0] * Math.sin(a) + ax.up[1] * Math.cos(a);
    if (Math.hypot(sr, su) > 0.3) s.ma = Math.atan2(sr, su) / RAD;
  }

  function frame() {             // -> {mx, my, sx, sy, mk, mr, ma}, or null when nothing moved past the deadband
    if (!o.gain || !s.n) return null;
    const g = o.gain;
    const L = clamp(s.lon * g / o.lon_full_g, -1, 1), D = clamp(dead(s.load, o.load_dead_g) * g / o.load_full_g, 0, 1);
    const z = clamp(s.bz * g, -1.2, 1.2), x = clamp(s.lx * g, -1.2, 1.2), th = clamp(s.lean, -o.lean_max, o.lean_max);
    const lx = -10 * x, ly = 16 * L + 5 * D + 10 * z, a = s.ma * RAD;      // along the orb's own axes, then onto the screen
    const v = {
      mx: clamp(lx * Math.cos(a) - ly * Math.sin(a), -16, 16), my: clamp(lx * Math.sin(a) + ly * Math.cos(a), -24, 24),
      sx: clamp(1 - (L < 0 ? 0.20 : 0.16) * L + 0.10 * D + 0.16 * z, 0.65, 1.35),
      sy: clamp(1 + (L < 0 ? 0.26 : 0.22) * L - 0.10 * D - 0.20 * z, 0.65, 1.35),
      mk: clamp(0.30 * th * g + 8 * x, -18, 18), mr: clamp(-th * Math.min(1, g), -45, 45), ma: s.ma,
    };
    if (!RIDE_ORB_MOTION_KEYS.every(k => Number.isFinite(v[k]))) return null;
    if (!RIDE_ORB_MOTION_KEYS.some(k => Math.abs(v[k] - last[k]) > RIDE_ORB_MOTION_EPS[k])) return null;
    for (const k of RIDE_ORB_MOTION_KEYS) v[k] = +v[k].toFixed(k === 'sx' || k === 'sy' ? 3 : k === 'mx' || k === 'my' ? 2 : 1);
    last = v;
    return Object.assign({}, v);
  }

  return {
    feed, frame, reset,
    setSpeed(mps, t) { speed = Number.isFinite(mps) && mps >= 0 ? mps : null; speedT = t; },   // GPS, about 1 Hz
    setCal(c) { cal = c || null; },                  // telemetry.js calibration(): {down, forward, sign} or null
    setGain(g) { o.gain = Number.isFinite(g) ? Math.max(0, Math.min(2, g)) : 1; },
    setScreenAngle(deg) { o.screenAngle = Number.isFinite(deg) ? deg : 0; },
    gain: () => o.gain,
    state: () => ({lean: s.lean, lon: s.lonLP, load: s.load, bump: s.bz, sway: s.lx, ma: s.ma, n: s.n}),
  };
}
if (typeof module === 'object' && module.exports) module.exports = {RideOrbMotion, rideOrbMotionGain, RIDE_ORB_MOTION_KEYS, RIDE_ORB_MOTION_EPS, RIDE_ORB_MOTION_DEFAULTS};
