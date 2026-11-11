// Fix round 4: Stop is terminal (round 3, A2: stopCapture() is internal now, the on-screen control is MUTE). On the real index.html scripts (page_rig) with mic tracks that can end and a
// getUserMedia / AudioContext.resume that the test resolves by hand.
//   AUD-R3-01: a mic recovery queued (the 1 s timer after a track 'ended') or in flight (awaiting the mic or the
//              audio resume) when Stop is pressed never adopts a live track or attaches the worklet afterwards.
//   AUD-R3-02: a sherpa hit queued in the worker, or a sherpa load that finishes after Stop, never reopens STT; Stop
//              terminates the worker and clears the pre-roll; the indicator says 'live' whenever STT/gate is open,
//              even if the tracks ended.
'use strict';
const {loadPage, tick, checker} = require('./page_rig.js');
const T = checker(), {eq} = T;

const workers = [];
class FakeWorker {   // the sherpa worker: 'ready' 5 ms after init unless FakeWorker.holdReady
  constructor(url) { this.url = url; this.posted = []; this.terminated = false; workers.push(this); }
  postMessage(m) { this.posted.push(m.t); if (m.t === 'init' && !FakeWorker.holdReady) setTimeout(() => this.onmessage({data: {t: 'ready'}}), 5); }
  terminate() { this.terminated = true; }
}
function rig(opts) {
  const o = opts || {};
  const tracks = [], handlers = {}, held = [];
  let hold = false, holdResume = false;
  const track = () => {
    const t = {label: 'fake mic', readyState: 'live', stops: 0, l: {},
               addEventListener(n, f) { this.l[n] = f; },
               stop() { this.stops++; this.readyState = 'ended'; }};
    tracks.push(t); return t;
  };
  const stream = () => { const t = track(); return {getAudioTracks: () => [t], getTracks: () => [t]}; };
  const navigator = {userAgent: 'node-page-test', geolocation: {watchPosition() {}},
    mediaDevices: {getUserMedia: () => hold ? new Promise(r => held.push(() => r(stream()))) : Promise.resolve(stream())},
    mediaSession: {metadata: null, setActionHandler(a, f) { handlers[a] = f; }}};
  const resumes = [], srcs = []; let closes = 0;
  class AC {
    constructor() { this.audioWorklet = {addModule: async () => {}}; this.state = 'running'; }
    createMediaStreamSource() { const n = {connect() {}, disconnect() { this.cut = true; }}; srcs.push(n); return n; }
    close() { this.state = 'closed'; closes++; return Promise.resolve(); }
    resume() { return holdResume ? new Promise(r => resumes.push(r)) : Promise.resolve(); }
  }
  const P = loadPage(Object.assign({navigator, AudioContext: AC, RIDE_SETUP_WAIT: {hello: 500, revive: 30, revive_tap: 30}}, o.sherpa ? {Worker: FakeWorker} : {}));
  Object.assign(P, {srcs, closed: () => closes, tracks, handlers, held, resumes,
    hold: on => { hold = on; }, holdResume: on => { holdResume = on; },
    gum: () => tracks.length,
    live: () => tracks.filter(t => t.readyState === 'live').length,
    mic: () => P.els.mic ? P.els.mic.className : undefined,
    endTrack: i => { const t = tracks[i]; t.readyState = 'ended'; if (t.l.ended) t.l.ended(); }});
  return P;
}
const stopNow = P => P.run("stopCapture('test')");   // round 3 (A2): the hard stop has no button any more (MUTE replaced it); called directly
async function start(P, engine) {
  await P.els.b1.onclick();
  const page = P.pages()[0];
  page.readyState = 1; page.onopen();
  P.msg(page, {t: 'hello', epoch: 'p1', cfg: {stt_route: 'phone', wake: {engine: engine || 'transcript'}}});
  await tick(40);
  return page;
}

