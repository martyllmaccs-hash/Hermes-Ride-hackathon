// Mic gate (W3): energy VAD on 20 ms frames + the gate states that decide what leaves the phone. Pure: the page
// feeds frames and acts on the events; tests/gate_test.js drives it with synthetic PCM. Time = frames * 20 ms.
//
//   sleep     nothing leaves the phone (telemetry aside). The ring keeps the last preroll_ms of audio.
//   probe     transcript wake engine only: VAD speech in sleep opens STT so the phone can look for the wake word
//             in the committed text; probeResult(false) sends it back to sleep.
//   awake     opened by the wake word: STT open, every frame streams (Scribe needs the silence to commit)
//   window    after the reply's done cue: stays open window_s (10 s) with no speech, so follow-ups need no wake word.
//             Speech in progress keeps it open: VAD speech, or a Scribe partial (heard()) in the last 1.5 s (R3-5).
//             A VAD run whose level stays flat (steady noise) stops counting after vad_alone_ms (R3-9); a run whose
//             level varies like speech always counts, partials or not (AUD-R3-03)
//             WP3: speech means a run of voiced_ms (300 ms) of consecutive loud frames, not just 2 (40 ms): keyboard taps
//             are runs of at most ~240 ms, so they open no probe and do not hold a window open. 0 = the old behaviour.
//   speaking  Lana is talking: STT stays open for barge-in candidates
//
// Fix A (item 7): hold(on) while one of our own cues or clips plays (the page's io.cue). While held and for cue_tail_ms
// after: a frame that would stream goes as zeros of the same length (Scribe keeps its timing and hears silence), VAD
// ignores it (no edge, no loud run, no activity: the window timer does not count the hold as talking), the pre-roll
// ring does not keep it, and the page does not feed it to the wake spotter (held()). Replies are not held (barge-in).
//
// Events (emit(ev)): {e:'open'} {e:'audio', pcm:Int16Array} {e:'close'} {e:'vad', s:1|0, at:ms} {e:'state', s}
'use strict';
// preroll_ms: before a VAD start (transcript probe). wake_preroll_ms: before a sherpa hit, which fires ~0.1 s after
// the phrase ends, so the whole "Hey Lana" reaches STT and can be stripped (live run 1: 800 ms gave "Anna, ...").
const RIDE_GATE_DEFAULTS = {base_db: -38, k: 0.15, cap_db: -25, hangover_ms: 600, preroll_ms: 800, wake_preroll_ms: 1500,
                            window_s: 10,
                            start_frames: 2, probe_tail_ms: 1500, engine: 'sherpa',
                            vad_alone_ms: 8000, heard_ms: 1500, vary_ms: 1000, vary_db: 3,
                            voiced_ms: 300, voiced_hold_ms: 1500, cue_tail_ms: 300};

function rideFrameDb(pcm) {
  let s = 0;
  for (let i = 0; i < pcm.length; i++) { const v = pcm[i] / 32768; s += v * v; }
  return 10 * Math.log10(s / Math.max(1, pcm.length) + 1e-12);
}

