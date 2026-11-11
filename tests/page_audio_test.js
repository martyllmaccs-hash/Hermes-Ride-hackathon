// Fix round 3: the page's audio settings on the real index.html scripts (page_rig).
// R3-2: cue/clip loudness is its own setting (hello.cfg.cue_gain), applied on the phone to the cue and clip audio
// only (decoded, scaled, re-encoded: iOS ignores HTMLMediaElement.volume), never to the reply voice.
'use strict';
const {loadPage, tick, checker} = require('./page_rig.js');
const T = checker(), {eq} = T;

function rig() {
  const blobs = {};                      // object URL -> Blob
  let n = 0;
  class Blob { constructor(parts, o) { this.parts = parts; this.type = (o && o.type) || ''; }
               async arrayBuffer() { return new ArrayBuffer(8); } }
  const URLs = Object.assign(function (u, b) { return new URL(u, b); },
    {createObjectURL: b => { const u = 'blob:' + (++n); blobs[u] = b; return u; }, revokeObjectURL() {}});
  const audioLog = [];
  const param = (name) => ({setValueAtTime: (v, t) => audioLog.push([name, 'set', v]),
                            exponentialRampToValueAtTime: (v, t) => audioLog.push([name, 'ramp', +v.toFixed(4)])});
  class AudioContext {
    constructor() { this.audioWorklet = {addModule: async () => {}}; this.state = 'running'; this.sampleRate = 48000;
                    this.currentTime = 0; this.destination = {name: 'dest'}; }
    createOscillator() { return {frequency: param('freq'), connect() {}, start() { audioLog.push(['osc', 'start']); }, stop() {}}; }
    createGain() { return {gain: param('gain'), connect() {}, disconnect() { audioLog.push(['bus', 'disconnect']); }}; }
    createMediaStreamSource() { return {connect() {}}; }
    resume() { return Promise.resolve(); }
    async decodeAudioData() {            // a 0.5 s stereo cue, quiet at both ends, full scale in the middle
      const n = 24000, mk = () => { const a = new Float32Array(n); a.set([1, -1, 0.5, 0], 12000); return a; };
      const ch = [mk(), mk()];
      return {numberOfChannels: 2, sampleRate: 48000, length: n, getChannelData: i => ch[i]};
    }
  }
  const P = loadPage({Blob, URL: URLs, AudioContext,
    fetch: async p => ({ok: true, clone() { return this; }, blob: async () => new Blob(['m4a:' + p], {type: 'audio/mp4'})})});
  P.blobs = blobs; P.audioLog = audioLog;
  return P;
}
function wavPeak(blob) {                 // largest |sample| of a 16-bit PCM WAV blob built by the page
  const part = blob.parts[0], bytes = part instanceof ArrayBuffer ? new Uint8Array(part) : new Uint8Array(part.buffer || part);
  const v = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let peak = 0;
  for (let i = 44; i + 1 < bytes.length; i += 2) peak = Math.max(peak, Math.abs(v.getInt16(i, true)));
  return {peak, channels: v.getUint16(22, true), rate: v.getUint32(24, true), riff: String.fromCharCode(...bytes.slice(0, 4))};
}

async function gainScenario(gain, label) {
  const P = rig();
  await P.els.b1.onclick();
  const page = P.pages()[0];
  page.readyState = 1; page.onopen();
  P.msg(page, {t: 'hello', epoch: 'e1', cfg: Object.assign({wake: {engine: 'transcript'}, play_buffer_ms: 0}, gain === undefined ? {} : {cue_gain: gain})});
  await tick(30);
  const url = p => P.run(`C.state() && cueUrls[${JSON.stringify(p)}]`);
  return {P, page, url, blob: p => P.blobs[url(p)]};
}

