// W4: the phone's Scribe client (static/stt.js) against a fake Scribe WebSocket. Prints {checks, failures}.
'use strict';
const path = require('path');
const {RideStt} = require(path.join(__dirname, '..', 'static', 'stt.js'));

let checks = 0;
const failures = [];
function eq(name, got, want) { checks++; if (JSON.stringify(got) !== JSON.stringify(want)) failures.push({name, got, want}); }

function rig(extra) {
  const r = {sockets: [], server: [], partials: [], committed: [], t: 0, timers: []};
  class FakeScribe {                         // the browser WebSocket surface stt.js uses
    constructor(url) { this.url = url; this.sent = []; this.closed = false; r.sockets.push(this); }
    send(s) { this.sent.push(JSON.parse(s)); }
    close() { this.closed = true; }
    emit(m) { this.onmessage({data: JSON.stringify(m)}); }
  }
  r.stt = RideStt({
    WebSocket: FakeScribe, send: m => r.server.push(m), onPartial: t => r.partials.push(t),
    onCommitted: (t, lang, s) => r.committed.push([t, lang, s]), now: () => r.t,
    timer: (f, ms) => r.timers.push({f, at: r.t + ms}), b64: u8 => Buffer.from(u8).toString('base64'), ...(extra || {}),
  });
  r.token = n => r.stt.onServer({t: 'token', kind: 'realtime_scribe', token: 'sutkn_' + n, url: 'wss://scribe.test/rt',
                                 params: {model_id: 'scribe_v2_realtime', commit_strategy: 'vad', secondary_languages: ['en', 'zh']}});
  r.frame = v => { const f = new Int16Array(320); f.fill(v); return f; };
  r.advance = ms => { r.t += ms; r.timers.filter(x => x.at <= r.t && !x.done).forEach(x => { x.done = true; x.f(); }); };
  return r;
}

// 1. token flow: prefetch, open uses the token once, the url carries params (repeated lists) + token
{
  const r = rig();
  r.stt.prefetch();
  eq('1 asks once', r.server, [{t: 'need_token'}]);
  r.stt.prefetch();
  eq('1 no duplicate ask', r.server.length, 1);
  r.token('A');
  eq('1 holding a token: no socket yet', r.sockets.length, 0);
  r.stt.open();
  const s = r.sockets[0];
  eq('1 url', s.url, 'wss://scribe.test/rt?model_id=scribe_v2_realtime&commit_strategy=vad&secondary_languages=en&secondary_languages=zh&token=sutkn_A');
  eq('1 prefetches the next token', r.server.filter(m => m.t === 'need_token').length, 2);
  // 2. audio before session_started is kept and sent first, in 100 ms chunks
  for (let i = 0; i < 7; i++) r.stt.audio(r.frame(i));
  eq('2 nothing before session_started', s.sent.length, 0);
  s.emit({message_type: 'session_started', session_id: 'x', config: {}});
  eq('2 stt_ok to the server', r.server.slice(-1), [{t: 'stt_ok'}]);
  eq('2 one 100 ms chunk sent', s.sent.length, 1);
  const c = s.sent[0];
  eq('2 chunk format', [c.message_type, c.commit, c.sample_rate, Buffer.from(c.audio_base_64, 'base64').length],
     ['input_audio_chunk', false, 16000, 5 * 320 * 2]);
  const b = Buffer.from(c.audio_base_64, 'base64');
  eq('2 pre-session audio in order', [0, 1, 4].map(i => new Int16Array(b.buffer, b.byteOffset, b.length / 2)[i * 320]), [0, 1, 4]);
  for (let i = 0; i < 3; i++) r.stt.audio(r.frame(9));
  eq('2 second chunk after 5 frames', s.sent.length, 2);
  // 3. partial + committed go to the page with the detected language and the seconds sent
  s.emit({message_type: 'partial_transcript', text: 'hey lana how'});
  s.emit({message_type: 'committed_transcript_with_timestamps', text: 'Hey, Lana. How fast?', language_code: 'en', words: []});
  s.emit({message_type: 'committed_transcript', text: 'Hey, Lana. How fast?'});
  eq('3 partial', r.partials, ['hey lana how']);
  eq('3 committed with lang and stt_s', r.committed, [['Hey, Lana. How fast?', 'en', 0.2]]);
  s.emit({message_type: 'committed_transcript', text: '  '});
  eq('3 empty commit ignored', r.committed.length, 1);
  // 4. close when the window ends: socket closed, later audio dropped
  r.stt.close();
  eq('4 closed', [s.closed, r.stt.stats().open], [true, false]);
  r.stt.audio(r.frame(1));
  eq('4 no audio after close', s.sent.length, 2);   // 7 + 3 frames = exactly two chunks, nothing left
}

