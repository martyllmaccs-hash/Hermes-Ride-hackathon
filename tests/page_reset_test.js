// WP6 (D5, P6): the reset on the phone page. The real index.html scripts in node (tests/page_rig.js) with pointer events
// dispatched on the document. A visible grabber near the bottom edge; an upward drag from it reveals the reset bar and
// the MUTE button (#mute, where the hard stop #stop was); holding the red bar 3 s (it fills while held; a release or a movement cancels)
// sends {t:'reset', epoch} once and plays a short cue; the page reloads on {t:'reset_done'} or after 5 s. A failure
// ({t:'reset_failed'}) is shown with its own cue and the bar can be held again. The reveal and everything in it carry
// data-no-dbltap (no tap wake from taps there). Real time for the acceptance timings (3 s hold, release at 2.9 s, 5 s);
// window.RIDE_RESET_WAIT shortens the rest. Prints {checks, failures}.
'use strict';
const fs = require('fs'), path = require('path');
const {loadPage, tick, checker} = require('./page_rig.js');
const T = checker(), {eq} = T;
const HTML = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');

function rig(over) {
  const tracks = [];
  const navigator = {userAgent: 'node-page-test', geolocation: {watchPosition() {}},
    mediaDevices: {getUserMedia: async () => {
      const t = {label: 'fake mic', readyState: 'live', enabled: true, addEventListener() {}, stop() { this.readyState = 'ended'; }};
      tracks.push(t); return {getAudioTracks: () => [t], getTracks: () => [t]};
    }}};
  const reloads = [];
  const location = {href: 'https://ride.test/', reload() { reloads.push(Date.now()); }};
  const store = {};
  const sessionStorage = {getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; }};
  const P = loadPage(Object.assign({navigator, location, sessionStorage, RIDE_SETUP_WAIT: {hello: 500}}, over || {}));
  Object.assign(P, {tracks, reloads, store});
  P.t = 10000;
  const ev = (id, x, y, target) => ({pointerId: id, isPrimary: id === 1, pointerType: 'touch', clientX: x, clientY: y,
                                     timeStamp: P.t, target: target || P.body});
  P.down = (id, x, y, target) => P.fire('pointerdown', ev(id, x, y, target));
  P.move = (id, x, y, target) => P.fire('pointermove', ev(id, x, y, target));
  P.up = (id, x, y, target) => P.fire('pointerup', ev(id, x, y, target));
  P.cancel = (id, x, y, target) => P.fire('pointercancel', ev(id, x, y, target));
  P.reveal = () => {                     // an upward drag from the grabber
    const g = P.els.grab;
    P.down(1, 200, 860, g); P.t += 50; P.move(1, 200, 830, g); P.t += 50; P.move(1, 200, 780, g); P.t += 30; P.up(1, 200, 780, g);
  };
  P.resets = () => P.pages()[0].sent.filter(m => m.t === 'reset');
  P.cues = () => { const st = P.run('C.state()'); return [...st.log, ...st.queue].filter(x => /^cue:reset/.test(x)); };   // played + queued
  return P;
}
async function start(over) {
  const P = rig(over);
  await P.els.b1.onclick();
  const page = P.pages()[0];
  page.readyState = 1; page.onopen();
  P.msg(page, {t: 'hello', epoch: 'E1', cfg: {stt_route: 'phone', wake: {engine: 'transcript'}}});
  await tick(20);
  return P;
}
const inside = (P, id, anc) => { for (let n = P.els[id]; n; n = n.parentElement) if (n === P.els[anc]) return true; return false; };

