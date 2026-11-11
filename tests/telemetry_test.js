// W7: phone telemetry calibration on synthetic motion (static/telemetry.js). Prints {checks, failures}.
// The phone sits in an arbitrary orientation; the "bike" frame is x forward, y left, z up. Samples are generated
// in the bike frame and rotated into the phone frame; the calibration must recover down and forward (+ sign).
'use strict';
const path = require('path');
const {RideTelemetry, rideCalibrate, RideOutbox, V} = require(path.join(__dirname, '..', 'static', 'telemetry.js'));

let checks = 0;
const failures = [];
function eq(name, got, want) { checks++; if (JSON.stringify(got) !== JSON.stringify(want)) failures.push({name, got, want}); }
function ok(name, cond, info) { checks++; if (!cond) failures.push({name, got: info}); }

// rotation matrices
function rot(axis, deg) {
  const [x, y, z] = V.unit(axis), a = deg * Math.PI / 180, c = Math.cos(a), s = Math.sin(a), t = 1 - c;
  return [[t * x * x + c, t * x * y - s * z, t * x * z + s * y], [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
          [t * x * z - s * y, t * y * z + s * x, t * z * z + c]];
}
const mm = (A, B) => A.map((r, i) => [0, 1, 2].map(j => r[0] * B[0][j] + r[1] * B[1][j] + r[2] * B[2][j]));
const mv = (A, v) => A.map(r => r[0] * v[0] + r[1] * v[1] + r[2] * v[2]);
let seed = 7;
const rnd = () => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed / 0x7fffffff - 0.5; };

// a ride: 20 Hz motion, 1 Hz GPS, speed oscillating (accelerate / brake) along the bike's forward axis
function ride(opts) {
  const o = Object.assign({secs: 150, mount: mm(rot([0, 0, 1], 35), rot([1, 0.3, 0], 70)), leanDeg: () => 0,
                           yaw: () => 0, gps: true, accel: s => 1.5 * Math.sin(2 * Math.PI * s / 20)}, opts);
  const tel = RideTelemetry(o.tel);
  const toPhone = v => mv(o.mount, v);       // bike frame -> phone frame
  const recs = [];
  let spd = 15, t0 = 1700000000000;
  for (let s = 0; s < o.secs; s++) {
    const a = o.accel(s), mount = typeof o.mountAt === 'function' ? o.mountAt(s) : null;
    const to = mount ? (v => mv(mount, v)) : toPhone;
    const lean = o.leanDeg(s) * Math.PI / 180;   // + = right: gravity gains a +right (= -y) component
    const gB = [0, -9.81 * Math.sin(lean), -9.81 * Math.cos(lean)];
    for (let k = 0; k < 20; k++) {
      const laB = [a + 0.3 * rnd(), 0.4 * rnd(), 0.6 * rnd()];  // vibration: lateral and vertical noise
      const rotB = [0, 0, -o.yaw(s) * 180 / Math.PI];            // right turn = negative about up (z)
      tel.onMotion({g: to(gB), la: to(laB), rot: to(rotB)});
    }
    spd = Math.max(0, spd + a);
    if (o.gps) tel.onGps({t: t0 + s * 1000, lat: -36.7 + s * 1e-4, lon: 146.9, acc: 5, spd, hdg: 90});
    const r = tel.tick(t0 + s * 1000);
    if (r) recs.push(r);
  }
  return {tel, recs, toPhone};
}
const trueDown = m => mv(m, [0, 0, -1]), trueFwd = m => mv(m, [1, 0, 0]);

// 1. known down and forward recovered within 5 deg, sign chosen from GPS, lean ~0 upright
{
  const r = ride({});
  const cal = r.tel.calibration();
  const m = mm(rot([0, 0, 1], 35), rot([1, 0.3, 0], 70));
  ok('1 down within 5 deg', V.angle(cal.down, trueDown(m)) < 5, V.angle(cal.down, trueDown(m)));
  ok('1 forward within 5 deg (with sign)', V.angle(cal.forward, trueFwd(m)) < 5, V.angle(cal.forward, trueFwd(m)));
  eq('1 sign from GPS', cal.sign, 1);
  const last = r.recs[r.recs.length - 1];
  ok('1 upright lean ~0', Math.abs(last.lean) < 3, last.lean);
  ok('1 lon_g follows the acceleration', last.lon_g !== undefined, last);
  eq('1 6-decimal position', String(last.lat).split('.')[1].length <= 6, true);
}

