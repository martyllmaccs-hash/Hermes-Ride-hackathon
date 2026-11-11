// Tap to wake (10 Nov): a double tap anywhere wakes the agent exactly like the wake word (same path: C.wake,
// gate.wake, {t:'wake'}, spotter reset), only from sleep (or the transcript engine's probe); awake / thinking /
// speaking a double tap does nothing, and muted (round 3: the MUTE button, tests/page_mute_test.js) it does nothing
// either: a double tap never mutes or unmutes. #mute is the same size as #resetbar. The real
// index.html scripts in node (tests/page_rig.js) with pointer events dispatched on the document. Prints {checks, failures}.
'use strict';
const fs = require('fs'), path = require('path');
const {loadPage, tick, checker} = require('./page_rig.js');
const T = checker(), {eq} = T;
const ROOT = path.join(__dirname, '..');
// ---- the page
function rig(over) {
  const tracks = [];
  const track = () => { const t = {label: 'fake mic', readyState: 'live', enabled: true, l: {}, addEventListener(n, f) { this.l[n] = f; },
                                   stop() { this.readyState = 'ended'; }}; tracks.push(t); return t; };
  const navigator = {userAgent: 'node-page-test', geolocation: {watchPosition() {}},
    mediaDevices: {getUserMedia: async () => { const t = track(); return {getAudioTracks: () => [t], getTracks: () => [t]}; }}};
  const P = loadPage(Object.assign({navigator, RIDE_SETUP_WAIT: {hello: 500}}, over || {}));
  P.tracks = tracks;
  P.t = 10000;                                      // event time (ms), advanced by the helpers
  const ev = (id, x, y, target) => ({pointerId: id, isPrimary: id === 1, pointerType: 'touch', clientX: x, clientY: y,
                                     timeStamp: P.t, target: target || P.body});
  P.down = (id, x, y, target) => P.fire('pointerdown', ev(id, x, y, target));
  P.move = (id, x, y, target) => P.fire('pointermove', ev(id, x, y, target));
  P.up = (id, x, y, target) => P.fire('pointerup', ev(id, x, y, target));
  P.tap = (target, x, y) => { P.down(1, x || 100, y || 300, target); P.t += 60; P.up(1, x || 100, y || 300, target); };
  P.wait = ms => { P.t += ms; };
  P.dbl = (target, gap) => { P.tap(target); P.wait(gap == null ? 150 : gap); P.tap(target); P.wait(1000); };
  P.muted = () => P.run("typeof muted === 'undefined' ? false : muted");
  P.disp = () => P.run('C.display()');
  P.mic = () => P.els.mic.className;
  P.cues = () => { const s = P.run('C.state()'); return s.log.concat(s.queue).filter(x => x === 'cue:mute' || x === 'cue:unmute'); };   // played or queued
  P.speech = (n) => P.feed(-20, n || 16);
  P.feedPcm = pcm => {
    const db = P.run('rideFrameDb');
    for (let i = 0; i + 320 <= pcm.length; i += 320) {
      const f = pcm.slice(i, i + 320);
      P.workletPort.onmessage({data: {pcm: f.buffer, db: db(f)}});
    }
  };
  return P;
}
async function start(P, cfg, keepSetup) {
  await P.els.b1.onclick();
  const page = P.pages()[0];
  page.readyState = 1; page.onopen();
  P.msg(page, {t: 'hello', epoch: 'm1', cfg: Object.assign({stt_route: 'phone', wake: {engine: 'transcript'}}, cfg || {})});
  await tick(40);
  return page;
}
const opened = (page, from) => page.sent.slice(from || 0).filter(m => ['need_token', 'wake', 'committed', 'vad', 'no_wake'].includes(m.t)).length;


const wakes = page => page.sent.filter(m => m.t === 'wake');
const cssOf = (html, id) => {            // merged declarations of every rule whose selector list names #id
  const d = {};
  for (const m of html.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (!m[1].split(',').some(s => s.trim() === '#' + id)) continue;
    for (const x of m[2].split(';')) { const i = x.indexOf(':'); if (i > 0) d[x.slice(0, i).trim()] = x.slice(i + 1).trim(); }
  }
  return d;
};
// a sherpa-like engine object through the page's own useEngine (the real onWake handler is installed on it)
const sherpa = P => { P.run("globalThis.__resets = 0; useEngine('sherpa', {feed() {}, reset() { globalThis.__resets++; }, stop() {}})"); };