(async () => {
  {   // 1. markup: the reveal holds the grabber, the bar and the hard stop; all excluded from the double tap
    const P = rig();
    eq('1 the reveal is marked data-no-dbltap', P.els.reveal.hasAttribute('data-no-dbltap'), true);
    eq('1 grabber, bar, fill and #mute are inside the reveal', ['grab', 'resetbar', 'resetfill', 'mute'].map(i => inside(P, i, 'reveal')), [true, true, true, true]);
    eq('1 rideNoDoubleTap: #mute, the bar, the grabber', ['mute', 'resetbar', 'grab'].map(i => P.run(`rideNoDoubleTap($('${i}'))`)), [true, true, true]);
    eq('1 hidden before tap 1, panel closed', [P.els.reveal.hidden, P.els.rpanel.hidden], [true, true]);
    eq('1 defaults: hold 3 s, reload after 5 s', [P.run('RIDE_RESET_DEFAULTS.hold_ms'), P.run('RIDE_RESET_DEFAULTS.reload_ms')], [3000, 5000]);
    const css = (HTML.match(/#reveal\{[^}]*\}/) || [''])[0], grab = (HTML.match(/#grab\{[^}]*\}/) || [''])[0],
          bar = (HTML.match(/#resetbar\{[^}]*\}/) || [''])[0];
    eq('1 the reveal sits above the iOS home indicator (safe-area inset + margin)', /bottom:calc\(\s*\d+px \+ env\(safe-area-inset-bottom\)\)/.test(css), true);
    eq('1 the grabber is visible (a pill with a colour) and at least 44 px tall to touch', [/background:#[0-9a-f]{3,6}/.test(HTML.match(/#grab::after\{[^}]*\}/) ? HTML.match(/#grab::after\{[^}]*\}/)[0] : ''),
       Number((grab.match(/height:(\d+)px/) || [])[1]) >= 44], [true, true]);
    const fill = (HTML.match(/#resetfill\{[^}]*\}/) || [''])[0];
    eq('1 the red bar is a large target (>= 64 px tall, >= 80vw wide), its fill red', [Number((bar.match(/height:(\d+)px/) || [])[1]) >= 64,
       Number((bar.match(/width:(\d+)vw/) || [])[1]) >= 80, /background:#[c-f][0-9a-f][0-3][0-9a-f][0-3][0-9a-f]\b/.test(fill)], [true, true, true]);
  }
  {   // 2. reveal: only an upward drag from the grabber; a hold while closed does nothing
    const P = await start();
    eq('2 after setup the grabber shows, the panel is closed', [P.els.reveal.hidden, P.els.rpanel.hidden], [false, true]);
    P.down(1, 200, 860, P.els.resetbar); await tick(3100); P.up(1, 200, 860, P.els.resetbar);
    eq('2 holding the (closed) bar sends nothing', P.resets().length, 0);
    const g = P.els.grab;
    P.down(1, 200, 860, g); P.move(1, 200, 875, g); P.up(1, 200, 875, g);
    eq('2 a downward drag does not reveal', P.els.rpanel.hidden, true);
    P.down(1, 200, 860, g); P.move(1, 210, 850, g); P.up(1, 210, 850, g);
    eq('2 a small drag (10 px) does not reveal', P.els.rpanel.hidden, true);
    P.down(1, 200, 860, P.body); P.move(1, 200, 700, P.body); P.up(1, 200, 700, P.body);
    eq('2 an upward drag elsewhere does not reveal', P.els.rpanel.hidden, true);
    P.reveal();
    eq('2 an upward drag from the grabber reveals the bar and #mute', [P.els.rpanel.hidden, P.els.reveal.classList.contains('open'), P.els.mute.hidden], [false, true, false]);
    eq('2 revealing sends nothing', P.resets().length, 0);
  }
  {   // 3. hold 3 s: sends once, with the link's epoch, a cue, a visible note
    const P = await start();
    P.reveal();
    const bar = P.els.resetbar;
    P.down(1, 200, 800, bar);
    eq('3 the bar fills while held', bar.classList.contains('holding'), true);
    await tick(1500);
    eq('3 nothing sent at 1.5 s', P.resets().length, 0);
    await tick(1600);
    eq('3 held 3 s: one reset with the epoch', P.resets(), [{t: 'reset', epoch: 'E1'}]);
    eq('3 a short reset cue', P.cues(), ['cue:reset']);
    eq('3 no text on screen while it resets (diag only)', [/reset/.test(P.els.diag.textContent), P.els.status.textContent], [true, '']);
    await tick(1000); P.up(1, 200, 800, bar);
    eq('3 holding on and releasing: still one', P.resets().length, 1);
    P.down(1, 200, 800, bar); await tick(3100); P.up(1, 200, 800, bar);
    eq('3 a second hold while one is pending: still one', P.resets().length, 1);
    P.msg(P.pages()[0], {t: 'reset_done', epoch: 'E2'});
    eq('3 reset_done of another epoch: no reload', P.reloads.length, 0);
    P.msg(P.pages()[0], {t: 'reset_done', epoch: 'E1'});
    eq('3 reset_done: the page reloads', P.reloads.length, 1);
    eq('3 the reload is remembered for the setup screen', P.store['ride-reset'] != null, true);
    await tick(5100);
    eq('3 no second reload from the 5 s timer', P.reloads.length, 1);
  }
  {   // 4. release at 2.9 s: nothing
    const P = await start();
    P.reveal();
    const bar = P.els.resetbar;
    P.down(1, 200, 800, bar); await tick(2900); P.up(1, 200, 800, bar);
    eq('4 released at 2.9 s: the fill drops back', bar.classList.contains('holding'), false);
    await tick(600);
    eq('4 released at 2.9 s: nothing sent, no cue, no reload', [P.resets().length, P.cues(), P.reloads.length], [0, [], 0]);
  }
  {   // 5. a swipe across the bar without a hold, a movement while holding, a second finger, a cancel
    const P = await start();
    P.reveal();
    const bar = P.els.resetbar;
    P.down(1, 100, 800, bar); P.move(1, 160, 800, bar); P.move(1, 300, 790, bar); P.up(1, 300, 790, bar);
    await tick(3200);
    eq('5 a swipe across the bar sends nothing', P.resets().length, 0);
    P.down(1, 200, 800, bar); await tick(1000); P.move(1, 240, 800, bar); await tick(2300);
    eq('5 moving 40 px while holding cancels', [P.resets().length, bar.classList.contains('holding')], [0, false]);
    P.up(1, 240, 800, bar);
    P.down(1, 200, 800, bar); await tick(500); P.down(2, 260, 600, P.body); await tick(2800);
    eq('5 a second finger cancels', P.resets().length, 0);
    P.up(2, 260, 600, P.body); P.up(1, 200, 800, bar);
    P.down(1, 200, 800, bar); await tick(500); P.cancel(1, 200, 800, bar); await tick(2800);
    eq('5 pointercancel cancels', P.resets().length, 0);
    P.down(1, 200, 800, bar); P.move(1, 210, 805, bar); await tick(3100);
    eq('5 a small wobble (gloves, 11 px) still counts as held', P.resets().length, 1);
  }
  {   // 6. no answer: reload after 5 s
    const P = await start();
    P.reveal();
    P.down(1, 200, 800, P.els.resetbar); await tick(3050);
    eq('6 sent', P.resets().length, 1);
    await tick(4800);
    eq('6 no reload before 5 s', P.reloads.length, 0);
    await tick(400);
    eq('6 reloaded after 5 s', P.reloads.length, 1);
    P.msg(P.pages()[0], {t: 'reset_done', epoch: 'E1'});
    eq('6 a late reset_done: no second reload', P.reloads.length, 1);
  }
  {   // 7. failure: visible, own cue, no reload, the bar can be held again
    const P = await start({RIDE_RESET_WAIT: {hold_ms: 100, reload_ms: 300}});
    P.reveal();
    P.down(1, 200, 800, P.els.resetbar); await tick(150); P.up(1, 200, 800, P.els.resetbar);
    eq('7 short hold (test timing) sends', P.resets().length, 1);
    P.msg(P.pages()[0], {t: 'reset_failed', step: 'new', epoch: 'E1'});
    eq('7 failure shown with the step', /reset failed/.test(P.els.status.textContent) && /new/.test(P.els.status.textContent), true);
    eq('7 failure cue', P.cues(), ['cue:reset', 'cue:reset_failed']);
    await tick(500);
    eq('7 no reload after a failure', P.reloads.length, 0);
    P.down(1, 200, 800, P.els.resetbar); await tick(150); P.up(1, 200, 800, P.els.resetbar);
    eq('7 the bar can be held again', P.resets().length, 2);
  }
  {   // 8. link down: nothing to send, the page still reloads (a fresh page is the reset it can do)
    const P = await start({RIDE_RESET_WAIT: {hold_ms: 100, reload_ms: 300}});
    P.pages()[0].onclose({code: 1006});
    P.reveal();
    P.down(1, 200, 800, P.els.resetbar); await tick(150); P.up(1, 200, 800, P.els.resetbar);
    await tick(400);
    eq('8 link down: no reset frame, reloaded', [P.pages()[0].sent.filter(m => m.t === 'reset').length, P.reloads.length], [0, 1]);
  }
  {   // 9. round 3: MUTE inside the reveal works with one tap; a double tap gesture in the reveal never wakes
    const P = await start();
    P.reveal();
    const stop = P.els.mute;
    P.down(1, 50, 820, stop); P.t += 60; P.up(1, 50, 820, stop); P.t += 150; P.down(1, 50, 820, stop); P.t += 60; P.up(1, 50, 820, stop);
    eq('9 a double tap gesture on #mute does not wake (pointer events alone never mute either)', [P.run('muted'), P.pages()[0].sent.filter(m => m.t === 'wake').length], [false, 0]);
    const g = P.els.grab;
    P.t += 1000; P.down(1, 200, 860, g); P.t += 60; P.up(1, 200, 860, g); P.t += 150; P.down(1, 200, 860, g); P.t += 60; P.up(1, 200, 860, g);
    eq('9 a double tap on the grabber does not wake or mute', [P.run('muted'), P.pages()[0].sent.filter(m => m.t === 'wake').length], [false, 0]);
    stop.onclick();
    eq('9 #mute: one click mutes (UNMUTE shown), no arm step', [P.run('muted'), stop.textContent, stop.classList.contains('arm')], [true, 'UNMUTE', false]);
    stop.onclick();
    eq('9 #mute again: unmuted, the mic was never stopped', [P.run('muted'), P.tracks.every(t => t.readyState !== 'ended'), /mic off/.test(P.els.status.textContent)], [false, true, false]);
  }
  {   // 10. the reveal closes by itself when left alone (the screen goes back to black)
    const P = await start({RIDE_RESET_WAIT: {hide_ms: 200}});
    P.reveal();
    eq('10 open', P.els.rpanel.hidden, false);
    await tick(300);
    eq('10 closed again after hide_ms', [P.els.rpanel.hidden, P.els.reveal.classList.contains('open')], [true, false]);
  }
  {   // 11. after a reset reload, the setup screen says the two taps are needed again (once)
    const P = rig();
    P.store['ride-reset'] = '1';
    let cleared = false;
    const Q = rig({sessionStorage: {getItem: k => (k === 'ride-reset' ? '1' : null), setItem() {}, removeItem() { cleared = true; }}});
    eq('11 note after a reset reload', /reset done/i.test(Q.els.diag.textContent) && Q.els.note.textContent === '', true);
    eq('11 the flag is cleared', cleared, true);
    eq('11 no note on a normal load', /reset/i.test(P.els.note.textContent + P.els.diag.textContent), false);
  }
  {   // 12. a swipe down closes the open panel at once (anywhere), and cancels a hold on the bar
    const P = await start();
    P.reveal();
    eq('12 open after the swipe up', P.els.rpanel.hidden, false);
    P.down(1, 200, 500, P.body); P.t += 50; P.move(1, 200, 530, P.body); P.t += 50; P.move(1, 200, 560, P.body); P.up(1, 200, 560, P.body);
    eq('12 a swipe down closes it', [P.els.rpanel.hidden, P.els.reveal.classList.contains('open')], [true, false]);
    P.reveal();
    const bar = P.els.resetbar;
    P.down(1, 200, 700, bar); await tick(500); P.move(1, 200, 760, bar); P.up(1, 200, 760, bar);
    await tick(2700);
    eq('12 dragging down off a held bar closes the panel and sends no reset', [P.els.rpanel.hidden, P.resets().length], [true, 0]);
    P.reveal();
    P.down(1, 200, 500, P.body); P.move(1, 200, 520, P.body); P.up(1, 200, 520, P.body);
    eq('12 a short drag (< 40 px) leaves it open', P.els.rpanel.hidden, false);
  }
  T.done();
})().catch(e => T.crash(e));