// 5. quota: stt_error to the server, no reconnect storm; retry only after the server's backoff
{
  const r = rig();
  r.token('A'); r.stt.open();
  const s = r.sockets[0];
  s.emit({message_type: 'session_started'});
  s.emit({message_type: 'quota_exceeded', error: 'Quota exceeded'});
  eq('5 stt_error sent', r.server.filter(m => m.t === 'stt_error'), [{t: 'stt_error', code: 'quota_exceeded'}]);
  eq('5 socket dropped, not reopened', [s.closed, r.sockets.length], [true, 1]);
  r.stt.onServer({t: 'stt_backoff', code: 'quota_exceeded', retry_in: 5});
  r.advance(1000);
  r.stt.open();
  eq('5 open during backoff: no token ask, no socket', [r.sockets.length, r.server.filter(m => m.t === 'need_token').length], [1, 1]);
  r.advance(4100);
  eq('5 asks again after 5 s', r.server.filter(m => m.t === 'need_token').length, 2);
  r.token('B');
  eq('5 reconnects with the new token', [r.sockets.length, r.sockets[1].url.endsWith('token=sutkn_B')], [2, true]);
}

// 6. a session that ends while the gate is open (time limit) reconnects with a fresh token; expired tokens unused
{
  const r = rig();
  r.token('A'); r.stt.open();
  r.sockets[0].emit({message_type: 'session_started'});
  r.sockets[0].onclose();
  eq('6 asks for a token', r.server.filter(m => m.t === 'need_token').length >= 1, true);
  r.token('B');
  eq('6 reconnected', r.sockets.length, 2);
  r.stt.close();
  r.token('C');
  r.advance(13 * 60 * 1000);                 // the held token is older than 12 min
  r.stt.open();
  eq('6 stale token not used', r.sockets.length, 2);
  r.token('D');
  eq('6 fresh token used', r.sockets[2].url.endsWith('token=sutkn_D'), true);
}

function block(name, fn) { try { fn(); } catch (e) { failures.push({name: name + ' crashed', got: String(e.stack || e)}); } }
const asks = r => r.server.filter(m => m.t === 'need_token').length;

// 7. AUD-V2-02: quota while the gate stays open: no manual open(); after the backoff the client gets a token and
//    reconnects by itself
block('7', () => {
  const r = rig();
  r.token('A'); r.stt.open();
  r.sockets[0].emit({message_type: 'session_started'});
  r.sockets[0].emit({message_type: 'quota_exceeded'});
  r.stt.onServer({t: 'stt_backoff', code: 'quota_exceeded', retry_in: 5});
  r.advance(5100);
  eq('7 asks after the backoff', asks(r) >= 2, true);
  r.token('B');
  eq('7 reconnects without a manual open', [r.sockets.length, r.sockets[1] && r.sockets[1].url.endsWith('token=sutkn_B')], [2, true]);
});

// 8. AUD-V2-02: the provider closes a session after the replacement token was already prefetched: use it now
block('8', () => {
  const r = rig();
  r.token('A'); r.stt.open();
  r.token('B');                              // the prefetch answered
  r.sockets[0].emit({message_type: 'session_started'});
  r.sockets[0].onclose();
  eq('8 reconnects with the prefetched token', [r.sockets.length, r.sockets[1] && r.sockets[1].url.endsWith('token=sutkn_B')], [2, true]);
});

// 9. AUD-V2-02: retryAt is honoured even with a fresh cached token; after it the cached token is used
block('9', () => {
  const r = rig();
  r.token('A'); r.stt.open(); r.token('B');
  r.sockets[0].emit({message_type: 'session_started'});
  r.sockets[0].emit({message_type: 'rate_limited'});
  r.stt.onServer({t: 'stt_backoff', code: 'rate_limited', retry_in: 5});
  r.advance(1000);
  eq('9 no socket during the backoff', r.sockets.length, 1);
  r.advance(4100);
  eq('9 cached token used after the backoff', [r.sockets.length, r.sockets[1] && r.sockets[1].url.endsWith('token=sutkn_B')], [2, true]);
});

