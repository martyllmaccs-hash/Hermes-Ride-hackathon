// Telemetry on the phone (W7): devicemotion at its native rate + GPS -> one record per second, rolling orientation
// calibration (the rider's method), lean, per-minute stats and threshold events. Pure: the page feeds samples and
// ships records; tests/telemetry_test.js checks the calibration on synthetic data.
//
// Calibration (phone frame):
//   down     normalised mean gravity over a rolling 60 s window
//   forward  principal axis (2x2 covariance, larger eigenvector) of the per-second linear acceleration projected
//            on the horizontal plane, rolling 120 s; sign from GPS: positive where the projection correlates with
//            d(speed)/dt > 0 (no GPS -> sign unknown, forward-axis values are not reported)
//   pocket shift  the 5 s mean gravity is > 25 deg from `down` -> flag, restart both windows. Once forward is
//            known, a rotation about forward (a long corner at 30 deg+) is a lean, not a shift: then only > 15 deg of
//            pitch or > 65 deg in total counts
// Lean = rotation of gravity about forward, relative to down (+ = right). Cross-check only (not fused):
// lean_turn = atan(v * yaw_rate / g) from GPS speed and the gyro rate about down. In a steady coordinated turn an
// accelerometer-only gravity tilts with the bike and lean reads low; lean_turn doesn't have that failure.
'use strict';
const RIDE_G = 9.81;
const RIDE_TEL_DEFAULTS = {down_s: 60, fwd_s: 120, shift_deg: 25, shift_s: 5, shift_pitch_deg: 15, shift_max_lean_deg: 65, brake_g: -0.6, accel_g: 0.4,
                           lean_deg: 40, stopped_s: 120, gps_lost_s: 30, min_fwd_records: 20};

const V = {
  add: (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]], sub: (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]],
  mul: (a, k) => [a[0] * k, a[1] * k, a[2] * k], dot: (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2],
  cross: (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]],
  norm: a => Math.hypot(a[0], a[1], a[2]),
  unit: a => { const n = Math.hypot(a[0], a[1], a[2]) || 1; return [a[0] / n, a[1] / n, a[2] / n]; },
  angle: (a, b) => Math.acos(Math.max(-1, Math.min(1, V.dot(V.unit(a), V.unit(b))))) * 180 / Math.PI,
};
const DEG = 180 / Math.PI;
const r1 = x => Math.round(x * 10) / 10, r2 = x => Math.round(x * 100) / 100, r3 = x => Math.round(x * 1000) / 1000;

function rideHaversine(a, b) {
  const R = 6371000, p = Math.PI / 180, dLat = (b.lat - a.lat) * p, dLon = (b.lon - a.lon) * p;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * p) * Math.cos(b.lat * p) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

// Pure calibration from per-second means: secs = [{g:[3], la:[3], dv: m/s per s | null}] (oldest first).
function rideCalibrate(secsDown, secsFwd, minFwd) {
  if (!secsDown.length) return null;
  const down = V.unit(secsDown.reduce((s, r) => V.add(s, r.g), [0, 0, 0]));
  const ref = Math.abs(down[0]) < 0.9 ? [1, 0, 0] : [0, 1, 0];
  const e1 = V.unit(V.cross(down, ref)), e2 = V.cross(down, e1);
  const out = {down, forward: null, sign: 0, n_fwd: secsFwd.length};
  if (secsFwd.length < (minFwd || 1)) return out;
  let sxx = 0, syy = 0, sxy = 0, mx = 0, my = 0;
  const pts = secsFwd.map(r => [V.dot(r.la, e1), V.dot(r.la, e2)]);
  pts.forEach(p => { mx += p[0]; my += p[1]; });
  mx /= pts.length; my /= pts.length;
  pts.forEach(p => { const x = p[0] - mx, y = p[1] - my; sxx += x * x; syy += y * y; sxy += x * y; });
  const th = 0.5 * Math.atan2(2 * sxy, sxx - syy);           // angle of the larger eigenvector
  let fwd = V.add(V.mul(e1, Math.cos(th)), V.mul(e2, Math.sin(th)));
  let c = 0, n = 0;                                          // sign: projection vs d(speed)/dt from GPS
  secsFwd.forEach((r, i) => { if (r.dv != null && Number.isFinite(r.dv)) { c += (pts[i][0] * Math.cos(th) + pts[i][1] * Math.sin(th)) * r.dv; n++; } });
  if (n >= 3 && Math.abs(c) > 1e-9) { if (c < 0) fwd = V.mul(fwd, -1); out.sign = 1; }
  out.forward = fwd;
  return out;
}

