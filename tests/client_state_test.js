// Node check of the page's protocol v2 / audio-queue state machine: loads static/client_state.js (the same file
// index.html loads) with a fake <audio>, fake blob URLs, fake timers and a fake send. Run by tests/test_client.py.
// Prints one JSON line: {checks: n, failures: [...]}.
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm');
const ctx = {Set, Promise, Math, DataView, Uint8Array, ArrayBuffer};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'static', 'client_state.js'), 'utf8'), ctx);

class FakeAudio {
  constructor() { this.l = {}; this.src = ''; this.paused = true; }
  addEventListener(t, f) { (this.l[t] = this.l[t] || []).push(f); }
  fire(t) { (this.l[t] || []).forEach(f => f()); }
  play() {                                 // this.fail: a list of outcomes for the next play() calls ('reject')
    this.paused = false;
    return this.fail && this.fail.length && this.fail.shift() === 'reject' ? Promise.reject({name: 'NotAllowedError'}) : Promise.resolve();
  }
  pause() { this.paused = true; }
}
function frame(type, turn, seq, payload) {     // the server's binary audio frame
  const b = new ArrayBuffer(7 + payload.length), v = new DataView(b);
  v.setUint8(0, type); v.setUint32(1, turn); v.setUint16(5, seq);
  new Uint8Array(b, 7).set([...payload].map(c => c.charCodeAt(0)));
  return b;
}
function page(rnd) {
  const p = {audio: new FakeAudio(), sent: [], made: [], revoked: [], renders: 0, n: 0, timers: [], cues: []};
  p.c = ctx.RideClient({
    audio: p.audio,
    makeUrl: (bytes, mime) => { const u = `blob:${++p.n}:${mime}:${String.fromCharCode(...bytes)}`; p.made.push(u); return u; },
    cueUrl: path => 'cache:' + path,
    revoke: u => p.revoked.push(u),
    send: o => p.sent.push(o),
    render: () => { p.renders++; },
    timer: (f, ms, kind) => { p.timers.push({f, ms, kind, live: true}); return p.timers.length - 1; },
    clearTimer: id => { p.timers[id].live = false; },
    random: () => (rnd === undefined ? 0 : rnd),
    cue: on => p.cues.push(on),                // Fix A: the page holds the mic gate while a cue or clip plays
  });
  p.msg = (epoch, m) => p.c.onMessage(Object.assign({epoch}, m));
  p.bin = (turn, seq, payload, type) => p.c.onBinary(frame(type || 1, turn, seq, payload || 'ID3x'));
  p.end = () => { p.audio.fire('playing'); p.audio.fire('ended'); };   // the current item plays to its end
  p.drain = () => { for (let i = 0; i < 20 && p.c.state().playing; i++) p.end(); };
  p.fireTimers = (upTo, kind) => p.timers.forEach(t => { if (t.live && t.ms <= upTo && t.kind === kind) { t.live = false; t.f(); } });
  // the sections below test other behaviour with the playback buffer off (hello.cfg.play_buffer_ms: 0); section 16 tests the hold
  p.up = epoch => { p.c.onOpen(); p.msg(null, {t: 'hello', epoch, cfg: {play_buffer_ms: 0}}); };
  return p;
}
let checks = 0;
const failures = [];
function eq(name, got, want) {
  checks++;
  if (JSON.stringify(got) !== JSON.stringify(want)) failures.push({name, got, want});
}

// 1. state -> cue and colour mapping (D2), one turn end to end; ready cue completes before the first segment;
//    done again after idle
{
  const p = page();
  p.up('b1-1');
  p.c.recognition(true);                     // the page reports recognition on hello (index.html listening())
  eq('1 sleep is pure black', [p.c.display(), p.c.colour()], ['sleep', '#000000']);
  p.c.setGate('awake');
  eq('1 awake = dim red', [p.c.display(), p.c.colour()], ['listening', '#2a0000']);
  p.c.wake();
  eq('1 wake cue (random pick a)', p.c.state().log, ['cue:wake']);
  eq('1 wake cue url', p.audio.src, 'cache:cues/wake_a.m4a');
  p.end();
  p.msg('b1-1', {t: 'state', s: 'thinking', turn: 1});
  eq('1 thinking = blue + cue', [p.c.display(), p.c.colour(), p.audio.src], ['thinking', '#00002e', 'cache:cues/thinking.m4a']);
  p.msg('b1-1', {t: 'state', s: 'extended', turn: 1});
  eq('1 extended frame: still the thinking orb state, no violet (WP1/D3)', [p.c.display(), p.c.colour()], ['thinking', '#00002e']);
  p.msg('b1-1', {t: 'state', s: 'ready', turn: 1});
  p.bin(1, 0, 'A'); p.bin(1, 1, 'B');
  eq('1 queue order while thinking cue plays', p.c.state().queue, ['cue:done', 'seg:1.0', 'seg:1.1']);
  eq('1 speaking = green', [p.c.display(), p.c.colour()], ['speaking', '#002200']);
  p.end();                                   // thinking cue ends -> done (ready); no extended cue
  eq('1 ready cue playing, segment waits', [p.audio.src, p.sent], ['cache:cues/done.m4a', []]);
  p.end();                                   // done ends -> first segment starts
  eq('1 first segment after the ready cue', p.audio.src.startsWith('blob:1:audio/mpeg'), true);
  p.audio.fire('playing');
  eq('1 play reported once for seq 0', p.sent, [{t: 'play', turn: 1, seq: 0}]);
  p.msg('b1-1', {t: 'end', turn: 1, segs: 2});
  p.audio.fire('ended');                     // seg 0 done -> seg 1
  eq('1 no idle before the last segment', p.sent.filter(m => m.t === 'idle'), []);
  p.end();                                   // seg 1 done -> idle + done cue
  eq('1 idle after the last segment', p.sent.filter(m => m.t === 'idle'), [{t: 'idle', turn: 1, heard: true}]);
  eq('1 done cue after idle', p.audio.src, 'cache:cues/done.m4a');
  p.end();
  p.msg('b1-1', {t: 'state', s: 'listening'});
  eq('1 full order', p.c.state().log, ['cue:wake', 'cue:thinking', 'cue:done', 'seg:1.0', 'seg:1.1', 'cue:done']);
  eq('1 back to dim red (window open)', p.c.display(), 'listening');
  eq('1 segment urls revoked, cue urls kept', p.revoked.length, 2);
  p.c.setGate('sleep');
  eq('1 sleep again', p.c.colour(), '#000000');
}

