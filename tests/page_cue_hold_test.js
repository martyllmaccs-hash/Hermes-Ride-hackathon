// Fix A (item 7): Ride must not hear its own cues. The real index.html scripts in node (tests/page_rig.js): while a cue
// or clip plays, the gate is held: Scribe gets zeros only, no VAD edge, no turn, and the wake spotter is not fed.
// A reply segment does not hold the mic (barge-in stays). Prints {checks, failures}.
'use strict';
const {loadPage, tick, checker} = require('./page_rig.js');
const T = checker(), {eq} = T;

async function up() {
  const P = loadPage();
  await P.els.b1.onclick();
  const page = P.pages()[0];
  page.readyState = 1; page.onopen();
  P.msg(page, {t: 'hello', epoch: 'h1', cfg: {stt_route: 'phone', play_buffer_ms: 0, wake: {engine: 'transcript'}}});
  await tick(30);
  P.run('globalThis.__stt = []; const __a = stt.audio; stt.audio = pcm => { __stt.push(Array.from(pcm)); return __a(pcm); }');
  return {P, page};
}
const vads = page => page.sent.filter(m => m.t === 'vad' || m.t === 'committed');

(async () => {
  {   // wake -> the wake cue plays -> loud frames (our own "yes" from the speaker): zeros to STT, no edge, no turn
    const {P, page} = await up();
    P.run('gate.wake(); C.wake()');
    eq('wake cue playing, gate held', [P.run('C.state().log.slice(-1)[0]'), P.run('gate.held()')], ['cue:wake', true]);
    const before = vads(page).length;
    P.feed(-15, 50);
    const got = P.run('__stt');
    eq('STT got the frames, all zeros', [got.length > 0, got.every(f => f.length === 320 && f.every(x => x === 0))], [true, true]);
    eq('no vad edge, no committed text', vads(page).length - before, 0);
    P.audio.fire('playing'); P.audio.fire('ended');
    eq('cue ended: tail still held', P.run('gate.held()'), true);
    P.feed(-15, 15);
    eq('tail over: released', P.run('gate.held()'), false);
    P.run('__stt.length = 0');
    P.feed(-15, 3);
    eq('then real audio flows again', P.run('__stt').some(f => f.some(x => x !== 0)), true);
  }
  {   // a reply segment does not hold (barge-in stays)
    const {P, page} = await up();
    P.run('gate.wake()');
    const b = new ArrayBuffer(11), v = new DataView(b); v.setUint8(0, 2); v.setUint32(1, 1); v.setUint16(5, 0);
    page.onmessage({data: b});
    eq('reply segment playing, gate not held', [P.run('C.state().log.slice(-1)[0]'), P.run('gate.held()')], ['seg:1.0', false]);
  }
  {   // asleep: a clip from the speaker never reaches the wake spotter
    const {P} = await up();
    P.run('globalThis.__fed = 0; wakeEngine = {feed() { __fed++; }, reset() {}, stop() {}}');
    P.feed(-60, 3);
    eq('spotter fed while asleep', P.run('__fed'), 3);
    P.run("C.noMic()");
    eq('clip playing: held', P.run('gate.held()'), true);
    P.feed(-15, 20);
    eq('spotter not fed while held', P.run('__fed'), 3);
    eq('still asleep, nothing opened', [P.run('gate.state()'), P.run('gate.isOpen()')], ['sleep', false]);
  }
  T.done();
})().catch(e => T.crash(e));
