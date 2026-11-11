// WP5 (D1, P5): the orb, phase 1. The real index.html scripts in node (tests/page_rig.js) with counted timers and
// animation frames. Checks: the orb's DOM/classes for every display state, sleep is static (no timer, no animation
// frame, no style write, no animated CSS), the level ring follows the gate's frame level at most 20 Hz in listening
// only, the thinking sequence (swell, then a breath retriggered by each doo-doo tick), orb_design fallback, the
// layout rule (min(75vw, 75vh), centred) at 320 to 430 px and landscape, show_word as a debug option, the Media
// Session title, and nothing of the old #bg colour page left. Battery numbers (timers, frames, style writes per
// second per state) are measured here and printed as `battery`. Prints {checks, failures, battery}.
'use strict';
const fs = require('fs'), path = require('path');
const {loadPage, tick, checker} = require('./page_rig.js');
const T = checker(), {eq} = T;
const ROOT = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'static', 'index.html'), 'utf8');
const css = html.match(/<style>([\s\S]*?)<\/style>/)[1];

function rig(over) {
  const counts = {timeout: 0, interval: 0, raf: 0};
  const gps = {cb: null};                              // P4: the page's GPS callback (speed for the orb's lean)
  const perf = {t: 1000, now() { return this.t; }};   // the page's clock for orb level timestamps (performance.now)
  const P = loadPage(Object.assign({
    performance: perf,
    setTimeout: (f, ms, ...a) => { counts.timeout++; return setTimeout(f, ms, ...a); },
    setInterval: (f, ms, ...a) => { counts.interval++; return setInterval(f, ms, ...a); },
    requestAnimationFrame: f => { counts.raf++; return setTimeout(() => f(Date.now()), 16); },
    navigator: {userAgent: 'node-page-test', geolocation: {watchPosition(cb) { gps.cb = cb; }}, mediaSession: {metadata: null, setActionHandler() {}},
                mediaDevices: {getUserMedia: async () => { const t = {label: 'fake mic', readyState: 'live', enabled: true, addEventListener() {}, stop() {}};
                                                           return {getAudioTracks: () => [t], getTracks: () => [t]}; }}},
    MediaMetadata: class { constructor(m) { Object.assign(this, m); } },
    RIDE_SETUP_WAIT: {hello: 300},
  }, over || {}));
  P.counts = counts; P.perf = perf; P.gps = gps;
  P.frames = (db, n) => { for (let i = 0; i < n; i++) { perf.t += 20; P.feed(db, 1); } };   // frames at the real 20 ms cadence
  P.snap = () => Object.assign({}, counts);
  P.orb = () => P.els.orb.className;
  P.has = c => P.els.orb.classList.contains(c);
  P.lvl = () => P.els.orbl.style.transform;
  P.disp = () => P.run('C.display()');
  return P;
}
// Fix F: a 16-bit PCM WAV like the server's hermes-engine segments: 1 s of a sine at -6 dBFS peak, then 1 s of silence
function wav(rate, ch) {
  rate = rate || 16000; ch = ch || 1;
  const n = rate * 2, b = new ArrayBuffer(44 + n * 2 * ch), v = new DataView(b);
  const str = (o, x) => { for (let i = 0; i < x.length; i++) v.setUint8(o + i, x.charCodeAt(i)); };
  str(0, 'RIFF'); v.setUint32(4, 36 + n * 2 * ch, true); str(8, 'WAVE'); str(12, 'fmt '); v.setUint32(16, 16, true);
  v.setUint16(20, 1, true); v.setUint16(22, ch, true); v.setUint32(24, rate, true); v.setUint32(28, rate * 2 * ch, true);
  v.setUint16(32, 2 * ch, true); v.setUint16(34, 16, true); str(36, 'data'); v.setUint32(40, n * 2 * ch, true);
  for (let i = 0; i < n; i++) for (let c = 0; c < ch; c++)
    v.setInt16(44 + (i * ch + c) * 2, i < rate ? Math.round(16384 * Math.sin(2 * Math.PI * 220 * i / rate)) : 0, true);
  return new Uint8Array(b);
}
const wavFrame = (turn, seq) => { const w = wav(), b = new ArrayBuffer(7 + w.length), v = new DataView(b);
  v.setUint8(0, 2); v.setUint32(1, turn); v.setUint16(5, seq); new Uint8Array(b, 7).set(w); return b; };
async function start(P, cfg) {
  await P.els.b1.onclick();
  const page = P.pages()[0];
  page.readyState = 1; page.onopen();
  P.msg(page, {t: 'hello', epoch: 'o1', cfg: Object.assign({stt_route: 'phone', play_buffer_ms: 0, wake: {engine: 'transcript'}}, cfg || {})});
  await tick(40);
  return page;
}
const awake = (P, page) => {                       // transcript probe -> token -> Scribe ready -> wake: listening
  P.feed(-20, 16);
  P.msg(page, {t: 'token', epoch: 'o1', kind: 'realtime_scribe', token: 'sutkn_x', url: 'wss://scribe.test/rt', params: {}});
  const scribe = P.sockets[P.sockets.length - 1];
  scribe.onmessage({data: JSON.stringify({message_type: 'session_started'})});
  scribe.onmessage({data: JSON.stringify({message_type: 'committed_transcript', text: 'Hey Hermes, how far to Bright?'})});
  P.audio.fire('playing'); P.audio.fire('ended');   // Fix A: the wake cue holds the mic until it ends, plus a 300 ms tail
  P.feed(-70, 15);
  return scribe;
};
const bin = (turn, seq) => { const b = new ArrayBuffer(10), v = new DataView(b); v.setUint8(0, 1); v.setUint32(1, turn); v.setUint16(5, seq); return b; };

