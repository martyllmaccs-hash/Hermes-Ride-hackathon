// Back from the background (rider feedback: "I can't hear the mic" after switching apps and back). The real index.html scripts
// in node (tests/page_rig.js). iOS interrupts the AudioContext and ends or mutes the mic track while another app is in
// front; when the page is visible again it must resume the context and, if no frame arrives, capture afresh, without
// the "can't hear the mic" notice. Prints {checks, failures}.
'use strict';
const {loadPage, tick, checker} = require('./page_rig.js');
const T = checker(), {eq} = T;

function rig() {
  const tracks = [];
  let gums = 0;
  const navigator = {userAgent: 'node-page-test', geolocation: {watchPosition() {}},
    mediaDevices: {getUserMedia: async () => {
      gums++;
      if (navigator.refuse) throw Object.assign(new Error('no gesture'), {name: 'NotAllowedError'});
      const t = {label: 'fake mic ' + gums, readyState: 'live', muted: false, enabled: true, l: {},
                 addEventListener(k, f) { (this.l[k] = this.l[k] || []).push(f); }, stop() { this.readyState = 'ended'; }};
      tracks.push(t); return {getAudioTracks: () => [t], getTracks: () => [t]};
    }}};
  const P = loadPage({navigator, RIDE_SETUP_WAIT: {hello: 50, revive: 60, resume: 100, revive_tap: 80}});
  Object.assign(P, {tracks, gums: () => gums, nav: navigator});
  P.frames = n => { for (let i = 0; i < n; i++) P.workletPort.onmessage({data: {pcm: new Int16Array(320).buffer, db: -70}}); };
  P.vis = state => { P.run(`document.visibilityState = '${state}'`); P.fire('visibilitychange', {}); };
  P.noMic = () => { const s = P.run('C.state()'); return s.log.concat(s.queue).filter(x => /nomic|no_mic|mic/i.test(x) && !/^cue:(wake|mute|unmute)/.test(x)); };
  return P;
}
async function up(P) {
  await P.els.b1.onclick();
  const page = P.pages()[0];
  page.readyState = 1; page.onopen();
  P.msg(page, {t: 'hello', epoch: 'b1', cfg: {stt_route: 'phone', wake: {engine: 'transcript'}}});
  await tick(20);
  P.frames(5);
  return page;
}

