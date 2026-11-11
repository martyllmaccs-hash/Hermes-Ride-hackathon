// Recognition recovery on the phone route (fix round 2, R2 / AUD-V2-01, -02): the real index.html scripts in node
// (tests/page_rig.js). The page shows 'listening' and plays 'back' only after a Scribe session really started; a
// socket stuck before session_started is closed at its deadline and retried; a quota episode that expires while
// asleep is not reported as recovered. Only page-level observables (sockets, C.display(), the queue log).
// Prints {checks, failures}.
'use strict';
const {loadPage, tick, checker} = require('./page_rig.js');
const T = checker(), {eq} = T;

const STT = {session_start_ms: 100, start_backoff_ms: [100], ask_timeout_ms: 2000};
const scribes = P => P.sockets.filter(s => !s.url.endsWith('/ws'));
const heard = P => { const s = P.run('C.state()'); return s.log.concat(s.queue); };
const token = (P, page, n) => P.msg(page, {t: 'token', epoch: 'r1', kind: 'realtime_scribe', token: 'sutkn_' + n,
                                           url: 'wss://scribe.test/rt', params: {model_id: 'scribe_v2_realtime'}});

async function up(P) {
  await P.els.b1.onclick();
  await tick(20);
  const page = P.pages()[0];
  page.readyState = 1; page.onopen();
  P.msg(page, {t: 'hello', v: 2, epoch: 'r1', cfg: {stt_route: 'phone', window_s: 30, wake: {engine: 'transcript'}}});
  await tick(20);
  return page;
}

async function main() {
  // 1. awake, Scribe socket allocated but never started: not 'listening'; closed at the deadline with a spoken
  //    notice, retried, and 'listening' + 'back' only after session_started
  const P = loadPage({RIDE_STT_OPTS: STT});
  const page = await up(P);
  P.run('gate.wake()');
  token(P, page, 'A'); token(P, page, 'B');
  eq('socket allocated', scribes(P).length, 1);
  eq('not started: display is not listening', P.run('C.display()') !== 'listening', true);
  await tick(150);
  eq('deadline: socket closed, failure reported', [scribes(P)[0].closed === true, page.sent.some(m => m.t === 'stt_error' && m.code === 'session_timeout')],
     [true, true]);
  eq('awake: the failure is spoken', heard(P).includes('clip:reconnecting'), true);
  await tick(150);
  eq('retried after the backoff', scribes(P).length, 2);
  eq('still not listening before session_started', [P.run('C.display()') !== 'listening', heard(P).includes('clip:back')], [true, false]);
  const s1 = scribes(P)[scribes(P).length - 1];             // the retry (the parent never makes one: the first)
  s1.onmessage({data: JSON.stringify({message_type: 'session_started'})});
  eq('started: listening + back', [P.run('C.display()'), heard(P).includes('clip:back')], ['listening', true]);

  // 2. quota while asleep, backoff expires while asleep: no 'back' yet (nothing proved recognition works); the
  //    next wake's started session plays it
  const Q = loadPage({RIDE_STT_OPTS: STT});
  const qp = await up(Q);
  Q.msg(qp, {t: 'state', s: 'quota', epoch: 'r1'});
  Q.msg(qp, {t: 'stt_backoff', code: 'quota_exceeded', retry_in: 0.1, epoch: 'r1'});
  await tick(300);
  eq('quota expired while asleep: no back', [heard(Q).includes('clip:quota'), heard(Q).includes('clip:back')], [true, false]);
  Q.run('gate.wake()');
  const asked = qp.sent.filter(m => m.t === 'need_token').length;
  token(Q, qp, 'C');
  eq('wake asks for / uses a token', asked >= 1 && scribes(Q).length === 1, true);
  eq('socket open, not started: still no back', heard(Q).includes('clip:back'), false);
  scribes(Q)[0].onmessage({data: JSON.stringify({message_type: 'session_started'})});
  eq('session started: back', heard(Q).includes('clip:back'), true);

  // 3. R5: the page counts English words like the server: a typographic contraction is one word
  const W = loadPage({RIDE_STT_OPTS: STT});
  const wp = await up(W);
  W.feed(-20, 40);                                          // VAD probe (transcript engine) opens STT
  token(W, wp, 'D');
  const ws3 = scribes(W)[0];
  ws3.onmessage({data: JSON.stringify({message_type: 'session_started'})});
  ws3.onmessage({data: JSON.stringify({message_type: 'committed_transcript', text: 'I don’t know'})});
  eq('no_wake word count: typographic apostrophe = ASCII', wp.sent.filter(m => m.t === 'no_wake').map(m => m.words), [3]);

  // 4. AUD-R3-06, server route: 'listening' and 'back' only once the server says its Scribe session is ready; a
  //    server start failure while awake is spoken; after a link loss the next session must be ready again
  const R = loadPage({RIDE_STT_OPTS: STT});
  await R.els.b1.onclick();
  await tick(20);
  const rp = R.pages()[0];
  rp.readyState = 1; rp.onopen();
  R.msg(rp, {t: 'hello', v: 2, epoch: 'v1', cfg: {stt_route: 'server', window_s: 30, wake: {engine: 'transcript'}}});
  await tick(20);
  R.feed(-20, 16);                                          // probe: PCM to the server, which checks the wake word
  R.msg(rp, {t: 'wake_hit', epoch: 'v1'});
  eq('R3-06 server: awake', R.run('gate.state()'), 'awake');
  R.msg(rp, {t: 'stt', s: 'connecting', epoch: 'v1'});
  eq('R3-06 server: Scribe connecting: not listening', R.run('C.display()'), 'connecting');
  R.msg(rp, {t: 'stt', s: 'failed', why: 'start_timeout', epoch: 'v1'});
  eq('R3-06 server: start failure while awake is spoken, not listening', [heard(R).includes('clip:reconnecting'), R.run('C.display()')],
     [true, 'connecting']);
  R.msg(rp, {t: 'stt', s: 'ready', epoch: 'v1'});
  eq('R3-06 server: ready: listening + back', [R.run('C.display()'), heard(R).includes('clip:back')], ['listening', true]);
  rp.onclose({code: 1006});
  await tick(1100);
  const rp2 = R.pages()[1];
  rp2.readyState = 1; rp2.onopen();
  R.msg(rp2, {t: 'hello', v: 2, epoch: 'v2', cfg: {stt_route: 'server', window_s: 30, wake: {engine: 'transcript'}}});
  R.run('gate.wake()');
  eq('R3-06 server: after a reconnect the link alone is not listening', R.run('C.display()'), 'connecting');
  R.msg(rp2, {t: 'stt', s: 'ready', epoch: 'v1'});          // a frame of the old connection does not count
  eq('R3-06 server: a ready of another epoch does not count', R.run('C.display()'), 'connecting');
  R.msg(rp2, {t: 'stt', s: 'ready', epoch: 'v2'});
  eq('R3-06 server: ready again: listening', R.run('C.display()'), 'listening');
  R.msg(rp2, {t: 'stt', s: 'closed', epoch: 'v2'});         // idle close while the gate is open: not ready now
  eq('R3-06 server: closed while awake: not listening', R.run('C.display()'), 'connecting');
  T.done();
}
main().catch(e => T.crash(e));