// 2. WP1 (D3, P4): extended/working frames are part of thinking. Whatever mix of thinking, extended, working and a
//    repeated thinking (the server re-sends it after 'extended', see docs/evidence/wp0_baseline) arrives within one
//    turn, display() stays 'thinking' and no extended cue or working clip plays; a new turn id cues thinking once.
{
  const p = page(0.99);
  p.up('b1-1');
  p.c.wake();
  eq('2 random pick b', p.audio.src, 'cache:cues/wake_b.m4a');
  p.drain();
  const seen = [];
  for (const s of ['thinking', 'extended', 'extended', 'working', 'thinking', 'working', 'extended', 'thinking']) {
    p.msg('b1-1', {t: 'state', s, turn: 4});
    seen.push(p.c.display());
    p.drain();
  }
  eq('2 one turn: display stays thinking for every frame order', seen, Array(8).fill('thinking'));
  eq('2 one turn: only the wake and one thinking cue', p.c.state().log, ['cue:wake', 'cue:thinking']);
  p.msg('b1-1', {t: 'state', s: 'extended', turn: 5});
  eq('2 extended of a turn whose thinking was never seen: still thinking', p.c.display(), 'thinking');
  p.drain();
  p.msg('b1-1', {t: 'state', s: 'thinking', turn: 5}); p.drain();
  eq('2 new turn id: one thinking cue, never an extended cue or working clip',
     p.c.state().log, ['cue:wake', 'cue:thinking', 'cue:thinking']);
  p.msg('b1-1', {t: 'state', s: 'listening'});
  eq('2 listening clears thinking', p.c.display() === 'thinking', false);
}

// 2b. WP3 (P2): the transcript fallback's 'probe' gate (VAD speech sent to STT to look for the wake word) is still
//     asleep for the rider: display() says 'sleep' and the colour stays black, so a bystander, a TV or a keyboard can
//     never make the page look like it is listening. Awake/window/speaking still show their states.
{
  const p = page();
  p.up('b1-1');
  p.c.recognition(true);
  p.c.setGate('sleep');
  eq('2b sleep', p.c.display(), 'sleep');
  p.c.setGate('probe');
  eq('2b probe renders as sleep, black', [p.c.display(), p.c.colour()], ['sleep', '#000000']);
  p.c.setGate('awake');
  eq('2b awake is listening', p.c.display(), 'listening');
  p.c.setGate('probe');
  p.msg('b1-1', {t: 'state', s: 'thinking', turn: 1});
  eq('2b thinking wins over a probe gate', p.c.display(), 'thinking');
  p.msg('b1-1', {t: 'state', s: 'listening'});
  p.c.setGate('window');
  eq('2b window is listening', p.c.display(), 'listening');
  p.c.setGate('probe');
  p.bin(2, 0, 'A');
  eq('2b speaking wins over a probe gate', p.c.display(), 'speaking');
}

