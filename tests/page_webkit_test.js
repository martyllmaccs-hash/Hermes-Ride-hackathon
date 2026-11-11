// Fix round 3, R3-7: Safari / WebKit differences on the real index.html scripts (page_rig). What node can check:
// motion permission asked inside the tap's user gesture, a prefixed-only AudioContext, no AudioWorklet, no Cache
// Storage, no Screen Wake Lock. What only the phone can show is listed in HANDOFF (R3-7).
'use strict';
const {loadPage, tick, checker} = require('./page_rig.js');
const T = checker(), {eq} = T;

(async () => {
  {   // iOS: DeviceMotionEvent / DeviceOrientationEvent.requestPermission() need the tap's user activation. A call made
      // after an await (the second one, before) may be refused: both must be asked before the first await.
    let gesture = false;
    const asked = [], added = [];
    const perm = name => ({requestPermission: () => { asked.push([name, gesture]); return Promise.resolve(gesture ? 'granted' : 'denied'); }});
    const P = loadPage({DeviceMotionEvent: perm('motion'), DeviceOrientationEvent: perm('orientation'),
                        addEventListener: t => added.push(t)});
    gesture = true;
    const p = P.els.b1.onclick();                      // one tap; motion is asked in it
    gesture = false;                                   // the synchronous part of the tap is over
    await p; await tick(5);
    eq('R3-7 both motion permissions asked inside the tap', asked, [['motion', true], ['orientation', true]]);
    eq('R3-7 devicemotion listened', added.includes('devicemotion'), true);
  }
  {   // a refused motion permission is said, not swallowed (lean and braking need it)
    const added = [];
    const deny = {requestPermission: () => Promise.resolve('denied')};
    const P = loadPage({DeviceMotionEvent: deny, DeviceOrientationEvent: deny, addEventListener: t => added.push(t)});
    await P.els.b1.onclick(); await tick(5);
    eq('R3-7 motion denied is visible', /motion/.test(P.els.status.textContent) && /not allowed|denied/.test(P.els.status.textContent), true);
  }
  {   // older WebKit: only webkitAudioContext
    const base = loadPage().ctx.AudioContext;
    const P = loadPage({AudioContext: undefined, webkitAudioContext: base});
    await P.els.b1.onclick();
    eq('R3-7 webkitAudioContext only: mic starts', /^mic: /m.test(P.els.diag.textContent), true);
  }
  {   // no AudioWorklet (iOS before 14.5): a clear reason, not a bare TypeError
    class NoWorklet { constructor() { this.state = 'running'; } createMediaStreamSource() { return {connect() {}}; }
                      resume() { return Promise.resolve(); } close() { return Promise.resolve(); } }
    const P = loadPage({AudioContext: NoWorklet});
    await P.els.b1.onclick();
    eq('R3-7 no AudioWorklet: says so', /AudioWorklet/.test(P.els.note.textContent), true);
    eq('R3-7 no AudioWorklet: tap 1 can be tried again', P.els.b1.disabled, false);
  }
  {   // no Cache Storage (e.g. a private window): cues still load from the network
    const P = loadPage({caches: undefined});
    await P.els.b1.onclick();
    eq('R3-7 no Cache Storage: cues loaded anyway', !!P.run("cueUrls['cues/done.m4a']"), true);
    eq('R3-7 no Cache Storage: mic starts', /^mic: /m.test(P.els.diag.textContent), true);
  }
  {   // no Screen Wake Lock (Safari before 16.4): no exception, setup goes on
    const P = loadPage();
    await P.els.b1.onclick();
    eq('R3-7 no wake lock: setup completes', P.run("$('setup') === null"), true);
  }
  T.done();
})().catch(T.crash);
