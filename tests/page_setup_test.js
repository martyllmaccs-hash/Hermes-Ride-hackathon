// Tap-1 setup bounds (fix round 2, R1 / AUD-V2-10, -04): the real index.html scripts in node (tests/page_rig.js).
// A pending audio unlock or AudioContext resume never stalls setup before connecting; a failed attempt leaves a
// visible "tap 1 again" and releases what it acquired, so the retry ends with one live mic stream and one context.
// Only page-level observables are used (note, button, sockets, fake streams/contexts). Prints {checks, failures}.
'use strict';
const {loadPage, tick, checker} = require('./page_rig.js');
const T = checker(), {eq} = T;

const within = (p, ms) => Promise.race([p, tick(ms).then(() => 'still waiting')]);
const hang = () => new Promise(() => {});
const WAIT = {cache: 50, idb: 50, worklet: 50, mic: 50, hello: 50, unlock: 50, resume: 50};

function media(opts) {                   // fake getUserMedia + AudioContext that record what is still alive
  const m = {streams: [], contexts: []};
  m.navigator = {userAgent: 'node-page-test', geolocation: {watchPosition() {}}, mediaDevices: {getUserMedia: async () => {
    const tr = {label: 'fake mic', live: true, addEventListener() {}, stop() { this.live = false; }};
    const st = {tracks: [tr], getAudioTracks() { return this.tracks; }, getTracks() { return this.tracks; }};
    m.streams.push(st); return st;
  }}};
  m.AudioContext = class {
    constructor() { this.state = 'running'; this.closed = false; m.contexts.push(this);
                    this.audioWorklet = {addModule: () => (opts.worklet ? opts.worklet() : Promise.resolve())}; }
    createMediaStreamSource() { return {connect() {}}; }
    resume() { return opts.resume ? opts.resume() : Promise.resolve(); }
    close() { this.closed = true; return Promise.resolve(); }
  };
  m.liveStreams = () => m.streams.filter(s => s.tracks.some(t => t.live)).length;
  m.liveContexts = () => m.contexts.filter(c => !c.closed).length;
  return m;
}

async function main() {
  // 1. an unlock play() that never settles: setup returns with tap 1 again, nothing connects; the retry connects
  const P = loadPage({RIDE_SETUP_WAIT: WAIT});
  const play = P.audio.play;
  P.audio.play = hang;
  eq('pending unlock: setup returns', await within(P.els.b1.onclick(), 500), undefined);
  eq('pending unlock: tap 1 again, no link yet', [P.els.b1.disabled, /tap 1 again/.test(P.els.note.textContent), P.pages().length],
     [false, true, 0]);
  P.audio.play = play;
  await within(P.els.b1.onclick(), 500);
  eq('unlock retry: connects', P.pages().length, 1);

  // 2. AudioContext.resume() that never settles: bounded, visible, resources released; the retry leaves one of each
  let resumeHangs = true;
  const M = media({resume: () => (resumeHangs ? hang() : Promise.resolve())});
  const Q = loadPage({RIDE_SETUP_WAIT: WAIT, navigator: M.navigator, AudioContext: M.AudioContext});
  eq('pending resume: setup returns', await within(Q.els.b1.onclick(), 500), undefined);
  eq('pending resume: mic failed, tap 1 again, no link', [/mic failed/.test(Q.els.note.textContent), /tap 1 again/.test(Q.els.note.textContent),
     Q.els.b1.disabled, Q.pages().length], [true, true, false, 0]);
  eq('pending resume: stream and context released', [M.liveStreams(), M.liveContexts()], [0, 0]);
  resumeHangs = false;
  await within(Q.els.b1.onclick(), 500);
  eq('resume retry: connects with one live stream and one context', [Q.pages().length, M.liveStreams(), M.liveContexts()], [1, 1, 1]);

  // 3. worklet module timeout, then a retry: the failed attempt's stream and context do not stay alive
  let workletHangs = true;
  const W = media({worklet: () => (workletHangs ? hang() : Promise.resolve())});
  const R = loadPage({RIDE_SETUP_WAIT: WAIT, navigator: W.navigator, AudioContext: W.AudioContext});
  await within(R.els.b1.onclick(), 500);
  eq('worklet timeout: stream and context released', [W.liveStreams(), W.liveContexts(), R.els.b1.disabled], [0, 0, false]);
  workletHangs = false;
  await within(R.els.b1.onclick(), 500);
  eq('worklet retry: one live stream and one context', [R.pages().length, W.liveStreams(), W.liveContexts()], [1, 1, 1]);
  T.done();
}
main().catch(e => T.crash(e));