// the CSS rules, flattened: [{sel, body}] (keyframes bodies skipped)
function rules(text) {
  const out = [];
  const flat = text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/@keyframes[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}/g, '');
  for (const m of flat.matchAll(/([^{}]+)\{([^{}]*)\}/g)) out.push({sel: m[1].trim(), body: m[2]});
  return out;
}
const R = rules(css);
const decl = (sel, prop) => { const r = R.find(x => x.sel.split(',').map(s => s.trim()).includes(sel)); const m = r && r.body.match(new RegExp('(?:^|;)\\s*' + prop + ':([^;]+)')); return m ? m[1].trim() : null; };

const one = (sel, prop) => { const r = R.find(x => x.sel === sel); const m = r && r.body.match(new RegExp('(?:^|;)\\s*' + prop + ':([^;]+)')); return m ? m[1].trim() : null; };   // the rule with exactly this selector
(async () => {
  // ---- markup and CSS
  eq('orb element: the motion wrapper (P4) around halo, level ring and core (flow + gyro light inside)',
     [/<div id="orb"[^>]*>\s*<div id="orbm">\s*<div class="halo"><\/div>\s*<div id="orbl"[^>]*><\/div>\s*<div id="orbc"[^>]*><div class="flow"><\/div><div class="gyro"><\/div><\/div>\s*<\/div>\s*<\/div>/.test(html)], [true]);
  eq('P4 orb_motion.js loaded before orb.js and the page script', /<script src="orb_motion.js"><\/script>\s*<script src="orb.js"><\/script>/.test(html), true);
  {   // P4 css: the wrapper's transform is the motion frame's custom properties, smoothed by a transition; neutral without them
    const tr = decl('#orbm', 'transform') || '';
    eq('P4 css: #orbm transform reads --mx --my --ma --mk --sx --sy with neutral fallbacks',
       ['--mx,0', '--my,0', '--ma,0', '--mk,0', '--sx,1', '--sy,1'].map(v => tr.includes('var(' + v + ')')), [true, true, true, true, true, true]);
    eq('P4 css: translate, then the mount angle, shear, scale', /^translate\(.*\) rotate\(.*\) skewX\(.*\) scale\(.*\)$/.test(tr), true);
    eq('P4 css: CSS transitions smooth the 20 Hz writes', [decl('#orbm', 'transition'), /transform 120ms linear/.test(one('.orb .gyro', 'transition') || '')], ['transform 100ms linear', true]);
    eq('P4 css: the light turns by --mr (it stays under the real sky in a lean)', one('.orb .gyro', 'transform'), 'rotate(calc(var(--mr,0)*1deg))');
    eq('P4 css: the new layers are invisible unless a state shows them (sleep: black)', one('.orb .halo,.orb .flow,.orb .gyro', 'opacity'), '0');
    eq('P4 css: no rule shows or animates anything in sleep', R.filter(r => /\.s-sleep\b/.test(r.sel)).length, 0);
    eq('P4 css: d-plain is the flat orb (no halo, flow, gyro)', decl('.d-plain .halo', 'display'), 'none');
    eq('P4 css: reduced motion switches the transforms off', /@media \(prefers-reduced-motion:reduce\)\{#orbm,[^}]*\{transform:none\}\}/.test(css), true);
    eq('P4 css: nothing but transform and opacity is animated or transitioned', [
      (css.match(/@keyframes[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}/g) || []).filter(k => /orb-/.test(k)).every(k => [...k.matchAll(/\{([^{}]*)\}/g)].every(m => m[1].split(';').filter(Boolean).every(d => /^\s*(opacity|transform):/.test(d)))),
      R.filter(r => /orb|\.core|\.lvl|\.halo|\.flow|\.gyro/.test(r.sel) && !/sorb|resetbar/.test(r.sel)).map(r => (r.body.match(/(?:^|;)\s*transition:([^;]+)/) || [])[1]).filter(Boolean)
        .every(t => t.split(',').every(x => /^\s*(opacity|transform)\s/.test(x)))], [true, true]);
  }
  eq('old #bg colour page gone', [/id="bg"/.test(html), /#bg\b/.test(css)], [false, false]);
  eq('orb.js loaded before the page script', /<script src="orb.js"><\/script>/.test(html), true);
  eq('layout: diameter min(75vw,75vh)', [decl('#orb', 'width'), decl('#orb', 'height')], ['min(75vw,75vh)', 'min(75vw,75vh)']);
  eq('layout: centred', [decl('#orb', 'position'), decl('#orb', 'left'), decl('#orb', 'top'), decl('#orb', 'transform')],
     ['fixed', '50%', '50%', 'translate(-50%,-50%)']);
  eq('layout: the orb never takes taps (WP4 double tap reaches the page)', decl('#orb', 'pointer-events'), 'none');
  {   // the rule at phone sizes, portrait and landscape: d = min(.75w, .75h), centred, inside the screen, <= 3/4 width
    const sizes = [[320, 568], [360, 740], [375, 667], [390, 844], [414, 896], [430, 932], [568, 320], [844, 390], [932, 430]];
    const bad = sizes.filter(([w, h]) => {
      const d = Math.min(0.75 * w, 0.75 * h), left = w / 2 - d / 2, top = h / 2 - d / 2;
      return !(d <= 0.75 * w + 1e-9 && d <= h && left >= 0 && top >= 0 && Math.abs(left * 2 + d - w) < 1e-9 && Math.abs(top * 2 + d - h) < 1e-9);
    });
    eq('layout: 320-430 px portrait and landscape fit, centred, <= 3/4 width', bad, []);
    eq('layout: 390x844 portrait = 292.5 px, landscape 844x390 = 292.5 px', [Math.min(0.75 * 390, 0.75 * 844), Math.min(0.75 * 844, 0.75 * 390)], [292.5, 292.5]);
  }
  {   // sleep is static by construction: every animation in the stylesheet belongs to a non-sleep state class
    const animated = R.filter(r => /(^|;)\s*animation(-name)?:\s*(?!none)/.test(r.body));
    // the setup orb (.sorb, inside #setup, removed by tap 2) pulses while it waits for a tap; it never runs while riding
    const ok = animated.filter(r => r.sel.split(',').every(s => /\.s-(listening|thinking|speaking|offline|reconnecting|connecting)\b|^\s*\.sorb\b/.test(s)));
    eq('css: every animation is scoped to an animated state or the setup orb (none for sleep or muted)', animated.length > 0 && ok.length === animated.length, true);
    eq('css: no sleep or muted rule animates', R.filter(r => /\.s-(sleep|muted)\b/.test(r.sel) && /animation:\s*(?!none)/.test(r.body)).length, 0);
    eq('css: an infinite animation for listening, thinking, speaking and the amber states',
       ['listening', 'thinking', 'speaking', 'offline', 'reconnecting', 'connecting'].map(s => animated.some(r => r.sel.includes('.s-' + s) && /infinite/.test(r.body))),
       [true, true, true, true, true, true]);
    eq('css: thinking swells about 1 s first, then breathes', /orb-swell\s+1s[^,;]*,\s*orb-breath[^;]*\s1s\s+infinite/.test(decl('.d-simple.s-thinking .core', 'animation') || ''), true);
    eq('css: the tone retriggers the breath (two identical keyframes)', [/@keyframes orb-beat-a\{/.test(css), /@keyframes orb-beat-b\{/.test(css),
       /orb-beat-a/.test(decl('.d-simple.s-thinking.beat-a .core', 'animation') || ''), /orb-beat-b/.test(decl('.d-simple.s-thinking.beat-b .core', 'animation') || '')],
       [true, true, true, true]);
    eq('css: thinking peaks at white', /#fff\b|#ffffff\b/.test(decl('.d-simple.s-thinking .core', 'background') || ''), true);
    {   // hackathon: speaking = the listening look with the cyan turned to pink at the same HSL lightness (hue 186 -> 325)
      const CYAN = /#e8fdff|#a8f7ff|#7ff0ff|rgba\((127,240,255|168,247,255|232,253,255|120,255,230|47,124,140),/i;
      const PINK = /#ffe8f7|#ffa8e2|#ff80d4|rgba\((255,128,212|255,168,226|255,232,247|255,120,199|140,47,101),/i;
      const layers = ['.core', '.lvl', '.halo', '.flow::before', '.gyro'];
      const rulesFor = sel => R.filter(r => r.sel.split(',').map(s => s.trim()).includes(sel));
      eq('css: speaking has a pink rule for each of the 5 layers (core, ring, halo, cloud, glass)',
         layers.map(l => rulesFor('.d-simple.s-speaking ' + l).some(r => PINK.test(r.body))), [true, true, true, true, true]);
      eq('css: no rule that names speaking carries a cyan colour', layers.flatMap(l => rulesFor('.d-simple.s-speaking ' + l)).filter(r => CYAN.test(r.body)).map(r => r.sel), []);
      eq('css: speaking ring is #ffa8e2, listening ring stays #a8f7ff', [/#ffa8e2/i.test(decl('.d-simple.s-speaking .lvl', 'border') || ''), /#a8f7ff/i.test(decl('.d-simple.s-listening .lvl', 'border') || '')], [true, true]);
    }
    eq('css: muted (round 3) = a thin static grey ring (#555), no core rule', [/border:\s*2px solid #555/.test((R.find(r => r.sel.includes('.d-simple.s-muted .lvl')) || {body: ''}).body), R.some(r => /\.s-muted\b[^,]*\.core/.test(r.sel))], [true, false]);
  }
  {   // Fix F: rideWavEnvelope = dBFS per 50 ms from the WAV bytes the page already holds
    const {rideWavEnvelope} = require(path.join(ROOT, 'static', 'orb.js'));
    const e = rideWavEnvelope(wav());
    eq('F envelope: 2 s -> 40 values of 50 ms', e && e.length, 40);
    eq('F envelope: the sine at about -9 dBFS RMS, then silence', e && [Math.round(e[5]), e[30] < -100], [-9, true]);
    const st = rideWavEnvelope(wav(24000, 2));
    eq('F stereo 24 kHz too', st && [st.length, Math.round(st[0])], [40, -9]);
    const off = new Uint8Array(7 + wav().length); off.set(wav(), 7);
    eq('F a view into a larger buffer (the frame header before it)', (rideWavEnvelope(off.subarray(7)) || []).length, 40);
    const mp3 = new Uint8Array([0x49, 0x44, 0x33, 4, 0, 0, 0, 0, 0, 0]);
    const f32 = wav(); new DataView(f32.buffer).setUint16(20, 3, true);
    eq('F not a 16-bit PCM WAV: null', [rideWavEnvelope(mp3), rideWavEnvelope(f32), rideWavEnvelope(new Uint8Array(0)), rideWavEnvelope(null)], [null, null, null, null]);
  }
  {   // module: design table, fallback, state mapping in one place
    const P = rig();
    eq('orb_design fallback', [P.run("rideOrbDesign({})"), P.run("rideOrbDesign({orb_design: 'simple'})"), P.run("rideOrbDesign({orb_design: 'Plasma'})"),
       P.run("rideOrbDesign({orb_design: ' SIMPLE '})"), P.run("rideOrbDesign(null)")], ['simple', 'simple', 'simple', 'simple', 'simple']);
    eq('every display() state has an orb mapping', P.run("['offline','reconnecting','connecting','speaking','thinking','muted','sleep','listening'].every(s => !!RIDE_ORB_STATES[s])"), true);
    eq('the input level shows in listening, the reply level in speaking (Fix F)', P.run("Object.keys(RIDE_ORB_STATES).filter(s => RIDE_ORB_STATES[s].level)"), ['listening', 'speaking']);
  }
  // ---- states on the real page
  const P = rig();
  eq('before setup: the markup orb is static sleep (nothing animates behind the setup panel)', P.orb(), 'orb d-simple s-sleep');
  const page = await start(P);
  eq('after setup, connected: sleep', [P.disp(), P.orb()], ['sleep', 'orb d-simple s-sleep']);
  eq('show_word off by default: no word', P.els.w.textContent, '');
  eq('Media Session title mirrors the state', P.ctx.navigator.mediaSession.metadata && P.ctx.navigator.mediaSession.metadata.title, 'Hermes-Ride · sleep');
  {   // sleep: no timers, no animation frames, no orb style writes while frames (quiet and loud taps) arrive
    const before = P.snap(), lv = P.lvl();
    P.feed(-70, 250);                               // 5 s of quiet
    P.feed(-20, 8); P.feed(-70, 20);                // a short loud burst (a tap): stays asleep
    await tick(30);
    const after = P.snap();
    eq('sleep: no timer, interval or animation frame started', [after.timeout - before.timeout, after.interval - before.interval, after.raf - before.raf], [0, 0, 0]);
    eq('sleep: the level ring is not touched', [P.lvl(), P.run('orb.stats().levelWrites')], [lv, 0]);
    eq('sleep: still the static class', P.orb(), 'orb d-simple s-sleep');
  }
  const scribe = awake(P, page);
  eq('listening: blue alive orb', [P.disp(), P.orb()], ['listening', 'orb d-simple s-listening']);
  {   // level: from the gate's frame level, 20 Hz at most
    const w0 = P.run('orb.stats().levelWrites');
    P.frames(-20, 3);
    eq('listening: loud frames grow the ring', [P.run('orb.stats().levelWrites') - w0 >= 1, /^scale\(/.test(P.lvl())], [true, true]);
    const loud = parseFloat(P.lvl().slice(6));
    P.frames(-70, 5);
    const quiet = parseFloat(P.lvl().slice(6));
    eq('listening: louder = bigger ring', loud > quiet, true);
    const n0 = P.run('orb.stats().levelWrites');
    P.frames(-30, 50);                                                                   // 1 s of 50 Hz frames
    const perS = P.run('orb.stats().levelWrites') - n0;
    eq('listening: 50 Hz frames -> 20 ring updates per second (at most 20 Hz)', perS, 20);
    const n1 = P.run('orb.stats().levelWrites'), t1 = P.perf.t;
    P.run(`for (let i = 0; i < 500; i++) orb.level(-30, ${t1} + 1 + i * 2)`);            // 1 s of 500 Hz calls
    eq('listening: even 500 calls/s -> at most 20 updates', P.run('orb.stats().levelWrites') - n1 <= 20, true);
    P.perf.t = t1 + 1000;
    eq('listening: the level reads the gate (no second analysis)', /gate\.level\(\)/.test(html) && !/rideFrameDb\(/.test(html.match(/<script>([\s\S]*?)<\/script>/)[1]), true);
  }
  // thinking: swell first, then each doo-doo tick retriggers the breath (beat-a / beat-b alternate)
  P.msg(page, {t: 'state', s: 'thinking', turn: 1, epoch: 'o1'});
  eq('thinking: swell class', [P.disp(), P.orb()], ['thinking', 'orb d-simple s-thinking']);
  {
    // the real path: client_state's toneTick calls io.tone -> the page's tone hook -> orb.tone()
    const toneHook = P.run("typeof onTone === 'function'");
    eq('the page routes io.tone through onTone (orb + sound)', toneHook, true);
    P.run('onTone(0.5)');
    eq('first doo-doo: breath retriggered (beat-a)', P.orb(), 'orb d-simple s-thinking beat-a');
    P.run('onTone(0.5)');
    eq('next doo-doo: beat-b (animation restarts in step with the tone)', P.orb(), 'orb d-simple s-thinking beat-b');
    P.run('onTone(0.5)');
    eq('and again: beat-a', P.orb(), 'orb d-simple s-thinking beat-a');
  }
  {   // the cadence comes from client_state's tone timers: first after tone_first_s, so the swell runs alone first
    const Q = rig();
    const qp = await start(Q, {tone_first_s: 1.2});
    awake(Q, qp);
    Q.run('var _tt = []; var _t0 = onTone; onTone = g => { _tt.push(Date.now()); _t0(g); };');
    Q.msg(qp, {t: 'state', s: 'thinking', turn: 1, epoch: 'o1'});
    for (let i = 0; i < 5; i++) { Q.audio.fire('playing'); Q.audio.fire('ended'); }   // the thinking cue ends
    eq('thinking (tone not yet): swell only', Q.orb(), 'orb d-simple s-thinking');
    await tick(1300);
    eq('after tone_first_s the doo-doo retriggers the breath', [Q.run('_tt.length'), Q.orb()], [1, 'orb d-simple s-thinking beat-a']);
  }
  P.msg(page, {t: 'state', s: 'ready', turn: 1, epoch: 'o1'});
  page.onmessage({data: bin(1, 0)});
  eq('speaking: blue with the synthetic envelope, beat classes gone', [P.disp(), P.orb()], ['speaking', 'orb d-simple s-speaking']);
  {
    const w = P.run('orb.stats().levelWrites');
    P.feed(-20, 30);
    eq('speaking an MP3 segment: no level writes (the CSS pulse stays; no curve for MP3)', P.run('orb.stats().levelWrites') - w, 0);
    eq('speaking: no createMediaElementSource anywhere', /createMediaElementSource/.test(html + fs.readFileSync(path.join(ROOT, 'static', 'orb.js'), 'utf8')), false);
  }
  {   // Fix F: speaking a WAV segment: the ring follows her voice (the envelope at audio.currentTime), at most 20/s
    const Q = rig();
    const qp = await start(Q);
    awake(Q, qp);
    Q.msg(qp, {t: 'state', s: 'ready', turn: 7, epoch: 'o1'});
    qp.onmessage({data: wavFrame(7, 0)});
    Q.audio.fire('playing'); Q.audio.fire('ended');              // the done cue first, then the segment plays
    eq('F WAV segment playing: speaking, its envelope known', [Q.disp(), Q.run('C.nowPlaying() && C.nowPlaying().kind'),
       Q.run('C.nowPlaying() && C.nowPlaying().env.length')], ['speaking', 'seg', 40]);
    const w0 = Q.run('orb.stats().levelWrites');
    Q.audio.currentTime = 0.5; Q.frames(-70, 5);                // the mic is quiet, Lana is loud
    const loud = parseFloat(Q.lvl().slice(6));
    Q.audio.currentTime = 1.5; Q.frames(-20, 5);                // the mic is loud, Lana pauses
    const quiet = parseFloat(Q.lvl().slice(6));
    eq('F loud part -> ring near 1.0, quiet part -> 0.6 (her voice, not the mic)', [loud >= 0.95, Math.abs(quiet - 0.6) < 1e-6], [true, true]);
    const w1 = Q.run('orb.stats().levelWrites');
    Q.frames(-70, 100);                                          // 2 s of mic frames
    const per_s = (Q.run('orb.stats().levelWrites') - w1) / 2;
    eq('F level writes while speaking: at most 20/s', [w1 - w0 > 0, per_s > 0 && per_s <= 20], [true, true]);
    eq('F ring visible while speaking: pink rim (#ffa8e2, hackathon), 60 ms transform', [decl('.d-simple.s-speaking .lvl', 'opacity'),
       /#ffa8e2/.test(decl('.d-simple.s-speaking .lvl', 'border') || ''), decl('.d-simple.s-speaking .lvl', 'transition')],
       ['1', true, 'transform 60ms linear']);
    eq('F the core keeps orb-env', /orb-env/.test(decl('.d-simple.s-speaking .core', 'animation') || ''), true);
  }
  for (let i = 0; i < 6; i++) { P.audio.fire('playing'); P.audio.fire('ended'); }
  P.msg(page, {t: 'end', turn: 1, segs: 1, epoch: 'o1'});
  for (let i = 0; i < 6; i++) { P.audio.fire('playing'); P.audio.fire('ended'); }
  P.msg(page, {t: 'state', s: 'listening', epoch: 'o1'});
  eq('after the reply: listening (window)', P.orb(), 'orb d-simple s-listening');
  P.run('setMuted(true)');
  eq('muted (round 3): the static grey ring state', [P.disp(), P.orb(), P.run('orb.animations()')], ['muted', 'orb d-simple s-muted', 0]);
  {
    const before = P.snap(), w = P.run('orb.stats().levelWrites'), m = P.run('orb.stats().motionWrites');
    P.frames(-20, 50);
    P.run('orb.motion(' + (P.perf.t + 500) + ')');
    await tick(30);
    const after = P.snap();
    eq('muted: static (no timers, animation frames, level writes or motion writes while voices are loud)', [after.timeout - before.timeout, after.interval - before.interval, after.raf - before.raf,
       P.run('orb.stats().levelWrites') - w, P.run('orb.stats().motionWrites') - m, P.lvl()], [0, 0, 0, 0, 0, '']);
  }
  P.run('setMuted(false)');
  eq('unmuted: sleep', P.orb(), 'orb d-simple s-sleep');
  page.onclose({code: 1006});
  eq('link lost: amber pulse (reconnecting)', [P.disp(), P.orb()], ['reconnecting', 'orb d-simple s-reconnecting']);
  eq('Media Session title follows', P.ctx.navigator.mediaSession.metadata.title, 'Hermes-Ride · reconnecting');
  void scribe;
  {   // connecting (awake, recognition not up yet) is amber too
    const Q = rig();
    const qp = await start(Q);
    Q.feed(-20, 16);
    Q.run('gate.wake()');
    eq('connecting: amber pulse', [Q.disp(), Q.orb()], ['connecting', 'orb d-simple s-connecting']);
    void qp;
  }
  {   // orb_design from hello: unknown names fall back; show_word as a debug option
    const Q = rig();
    await start(Q, {orb_design: 'plasma', word: true});
    eq('orb_design plasma (unknown): simple design', Q.orb(), 'orb d-simple s-sleep');
    const F = rig();
    await start(F, {orb_design: 'Plain'});
    eq('P4 orb_design plain: the flat orb (d-simple rules, extra layers off)', F.orb(), 'orb d-simple d-plain s-sleep');
    eq('show_word on: the state word is shown', Q.els.w.textContent, 'sleep');
  }
  // ---- P4: the orb moves with the bike. DeviceMotion events (60 Hz) -> orb_motion.js -> custom properties on #orbm
  {
    const G = 9.81, c30 = Math.cos(Math.PI / 6), s30 = Math.sin(Math.PI / 6);
    // the phone on its mount, tilted back 30 deg, iOS signs: up = (0, cos, sin), forward = (0, sin, -cos) in the phone frame
    const ev = (lon, o) => { o = o || {}; const k = o.load || 1, f = [0, c30 * k + s30 * lon, s30 * k - c30 * lon];
      return {acceleration: {x: 0, y: -G * s30 * lon, z: G * c30 * lon}, accelerationIncludingGravity: {x: -G * f[0], y: -G * f[1], z: -G * f[2]},
              rotationRate: {beta: 0, gamma: (o.roll || 0) * s30 - (o.yaw || 0) * c30, alpha: -(o.roll || 0) * c30 - (o.yaw || 0) * s30}}; };
    const mot = (Q, secs, lon, o) => { for (let i = 0; i < secs * 60; i++) { Q.perf.t += 1000 / 60; Q.ctx.__ev = ev(lon, o); Q.run('onMotion(__ev)'); } };
    const mw = Q => Q.els.orbm.propWrites;
    const Q = rig();
    const qp = await start(Q);
    eq('P4 the page has the motion module at gain 1 and gives it to the orb', [Q.run("typeof orbMotion.feed"), Q.run('orbMotion.gain()'), Q.run('orb.stats().motionWrites')], ['function', 1, 0]);
    {   // asleep: the module listens (it is warm at the wake), the orb is not touched
      const before = Q.snap();
      mot(Q, 3, 0); mot(Q, 2, -0.5);
      const after = Q.snap();
      eq('P4 sleep: braking writes nothing on #orbm, starts no timer or animation frame', [mw(Q), Q.run('orb.stats().motionWrites'), after.timeout - before.timeout, after.interval - before.interval, after.raf - before.raf], [0, 0, 0, 0, 0]);
      eq('P4 sleep: the module still saw the samples', Q.run('orbMotion.state().n') >= 300, true);
      mot(Q, 3, 0);
    }
    awake(Q, qp);
    eq('P4 listening', Q.disp(), 'listening');
    {
      const w0 = mw(Q), n0 = Q.run('orb.stats().motionWrites');
      // round 3: the 2 s 0.5 g brake fed while asleep moved the 30 s baseline (lon reads +0.055 g after it); at full
      // scale 0.35 g that is one catch-up frame at the wake (--my 2.5 %) and then a slow drift home: measured one frame
      // every 1.2 s (0.15 % per frame). At full scale 0.7 g the same residue stayed inside the deadband (0 frames).
      mot(Q, 0.5, 0);
      eq('P4 awake after a brake while asleep: at most one catch-up frame', Q.run('orb.stats().motionWrites') - n0 <= 1, true);
      const w1 = mw(Q), n1 = Q.run('orb.stats().motionWrites');
      mot(Q, 2, 0);
      eq('P4 listening at rest after that brake: the slow drift home is at most 1 frame a second (deadband; measured 2 in 2 s)', [(mw(Q) - w1) % 7, Q.run('orb.stats().motionWrites') - n1 <= 2], [0, true]);
      mot(Q, 1, -0.5);
      const frames = Q.run('orb.stats().motionWrites') - n1;
      eq('P4 braking 1 s at 60 Hz events: frames written, at most 20', [frames > 3, frames <= 20], [true, true]);
      eq('P4 one frame = the seven custom properties', [(mw(Q) - w1) / frames, Object.keys(Q.els.orbm.props).sort()], [7, ['--ma', '--mk', '--mr', '--mx', '--my', '--sx', '--sy']]);
      const p = Q.els.orbm.props;
      eq('P4 braking: squashed (--sy < 1 < --sx), pushed up the screen (--my < 0)', [parseFloat(p['--sy']) < 0.97, parseFloat(p['--sx']) > 1.02, parseFloat(p['--my']) < -1], [true, true, true]);
      eq('P4 the orb\'s own transform is never written (CSS owns it; the level ring keeps its own)', [Q.els.orbm.style.transform, Q.els.orb.style.transform], [undefined, undefined]);
      // mic frames and motion events together share one 50 ms grid: at most 20 style ticks a second in total
      const t0 = Q.run('orb.stats().ticks'), l0 = Q.run('orb.stats().levelWrites'), m0 = Q.run('orb.stats().motionWrites');
      for (let i = 0; i < 150; i++) {                       // 3 s: accelerate, brake, accelerate; 50 Hz mic frames, 60 Hz motion
        Q.perf.t += 20; Q.feed(-25 - (i % 7), 1);
        Q.ctx.__ev = ev(i < 50 ? 0.25 : i < 100 ? -0.3 : 0.25); Q.run('onMotion(__ev)');   // round 3: inside the 0.35 g full scale (0.4 / -0.5 saturate: fewer frames)
        if (i % 5 === 0) Q.run('onMotion(__ev)');
      }
      const ticks = Q.run('orb.stats().ticks') - t0;
      eq('P4 mic level + motion together: at most 20 style ticks per second, both written',
         [ticks <= 60, Q.run('orb.stats().levelWrites') - l0 > 30, Q.run('orb.stats().motionWrites') - m0 > 20], [true, true, true]);
    }
    {   // a right turn: roll in 30 deg/s for 1 s, then hold with the turn's yaw rate and load; GPS speed 20 m/s
      mot(Q, 3, 0);
      const gps = Q.gps;
      eq('P4 the page watches GPS', typeof gps.cb, 'function');
      const fixAt = () => gps.cb({timestamp: Date.now(), coords: {latitude: -36.7, longitude: 146.9, accuracy: 5, speed: 20, heading: 90}});
      const yaw = Math.tan(Math.PI / 6) * G / 20 * 180 / Math.PI;                  // world yaw rate, deg/s
      fixAt(); mot(Q, 1, 0, {roll: 30});
      for (let i = 0; i < 6; i++) { fixAt(); mot(Q, 1, 0, {yaw: yaw * c30, load: 1 / c30}); }
      const p = Q.els.orbm.props, lean = Q.run('orbMotion.state().lean');
      eq('P4 right turn at 30 deg: lean from the gyro and the GPS speed (not from the accelerometer)', [Math.abs(lean - 30) < 7, Math.abs(parseFloat(p['--mr']) + 30) < 8, parseFloat(p['--mk']) > 2], [true, true, true]);
      eq('P4 right turn: no sideways throw, pressed by the load', [Math.abs(parseFloat(p['--mx'])) < 1, parseFloat(p['--sy']) < 1], [true, true]);
      for (let i = 0; i < 3; i++) { fixAt(); mot(Q, 1, 0, i ? {} : {roll: -30}); }
      eq('P4 upright again after the corner', Math.abs(parseFloat(Q.els.orbm.props['--mr'])) < 5, true);
    }
    {   // thinking, and speaking an MP3 segment (no level writes there): the motion still shows, 20 Hz at most
      Q.msg(qp, {t: 'state', s: 'thinking', turn: 3, epoch: 'o1'});
      let n = Q.run('orb.stats().motionWrites'); mot(Q, 1, -0.5);
      const th = Q.run('orb.stats().motionWrites') - n;
      Q.msg(qp, {t: 'state', s: 'ready', turn: 3, epoch: 'o1'});
      qp.onmessage({data: bin(3, 0)});
      n = Q.run('orb.stats().motionWrites'); mot(Q, 1, 0.4);
      const sp = Q.run('orb.stats().motionWrites') - n;
      eq('P4 thinking and speaking: motion frames, at most 20 a second', [Q.disp(), th > 3 && th <= 20, sp > 3 && sp <= 20, Q.run('orb.stats().levelWrites') >= 0], ['speaking', true, true, true]);
    }
    {   // the setting: orb_motion 0 = off (nothing fed, nothing written); 2 = more; reduced motion = off
      const Z = rig();
      const zp = await start(Z, {orb_motion: 0});
      awake(Z, zp); mot(Z, 3, 0); mot(Z, 1, -0.5);
      eq('P4 orb_motion 0: gain 0, no sample taken, no write', [Z.run('orbMotion.gain()'), Z.run('orbMotion.state().n'), mw(Z), Z.run('orb.stats().motionWrites')], [0, 0, 0, 0]);
      const size = async (cfg, over) => { const A = rig(over); const ap = await start(A, cfg); awake(A, ap); mot(A, 3, 0); mot(A, 1, -0.3); return [A.run('orbMotion.gain()'), parseFloat(A.els.orbm.props['--sy'] || '1')]; };
      const one = await size({}), two = await size({orb_motion: 2}), junk = await size({orb_motion: 'x'});
      eq('P4 orb_motion: default 1, 2 squashes more, junk = 1', [one[0], two[0], junk[0], two[1] < one[1] && one[1] < 1], [1, 2, 1, true]);
      const red = await size({orb_motion: 2}, {matchMedia: q => ({matches: /prefers-reduced-motion/.test(q)})});
      eq('P4 prefers-reduced-motion: off whatever the setting', red, [0, 1]);
    }
    {   // round 3: true rest on a fresh page writes nothing; a 0.3 g brake moves the orb at least 12 % of its diameter
      const R = rig();
      const rp = await start(R);
      mot(R, 3, 0); awake(R, rp);
      const n0 = R.run('orb.stats().motionWrites');
      mot(R, 3, 0);
      eq('P4 round 3: listening at true rest for 3 s: no motion write (deadband)', [R.run('orb.stats().motionWrites') - n0, mw(R)], [0, 0]);
      mot(R, 1.5, -0.3);
      const p = R.els.orbm.props, my = parseFloat(p['--my']), ratio = parseFloat(p['--sx']) / parseFloat(p['--sy']);
      eq('P4 round 3: 0.3 g brake on the page: --my at most -12 (% of the diameter), --sx/--sy at least 1.35 (measured about -12.6 and 1.46)', [my <= -12, ratio >= 1.35], [true, true]);
    }
    {   // the motion data still reaches the ride log exactly as before
      eq('P4 telemetry still gets every sample (the ride log is unchanged)', /tel\.onMotion\(m\);/.test(html) || /tel\.onMotion\(\{g, la/.test(html), true);
    }
  }
  // ---- battery: per state, what runs per second (rig-measured over 2 s of 50 Hz frames)
  const battery = {};
  {
    const B = rig();
    const bp = await start(B);
    const bev = lon => ({acceleration: {x: 0, y: 0, z: 9.81 * lon}, accelerationIncludingGravity: {x: 0, y: -9.81, z: 9.81 * lon}, rotationRate: {alpha: 0, beta: 0, gamma: 0}});
    const measure = async (name, frameDb) => {
      const s0 = B.snap(), w0 = B.run('orb.stats().levelWrites'), m0 = B.run('orb.stats().motionWrites'), k0 = B.run('orb.stats().ticks');
      for (let i = 0; i < 100; i++) {                // 2 s of frames at the page's 20 ms cadence (simulated clock)
        B.frames(frameDb, 1);                        // P4: with the bike moving under it (a 2 Hz surge, 0.4 g): a motion event per frame
        B.ctx.__ev = bev(0.4 * Math.sin(i / 8)); B.run('onMotion(__ev)');
      }
      await tick(10);
      const s1 = B.snap();
      battery[name] = {js_timers_per_s: (s1.timeout - s0.timeout + s1.interval - s0.interval) / 2, animation_frames_per_s: (s1.raf - s0.raf) / 2,
                       level_writes_per_s: (B.run('orb.stats().levelWrites') - w0) / 2, css_animations: B.run('orb.animations()'),
                       motion_frames_per_s: (B.run('orb.stats().motionWrites') - m0) / 2, style_ticks_per_s: (B.run('orb.stats().ticks') - k0) / 2};
    };
    await measure('sleep', -70);
    awake(B, bp);
    await measure('listening', -25);
    B.msg(bp, {t: 'state', s: 'thinking', turn: 1, epoch: 'o1'});
    await measure('thinking', -70);
    B.msg(bp, {t: 'state', s: 'ready', turn: 1, epoch: 'o1'});
    bp.onmessage({data: bin(1, 0)});
    await measure('speaking', -70);
    eq('battery: no JS timers or animation frames from the orb in any state',
       Object.values(battery).every(b => b.js_timers_per_s === 0 && b.animation_frames_per_s === 0), true);
    eq('battery: level writes only in listening, <= 20/s', [battery.sleep.level_writes_per_s, battery.listening.level_writes_per_s <= 20 && battery.listening.level_writes_per_s > 0,
       battery.thinking.level_writes_per_s, battery.speaking.level_writes_per_s], [0, true, 0, 0]);
    eq('battery P4: motion frames: none asleep, at most 20/s awake; level + motion style ticks at most 20/s in every state',
       [battery.sleep.motion_frames_per_s, battery.sleep.style_ticks_per_s, ['listening', 'thinking', 'speaking'].every(s => battery[s].motion_frames_per_s > 0 && battery[s].motion_frames_per_s <= 20),
        Object.values(battery).every(b => b.style_ticks_per_s <= 20)], [0, 0, true, true]);
    eq('battery: CSS animations running: sleep 0, listening 1, thinking 1, speaking 1',
       [battery.sleep.css_animations, battery.listening.css_animations, battery.thinking.css_animations, battery.speaking.css_animations], [0, 1, 1, 1]);
  }
  console.log(JSON.stringify({checks: T.checks, failures: T.failures, battery}));
  process.exit(0);
})().catch(T.crash);