// 3. local clips on WS close: reconnecting after 5 s down, offline after 60 s, back on reconnect; no reply audio
//    of the old connection survives
{
  const p = page();
  p.up('b1-1');
  p.msg('b1-1', {t: 'state', s: 'ready', turn: 2});
  p.bin(2, 0, 'A');
  p.end();
  p.audio.fire('playing');
  p.c.onClose();
  eq('3 after close: amber, reply dropped', [p.c.display(), p.c.pulse(), p.c.state().spkTurn, p.c.state().queued, p.audio.paused],
     ['reconnecting', true, null, 0, true]);
  eq('3 no clip before 5 s', p.c.state().log.filter(x => x.startsWith('clip')), []);
  p.fireTimers(5000);
  eq('3 reconnecting clip plays with no connection', p.audio.src, 'cache:clips/reconnecting.m4a');
  p.fireTimers(60000);
  p.drain();
  eq('3 offline clip after 60 s', [p.c.display(), p.c.state().log.filter(x => x.startsWith('clip'))],
     ['offline', ['clip:reconnecting', 'clip:offline']]);
  p.up('b1-2');
  eq('3 link up alone is not "back" (AUD-V2-01)', p.c.state().log.filter(x => x === 'clip:back'), []);
  p.c.recognition(false);
  eq('3 still no back while recognition is down', p.c.state().log.filter(x => x === 'clip:back'), []);
  p.c.recognition(true);
  eq('3 back clip once recognition works again', p.audio.src, 'cache:clips/back.m4a');
  p.drain();
  p.c.onClose(); p.up('b1-3'); p.c.recognition(true);   // a short blip (< 5 s): no clips at all
  p.fireTimers(60000);
  eq('3 short blip is silent', p.c.state().log.filter(x => x.startsWith('clip')), ['clip:reconnecting', 'clip:offline', 'clip:back']);
}

// 4. epochs: frames of another connection are ignored; a new-epoch hello clears turn state (ids restart)
{
  const p = page();
  p.up('b1-1');
  p.bin(1, 0, 'A');
  p.msg('b1-1', {t: 'stop', turn: 1});
  eq('4 stopped', [p.c.display(), p.c.state().dropTurns], ['sleep', [1]]);
  p.bin(1, 1, 'B');
  eq('4 dropped turn stays silent', [p.c.state().queued, p.c.state().playing], [0, false]);
  p.msg(null, {t: 'hello', epoch: 'b2-1', cfg: {play_buffer_ms: 0}});
  eq('4 state cleared', [p.c.state().dropTurns, p.c.state().endedTurns], [[], []]);
  p.msg('b1-1', {t: 'state', s: 'thinking', turn: 9});     // old epoch
  eq('4 old frame ignored', p.c.state().serverState, 'listening');
  p.bin(1, 0, 'C');
  eq('4 turn 1 plays again', [p.c.display(), p.c.state().playing], ['speaking', true]);
  p.c.onClose();
  p.bin(3, 0, 'D');                                          // binary before any hello: ignored
  eq('4 no epoch, no audio', p.c.state().queued, 0);
}

// 5. barge-in stop and stale discard drop the turn's audio and its queued cues; nothing else
{
  const p = page();
  p.up('b1-1');
  p.c.wake();
  p.msg('b1-1', {t: 'state', s: 'ready', turn: 3});
  p.bin(3, 0, 'A'); p.bin(3, 1, 'B');
  p.msg('b1-1', {t: 'discard', turn: 3});
  eq('5 discard leaves the wake cue only', [p.c.state().queue, p.audio.src], [[], 'cache:cues/wake_a.m4a']);
  p.drain();
  eq('5 no idle, no done for a discarded turn', [p.sent, p.c.state().log], [[], ['cue:wake']]);
}

// 6. binary frame decoding
{
  const f = ctx.rideDecodeFrame(frame(2, 70000, 513, 'RIFF'));
  eq('6 decode', [f.mime, f.turn, f.seq, f.bytes.length], ['audio/wav', 70000, 513, 4]);
  eq('6 short frame', ctx.rideDecodeFrame(new ArrayBuffer(3)), null);
  eq('6 unknown type', ctx.rideDecodeFrame(frame(9, 1, 0, 'x')), null);
}

// 7. hello cfg cue map overrides the default (user cue folder)
{
  const p = page();
  p.c.onOpen();
  p.msg(null, {t: 'hello', epoch: 'b1-1', cfg: {play_buffer_ms: 0, cues: {wake: ['my/w.m4a'], thinking: 'my/t.m4a', done: 'my/d.m4a'}}});
  p.c.wake();
  eq('7 custom cue', p.audio.src, 'cache:my/w.m4a');
  p.c.noMic(); p.drain();
  eq('7 nomic clip', p.c.state().log, ['cue:wake', 'clip:nomic']);
}

// 8. AUD-V2-02: after a quota notice, 'back' plays only when recognition really works again, once
{
  const p = page();
  p.up('b1-1'); p.c.recognition(true);
  p.msg('b1-1', {t: 'state', s: 'quota'});
  p.c.recognition(false);
  p.drain();
  p.c.recognition(true); p.drain();
  p.c.recognition(true); p.c.recognition(false); p.c.recognition(true); p.drain();
  eq('8 quota then one back', p.c.state().log.filter(x => x.startsWith('clip')), ['clip:quota', 'clip:back']);
  p.c.onClose(); p.fireTimers(5000); p.drain();
  p.up('b1-2'); p.c.onClose(); p.up('b1-3');           // link flaps, recognition never confirmed
  p.fireTimers(60000); p.drain();
  eq('8 no back without confirmed recognition', p.c.state().log.filter(x => x.startsWith('clip')), ['clip:quota', 'clip:back', 'clip:reconnecting']);
}