// 10. AUD-V2-01: a token request that is never answered times out and is asked again
block('10', () => {
  const r = rig();
  r.stt.open();
  eq('10 asks', asks(r), 1);
  r.advance(5000);
  eq('10 no duplicate while outstanding', asks(r), 1);
  r.advance(6000);
  eq('10 asks again after the request timed out', asks(r), 2);
});

// 11. AUD-V2-01: link down/up: the outstanding request is forgotten (the server lost it), the desired state survives
block('11', () => {
  const r = rig();
  r.token('A'); r.stt.open();
  r.sockets[0].emit({message_type: 'session_started'});
  r.stt.linkDown();
  eq('11 scribe socket dropped on link loss', [r.sockets[0].closed, r.stt.stats().want], [true, true]);
  const before = asks(r);
  r.stt.audio(r.frame(1));
  eq('11 no buffering while the link is down', r.stt.stats().pending, 0);
  r.stt.linkUp();
  eq('11 asks again at once on link up', asks(r), before + 1);
  r.token('B');
  eq('11 reopened', [r.sockets.length, r.sockets[1] && r.sockets[1].url.endsWith('token=sutkn_B')], [2, true]);
});

// 12. status: off -> connecting -> ready -> backoff -> connecting -> ready (the page plays 'back' on ready only)
block('12', () => {
  const seen = [], r = rig({onStatus: s => { if (seen[seen.length - 1] !== s) seen.push(s); }});
  r.token('A'); r.stt.open();
  r.sockets[0].emit({message_type: 'session_started'});
  r.sockets[0].emit({message_type: 'quota_exceeded'});
  r.stt.onServer({t: 'stt_backoff', code: 'quota_exceeded', retry_in: 5});
  r.advance(5100); r.token('B');
  r.sockets[1].emit({message_type: 'session_started'});
  eq('12 status sequence', seen, ['connecting', 'ready', 'backoff', 'connecting', 'ready']);
  eq('12 status()', r.stt.status(), 'ready');
});

// 13. AUD-V2-12: every 20 ms frame sent to Scribe is counted, with or without a transcript (probe misses, empty
//     commits, the tail after the last commit), and taken once
block('13', () => {
  const r = rig();
  r.token('A'); r.stt.open();
  r.sockets[0].emit({message_type: 'session_started'});
  for (let i = 0; i < 10; i++) r.stt.audio(r.frame(1));
  r.sockets[0].emit({message_type: 'committed_transcript', text: ''});     // empty: no transcript at all
  for (let i = 0; i < 7; i++) r.stt.audio(r.frame(1));                     // tail after the commit
  r.stt.close();                                                           // flushes the last partial chunk
  eq('13 all audio counted', r.stt.takeUsageS(), 0.34);
  eq('13 taken once', r.stt.takeUsageS(), 0);
});

// 14. fix round 2 (R2): an allocated Scribe socket that never reaches session_started is closed at the deadline
//     (8 s), reported, and retried through the one state machine after a growing backoff (no manual open); a
//     socket that closes before session_started is a start failure too, not an instant reconnect
block('14', () => {
  const r = rig();
  r.token('A'); r.stt.open(); r.token('B');                 // B = the prefetched next token
  r.stt.audio(r.frame(1));
  eq('14 allocated, not ready', [r.sockets.length, r.stt.status()], [1, 'connecting']);
  r.advance(7900);
  eq('14 before the deadline: still waiting', r.sockets[0].closed, false);
  r.advance(200);
  eq('14 deadline: closed, reported, backing off', [r.sockets[0].closed, r.server.filter(m => m.t === 'stt_error').map(m => m.code),
     r.stt.status()], [true, ['session_timeout'], 'backoff']);
  r.advance(1000);
  eq('14 no retry inside the backoff', r.sockets.length, 1);
  r.advance(1200);
  eq('14 retried after the backoff with the prefetched token', [r.sockets.length, /token=sutkn_B/.test(r.sockets[1] && r.sockets[1].url)], [2, true]);
  r.token('C');
  if (r.sockets[1]) r.sockets[1].onclose();                 // closed before session_started
  eq('14 close before start: backoff, no instant reconnect', [r.sockets.length, r.stt.status()], [2, 'backoff']);
  r.advance(3000);
  eq('14 second backoff is longer', r.sockets.length, 2);
  r.advance(1200);
  r.sockets[2] && r.sockets[2].emit({message_type: 'session_started'});
  eq('14 third socket starts: ready', [r.sockets.length, r.stt.status()], [3, 'ready']);
});

console.log(JSON.stringify({checks, failures}));
