// W3: the mic gate on synthetic PCM (static/gate.js). Prints {checks, failures}.
'use strict';
const path = require('path');
const {RideGate, rideFrameDb} = require(path.join(__dirname, '..', 'static', 'gate.js'));
const TAPS = require(path.join(__dirname, 'tap_audio.js'));

let checks = 0;
const failures = [];
function eq(name, got, want) { checks++; if (JSON.stringify(got) !== JSON.stringify(want)) failures.push({name, got, want}); }

// 20 ms frames (320 samples). tone(db) = sine at that RMS level; tag = a marker written into sample 0
function tone(db, tag) {
  const a = Math.sqrt(2) * 32768 * Math.pow(10, db / 20), f = new Int16Array(320);
  for (let i = 0; i < 320; i++) f[i] = Math.round(a * Math.sin(2 * Math.PI * 300 * i / 16000));
  if (tag !== undefined) f[0] = tag;
  return f;
}
function noise(db, seed) {
  let s = seed || 1; const f = new Int16Array(320), a = Math.sqrt(3) * 32768 * Math.pow(10, db / 20);
  for (let i = 0; i < 320; i++) { s = (s * 1103515245 + 12345) & 0x7fffffff; f[i] = Math.round(a * (2 * s / 0x7fffffff - 1)); }
  return f;
}
const silence = () => new Int16Array(320);
function rig(opts) {
  const ev = [];
  const g = RideGate(opts, e => ev.push(e));
  const run = (n, make) => { for (let i = 0; i < n; i++) g.feed(make(i)); };
  return {g, ev, run, kinds: () => ev.map(e => e.e), audio: () => ev.filter(e => e.e === 'audio')};
}

eq('level of a -20 dB tone', Math.round(rideFrameDb(tone(-20))), -20);

// 1. silence (and quiet road noise) never opens the gate, with either engine
for (const engine of ['transcript', 'sherpa']) {
  const r = rig({engine});
  r.run(1500, () => silence());               // 30 s
  r.run(1500, i => noise(-55, i + 1));        // 30 s of noise below -45 dB
  eq(`1 ${engine}: nothing leaves the phone`, [r.ev.length, r.g.state(), r.g.isOpen()], [0, 'sleep', false]);
}

// 2. the speed-scaled threshold: base_db standing (here the old -45 dB; Fix E made -38 the default), +0.15 dB per km/h,
//    capped at -25 dB
{
  const r = rig({engine: 'transcript', base_db: -45});
  eq('2 threshold 0 / 60 / 100 / 250 km/h', [0, 60, 100, 250].map(v => { r.g.setSpeed(v); return +r.g.threshold().toFixed(2); }),
     [-45, -36, -30, -25]);
  r.g.setSpeed(0);
  r.run(100, i => noise(-40, i + 7));         // -40 dB wind noise opens a probe while standing ...
  eq('2 -40 dB noise opens at 0 km/h', r.g.state(), 'probe');
  const r2 = rig({engine: 'transcript', base_db: -45});
  r2.g.setSpeed(60);
  r2.run(500, i => noise(-40, i + 7));        // ... but not at 60 km/h (threshold -36 dB)
  eq('2 the same noise stays shut at 60 km/h', [r2.ev.length, r2.g.state()], [0, 'sleep']);
  r2.run(16, () => tone(-20));                // a voice over the noise still gets through (WP3: 300 ms of it, was 40 ms)
  eq('2 speech over it opens', r2.g.state(), 'probe');
}

// 3. pre-roll: the 800 ms before the wake are sent first, in order, then live audio
{
  const r = rig({engine: 'sherpa'});
  r.run(100, i => tone(-30, i));               // 2 s of speech-like frames, tagged 0..99 (wake word in there)
  eq('3 sleeping: nothing sent', r.ev.length, 0);
  r.g.wake();
  r.g.feed(tone(-30, 1000));
  const a = r.audio().map(e => e.pcm[0]);
  eq('3 open first', r.kinds().slice(0, 2), ['state', 'open']);
  eq('3 wake pre-roll = last 75 frames (1.5 s) then live', [a.length, a[0], a[74], a[75]], [76, 25, 99, 1000]);
}