function RideGate(opts, emit) {
  const o = Object.assign({}, RIDE_GATE_DEFAULTS, opts || {});
  const FRAME = 20, ring = [];
  let ringMax = Math.max(1, Math.round(Math.max(o.preroll_ms, o.wake_preroll_ms) / FRAME));
  let state = 'sleep', t = 0, speed = 0, speech = false, loud = 0, quietMs = 0;
  let lastActivity = 0, busy = false, probeTailLeft = 0, sentMs = 0, open = false;
  let held = false, tailLeft = 0;            // Fix A: hold() and the cue_tail_ms after it
  let lastDb = -Infinity;                      // WP5: the level of the last frame, read by the orb (no second analysis)
  let runStart = 0, lastHeard = -Infinity, lastVoiced = -Infinity;   // WP3: lastVoiced = last frame of a run >= voiced_ms    // R3-9: start of the current VAD run; last Scribe partial (heard())
  const lv = []; let lvSum = 0, lvSq = 0;      // AUD-R3-03: frame levels of the last vary_ms (clamped), running sums
  function level(db) {                         // -> the spread (standard deviation, dB) of the last vary_ms of levels
    const x = Math.max(db, threshold() - 20);  // a pause between words counts as a dip, not as -120 dB
    lv.push(x); lvSum += x; lvSq += x * x;
    while (lv.length > Math.max(2, Math.round(o.vary_ms / FRAME))) { const y = lv.shift(); lvSum -= y; lvSq -= y * y; }
    const m = lvSum / lv.length;
    return Math.sqrt(Math.max(0, lvSq / lv.length - m * m));
  }

  function threshold() { return Math.min(o.base_db + o.k * Math.max(0, speed), o.cap_db); }
  function setState(s) {
    if (s === state) return;
    state = s; emit({e: 'state', s});
  }
  function openStt(prerollMs) {
    if (!open) { open = true; emit({e: 'open'}); }
    if (prerollMs) { for (const f of ring.slice(-Math.max(1, Math.round(prerollMs / FRAME)))) send(f); }
    ring.length = 0;
  }
  function closeStt() {
    if (open) { open = false; emit({e: 'close'}); }
  }
  function send(pcm) { sentMs += FRAME; emit({e: 'audio', pcm}); }

  function feedHeld(pcm) {                   // Fix A: our own cue is playing: silence out, VAD untouched
    if (!held) tailLeft -= FRAME;
    lastDb = -120;
    if (state === 'sleep') return;
    const zero = new Int16Array(pcm.length);
    if (state === 'probe') { if (speech || probeTailLeft > 0) send(zero); return; }
    send(zero);
    if (state !== 'speaking' && !busy && t - lastActivity >= o.window_s * 1000) sleep();
  }
  function feed(pcm, db) {
    t += FRAME;
    if (held || tailLeft > 0) { feedHeld(pcm); return; }
    if (db === undefined || db === null) db = rideFrameDb(pcm);
    lastDb = db;
    // ---- VAD with a start run and a hangover
    const isLoud = db > threshold();
    let edge = 0;
    if (isLoud) { loud++; quietMs = 0; if (!speech && loud >= o.start_frames) { speech = true; edge = 1; runStart = t; } }
    else { loud = 0; if (speech) { quietMs += FRAME; if (quietMs >= o.hangover_ms) { speech = false; edge = -1; } } }
    // R3-9 / AUD-R3-03: VAD is energy only, so speech and noise are told apart by how the level moves. Speech has
    // syllables and short dips: over the last vary_ms its frame level spreads by vary_db (3 dB) or more, and such a
    // run always counts as speech, with or without partials (a late or failing Scribe never cuts the rider). A flat
    // run (fan, engine at a constant speed: well under 1 dB) counts only for its first vad_alone_ms or while Scribe
    // partials keep coming (heard() in the last heard_ms); after that it no longer holds the window open (indoors
    // that read "stuck listening").
    // WP3: a short transient (a key click, a thunk) is loud but never voiced: `voicedRun` is the number of consecutive loud
    // frames that make a run of voiced_ms. Only voiced audio opens a probe in sleep or counts as talking in a window.
    const voicedRun = Math.max(2, Math.round(o.voiced_ms / FRAME));
    const voiced = speech && loud >= voicedRun;
    if (voiced) lastVoiced = t;
    const varying = level(db) >= o.vary_db && lv.length * FRAME >= o.vary_ms / 2;
    const talking = speech && t - lastVoiced < o.voiced_hold_ms && (varying || t - runStart < o.vad_alone_ms || t - lastHeard < o.heard_ms);
    if (talking) lastActivity = t;

    if (state === 'sleep') {
      ring.push(pcm); if (ring.length > ringMax) ring.shift();
      if (o.engine === 'transcript' && voiced && loud === voicedRun) {   // voiced speech in sleep: let STT hear it (pre-roll first)
        setState('probe'); openStt(o.preroll_ms); emit({e: 'vad', s: 1, at: t}); probeTailLeft = 0;
      }
      return;
    }
    if (state === 'probe') {
      if (edge === -1) { emit({e: 'vad', s: 0, at: t}); probeTailLeft = o.probe_tail_ms; }
      if (edge === 1) { emit({e: 'vad', s: 1, at: t}); probeTailLeft = 0; }
      if (speech || probeTailLeft > 0) { send(pcm); if (!speech) probeTailLeft -= FRAME; }
      else if (t - lastActivity > o.probe_tail_ms + 4000) probeResult(false);   // no answer from STT: give up
      return;
    }
    // awake / window / speaking: everything streams
    send(pcm);
    if (edge) emit({e: 'vad', s: edge > 0 ? 1 : 0, at: t});
    if (edge === 1 && state === 'window') setState('awake');
    if (state !== 'speaking' && !busy && !talking && t - lastActivity >= o.window_s * 1000) sleep();
  }
  function wake() {                        // the wake word fired (sherpa) or matched (transcript probe)
    lastActivity = t;
    if (state === 'probe') { setState('awake'); return; }
    if (state !== 'sleep') { setState('awake'); return; }
    setState('awake'); openStt(o.wake_preroll_ms);
  }
  function probeResult(hit) {
    if (state !== 'probe') return;
    if (hit) wake(); else { closeStt(); ring.length = 0; setState('sleep'); }
  }
  function sleep() { closeStt(); setState('sleep'); }
  function hold(on) {
    if (on) { held = true; tailLeft = 0; }
    else if (held) { held = false; tailLeft = o.cue_tail_ms; }
  }
  function dropPreroll() { ring.length = 0; }   // AUD-R3-02: Stop leaves no retained audio a late wake could forward
  function setSpeaking(on) {
    if (on) { if (state === 'awake' || state === 'window') setState('speaking'); }   // D1: a probe stays a probe (wake word first)
    else if (state === 'speaking') { lastActivity = t; setState('window'); }
  }
  function heard() { lastActivity = lastHeard = t; }   // R3-5: Scribe recognised speech now (it may be under the VAD threshold)
  function setBusy(on) { busy = !!on; if (!busy) lastActivity = t; }
  function setSpeed(kmh) { speed = Number.isFinite(kmh) ? kmh : 0; }
  function configure(opts2) {            // hello.cfg from the server (undefined values keep the defaults)
    for (const [k, v] of Object.entries(opts2 || {})) if (v !== undefined && v !== null) o[k] = v;
    ringMax = Math.max(1, Math.round(Math.max(o.preroll_ms, o.wake_preroll_ms) / FRAME));
  }
  function setEngine(name) { o.engine = name === 'transcript' ? 'transcript' : 'sherpa'; }
  function takeSentMs() { const s = sentMs; sentMs = 0; return s; }
  return {feed, hold, held: () => held || tailLeft > 0, wake, probeResult, sleep, dropPreroll, setSpeaking, setBusy, heard, setSpeed, threshold, takeSentMs, configure, setEngine,
          opts: () => Object.assign({}, o),
          state: () => state, speech: () => speech, isOpen: () => open, now: () => t, prerollFrames: () => ring.length,
          level: () => lastDb};
}
if (typeof module === 'object' && module.exports) module.exports = {RideGate, rideFrameDb, RIDE_GATE_DEFAULTS};
