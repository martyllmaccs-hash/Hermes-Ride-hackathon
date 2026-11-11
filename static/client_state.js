// Page protocol v2 + the one audio queue (cues, status clips, reply segments). Loaded by index.html and, from this
// same file, by tests/client_state_test.js under node. Browser bits are injected:
//   io.audio              the one long-lived <audio> (addEventListener, play() -> Promise, pause(), src)
//   io.makeUrl(bytes, mime)   reply audio (Uint8Array) -> blob URL        io.revoke(url)   URL.revokeObjectURL
//   io.cueUrl(path)       cue/clip path from hello.cfg ("cues/done.m4a") -> preloaded URL (blob from Cache Storage)
//   io.send(obj)          JSON to the server (no-op while the WebSocket is down)
//   io.render()           redraw (index.html: the orb, orb.js; RIDE_COLOURS/colour()/pulse() are the old colour page's
//                         table, kept for tests and as a reference, not drawn since WP5)
//   io.timer(fn, ms, kind) -> id, io.clearTimer(id), io.random() -> [0,1)   (kind 'media': playback watchdogs)
//   io.tone(gain), io.toneStop()   R3-4: one soft "doo, doo" (WebAudio, outside the queue) / silence it now
//   io.envelope(bytes)    Fix F: a WAV reply segment -> its level curve (dBFS per 50 ms) for the orb, or null
//   io.cue(on)            Fix A: a cue or clip starts (true) / ends (false): the page holds the mic gate meanwhile, so
//                         Scribe and the wake spotter never hear our own "yes", "I'm here" or "thinking". Not for replies.
// Every JSON server frame carries an epoch (server boot id + connection id). A new epoch or a closed socket drops
// all reply audio and turn state, and frames of any other epoch are ignored. Binary audio frames belong to the
// socket they arrive on. Cues and clips are local: they play with no connection at all.
'use strict';
const RIDE_COLOURS = {            // on black, low brightness; the page fades between them (600 ms)
  sleep: '#000000', listening: '#2a0000', thinking: '#00002e', speaking: '#002200',
  reconnecting: '#2a1a00', offline: '#2a1a00', connecting: '#2a1a00',
  muted: '#141414',               // round 3: the MUTE button (dim grey, apart from sleep's black)
};
const RIDE_PULSE = {reconnecting: true, offline: true};
const RIDE_DEFAULT_CUES = {wake: ['cues/wake_a.m4a', 'cues/wake_b.m4a'], thinking: 'cues/thinking.m4a', done: 'cues/done.m4a',
                           mute: 'cues/mute.m4a', unmute: 'cues/unmute.m4a',       // round 3: the MUTE / UNMUTE button
                           reset: 'cues/reset.m4a', reset_failed: 'cues/reset_failed.m4a'};   // WP6: the reset bar
const RIDE_DEFAULT_CLIPS = {reconnecting: 'clips/reconnecting.m4a', back: 'clips/back.m4a', quota: 'clips/quota.m4a',
                            working: 'clips/working.m4a', offline: 'clips/offline.m4a', nomic: 'clips/nomic.m4a'};
const AUDIO_TYPES = {1: 'audio/mpeg', 2: 'audio/wav', 3: 'audio/mp4', 4: 'audio/ogg', 5: 'audio/aac', 6: 'audio/flac'};

function rideDecodeFrame(buf) {   // ArrayBuffer -> {mime, turn, seq, bytes} | null
  if (!buf || buf.byteLength < 7) return null;
  const v = new DataView(buf), type = v.getUint8(0);
  if (!AUDIO_TYPES[type]) return null;
  return {mime: AUDIO_TYPES[type], turn: v.getUint32(1), seq: v.getUint16(5), bytes: new Uint8Array(buf, 7)};
}