(async () => {
  {   // cue_gain 0.5: every cue and clip is a scaled WAV at half amplitude
    const {P, page, blob} = await gainScenario(0.5);
    for (const p of ['cues/done.m4a', 'cues/wake_a.m4a', 'clips/back.m4a']) {
      const b = blob(p);
      eq('R3-2 gain 0.5: ' + p + ' is a WAV', b && b.type, 'audio/wav');
      const w = b && b.type === 'audio/wav' ? wavPeak(b) : {};
      eq('R3-2 gain 0.5: ' + p + ' peak is half scale', w.peak !== undefined && Math.abs(w.peak - 16384) <= 2, true);
      eq('R3-2 gain 0.5: ' + p + ' header', [w.riff, w.rate], ['RIFF', 48000]);
    }
    // the reply voice is not touched: a reply segment still plays its own bytes at full level
    const seg = new Uint8Array([1, 0, 0, 0, 9, 0, 0, 0x49, 0x44, 0x33]).buffer;      // mp3 frame, turn 9, seq 0
    page.onmessage({data: seg});
    const src = P.audio.src;
    eq('R3-2 reply segment keeps its mime (not re-encoded)', P.blobs[src] && P.blobs[src].type, 'audio/mpeg');
    eq('R3-2 element volume untouched (iOS ignores it)', P.audio.volume === undefined || P.audio.volume === 1, true);
  }
  {   // cue_gain 1: the cached files themselves
    const {blob} = await gainScenario(1);
    eq('R3-2 gain 1: original cue blob', blob('cues/done.m4a') && blob('cues/done.m4a').type, 'audio/mp4');
  }
  {   // a server without the setting: the page's default is 0.5 too
    const {blob} = await gainScenario(undefined);
    const b = blob('cues/done.m4a');
    eq('R3-2 no cue_gain in hello: default 0.5', b && b.type === 'audio/wav' && Math.abs(wavPeak(b).peak - 16384) <= 2, true);
  }
  {   // R3-4/WP2: the thinking tone = Hermes's thinking blubs, G4 then E4, peak 0.16 x cue_gain; stopTone cuts it
    const {P} = await gainScenario(0.5);
    P.audioLog.length = 0;
    const has = P.run("typeof playTone === 'function' && typeof stopTone === 'function'");
    eq('R3-4 page has the tone', has, true);
    if (has) P.run('playTone(0.5)');
    const L = P.audioLog;
    eq('R3-4 two blubs', L.filter(x => x[0] === 'osc').length, 2);
    eq('R3-4 pitches G4, E4 gliding to x0.72', L.filter(x => x[0] === 'freq'),
       [['freq', 'set', 392], ['freq', 'ramp', 282.24], ['freq', 'set', 329.6], ['freq', 'ramp', 237.312]]);
    eq('R3-4 peak 0.08 at cue_gain 0.5 (Hermes level)', L.filter(x => x[0] === 'gain' && x[1] === 'ramp').map(x => x[2]),
       [0.08, 0.0001, 0.08, 0.0001]);
    if (has) P.run('stopTone()');
    eq('R3-4 stopTone disconnects at once', L.filter(x => x[0] === 'bus').length, 1);
    P.audioLog.length = 0;
    if (has) P.run('playTone(0)');
    eq('R3-4 gain 0: silent', P.audioLog.filter(x => x[0] === 'osc').length, 0);
  }
  {   // R3-5: Scribe partials reach the gate on both routes (phone: stt.onPartial; server: {t:'heard'} frames)
    const {P, page} = await gainScenario(0.5);
    P.run("var heardN = 0; gate.heard = () => { heardN++; }; gate.wake();");
    const scribe = P.sockets.find(s => !s.url.endsWith('/ws'));
    P.run("stt.open()");
    P.msg(page, {t: 'token', epoch: 'e1', token: 'sutkn_' + 'x'.repeat(28)});
    const sc = P.sockets.find(s => !s.url.endsWith('/ws'));
    if (sc) { sc.readyState = 1; if (sc.onopen) sc.onopen(); sc.onmessage({data: JSON.stringify({message_type: 'session_started'})});
              sc.onmessage({data: JSON.stringify({message_type: 'partial_transcript', text: 'so I was'})}); }
    eq('R3-5 phone route: a Scribe partial extends the window', P.run('heardN'), 1);
    P.msg(page, {t: 'heard', epoch: 'e1'});
    eq('R3-5 server route: a heard frame extends the window', P.run('heardN'), 2);
    void scribe;
  }
  T.done();
})().catch(T.crash);
