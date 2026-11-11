// W5: wake word rules (static/wake.js) and, when static/vendor/ is fetched, the real sherpa-onnx WASM spotter on
// the speech fixtures (static/kws_worker.js core + the vendored build). Prints {checks, failures, kws}.
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm');
const ROOT = path.join(__dirname, '..');
const {rideMatchWake, rideWakeRe, rideKwsName, RideRefractory, rideChooseEngine, rideSherpaEngine} = require(path.join(ROOT, 'static', 'wake.js'));
const {rideKwsCreate, rideInt16ToFloat, RIDE_KWS_FILES} = require(path.join(ROOT, 'static', 'kws_worker.js'));

let checks = 0;
const failures = [];
function eq(name, got, want) { checks++; if (JSON.stringify(got) !== JSON.stringify(want)) failures.push({name, got, want}); }

async function main() {
  // 1. transcript engine regex: hits strip the wake word and keep the rest (10 Nov: "Hey Lana" is a config value;
  //    these cases run with its spellings from hello.cfg.wake.alts, the default is section 1b)
  const LANA = rideWakeRe(['lana', 'lanna', 'lena', 'llana', 'alana']);
  const hits = {
    'Hey, Lana. How fast am I going?': 'How fast am I going?',
    'hey lana what is the weather': 'what is the weather',
    'Lana, where is the next fuel stop?': 'where is the next fuel stop?',
    'Hey Lena, call home.': 'call home.',
    'Hey Llana!': '', 'Alana, stop.': 'stop.', '  "Lanna," turn left?': 'turn left?', 'HEY - LANA - test': 'test',
  };
  for (const [t, rest] of Object.entries(hits)) eq(`1 hit ${t}`, [rideMatchWake(t, LANA).hit, rideMatchWake(t, LANA).rest], [true, rest]);
  // near-misses and Lana mid-sentence are not a wake
  for (const t of ['Banana bread sounds good', 'Lanark is the next town', 'I told Lana about it', 'hey banana',
                   'Elena said hi', 'Hey Alan, wait', 'Llanberis pass', 'hey there Lana', 'Planar road', '', 'hey']) {
    eq(`1 miss ${t}`, rideMatchWake(t, LANA).hit, false);
  }
  eq('1 miss keeps the text', rideMatchWake('I told Lana', LANA).rest, 'I told Lana');
  // 1b. 10 Nov (Hermes-Ride): the default (no cfg) is "Hey Hermes"; Lana is not a wake by default
  for (const [t, rest] of Object.entries({'Hey, Hermes. How fast am I going?': 'How fast am I going?',
                                          'hermes where is fuel': 'where is fuel', 'Hey Hermès!': ''})) {
    eq(`1b default hit ${t}`, [rideMatchWake(t).hit, rideMatchWake(t).rest], [true, rest]);
  }
  for (const t of ['Hey Lana, how fast?', 'I told Hermes about it', 'Hermione said hi', 'hey her messages'])
    eq(`1b default miss ${t}`, rideMatchWake(t).hit, false);
  eq('1b rideWakeRe(empty) is the default', rideMatchWake('Hey Hermes, hi', rideWakeRe([])).hit, true);
  eq('1b keywords file: configured flat name, else the default',
     [rideKwsName({kws: 'kws-words-hey-lana.txt'}), rideKwsName({kws: '../secret.txt'}), rideKwsName(null),
      rideKwsName({kws: 'kws-words-hey-lana.txt?x'})],
     ['kws-words-hey-lana.txt', 'kws-words-hey-hermes.txt', 'kws-words-hey-hermes.txt', 'kws-words-hey-hermes.txt']);

  // 2. refractory period: one wake per 2 s
  const r = RideRefractory(2000);
  eq('2 refractory', [0, 500, 1999, 2000, 2100, 4100].map(t => r.accept(t)), [true, false, false, true, false, true]);

  // 3. engine choice: sherpa when it loads in time, else transcript (load error, timeout, or not wanted)
  const timers = [];
  const fakeTimer = (f, ms) => timers.push({f, ms});
  eq('3 sherpa loads', (await rideChooseEngine('sherpa', async () => ({feed() {}}), 8000, fakeTimer)).name, 'sherpa');
  const failed = await rideChooseEngine('sherpa', async () => { throw new Error('wasm: HTTP 404'); }, 8000, fakeTimer);
  eq('3 load failure falls back', [failed.name, failed.error], ['transcript', 'wasm: HTTP 404']);
  const slow = rideChooseEngine('sherpa', () => new Promise(() => {}), 8000, fakeTimer);
  timers.filter(t => t.ms === 8000).forEach(t => t.f());
  eq('3 8 s timeout falls back', (await slow).name, 'transcript');
  eq('3 transcript wanted', (await rideChooseEngine('transcript', async () => ({}), 8000, fakeTimer)).name, 'transcript');
  eq('3 timeout value from cfg', timers.every(t => t.ms === 8000), true);

  // 3b. AUD-V2-09: a worker that fails after it was ready reports it (onFail) and is terminated; a loader that
  //     finishes after the timeout is stopped, not left running
  class FakeWorker {
    constructor() { FakeWorker.last = this; this.terminated = false; }
    postMessage(m) { if (m.t === 'init') { FakeWorker.init = m; Promise.resolve().then(() => this.onmessage({data: {t: 'ready'}})); } }
    terminate() { this.terminated = true; }
  }
  await rideSherpaEngine({Worker: FakeWorker});
  eq('3d init asks for the default keywords file', FakeWorker.init.keywords, 'kws-words-hey-hermes.txt');
  await rideSherpaEngine({Worker: FakeWorker, keywords: 'kws-words-hey-lana.txt'});
  eq('3d init asks for the configured keywords file', FakeWorker.init.keywords, 'kws-words-hey-lana.txt');
  const eng = await rideSherpaEngine({Worker: FakeWorker});
  const why = [];
  eng.onFail = e => why.push(String(e && e.message || e));
  FakeWorker.last.onmessage({data: {t: 'error', error: 'decode: out of memory'}});
  eq('3b error after ready -> onFail + terminate', [why, FakeWorker.last.terminated], [['decode: out of memory'], true]);
  const eng2 = await rideSherpaEngine({Worker: FakeWorker});
  const why2 = []; eng2.onFail = e => why2.push(String(e && e.message || e));
  FakeWorker.last.onerror({message: 'worker crashed'});
  FakeWorker.last.onerror({message: 'again'});
  eq('3b worker error after ready -> onFail once', why2, ['worker crashed']);
  const t2 = []; let stopped = 0, release;
  const late = rideChooseEngine('sherpa', () => new Promise(res => { release = res; }), 8000, (f, ms) => t2.push(f));
  t2.forEach(f => f());
  eq('3b timeout -> transcript', (await late).name, 'transcript');
  release({feed() {}, stop() { stopped++; }});
  await new Promise(r => setTimeout(r, 0));
  eq('3b late engine stopped', stopped, 1);
  {   // Fix D (item 6): with onLate the timeout falls back (pending: true) but does not cancel; a late engine is handed over
    const t4 = []; let rel4, cancelled = 0, stopped4 = 0; const lateGot = [];
    const lp = rideChooseEngine('sherpa', () => Object.assign(new Promise(res => { rel4 = res; }), {cancel() { cancelled++; }}), 8000,
                                (f, ms) => t4.push(f), (e, ms) => lateGot.push([e.tag, typeof ms === 'number' && ms >= 0]));
    await new Promise(r => setTimeout(r, 0));
    t4.forEach(f => f());
    const r4 = await lp;
    eq('D timeout with onLate: transcript for now, pending, not cancelled', [r4.name, r4.error, r4.pending, cancelled], ['transcript', 'sherpa load timeout', true, 0]);
    rel4({tag: 'late', feed() {}, stop() { stopped4++; }});
    await new Promise(r => setTimeout(r, 0));
    eq('D late engine adopted via onLate, not stopped', [lateGot, stopped4], [[['late', true]], 0]);
    const t5 = []; let cancelled5 = 0;
    const lp5 = rideChooseEngine('sherpa', () => Object.assign(new Promise(() => {}), {cancel() { cancelled5++; }}), 8000, (f, ms) => t5.push(f));
    await new Promise(r => setTimeout(r, 0));
    t5.forEach(f => f());
    const r5 = await lp5;
    eq('D without onLate: old behaviour (cancelled, no pending)', [r5.name, cancelled5, r5.pending], ['transcript', 1, undefined]);
    const t6 = []; let rej6; const lateGot6 = [];
    const lp6 = rideChooseEngine('sherpa', () => new Promise((res, rej) => { rej6 = rej; }), 8000, (f, ms) => t6.push(f), e => lateGot6.push(e));
    t6.forEach(f => f()); await lp6;
    rej6(new Error('wasm: HTTP 404')); await new Promise(r => setTimeout(r, 0));
    eq('D a late failure calls nothing', lateGot6, []);
    const ok7 = await rideChooseEngine('sherpa', async () => ({feed() {}}), 8000, fakeTimer, () => {});
    eq('D in-time load: sherpa with its load time', [ok7.name, typeof ok7.ms === 'number' && ok7.ms >= 0], ['sherpa', true]);
  }

  // 3c. fix round 2 (R7): no worker outlives a failed or abandoned load, and a silent spotter is detected
  class ErrWorker extends FakeWorker {          // fails during init, before 'ready'
    postMessage(m) { if (m.t === 'init') Promise.resolve().then(() => this.onmessage({data: {t: 'error', error: 'model: HTTP 404'}})); }
  }
  let preErr = null;
  try { await rideSherpaEngine({Worker: ErrWorker}); } catch (e) { preErr = String(e && e.message || e); }
  eq('3c pre-ready error: rejected + terminated', [preErr, FakeWorker.last.terminated], ['model: HTTP 404', true]);
  class MuteWorker extends FakeWorker { postMessage() {} }    // never ready, never errors
  const t3 = [];
  const never = rideChooseEngine('sherpa', () => rideSherpaEngine({Worker: MuteWorker}), 8000, (f, ms) => t3.push(f));
  await new Promise(r => setTimeout(r, 0));
  const mute = FakeWorker.last;
  t3.forEach(f => f());
  eq('3c never-ready load: timeout -> transcript, worker terminated', [(await never).name, mute.terminated], ['transcript', true]);
  const eng3 = await rideSherpaEngine({Worker: FakeWorker});          // ready, then silent: no reply to any frame
  const silent = FakeWorker.last, why3 = [];
  eng3.onFail = e => why3.push(String(e && e.message || e));
  for (let i = 0; i < 300; i++) eng3.feed(new Int16Array(320));       // 6 s of frames, not one message back
  eq('3c silent after ready: onFail once + terminated', [why3.length, /stall/.test(why3[0] || ''), silent.terminated], [1, true, true]);
  class AliveWorker extends FakeWorker {                              // answers like kws_worker.js: alive every 50
    postMessage(m) { super.postMessage(m); if (m.t === 'pcm' && ++this.n % 50 === 0) this.onmessage({data: {t: 'alive'}}); }
  }
  AliveWorker.prototype.n = 0;
  const eng4 = await rideSherpaEngine({Worker: AliveWorker});
  const why4 = []; eng4.onFail = e => why4.push(e);
  for (let i = 0; i < 1000; i++) eng4.feed(new Int16Array(320));
  eq('3c a live spotter is never failed', [why4.length, FakeWorker.last.terminated], [0, false]);

  // 4. the real spotter (vendored WASM build + zh-en model + generated keywords) on the fixtures
  const vendor = path.join(ROOT, 'static', 'vendor');
  const need = ['sherpa-onnx-wasm-kws-main.js', 'sherpa-onnx-wasm-kws-main.wasm', 'sherpa-onnx-kws.js',
                'kws-words-hey-lana.txt', 'kws-words-hey-hermes.txt', ...RIDE_KWS_FILES];
  if (!need.every(f => fs.existsSync(path.join(vendor, f)))) return {skipped: 'static/vendor not fetched (sh tools/fetch_assets.sh)'};
  const t0 = Date.now();
  globalThis.require = require; globalThis.__dirname = vendor; globalThis.__filename = path.join(vendor, 'x.js');
  const ready = new Promise(res => {
    globalThis.Module = {locateFile: p => path.join(vendor, p), getPreloadedPackage: () => new ArrayBuffer(0),
                         print: () => {}, printErr: () => {}, onRuntimeInitialized: res};
  });
  vm.runInThisContext(fs.readFileSync(path.join(vendor, 'sherpa-onnx-kws.js'), 'utf8').replace(/if \(typeof process[\s\S]*$/, ''));
  vm.runInThisContext(fs.readFileSync(path.join(vendor, 'sherpa-onnx-wasm-kws-main.js'), 'utf8'));
  await ready;
  const files = {};
  for (const n of RIDE_KWS_FILES) files[n] = new Uint8Array(fs.readFileSync(path.join(vendor, n)));
  const kwFile = n => fs.readFileSync(path.join(vendor, n), 'utf8');
  const spot = rideKwsCreate(globalThis.Module, globalThis.createKws, files, kwFile('kws-words-hey-lana.txt'), 0.25, 1.0);
  const initMs = Date.now() - t0;
  const fx = path.join(ROOT, 'tests', 'fixtures');
  const results = {};
  const ALL_FX = ['speech_clean.wav', 'speech_road.wav', 'stale_pause.wav', 'search_clean.wav', 'stretch_clean.wav',
                  'sensor_clean.wav', 'sensor_road.wav', 'approval_clean.wav', 'hermes_clean.wav', 'hermes_road.wav',
                  'hermes_search.wav'];
  for (const w of ALL_FX) {
    const b = fs.readFileSync(path.join(fx, w)), pcm = new Int16Array(b.buffer, b.byteOffset + 44, (b.length - 44) >> 1);
    const got = [], t1 = Date.now(), refr = RideRefractory(2000);
    for (let i = 0; i < pcm.length; i += 320) {             // the page feeds 20 ms frames
      for (const k of spot.feed(rideInt16ToFloat(pcm.subarray(i, i + 320)))) {
        const at = +((i + 320) / 16000).toFixed(2);
        if (refr.accept(at * 1000)) got.push([k, at]);
      }
    }
    spot.reset();
    results[w] = {hits: got, decode_ms: Date.now() - t1, audio_s: +(pcm.length / 16000).toFixed(2)};
  }
  eq('4 "Hey, Lana" fires (clean)', results['speech_clean.wav'].hits.map(h => h[0]), ['HEY_LANA']);
  eq('4 "Hey, Lana" fires (road noise)', results['speech_road.wav'].hits.map(h => h[0]), ['HEY_LANA']);
  eq('4 fires within the first 2.5 s', results['speech_clean.wav'].hits[0][1] <= 2.5, true);
  eq('4 a second speaker (stock voice) fires too', ['stale_pause.wav', 'search_clean.wav', 'stretch_clean.wav'].map(w => results[w].hits.map(h => h[0])),
     [['HEY_LANA'], ['HEY_LANA'], ['HEY_LANA']]);
  for (const w of ['sensor_clean.wav', 'sensor_road.wav', 'approval_clean.wav']) eq(`4 no false wake: ${w}`, results[w].hits, []);
  // 4b. 10 Nov (Hermes-Ride): the public default file. "Hey Hermes" fixtures are a stock voice (tools/render_fixtures.py);
  //     hermes_road is hermes_clean + seeded brown noise at -30 dBFS (a stand-in, not a road recording).
  for (const w of ['hermes_clean.wav', 'hermes_road.wav', 'hermes_search.wav']) eq(`4b the Lana file ignores ${w}`, results[w].hits, []);
  const spotH = rideKwsCreate(globalThis.Module, globalThis.createKws, files, kwFile('kws-words-hey-hermes.txt'), 0.25, 1.0);
  const resultsH = {};
  for (const w of ALL_FX) {
    const b = fs.readFileSync(path.join(fx, w)), pcm = new Int16Array(b.buffer, b.byteOffset + 44, (b.length - 44) >> 1);
    const got = [], refr = RideRefractory(2000);
    for (let i = 0; i < pcm.length; i += 320) {
      for (const k of spotH.feed(rideInt16ToFloat(pcm.subarray(i, i + 320)))) {
        const at = +((i + 320) / 16000).toFixed(2);
        if (refr.accept(at * 1000)) got.push([k, at]);
      }
    }
    spotH.reset();
    resultsH[w] = got;
  }
  eq('4b "Hey Hermes" fires (clean)', resultsH['hermes_clean.wav'].map(h => h[0]), ['HEY_HERMES']);
  eq('4b "Hey Hermes" fires (brown noise -30 dBFS)', resultsH['hermes_road.wav'].map(h => h[0]), ['HEY_HERMES']);
  // Plain "Hermes," (hermes_search.wav) is measured, not asserted: 10 Nov it did not fire at #0.35, nor at 0.3, 0.25
  // or 0.2 (scratch sweep), so the sherpa wake is the two-word phrase; plain "Hermes" works on the transcript engine.
  for (const w of ALL_FX.filter(w => !w.startsWith('hermes_'))) eq(`4b the Hermes file ignores ${w}`, resultsH[w], []);
  // 5. WP3: keyboard taps (tests/tap_audio.js, synthetic, seeded) must not trip the spotter (asserted). Typing mixed
  //    over a real "Hey, Lana" is measured and recorded, not asserted (see below).
  const TAPS = require(path.join(ROOT, 'tests', 'tap_audio.js'));
  const feedAll = pcm => {
    const hits = [];
    for (let i = 0; i + 320 <= pcm.length; i += 320) for (const k of spot.feed(rideInt16ToFloat(pcm.subarray(i, i + 320)))) hits.push([k, +((i + 320) / 16000).toFixed(2)]);
    spot.reset(); return hits;
  };
  let tapSeconds = 0, falseWakes = 0;
  for (const pattern of ['single', 'typing', 'rapid']) for (const seed of [1, 2, 3]) for (const lv of [[-34, -14], [-45, -30]]) {
    falseWakes += feedAll(TAPS.tapAudio({seconds: 20, seed, pattern, level_db: lv})).length; tapSeconds += 20;
  }
  eq(`5 taps never trip the spotter (${tapSeconds} s of tap audio)`, falseWakes, 0);
  const mixed = {};
  for (const w of ['speech_clean.wav', 'speech_road.wav']) {
    const b = fs.readFileSync(path.join(fx, w)), sp = new Int16Array(b.buffer.slice(b.byteOffset + 44, b.byteOffset + b.length));
    for (const [pattern, lv] of [['typing', [-34, -14]], ['typing', [-45, -30]]]) {
      const tp = TAPS.tapAudio({seconds: sp.length / 16000, seed: 7, pattern, level_db: lv});
      const mix = new Int16Array(sp.length);
      for (let i = 0; i < sp.length; i++) mix[i] = Math.max(-32768, Math.min(32767, sp[i] + tp[i]));
      mixed[`${w}+${pattern}${lv[0]}`] = feedAll(mix).map(h => h[0]);
    }
  }
  const missed = Object.values(mixed).filter(h => h.join() !== 'HEY_LANA').length;
  // Not asserted. Fresh-spotter runs hear "Hey, Lana" over typing every time (quiet typing 40/40, loud 20/20 per
  // fixture; 1800 s of typing first: still heard). But in a long replay inside one process, after a first miss, the
  // same mixes can alternate hit/miss (10 of 20), with or without silence between runs. Cause unknown (see
  // HANDOFF.md, WP3 open item), so the number is recorded, not enforced.
  return {init_ms: initMs, results, hermes: resultsH, plain_hermes_fired: resultsH['hermes_search.wav'].length > 0, taps: {seconds: tapSeconds, false_wakes: falseWakes}, mixed_with_typing: mixed, missed_wakes_of_4_mixes_in_this_run: missed, note: 'state-dependent in long replays, see HANDOFF WP3'};
}

main().then(kws => console.log(JSON.stringify({checks, failures, kws})),
            e => console.log(JSON.stringify({checks, failures: failures.concat([{name: 'crash', got: String(e && e.stack || e)}])})));