// R3-2: cue/clip loudness (hello.cfg.cue_gain) is applied to the decoded cue audio, not to the <audio> element: iOS
// ignores HTMLMediaElement.volume. AudioBuffer-like {numberOfChannels, sampleRate, length, getChannelData(i)} ->
// mono 16-bit PCM WAV (ArrayBuffer), samples scaled by gain and clipped.
function rideCueGain(cfg) {
  const g = Number(cfg && cfg.cue_gain);
  return Number.isFinite(g) ? Math.max(0, Math.min(1, g)) : 0.5;
}
function rideScaledWav(buf, gain) {
  const n = buf.length, nc = buf.numberOfChannels, rate = buf.sampleRate;
  const ch = []; for (let c = 0; c < nc; c++) ch.push(buf.getChannelData(c));
  const out = new ArrayBuffer(44 + n * 2), v = new DataView(out);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); str(8, 'WAVE'); str(12, 'fmt ');
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); str(36, 'data'); v.setUint32(40, n * 2, true);
  const fade = Math.min(Math.round(rate * 0.003), Math.floor(n / 2));   // 3 ms in and out: no click at start or stop
  for (let i = 0; i < n; i++) {
    let x = 0; for (let c = 0; c < nc; c++) x += ch[c][i];
    const k = fade > 0 ? Math.min(1, i / fade, (n - 1 - i) / fade) : 1;
    x = Math.max(-1, Math.min(1, x / nc * gain * k));
    v.setInt16(44 + i * 2, Math.round(x * 32767), true);
  }
  return out;
}

