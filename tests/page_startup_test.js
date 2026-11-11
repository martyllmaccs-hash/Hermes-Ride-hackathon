// Real page scripts + realistic setup removal. Do not let async startup exceptions escape unnoticed.
'use strict';
const {loadPage, tick, checker} = require('./page_rig.js');
const T = checker(), {eq} = T;
async function scenario(mode) {
  const workers = [];
  class Worker {
    constructor() { workers.push(this); }
    postMessage(m) {
      if (m.t === 'init' && mode === 'runtime') setTimeout(() => this.onmessage({data: {t: 'ready'}}), 0);
      if (m.t === 'init' && mode === 'late hello') setTimeout(() => this.onerror({message: 'wasm unavailable'}), 0);
    }
    terminate() { this.terminated = true; }
  }
  const P = loadPage({Worker, RIDE_SETUP_WAIT: {hello: 30}});
  P.run(`const startupErrors = []; const originalStartWake = startWake;
    startWake = () => originalStartWake().catch(e => startupErrors.push(String(e)));`);
  await P.els.b1.onclick();
  const page = P.pages()[0];
  page.readyState = 1; page.onopen();
  if (mode !== 'late hello') P.msg(page, {t: 'hello', cfg: {wake: {engine: 'sherpa', load_timeout_ms: 30}}});
  if (mode === 'runtime') await tick(10);
  eq(mode + ': removed setup children really disappear', P.run("$('setup') === null && $('note') === null && $('b1') === null"), true);
  if (mode === 'runtime') {
    try { workers[0].onerror({message: 'wasm runtime trap'}); }
    catch (e) { P.run('startupErrors').push(String(e)); }
  } else if (mode === 'load error') {
    await tick(0);
    workers[0].onerror({message: 'wasm fetch failed'});
  } else if (mode === 'late hello') {
    await tick(60);
    P.msg(page, {t: 'hello', cfg: {wake: {engine: 'transcript'}}});
  }
  await tick(80);
  eq(mode + ': no startup/callback exception', Array.from(P.run('startupErrors')), []);
  eq(mode + ': fallback selected on gate', P.run('gate.opts().engine'), 'transcript');
  eq(mode + ': capabilities sent after fallback', page.sent.filter(m => m.t === 'hello').pop().caps.wake, 'transcript');
  eq(mode + ': wake-ready diagnostic survives setup (in #diag, off screen)', P.run("!!$('diag') && /wake ready: transcript/.test($('diag').textContent)"), true);
  eq(mode + ': sleeping state is visible (WP5: the orb; the word is debug-only now)', P.els.orb.className, 'orb d-simple s-sleep');
  P.feed(-20, 16);
  eq(mode + ': VAD opens probe and requests token', [P.run('gate.state()'), page.sent.filter(m => m.t === 'need_token').length], ['probe', 1]);
}
(async () => {
  for (const mode of ['load error', 'load timeout', 'late hello', 'runtime']) await scenario(mode);
  console.log(JSON.stringify({checks: T.checks, failures: T.failures}));
  process.exit(T.failures.length ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
