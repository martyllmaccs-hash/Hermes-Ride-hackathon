// Page glue around the wake engine: the real index.html scripts in node (tests/page_rig.js) with a fake sherpa
// Worker. Prints {checks, failures}.
'use strict';
const {loadPage, tick, checker} = require('./page_rig.js');
const T = checker(), {eq} = T;

const within = (p, ms) => Promise.race([p, tick(ms).then(() => 'still waiting')]);
const workers = [];
class FakeWorker {
  constructor(url) { this.url = url; this.posted = []; this.terminated = false; workers.push(this); }
  postMessage(m) { this.posted.push(m.t); if (m.t === 'init') setTimeout(() => this.onmessage({data: {t: 'ready'}}), 5); }
  terminate() { this.terminated = true; }
}

async function main() {
  // AUD-V2-09: sherpa loads, then fails at runtime -> transcript fallback, caps reported again, VAD probes work
  const P = loadPage({Worker: FakeWorker});
  await P.els.b1.onclick();
  await tick(20);
  const page = P.pages()[0];
  page.readyState = 1; page.onopen();
  P.msg(page, {t: 'hello', v: 2, epoch: 'w1', cfg: {stt_route: 'phone', window_s: 30, wake: {engine: 'sherpa'}}});
  await tick(50);
  eq('sherpa active', [P.run('engineName'), P.run('gate.opts().engine')], ['sherpa', 'sherpa']);
  const w = workers[workers.length - 1];
  w.onmessage({data: {t: 'error', error: 'wasm trap'}});
  eq('runtime failure: transcript engine', [P.run('engineName'), P.run('gate.opts().engine'), w.terminated], ['transcript', 'transcript', true]);
  eq('caps reported again', page.sent.filter(m => m.t === 'hello').pop().caps.wake, 'transcript');
  eq('note says why', /wake ready: transcript/.test(P.els.diag.textContent), true);
  P.feed(-20, 16);
  eq('a VAD probe opens STT now', [P.run('gate.state()'), page.sent.filter(m => m.t === 'need_token').length], ['probe', 1]);

  // AUD-V2-10: the engine is chosen from hello.cfg, not from defaults read before the hello arrived
  const nW = workers.length;
  const Q = loadPage({Worker: FakeWorker});
  await Q.els.b1.onclick();
  await tick(20);
  const qp = Q.pages()[0];
  qp.readyState = 1; qp.onopen();
  Q.msg(qp, {t: 'hello', v: 2, epoch: 'w2', cfg: {stt_route: 'server', wake: {engine: 'transcript'}}});
  await tick(50);
  eq('configured transcript engine used, no sherpa worker', [Q.run('engineName'), workers.length - nW], ['transcript', 0]);
  eq('caps after hello carry the configured route', qp.sent.filter(m => m.t === 'hello').pop().caps, {wake: 'transcript', stt: 'server'});

  // AUD-V2-10: hung Cache Storage / IndexedDB / worklet module never block setup forever
  const hang = () => new Promise(() => {});
  const R = loadPage({Worker: FakeWorker, RIDE_SETUP_WAIT: {cache: 50, idb: 50, worklet: 50, mic: 50, hello: 50},
                      caches: {open: hang}, indexedDB: {open: () => ({})}});
  const done1 = R.els.b1.onclick();
  await tick(300);
  eq('hung cache + IndexedDB: still connects', R.pages().length, 1);
  eq('setup finished', await within(done1, 500), undefined);
  eq('no hello within the bound: explicit default engine', [R.run('engineName'), /default/.test(R.els.diag.textContent)], ['sherpa', true]);
  const S2 = loadPage({RIDE_SETUP_WAIT: {cache: 50, idb: 50, worklet: 50, mic: 50, hello: 50},
                       AudioContext: class { constructor() { this.audioWorklet = {addModule: hang}; this.state = 'running'; } resume() { return Promise.resolve(); } }});
  eq('setup returned', await within(S2.els.b1.onclick(), 500), undefined);
  eq('hung worklet: mic failure shown, tap 1 again', [/mic failed/.test(S2.els.note.textContent), S2.els.b1.disabled], [true, false]);
  // Fix D (item 6): sherpa misses load_timeout_ms -> transcript for now, but the load keeps going and a late ready
  // switches the gate to sherpa, re-sends hello (caps.wake_ms) and says so in #diag
  class SlowWorker extends FakeWorker {
    postMessage(m) { this.posted.push(m.t); if (m.t === 'init') setTimeout(() => this.onmessage({data: {t: 'ready'}}), 120); }
  }
  const L = loadPage({Worker: SlowWorker});
  await L.els.b1.onclick();
  await tick(20);
  const lp = L.pages()[0];
  lp.readyState = 1; lp.onopen();
  L.msg(lp, {t: 'hello', v: 2, epoch: 'w3', cfg: {stt_route: 'phone', wake: {engine: 'sherpa', load_timeout_ms: 40}}});
  await tick(70);
  const lw = workers[workers.length - 1];
  eq('D timeout: transcript for now, the load not cancelled', [L.run('engineName'), L.run('gate.opts().engine'), lw.terminated],
     ['transcript', 'transcript', false]);
  eq('D hello says transcript (no wake_ms yet)', lp.sent.filter(m => m.t === 'hello').pop().caps, {wake: 'transcript', stt: 'phone'});
  await tick(120);
  const h = lp.sent.filter(m => m.t === 'hello').pop().caps;
  eq('D late ready: switched to sherpa', [L.run('engineName'), L.run('gate.opts().engine'), L.run('!!wakeEngine')], ['sherpa', 'sherpa', true]);
  eq('D hello again with wake sherpa and wake_ms', [h.wake, typeof h.wake_ms === 'number' && h.wake_ms >= 100], ['sherpa', true]);
  eq('D diag says late', /wake ready: sherpa \(late, \d+ ms\)/.test(L.els.diag.textContent), true);
  L.run('gate.sleep()');
  const hits = lp.sent.filter(m => m.t === 'wake').length;
  L.run('wakeEngine.onWake(1.5)');
  eq('D the late engine wakes the gate', [L.run('gate.state()'), lp.sent.filter(m => m.t === 'wake').length - hits], ['awake', 1]);
  // a late load after Stop is never adopted
  const M = loadPage({Worker: SlowWorker});
  await M.els.b1.onclick();
  await tick(20);
  const mp = M.pages()[0];
  mp.readyState = 1; mp.onopen();
  M.msg(mp, {t: 'hello', v: 2, epoch: 'w4', cfg: {stt_route: 'phone', wake: {engine: 'sherpa', load_timeout_ms: 40}}});
  await tick(70);
  const mw = workers[workers.length - 1];
  M.run("stopCapture('test')");
  await tick(150);
  eq('D Stop during the late load: worker terminated, never adopted', [mw.terminated, M.run('wakeEngine'), M.run('engineName')], [true, null, 'transcript']);
  T.done();
}
main().catch(e => T.crash(e));