function RideClient(io) {
  const A = io.audio;
  const timer = io.timer || ((f, ms) => setTimeout(f, ms)), clearTimer = io.clearTimer || (id => clearTimeout(id));
  const rnd = io.random || Math.random;
  let cues = RIDE_DEFAULT_CUES, clips = RIDE_DEFAULT_CLIPS, cfg = {};
  let open = false, epoch = null, serverState = 'listening', gate = 'sleep';
  let q = [], cur = null, playing = false;
  let spkTurn = null, endedTurns = new Set(), dropTurns = new Set(), failedTurns = new Set(), watchdog = null;
  let att = null, retryT = null, holdT = null;   // the current playback attempt {it, done}; the pending retry timer; the seg-0 hold
  let cueHeld = false;                     // Fix A: io.cue(true) was the last call
  let cuedThinking = new Set();
  let down = null, downTimers = [], downClipPlayed = false, everOpen = false, recog = false, muted = false;
  const log = [];                          // what the queue played, in order (tests + debug)

  // ---- the thinking tone (R3-4, WP2): the Hermes voice-mode "thinking" blubs (apps/desktop/src/lib/thinking-sound.ts),
  // "doo, doo", for as long as Lana is working. It belongs to 'thinking' (an 'extended' or 'working' frame is only more
  // of the same, WP1): the first one sounds hello.cfg.tone_first_s (default 1.2 s) after the 'thinking' frame, so a
  // quick answer never hears it, then one every 4-6 s. A tick while a cue, clip or reply plays is skipped. It stops at
  // once on reply audio, ready, turn end, stop/discard, listening, a wake, a lost link or a new epoch, and when the page
  // stops capture; it does not start again for a turn that already ended its tone or began speaking.
  const TONE_FIRST_DEFAULT_MS = 1200, TONE_EVERY_MS = 4000, TONE_JITTER_MS = 2000;
  let toneTurn = null, toneT = null;
  const toneEnded = new Set();
  function toneFirstMs() {
    const raw = cfg.tone_first_s, v = raw == null || raw === '' ? NaN : Number(raw);
    return Number.isFinite(v) ? Math.max(0, Math.min(30, v)) * 1000 : TONE_FIRST_DEFAULT_MS;
  }
  // ---- the playback buffer: the first segment of a reply (kind 'seg', seq 0) waits hello.cfg.play_buffer_ms (default
  // 250, 0 to 1000, 0 = off) before play(), so the next segments are already queued when it starts and the joins
  // do not gap. Cues and clips never wait; a retry of a held segment does not wait again.
  const PLAY_BUFFER_DEFAULT_MS = 250;
  function playBufferMs() {
    const raw = cfg.play_buffer_ms, v = raw == null || raw === '' ? NaN : Number(raw);
    return Number.isFinite(v) ? Math.round(Math.max(0, Math.min(1000, v))) : PLAY_BUFFER_DEFAULT_MS;
  }
  function toneStart(turn) {
    const k = turn == null ? 'none' : turn;
    if (!io.tone || toneTurn === k) return;
    if (spkTurn !== null || toneEnded.has(k) || (turn != null && dropTurns.has(turn))) return;
    toneStop();
    toneTurn = k;
    toneT = timer(toneTick, toneFirstMs(), 'tone');
  }
  function toneTick() {
    toneT = null;
    if (toneTurn === null) return;
    if (!playing && spkTurn === null) { try { io.tone(rideCueGain(cfg)); } catch (e) {} }
    toneT = timer(toneTick, TONE_EVERY_MS + rnd() * TONE_JITTER_MS, 'tone');
  }
  function toneStop() {
    const was = toneTurn !== null || toneT !== null;
    if (toneTurn !== null && toneTurn !== 'none') toneEnded.add(toneTurn);
    if (toneT !== null) { clearTimer(toneT); toneT = null; }
    toneTurn = null;
    if (was && io.toneStop) { try { io.toneStop(); } catch (e) {} }
  }

  // ---- the queue: one item plays at a time, cues never overlap a reply
  function enqueue(item, front) {
    if (front) q.unshift(item); else q.push(item);
    playNext();
  }
  function cue(name, turn, front) {
    const path = name === 'wake' ? cues.wake[Math.floor(rnd() * cues.wake.length) % cues.wake.length] : cues[name] || RIDE_DEFAULT_CUES[name];
    if (path) enqueue({kind: 'cue', name, turn, url: io.cueUrl(path)}, front);
  }
  function clip(name) {
    if (clips[name]) enqueue({kind: 'clip', name, url: io.cueUrl(clips[name])});
  }
  // AUD-V2-04: a rejected play(), a media error or a stall (no 'playing' within START_MS, no 'ended' by the item's
  // duration + slack) is a failure, not a completion: the item is tried once more, then skipped so later cues,
  // clips and replies still play. A reply with a failed segment is reported (play_error, idle heard:false) and gets
  // no done cue: the server logs it as not delivered instead of answered.
  // Fix round 2 (R3): every start() is one attempt. The first failure signal of an attempt (rejected play(), media
  // error, watchdog) retires it: the element is paused, at most one retry timer is pending (its handle is kept and
  // cancelled with the item), and any later event of that attempt (another error, a late 'playing'/'ended') is
  // ignored, so it can neither schedule a second retry nor complete the item.
  const START_MS = 8000, SLACK_MS = 3000, MAX_MS = 60000, RETRY_MS = 300;
  function arm(ms, a) { disarm(); watchdog = timer(() => { watchdog = null; fail(a, 'stalled'); }, ms, 'media'); }
  function disarm() { if (watchdog !== null) { clearTimer(watchdog); watchdog = null; } }
  function cancelRetry() { if (retryT !== null) { clearTimer(retryT); retryT = null; } }
  function cancelHold() { if (holdT !== null) { clearTimer(holdT); holdT = null; } }
  function live() { return att && !att.done ? att : null; }
  function playNext() {
    if (playing || !q.length) return;
    const it = q.shift();
    playing = true; cur = it;
    start(it);
  }
  function holdMic(on) {
    if (!io.cue || on === cueHeld) return;
    cueHeld = on;
    try { io.cue(on); } catch (e) {}
  }
  const ownVoice = it => it.kind === 'cue' || it.kind === 'clip';
  function start(it) {
    if (ownVoice(it)) holdMic(true);
    const hold = !it.tries && it.kind === 'seg' && it.seq === 0 ? playBufferMs() : 0;
    if (hold > 0) {                          // no attempt and no watchdog yet: both begin when play() is called
      cancelHold();
      holdT = timer(() => { holdT = null; if (cur === it) begin(it); }, hold, 'media');
      return;
    }
    begin(it);
  }
  function begin(it) {
    log.push(it.kind === 'seg' ? `seg:${it.turn}.${it.seq}` : `${it.kind}:${it.name}`);
    it.tries = (it.tries || 0) + 1;
    const a = att = {it, done: false};
    A.src = it.url;
    arm(START_MS, a);
    let p;
    try { p = A.play(); } catch (e) { p = Promise.reject(e); }
    Promise.resolve(p).catch(e => fail(a, (e && e.name) || 'play_rejected'));
  }
  function fail(a, why) {
    if (!a || a.done || att !== a) return;   // one failure transition per attempt
    a.done = true;
    disarm();
    try { A.pause(); } catch (e) {}          // the retired attempt is stopped, whatever it does later
    const it = a.it;
    if (it.tries < 2) { cancelRetry(); retryT = timer(() => { retryT = null; if (cur === it) start(it); }, RETRY_MS, 'media'); return; }
    if (it.kind === 'seg') { failedTurns.add(it.turn); io.send({t: 'play_error', turn: it.turn, seq: it.seq, why: String(why).slice(0, 32)}); }
    finish(it);
  }
  function finish(it) {
    disarm(); cancelRetry(); cancelHold(); att = null;
    if (ownVoice(it)) holdMic(false);
    if (it.kind === 'seg') io.revoke(it.url);
    cur = null; playing = false;
    if (it.kind === 'seg') maybeIdle();
    playNext();
  }
  function hasSegs(turn) { return (cur && cur.kind === 'seg' && cur.turn === turn) || q.some(i => i.kind === 'seg' && i.turn === turn); }
  function maybeIdle() {
    if (spkTurn !== null && endedTurns.has(spkTurn) && !hasSegs(spkTurn)) {
      const t = spkTurn, heard = !failedTurns.has(t);
      io.send({t: 'idle', turn: t, heard}); spkTurn = null;
      if (heard) cue('done', t, true);     // answer finished: done right after the last segment
      io.render();
    }
  }
  A.addEventListener('playing', () => {
    const a = live();
    if (!a) return;
    const it = a.it, d = A.duration;
    arm(Number.isFinite(d) && d > 0 ? d * 1000 + SLACK_MS : MAX_MS, a);
    if (it.kind === 'seg' && !it.reported) { it.reported = true; if (it.seq === 0) io.send({t: 'play', turn: it.turn, seq: 0}); }
  });
  A.addEventListener('ended', () => { const a = live(); if (a) { a.done = true; finish(a.it); } });
  A.addEventListener('error', () => fail(live(), 'media_error'));

  function dropSegs(pred) {                // remove reply audio (queued + playing) matching pred
    q = q.filter(i => { if (i.kind === 'seg' && pred(i)) { io.revoke(i.url); return false; } return true; });
    if (cur && cur.kind === 'seg' && pred(cur)) { A.pause(); disarm(); cancelRetry(); cancelHold(); att = null; io.revoke(cur.url); cur = null; playing = false; }
  }
  function reset() {                       // link lost or new epoch: no reply audio of the old connection survives
    toneStop(); toneEnded.clear();
    dropSegs(() => true);
    q = q.filter(i => i.kind === 'clip' || (i.kind === 'cue' && ['wake', 'mute', 'unmute', 'reset', 'reset_failed'].includes(i.name)));
    spkTurn = null; endedTurns.clear(); dropTurns.clear(); failedTurns.clear();
    cuedThinking.clear();
    serverState = 'listening';
    playNext();
  }
  function stopTurn(turn) {                // barge-in or discard: silence that turn now, no done cue
    toneStop();
    dropTurns.add(turn);
    dropSegs(i => i.turn === turn);
    q = q.filter(i => !(i.kind === 'cue' && i.turn === turn));
    if (spkTurn === turn) spkTurn = null;
    io.render(); playNext();
  }

  // ---- server frames
  function onState(m) {
    const s = m.s, turn = m.turn;
    if (s === 'thinking') {
      serverState = 'thinking';
      if (toneTurn !== null && toneTurn !== (turn == null ? 'none' : turn)) toneStop();   // a new turn
      if (turn == null || !cuedThinking.has(turn)) { if (turn != null) cuedThinking.add(turn); cue('thinking', turn); }
      toneStart(turn);
    } else if (s === 'extended') {
      if (turn != null && dropTurns.has(turn)) { io.render(); return; }     // a late frame of a barged-in turn
      // WP1 (D3/P4): no separate state, cue or colour. 'extended' (and 'working') are more thinking; the server still
      // sends them for its logs. Setting 'thinking' here also keeps a later repeated 'thinking' frame invisible.
      serverState = 'thinking';
      toneStart(turn);
    } else if (s === 'ready') {
      toneStop();
      if (turn == null || !dropTurns.has(turn)) cue('done', turn);                  // before the first segment
    } else if (s === 'working') {
      if (turn != null && dropTurns.has(turn)) { io.render(); return; }
      serverState = 'thinking';
      toneStart(turn);                           // R3-4: the tone, no spoken clip
    } else if (s === 'quota') {
      clip('quota'); downClipPlayed = true;      // 'back' once recognition works again
    } else if (s === 'listening') {
      toneStop();
      serverState = 'listening';
    }
    io.render();
  }
  function onMessage(m) {
    if (!m || typeof m !== 'object') return;
    if (m.t === 'hello') {
      if (m.epoch !== epoch) { reset(); epoch = m.epoch; }
      cfg = m.cfg || {};
      if (cfg.cues) cues = cfg.cues;
      if (cfg.clips) clips = cfg.clips;
      io.render();
      return;
    }
    if (epoch === null || m.epoch !== epoch) return;   // another connection's (or server's) frame
    if (m.t === 'state') onState(m);
    else if (m.t === 'end') { toneStop(); endedTurns.add(m.turn); maybeIdle(); }
    else if (m.t === 'stop' || m.t === 'discard') stopTurn(m.turn);
  }
  function onBinary(buf) {
    if (epoch === null) return;
    const f = rideDecodeFrame(buf);
    if (!f || dropTurns.has(f.turn)) return;
    toneStop();
    let env = null;
    if (f.mime === 'audio/wav' && io.envelope) { try { env = io.envelope(f.bytes); } catch (e) {} }
    enqueue({kind: 'seg', turn: f.turn, seq: f.seq, url: io.makeUrl(f.bytes, f.mime), env});
    spkTurn = f.turn; io.render();
  }

  // ---- link up/down: local clips only (they work with no connection). A link that is up again is not yet
  // recognition that works again (AUD-V2-01/02): 'back' waits for the page's recognition(true), which it reports
  // from its gate + STT + token state once a fresh Scribe session (or the server route) is really listening.
  function onOpen() {
    open = true;
    downTimers.forEach(clearTimer); downTimers = [];
    down = null; everOpen = true;
    io.render();
  }
  // The page reports recognition on every change; while the gate is open and it is not up, the display says
  // 'connecting', never 'listening' (fix round 2, R2).
  function recognition(ok) {
    if (!!ok !== recog) { recog = !!ok; io.render(); }
    if (ok && open && downClipPlayed) { downClipPlayed = false; clip('back'); }
  }
  function sttDown() {                     // the page's Scribe session failed to start while awake: say so once
    if (!downClipPlayed) { downClipPlayed = true; clip('reconnecting'); io.render(); }
  }
  function onClose() {
    const was = open;
    open = false; epoch = null; reset();
    if (was || (everOpen && down === null)) {
      down = 'reconnecting';
      downTimers.forEach(clearTimer);
      downTimers = [
        timer(() => { if (!open) { downClipPlayed = true; clip('reconnecting'); io.render(); } }, 5000),
        timer(() => { if (!open) { down = 'offline'; downClipPlayed = true; clip('offline'); io.render(); } }, 60000),
      ];
    }
    io.render();
  }

  // ---- local events from the page
  function wake() { toneStop(); cue('wake'); io.render(); }      // the wake answer plays now, no server round trip
  function setGate(s) { if (s !== gate) { gate = s; io.render(); } }
  function noMic() { clip('nomic'); }
  // Round 3 (A2): the page muted / unmuted the mic (the MUTE button, or the lock screen pause / stop). One short cue per
  // toggle, next in the queue: it never cuts a reply segment that is playing, it plays right after it. The flag is page
  // state: it survives a link loss (reset() does not touch it).
  function setMuted(on) {
    if (!!on === muted) return;
    muted = !!on;
    cue(muted ? 'mute' : 'unmute', null, true);
    io.render();
  }
  // WP6: the reset bar was held 3 s (ok) / the server said the reset failed: one short cue, next in the queue
  function resetCue(ok) { cue(ok ? 'reset' : 'reset_failed', null, true); }

  function display() {
    if (!open) return everOpen ? (down || 'reconnecting') : 'offline';
    if (spkTurn !== null) return 'speaking';
    if (serverState === 'thinking') return 'thinking';
    if (muted) return 'muted';            // round 3: a reply already asked for still shows; otherwise the mute does
    if (gate === 'sleep' || gate === 'probe') return 'sleep';     // WP3 (P2): a probe is still asleep for the rider
    return recog ? 'listening' : 'connecting';
  }
  function nowPlaying() {                  // Fix F: what the <audio> element plays now (the orb reads env)
    return cur ? {kind: cur.kind, turn: cur.turn, seq: cur.seq, env: cur.env || null} : null;
  }
  function word() { return display(); }
  function colour() { return RIDE_COLOURS[display()]; }
  function pulse() { return !!RIDE_PULSE[display()]; }
  function state() {                       // for tests
    return {open, epoch, serverState, gate, spkTurn, playing, queued: q.length, queue: q.map(i => i.kind === 'seg' ? `seg:${i.turn}.${i.seq}` : `${i.kind}:${i.name}`),
            endedTurns: [...endedTurns], dropTurns: [...dropTurns], word: word(), log: log.slice()};
  }
  return {word, colour, pulse, display, onMessage, onBinary, onOpen, onClose, recognition, sttDown, wake, setGate, noMic, setMuted, resetCue, state, toneStop, nowPlaying,
          cfg: () => cfg};
}
if (typeof module === 'object' && module.exports) module.exports = {RideClient, rideDecodeFrame, RIDE_COLOURS, rideScaledWav, rideCueGain};