// 4. conversation window: closes 30 s after the reply with no speech; speech restarts it; never while busy
{
  const r = rig({engine: 'sherpa', window_s: 30});
  r.g.wake();
  r.run(50, () => tone(-20));                  // 1 s question
  r.g.setBusy(true);
  r.run(2500, () => silence());                // 50 s thinking: no timeout while busy
  eq('4 open while busy', r.g.isOpen(), true);
  r.g.setBusy(false); r.g.setSpeaking(true);
  r.run(250, () => silence());                 // 5 s reply
  r.g.setSpeaking(false);
  eq('4 window after the reply', r.g.state(), 'window');
  r.run(1499, () => silence());                // 29.98 s
  eq('4 still open at 29.98 s', r.g.isOpen(), true);
  r.run(1, () => silence());
  eq('4 closed at 30 s', [r.g.isOpen(), r.g.state(), r.kinds().slice(-2)], [false, 'sleep', ['close', 'state']]);
  const r2 = rig({engine: 'sherpa', window_s: 30});
  r2.g.wake(); r2.g.setSpeaking(true); r2.g.setSpeaking(false);
  r2.run(1000, () => silence());               // 20 s
  r2.run(50, () => tone(-20));                 // a follow-up, no wake word needed
  eq('4 follow-up re-awakens', r2.g.state(), 'awake');
  r2.run(1000, () => silence());               // 20 s after it (minus hangover): still open
  eq('4 window counts from the last speech', r2.g.isOpen(), true);
  r2.run(600, () => silence());
  eq('4 then closes', r2.g.isOpen(), false);
}

// 5. VAD edges: start after 2 loud frames, stop after the 600 ms hangover; edges carry the frame clock
{
  const r = rig({engine: 'sherpa'});
  r.g.wake();
  r.run(10, () => silence());
  r.run(25, () => tone(-20));
  r.run(40, () => silence());
  const v = r.ev.filter(e => e.e === 'vad');
  eq('5 edges', v.map(e => [e.s, e.at]), [[1, 240], [0, 1300]]);
}

// 6. transcript engine probe: speech in sleep streams (pre-roll first) plus a 1.5 s tail; a miss goes back to sleep
{
  const r = rig({engine: 'transcript'});
  r.run(50, i => silence());
  r.run(50, i => tone(-20, 500 + i));          // 1 s "Hey Lana ..." or bystander speech
  r.run(200, () => silence());                 // 4 s
  const sent = r.audio().length;
  eq('6 probe streamed speech + hangover + tail', [r.g.state(), sent > 50 + 30 + 70, sent < 50 + 30 + 80 + 41], ['probe', true, true]);
  eq('6 pre-roll came first', r.audio()[0].pcm[0] !== 500, true);
  r.g.probeResult(false);
  eq('6 miss: back to sleep, closed', [r.g.state(), r.g.isOpen()], ['sleep', false]);
  r.run(50, i => tone(-20));
  r.g.probeResult(true);
  eq('6 hit: awake', [r.g.state(), r.g.isOpen()], ['awake', true]);
  const r3 = rig({engine: 'transcript'});
  r3.run(30, () => tone(-20)); r3.run(400, () => silence());
  eq('6 no STT answer: probe gives up', r3.g.state(), 'sleep');
}

