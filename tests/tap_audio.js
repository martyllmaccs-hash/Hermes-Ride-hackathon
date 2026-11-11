// WP3: deterministic synthetic keyboard-tap audio for the gate and spotter tests (no recorded audio needed).
// A key press = a click (broadband noise, ~3 ms attack, decay tau 4 to 9 ms) plus, for some keys, a low "thunk"
// (180 to 320 Hz sine, tau 15 to 30 ms). Typing = bursts of 3 to 12 keys 80 to 250 ms apart, 0.4 to 1.5 s between
// bursts, over a quiet room (-62 dBFS noise). Levels are peak-ish dBFS of the click; the loudest are what a laptop
// mic right next to the keys hears. 16 kHz mono Int16, like the page's capture. Seeded: same input, same audio.
'use strict';
const RATE = 16000;

function rng(seed) {                               // mulberry32
  let a = seed >>> 0;
  return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1);
                 t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}

// opts: {seconds, seed, floor_db (-62), level_db: [lo, hi] click peak dBFS ([-34, -14]), thunk: 0..1 (0.5),
//        pattern: 'typing' | 'single' | 'rapid' (typing)}; -> Int16Array
function tapAudio(opts) {
  const o = Object.assign({seconds: 6, seed: 1, floor_db: -62, level_db: [-34, -14], thunk: 0.5, pattern: 'typing'}, opts || {});
  const r = rng(o.seed), n = Math.round(o.seconds * RATE), x = new Float32Array(n);
  const fa = Math.sqrt(3) * Math.pow(10, o.floor_db / 20);
  for (let i = 0; i < n; i++) x[i] = fa * (2 * r() - 1);
  const press = at => {
    const db = o.level_db[0] + r() * (o.level_db[1] - o.level_db[0]), amp = Math.pow(10, db / 20);
    const tau = (4 + 5 * r()) * RATE / 1000, len = Math.round(tau * 6);
    for (let i = 0; i < len && at + i < n; i++) {
      const env = Math.exp(-i / tau) * Math.min(1, i / (0.003 * RATE));
      x[at + i] += amp * env * (2 * r() - 1);
    }
    if (r() < o.thunk) {
      const f = 180 + 140 * r(), tt = (15 + 15 * r()) * RATE / 1000, tl = Math.round(tt * 5), a2 = amp * 0.7;
      for (let i = 0; i < tl && at + i < n; i++) x[at + i] += a2 * Math.exp(-i / tt) * Math.sin(2 * Math.PI * f * i / RATE);
    }
  };
  let t = Math.round((0.3 + 0.5 * r()) * RATE);
  while (t < n - 0.2 * RATE) {
    if (o.pattern === 'single') { press(t); t += Math.round((1 + 2 * r()) * RATE); continue; }
    const keys = o.pattern === 'rapid' ? 20 : 3 + Math.floor(r() * 10);
    for (let k = 0; k < keys && t < n - 0.2 * RATE; k++) {
      press(t);
      t += Math.round((o.pattern === 'rapid' ? 0.05 + 0.04 * r() : 0.08 + 0.17 * r()) * RATE);
    }
    t += Math.round((0.4 + 1.1 * r()) * RATE);
  }
  const out = new Int16Array(n);
  for (let i = 0; i < n; i++) out[i] = Math.max(-32768, Math.min(32767, Math.round(x[i] * 32768)));
  return out;
}

function frames(pcm, size) {                       // 20 ms frames (320 samples) as views
  size = size || 320; const f = [];
  for (let i = 0; i + size <= pcm.length; i += size) f.push(pcm.subarray(i, i + size));
  return f;
}
function readWav(file) {                           // 44-byte-header PCM16 mono wav -> Int16Array copy
  const b = require('fs').readFileSync(file);
  return new Int16Array(b.buffer.slice(b.byteOffset + 44, b.byteOffset + b.length - ((b.length - 44) & 1)));
}
function concat(...parts) {
  const out = new Int16Array(parts.reduce((s, p) => s + p.length, 0)); let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}
module.exports = {tapAudio, frames, readWav, concat, RATE};