// 9. AUD-V2-04: a failed playback is never a heard answer and never wedges the queue
const flush = () => new Promise(r => setTimeout(r, 0));
(async () => {
  {   // a rejected play() is retried once; the second try plays and counts
    const p = page();
    p.up('b1-1');
    p.audio.fail = ['reject'];
    p.bin(1, 0, 'A'); p.msg('b1-1', {t: 'end', turn: 1, segs: 1});
    await flush(); p.fireTimers(1000, 'media');
    p.end();
    eq('9a retried once, heard', [p.c.state().log, p.sent.filter(m => m.t === 'idle')], [['seg:1.0', 'seg:1.0', 'cue:done'], [{t: 'idle', turn: 1, heard: true}]]);
  }
  {   // play() keeps failing: reported, no done cue, the queue moves on to the next item
    const p = page();
    p.up('b1-1');
    p.audio.fail = ['reject', 'reject'];
    p.bin(2, 0, 'A'); p.msg('b1-1', {t: 'end', turn: 2, segs: 1}); p.msg('b1-1', {t: 'state', s: 'quota'});
    await flush(); p.fireTimers(1000, 'media'); await flush();
    eq('9b play_error + idle heard:false', p.sent.filter(m => m.t === 'play_error' || m.t === 'idle').map(m => [m.t, m.turn, m.heard]),
       [['play_error', 2, undefined], ['idle', 2, false]]);
    eq('9b no done cue, next item plays', [p.c.state().log, p.audio.src], [['seg:2.0', 'seg:2.0', 'clip:quota'], 'cache:clips/quota.m4a']);
  }
  {   // a media error after play() resolved (missing cue file): skipped after one retry, the reply still plays
    const p = page();
    p.up('b1-1');
    p.c.wake(); p.bin(3, 0, 'A');
    p.audio.fire('error'); p.fireTimers(1000, 'media'); p.audio.fire('error');
    eq('9c reply plays after the broken cue', [p.c.state().log, p.c.state().playing], [['cue:wake', 'cue:wake', 'seg:3.0'], true]);
  }
  {   // a stalled item (never 'playing', never 'ended') is cut by the watchdog
    const p = page();
    p.up('b1-1');
    p.c.wake(); p.bin(4, 0, 'A');
    p.fireTimers(10000, 'media'); p.fireTimers(1000, 'media'); p.fireTimers(10000, 'media');
    eq('9d watchdog moves on', p.c.state().log, ['cue:wake', 'cue:wake', 'seg:4.0']);
  }

  // 10. fix round 2 (R3): one failure transition per media attempt; retired playback is stopped; late events of
  //     a retired attempt are not completions
  const retries = p => p.timers.filter(t => t.live && t.kind === 'media' && t.ms === 300).length;
  {   // the same attempt is rejected AND reports a media error: one retry, not two
    const p = page();
    p.up('b1-1');
    p.audio.fail = ['reject'];
    p.bin(5, 0, 'A'); p.msg('b1-1', {t: 'end', turn: 5, segs: 1});
    p.audio.fire('error');
    await flush();
    eq('10a reject + error: one retry scheduled', retries(p), 1);
    p.fireTimers(1000, 'media');
    eq('10a the item is started twice, not three times', p.c.state().log, ['seg:5.0', 'seg:5.0']);
  }
  {   // repeated media errors before the retry timer fires: still one retry
    const p = page();
    p.up('b1-1');
    p.c.wake();
    p.audio.fire('error'); p.audio.fire('error'); p.audio.fire('error');
    eq('10b repeated errors: one retry scheduled', retries(p), 1);
    p.fireTimers(1000, 'media');
    eq('10b one retry started', p.c.state().log, ['cue:wake', 'cue:wake']);
  }
  {   // a stalled segment is retired by the watchdog; its late 'ended' is not a heard answer
    const p = page();
    p.up('b1-1');
    p.bin(6, 0, 'A'); p.msg('b1-1', {t: 'end', turn: 6, segs: 1});
    p.fireTimers(8000, 'media');                               // start watchdog: retired, retry pending
    eq('10c retired attempt stopped', p.audio.paused, true);
    p.audio.fire('ended');                                     // late event of the retired attempt
    eq('10c late ended: no idle, still the current item', [p.sent.filter(m => m.t === 'idle'), p.c.state().playing], [[], true]);
    p.fireTimers(1000, 'media');                               // the retry starts and plays to its end
    p.end();
    eq('10c the retry is the heard answer', [p.c.state().log, p.sent.filter(m => m.t === 'idle')],
       [['seg:6.0', 'seg:6.0', 'cue:done'], [{t: 'idle', turn: 6, heard: true}]]);
  }
  {   // terminal watchdog failure: the media element is paused, not left running
    const p = page();
    p.up('b1-1');
    p.bin(7, 0, 'A'); p.msg('b1-1', {t: 'end', turn: 7, segs: 1});
    p.fireTimers(8000, 'media'); p.fireTimers(1000, 'media');
    p.audio.fire('playing');                                   // the retry starts, then hangs past its watchdog
    p.fireTimers(60000, 'media');
    eq('10d terminal failure: paused, reported', [p.audio.paused, p.sent.filter(m => m.t === 'idle').map(m => m.heard)], [true, [false]]);
  }

  // 11. WP2 (D3, P3): the doo-doo tone belongs to thinking, not to 'extended'. It starts tone_first_s (default 1.2 s)
  //     after the 'thinking' frame (an 'extended'/'working' frame with no 'thinking' before it starts it the same way),
  //     then repeats every 4-6 s, at the cue gain, until reply audio / ready / end / barge-in / discard / listening /
  //     a wake / a lost link. A reply that comes first plays no tone at all. Cues never overlap a tone tick.
  function tonePage(rnd) {
    const p = page(rnd);
    p.tones = []; p.toneStops = 0;
    p.c = ctx.RideClient({
      audio: p.audio, makeUrl: () => 'blob:x', cueUrl: path => 'cache:' + path, revoke() {}, send: o => p.sent.push(o),
      render() {}, random: () => (rnd === undefined ? 0 : rnd),
      timer: (f, ms, kind) => { p.timers.push({f, ms, kind, live: true}); return p.timers.length - 1; },
      clearTimer: id => { p.timers[id].live = false; },
      tone: g => p.tones.push(g), toneStop: () => { p.toneStops++; },
    });
    p.up('b1-1');
    return p;
  }
  const liveTones = p => p.timers.filter(t => t.live && t.kind === 'tone');
  {   // a short turn: the reply arrives before the lead-in is over: no doo-doo, no live tone timer left
    const p = tonePage();
    p.msg('b1-1', {t: 'state', s: 'thinking', turn: 1}); p.drain();
    eq('11a lead-in is scheduled at 1.2 s (default), no jitter', liveTones(p).map(t => t.ms), [1200]);
    p.msg('b1-1', {t: 'state', s: 'ready', turn: 1}); p.bin(1, 0, 'A'); p.msg('b1-1', {t: 'end', turn: 1, segs: 1}); p.drain();
    p.fireTimers(60000, 'tone');
    eq('11a short turn: nothing played, nothing pending', [p.tones, liveTones(p).length], [[], 0]);
  }
  {   // a long turn: first tone at the lead-in, then every 4-6 s at the cue gain, one loop, no spoken clip
    const p = tonePage(0.5);
    p.msg('b1-1', {t: 'hello', epoch: 'b1-1', cfg: {play_buffer_ms: 0, cue_gain: 0.3}});
    p.msg('b1-1', {t: 'state', s: 'thinking', turn: 2}); p.drain();
    eq('11b first tone due after the lead-in', liveTones(p).map(t => t.ms), [1200]);
    p.fireTimers(1200, 'tone');
    eq('11b first tone played at cue_gain', p.tones, [0.3]);
    eq('11b next one due in 4-6 s', liveTones(p).map(t => t.ms), [5000]);
    p.msg('b1-1', {t: 'state', s: 'extended', turn: 2}); p.msg('b1-1', {t: 'state', s: 'working', turn: 2});
    p.msg('b1-1', {t: 'state', s: 'thinking', turn: 2}); p.drain();
    eq('11b extended/working/repeated thinking: no spoken clip, still one loop', [p.c.state().log.filter(x => x.startsWith('clip:')), liveTones(p).length], [[], 1]);
    p.fireTimers(6000, 'tone');
    eq('11b repeats', p.tones.length, 2);
  }
  {   // tone_first_s from the hello cfg
    const p = tonePage();
    p.msg('b1-1', {t: 'hello', epoch: 'b1-1', cfg: {play_buffer_ms: 0, tone_first_s: 3}});
    p.msg('b1-1', {t: 'state', s: 'thinking', turn: 2});
    eq('11e tone_first_s from cfg', liveTones(p).map(t => t.ms), [3000]);
    const q = tonePage();
    q.msg('b1-1', {t: 'hello', epoch: 'b1-1', cfg: {play_buffer_ms: 0, tone_first_s: 'junk'}});
    q.msg('b1-1', {t: 'state', s: 'thinking', turn: 2});
    eq('11e junk cfg falls back to 1.2 s', liveTones(q).map(t => t.ms), [1200]);
  }
  {   // an 'extended' frame with no earlier 'thinking' starts the same lead-in; a new turn id restarts it
    const p = tonePage();
    p.msg('b1-1', {t: 'state', s: 'extended', turn: 3});
    eq('11f extended alone: lead-in', liveTones(p).map(t => t.ms), [1200]);
    p.msg('b1-1', {t: 'state', s: 'thinking', turn: 4});
    eq('11f new turn id: one loop, lead-in again', liveTones(p).map(t => t.ms), [1200]);
    p.drain();                                                  // the thinking cue ends first
    p.fireTimers(1200, 'tone');
    eq('11f played once', p.tones.length, 1);
  }
  {   // once reply audio has started for the turn, a late thinking/extended frame does not start the tone again
    const p = tonePage();
    p.msg('b1-1', {t: 'state', s: 'thinking', turn: 5}); p.drain();
    p.bin(5, 0, 'A');
    p.msg('b1-1', {t: 'state', s: 'thinking', turn: 5}); p.msg('b1-1', {t: 'state', s: 'extended', turn: 5});
    eq('11g no tone while/after the reply speaks', liveTones(p).length, 0);
  }
  {   // a tick while a cue or clip is playing stays silent (no overlap) but the loop continues
    const p = tonePage();
    p.msg('b1-1', {t: 'state', s: 'thinking', turn: 3});       // the thinking cue is still playing
    p.fireTimers(1200, 'tone');
    eq('11c silent while a cue plays, loop continues', [p.tones.length, liveTones(p).length], [0, 1]);
  }
  for (const [why, act] of [
    ['audio frame', p => p.bin(4, 0, 'A')],
    ['ready', p => p.msg('b1-1', {t: 'state', s: 'ready', turn: 4})],
    ['turn end', p => p.msg('b1-1', {t: 'end', turn: 4, segs: 0})],
    ['barge-in stop', p => p.msg('b1-1', {t: 'stop', turn: 4})],
    ['stale discard', p => p.msg('b1-1', {t: 'discard', turn: 4})],
    ['listening', p => p.msg('b1-1', {t: 'state', s: 'listening'})],
    ['wake', p => p.c.wake()],
    ['link lost', p => p.c.onClose()],
    ['new epoch', p => p.msg(null, {t: 'hello', epoch: 'b2-1', cfg: {play_buffer_ms: 0}})],
  ]) {
    const p = tonePage();
    p.msg('b1-1', {t: 'state', s: 'thinking', turn: 4}); p.drain();
    p.fireTimers(1200, 'tone');
    const before = p.tones.length, stops = p.toneStops;
    act(p);
    p.fireTimers(60000, 'tone');
    eq('11d stops at once: ' + why, [before, p.tones.length, liveTones(p).length, p.toneStops > stops], [1, 1, 0, true]);
  }
  // 12. Round 3 (A2; first built as WP4): the soft mute (the MUTE button). A 'muted' display state with its own dim colour; one short cue per toggle, next in
  //     the queue (never over a reply); a reply already asked for still shows speaking/thinking; link states win.
  {
    const p = page();
    p.up('m-1'); p.c.recognition(true);
    const cuesOf = () => p.c.state().log.filter(x => x === 'cue:mute' || x === 'cue:unmute');
    p.c.setMuted(true);
    const col = p.c.colour();
    eq('12 muted: display and a dim colour of its own', [p.c.display(), typeof col === 'string' && /^#[0-9a-f]{6}$/.test(col),
       Object.entries(vm.runInContext('RIDE_COLOURS', ctx)).filter(([k, v]) => k !== 'muted' && v === col).length, p.c.pulse()], ['muted', true, 0, false]);
    eq('12 muted colour is dim (every channel < 0x30)', [1, 3, 5].every(i => parseInt(col.slice(i, i + 2), 16) < 0x30), true);
    eq('12 mute cue plays', [cuesOf(), p.audio.src], [['cue:mute'], 'cache:cues/mute.m4a']);
    p.end();
    p.c.setMuted(true);
    eq('12 muting twice: one cue', cuesOf(), ['cue:mute']);
    p.c.setGate('awake');
    eq('12 muted beats listening', p.c.display(), 'muted');
    p.msg('m-1', {t: 'state', s: 'thinking', turn: 1}); p.drain();
    eq('12 a turn in flight still shows thinking', p.c.display(), 'thinking');
    p.bin(1, 0, 'A');
    eq('12 and speaking', p.c.display(), 'speaking');
    p.c.setMuted(false);
    eq('12 unmute cue waits for the reply segment, then plays next', [p.c.state().queue[0], p.c.state().playing], ['cue:unmute', true]);
    p.c.setMuted(true);
    p.end();
    p.msg('m-1', {t: 'end', turn: 1, segs: 1});
    p.drain();
    p.msg('m-1', {t: 'state', s: 'listening'});
    eq('12 after the turn: muted again', p.c.display(), 'muted');
    p.c.onClose();
    eq('12 link lost: reconnecting wins over muted', p.c.display(), 'reconnecting');
    const q = page();
    q.c.onOpen(); q.msg(null, {t: 'hello', epoch: 'm-2', cfg: {play_buffer_ms: 0, cues: {wake: ['cues/wake_a.m4a'], thinking: 'cues/thinking.m4a', done: 'cues/done.m4a'}}});
    q.c.setMuted(true);
    eq('12 a server cue map without mute/unmute: the default file plays', q.audio.src, 'cache:cues/mute.m4a');
    q.end(); q.c.setMuted(false);
    eq('12 unmute: sleep again, unmute cue', [q.c.display(), q.audio.src], ['sleep', 'cache:cues/unmute.m4a']);
  }
  // 13. WP6: the reset cues. One short cue when the bar has been held (ok) or the server says it failed; from the default
  //     files when the server's cue map has none; a queued reset cue survives the epoch change of a reconnect.
  {
    const p = page();
    p.c.onOpen(); p.msg(null, {t: 'hello', epoch: 'r-1', cfg: {play_buffer_ms: 0, cues: {wake: ['cues/wake_a.m4a'], thinking: 'cues/thinking.m4a', done: 'cues/done.m4a'}}});
    p.c.resetCue(true);
    eq('13 reset cue (default file)', [p.c.state().log.slice(-1)[0], p.audio.src], ['cue:reset', 'cache:cues/reset.m4a']);
    p.c.resetCue(false);
    eq('13 failure cue queued next', p.c.state().queue[0], 'cue:reset_failed');
    p.c.onClose(); p.c.onOpen(); p.msg(null, {t: 'hello', epoch: 'r-2', cfg: {play_buffer_ms: 0}});
    eq('13 the queued failure cue survives a new epoch', p.c.state().queue.includes('cue:reset_failed'), true);
    p.end();
    eq('13 then plays', p.audio.src, 'cache:cues/reset_failed.m4a');
  }
  // 14. Fix A (item 7): a cue or clip that plays holds the mic (io.cue(true)), its end releases it (io.cue(false));
  //     reply segments never do (barge-in stays); a failed cue releases it too
  {
    const p = page();
    p.up('h-1');
    p.c.wake();
    eq('14 wake cue starts: hold', p.cues, [true]);
    p.end();
    eq('14 wake cue ends: release', p.cues, [true, false]);
    p.msg('h-1', {t: 'state', s: 'thinking', turn: 1});
    p.end();
    eq('14 thinking cue: hold and release', p.cues, [true, false, true, false]);
    p.cues.length = 0;
    p.bin(1, 0); p.bin(1, 1);
    p.end(); p.end();
    eq('14 reply segments: no hold', p.cues, []);
    p.msg('h-1', {t: 'end', turn: 1});
    eq('14 done cue after the reply: hold', p.cues, [true]);
    p.end();
    eq('14 done cue ends: release', p.cues, [true, false]);
    p.cues.length = 0;
    p.c.noMic();
    eq('14 a clip holds too', p.cues, [true]);
    p.audio.fire('error');                      // retry: still held, no second hold call
    p.fireTimers(300, 'media');
    eq('14 retry keeps the hold', p.cues, [true]);
    p.audio.fire('error');                      // second failure: skipped, released
    eq('14 a failed clip releases', p.cues, [true, false]);
    eq('14 queue empty', p.c.state().playing, false);
  }
  {                                              // 15: scaled cues are faded at both ends (no click at start or stop)
    const rate = 44100, n = rate;                // one second of a constant, non-zero level: the worst case for a click
    const buf = {numberOfChannels: 1, sampleRate: rate, length: n, getChannelData: () => new Float32Array(n).fill(0.8)};
    const v = new DataView(ctx.rideScaledWav(buf, 0.5));
    const at = i => v.getInt16(44 + i * 2, true), mid = Math.round(0.4 * 32767);
    eq('15 first sample is zero', at(0), 0);
    eq('15 last sample is zero', at(n - 1), 0);
    eq('15 the fade is gone by 5 ms', at(Math.round(rate * 0.005)), mid);
    eq('15 the middle is at gain', at(Math.round(n / 2)), mid);
    const tiny = new DataView(ctx.rideScaledWav({numberOfChannels: 1, sampleRate: rate, length: 20, getChannelData: () => new Float32Array(20).fill(0.8)}, 1));
    eq('15 a very short cue stays finite', [0, 10, 19].every(i => Number.isFinite(tiny.getInt16(44 + i * 2, true))), true);
  }
  {                                              // 16: play_buffer_ms: only seg 0 of a reply waits before play()
    const pb = (cfg, epoch) => { const p = page(); p.c.onOpen(); p.msg(null, {t: 'hello', epoch: epoch || 'pb-1', cfg}); return p; };
    const holds = p => p.timers.filter(t => t.kind === 'media' && t.ms === 250 && t.live);
    const none = {live: 'no hold timer', f() {}};       // stands in for a missing hold so a red run reports checks, not a crash
    const hold = p => holds(p)[0] || none;
    const plays = p => p.audio.src;
    {                                            // a: seg 0 is held 250 ms (default, cfg unset), seg 1 is not
      const p = pb({});
      p.bin(1, 0, 'A'); p.bin(1, 1, 'B');
      eq('16a seg 0 is not playing yet', [p.audio.paused, plays(p)], [true, '']);
      eq('16a one 250 ms media hold', holds(p).length, 1);
      eq('16a the START_MS watchdog has not started', p.timers.filter(t => t.live && t.ms === 8000).length, 0);
      eq('16a seg 0 is the current item while it waits', [p.c.state().playing, p.c.nowPlaying().seq], [true, 0]);
      const h0 = holds(p)[0]; if (h0) { h0.live = false; h0.f(); }
      eq('16a after the hold seg 0 plays', [p.audio.paused, plays(p).startsWith('blob:1:')], [false, true]);
      eq('16a the watchdog starts at play()', p.timers.filter(t => t.live && t.ms === 8000).length, 1);
      p.audio.fire('playing'); p.audio.fire('ended');
      eq('16a seg 1 follows with no hold', [plays(p).startsWith('blob:2:'), p.timers.filter(t => t.live && t.ms === 250).length], [true, 0]);
    }
    {                                            // b: a cue never waits, also not with a buffer set
      const p = pb({play_buffer_ms: 400});
      p.c.wake();
      eq('16b wake cue plays at once', [p.audio.paused, plays(p)], [false, 'cache:cues/wake_a.m4a']);
      eq('16b no hold timer for a cue', p.timers.filter(t => t.live && t.ms === 400).length, 0);
      p.end();
      p.msg('pb-1', {t: 'state', s: 'thinking', turn: 3});
      eq('16b the thinking cue plays at once', plays(p), 'cache:cues/thinking.m4a');
    }
    {                                            // c: a clip never waits
      const p = pb({});
      p.c.noMic();
      eq('16c clip plays at once', [p.audio.paused, plays(p)], [false, 'cache:clips/nomic.m4a']);
    }
    {                                            // d: stop during the hold cancels it, nothing plays, the queue does not wedge
      const p = pb({});
      p.bin(5, 0, 'A'); p.bin(5, 1, 'B');
      const h = hold(p);
      p.msg('pb-1', {t: 'stop', turn: 5});
      eq('16d the hold timer is cancelled', h.live, false);
      eq('16d nothing is playing, queue empty', [p.c.state().playing, p.c.state().queued, p.audio.paused], [false, 0, true]);
      h.f();                                     // even if the timer fired late, nothing starts
      eq('16d a late fire plays nothing', [p.audio.paused, plays(p)], [true, '']);
      p.bin(6, 0, 'C');                          // the queue still works: a new turn is held and then plays
      const h2 = hold(p);
      eq('16d a new reply is held again', [h2 !== none, p.audio.paused], [true, true]);
      h2.live = false; h2.f();
      eq('16d and then plays', [p.audio.paused, plays(p).startsWith('blob:')], [false, true]);
    }
    {                                            // e: discard and reset (a new epoch) during the hold cancel it too
      const p = pb({});
      p.bin(7, 0, 'A');
      const h = hold(p);
      p.msg('pb-1', {t: 'discard', turn: 7});
      eq('16e discard cancels the hold', [h.live, p.c.state().playing], [false, false]);
      const q = pb({}, 'pb-2');
      q.bin(8, 0, 'A');
      const h3 = hold(q);
      q.msg(null, {t: 'hello', epoch: 'pb-3', cfg: {}});
      eq('16e reset on a new epoch cancels the hold', [h3.live, q.c.state().playing, q.audio.paused], [false, false, true]);
      h3.f();
      eq('16e and a late fire plays nothing', q.audio.src, '');
    }
    {                                            // f: 0 disables the buffer
      const p = pb({play_buffer_ms: 0});
      p.bin(1, 0, 'A');
      eq('16f 0 = seg 0 plays at once', [p.audio.paused, p.timers.filter(t => t.live && t.kind === 'media' && t.ms !== 8000).length], [false, 0]);
    }
    {                                            // g: junk or missing falls back to 250; a number is clamped to 0..1000
      for (const [raw, want] of [['junk', 250], [null, 250], ['', 250], [-5, 0], [5000, 1000], [120, 120], ['300', 300], [NaN, 250]]) {
        const p = pb({play_buffer_ms: raw});
        p.bin(1, 0, 'A');
        const t = p.timers.filter(x => x.live && x.kind === 'media' && x.ms !== 8000);
        eq(`16g cfg ${typeof raw === 'number' && raw !== raw ? 'NaN' : JSON.stringify(raw)} -> ${want} ms`, want === 0 ? [] : [want], t.map(x => x.ms));
      }
    }
    {                                            // h: a failed play() of the held seg 0 still retries once, then is skipped (no second hold)
      const p = pb({});
      p.audio.fail = ['reject', 'reject'];
      p.bin(9, 0, 'A'); p.bin(9, 1, 'B');
      const h = hold(p); h.live = false; h.f();   // the hold fires (a fired timer is no longer live)
      await flush();
      const retry = p.timers.find(t => t.live && t.ms === 300 && t.kind === 'media') || none;
      eq('16h a rejected play() schedules the retry', retry !== none, true);
      retry.live = false; retry.f();             // the retry plays at once: seg 0 is not held a second time
      eq('16h the retry did not hold again', p.timers.filter(t => t.live && t.ms === 250).length, 0);
      await flush();
      eq('16h after two failures seg 1 plays and play_error was sent', [p.sent.filter(m => m.t === 'play_error').length, p.c.state().log.slice(-1)[0]], [1, 'seg:9.1']);
    }
  }
  console.log(JSON.stringify({checks, failures}));
})();
