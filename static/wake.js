// Wake word (W5): a pluggable spotter. The word is a server setting (10 Nov, Hermes-Ride: hello.cfg.wake carries
// word, name, alts, kws (the keywords file); default "Hey Hermes", a personal name like "Hey Lana" is config only). Engines:
//   sherpa      (default) sherpa-onnx open-vocabulary keyword spotter in a Worker (kws_worker.js + static/vendor/),
//               fed every 20 ms frame while the gate sleeps; fires onWake(score) on "HEY <NAME>" / "<NAME>" from the
//               configured keywords file (cfg.wake.kws).
//   transcript  (fallback, always available) VAD speech opens STT (gate 'probe') and the committed text must start
//               with the wake word: matchWake() strips it and keeps the rest as the utterance.
// Pure parts (matchWake, Refractory, chooseEngine) are tested under node (tests/wake_test.js).
'use strict';
const RIDE_WAKE_ALTS = ['hermes', 'hermès', 'hermez', 'hermies'];     // the default ("Hey Hermes"), = ride_logic
function rideWakeRe(alts) {        // cfg.wake.alts -> the transcript rule (same shape as ride_logic.strip_wake)
  const a = (Array.isArray(alts) && alts.length ? alts : RIDE_WAKE_ALTS).map(x => String(x).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  return new RegExp('^[\\s"\'“‘(\\[-]*(?:hey[\\s,.!?;:-]+)?(' + a.join('|') + ')\\b[\\s,.!?;:"\'”’)\\]-]*', 'i');
}
const RIDE_WAKE_RE = rideWakeRe();
const RIDE_KWS_DEFAULT = 'kws-words-hey-hermes.txt';
function rideKwsName(cfgWake) {    // hello.cfg.wake.kws -> the keywords file: a flat kws-words-*.txt name, else the default
  const n = String((cfgWake && cfgWake.kws) || '');
  return /^kws-words-[a-z0-9-]{1,40}\.txt$/.test(n) ? n : RIDE_KWS_DEFAULT;
}

function rideMatchWake(text, re) {
  // -> {hit: true, rest} when the text starts with the wake word (re: rideWakeRe(cfg.wake.alts)), else {hit: false, rest: text}
  const s = String(text || '');
  const m = (re || RIDE_WAKE_RE).exec(s);
  if (!m) return {hit: false, rest: s};
  return {hit: true, word: m[1].toLowerCase(), rest: s.slice(m[0].length).trim()};
}

function RideRefractory(ms) {             // at most one wake per `ms`
  let last = -Infinity;
  return {accept(now) { if (now - last < ms) return false; last = now; return true; }};
}

// load: () => Promise<engine> (optionally with .cancel()); resolves to {name, engine, error?, ms?} within timeoutMs
// (sherpa) or falls back. At the timeout a still-pending load is cancelled (fix round 2, R7: its worker is
// terminated, not left loading forever); an engine that still arrives later is stopped.
// Fix D (item 6): with onLate(engine, ms) the timeout resolves {name: 'transcript', error, pending: true} and the load
// keeps going; an engine that arrives later is handed to onLate (the page switches to it) instead of being stopped.
// ms = load start to ready (the page reports it as hello caps.wake_ms).
function rideChooseEngine(want, load, timeoutMs, timer, onLate) {
  timer = timer || ((f, ms) => setTimeout(f, ms));
  if (want !== 'sherpa' || typeof load !== 'function') return Promise.resolve({name: 'transcript', engine: null});
  const t0 = Date.now();
  return new Promise(resolve => {
    let done = false, loading = null;
    const finish = r => { if (!done) { done = true; resolve(r); } };
    timer(() => {
      if (done) return;
      if (typeof onLate === 'function') { finish({name: 'transcript', engine: null, error: 'sherpa load timeout', pending: true}); return; }
      finish({name: 'transcript', engine: null, error: 'sherpa load timeout'});
      if (loading && typeof loading.cancel === 'function') try { loading.cancel(); } catch (x) {}
    }, timeoutMs);
    Promise.resolve().then(() => (loading = load())).then(
      e => {
        const ms = Date.now() - t0;
        if (done) {
          if (e && typeof onLate === 'function') { onLate(e, ms); return; }
          if (e && e.stop) try { e.stop(); } catch (x) {}   // too late: don't leave it running
          return;
        }
        finish(e ? {name: 'sherpa', engine: e, ms} : {name: 'transcript', engine: null, error: 'sherpa unavailable'});
      },
      err => finish({name: 'transcript', engine: null, error: String(err && err.message || err)}));
  });
}

// Browser: the sherpa engine runs in a Worker. start(frames) is implicit: the page calls feed(pcm) per frame.
// A failure before 'ready' terminates the worker and rejects the load (fix round 2, R7), as does cancel() on the
// returned promise; after it (AUD-V2-09) the worker is terminated and eng.onFail(err) fires once, so the page can
// switch to the transcript engine instead of sleeping deaf. Liveness (R7): kws_worker.js answers every 50 frames
// ({t:'alive'}); liveness_frames (250 = 5 s) fed frames without any message back is a stall, handled as a failure.
function rideSherpaEngine(opts) {
  let cancel = () => {};
  const p = new Promise((resolve, reject) => {
    let w, ready = false, dead = false, quiet = 0;
    const LIVE = opts.liveness_frames || 250;
    const W = opts.Worker || (typeof Worker !== 'undefined' ? Worker : undefined);
    try { w = new W(opts.workerUrl || 'kws_worker.js'); } catch (e) { reject(e); return; }
    const eng = {
      onWake: null, onFail: null,
      feed(pcm) {
        if (dead) return;
        if (++quiet > LIVE) { fail(new Error('sherpa stalled: no reply for ' + LIVE + ' frames')); return; }
        try { w.postMessage({t: 'pcm', pcm}, [pcm.buffer]); } catch (e) { fail(e); }
      },
      reset() { if (!dead) w.postMessage({t: 'reset'}); },
      stop() { if (!dead) { dead = true; try { w.terminate(); } catch (e) {} } },
    };
    function fail(err) {
      if (!ready) { eng.stop(); reject(err); return; }
      if (dead) return;
      eng.stop();
      if (eng.onFail) eng.onFail(err);
    }
    cancel = () => { if (!ready) fail(new Error('sherpa load cancelled')); };
    w.onmessage = ev => {
      if (dead) return;                    // AUD-R3-02: nothing from a stopped worker (a hit already queued) counts
      const m = ev.data || {};
      quiet = 0;
      if (m.t === 'ready') { ready = true; resolve(eng); }
      else if (m.t === 'error') fail(new Error(m.error));
      else if (m.t === 'hit' && eng.onWake) eng.onWake(m.score == null ? 1 : m.score, m.keyword);
    };
    w.onerror = e => fail(new Error((e && e.message) || 'worker error'));
    w.postMessage({t: 'init', base: opts.base || 'vendor/', threshold: opts.threshold, score: opts.score,
                   keywords: rideKwsName({kws: opts.keywords})});
  });
  p.cancel = () => cancel();
  return p;
}

if (typeof module === 'object' && module.exports) {
  module.exports = {rideMatchWake, rideWakeRe, rideKwsName, RideRefractory, rideChooseEngine, rideSherpaEngine, RIDE_WAKE_RE,
                    RIDE_WAKE_ALTS, RIDE_KWS_DEFAULT};
}
