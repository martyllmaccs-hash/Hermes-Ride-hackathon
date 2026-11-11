// Round 3, A2 (rider feedback, 10 Nov: "the stop mic does not work ... mute used to"): the one mic control is a one-tap
// MUTE / UNMUTE button where #stop was (same size as the reset bar). Muted is a soft, page-only state: gate asleep, STT
// closed, every worklet frame dropped before the wake spotter, the gate and the orb level (the frame counters still
// run, so the mic watchdog stays quiet), wake hits and the double tap ignored, nothing leaves the phone, a reply that
// is playing keeps playing. The orb shows a static grey ring. Unmute returns to sleep at once: no reload, the tracks
// were never stopped. Mute survives a mic revive and a WebSocket reconnect. The lock screen pause / stop buttons mute.
// The real index.html scripts in node (tests/page_rig.js). Prints {checks, failures}.
'use strict';
const fs = require('fs'), path = require('path');
const {loadPage, tick, checker} = require('./page_rig.js');
const T = checker(), {eq} = T;
const ROOT = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'static', 'index.html'), 'utf8');

function rig(over) {
  const tracks = [], handlers = {}, acs = [];
  const track = () => { const t = {label: 'fake mic', readyState: 'live', enabled: true, muted: false, stops: 0, l: {},
                                   addEventListener(n, f) { (this.l[n] = this.l[n] || []).push(f); },
                                   stop() { this.stops++; this.readyState = 'ended'; }}; tracks.push(t); return t; };
  const navigator = {userAgent: 'node-page-test', geolocation: {watchPosition() {}},
    mediaSession: {metadata: null, setActionHandler(a, f) { handlers[a] = f; }},
    mediaDevices: {getUserMedia: async () => { const t = track(); return {getAudioTracks: () => [t], getTracks: () => [t]}; }}};
  class AC {
    constructor() { this.audioWorklet = {addModule: async () => {}}; this.state = 'running'; this.closes = 0; acs.push(this); }
    createMediaStreamSource() { return {connect() {}, disconnect() {}}; }
    close() { this.state = 'closed'; this.closes++; return Promise.resolve(); }
    resume() { return Promise.resolve(); }
  }
  const P = loadPage(Object.assign({navigator, AudioContext: AC, RIDE_SETUP_WAIT: {hello: 500, revive: 30, revive_tap: 30}}, over || {}));
  let pauses = 0; P.audio.pause = () => { pauses++; };
  Object.assign(P, {tracks, handlers, acs, pauses: () => pauses});
  P.t = 10000;
  const ev = (id, x, y, target) => ({pointerId: id, isPrimary: id === 1, pointerType: 'touch', clientX: x, clientY: y, timeStamp: P.t, target: target || P.body});
  P.tap = target => { P.fire('pointerdown', ev(1, 100, 300, target)); P.t += 60; P.fire('pointerup', ev(1, 100, 300, target)); };
  P.dbl = target => { P.tap(target); P.t += 150; P.tap(target); P.t += 1000; };
  P.muted = () => P.run("typeof muted === 'undefined' ? 'no mute' : muted");
  P.btn = () => { const b = P.els.mute; return b ? [b.textContent, b.getAttribute('aria-pressed'), b.classList.contains('on'), !!b.hidden] : 'no #mute'; };
  P.press = () => { const b = P.els.mute; if (b && b.onclick) b.onclick(); };
  P.disp = () => P.run('C.display()');
  P.orb = () => P.els.orb.className;
  P.cues = () => { const s = P.run('C.state()'); return s.log.concat(s.queue).filter(x => x === 'cue:mute' || x === 'cue:unmute'); };
  P.speech = n => P.feed(-20, n || 16);
  // spies: frames that reach the gate, the wake spotter and the orb's level
  P.spy = () => P.run(`globalThis.__n = {gate: 0, spot: 0, orb: 0};
    { const gf = gate.feed; gate.feed = (...a) => { __n.gate++; return gf(...a); };
      const ol = orb.level; orb.level = (...a) => { __n.orb++; return ol(...a); }; }
    useEngine('sherpa', {feed() { __n.spot++; }, reset() {}, stop() {}});`);
  P.n = () => { const n = P.run('__n'); return [n.gate, n.spot, n.orb]; };
  P.endCues = () => { for (let i = 0; i < 3; i++) { P.audio.fire('playing'); P.audio.fire('ended'); } P.feed(-70, 20); };   // Fix A: cue hold + tail
  return P;
}
async function start(P, cfg) {
  await P.els.b1.onclick();
  const page = P.pages()[0];
  page.readyState = 1; page.onopen();
  P.msg(page, {t: 'hello', epoch: 'm1', cfg: Object.assign({stt_route: 'phone', play_buffer_ms: 0, wake: {engine: 'transcript'}}, cfg || {})});
  await tick(40);
  return page;
}
const LEAVES = ['need_token', 'wake', 'committed', 'partial', 'vad', 'no_wake'];
const left = (page, from) => [page.sent.slice(from).filter(m => LEAVES.includes(m.t)).length, page.sent.slice(from).filter(m => m.binary).length];
const cssOf = id => {                    // merged declarations of every rule whose selector list names #id
  const d = {};
  for (const m of html.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (!m[1].split(',').some(s => s.trim() === '#' + id)) continue;
    for (const x of m[2].split(';')) { const i = x.indexOf(':'); if (i > 0) d[x.slice(0, i).trim()] = x.slice(i + 1).trim(); }
  }
  return d;
};
const bin = (turn, seq) => { const b = new ArrayBuffer(10), v = new DataView(b); v.setUint8(0, 1); v.setUint32(1, turn); v.setUint16(5, seq); return b; };

