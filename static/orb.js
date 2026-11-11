// WP5 (D1, P5): the orb, phase 1. One element in the middle of the black page shows display() (client_state.js)
// plus the mic level; index.html calls show(display) on every render, level(db, t) on every mic frame and tone() on
// every doo-doo tick. The look lives in index.html's CSS, one class per design (d-<name>) and per state (s-<state>);
// this file holds the two tables, so a new design is a table row plus its CSS, with no logic change.
//   sleep      pure black, static: no class that animates, no timer, no animation frame, no style write
//   listening  blue, a slow "alive" CSS breath, plus the input level as a ring scaled at most 20 times a second from
//              the frame level the gate already computed (gate.level()); nothing else is analysed
//   thinking   CSS: swells to bright white over about 1 s, then a slow breath; each doo-doo tick (tone()) restarts
//              the breath by flipping beat-a / beat-b (two identical keyframes), so the breath follows the tone
//   speaking   blue with a synthetic CSS envelope (P5: the reply audio is not routed through Web Audio); Fix F: with
//              a WAV reply segment (the hermes TTS engine) the ring follows her voice: rideWavEnvelope() turns the
//              segment's bytes into dBFS per 50 ms once, and the page reads it at audio.currentTime on each mic frame
//              (no new timer, no analyser). MP3 segments (direct engine) have no curve: the CSS pulse only.
//   muted      asleep-dark with a thin grey ring, static (round 3: the MUTE button): no animation, no level, no motion
//   offline, reconnecting, connecting   a slow amber CSS pulse
// P4: the orb moves with the motorcycle (orb_motion.js). setMotion(source) hands over the physics; the page calls
// motion(t) on every DeviceMotion event. In every state but sleep the source's frame() (null when nothing moved) is
// written as custom properties (--mx ... --ma) on els.motion (#orbm), whose CSS transform reads them.
// The orb itself never starts a timer or an animation frame: the only JS work is the class change per state and, awake,
// at most 20 style ticks per second: the level ring and the motion frame share one 50 ms grid.
'use strict';
// simple: the P4 look (halo, drifting light, glass shell); plain: the flat orb as it was before P4
const RIDE_ORB_DESIGNS = {simple: {cls: 'd-simple'}, plain: {cls: 'd-simple d-plain'}};   // later designs are added here + CSS
const RIDE_ORB_STATES = {        // display() -> what the orb does; level: input ring; anim: CSS animations running
  sleep: {anim: 0}, muted: {anim: 0},
  listening: {anim: 1, level: true}, thinking: {anim: 1, beat: true}, speaking: {anim: 1, level: true},
  offline: {anim: 1}, reconnecting: {anim: 1}, connecting: {anim: 1},
};
const RIDE_ORB_LEVEL_MS = 50;    // 20 Hz at most

function rideOrbDesign(cfg) {    // hello.cfg.orb_design -> a known design name, else 'simple'
  const n = String((cfg && cfg.orb_design) || '').trim().toLowerCase();
  return Object.prototype.hasOwnProperty.call(RIDE_ORB_DESIGNS, n) ? n : 'simple';
}
function rideOrbScale(db) {      // frame level (dBFS) -> ring scale: -60 dB and below 0.6, -20 dB and above 1.0
  const x = Math.max(0, Math.min(1, ((Number.isFinite(db) ? db : -120) + 60) / 40));
  return 0.6 + 0.4 * x;
}

