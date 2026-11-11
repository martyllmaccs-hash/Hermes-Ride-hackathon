// P4: the orb's motion physics on synthetic rides (static/orb_motion.js). Prints {checks, failures}.
// The "bike" frame is x forward, y left, z up. The phone is mounted with its screen facing the rider, tilted back
// by `tilt` degrees (0 = upright); samples are generated in the bike frame and turned into the phone frame.
// A motorcycle is not a car: in a coordinated turn the accelerometer keeps reading along the BIKE's vertical axis,
// only stronger (g / cos(lean)), so lean cannot come from the accelerometer. It comes from the gyro: the roll rate
// about forward, held by the turn rate (sin(lean) = v * yaw_rate_about_bike_down / g). These tests pin that.
'use strict';
const path = require('path');
const {RideOrbMotion, RIDE_ORB_MOTION_KEYS, rideOrbMotionGain} = require(path.join(__dirname, '..', 'static', 'orb_motion.js'));

let checks = 0;
const failures = [];
function eq(name, got, want) { checks++; if (JSON.stringify(got) !== JSON.stringify(want)) failures.push({name, got, want}); }
function ok(name, cond, info) { checks++; if (!cond) failures.push({name, got: info}); }
const G = 9.81, RAD = Math.PI / 180;

function mount(tilt) {                     // bike frame -> phone frame (x screen right, y screen top, z out of the screen)
  const b = tilt * RAD, xp = [0, -1, 0], yp = [Math.sin(b), 0, Math.cos(b)], zp = [-Math.cos(b), 0, Math.sin(b)];
  const dot = (a, v) => a[0] * v[0] + a[1] * v[1] + a[2] * v[2];
  return v => [dot(xp, v), dot(yp, v), dot(zp, v)];
}
// one ride: fn(s) -> {f: specific force in g (bike frame; at rest [0,0,1]), rot: deg/s (bike frame), spd: m/s|null|undefined}
// 60 Hz samples, one frame() every 50 ms. o.spec: the W3C sign (the vector points up at rest) instead of the iOS one.
function ride(m, secs, fn, o) {
  o = Object.assign({tilt: 30, hz: 60, t0: 0, split: false}, o || {});
  const P = mount(o.tilt), frames = [], n = Math.round(secs * o.hz);
  let cur = o.cur || null, nextFrame = o.t0, lastSpd = -Infinity;
  for (let i = 1; i <= n; i++) {
    const s = i / o.hz, t = o.t0 + s * 1000, x = fn(s);
    const f = P(x.f || [0, 0, 1]).map(v => v * G * (o.spec ? 1 : -1));       // iOS: at rest the vector points down
    const sample = o.split ? {g: f.map(v => v * 0.9), la: f.map(v => v * 0.1)} : {g: f, la: [0, 0, 0]};
    sample.rot = x.rot === null ? null : P(x.rot || [0, 0, 0]);
    if (x.spd !== undefined && t - lastSpd >= 1000) { m.setSpeed(x.spd, t); lastSpd = t; }     // GPS: 1 Hz
    m.feed(sample, t);
    if (t >= nextFrame) { nextFrame += 50; const v = m.frame(t); frames.push(v); if (v) cur = v; }
  }
  return {frames, cur, writes: frames.filter(Boolean).length, t1: o.t0 + secs * 1000};
}
const rest = () => ({});
// a coordinated turn at `lean` degrees (+ right) and v m/s: roll in over 1 s, hold. What the phone really senses.
function turn(lean, v, o) {
  o = Object.assign({rollS: 1, spd: v}, o || {});
  return s => {
    const th = lean * Math.min(1, s / o.rollS), om = G * Math.tan(th * RAD) / v / RAD;       // world yaw rate, deg/s
    return {f: [0, 0, 1 / Math.cos(th * RAD)],
            rot: [s < o.rollS ? lean / o.rollS : 0, -om * Math.sin(th * RAD), -om * Math.cos(th * RAD)], spd: o.spd};
  };
}
const peak = (frames, k) => frames.filter(Boolean).reduce((a, v) => Math.abs(v[k] - (k === 'sx' || k === 'sy' ? 1 : 0)) > Math.abs(a) ? v[k] - (k === 'sx' || k === 'sy' ? 1 : 0) : a, 0);