// 6b. D1: a probe while a reply plays stays a probe (the page calls setSpeaking(true) on every render): no promotion to
//     'speaking' / 'window' without the wake word, and no frames after the probe tail
{
  const r = rig({engine: 'transcript'});
  r.run(50, () => silence());
  r.run(50, () => tone(-20));                  // a voiced run with no wake word, while the reply plays
  eq('6b probe opened', r.g.state(), 'probe');
  r.g.setSpeaking(true);
  eq('6b probe while speaking stays probe', r.g.state(), 'probe');
  r.run(30 + 75, () => silence());             // hangover + the 1.5 s tail
  const sent = r.audio().length;
  r.run(100, () => silence());                 // 2 s more of the reply
  eq('6b no frames after the probe tail without a wake', [r.audio().length - sent, r.g.state()], [0, 'probe']);
  r.g.setSpeaking(false);
  eq('6b reply ends: no window', r.g.state(), 'probe');
  r.run(200, () => silence());
  eq('6b no wake: the probe gives up, closed', [r.g.state(), r.g.isOpen(), r.audio().length - sent], ['sleep', false, 0]);
  r.g.setSpeaking(true);
  eq('6b asleep while speaking stays asleep', r.g.state(), 'sleep');
  const h = rig({engine: 'transcript'});       // the wake word still resolves a probe, and then the reply promotes it
  h.run(16, () => tone(-20)); h.g.setSpeaking(true); h.g.probeResult(true);
  eq('6b probe + wake hit: awake', h.g.state(), 'awake');
  h.g.setSpeaking(true);
  eq('6b awake + speaking: speaking', h.g.state(), 'speaking');
  h.g.setSpeaking(false);
  eq('6b then the window', h.g.state(), 'window');
  h.g.setSpeaking(true);
  eq('6b window + speaking: speaking', h.g.state(), 'speaking');
}

// 7. sent audio accounting (stt_audio_s for the usage log)
{
  const r = rig({engine: 'sherpa'});
  r.run(40, () => silence());
  r.g.wake(); r.run(50, () => silence());
  eq('7 sent ms = pre-roll + live', r.g.takeSentMs(), 800 + 1000);   // only 800 ms of audio existed before the wake
  eq('7 reset after take', r.g.takeSentMs(), 0);
}

// 8. fix round 3. R3-5: the window is 10 s by default and active speech is never cut: VAD speech, or a Scribe partial
//    (g.heard()) within the last 1.5 s, extends it. R3-9: VAD held on by steady room noise with nothing recognised
//    is not speech forever: the window still closes (indoors this showed as "stuck listening").
const replyDone = r => { r.g.wake(); r.g.setSpeaking(true); r.g.setSpeaking(false); };
const heard = g => { if (g.heard) g.heard(); };      // absent before fix round 3: the check still runs and fails
{
  const r = rig({engine: 'sherpa'});
  eq('8 default window_s is 10', r.g.opts().window_s, 10);
  replyDone(r);
  r.run(499, () => silence());                 // 9.98 s
  eq('8 open at 9.98 s', r.g.isOpen(), true);
  r.run(1, () => silence());
  eq('8 closed at 10 s', [r.g.isOpen(), r.g.state()], [false, 'sleep']);
}
{   // speech that starts at 9 s and goes on for 15 s with partials: not cut; closes 10 s after it ends
  const r = rig({engine: 'sherpa'});
  replyDone(r);
  r.run(450, () => silence());                 // 9 s
  for (let s = 0; s < 15; s++) { r.run(50, () => tone(-20)); heard(r.g); }
  eq('8 VAD speech 9-24 s: still open', [r.g.isOpen(), r.g.state()], [true, 'awake']);
  r.run(528, () => silence());                 // the 0.6 s hangover counts as speech, then 9.98 s
  eq('8 open 9.98 s after the speech', r.g.isOpen(), true);
  r.run(1, () => silence());
  eq('8 closes 10 s after the speech', r.g.isOpen(), false);
}
{   // VAD speech alone (no partial reaches the page yet): a 6 s utterance from 8 s still extends the window
  const r = rig({engine: 'sherpa', window_s: 10});
  replyDone(r);
  r.run(400, () => silence());
  r.run(300, () => tone(-20));
  eq('8 VAD-only speech 8-14 s: open', r.g.isOpen(), true);
  r.run(450, () => silence());                 // 23 s
  eq('8 still open 9 s after it', r.g.isOpen(), true);
}
{   // quiet speech under the VAD threshold, seen only as Scribe partials every second from 9.5 s to 20 s
  const r = rig({engine: 'sherpa', window_s: 10});
  replyDone(r);
  r.run(475, () => silence());
  for (let s = 0; s < 11; s++) { heard(r.g); r.run(50, () => tone(-60)); }
  eq('8 partial-only speech: open at 20 s', r.g.isOpen(), true);
  r.run(445, () => silence());                 // 1 s already passed since the last partial: 9.9 s now
  eq('8 open 9.9 s after the last partial', r.g.isOpen(), true);
  r.run(5, () => silence());
  eq('8 closes 10 s after the last partial', r.g.isOpen(), false);
}
{   // R3-9: indoor fan/TV noise above the standing threshold (-45 dB) holds VAD on; nothing is recognised
  const r = rig({engine: 'sherpa'});
  replyDone(r);
  r.run(3000, i => noise(-38, i + 1));         // 60 s
  eq('R3-9 steady noise, no partials: window closed within 60 s', [r.g.isOpen(), r.g.state()], [false, 'sleep']);
  const r2 = rig({engine: 'sherpa'});
  replyDone(r2);
  for (let s = 0; s < 30; s++) { r2.run(50, i => noise(-38, i + 1)); if (s % 2 === 0) heard(r2.g); }
  eq('R3-9 the same noise level with real recognised speech in it stays open', r2.g.isOpen(), true);
}

