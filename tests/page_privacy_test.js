// Fix round 3, R3-6: privacy. On the real index.html scripts (page_rig): a mic indicator separate from the state
// colours (#mic: 'live' = audio can leave the phone / STT open, 'local' = mic captured for the on-phone wake word only,
// 'off' = every mic track really stopped, 'muted' = the MUTE button), the internal hard stop (stopCapture: stops
// capture, closes STT and calls track.stop()) and, since round 3 (A2), the one-tap MUTE button and the Media Session
// pause/stop actions (lock screen / Control Centre), which mute instead of stopping. The page never shows 'off' unless
// the tracks really ended.
'use strict';
const {loadPage, tick, checker} = require('./page_rig.js');
const T = checker(), {eq} = T;

function rig(opts) {
  const o = opts || {};
  const tracks = [], handlers = {};
  const track = () => {
    const t = {label: 'fake mic', readyState: 'live', stops: 0, l: {},
               addEventListener(n, f) { this.l[n] = f; },
               stop() { this.stops++; if (!o.stopFails) this.readyState = 'ended'; }};
    tracks.push(t); return t;
  };
  const navigator = {userAgent: 'node-page-test', geolocation: {watchPosition() {}},
    mediaDevices: {getUserMedia: async () => { const t = track(); return {getAudioTracks: () => [t], getTracks: () => [t]}; }},
    mediaSession: {metadata: null, setActionHandler(a, f) { handlers[a] = f; }}};
  const P = loadPage({navigator, RIDE_SETUP_WAIT: {hello: 30}});
  P.tracks = tracks; P.handlers = handlers;
  P.mic = () => P.els.mic ? P.els.mic.className : undefined;
  return P;
}
const hardStop = P => P.run("stopCapture('test')");     // round 3: the hard stop is internal (no button)
const tapMute = P => { if (P.els.mute && P.els.mute.onclick) P.els.mute.onclick(); };
async function start(P, route) {
  await P.els.b1.onclick();
  const page = P.pages()[0];
  page.readyState = 1; page.onopen();
  P.msg(page, {t: 'hello', epoch: 'p1', cfg: {stt_route: route || 'phone', wake: {engine: 'transcript'}}});
  await tick(40);
  return page;
}

(async () => {
  {   // indicator follows what leaves the phone
    const P = rig();
    eq('6 before tap 1: no indicator', P.mic() || '', '');
    const page = await start(P);
    eq('6 asleep, mic only feeds the on-phone wake engine: local', P.mic(), 'local');
    P.feed(-20, 16);                                   // speech in sleep: transcript probe opens STT
    eq('6 probe/awake: audio may leave: live', [P.run('gate.isOpen()'), P.mic()], [true, 'live']);
    P.run('gate.sleep()');
    eq('6 back to sleep: local', P.mic(), 'local');
    void page;
  }
  {   // round 3: the one mic control mutes with ONE tap; nothing is stopped; the indicator says muted
    const P = rig();
    await start(P);
    tapMute(P);
    eq('6 one tap on MUTE: muted at once, track not stopped, indicator muted (not off)', [P.tracks[0].stops, P.tracks[0].readyState, P.mic(), P.els.mute.textContent], [0, 'live', 'muted', 'UNMUTE']);
  }
  {   // ... the second tap stops everything for real
    const P = rig();
    const page = await start(P);
    P.feed(-20, 16);                                   // awake/probing, audio leaving
    hardStop(P);
    eq('6 confirmed: track.stop() called', P.tracks.map(t => [t.stops, t.readyState]), [[1, 'ended']]);
    eq('6 confirmed: gate asleep, STT closed', [P.run('gate.state()'), P.run('gate.isOpen()'), P.run('stt.stats().want'), P.run('stt.stats().open')],
       ['sleep', false, false, false]);
    eq('6 confirmed: indicator off, status says so', [P.mic(), /mic off/.test(P.els.status.textContent)], ['off', true]);
    const before = page.sent.length;
    eq('6 after stop: worklet frames detached', typeof P.workletPort.onmessage === 'function', false);
    if (typeof P.workletPort.onmessage === 'function') P.feed(-20, 50);   // a late frame must not reopen anything
    eq('6 after stop: nothing more leaves the phone', [page.sent.slice(before).filter(m => ['need_token', 'vad', 'committed'].includes(m.t)).length,
       P.run('gate.state()')], [0, 'sleep']);
    await tick(1200);
    eq('6 no automatic mic restart after a deliberate stop', P.tracks.length, 1);
  }
  {   // lock screen / Control Centre: Media Session pause and stop MUTE (round 3; they used to stop capture for good)
    for (const a of ['pause', 'stop']) {
      const P = rig();
      const page = await start(P);
      P.feed(-20, 16);                                 // probing: audio leaving
      eq('6 media session ' + a + ' handler registered', typeof P.handlers[a], 'function');
      if (typeof P.handlers[a] === 'function') P.handlers[a]();
      const before = page.sent.length;
      P.feed(-20, 50);
      eq('6 media session ' + a + ' mutes: track live, gate asleep, STT closed, indicator muted, nothing leaves',
         [P.tracks[0].readyState, P.run('gate.state()'), P.run('stt.stats().want'), P.mic(), page.sent.slice(before).filter(m => ['need_token', 'vad', 'committed', 'wake'].includes(m.t) || m.binary).length],
         ['live', 'sleep', false, 'muted', 0]);
    }
  }
  {   // honesty: a track that does not end is never shown as off
    const P = rig({stopFails: true});
    await start(P);
    hardStop(P);
    eq('6 stop failed: not shown as off', [P.tracks[0].stops, P.mic() !== 'off', /fail/.test(P.els.status.textContent)], [1, true, true]);
  }
  T.done();
})().catch(T.crash);