(async () => {
  {   // 1. markup and CSS
    const rb = cssOf('resetbar'), mu = cssOf('mute'), rt = cssOf('resettext'), on = cssOf('mute.on');
    eq('1 #mute is a real button inside #rpanel, hidden until setup, aria-pressed false, text MUTE',
       /<div id="rpanel" hidden>[\s\S]*<button id="mute" aria-pressed="false" hidden>MUTE<\/button>[\s\S]*<\/div>\s*<\/div>\s*<audio/.test(html), true);
    eq('1 no #stop element, no arm step, no 4 s timer left on the page', [/id="stop"/.test(html), /\barmStop\b/.test(html), /\bstopArmed\b/.test(html), /#stop\b/.test(html)], [false, false, false, false]);
    eq('1 css: #mute = #resetbar size (88vw x 72px, same box), 19px like the bar text, no padding',
       [mu.width, rb.width, mu.height, rb.height, mu['box-sizing'], rb['box-sizing'], mu['font-size'], rt['font-size'], /^0/.test(mu.padding || '')],
       ['88vw', '88vw', '72px', '72px', 'content-box', 'content-box', '19px', '19px', true]);
    eq('1 css: the UNMUTE state looks different (cyan #a8f7ff text and border)', [on.color, on['border-color']], ['#a8f7ff', '#a8f7ff']);
    const muted = [...html.matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter(m => /\.s-muted\b/.test(m[1]));
    eq('1 css: the muted orb is a thin static grey ring (#555, no animation)',
       [muted.length > 0, muted.some(m => /\.lvl/.test(m[1]) && /border:\s*[12]px solid #555/.test(m[2])), muted.some(m => /animation/.test(m[2]))], [true, true, false]);
    eq('1 the comments no longer say there is no mute', /There is no mute/.test(html), false);
  }
  {   // 1b. the cue files: the page preloads and plays the defaults when the server's cue map has no mute / unmute
    const P = rig();
    const page = await start(P, {cues: {wake: ['cues/wake_a.m4a'], thinking: 'cues/thinking.m4a', done: 'cues/done.m4a', reset: 'cues/reset.m4a', reset_failed: 'cues/reset_failed.m4a'}});
    P.press();
    eq('1b a server cue map without mute/unmute: the default mute file plays', [P.cues(), P.run('cueUrls')['cues/mute.m4a'] !== undefined], [['cue:mute'], true]);
    P.audio.fire('playing'); P.audio.fire('ended');
    P.press();
    eq('1b unmute: the default unmute file', P.run('cueUrls')['cues/unmute.m4a'] !== undefined, true);
    void page;
  }
  {   // 2. before setup nothing mutes
    const P = rig();
    eq('2 before setup: the button is hidden, not pressed', P.btn().slice(1), ['false', false, true]);
    P.press(); P.run("typeof setMuted === 'function' && setMuted(true)");
    eq('2 before the mic exists: not muted', P.muted(), false);
  }
  {   // 3. one tap mutes, one tap unmutes
    const P = rig();
    const page = await start(P);
    eq('3 after setup: MUTE shown, not pressed; asleep', [P.btn(), P.muted(), P.disp()], [['MUTE', 'false', false, false], false, 'sleep']);
    const t0 = Date.now();
    P.press();
    eq('3 ONE tap: muted (no arm step)', P.muted(), true);
    eq('3 muted: the button says UNMUTE, aria-pressed true, cyan state', P.btn(), ['UNMUTE', 'true', true, false]);
    eq('3 muted: gate asleep, STT closed, no pre-roll kept', [P.run('gate.state()'), P.run('gate.isOpen()'), P.run('stt.stats().want'), P.run('gate.prerollFrames()')], ['sleep', false, false, 0]);
    eq('3 muted: display muted, the orb in its muted state with nothing animating', [P.disp(), P.orb(), P.run('orb.animations()')], ['muted', 'orb d-simple s-muted', 0]);
    eq('3 muted: no hard stop: the track is live, never stopped, capture not stopped, context open',
       [P.tracks.map(t => [t.readyState, t.stops]), P.run('capStopped'), P.acs.map(a => a.closes), /mic off/.test(P.els.status.textContent)], [[['live', 0]], false, [0], false]);
    eq('3 muted: the mute cue', P.cues(), ['cue:mute']);
    await tick(30);
    eq('3 no timer changes it back (no 4 s arm timer): still UNMUTE', [P.btn()[0], Date.now() - t0 < 4000], ['UNMUTE', true]);
    P.audio.fire('playing'); P.audio.fire('ended');
    P.press();
    eq('3 ONE tap again: unmuted, MUTE, back to sleep, unmute cue', [P.muted(), P.btn(), P.disp(), P.orb(), P.cues()],
       [false, ['MUTE', 'false', false, false], 'sleep', 'orb d-simple s-sleep', ['cue:mute', 'cue:unmute']]);
    eq('3 unmuted without a reload: the same track, still live', [P.tracks.length, P.tracks[0].readyState, P.tracks[0].stops], [1, 'live', 0]);
    P.endCues();
    const from = page.sent.length;
    P.speech();
    eq('3 unmuted: the transcript wake path works again (probe opens STT)', [P.run('gate.state()'), page.sent.slice(from).filter(m => m.t === 'need_token').length], ['probe', 1]);
  }
  {   // 4. frames while muted reach neither the gate nor the spotter nor the orb level; nothing leaves; the counters run
    const P = rig();
    const page = await start(P);
    P.spy();
    P.feed(-60, 10);
    eq('4 guard, unmuted: 10 frames reach the gate, the spotter and orb.level', P.n(), [10, 10, 10]);
    P.press();
    const from = page.sent.length, n0 = P.n(), f0 = P.run('frameN'), lvl = P.els.orbl.style.transform, w0 = P.run('orb.stats().levelWrites');
    P.run('lastFrame = 0');
    P.speech(60);
    eq('4 muted: 60 loud frames reach neither the gate nor the wake spotter nor orb.level', P.n().map((v, i) => v - n0[i]), [0, 0, 0]);
    eq('4 muted: the orb ring is not written', [P.els.orbl.style.transform === lvl, P.run('orb.stats().levelWrites') - w0], [true, 0]);
    eq('4 muted: frameN and lastFrame still run (the mic watchdog stays quiet)', [P.run('frameN') - f0, Date.now() - P.run('lastFrame') < 1000], [60, true]);
    eq('4 muted: nothing leaves the phone (no need_token, wake, vad, committed, partial, no_wake; no PCM)', left(page, from), [0, 0]);
    eq('4 muted: gate still asleep, display muted', [P.run('gate.state()'), P.disp()], ['sleep', 'muted']);
    P.run('wakeEngine.onWake(1)');
    eq('4 muted: a wake-engine hit is ignored', [P.run('gate.state()'), left(page, from)], ['sleep', [0, 0]]);
    P.dbl();
    eq('4 muted: the double tap does nothing', [P.run('gate.state()'), left(page, from), P.muted()], ['sleep', [0, 0], true]);
    P.run('outbox.add({t: Date.now(), spd: 50})'); P.run('flushTelemetry(true)');
    eq('4 muted: telemetry still flows (sensors are not the mic)', page.sent.slice(from).filter(m => m.t === 'telemetry').length, 1);
    P.audio.fire('playing'); P.audio.fire('ended');
    P.press(); P.endCues();
    const from2 = page.sent.length;
    P.run('wakeEngine.onWake(0.9)');
    eq('4 unmute, then a wake-engine hit wakes at once (one wake frame, STT asked for)',
       [P.run('gate.state()'), page.sent.slice(from2).filter(m => m.t === 'wake').length, page.sent.slice(from2).filter(m => m.t === 'need_token').length], ['awake', 1, 1]);
  }
  {   // 5. unmute, then a double tap wakes
    const P = rig();
    const page = await start(P);
    P.press(); P.audio.fire('playing'); P.audio.fire('ended'); P.press(); P.endCues();
    P.dbl();
    eq('5 unmute, then a double tap wakes', [P.run('gate.state()'), page.sent.filter(m => m.t === 'wake').length], ['awake', 1]);
  }
  {   // 6. muted mid-conversation: STT closes at once, a late transcript is dropped
    const P = rig();
    const page = await start(P);
    P.speech();
    P.msg(page, {t: 'token', epoch: 'm1', kind: 'realtime_scribe', token: 'sutkn_x', url: 'wss://scribe.test/rt', params: {}});
    const scribe = P.sockets[P.sockets.length - 1];
    scribe.onmessage({data: JSON.stringify({message_type: 'session_started'})});
    scribe.onmessage({data: JSON.stringify({message_type: 'committed_transcript', text: 'Hey Hermes, how far to Bright?'})});
    eq('6 guard: awake with STT open', [P.run('gate.state()'), P.run('gate.isOpen()')], ['awake', true]);
    P.press();
    eq('6 muted while awake: gate asleep, STT closed', [P.run('gate.state()'), P.run('gate.isOpen()'), P.run('stt.stats().want')], ['sleep', false, false]);
    const from = page.sent.length;
    if (scribe.onmessage) scribe.onmessage({data: JSON.stringify({message_type: 'committed_transcript', text: 'and the weather there'})});
    if (scribe.onmessage) scribe.onmessage({data: JSON.stringify({message_type: 'partial_transcript', text: 'and the'})});
    P.run("onCommitted('and the weather there', 'en', 1)");
    eq('6 muted: a late transcript or partial is not sent', left(page, from), [0, 0]);
  }
  {   // 7. a reply that is playing keeps playing
    const P = rig();
    const page = await start(P);
    P.msg(page, {t: 'state', s: 'thinking', turn: 3, epoch: 'm1'});
    P.msg(page, {t: 'state', s: 'ready', turn: 3, epoch: 'm1'});
    for (let i = 0; i < 4 && P.disp() !== 'speaking'; i++) { P.audio.fire('playing'); P.audio.fire('ended'); }   // the thinking cue first
    page.onmessage({data: bin(3, 0)});
    P.audio.fire('playing');
    eq('7 guard: a reply segment is playing', [P.disp(), P.run('C.state().playing')], ['speaking', true]);
    const src = P.audio.src, p0 = P.pauses();
    P.press();
    eq('7 muted during a reply: it keeps playing (same source, no pause), still shown as speaking', [P.audio.src === src, P.pauses() - p0, P.run('C.state().playing'), P.disp(), P.muted()], [true, 0, true, 'speaking', true]);
    P.audio.fire('ended');
    P.msg(page, {t: 'end', turn: 3, segs: 1, epoch: 'm1'});
    for (let i = 0; i < 4; i++) { P.audio.fire('playing'); P.audio.fire('ended'); }
    P.msg(page, {t: 'state', s: 'listening', epoch: 'm1'});
    eq('7 after the reply: muted (no conversation window opens)', [P.disp(), P.run('gate.state()'), P.run('gate.isOpen()')], ['muted', 'sleep', false]);
  }
  {   // 8. server route: no PCM, no vad while muted
    const P = rig();
    const page = await start(P, {stt_route: 'server'});
    P.run('C.wake(); gate.wake()');
    P.feed(-20, 10);
    eq('8 guard (server route, awake): PCM leaves', page.sent.filter(m => m.binary).length > 0, true);
    P.press();
    const from = page.sent.length;
    P.speech(60);
    eq('8 server route muted: no PCM, no vad, no wake', left(page, from), [0, 0]);
  }
  {   // 9. mute survives a mic revive (a fresh track and worklet) and a WebSocket reconnect
    const P = rig();
    const page = await start(P);
    P.spy();
    P.press();
    const port0 = P.workletPort;
    await P.run("micRevive('return')");            // no frame within the revive wait: the mic is captured afresh
    eq('9 guard: the revive made a fresh track and worklet', [P.tracks.length, P.workletPort !== port0, P.tracks[1].readyState], [2, true, 'live']);
    const n0 = P.n(), from = page.sent.length;
    P.speech(40);
    eq('9 after a mic revive: still muted, the new worklet feeds nothing, nothing leaves', [P.muted(), P.btn()[0], P.disp(), P.n().map((v, i) => v - n0[i]), left(page, from)],
       [true, 'UNMUTE', 'muted', [0, 0, 0], [0, 0]]);
    page.onclose({code: 1006});
    await tick(1100);
    const p2 = P.pages()[1];
    p2.readyState = 1; p2.onopen();
    P.msg(p2, {t: 'hello', epoch: 'm2', cfg: {stt_route: 'phone', play_buffer_ms: 0, wake: {engine: 'transcript'}}});
    await tick(20);
    P.speech(40);
    eq('9 after a WebSocket reconnect: still muted, the button and the orb say so, nothing leaves', [P.muted(), P.btn()[0], P.disp(), P.orb(), left(p2, 0)],
       [true, 'UNMUTE', 'muted', 'orb d-simple s-muted', [0, 0]]);
    P.audio.fire('playing'); P.audio.fire('ended');
    P.press(); P.endCues();
    const g0 = P.n()[0];
    P.feed(-60, 5);
    eq('9 unmute after the revive: frames reach the gate again', [P.muted(), P.n()[0] - g0, P.disp()], [false, 5, 'sleep']);
  }
  {   // 10. the lock screen / Control Centre pause and stop buttons mute (they no longer stop capture for good)
    for (const a of ['pause', 'stop']) {
      const P = rig();
      const page = await start(P);
      eq('10 Media Session ' + a + ': a handler is registered', typeof P.handlers[a], 'function');
      if (P.handlers[a]) P.handlers[a]();
      eq('10 lock screen ' + a + ': muted, not a hard stop (track live, capture not stopped), UNMUTE shown',
         [P.muted(), P.tracks.map(t => t.readyState), P.run('capStopped'), P.btn()[0], P.disp()], [true, ['live'], false, 'UNMUTE', 'muted']);
      if (P.handlers[a]) P.handlers[a]();
      eq('10 lock screen ' + a + ' twice: still muted (it never toggles back)', P.muted(), true);
      P.press();
      eq('10 after a lock screen ' + a + ': one tap on UNMUTE unmutes', [P.muted(), P.btn()[0]], [false, 'MUTE']);
    }
  }
  {   // 11. the button never hard-stops; the internal stopCapture still wins over the mute
    const P = rig();
    await start(P);
    for (let i = 0; i < 6; i++) P.press();
    eq('11 six taps: back to unmuted, never a hard stop', [P.muted(), P.tracks.map(t => [t.readyState, t.stops]), P.run('capStopped'), P.acs[0].closes], [false, [['live', 0]], false, 0]);
    P.press();
    P.run("stopCapture('test')");
    eq('11 stopCapture (internal) while muted: tracks ended', P.tracks.map(t => t.readyState), ['ended']);
    P.press();
    eq('11 after stopCapture the button is dead (nothing to unmute)', [P.muted(), P.els.mute.disabled], [true, true]);
  }
  {   // 12. Media Session 'play' unmutes (Lana's review, 11 Nov): after a pause-mute, a play press must not leave the
      // mic muted with no reachable control
    const P = rig();
    await start(P);
    eq('12 Media Session play: a handler is registered', typeof P.handlers.play, 'function');
    P.handlers.pause();
    eq('12 pause mutes', P.muted(), true);
    if (P.handlers.play) P.handlers.play();
    eq('12 play unmutes', [P.muted(), P.btn()[0], P.disp()], [false, 'MUTE', 'sleep']);
    if (P.handlers.play) P.handlers.play();
    eq('12 play while already unmuted: still unmuted, no second cue', [P.muted(), P.btn()[0]], [false, 'MUTE']);
  }
  {   // 13. D4: MUTE pressed while a mic revive has no track (the restart failed, the Tap overlay is up) is kept: the
      // mic that the Tap brings back is muted
    const tracks = []; let failGum = false;
    const navigator = {userAgent: 'node-page-test', geolocation: {watchPosition() {}},
      mediaSession: {metadata: null, setActionHandler() {}},
      mediaDevices: {getUserMedia: async () => {
        if (failGum) throw Object.assign(new Error('no gesture'), {name: 'NotAllowedError'});
        const t = {label: 'fake mic', readyState: 'live', enabled: true, muted: false, l: {},
                   addEventListener(n, f) { (this.l[n] = this.l[n] || []).push(f); }, stop() { this.readyState = 'ended'; }};
        tracks.push(t); return {getAudioTracks: () => [t], getTracks: () => [t]};
      }}};
    const P = rig({navigator});
    P.run('document.visibilityState = "visible"');
    const page = await start(P);
    P.spy();
    failGum = true; tracks[0].readyState = 'ended';
    await P.run("micRevive('return')");
    eq('13 guard: the revive failed: no track, the Tap overlay is up, not muted', [P.run('micTracks.length'), P.els.rv.hidden, P.muted()], [0, false, false]);
    P.press();                                     // the rider presses MUTE
    eq('13 MUTE during the failed revive: muted, the button says UNMUTE', [P.muted(), P.btn()[0], P.disp()], [true, 'UNMUTE', 'muted']);
    failGum = false;
    await P.els.rvb.onclick();                     // the Tap overlay revive
    for (let i = 0; i < 150 && P.run('!!reviving'); i++) await tick(20);
    eq('13 guard: the Tap revive captured a fresh mic', [P.run('micTracks.length'), tracks.length, tracks[1] && tracks[1].readyState], [1, 2, 'live']);
    const n0 = P.n(), from = page.sent.length;
    P.speech(40);
    eq('13 after the Tap revive: still muted, UNMUTE shown, no frames to the gate, nothing leaves',
       [P.muted(), P.btn()[0], P.disp(), P.n().map((v, i) => v - n0[i]), left(page, from)], [true, 'UNMUTE', 'muted', [0, 0, 0], [0, 0]]);
    P.audio.fire('playing'); P.audio.fire('ended');
    P.press(); P.endCues();
    const g0 = P.n()[0];
    P.feed(-60, 5);
    eq('13 UNMUTE after it: frames reach the gate again', [P.muted(), P.btn()[0], P.n()[0] - g0], [false, 'MUTE', 5]);
  }
  T.done();
})().catch(T.crash);