// AUD-R3-03 (fix round 4): missing partials alone are not proof that speech ended. Speech energy varies (syllables,
// short dips); steady noise is flat. A VAD run that varies like speech holds the window with or without partials;
// only a flat run (R3-9) is released after vad_alone_ms.
// speechy(i): 4 Hz syllable envelope, -24 dB +/- 9 dB, with a 200 ms pause every 2 s (inside the 600 ms hangover)
const speechy = i => (i % 100 >= 90 ? silence() : tone(-24 + 9 * Math.sin(2 * Math.PI * 4 * i * 0.02) + ((i * 7919) % 5 - 2)));
{
  const r = rig({engine: 'sherpa'});
  replyDone(r);
  r.run(100, () => silence());                 // 2 s
  r.run(1990, speechy);                        // 39.8 s of continuous speech (ends on a syllable), no partial
  eq('R3-03 40 s of varying VAD speech without partials: still open', [r.g.isOpen(), r.g.state()], [true, 'awake']);
  r.run(528, () => silence());                 // hangover + 9.96 s
  eq('R3-03 open 9.96 s after the speech ends', r.g.isOpen(), true);
  r.run(5, () => silence());
  eq('R3-03 closes 10 s after the speech ends', r.g.isOpen(), false);
}
{   // speech that goes on while the partials stop (STT delayed/failing): never cut
  const r = rig({engine: 'sherpa'});
  replyDone(r);
  for (let s = 0; s < 5; s++) { r.run(50, i => speechy(s * 50 + i)); heard(r.g); }
  r.run(1500, i => speechy(250 + i));          // 30 more seconds, no partial
  eq('R3-03 partials stop, speech goes on 30 s: still open', r.g.isOpen(), true);
}
{   // steady noise with a small wobble (+/- 1 dB, a fan) is still flat: released as in R3-9
  const r = rig({engine: 'sherpa'});
  replyDone(r);
  r.run(3000, i => noise(-38 + Math.sin(i / 7), i + 1));
  eq('R3-03 wobbling steady noise: window closed within 60 s', [r.g.isOpen(), r.g.state()], [false, 'sleep']);
}
{   // steady noise, then speech over it without partials: the speech holds the window again (noise over the old
    // -45 dB threshold: the test needs the noise to be loud)
  const r = rig({engine: 'sherpa', base_db: -45});
  replyDone(r);
  r.run(600, i => noise(-38, i + 1));          // 12 s flat noise: no longer counts
  r.run(1000, i => (i % 100 >= 90 ? noise(-38, i + 3) : tone(-22 + 9 * Math.sin(2 * Math.PI * 4 * i * 0.02))));   // 20 s
  eq('R3-03 speech over steady noise, no partials: open', r.g.isOpen(), true);
}

