// Direct STT from the phone (W4, stt_route=phone): the page talks to ElevenLabs Scribe v2 realtime itself with a
// single-use token minted by the server ({t:'need_token'} -> {t:'token'}), so the API key stays on the server and
// the VPS sees text only. Opened when the gate opens, closed when the conversation window ends.
// Injected: io.WebSocket (class), io.send(obj) (to the ride server), io.onPartial(text), io.onCommitted(text, lang,
// stt_s), io.onStatus(off|backoff|connecting|ready), io.onSessionFail(code), io.now() ms, io.timer(fn, ms),
// io.b64(Uint8Array) -> base64.
// Tested under node with a fake socket.
'use strict';
const RIDE_STT_BACKOFF = ['quota_exceeded', 'rate_limited', 'auth_error', 'resource_exhausted', 'unaccepted_terms'];
const RIDE_STT_ERRORS = RIDE_STT_BACKOFF.concat(['error', 'commit_throttled', 'queue_overflow', 'session_time_limit_exceeded',
  'input_error', 'invalid_request', 'chunk_size_exceeded', 'insufficient_audio_activity', 'transcriber_error']);

function RideStt(io, opts) {
  // One connection state machine (AUD-V2-01/02). `want` is the gate's desired state and survives backoff, provider
  // closes and ride-server link losses; reconcile() is the only place that opens a Scribe socket or asks for a
  // token, and it runs on every edge: open, token, token_error/backoff expiry, socket close, link up, ask timeout.
  // Fix round 2 (R2): an allocated Scribe socket must reach session_started within session_start_ms; a miss (or a
  // close before it) closes the socket and backs off (start_backoff_ms, growing per consecutive miss, reset by a
  // started session) before reconcile() tries again. Only session_started makes the status 'ready'.
  const o = Object.assign({token_max_age_ms: 12 * 60 * 1000, chunk_frames: 5, max_pending_frames: 300,
                           ask_timeout_ms: 10000, error_backoff_ms: 5000, session_start_ms: 8000,
                           start_backoff_ms: [2000, 4000, 8000, 15000, 30000]}, opts || {});
  const now = io.now || (() => Date.now()), timer = io.timer || ((f, ms) => setTimeout(f, ms));
  let token = null, tokenAt = 0, askedAt = null, retryAt = 0, link = true, lastStatus = 'off';
  let ws = null, ready = false, want = false, pending = [], chunk = [], lang = null, sentFrames = 0, usageFrames = 0;
  let startFails = 0;
  const stats = {sessions: 0, chunks: 0, partials: 0, committed: 0, errors: []};

  function b64(u8) {
    if (io.b64) return io.b64(u8);
    let s = ''; for (let i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]);
    return btoa(s);
  }
  function status() {                      // off | backoff | connecting | ready
    if (ws && ready) return 'ready';
    if (now() < retryAt) return 'backoff';
    return want ? 'connecting' : 'off';
  }
  function report() {
    const s = status();
    if (s !== lastStatus) { lastStatus = s; if (io.onStatus) io.onStatus(s); }
  }
  function asking() { return askedAt !== null && now() - askedAt < o.ask_timeout_ms; }
  function fresh() { return token && now() - tokenAt < o.token_max_age_ms; }
  function needToken() {                   // also the prefetch: at most one outstanding request, none in backoff
    if (!link || asking() || fresh() || now() < retryAt) return;
    askedAt = now(); io.send({t: 'need_token'});
    timer(() => { if (askedAt !== null && now() - askedAt >= o.ask_timeout_ms) { askedAt = null; reconcile(); } },
          o.ask_timeout_ms + 10);          // a lost answer (link drop, server restart) never blocks later requests
  }
  function backoff(ms) {
    retryAt = Math.max(retryAt, now() + ms);
    timer(() => { if (now() >= retryAt) { reconcile(); if (!want) needToken(); } }, retryAt - now() + 50);
  }
  function reconcile() {
    if (want && !ws && link && now() >= retryAt) { if (fresh()) connect(); else { token = null; needToken(); } }
    report();
  }
  function onServer(m) {                   // token / token_error / stt_backoff frames from the ride server
    if (m.t === 'token' && m.token) {
      token = m; tokenAt = now(); askedAt = null;
      reconcile();
    } else if (m.t === 'token_error' || m.t === 'stt_backoff') {
      askedAt = null;
      backoff(1000 * (m.retry_in || 5));
      report();
    }
  }
  function connect() {
    const t = token; token = null;                       // single use
    const q = [];
    for (const [k, v] of Object.entries(t.params || {})) {
      for (const x of (Array.isArray(v) ? v : [v])) q.push(encodeURIComponent(k) + '=' + encodeURIComponent(x));
    }
    q.push('token=' + encodeURIComponent(t.token));
    ready = false; stats.sessions++;
    const s = ws = new io.WebSocket(t.url + '?' + q.join('&'));
    timer(() => { if (ws === s && !ready) startFailed('session_timeout'); }, o.session_start_ms);
    s.onmessage = ev => {
      if (ws !== s) return;
      let d; try { d = JSON.parse(ev.data); } catch (e) { return; }
      const mt = d.message_type;
      if (mt === 'session_started') {
        ready = true; startFails = 0; io.send({t: 'stt_ok'});
        const p = pending; pending = [];
        p.forEach(f => push(f));
        report();
      } else if (mt === 'partial_transcript') {
        stats.partials++; if (d.text) io.onPartial(d.text);
      } else if (mt === 'committed_transcript_with_timestamps') {
        lang = d.language_code || null;
      } else if (mt === 'committed_transcript') {
        stats.committed++;
        const sttS = sentFrames * 0.02; sentFrames = 0;
        if (d.text && d.text.trim()) io.onCommitted(d.text, lang, +sttS.toFixed(2));
        lang = null;
      } else if (RIDE_STT_ERRORS.includes(mt)) {
        stats.errors.push(mt);
        io.send({t: 'stt_error', code: mt});
        if (RIDE_STT_BACKOFF.includes(mt)) { drop(); backoff(o.error_backoff_ms); }   // the server's stt_backoff extends it
      }
    };
    s.onclose = () => {
      if (ws !== s) return;
      if (!ready) { startFailed('session_closed'); return; }   // never started: back off, then try again
      ws = null; ready = false;
      reconcile();                         // e.g. session time limit: the prefetched token, or a new one (rate-limited)
    };
    s.onerror = () => {};
    needToken();                           // prefetch the next one (tokens live 15 min)
    report();
  }
  function startFailed(code) {             // the socket never reached session_started: close it, back off, retry
    stats.errors.push(code);
    io.send({t: 'stt_error', code});
    drop();
    const b = o.start_backoff_ms;
    backoff(b[Math.min(startFails++, b.length - 1)]);
    if (io.onSessionFail) io.onSessionFail(code);
    report();
  }
  function push(frame) {                   // 20 ms frames -> 100 ms input_audio_chunk messages
    chunk.push(frame);
    if (chunk.length < o.chunk_frames) return;
    flush();
  }
  function flush() {
    if (!chunk.length || !ws || !ready) return;
    const n = chunk.reduce((a, f) => a + f.length, 0), all = new Int16Array(n);
    let off = 0; for (const f of chunk) { all.set(f, off); off += f.length; }
    sentFrames += chunk.length; usageFrames += chunk.length; chunk = [];
    ws.send(JSON.stringify({message_type: 'input_audio_chunk', audio_base_64: b64(new Uint8Array(all.buffer)),
                            commit: false, sample_rate: 16000}));
    stats.chunks++;
  }
  function audio(frame) {
    if (!want || !link) return;            // audio from a link outage would be stale by the time it is heard
    if (ws && ready) push(frame);
    else { pending.push(frame); if (pending.length > o.max_pending_frames) pending.shift(); }
  }
  function open() { want = true; reconcile(); }
  function drop() {
    pending = []; chunk = [];
    const s = ws; ws = null; ready = false;
    if (s) try { s.close(); } catch (e) {}
  }
  function close() { want = false; flush(); drop(); report(); }
  // The ride-server link: text can't reach the server while it is down, so the Scribe socket goes too; the request
  // in flight is forgotten (its answer would come on the dead socket). `want` stays: link up reopens at once.
  function linkDown() { link = false; askedAt = null; drop(); report(); }
  function linkUp() { link = true; reconcile(); }
  // AUD-V2-12: seconds of audio actually sent to Scribe since the last call, with or without any transcript
  function takeUsageS() { const s = +(usageFrames * 0.02).toFixed(2); usageFrames = 0; return s; }
  return {open, close, audio, onServer, linkDown, linkUp, status, prefetch: needToken, takeUsageS,
          stats: () => Object.assign({}, stats, {open: !!ws, ready, want, link, pending: pending.length, status: status()})};
}
if (typeof module === 'object' && module.exports) module.exports = {RideStt, RIDE_STT_BACKOFF};