(async () => {
  {   // the page
    const html = fs.readFileSync(path.join(ROOT, 'static', 'index.html'), 'utf8');
    eq('css: touch-action manipulation on html/body (no iOS double-tap zoom)', /html,body\{[^}]*touch-action:manipulation/.test(html), true);
    eq('setup, reveal and the Tap overlay stay excluded (data-no-dbltap)', [/<div id="setup" data-no-dbltap/.test(html), /<div id="reveal" data-no-dbltap/.test(html), /<div id="rv" data-no-dbltap/.test(html)], [true, true, true]);
    const rb = cssOf(html, 'resetbar'), st = cssOf(html, 'mute'), rt = cssOf(html, 'resettext');
    eq('css: #mute width = #resetbar width (88vw)', [st.width, rb.width], ['88vw', '88vw']);
    eq('css: #mute height = #resetbar height (72px)', [st.height, rb.height], ['72px', '72px']);
    eq('css: #mute font-size = the reset bar text (19px)', [st['font-size'], rt['font-size']], ['19px', '19px']);
    eq('css: same box model, so the rendered size is equal', st['box-sizing'] || 'content-box', rb['box-sizing'] || 'content-box');
    eq('css: #mute has no padding left that would grow it', st.padding === undefined || /^0/.test(st.padding), true);
    eq('#mute is inside #rpanel (where #stop was), hidden until the reveal', [/<div id="rpanel" hidden>[\s\S]*<button id="mute" aria-pressed="false" hidden>MUTE<\/button>[\s\S]*<\/div>\s*<\/div>\s*<audio/.test(html)], [true]);
    eq('round 3: the soft mute is back as a button, and the double tap is still wired to the wake only', [/\bsetMuted\b/.test(html), /RideDoubleTap\(\{\}, tapWake\)/.test(html), /RideDoubleTap\([^)]*setMuted/.test(html)], [true, true, false]);
  }
  {   // before the mic exists nothing happens; excluded areas never wake
    const P = rig();
    P.dbl();
    eq('no mic yet: a double tap does nothing', P.run('gate.state()'), 'sleep');
    P.dbl(P.els.b1); P.dbl(P.els.setup);
    eq('double tap on the setup orb / panel: nothing', P.run('gate.state()'), 'sleep');
  }
  {   // sherpa engine, asleep: the wake word's path
    const P = rig();
    const page = await start(P, {wake: {engine: 'sherpa'}});
    sherpa(P);
    eq('asleep, mic running', [P.run('gate.state()'), P.disp()], ['sleep', 'sleep']);
    const from = page.sent.length;
    P.dbl();
    eq('double tap from sleep: gate awake, STT opened, display no longer sleep', [P.run('gate.state()'), page.sent.slice(from).filter(m => m.t === 'need_token').length, P.disp() !== 'sleep'], ['awake', 1, true]);
    eq('exactly one wake frame, w = the wake name (default hermes), like the spoken one', [wakes(page).length, wakes(page)[0] && wakes(page)[0].w], [1, 'hermes']);
    eq('the wake cue is queued/played, like the spoken wake', P.run('C.state()').log.concat(P.run('C.state()').queue).filter(x => /^cue:wake/.test(x)).length, 1);
    eq('the spotter was reset, as after a spoken wake', P.run('globalThis.__resets'), 1);
    eq('no mute: tracks enabled, mic indicator not muted', [P.tracks.every(t => t.enabled), P.mic() !== 'muted', P.muted()], [true, true, false]);
    P.dbl();
    eq('a second double tap while awake: no second wake, no state change', [wakes(page).length, P.run('gate.state()')], [1, 'awake']);
  }
  {   // awake, thinking, speaking: nothing
    const P = rig();
    const page = await start(P, {wake: {engine: 'sherpa'}});
    sherpa(P);
    P.run("C.wake(); gate.wake()");
    const n = wakes(page).length, tr = P.run('JSON.stringify(C.state())');
    P.dbl();
    eq('awake: a double tap does nothing', [wakes(page).length - n, P.run('gate.state()'), P.tracks.every(t => t.enabled)], [0, 'awake', true]);
    P.run('gate.setSpeaking(true)');
    const st = P.run('gate.state()');
    P.dbl();
    eq('speaking: a double tap does not cut her off or wake', [P.run('gate.state()'), wakes(page).length - n, P.run('gate.isOpen()')], [st, 0, true]);
    P.run('gate.setSpeaking(false); gate.wake(); gate.setBusy(true)');
    const busyState = P.run('gate.state()');
    P.dbl();
    eq('thinking (busy): a double tap does nothing', [P.run('gate.state()'), wakes(page).length - n], [busyState, 0]);
  }
  {   // taps that are not a double tap never wake
    const P = rig();
    const page = await start(P, {wake: {engine: 'sherpa'}});
    sherpa(P);
    const n = () => wakes(page).length;
    P.tap(); P.wait(2000);
    eq('single tap ignored', n(), 0);
    P.dbl(null, 400);
    eq('two taps 400 ms apart ignored (default 350)', n(), 0);
    P.down(1, 100, 300); P.t += 60; P.move(1, 100, 340); P.t += 60; P.up(1, 100, 340); P.wait(100); P.tap(); P.wait(1000);
    eq('a drag then a tap: ignored', n(), 0);
    P.down(1, 100, 300); P.down(2, 200, 300); P.t += 50; P.up(1, 100, 300); P.up(2, 200, 300); P.wait(100);
    P.down(1, 100, 300); P.down(2, 200, 300); P.t += 50; P.up(1, 100, 300); P.up(2, 200, 300); P.wait(1000);
    eq('two-finger taps ignored', n(), 0);
    P.down(1, 100, 300); P.t += 900; P.up(1, 100, 300); P.wait(100); P.tap(); P.wait(1000);
    eq('a long press then a tap: ignored', n(), 0);
    P.dbl(P.els.mute);
    eq('double tap gesture on #mute: no wake, not muted by pointer events, mic still live', [n(), P.muted(), P.tracks[0].readyState], [0, false, 'live']);
    const reveal = {tagName: 'DIV', parentElement: P.body, attrs: {'data-no-dbltap': ''},
                    getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; }, hasAttribute(n) { return n in this.attrs; }};
    const inner = {tagName: 'SPAN', parentElement: reveal, getAttribute: () => null, hasAttribute: () => false};
    P.dbl(reveal); P.dbl(inner);
    eq('double tap on / inside a data-no-dbltap element (the reveal): nothing', [n(), P.run('gate.state()')], [0, 'sleep']);
    const link = {tagName: 'A', parentElement: P.body, getAttribute: () => null, hasAttribute: () => false};
    P.dbl(link);
    eq('double tap on a link: nothing', n(), 0);
    P.dbl(null, 300);
    eq('guard: two taps 300 ms apart wake', [n(), P.run('gate.state()')], [1, 'awake']);
  }
  {   // after a hard stop a double tap does nothing
    const P = rig();
    const page = await start(P, {wake: {engine: 'sherpa'}});
    sherpa(P);
    P.run("stopCapture('test')");
    P.dbl();
    eq('after a hard stop: no wake, tracks ended', [wakes(page).length, P.tracks.map(t => t.readyState), P.run('gate.state()')], [0, ['ended'], 'sleep']);
  }
  {   // round 3: muted, a double tap neither wakes nor unmutes; after unmute it wakes again
    const P = rig();
    const page = await start(P, {wake: {engine: 'sherpa'}});
    sherpa(P);
    P.els.mute.onclick();
    P.dbl();
    eq('muted: a double tap does not wake and does not unmute', [wakes(page).length, P.run('gate.state()'), P.muted()], [0, 'sleep', true]);
    P.els.mute.onclick();
    P.dbl();
    eq('unmuted: the double tap wakes again', [wakes(page).length, P.run('gate.state()'), P.muted()], [1, 'awake', false]);
  }
  {   // transcript engine: asleep, and in the probe state
    const P = rig();
    const page = await start(P);                 // transcript engine
    P.feed(-70, 15);
    const from = page.sent.length;
    P.dbl();
    eq('transcript engine, asleep: a double tap wakes (STT opened, one wake frame)', [P.run('gate.state()'), wakes(page).length, page.sent.slice(from).filter(m => m.t === 'need_token').length], ['awake', 1, 1]);
    const Q = rig();
    const qp = await start(Q);
    Q.feed(-70, 15); Q.speech();
    eq('transcript probe is open (speech heard)', Q.run('gate.state()'), 'probe');
    Q.dbl();
    eq('transcript engine, in probe: the tap wakes the same way (awake, one wake frame)', [Q.run('gate.state()'), wakes(qp).length], ['awake', 1]);
  }
  {   // double_tap_ms from hello.cfg still applies
    const P = rig();
    const page = await start(P, {double_tap_ms: 600, wake: {engine: 'sherpa'}});
    sherpa(P);
    P.dbl(null, 500);
    eq('double_tap_ms 600: taps 500 ms apart wake', wakes(page).length, 1);
    eq('client clamp', [P.run('rideDoubleTapMs({double_tap_ms: 5})'), P.run('rideDoubleTapMs({double_tap_ms: 5000})'), P.run('rideDoubleTapMs({})')], [150, 1000, 350]);
  }
  T.done();
})().catch(T.crash);