// ---- WP3 (P2, TODO 15/16): keyboard taps. Synthetic key clicks + thunks (tests/tap_audio.js, seeded) are short:
// a loud run of at most 12 frames (240 ms) at the standing threshold, while speech makes runs of 14 to 49 frames.
// Measured on the old gate (transcript engine, 10 x 30 s per pattern): typing held 'probe' open for ~98 % of the
// time and sent nearly all of the audio to STT, and 60 s of typing in an open window never let it close. The fix is a
// minimum voiced duration (voiced_ms, 300 ms = 15 consecutive loud frames) before sleep opens a probe, and the same
// test before a VAD run counts as someone talking in an open window. The 800 ms pre-roll covers the wait.
const PATTERNS = [['single', [-34, -14]], ['typing', [-34, -14]], ['rapid', [-34, -14]], ['single', [-45, -30]],
                  ['typing', [-45, -30]], ['rapid', [-45, -30]]];
{
  let opens = 0, probes = 0, sent = 0, runs = 0;
  for (const [pattern, lv] of PATTERNS) for (let seed = 1; seed <= 6; seed++) {
    const r = rig({engine: 'transcript'});
    for (const f of TAPS.frames(TAPS.tapAudio({seconds: 30, seed, pattern, level_db: lv}))) { r.g.feed(f); if (r.g.state() === 'probe') probes++; }
    opens += r.ev.filter(e => e.e === 'open').length; sent += r.audio().length; runs++;
  }
  eq(`WP3 taps (${runs} runs x 30 s, transcript engine): no probe, no open, no audio to STT`, [opens, probes, sent], [0, 0, 0]);
}
{   // the sherpa engine never opens on VAD in sleep (unchanged): taps send nothing either
  const r = rig({engine: 'sherpa'});
  for (const f of TAPS.frames(TAPS.tapAudio({seconds: 60, seed: 3, pattern: 'rapid'}))) r.g.feed(f);
  eq('WP3 sherpa engine: taps send nothing', [r.ev.length, r.g.state()], [0, 'sleep']);
}
{   // an open window is not held by typing: it closes window_s after the reply, like silence does
  const res = [];
  for (const [pattern, lv] of PATTERNS) for (let seed = 1; seed <= 3; seed++) {
    const r = rig({engine: 'sherpa'});
    replyDone(r);
    let slept = null, i = 0;
    for (const f of TAPS.frames(TAPS.tapAudio({seconds: 60, seed, pattern, level_db: lv}))) { r.g.feed(f); i++; if (slept === null && r.g.state() === 'sleep') slept = i * 0.02; }
    res.push(slept !== null && slept <= 11);
  }
  eq('WP3 window: 60 s of typing (18 runs) still closes at about 10 s', res.every(Boolean), true);
}
{   // real speech still wakes the probe, and its onset is inside the audio sent (the pre-roll covers the wait)
  const fx = path.join(__dirname, 'fixtures');
  const out = {};
  for (const w of ['speech_clean', 'speech_road', 'stale_pause', 'search_clean', 'stretch_clean']) {
    const r = rig({engine: 'transcript'});
    const fr = TAPS.frames(TAPS.readWav(path.join(fx, w + '.wav')));
    let onset = -1, opened = -1;
    fr.forEach((f, i) => { r.g.feed(f); if (onset < 0 && rideFrameDb(f) > r.g.threshold()) onset = i; if (opened < 0 && r.g.state() === 'probe') opened = i; });
    const sentFrames = new Set(r.audio().map(e => e.pcm));
    // the first loud frame that starts a run of 2+ (the gate's own onset), not a stray one-frame click
    let first = fr.findIndex((f, i) => rideFrameDb(f) > r.g.threshold() && i + 1 < fr.length && rideFrameDb(fr[i + 1]) > r.g.threshold());
    out[w] = {probe: opened >= 0, delay_ms: opened >= 0 ? (opened - first) * 20 : null, onset_sent: first >= 0 && sentFrames.has(fr[first])};
  }
  eq('WP3 speech fixtures: all open the probe within 1 s of onset, onset audio is sent',
     Object.values(out).every(v => v.probe && v.delay_ms <= 1000 && v.onset_sent), true);
  console.error('WP3 speech probe delays (ms after onset):', JSON.stringify(out));
}
{   // voiced_ms is configurable and bounds the wait: 0 restores the old behaviour (a 10-frame burst opens a probe)
  const r = rig({engine: 'transcript', voiced_ms: 0});
  r.run(10, () => tone(-20));
  eq('WP3 voiced_ms 0 = old behaviour', r.g.state(), 'probe');
  const r2 = rig({engine: 'transcript'});
  r2.run(10, () => tone(-20));
  eq('WP3 default: a 200 ms burst stays asleep', [r2.g.state(), r2.ev.length], ['sleep', 0]);
  r2.run(5, () => tone(-20));
  eq('WP3 default: 300 ms of voiced audio opens the probe', r2.g.state(), 'probe');
}