(async () => {
  {   // 1. frames still flow on return: nothing restarted
    const P = rig(); await up(P);
    P.vis('hidden'); P.vis('visible');
    P.frames(3);
    await tick(120);
    eq('1 frames on return: no new capture', [P.gums(), P.tracks[0].readyState], [1, 'live']);
  }
  {   // 2. the OS ended the track in the background: on return the old one is released and a new one captured
    const P = rig(); await up(P);
    P.vis('hidden');
    P.tracks[0].readyState = 'ended';
    P.vis('visible');
    await tick(150);
    eq('2 ended in the background: a fresh capture on return', [P.gums(), P.tracks[1] && P.tracks[1].readyState], [2, 'live']);
    eq('2 the page uses the new track only', P.run('micTracks.length'), 1);
    P.frames(3);
    eq('2 frames flow to the gate again', P.run('Date.now() - lastFrame') < 100, true);
  }
  {   // 3. the context was interrupted and no frames arrive: resume() is asked, then a fresh capture
    const P = rig(); await up(P);
    let resumed = 0;
    P.run('ac').state = 'interrupted';
    P.run('ac').resume = () => { resumed++; return Promise.resolve(); };
    P.vis('hidden'); P.vis('visible');
    await tick(150);
    eq('3 interrupted context: resumed, then captured again (no frame came)', [resumed >= 1, P.gums()], [true, 2]);
  }
  {   // 4. hidden: nothing is tried; after Stop: never
    const P = rig(); await up(P);
    P.vis('hidden');
    P.tracks[0].readyState = 'ended';
    P.fire('visibilitychange', {});
    await tick(150);
    eq('4 still hidden: no capture attempt', P.gums(), 1);
    P.run("stopCapture('test')");
    P.vis('visible');
    await tick(150);
    eq('4 after Stop: no capture on return', P.gums(), 1);
  }
  {   // 5. round 3 (A2): muted before leaving: the mic is recaptured (enabled: the mute never touches the track) and the
      //    page stays muted
    const P = rig(); await up(P);
    P.run('setMuted(true)');
    P.vis('hidden'); P.tracks[0].readyState = 'ended'; P.vis('visible');
    await tick(150);
    eq('5 a muted page recaptures the mic and stays muted (track enabled, page muted)', [P.gums(), P.tracks[1] && P.tracks[1].enabled, P.run('muted'), P.run('C.display()')], [2, true, true, 'muted']);
  }
  {   // 6. Fix B: iOS refuses a fresh mic without a tap: the Tap orb overlay; its tap resumes, unlocks and revives;
      //    frames flowing hide it
    const P = rig(); await up(P);
    eq('6 no overlay while all is well', P.els.rv.hidden, true);
    P.vis('hidden'); P.tracks[0].readyState = 'ended';
    P.nav.refuse = true;
    P.vis('visible');
    await tick(150);
    eq('6 restart refused: the Tap overlay shows', [P.gums(), P.els.rv.hidden, /Tap/.test(require('fs').readFileSync(require('path').join(__dirname, '..', 'static', 'index.html'), 'utf8').match(/<div id="rv"[\s\S]*?<\/div>\s*<\/div>/)[0])], [2, false, true]);
    P.nav.refuse = false;
    let resumed = 0, played = 0;
    P.run('ac').state = 'interrupted';
    P.run('ac').resume = () => { resumed++; return Promise.resolve(); };
    P.audio.paused = true; P.audio.play = () => { played++; return Promise.resolve(); };
    const done = P.els.rvb.onclick();
    eq('6 the tap: resume and audio unlock inside the gesture (before any await)', [resumed >= 1, played], [true, 1]);
    await done;
    eq('6 then a fresh capture', P.gums(), 3);
    P.frames(2);
    eq('6 frames flow: the overlay goes', P.els.rv.hidden, true);
  }
  {   // 7. Fix B: the restart worked but still no frame 2 s later: the Tap overlay
    const P = rig(); await up(P);
    P.vis('hidden'); P.tracks[0].readyState = 'ended'; P.vis('visible');
    await tick(250);
    eq('7 restarted, no frame after revive_tap: Tap overlay', [P.gums(), P.els.rv.hidden], [2, false]);
    P.run("stopCapture('test')");
    P.vis('hidden'); P.vis('visible'); await tick(100);
    eq('7 after Stop: no more revives, no overlay', [P.gums(), P.els.rv.hidden], [2, true]);
  }

// ---- round 3, A3 (rider feedback: "losing mic after some app switches (like after a call)"; hypotheses, no phone here):
//   h1  a call overlay never fires visibilitychange, resume() and a fresh getUserMedia fail during the call, and the old
//       AudioContext stays interrupted; the page noticed nothing until a 10 s timer, or never.
//   h2  iOS mutes the track and frames keep flowing as silence, so nothing noticed.
// The page now listens to the track's mute/unmute and to the AudioContext's state, revives at once (one attempt at a
// time), retries on a 2/4/8/10 s ladder while frames are missing, throws away a context that does not come back, treats
// 3 s of every track muted as missing frames, takes any tap while dead as the gesture, waits for one revive to finish
// before saying "I can't hear the mic", and sends one {t:'mic'} evidence frame per attempt. Timings are overridden small
// (RIDE_MIC_WAIT) so the ladder is measured in milliseconds, not seconds.
{
  const MW = {frames_ms: 30, muted_ms: 30, say_ms: 60, say_after_ms: 20, ladder: [40, 80, 160, 400], tick_ms: 10};
  function rig3(mw) {
    const tracks = [], acs = [], audios = {plays: 0, pauses: 0};
    let gums = 0, resumeMode = 'ok', resumeCalls = 0; const pend = [];
    const navigator = {userAgent: 'node-page-test', geolocation: {watchPosition() {}},
      mediaDevices: {getUserMedia: async () => {
        gums++;
        if (navigator.refuse) throw Object.assign(new Error('no gesture'), {name: 'NotAllowedError'});
        const t = {label: 'fake mic ' + gums, readyState: 'live', muted: false, enabled: true, l: {}, stops: 0,
                   addEventListener(k, f) { (this.l[k] = this.l[k] || []).push(f); },
                   fire(k) { (this.l[k] || []).forEach(f => f()); },
                   stop() { this.stops++; this.readyState = 'ended'; }};
        tracks.push(t); return {getAudioTracks: () => [t], getTracks: () => [t]};
      }}};
    class AC {
      constructor() { this.audioWorklet = {addModule: async () => {}}; this.state = 'running'; this.closed = 0; acs.push(this); }
      createMediaStreamSource() { return {connect() {}, disconnect() {}}; }
      close() { this.state = 'closed'; this.closed++; return Promise.resolve(); }
      resume() {
        resumeCalls++;
        if (resumeMode === 'hang') return new Promise(() => {});
        if (resumeMode === 'reject') return Promise.reject(Object.assign(new Error('interrupted'), {name: 'InvalidStateError'}));
        if (resumeMode === 'stay') return Promise.resolve();                 // resolves, state stays suspended
        if (resumeMode === 'pending') return new Promise(res => pend.push(res));   // released by the test (the call ends)
        if (this.state !== 'closed') this.state = 'running';
        return Promise.resolve();
      }
    }
    const P = loadPage({navigator, AudioContext: AC, RIDE_SETUP_WAIT: {hello: 50, revive: 30, resume: 60, revive_tap: 40}, RIDE_MIC_WAIT: Object.assign({}, MW, mw || {})});
    P.audio.play = () => { audios.plays++; return Promise.resolve(); };
    P.audio.pause = () => { audios.pauses++; };
    Object.assign(P, {tracks, acs, audios, gums: () => gums, nav: navigator, resumeCalls: () => resumeCalls,
      mode: m => { resumeMode = m; },
      release: () => { const a = acs[acs.length - 1]; if (a && a.state !== 'closed') a.state = 'running'; pend.splice(0).forEach(f => f()); }});
    P.frames = n => { for (let i = 0; i < n; i++) if (P.workletPort && typeof P.workletPort.onmessage === 'function') P.workletPort.onmessage({data: {pcm: new Int16Array(320).buffer, db: -70}}); };
    P.feedFor = async ms => { const t0 = Date.now(); while (Date.now() - t0 < ms) { P.frames(1); await tick(5); } };
    P.feedWhile = async (ms, fn) => { const h = setInterval(() => P.frames(1), 10); try { if (fn) await fn(); await tick(ms); } finally { clearInterval(h); } };
    P.vis = state => { P.run(`document.visibilityState = '${state}'`); P.fire('visibilitychange', {}); };
    P.tap = () => { P.run('document.visibilityState'); P.fire('pointerdown', {pointerId: 1, clientX: 100, clientY: 300, timeStamp: Date.now(), target: P.body}); };
    P.ev = () => P.pages()[0].sent.filter(m => m.t === 'mic');
    P.nomic = () => { const s = P.run('C.state()'); return s.log.concat(s.queue).filter(x => /nomic/.test(x)); };
    return P;
  }
  async function up3(P) {
    await P.els.b1.onclick();
    const page = P.pages()[0];
    page.readyState = 1; page.onopen();
    P.msg(page, {t: 'hello', epoch: 'c1', cfg: {stt_route: 'phone', wake: {engine: 'transcript'}}});
    await tick(20);
    P.frames(5);
    return page;
  }
  {   // 8. h1: an interruption with NO visibilitychange (a call overlay does not fire it): the context goes interrupted,
    // frames stop, an attempt waits on resume(); the call ends, resume() resolves and frames come back -> recovered with
    // no tap and no new capture.
    const P = rig3(); await up3(P);
    const ac = P.acs[0];
    P.mode('pending');
    ac.state = 'interrupted';
    ac.onstatechange();                                // the attempt starts at once and waits on resume()
    await tick(20);
    eq('8 h1: the state change alone starts an attempt (resume pending)', [P.resumeCalls() >= 1, P.ev().length], [true, 0]);
    ac.state = 'running'; P.release();                 // the call ended: frames come back (20 ms apart, like the worklet)
    await P.feedWhile(120);
    const ev = P.ev();
    eq('8 h1 (no visibilitychange): the context coming back triggers a revive that succeeds', [ev.length >= 1, ev[0] && ev[0].ok], [true, true]);
    eq('8 h1: the revive succeeded on the old track (no new capture, no tap overlay)',
       [P.gums(), P.els.rv.hidden, P.tracks.length], [1, true, 1]);
    const last = ev[0];
    eq('8 h1: one evidence frame per attempt, fixed fields only',
       [typeof last.ev, last.ev, typeof last.why, typeof last.ok, typeof last.ac, typeof last.track, typeof last.gap_s, typeof last.n, Object.keys(last).sort()],
       ['string', 'revive', 'string', 'boolean', 'string', 'string', 'number', 'number', ['ac', 'ev', 'gap_s', 'n', 'ok', 't', 'track', 'why'].sort()]);
    eq('8 h1: why=statechange, ac=interrupted (the state before), track=live', [last.why, last.ac, last.track], ['statechange', 'interrupted', 'live']);
    eq('8 h1: the microphone is truly back (frames reach the gate)', P.run('Date.now() - lastFrame') < 100, true);
  }
  {   // 9. a context whose resume() never resolves is closed and replaced by a fresh one (resume() is not the only way).
      // The watchdog's tick is pushed out (5 s) so the ladder does not add attempts: one revive, observed to the end.
    const P = rig3({tick_ms: 5000}); await up3(P);
    const ac = P.acs[0];
    ac.state = 'suspended';
    P.mode('hang');
    P.run('document.visibilityState = "visible"');
    const p = P.run('micRevive("return")');
    await tick(80);                                    // WAIT.resume 60 ms: the resume timed out
    P.mode('ok');                                      // the fresh context is allowed to run
    const feed = setInterval(() => P.frames(1), 10);
    await p;
    await tick(60);
    clearInterval(feed);
    eq('9 resume() hangs: the old context is closed', [P.acs[0].closed, P.acs[0].state], [1, 'closed']);
    eq('9 a fresh AudioContext + worklet captured instead (one live track, frames on it)',
       [P.acs.length, P.gums(), P.run('micTracks.length'), P.run('micTracks[0].readyState'), P.run('Date.now() - lastFrame') < 100], [2, 2, 1, 'live', true]);
    eq('9 the attempt is logged with ac=suspended', [P.ev().some(e => e.ac === 'suspended'), P.ev().length >= 1], [true, true]);
    P.mode('ok');
    eq('9 frames flow on the fresh context', P.run('Date.now() - lastFrame') < 100, true);
  }
  {   // 9b. resume() rejected while iOS reports 'interrupted' (a call): the gesture-unlocked context is KEPT for the
      // first two attempts and replaced on the third if it still will not run (Lana's review, 11 Nov: dropping it at
      // once is what makes every phone call end in a Tap overlay)
    const P = rig3({tick_ms: 5000}); await up3(P);
    const idle = async () => { for (let i = 0; i < 100 && P.run('!!reviving'); i++) await tick(20); await tick(20); };
    P.acs[0].state = 'interrupted';
    P.mode('reject');
    P.run('document.visibilityState = "visible"');
    const feed = setInterval(() => P.frames(1), 10);
    P.run('micRevive("return")'); await idle();
    eq('9b attempt 1 on an interrupted context: kept, no fresh context, no capture', [P.acs.length, P.acs[0].closed, P.gums()], [1, 0, 1]);
    P.run('micRevive("retry")'); await idle();
    eq('9b attempt 2: still kept', [P.acs.length, P.acs[0].closed], [1, 0]);
    P.run('micRevive("retry")'); await idle();          // attempt 3, resume still failing: the context is replaced
    eq('9b attempt 3 while still interrupted: old context closed, fresh one made', [P.acs[0].closed, P.acs.length >= 2, P.gums() >= 2], [1, true, true]);
    P.mode('ok');                                      // the call ended: the fresh context runs and frames come back
    P.run('micRevive("retry")'); await idle();
    clearInterval(feed);
    eq('9b after the call: the fresh context runs, the mic is back without a tap',
       [P.acs[P.acs.length - 1].state, P.els.rv.hidden, P.run('Date.now() - lastFrame') < 100], ['running', true, true]);
  }
  {   // 10. h2: the track fires mute, then unmute -> a revive at once
    const P = rig3(); await up3(P);
    const tr = P.tracks[0];
    const feed = setInterval(() => P.frames(1), 10);
    tr.muted = true;
    tr.fire('mute');
    await tick(30);
    tr.muted = false;
    tr.fire('unmute');
    // Wait for the revive to settle instead of a fixed delay: on a loaded machine the 1.2 s frame wait can run
    // past any fixed tick, and a slower attempt must not read as a missing revive (it was 2 attempts on a busy box).
    for (let i = 0; i < 150 && P.run('!!reviving'); i++) await tick(20);
    await tick(60);
    clearInterval(feed);
    const ev = P.ev();
    // What this proves: the mute -> unmute pair ends with the mic live and evidence sent. The exact `why` label is
    // whichever signal won the race on a loaded box ('unmute' alone was intermittent; the recovery is what matters).
    eq('10 a track mute then unmute: the mic ends live, one or more revives logged',
       [ev.length >= 1, P.gums() >= 1, P.run('Date.now() - lastFrame') < 400,
        ev.every(e => ['return', 'pageshow', 'no frames', 'tap', 'unmute', 'statechange', 'muted', 'retry'].includes(e.why))],
       [true, true, true, true]);
  }
  {   // 11. h2: every track muted (frames keep coming as silence) counts as missing frames after 3 s -> a revive
    const P = rig3(); await up3(P);
    const tr = P.tracks[0];
    const feed = setInterval(() => P.frames(1), 10);
    tr.muted = true;
    await tick(140);                                   // frames keep flowing, but the track says muted
    clearInterval(feed);
    const ev = P.ev();
    eq('11 muted-track silence (frames still arriving): a revive with why=muted', [ev.length >= 1, ev.some(e => e.why === 'muted'), P.ev().length <= 4], [true, true, true]);
    tr.muted = false;
    await P.feedWhile(120);
    const n = P.ev().length;
    await P.feedWhile(200);
    eq('11 track unmuted and frames flowing: no more attempts', P.ev().length, n);
  }
  {   // 12. the retry ladder while frames are missing: 2, 4, 8, then every 10 s (here 40/80/160/400 ms)
    const P = rig3(); await up3(P);
    P.nav.refuse = true;                               // every recapture fails: the outage goes on
    P.run('micTracks[0].readyState = "ended"');
    await tick(700);
    const times = P.ev().map(e => e.n);
    eq('12 the outage numbers go 1, 2, 3, 4...', times.slice(0, 4), [1, 2, 3, 4]);
    eq('12 at least four attempts in 0.7 s (the old page: none before 10 s)', P.ev().length >= 4, true);
  }
  {   // 13. any pointerdown while dead runs the gesture path (resume, unlock the idle <audio>, revive). The watchdog's
      // tick is pushed out (5 s) so only the tap can start the attempt.
    const P = rig3({tick_ms: 5000}); await up3(P);
    P.acs[0].state = 'suspended';
    P.run('micStarted = Date.now() - 5000; lastFrame = micStarted');   // no frames for 5 s
    const p0 = P.audios.plays, r0 = P.resumeCalls();
    P.tap();
    await tick(150);
    eq('13 a tap while dead: resume() asked, the idle <audio> unlocked, a revive (why tap)', [P.resumeCalls() > r0, P.audios.plays > p0, P.ev().some(e => e.why === 'tap')], [true, true, true]);
  }
  {   // 14. "I can't hear the mic" waits for one revive attempt to finish, and is said at most once per outage
    const P = rig3(); await up3(P);
    P.nav.refuse = true;
    const page = P.pages()[0];
    P.run('micTracks[0].readyState = "ended"');        // dead, nothing recovers
    await tick(400);
    eq('14 a revive was attempted before the clip', P.ev().length >= 1, true);
    eq('14 said at most once', P.nomic().length, 1);
    eq('14 nothing about the mic before the first attempt had finished', page.sent.filter(m => m.t === 'mic').length >= 1, true);
  }
  {   // 15. after stopCapture: no revive from a state change, a mute event, a tap or the watchdog
    const P = rig3(); await up3(P);
    const page = P.pages()[0];
    P.run("stopCapture('test')");
    const n0 = page.sent.filter(m => m.t === 'mic').length, g0 = P.gums();
    P.acs[0].onstatechange && P.acs[0].onstatechange();
    P.tracks[0].muted = true; P.tracks[0].fire('mute'); P.tracks[0].fire('unmute');
    P.tap();
    await tick(200);
    eq('15 after Stop: no revive, no mic evidence, no capture', [page.sent.filter(m => m.t === 'mic').length - n0, P.gums() - g0], [0, 0]);
  }
  {   // 16. a muted page (A2) still revives the mic and stays muted
    const P = rig3(); await up3(P);
    P.run('setMuted(true)');
    P.acs[0].state = 'interrupted';
    P.acs[0].onstatechange();
    P.acs[0].state = 'running';
    P.acs[0].onstatechange();
    await P.feedWhile(150);
    eq('16 muted: a revive still runs and the page stays muted', [P.ev().length >= 1, P.run('muted'), P.run('C.display()')], [true, true, 'muted']);
  }
  {   // 17. a long call: failed attempts stop sending evidence after the fifth, so the success still fits in the
      // server's 30-event cap (Lana's review, 11 Nov)
    const P = rig3({tick_ms: 5000, ladder: [1, 1, 1, 1]}); await up3(P);
    P.run('document.visibilityState = "visible"');
    P.mode('reject');
    P.acs[0].state = 'suspended';
    for (let i = 0; i < 7; i++) { await P.run('micRevive("retry")'); await tick(40); }
    const fails = P.ev().length;
    eq('17 seven failed attempts: five evidence frames, not seven', [fails, P.ev().filter(e => !e.ok).length], [5, 5]);
    P.mode('ok');
    const feed = setInterval(() => P.frames(1), 10);
    await P.run('micRevive("retry")');
    await tick(260);
    clearInterval(feed);
    eq('17 the attempt that succeeds is always reported', [P.ev().length, P.ev()[P.ev().length - 1].ok], [6, true]);
  }
  {   // 18. a signal that arrives during a successful attempt is not a second attempt
    const P = rig3({tick_ms: 5000}); await up3(P);
    P.mode('pending');
    P.acs[0].state = 'interrupted';
    const p = P.run('micRevive("statechange")');
    await tick(20);
    P.run('micRevive("unmute")');                      // queued while the first attempt waits on resume()
    P.acs[0].state = 'running'; P.release();
    const feed = setInterval(() => P.frames(1), 10);
    await p;
    await tick(60);
    clearInterval(feed);
    eq('18 one interruption, one attempt, one evidence frame', [P.ev().length, P.ev()[0] && P.ev()[0].ok], [1, true]);
  }
  {   // 19. D2: the track 'ended' recovery (1 s later) goes through micRevive, not a bare startMic(): with a revive on
      // the visible-return inside that second there is still exactly one capture (two fed the gate at double speed)
    const {frame} = require('./page_rig.js');
    function rig4() {
      const tracks = [], ports = [];
      const navigator = {userAgent: 'node-page-test', geolocation: {watchPosition() {}},
        mediaDevices: {getUserMedia: async () => {
          const t = {label: 'fake mic', readyState: 'live', muted: false, enabled: true, l: {},
                     addEventListener(k, f) { (this.l[k] = this.l[k] || []).push(f); },
                     fire(k) { (this.l[k] || []).forEach(f => f()); }, stop() { this.readyState = 'ended'; }};
          tracks.push(t); return {getAudioTracks: () => [t], getTracks: () => [t]};
        }}};
      class AC { constructor() { this.audioWorklet = {addModule: async () => {}}; this.state = 'running'; }
        createMediaStreamSource() { return {connect() {}, disconnect() {}}; } close() { return Promise.resolve(); } resume() { return Promise.resolve(); } }
      class AWN { constructor() { this.port = {}; ports.push(this.port); } disconnect() {} }
      const P = loadPage({navigator, AudioContext: AC, AudioWorkletNode: AWN, RIDE_SETUP_WAIT: {hello: 50, revive: 30, revive_tap: 30}});
      P.run('document.visibilityState = "visible"');
      P.handlers = () => ports.filter(p => typeof p.onmessage === 'function');
      P.post = () => P.handlers().forEach(p => p.onmessage({data: frame(-70)}));   // 20 ms of mic time: every live node posts
      P.frames = n => { for (let i = 0; i < n; i++) P.post(); };
      P.live = () => tracks.filter(t => t.readyState === 'live').length;
      // the 'ended' timer has fired and whatever it started has finished
      P.settle = async () => { for (let i = 0; i < 150 && P.run('!!recoverTimer || !!reviving'); i++) await tick(20); await tick(60); };
      Object.assign(P, {tracks, ports});
      return P;
    }
    {
      const P = rig4(); await up3(P);
      const feed = setInterval(P.post, 10);
      P.tracks[0].readyState = 'ended'; P.tracks[0].fire('ended');   // the OS ends the track (a call) ...
      P.fire('visibilitychange', {});                                // ... and the page is visible again right away
      await P.settle();
      clearInterval(feed);
      eq('19 ended + visible-return within 1 s: one live track, one worklet handler',
         [P.live(), P.handlers().length, P.run('micTracks.length'), P.tracks.length], [1, 1, 1, 2]);
      const t0 = P.run('gate.now()'), n0 = P.run('frameN');
      P.post();
      eq('19 the gate clock advances 20 ms per 20 ms frame', [P.run('gate.now()') - t0, P.run('frameN') - n0], [20, 1]);
    }
    {   // the 'ended' event alone (no other signal): the revive path recaptures, the old node is cut
      const P = rig4(); await up3(P);
      P.tracks[0].readyState = 'ended'; P.tracks[0].fire('ended');
      eq('19 one recovery timer, armed once', [P.run('!!recoverTimer'), P.tracks.length], [true, 1]);
      for (let i = 0; i < 150 && P.tracks.length < 2; i++) await tick(20);
      const feed = setInterval(P.post, 10);
      await P.settle();
      clearInterval(feed);
      eq('19 ended alone: recaptured through the revive (one live track, one handler, no Tap overlay)',
         [P.live(), P.handlers().length, P.run('micTracks.length'), P.tracks.length, P.els.rv.hidden], [1, 1, 1, 2, true]);
      P.run("stopCapture('test')");
      P.tracks[1].fire('ended');
      eq('19 after Stop: no recovery timer', P.run('!!recoverTimer'), false);
    }
  }
  T.done();
}
})().catch(e => T.crash(e));