(async () => {
  {   // guard: without Stop an ended track is recovered (the recovery path itself still works)
    const P = rig();
    await start(P);
    P.endTrack(0);
    // D2: the recovery is the revive path now (1 s timer, then micRevive): wait for it to settle, not a fixed tick
    for (let i = 0; i < 150 && P.run('!!recoverTimer || !!reviving'); i++) await tick(20);
    eq('R3-01 guard: an ended track is recovered without Stop', [P.gum(), P.live(), P.mic()], [2, 1, 'local']);
  }
  {   // recovery queued (1 s timer) when Stop is pressed
    const P = rig();
    const page = await start(P);
    P.endTrack(0);                                     // the OS ended the track: recovery is scheduled in 1 s
    stopNow(P);
    const port = P.workletPort, before = page.sent.length;
    await tick(1300);
    eq('R3-01 queued recovery after Stop: no new mic request, no live track', [P.gum(), P.live()], [1, 0]);
    eq('R3-01 queued recovery after Stop: no new worklet attached', P.workletPort === port && typeof port.onmessage !== 'function', true);
    eq('R3-01 queued recovery after Stop: indicator off, nothing sent', [P.mic(), page.sent.slice(before).filter(m => ['need_token', 'vad'].includes(m.t)).length], ['off', 0]);
  }
  {   // recovery in flight, awaiting getUserMedia, when Stop is pressed; the mic arrives afterwards
    const P = rig();
    const page = await start(P);
    P.hold(true);
    P.endTrack(0);
    for (let i = 0; i < 150 && P.held.length < 1; i++) await tick(20);   // the timer fired: getUserMedia is pending
    eq('R3-01 in-flight (mic): request pending', P.held.length, 1);
    stopNow(P);
    const port = P.workletPort, before = page.sent.length;
    P.held.splice(0).forEach(f => f());
    await tick(50);
    eq('R3-01 in-flight (mic) after Stop: the late stream is stopped at once', [P.gum(), P.live(), P.tracks[1] && P.tracks[1].stops], [2, 0, 1]);
    eq('R3-01 in-flight (mic) after Stop: no worklet attached', P.workletPort === port && typeof port.onmessage !== 'function', true);
    // D2: the revive released the ended track before asking for the mic, so Stop finds no track to report on: the
    // indicator is blank (never 'live' or 'local'), as for a Stop during any other revive restart
    eq('R3-01 in-flight (mic) after Stop: indicator not live', [P.mic(), P.run('micTracks.length')], ['', 0]);
    await tick(1200);
    eq('R3-01 in-flight (mic) after Stop: no further attempt', [P.gum(), P.live(), page.sent.slice(before).filter(m => ['need_token', 'vad'].includes(m.t)).length], [2, 0, 0]);
  }
  {   // recovery in flight, awaiting the audio resume, when Stop is pressed
    const P = rig();
    await start(P);
    P.holdResume(true);
    P.endTrack(0);
    for (let i = 0; i < 150 && P.resumes.length < 1; i++) await tick(20);   // new stream acquired, worklet node made, resume pending
    eq('R3-01 in-flight (resume): resume pending', [P.resumes.length, P.gum()], [1, 2]);
    stopNow(P);
    P.resumes.splice(0).forEach(f => f());
    await tick(50);
    eq('R3-01 in-flight (resume) after Stop: the new track is stopped', [P.live(), P.tracks[1].readyState], [0, 'ended']);
    eq('R3-01 in-flight (resume) after Stop: its worklet frames detached', typeof P.workletPort.onmessage !== 'function', true);
    eq('R3-01 in-flight (resume) after Stop: indicator not live', [P.mic(), P.run('micTracks.length')], ['', 0]);
    await tick(1200);
    eq('R3-01 in-flight (resume) after Stop: no further attempt', [P.gum(), P.live()], [2, 0]);
  }
  const opened = (page, from) => page.sent.slice(from).filter(m => ['need_token', 'wake', 'committed'].includes(m.t)).length;
  const preroll = P => P.run("typeof gate.prerollFrames === 'function' ? gate.prerollFrames() : -1");
  {   // guard: a sherpa hit before Stop wakes the gate (the fake worker path works)
    const P = rig({sherpa: true});
    const page = await start(P, 'sherpa');
    eq('R3-02 guard: sherpa engine active', P.run('engineName'), 'sherpa');
    const w = workers[workers.length - 1], from = page.sent.length;
    w.onmessage({data: {t: 'hit', score: 0.9}});
    eq('R3-02 guard: a hit before Stop wakes and opens STT', [P.run('gate.state()'), opened(page, from) > 0], ['awake', true]);
  }
  {   // a hit already queued in the worker arrives after Stop
    const P = rig({sherpa: true});
    const page = await start(P, 'sherpa');
    const w = workers[workers.length - 1];
    P.feed(-60, 40);                                   // quiet frames in sleep fill the pre-roll ring
    eq('R3-02 pre-roll filled before Stop', preroll(P) !== 0, true);
    stopNow(P);
    eq('R3-02 Stop terminates the wake worker and clears the pre-roll', [w.terminated, preroll(P)], [true, 0]);
    const from = page.sent.length;
    if (w.onmessage) w.onmessage({data: {t: 'hit', score: 0.9}});
    await tick(20);
    eq('R3-02 late hit after Stop: gate stays asleep, STT closed', [P.run('gate.state()'), P.run('gate.isOpen()'), P.run('stt.stats().want')], ['sleep', false, false]);
    eq('R3-02 late hit after Stop: no token, wake or audio leaves', [opened(page, from), page.sent.slice(from).filter(m => m.binary).length], [0, 0]);
    eq('R3-02 late hit after Stop: indicator off', P.mic(), 'off');
  }
  {   // the sherpa load finishes after Stop
    FakeWorker.holdReady = true;
    const P = rig({sherpa: true});
    const page = await start(P, 'sherpa');
    FakeWorker.holdReady = false;
    const w = workers[workers.length - 1];
    eq('R3-02 sherpa load pending at Stop', [w.posted.includes('init'), P.run('engineName')], [true, 'transcript']);
    stopNow(P);
    if (w.onmessage) w.onmessage({data: {t: 'ready'}});
    await tick(30);
    eq('R3-02 late load after Stop: worker terminated, engine not adopted', [w.terminated, P.run('wakeEngine === null')], [true, true]);
    eq('R3-02 late load after Stop: never says wake ready: sherpa', /wake ready: sherpa/.test(P.els.diag.textContent), false);
    const from = page.sent.length;
    if (w.onmessage) w.onmessage({data: {t: 'hit', score: 0.9}});
    await tick(20);
    eq('R3-02 late load after Stop: a hit opens nothing', [P.run('gate.state()'), opened(page, from)], ['sleep', 0]);
  }
  {   // indicator: STT open with the track ended is still 'live', never 'off'
    const P = rig();
    await start(P);
    P.feed(-20, 16);                                   // transcript probe: STT open
    P.tracks[0].readyState = 'ended';                  // no 'ended' event yet (the recovery timer is not the point)
    P.run('showMic()');
    eq('R3-02 indicator: STT open with ended tracks shows live', [P.run('gate.isOpen()'), P.mic()], [true, 'live']);
  }
  {   // Stop cuts the whole capture chain, not only the track (iOS keeps capture alive otherwise)
    const P = rig();
    const page = await start(P);
    stopNow(P);
    eq('Stop: tracks disabled, every source disconnected, context closed, orb asleep',
      [P.tracks.every(t => t.enabled === false), P.srcs.length > 0 && P.srcs.every(n => n.cut), P.closed(), P.run("orb.state()")], [true, true, 1, 'sleep']);
    eq('Stop: no worklet handler left to hear frames', typeof P.workletPort.onmessage !== 'function', true);
  }
  T.done();
})().catch(T.crash);
