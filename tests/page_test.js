// Page glue smoke test: runs static/index.html's scripts (the real files, in page order) in node against stubbed
// browser APIs. No Worker -> sherpa can't load -> the transcript wake engine takes over (W5 fallback). Then:
// tap 1, server hello, loud frames -> gate probe -> need_token -> token -> Scribe socket -> "Hey, Lana. How fast
// am I going?" -> the page sends wake + the utterance without the wake word; a binary reply plays after the ready
// cue. Prints {checks, failures}.
'use strict';
const vm = require('vm');
const {loadPage, frame, tick, checker} = require('./page_rig.js');
const P = loadPage(), {sockets, els, audio, ctx} = P;
const T = checker(), {eq} = T;
const workletPort = {onmessage: d => P.workletPort.onmessage(d)};
async function main() {
  await els.b1.onclick();
  await tick(20);
  const page = sockets[0];
  eq('connects to /ws', page.url, 'wss://ride.test/ws');
  page.readyState = 1; page.onopen();
  page.onmessage({data: JSON.stringify({t: 'hello', v: 2, epoch: 'e1', cfg: {stt_route: 'phone', window_s: 30,
    vad: {base_db: -45, k: 0.15, cap_db: -25, hangover_ms: 600, preroll_ms: 800}, wake: {engine: 'sherpa', load_timeout_ms: 300,
      name: 'lana', alts: ['lana', 'lanna', 'lena', 'llana', 'alana']}, word: true}})});   // 10 Nov: a configured wake word
  await tick(400);                       // sherpa: no Worker here -> transcript fallback
  const hellos = page.sent.filter(m => m.t === 'hello');
  eq('hello v2 with caps', [hellos[0].v, hellos[0].ride.startsWith('ride-')], [2, true]);
  eq('fallback engine reported', hellos[hellos.length - 1].caps, {wake: 'transcript', stt: 'phone'});
  eq('wake-ready note', /^wake ready: transcript/m.test(els.diag.textContent), true);
  for (let i = 0; i < 50; i++) workletPort.onmessage({data: frame(-70)});      // silence: nothing happens
  eq('silence: no token request', page.sent.filter(m => m.t === 'need_token').length, 0);
  for (let i = 0; i < 40; i++) workletPort.onmessage({data: frame(-20)});      // speech -> probe opens STT
  eq('probe asks for a token', page.sent.filter(m => m.t === 'need_token').length, 1);
  eq('vad edge sent', page.sent.filter(m => m.t === 'vad')[0].s, 1);
  page.onmessage({data: JSON.stringify({t: 'token', epoch: 'e1', kind: 'realtime_scribe', token: 'sutkn_x', url: 'wss://scribe.test/rt',
                                        params: {model_id: 'scribe_v2_realtime'}})});
  const scribe = sockets[1];
  eq('scribe socket opened with the token', scribe && scribe.url, 'wss://scribe.test/rt?model_id=scribe_v2_realtime&token=sutkn_x');
  scribe.onmessage({data: JSON.stringify({message_type: 'session_started'})});
  eq('pre-roll + speech streamed', scribe.sent.filter(m => m.message_type === 'input_audio_chunk').length >= 10, true);
  scribe.onmessage({data: JSON.stringify({message_type: 'committed_transcript', text: 'Hey, Lana. How fast am I going?'})});
  const w = page.sent.filter(m => m.t === 'wake'), c = page.sent.filter(m => m.t === 'committed');
  eq('wake sent', w.length, 1);
  eq('the wake frame names the configured word', w[0] && w[0].w, 'lana');
  eq('utterance without the wake word', c.map(m => m.text), ['How fast am I going?']);
  eq('wake cue playing', audio.src.startsWith('blob:') || audio.src.startsWith('cues/'), true);
  eq('no PCM to the server on the phone route', page.sent.filter(m => m.binary).length, 0);
  // bystander speech without the wake word, in a later probe: dropped on the phone
  // reply: ready state then a binary frame -> queued behind the cues
  page.onmessage({data: JSON.stringify({t: 'state', s: 'thinking', turn: 1, epoch: 'e1'})});
  page.onmessage({data: JSON.stringify({t: 'state', s: 'ready', turn: 1, epoch: 'e1'})});
  const b = new ArrayBuffer(10), v = new DataView(b); v.setUint8(0, 1); v.setUint32(1, 1); v.setUint16(5, 0);
  page.onmessage({data: b});
  for (let i = 0; i < 4; i++) { audio.fire('playing'); audio.fire('ended'); }       // the cues ahead of the reply
  eq('hello without play_buffer_ms: the first reply segment is held (default 250 ms), not playing yet', page.sent.filter(m => m.t === 'play').length, 0);
  await tick(300);                                                                 // the real hold timer runs out, play() is called
  for (let i = 0; i < 4; i++) { audio.fire('playing'); audio.fire('ended'); }
  eq('play reported for the reply', page.sent.filter(m => m.t === 'play').length, 1);
  page.onmessage({data: JSON.stringify({t: 'end', turn: 1, segs: 1, epoch: 'e1'})});
  eq('idle after the reply', page.sent.filter(m => m.t === 'idle').length, 1);
  // link drop -> reconnect scheduled (1 s first)
  page.onclose({code: 1006});
  eq('display amber while down (WP5: the orb, was #bg #2a1a00)', els.orb.className, 'orb d-simple s-reconnecting');
  // AUD-V2-01: the conversation window is still open across the outage. After the reconnect the page must get a
  // fresh token and reopen Scribe (link up is not recognition up), and speech must reach the server again.
  const G = vm.runInContext('gate', ctx);
  eq('gate still open after the drop', [G.state(), G.isOpen()], ['window', true]);
  await tick(1100);
  const page2 = sockets.filter(s => s.url.endsWith('/ws'))[1];
  eq('reconnected', !!page2, true);
  if (page2) {
    page2.readyState = 1; page2.onopen();
    page2.onmessage({data: JSON.stringify({t: 'hello', v: 2, epoch: 'e2', cfg: {stt_route: 'phone', window_s: 30}})});
    eq('fresh token asked on the new link', page2.sent.filter(m => m.t === 'need_token').length, 1);
    page2.onmessage({data: JSON.stringify({t: 'token', epoch: 'e2', kind: 'realtime_scribe', token: 'sutkn_y', url: 'wss://scribe.test/rt', params: {}})});
    const scribe2 = sockets[sockets.length - 1];
    eq('scribe reopened with the new token', scribe2.url, 'wss://scribe.test/rt?token=sutkn_y');
    scribe2.onmessage({data: JSON.stringify({message_type: 'session_started'})});
    for (let i = 0; i < 10; i++) workletPort.onmessage({data: frame(-20)});
    eq('speech streams again', scribe2.sent.filter(m => m.message_type === 'input_audio_chunk').length >= 1, true);
    P.run("outbox.add({t: Date.now(), spd: 50})");          // AUD-V2-11: telemetry goes out before the utterance
    scribe2.onmessage({data: JSON.stringify({message_type: 'committed_transcript', text: 'And how far to Bright?'})});
    P.run('flushUsage()');                                  // AUD-V2-12: cumulative audio, not per transcript
    const u = page2.sent.filter(m => m.t === 'stt_usage');
    eq('stt usage reported independently of transcripts', u.length === 1 && u[0].s > 0, true);
    eq('committed carries no per-transcript seconds', page2.sent.filter(m => m.t === 'committed').every(m => m.stt_s === undefined), true);
    const order = page2.sent.map(m => m.t).filter(t => t === 'telemetry' || t === 'committed');
    eq('telemetry flushed before the committed text', order.slice(-2), ['telemetry', 'committed']);
    eq('utterance reaches the server after the reconnect', page2.sent.filter(m => m.t === 'committed').map(m => m.text), ['And how far to Bright?']);
  }
  T.done();
}
main().catch(e => T.crash(e));