function RideTelemetry(opts) {
  const o = Object.assign({}, RIDE_TEL_DEFAULTS, opts || {});
  let acc = null, gps = null, lastGps = null, prevSpd = null, secs = [], minute = [], cal = null;
  let shiftAt = -Infinity, events = [], armed = {}, stoppedSince = null, lastFixAt = null, lostFlag = false, startT = null;

  function resetAcc() { acc = {n: 0, g: [0, 0, 0], la: [0, 0, 0], rot: [0, 0, 0], peak: [0, 0, 0], rn: 0}; }
  resetAcc();
  function onMotion(s) {                   // {g:[3] gravity m/s2, la:[3] linear accel m/s2, rot:[3] deg/s (x,y,z)}
    if (!s || !s.g) return;
    acc.n++; acc.g = V.add(acc.g, s.g);
    if (s.la) { acc.la = V.add(acc.la, s.la); for (let i = 0; i < 3; i++) if (Math.abs(s.la[i]) > Math.abs(acc.peak[i])) acc.peak[i] = s.la[i]; }
    if (s.rot) { acc.rot = V.add(acc.rot, s.rot); acc.rn++; }
  }
  function onGps(f) { gps = f; lastFixAt = f.t; }    // {t ms, lat, lon, acc m, spd m/s|null, hdg deg|null}

  function event(t, ev, v, extra) { events.push(Object.assign({t, ev, v: v == null ? undefined : r2(v)}, extra || {})); }
  function edge(key, on, t, ev, v) {       // fire once when a condition starts; re-arm when it clears
    if (on && !armed[key]) { armed[key] = true; event(t, ev, v); } else if (!on) armed[key] = false;
  }

  function tick(t) {                       // once per second: -> one record (or null without motion and GPS)
    if (startT === null) startT = t;
    const rec = {t};
    const fix = gps && t - gps.t < 5000 ? gps : null;
    if (fix) {
      rec.lat = +fix.lat.toFixed(6); rec.lon = +fix.lon.toFixed(6); rec.acc = Math.round(fix.acc);
      if (fix.spd != null && fix.spd >= 0) rec.spd = r1(fix.spd * 3.6);
      if (fix.hdg != null && Number.isFinite(fix.hdg) && fix.spd > 1.5) rec.hdg = Math.round(fix.hdg);
    }
    const spdMs = fix && fix.spd != null && fix.spd >= 0 ? fix.spd : null;
    const dv = spdMs != null && prevSpd != null ? spdMs - prevSpd : null;
    prevSpd = spdMs;
    if (acc.n) {
      const g = V.mul(acc.g, 1 / acc.n), la = V.mul(acc.la, 1 / acc.n), rot = acc.rn ? V.mul(acc.rot, 1 / acc.rn) : null;
      rec.g = g.map(r3); rec.la_mean = la.map(r3); rec.la_peak = acc.peak.map(r3); if (rot) rec.rot = rot.map(r2);
      secs.push({t, g, la, dv, rot});
      const keep = Math.max(o.down_s, o.fwd_s);
      while (secs.length > keep) secs.shift();
      // pocket shift: the 5 s mean gravity has moved > shift_deg away from the calibrated down
      const recent = secs.slice(-o.shift_s);
      if (cal && recent.length >= o.shift_s && t - shiftAt > o.shift_s * 1000) {
        const g5 = recent.reduce((s, r) => V.add(s, r.g), [0, 0, 0]);
        const ang = V.angle(g5, cal.down);
        // a lean is a rotation about forward: with a known forward axis only the pitch part (gravity gaining a
        // forward component) or an impossible total counts as the phone moving in its mount / pocket
        const pitch = cal.forward ? Math.abs(Math.asin(Math.max(-1, Math.min(1, V.dot(V.unit(g5), cal.forward))))) * DEG : ang;
        if (ang > o.shift_deg && (pitch > o.shift_pitch_deg || ang > o.shift_max_lean_deg || !cal.forward)) {
          rec.pocket_shift = true; shiftAt = t; event(t, 'pocket_shift', V.angle(g5, cal.down));
          secs = recent.slice(); cal = null;         // restart both windows from the new position
        }
      }
      cal = rideCalibrate(secs.slice(-o.down_s), secs.slice(-o.fwd_s), o.min_fwd_records) || cal;
      if (cal) {
        const gu = V.unit(g), up = V.mul(cal.down, -1);
        rec.cal = {fwd: !!cal.forward, sign: cal.sign, n: cal.n_fwd};
        if (cal.forward && cal.sign) {
          const right = V.cross(cal.forward, up);
          rec.lean = r1(Math.atan2(V.dot(gu, right), V.dot(gu, cal.down)) * DEG);
          rec.lon_g = r2(V.dot(la, cal.forward) / RIDE_G);
          rec.lat_g = r2(V.dot(la, right) / RIDE_G);
        }
        if (rot && spdMs != null) {         // yaw rate = gyro rate about down (deg/s -> rad/s)
          const yaw = V.dot(rot, cal.down) / DEG;    // right-hand rule: a right turn is + about down
          rec.lean_turn = r1(Math.atan(spdMs * yaw / RIDE_G) * DEG);
        }
      }
      resetAcc();
    }
    if (!fix && !rec.g) return null;
    // ---- events (plain thresholds)
    if (rec.lon_g != null) { edge('brk', rec.lon_g < o.brake_g, t, 'braking', rec.lon_g); edge('acc', rec.lon_g > o.accel_g, t, 'acceleration', rec.lon_g); }
    if (rec.lean != null) edge('lean', Math.abs(rec.lean) > o.lean_deg, t, 'lean', rec.lean);
    if (rec.spd != null) {
      if (rec.spd < 2) { stoppedSince = stoppedSince == null ? t : stoppedSince; }
      else stoppedSince = null;
      edge('stop', stoppedSince != null && t - stoppedSince >= o.stopped_s * 1000, t, 'stopped', (t - (stoppedSince || t)) / 1000);
    }
    const lost = lastFixAt == null ? t - startT > o.gps_lost_s * 1000 : t - lastFixAt > o.gps_lost_s * 1000;
    if (lost !== lostFlag) { lostFlag = lost; if (lost) event(t, 'gps_lost', null); }
    if (lastGps && fix && fix !== lastGps && fix.acc < 50 && lastGps.acc < 50) rec.d_m = r1(rideHaversine(lastGps, fix));
    if (fix) lastGps = fix;
    if (events.length) { rec.ev = events; events = []; }
    minute.push(rec);
    return rec;
  }

  function minuteStats(t) {                // every 60 s: -> summary of the last minute, then reset
    const m = minute; minute = [];
    const mags = m.filter(r => r.la_mean).map(r => V.norm(r.la_mean)).sort((a, b) => a - b);
    const s = {t, n: m.length};
    if (mags.length) {
      const mean = mags.reduce((a, b) => a + b, 0) / mags.length;
      const sd = Math.sqrt(mags.reduce((a, b) => a + (b - mean) ** 2, 0) / mags.length);
      const med = mags.length % 2 ? mags[(mags.length - 1) / 2] : (mags[mags.length / 2 - 1] + mags[mags.length / 2]) / 2;
      Object.assign(s, {la_mean: r2(mean), la_median: r2(med), la_sd: r2(sd), la_mean_g: r3(mean / RIDE_G),
                        la_median_g: r3(med / RIDE_G), la_sd_g: r3(sd / RIDE_G)});
    }
    const lon = m.filter(r => r.lon_g != null).map(r => r.lon_g), lat = m.filter(r => r.lat_g != null).map(r => r.lat_g);
    if (lon.length) { s.peak_brake_g = Math.min(0, ...lon); s.peak_accel_g = Math.max(0, ...lon); }
    if (lat.length) s.peak_lat_g = lat.reduce((a, b) => Math.abs(b) > Math.abs(a) ? b : a, 0);
    const leans = m.filter(r => r.lean != null).map(r => r.lean);
    if (leans.length) s.max_lean = leans.reduce((a, b) => Math.abs(b) > Math.abs(a) ? b : a, 0);
    const sp = m.filter(r => r.spd != null).map(r => r.spd);
    if (sp.length) s.avg_spd = r1(sp.reduce((a, b) => a + b, 0) / sp.length);
    s.dist_m = r1(m.reduce((a, r) => a + (r.d_m || 0), 0));
    s.events = m.flatMap(r => r.ev || []);
    return s;
  }
  return {onMotion, onGps, tick, minuteStats, calibration: () => cal};
}

// Unsent batches survive reconnects: the page keeps every record (IndexedDB) and re-sends what the server hasn't
// acknowledged; the server is idempotent by t.
function RideOutbox() {
  let q = [];
  return {
    add(rec) { q.push(rec); },
    batch(max) { return q.slice(0, max || 600); },
    ack(upto) { q = q.filter(r => r.t > upto); },
    size: () => q.length,
  };
}
if (typeof module === 'object' && module.exports) {
  module.exports = {RideTelemetry, rideCalibrate, RideOutbox, rideHaversine, V};
}