// Fix F: Uint8Array (a 16-bit PCM RIFF/WAVE file) -> [dBFS of each 50 ms, all channels], else null
const RIDE_ORB_ENV_S = 0.05;
function rideWavEnvelope(bytes) {
  if (!bytes || !(bytes.byteLength >= 12)) return null;
  const v = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const tag = o => String.fromCharCode(v.getUint8(o), v.getUint8(o + 1), v.getUint8(o + 2), v.getUint8(o + 3));
  if (tag(0) !== 'RIFF' || tag(8) !== 'WAVE') return null;
  let fmt = null, off = 12;
  while (off + 8 <= v.byteLength) {
    const id = tag(off), size = v.getUint32(off + 4, true), body = off + 8;
    if (id === 'fmt ' && size >= 16 && body + 16 <= v.byteLength) {
      fmt = {format: v.getUint16(body, true), ch: v.getUint16(body + 2, true), rate: v.getUint32(body + 4, true), bits: v.getUint16(body + 14, true)};
    } else if (id === 'data') {
      if (!fmt || fmt.format !== 1 || fmt.bits !== 16 || !fmt.ch || !fmt.rate) return null;
      const end = Math.min(v.byteLength, body + size), frames = Math.floor((end - body) / (2 * fmt.ch));
      const step = Math.max(1, Math.round(fmt.rate * RIDE_ORB_ENV_S)), out = [];
      for (let f0 = 0; f0 < frames; f0 += step) {
        const f1 = Math.min(frames, f0 + step);
        let sum = 0;
        for (let i = body + f0 * 2 * fmt.ch, e = body + f1 * 2 * fmt.ch; i < e; i += 2) { const x = v.getInt16(i, true) / 32768; sum += x * x; }
        out.push(10 * Math.log10(sum / ((f1 - f0) * fmt.ch) + 1e-12));
      }
      return out;
    }
    off = body + size + (size & 1);
  }
  return null;
}

function RideOrb(els, opts) {
  const o = Object.assign({now: () => Date.now()}, opts || {});
  let design = 'simple', state = 'sleep', beat = '', nextAt = -Infinity, pendingDb = null, msrc = null;
  const stats = {levelWrites: 0, beats: 0, shows: 0, motionWrites: 0, ticks: 0};
  function paint() {
    els.root.className = ['orb', RIDE_ORB_DESIGNS[design].cls, 's-' + state].concat(beat ? [beat] : []).join(' ');
  }
  function show(display) {
    const s = RIDE_ORB_STATES[display] ? display : 'sleep';
    if (s === state && els.root.className) return;
    state = s; beat = ''; nextAt = -Infinity; pendingDb = null; stats.shows++;
    if (els.level) els.level.style.transform = '';   // the CSS rule of the new state applies (Fix F: speaking starts fresh)
    paint();
  }
  function flush(now) {          // one 50 ms grid for both writers: the ring's level and the motion frame
    if (now < nextAt) return;
    const st = RIDE_ORB_STATES[state], ring = st.level && els.level && pendingDb !== null;
    const mv = st.anim && msrc && els.motion ? msrc.frame(now) : null;       // null: nothing moved (deadband)
    if (!ring && !mv) return;
    nextAt = nextAt + RIDE_ORB_LEVEL_MS > now ? nextAt + RIDE_ORB_LEVEL_MS : now + RIDE_ORB_LEVEL_MS;
    stats.ticks++;
    if (ring) { els.level.style.transform = 'scale(' + rideOrbScale(pendingDb).toFixed(3) + ')'; pendingDb = null; stats.levelWrites++; }
    if (mv) { for (const k in mv) els.motion.style.setProperty('--' + k, String(mv[k])); stats.motionWrites++; }
  }
  function level(db, t) {        // every mic frame; acts only in a level state, at most every RIDE_ORB_LEVEL_MS
    if (!RIDE_ORB_STATES[state].level || !els.level) return;
    pendingDb = db;
    flush(t === undefined ? o.now() : t);
  }
  function motion(t) {           // P4: every DeviceMotion event; asleep nothing is asked and nothing is written
    if (!RIDE_ORB_STATES[state].anim || !msrc) return;
    flush(t === undefined ? o.now() : t);
  }
  function tone() {              // a doo-doo tick: restart the thinking breath in step with it
    if (!RIDE_ORB_STATES[state].beat) return;
    beat = beat === 'beat-a' ? 'beat-b' : 'beat-a'; stats.beats++;
    paint();
  }
  function setDesign(name) { design = RIDE_ORB_DESIGNS[name] ? name : 'simple'; paint(); }
  return {show, level, tone, setDesign, motion, setMotion(src) { msrc = src || null; }, state: () => state, design: () => design,
          animations: () => RIDE_ORB_STATES[state].anim, stats: () => Object.assign({}, stats)};
}
if (typeof module === 'object' && module.exports) module.exports = {RideOrb, rideOrbDesign, rideOrbScale, rideWavEnvelope, RIDE_ORB_DESIGNS, RIDE_ORB_STATES};