// 2. GPS sign: the phone mounted backwards (180 deg about up) still gives the bike's forward
{
  const back = mm(rot([0, 0, 1], 180), rot([1, 0, 0], 20));
  const r = ride({mount: back});
  const cal = r.tel.calibration();
  ok('2 backwards mount: forward is still the bike forward', V.angle(cal.forward, trueFwd(back)) < 5, V.angle(cal.forward, trueFwd(back)));
  const r2 = ride({gps: false});
  eq('2 no GPS: sign unknown, no forward-axis values', [r2.tel.calibration().sign, 'lean' in r2.recs[r2.recs.length - 1]], [0, false]);
}

// 3. lean: 30 deg right after calibration reads +30 (within 3); left is negative
{
  const r = ride({secs: 170, leanDeg: s => (s >= 150 && s < 160 ? 30 : s >= 160 ? -25 : 0)});
  const at = s => r.recs[s].lean;
  ok('3 lean right +30', Math.abs(at(155) - 30) < 3, at(155));
  ok('3 lean left -25', Math.abs(at(165) + 25) < 3, at(165));
  eq('3 a long 30 deg corner is not a pocket shift', r.recs.filter(x => x.pocket_shift).length, 0);
  eq('3 lean > 40 deg event only above 40', r.recs.flatMap(x => x.ev || []).filter(e => e.ev === 'lean').length, 0);
}

// 4. lean_turn cross-check: v = speed, right turn yaw 0.2 rad/s -> atan(v * 0.2 / 9.81), positive = right
{
  const r = ride({secs: 140, accel: () => 0.0, yaw: s => (s >= 130 ? 0.2 : 0), tel: {min_fwd_records: 5}});
  const rec = r.recs[135];
  const want = Math.atan(15 * 0.2 / 9.81) * 180 / Math.PI;
  ok('4 lean_turn', rec.lean_turn !== undefined && Math.abs(rec.lean_turn - want) < 1, [rec.lean_turn, want]);
}

// 5. pocket shift: a 30 deg rotation of the phone fires the flag and restarts both windows
{
  const m = mm(rot([0, 0, 1], 35), rot([1, 0.3, 0], 70));
  const shifted = mm(rot(mv(m, [0.3, 1, 0.2]), 30), m);    // 30 deg, mostly pitch, in the phone's own coordinates
  const r = ride({secs: 200, mountAt: s => (s < 150 ? m : shifted)});
  const flagged = r.recs.filter(x => x.pocket_shift).map(x => Math.round((x.t - 1700000000000) / 1000));
  ok('5 flag fires within 6 s of the shift', flagged.length === 1 && flagged[0] >= 150 && flagged[0] <= 156, flagged);
  const evs = r.recs.flatMap(x => x.ev || []).filter(e => e.ev === 'pocket_shift');
  eq('5 one pocket_shift event', evs.length, 1);
  const cal = r.tel.calibration();
  ok('5 recalibrated to the new mount', V.angle(cal.down, trueDown(shifted)) < 5, V.angle(cal.down, trueDown(shifted)));
  const r2 = ride({secs: 200});
  eq('5 no shift, no flag', r2.recs.filter(x => x.pocket_shift).length, 0);
}

// 6. events: hard braking (< -0.6 G) fires once per episode; minute stats
{
  const r = ride({secs: 150, accel: s => (s >= 140 && s < 143 ? -7.5 : 1.5 * Math.sin(2 * Math.PI * s / 20))});
  const ev = r.recs.flatMap(x => x.ev || []);
  eq('6 one braking event', ev.filter(e => e.ev === 'braking').length, 1);
  ok('6 braking value < -0.6 G', ev.find(e => e.ev === 'braking').v < -0.6, ev);
  const st = r.tel.minuteStats(1700000150000);
  ok('6 minute stats', st.n === 150 && st.peak_brake_g < -0.6 && st.la_mean > 0 && st.la_sd >= 0 && st.dist_m > 0 &&
     Math.abs(st.la_mean_g - st.la_mean / 9.81) < 0.002, st);
}

// 7. pure calibrate on hand data + the outbox (idempotent re-send by t)
{
  const secs = Array.from({length: 30}, (_, i) => ({g: [0, 0, -9.8], la: [i % 2 ? 2 : -2, 0.1, 0], dv: i % 2 ? 2 : -2}));
  const c = rideCalibrate(secs, secs, 20);
  ok('7 axis-aligned case', V.angle(c.down, [0, 0, -1]) < 1 && V.angle(c.forward, [1, 0, 0]) < 1, c);
  const ob = RideOutbox();
  [1, 2, 3, 4].forEach(t => ob.add({t}));
  eq('7 batch', ob.batch(2).map(r => r.t), [1, 2]);
  ob.ack(2);
  eq('7 ack drops sent', [ob.size(), ob.batch().map(r => r.t)], [2, [3, 4]]);
}

console.log(JSON.stringify({checks, failures}));