{   // WP5: the orb reads the level of the frame the gate just used (no second analysis path); level() is passive
  const r = rig({engine: 'transcript'});
  eq('WP5 level before any frame', r.g.level(), -Infinity);
  const f = tone(-30);
  r.g.feed(f);
  eq('WP5 level = rideFrameDb of the last frame', Math.abs(r.g.level() - rideFrameDb(f)) < 1e-9, true);
  r.g.feed(f, -12.5);
  eq('WP5 level = the db the worklet passed', r.g.level(), -12.5);
}

{   // Fix E (item 9): "mic is too damn sensitive": base_db -38 by default (was -45), and hello cfg.vad.base_db sets it
  const r = rig({engine: 'transcript'});
  eq('E default threshold at 0 km/h: -38 dB', r.g.threshold(), -38);
  r.run(100, () => tone(-42));
  eq('E a -42 dB run opens nothing at -38', [r.g.state(), r.ev.length], ['sleep', 0]);
  r.run(16, () => tone(-30));
  eq('E a -30 dB run does', r.g.state(), 'probe');
  const c = rig({engine: 'transcript'});
  c.g.configure({base_db: -50});
  eq('E threshold from cfg', c.g.threshold(), -50);
  c.run(16, () => tone(-42));
  eq('E at -50 the -42 dB run opens', c.g.state(), 'probe');
}

{   // Fix A (item 7): hold(on) while a cue or clip plays. Held frames (and cue_tail_ms after release) stream as zeros of
    // the same length, VAD ignores them (no vad edge, no window extension), the pre-roll ring is not filled with them.
  const r = rig({engine: 'sherpa', window_s: 2});
  r.g.wake(); r.ev.length = 0;
  r.g.hold(true);
  eq('A held() while held', r.g.held(), true);
  r.run(50, () => tone(-20, 1234));            // 1 s of our own cue, loud
  const a = r.audio();
  eq('A held frames stream as zeros of the same length', [a.length, a.every(e => e.pcm.length === 320 && e.pcm.every(x => x === 0))], [50, true]);
  eq('A no vad edge while held', r.kinds().filter(k => k === 'vad').length, 0);
  eq('A level reads silence while held', r.g.level() < -100, true);
  r.g.hold(false);
  eq('A held() through the tail', r.g.held(), true);
  r.ev.length = 0;
  r.run(15, () => tone(-20, 1234));            // the 300 ms tail, then live audio again
  eq('A the tail (300 ms) is zeros too', r.audio().every(e => e.pcm.every(x => x === 0)), true);
  eq('A tail over', r.g.held(), false);
  r.ev.length = 0;
  r.run(3, () => tone(-20, 77));
  eq('A after the tail: real audio again', r.audio().map(e => e.pcm[0]), [77, 77, 77]);
  // the hold never counted as talking: 2 s after wake (1 s held + tail + 60 ms) the window ran out on schedule
  const w = rig({engine: 'sherpa', window_s: 2});
  w.g.wake(); w.g.setSpeaking(true); w.g.setSpeaking(false);   // a window
  w.g.hold(true); w.run(80, () => tone(-20)); w.g.hold(false);  // 1.6 s held: loud but not talking
  w.run(30, () => silence());
  eq('A window not extended by a held cue', w.g.state(), 'sleep');
  // sleep: a held cue fills no pre-roll and opens no probe (transcript engine)
  const s = rig({engine: 'transcript'});
  s.g.hold(true); s.run(60, () => tone(-20, 5)); s.g.hold(false);
  eq('A sleep + held: no probe, no pre-roll', [s.g.state(), s.g.prerollFrames(), s.ev.length], ['sleep', 0, 0]);
  eq('A cue_tail_ms default', s.g.opts().cue_tail_ms, 300);
}

console.log(JSON.stringify({checks, failures}));
