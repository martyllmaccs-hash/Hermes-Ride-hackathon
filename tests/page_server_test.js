// Page glue on the server STT route (stt_route=server): the real index.html scripts in node (tests/page_rig.js).
// Gated PCM goes to the ride server as binary frames. Prints {checks, failures}.
'use strict';
const {loadPage, tick, checker} = require('./page_rig.js');
const T = checker(), {eq} = T;

async function main() {
  const P = loadPage();
  // AUD-V2-04: a refused audio unlock stops setup with an actionable note (tap again), not a silent page
  const play = P.audio.play;
  P.audio.play = () => Promise.reject({name: 'NotAllowedError'});
  await P.els.b1.onclick();
  eq('unlock refused: tap 1 again, nothing started', [P.els.b1.disabled, /tap 1 again/.test(P.els.note.textContent), P.pages().length],
     [false, true, 0]);
  P.audio.play = play;
  await P.els.b1.onclick();
  await tick(20);
  const page = P.pages()[0];
  page.readyState = 1; page.onopen();
  P.msg(page, {t: 'hello', v: 2, epoch: 's1', cfg: {stt_route: 'server', window_s: 30, wake: {engine: 'sherpa', load_timeout_ms: 300}}});
  await tick(400);                       // no Worker: transcript engine

  // AUD-V2-06: the transcript probe on the server route: the page reports the gate, the server decides the wake
  P.feed(-20, 16);
  eq('gate reported on open, then the probe', page.sent.filter(m => m.t === 'gate').map(m => m.s), ['sleep', 'probe']);
  P.msg(page, {t: 'wake_miss', epoch: 's1'});
  eq('miss: back to sleep', [P.run('gate.state()'), page.sent.filter(m => m.t === 'gate').map(m => m.s)], ['sleep', ['sleep', 'probe', 'sleep']]);
  P.feed(-70, 40); P.feed(-20, 16);
  P.msg(page, {t: 'wake_hit', epoch: 's1'});
  eq('hit: awake + wake cue', [P.run('gate.state()'), P.run('C.state().log')[0]], ['awake', 'cue:wake']);
  page.sent.length = 0;

  // AUD-V2-03: backpressure holds frames back; when the socket drains, the backlog leaves in 100 ms batches (never
  // an overfull fixed batch that throws on every later frame), bounded to 1 s.
  page.bufferedAmount = 100000;
  let err = null;
  try { P.feed(-20, 60); } catch (e) { err = e; }
  eq('no binary while backed up', page.sent.filter(m => m.binary).length, 0);
  page.bufferedAmount = 0;
  try { P.feed(-20, 1); P.feed(-20, 5); } catch (e) { err = err || e; }
  eq('no exception after backpressure', err && String(err), null);
  const bins = page.sent.filter(m => m.binary).map(m => m.binary);
  eq('every batch is 100 ms of PCM16', [...new Set(bins)], [3200]);
  eq('backlog bounded to 1 s (+ the new frames)', bins.length <= 12 && bins.length >= 2, true);
  const before = bins.length;
  P.feed(-20, 5);
  eq('audio keeps flowing after the drain', page.sent.filter(m => m.binary).length, before + 1);

  // link loss: no stale audio is kept for the next connection
  page.onclose({code: 1006});
  P.feed(-20, 20);
  await tick(1100);
  const page2 = P.pages()[1];
  page2.readyState = 1; page2.onopen();
  P.msg(page2, {t: 'hello', v: 2, epoch: 's2', cfg: {stt_route: 'server', window_s: 30}});
  P.feed(-20, 5);
  eq('only fresh audio after the reconnect', page2.sent.filter(m => m.binary).map(m => m.binary), [3200]);
  await probeWhileSpeaking();
  T.done();
}
// D1: a reply plays while the gate sleeps (muted while thinking, unmuted mid-reply). A voiced sound with no wake word
// opens a probe; render() must not promote it to 'speaking' and then a window: only the probe's own audio leaves, and
// with no wake frame nothing is committed.
async function probeWhileSpeaking() {
  const bin = (turn, seq) => { const b = new ArrayBuffer(10), v = new DataView(b); v.setUint8(0, 1); v.setUint32(1, turn); v.setUint16(5, seq); return b; };
  const track = () => ({label: 'm', readyState: 'live', muted: false, addEventListener() {}, stop() { this.readyState = 'ended'; }});
  const navigator = {userAgent: 'x', geolocation: {watchPosition() {}}, mediaSession: {setActionHandler() {}},
    mediaDevices: {getUserMedia: async () => { const t = track(); return {getAudioTracks: () => [t], getTracks: () => [t]}; }}};
  const P = loadPage({navigator, RIDE_SETUP_WAIT: {hello: 300}});
  await P.els.b1.onclick();
  const page = P.pages()[0]; page.readyState = 1; page.onopen();
  P.msg(page, {t: 'hello', epoch: 'e1', cfg: {stt_route: 'server', play_buffer_ms: 0, wake: {engine: 'transcript'}}});
  for (let i = 0; i < 150 && P.run('gate.opts().engine') !== 'transcript'; i++) await tick(20);
  P.run('setMuted(true)');
  P.msg(page, {t: 'state', s: 'thinking', turn: 7, epoch: 'e1'});
  page.onmessage({data: bin(7, 0)});
  P.run('setMuted(false)');
  for (let i = 0; i < 3; i++) { P.audio.fire('playing'); P.audio.fire('ended'); }   // thinking, mute, unmute cues end
  P.feed(-70, 20);                       // the cue tail
  eq('D1 a reply plays while the gate sleeps', [P.run('C.display()'), P.run('gate.state()')], ['speaking', 'sleep']);
  const from = page.sent.length;
  P.feed(-20, 20);                       // her voice through the speaker, or a passenger: voiced, no wake word
  eq('D1 voiced sound during the reply: a probe, not speaking', P.run('gate.state()'), 'probe');
  P.feed(-70, 100 + 75);                 // 2000 ms public VAD hangover + the 1.5 s probe tail
  const probeBins = page.sent.slice(from).filter(m => m.binary).length;
  P.feed(-70, 150);                      // the reply goes on: 5.5 s since the sound
  eq('D1 0 PCM frames after the probe tail without a wake', [page.sent.slice(from).filter(m => m.binary).length - probeBins, P.run('gate.state()')], [0, 'probe']);
  P.msg(page, {t: 'end', turn: 7, epoch: 'e1'}); P.audio.fire('playing'); P.audio.fire('ended');
  eq('D1 reply ended: still a probe, no conversation window', P.run('gate.state()'), 'probe');
  const afterReply = page.sent.length;
  P.feed(-70, 10);
  P.run("onCommitted('turn left at the lights', 'en', 1)");   // anything committed now has no wake word in it
  P.feed(-70, 100);
  const sent = page.sent.slice(from);
  eq('D1 no wake frame: asleep, no PCM after the reply, nothing committed',
     [P.run('gate.state()'), page.sent.slice(afterReply).filter(m => m.binary).length,
      sent.filter(m => m.t === 'committed').length, sent.filter(m => m.t === 'wake').length], ['sleep', 0, 0, 0]);
}
main().catch(e => T.crash(e));