// ---- 1. the shape of a frame, and rest is silent
{
  const m = RideOrbMotion();
  eq('keys: the custom properties a frame carries', RIDE_ORB_MOTION_KEYS, ['mx', 'my', 'sx', 'sy', 'mk', 'mr', 'ma']);
  eq('no samples: no frame', m.frame(0), null);
  const r = ride(m, 6, rest);
  ok('rest: at most one frame in 6 s (the deadband: no style write when nothing moves)', r.writes <= 1, r.writes);
  ok('rest: neutral values', !r.cur || (Math.abs(r.cur.mx) < 0.3 && Math.abs(r.cur.my) < 0.3 && Math.abs(r.cur.sx - 1) < 0.01 && Math.abs(r.cur.mr) < 1), r.cur);
  const s = m.state();
  ok('rest: lean, lon, load about 0', Math.abs(s.lean) < 0.5 && Math.abs(s.lon) < 0.02 && Math.abs(s.load) < 0.02, s);
}
// ---- 2. a steady coordinated turn: accelerometer along bike-down at g/cos(lean), yaw about down, speed known
for (const [lean, v] of [[30, 20], [-30, 20], [20, 12], [-40, 28]]) {
  const m = RideOrbMotion();
  ride(m, 3, rest, {});
  const r = ride(m, 6, turn(lean, v), {t0: 3000});
  const s = m.state();
  ok(`turn ${lean} deg at ${v} m/s: lean has the right sign and rough size (got ${s.lean.toFixed(1)})`, Math.abs(s.lean - lean) <= 6, s);
  ok(`turn ${lean}: the orb's light counter-rotates (mr about ${-lean})`, r.cur && Math.abs(r.cur.mr + lean) <= 7, r.cur);
  ok(`turn ${lean}: the body shears with it (mk sign of lean)`, r.cur && Math.sign(r.cur.mk) === Math.sign(lean) && Math.abs(r.cur.mk) <= 18, r.cur);   // round 3: shear 0.30 deg per deg, clamp 18 (was 0.18, 10)
  ok(`turn ${lean}: no sideways throw (there is no lateral G on a leaned bike)`, r.cur && Math.abs(r.cur.mx) < 1, r.cur);
  ok(`turn ${lean}: the load (|a| above 1 g) presses the orb: flatter, wider`, s.load > 0.04 && r.cur.sy < 1 && r.cur.sx > 1, {load: s.load, cur: r.cur});
}
{   // the turn holds for a long corner (the turn rate keeps it; a leaky gyro integral alone would fade)
  const m = RideOrbMotion();
  ride(m, 2, rest);
  ride(m, 25, turn(30, 20), {t0: 2000});
  ok('long corner (25 s): lean still about 30', Math.abs(m.state().lean - 30) <= 6, m.state());
  const r = ride(m, 5, s => ({f: [0, 0, 1], rot: [s < 1 ? -30 : 0, 0, 0], spd: 20}), {t0: 27000});
  ok('and upright again after the corner', Math.abs(m.state().lean) < 4 && Math.abs(r.cur.mr) < 5, m.state());
}
// ---- 3. a pure accelerometer roll (gravity turns in the screen plane, the gyro says nothing): not a lean
{
  const m = RideOrbMotion();
  ride(m, 3, rest);
  const a = 30 * RAD;
  const r = ride(m, 8, () => ({f: [0, Math.sin(a), Math.cos(a)], rot: [0, 0, 0], spd: 20}), {t0: 3000});
  ok('accel-only roll of 30 deg, no rotation rate: no lean', Math.abs(m.state().lean) < 3, m.state());
  ok('accel-only roll: the light does not rotate as a lean', peak(r.frames, 'mr') > -3 && peak(r.frames, 'mr') < 3, peak(r.frames, 'mr'));
}
{   // a steady lateral G with no rotation (a car's corner): also not a lean
  const m = RideOrbMotion();
  ride(m, 3, rest);
  ride(m, 6, () => ({f: [0, 0.5, 1], rot: [0, 0, 0], spd: 20}), {t0: 3000});
  ok('lateral 0.5 g without rotation: no lean', Math.abs(m.state().lean) < 3, m.state());
}
// ---- 4. no speed (GPS off): the roll rate alone, leaking back to upright
{
  const m = RideOrbMotion();
  ride(m, 2, rest);
  const r = ride(m, 1.5, turn(30, 20, {spd: undefined}), {t0: 2000});
  ok('no GPS: the roll-in shows at once (gyro)', m.state().lean > 20 && r.cur.mr < -15, m.state());
  ride(m, 40, s => ({f: [0, 0, 1 / Math.cos(30 * RAD)], rot: [0, 0, 0]}), {t0: 3500, cur: r.cur});
  ok('no GPS: it leaks back to upright (drift never sticks)', Math.abs(m.state().lean) < 3, m.state());
  const m2 = RideOrbMotion();
  ride(m2, 2, rest);
  ride(m2, 4, turn(30, 20, {spd: null}), {t0: 2000});
  ok('speed null (no fix): same fallback, no crash', Number.isFinite(m2.state().lean) && m2.state().lean > 5, m2.state());
  const m3 = RideOrbMotion();
  ride(m3, 2, rest);
  ride(m3, 3, s => Object.assign(turn(30, 20)(s), {rot: null}), {t0: 2000});
  eq('no gyro at all: upright', m3.state().lean, 0);
}
{   // a stale speed stops holding the lean: after 3 s without a fix the turn rate is not trusted
  const m = RideOrbMotion();
  m.setSpeed(20, 0);
  ride(m, 2, rest);
  ride(m, 30, s => ({f: [0, 0, 1 / Math.cos(30 * RAD)], rot: turn(30, 20)(s).rot}), {t0: 2000});
  ok('stale speed: the lean leaks away', Math.abs(m.state().lean) < 6, m.state());
}
// ---- 5. braking squashes and pushes the orb forward (up the screen); accelerating stretches and pulls it back
{
  const m = RideOrbMotion();
  ride(m, 3, rest);
  const r = ride(m, 2, () => ({f: [-0.5, 0, 1]}), {t0: 3000});
  ok('brake 0.5 g: lon about -0.5', Math.abs(m.state().lon + 0.5) < 0.12, m.state());
  ok('brake: squashed (sy < 1, sx > 1)', r.cur.sy < 0.96 && r.cur.sx > 1.02, r.cur);
  ok('brake: pushed up the screen (forward)', r.cur.my < -1.5 && Math.abs(r.cur.mx) < 0.5, r.cur);
  ok('brake: 20 Hz at most', r.writes <= 41, r.writes);
  const back = ride(m, 3, rest, {t0: 5000});
  // round 3: the 30 s baseline moved 0.03 g during the 2 s brake, which at full scale 0.35 g leaves my 2.65 % and sy 1.036 for a
  // while (was my < 0.6, sy within 0.02 at full scale 0.7 g)
  ok('brake released: back near neutral (my within 3 %, sy within 0.05)', Math.abs(back.cur.sy - 1) < 0.05 && Math.abs(back.cur.my) < 3, back.cur);
  const m2 = RideOrbMotion();
  ride(m2, 3, rest);
  const a = ride(m2, 2, () => ({f: [0.4, 0, 1]}), {t0: 3000});
  ok('accelerate 0.4 g: stretched (sy > 1, sx < 1), pulled back (down the screen)', a.cur.sy > 1.03 && a.cur.sx < 0.99 && a.cur.my > 1.2, a.cur);
  // an upright phone (forward is into the screen) and a flat one (forward is the top edge) behave the same way
  for (const tilt of [0, 85]) {
    const q = RideOrbMotion();
    ride(q, 3, rest, {tilt});
    const b = ride(q, 2, () => ({f: [-0.5, 0, 1]}), {tilt, t0: 3000});
    ok(`brake with the phone tilted back ${tilt} deg: squashed and pushed up`, b.cur.sy < 0.96 && b.cur.my < -1.5, b.cur);
  }
}
// ---- 6. a bump kicks a wobble that settles in under a second
{
  const m = RideOrbMotion();
  ride(m, 3, rest);
  const hit = ride(m, 0.4, s => ({f: [0, 0, s < 0.07 ? 2 : 1]}), {t0: 3000});
  ok('bump (1 g for 70 ms): the orb moves', Math.abs(peak(hit.frames, 'my')) > 1.5 && Math.abs(peak(hit.frames, 'sy')) > 0.02, [peak(hit.frames, 'my'), peak(hit.frames, 'sy')]);
  const tail = ride(m, 0.6, rest, {t0: 3400});
  const late = ride(m, 1.5, rest, {t0: 4000, cur: tail.cur || hit.cur});
  ok('bump: settled 1 s after the hit (no more frames)', late.writes === 0, late.writes);
  ok('bump: settles at neutral', Math.abs(late.cur.my) < 0.5 && Math.abs(late.cur.sy - 1) < 0.01, late.cur);
  const dirs = hit.frames.filter(Boolean).map(v => Math.sign(Math.round(v.my)));
  ok('bump: it wobbles (down, then back up past neutral)', dirs.includes(1) && [...hit.frames, ...tail.frames].filter(Boolean).some(v => v.my < -0.3), dirs);
}
{   // engine vibration (0.1 g at 25 Hz) is not a bump
  const m = RideOrbMotion();
  ride(m, 3, rest);
  const r = ride(m, 4, s => ({f: [0, 0, 1 + 0.1 * Math.sin(2 * Math.PI * 25 * s)]}), {t0: 3000, hz: 100});
  ok('vibration 0.1 g at 25 Hz: hardly a frame, hardly a move', r.writes <= 4 && Math.abs(peak(r.frames, 'my')) < 0.6, [r.writes, peak(r.frames, 'my')]);
}
// ---- 7. the gain setting, and the clamps
{
  eq('gain: default 1, clamp 0 to 2, junk 1', [rideOrbMotionGain({}), rideOrbMotionGain({orb_motion: 0}), rideOrbMotionGain({orb_motion: 5}), rideOrbMotionGain({orb_motion: -1}),
     rideOrbMotionGain({orb_motion: 'x'}), rideOrbMotionGain(null), rideOrbMotionGain({orb_motion: '1.5'})], [1, 0, 2, 0, 1, 1, 1.5]);
  eq('gain: prefers-reduced-motion means 0', [rideOrbMotionGain({orb_motion: 2}, true), rideOrbMotionGain({}, true)], [0, 0]);
  const off = RideOrbMotion({gain: 0});
  const r0 = ride(off, 3, turn(30, 20));
  eq('gain 0: never a frame', [r0.writes, off.frame(99999)], [0, null]);
  const brake = () => ({f: [-0.3, 0, 1]});
  const size = g => { const m = RideOrbMotion({gain: g}); ride(m, 3, rest); return ride(m, 2, brake, {t0: 3000}).cur; };
  const a = size(1), b = size(2), c = size(0.5);
  ok('gain 2 moves more than 1, 0.5 less', b.my < a.my && a.my < c.my && b.sy < a.sy && a.sy < c.sy, [c, a, b]);
  const m = RideOrbMotion({gain: 2});
  ride(m, 2, rest);
  const wild = ride(m, 3, s => ({f: [5 * Math.sin(9 * s), 4 * Math.cos(7 * s), 1 + 6 * Math.sin(40 * s)], rot: [900 * Math.sin(3 * s), 500, -700], spd: 60}), {t0: 2000});
  const bad = wild.frames.filter(Boolean).filter(v => !(RIDE_ORB_MOTION_KEYS.every(k => Number.isFinite(v[k])) && Math.abs(v.mx) <= 16 && Math.abs(v.my) <= 24 && v.sx >= 0.65 && v.sx <= 1.35 &&
    v.sy >= 0.65 && v.sy <= 1.35 && Math.abs(v.mk) <= 18 && Math.abs(v.mr) <= 45));   // round 3 clamps (were 10, 10, 0.8-1.2, 10)
  ok('absurd input at gain 2: every value finite and inside the clamps', wild.writes > 0 && bad.length === 0, bad[0]);
  m.setGain(0);
  eq('setGain(0) later: silent', ride(m, 1, brake, {t0: 5000}).writes, 0);
}
// ---- 8. conventions and robustness
{
  const run = o => { const m = RideOrbMotion(o.opts); ride(m, 3, rest, o); const r = ride(m, 2, s => (s < 1 ? {f: [-0.5, 0, 1]} : turn(25, 20)(s - 1)), Object.assign({t0: 3000}, o)); return r.cur; };
  const ios = run({});
  const near = (a, b) => !!a && !!b && RIDE_ORB_MOTION_KEYS.every(k => Math.abs(a[k] - b[k]) <= (k === 'sx' || k === 'sy' ? 0.01 : 0.6));
  ok('the iOS run itself: leaning right after the brake', ios && ios.mr < -15, ios);
  ok('W3C sign convention (vector up at rest, gdown: false): the same orb', near(run({spec: true, opts: {gdown: false}}), ios), ios);
  ok('gravity and linear acceleration split by the OS: the same orb (only their sum is used)', near(run({split: true}), ios), ios);
  // the telemetry calibration's forward axis is used when it agrees with the mount (its sign is not trusted)
  const P = mount(30);
  const withCal = fwd => { const m = RideOrbMotion(); m.setCal({down: P([0, 0, -1]), forward: P(fwd), sign: 1}); ride(m, 3, rest); return ride(m, 2, () => ({f: [-0.5, 0, 1]}), {t0: 3000}).cur; };
  const c1 = withCal([1, 0, 0]), c2 = withCal([-1, 0, 0]), c3 = withCal([0, 1, 0]);
  ok('cal.forward (either sign) gives the same brake as the mount guess', c1.sy < 0.96 && c1.my < -1.5 && JSON.stringify(c1) === JSON.stringify(c2), [c1, c2]);
  ok('a cal.forward far from the mount axis (sideways) is ignored', c3.sy < 0.96 && c3.my < -1.5, c3);
  const m = RideOrbMotion();
  m.setCal(null); m.setCal({down: [0, 0, 0], forward: null});
  ride(m, 1, rest);
  ok('junk calibration: no crash', m.frame(1001) === null || typeof m.frame(1001) === 'object', 0);
}
{   // landscape (device turned 90 deg counter-clockwise: its +x is up, tilted back 30): fed in the phone frame
  const b = 30 * RAD, up = [Math.cos(b), 0, Math.sin(b)], fwd = [Math.sin(b), 0, -Math.cos(b)];
  const feed = (m, secs, t0, lonG) => { let cur = null;
    for (let i = 1; i <= secs * 60; i++) { const t = t0 + i * 1000 / 60;
      m.feed({g: up.map(v => -G * v), la: fwd.map(v => -G * lonG * v), rot: [0, 0, 0]}, t);
      if (i % 3 === 0) cur = m.frame(t) || cur; }
    return cur; };
  const m = RideOrbMotion({screenAngle: 90});        // the page rotated with the screen
  feed(m, 3, 0, 0);
  const br = feed(m, 2, 3000, -0.5);
  ok('landscape (screen angle 90): brake squashes and pushes up the rotated screen', br && br.sy < 0.96 && br.my < -1.5 && Math.abs(br.mx) < 0.6 && Math.abs(br.ma) < 2, br);
  const p = RideOrbMotion();                         // the same pose, but the page did not rotate (orientation lock)
  feed(p, 3, 0, 0);
  const lk = feed(p, 2, 3000, -0.5);
  ok('landscape with a locked page: the orb\'s up axis turns 90 deg (ma), the brake push follows it (mx)', lk && Math.abs(Math.abs(lk.ma) - 90) < 3 && Math.abs(lk.mx) > 1.5 && Math.abs(lk.my) < 0.6, lk);
}
{   // gaps and junk samples
  const m = RideOrbMotion();
  ride(m, 2, rest);
  m.feed(null, 2100); m.feed({}, 2110); m.feed({g: [NaN, 0, 0], la: [0, 0, 0]}, 2120);
  const r = ride(m, 1, () => ({f: [-0.4, 0, 1]}), {t0: 60000});       // a 58 s gap (the page was in the background)
  ok('junk samples and a long gap: finite, sane', r.cur && RIDE_ORB_MOTION_KEYS.every(k => Number.isFinite(r.cur[k])) && r.cur.sy < 1, r.cur);
  m.reset();
  eq('reset: no frame until samples come', m.frame(61000), null);
}
// ---- 9. round 3 (rider feedback: "make it move more"): real rides peak near 0.2 g braking and 0.3-0.4 g accelerating, so the
// full-scale input is 0.35 g (was 0.7) and the shift / squash ranges are about 2.5x. Measured on these synthetic rides
// (new code; old code in brackets): 0.3 g brake my -12.57 % (-2.39), sx/sy 1.454 (1.088); 0.3 g accelerate my +12.57 %, sx/sy 0.745
// (0.935); 0.2 g bump for 150 ms peak my 1.92 % (0.86). L saturates at 0.35 g, so a longitudinal push alone tops out at 16 %.
{
  const steady = lonG => { const m = RideOrbMotion(); ride(m, 3, rest); return ride(m, 1.5, () => ({f: [lonG, 0, 1]}), {t0: 3000}).cur; };
  const b = steady(-0.3), a = steady(0.3);
  ok(`steady 0.3 g brake: pushed up at least 12 % of the diameter (got ${b && b.my})`, b && b.my <= -12, b);
  ok(`steady 0.3 g brake: squash sx/sy at least 1.35 (got ${b && (b.sx / b.sy).toFixed(3)})`, b && b.sx / b.sy >= 1.35, b);
  ok(`steady 0.3 g accelerate: pulled back at least 12 % (got ${a && a.my})`, a && a.my >= 12, a);
  ok(`steady 0.3 g accelerate: stretch sx/sy at most 0.78 (got ${a && (a.sx / a.sy).toFixed(3)})`, a && a.sx / a.sy <= 0.78, a);
  const m = RideOrbMotion();
  ride(m, 3, rest);
  const hit = ride(m, 0.6, s => ({f: [0, 0, s < 0.15 ? 1.2 : 1]}), {t0: 3000});
  ok(`bump of 0.2 g for 150 ms: peak shift at least 1.5 % (got ${peak(hit.frames, 'my')})`, Math.abs(peak(hit.frames, 'my')) >= 1.5, peak(hit.frames, 'my'));
  const late = ride(m, 1.5, rest, {t0: 4600, cur: ride(m, 1, rest, {t0: 3600, cur: hit.cur}).cur});
  ok('the 0.2 g bump: settled 1.6 s after the hit (no more frames)', late.writes === 0, late.writes);
  // the clamps at 1 g and gain 2: shift 16 / 24 %, scale 0.65 to 1.35, shear 18 deg
  const inside = v => RIDE_ORB_MOTION_KEYS.every(k => Number.isFinite(v[k])) && Math.abs(v.mx) <= 16 && Math.abs(v.my) <= 24 && v.sx >= 0.65 && v.sx <= 1.35 &&
    v.sy >= 0.65 && v.sy <= 1.35 && Math.abs(v.mk) <= 18 && Math.abs(v.mr) <= 45;
  for (const f of [[-1, 0, 1], [1, 0, 1], [0, 1, 1], [0, 0, 2]]) {
    const q = RideOrbMotion({gain: 2});
    ride(q, 3, rest);
    const r = ride(q, 2, s => ({f: s < 1 ? f : [0, 0, 1], rot: [0, 0, 0]}), {t0: 3000});
    const bad = r.frames.filter(Boolean).filter(v => !inside(v));
    ok(`1 g along [${f}] at gain 2: every frame inside the clamps (mx 16, my 24, scale 0.65-1.35, mk 18)`, r.writes > 0 && bad.length === 0, bad[0]);
  }
  const hard = RideOrbMotion({gain: 2});
  ride(hard, 3, rest);
  const hb = ride(hard, 1.5, () => ({f: [-1, 0, 1]}), {t0: 3000}).cur;
  ok(`1 g brake at gain 2 goes past the old range (my beyond -10: got ${hb && hb.my}; sy below 0.8: got ${hb && hb.sy})`, hb && hb.my < -10 && hb.my >= -24 && hb.sy < 0.8 && hb.sy >= 0.65, hb);
  // the deadband and gain 0 are untouched
  const still = RideOrbMotion();
  ride(still, 3, rest);
  eq('at rest: nothing written after the first 3 s (deadband)', ride(still, 5, rest, {t0: 3000}).writes, 0);
  const off = RideOrbMotion({gain: 0});
  ride(off, 3, rest);
  eq('gain 0: a 0.3 g brake and a bump write nothing', ride(off, 3, s => ({f: [-0.3, 0, s < 0.2 ? 1.5 : 1]}), {t0: 3000}).writes, 0);
}
console.log(JSON.stringify({checks, failures}));
