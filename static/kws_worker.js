// sherpa-onnx keyword spotter (the configured wake word, default "HEY HERMES") in a Web Worker. Assets come from static/vendor/ (tools/fetch_assets.sh):
//   sherpa-onnx-wasm-kws-main.js/.wasm, sherpa-onnx-kws.js  (local checksum-pinned upstream source build)
//   kws-encoder.onnx, kws-decoder.onnx, kws-joiner.onnx, kws-tokens.txt  (zh-en 3M model, phone tokens)
//   kws-words-<wake word>.txt  (one per wake word, generated from the model's own lexicon by tools/make_keywords.py)
// The local build disables upstream demo preloading; verified model files are written into
// the WASM file system instead. rideKwsCreate/rideKwsFeed are also run under node by tests/kws_test.js.
'use strict';
const RIDE_KWS_FILES = ['kws-tokens.txt', 'kws-encoder.onnx', 'kws-decoder.onnx', 'kws-joiner.onnx'];

function rideKwsCreate(Module, createKws, files, keywords, threshold, score) {
  // files: {name: Uint8Array}; returns {feed(Float32Array) -> [keyword...], reset()}
  try { Module.FS_createPath('/', 'ride', true, true); } catch (e) {}
  for (const n of RIDE_KWS_FILES) {        // a second spotter in one runtime (tests: one per wake word) reuses the files
    try { Module.FS_createDataFile('/ride', n, files[n], true, false, false); }
    catch (e) { if (!(e && e.errno === 20)) throw e; }    // 20 = EEXIST in the emscripten FS
  }
  const kws = createKws(Module, {
    featConfig: {samplingRate: 16000, featureDim: 80},
    modelConfig: {transducer: {encoder: '/ride/kws-encoder.onnx', decoder: '/ride/kws-decoder.onnx',
                               joiner: '/ride/kws-joiner.onnx'},
                  tokens: '/ride/kws-tokens.txt', provider: 'cpu', modelType: '', numThreads: 1, debug: 0,
                  modelingUnit: '', bpeVocab: ''},
    maxActivePaths: 4, numTrailingBlanks: 1, keywordsScore: score || 1.0, keywordsThreshold: threshold || 0.25,
    keywords: '', keywordsBuf: String(keywords).trim(),
    keywordsBufSize: new TextEncoder().encode(String(keywords).trim()).length,
  });
  const stream = kws.createStream();       // one stream for the page's lifetime (this build can't free streams)
  return {
    feed(samples) {
      const hits = [];
      stream.acceptWaveform(16000, samples);
      while (kws.isReady(stream)) {
        kws.decode(stream);
        const r = kws.getResult(stream);
        if (r && r.keyword) { hits.push(r.keyword); kws.reset(stream); }
      }
      return hits;
    },
    reset() { kws.reset(stream); },
  };
}

function rideInt16ToFloat(pcm) {
  const f = new Float32Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) f[i] = pcm[i] / 32768;
  return f;
}

if (typeof importScripts === 'function' && typeof WorkerGlobalScope !== 'undefined') {
  let spotter = null, pending = [], frames = 0;
  const fail = e => postMessage({t: 'error', error: String(e && e.message || e)});
  self.onmessage = async ev => {
    const m = ev.data || {};
    if (m.t === 'init') {
      try {
        const base = new URL(m.base || 'vendor/', self.location.href).href;
        const get = async n => { const r = await fetch(base + n); if (!r.ok) throw new Error(n + ': HTTP ' + r.status);
                                 return new Uint8Array(await r.arrayBuffer()); };
        const files = {};
        for (const n of RIDE_KWS_FILES) files[n] = await get(n);
        // 10 Nov: the configured wake word's file (wake.js rideKwsName already checked the flat name; checked again)
        const kw = /^kws-words-[a-z0-9-]{1,40}\.txt$/.test(String(m.keywords || '')) ? m.keywords : 'kws-words-hey-hermes.txt';
        const keywords = new TextDecoder().decode(await get(kw));
        self.Module = {locateFile: p => base + p, getPreloadedPackage: () => new ArrayBuffer(0),
                       print: () => {}, printErr: () => {}};
        self.Module.onRuntimeInitialized = () => {
          try {
            spotter = rideKwsCreate(self.Module, self.createKws, files, keywords, m.threshold, m.score);
            postMessage({t: 'ready'});
            pending.forEach(p => spotter.feed(p)); pending = [];
          } catch (e) { fail(e); }
        };
        importScripts(base + 'sherpa-onnx-kws.js', base + 'sherpa-onnx-wasm-kws-main.js');
      } catch (e) { fail(e); }
    } else if (m.t === 'pcm') {
      if (++frames % 50 === 0) postMessage({t: 'alive'});   // liveness for the page (fix round 2, R7)
      const f = rideInt16ToFloat(new Int16Array(m.pcm));
      if (!spotter) { pending.push(f); if (pending.length > 50) pending.shift(); return; }
      try { for (const k of spotter.feed(f)) postMessage({t: 'hit', keyword: k, score: 1}); }
      catch (e) { spotter = null; fail(e); }          // AUD-V2-09: a runtime failure reaches the page (fallback)
    } else if (m.t === 'reset' && spotter) {
      try { spotter.reset(); } catch (e) { spotter = null; fail(e); }
    }
  };
}
if (typeof module === 'object' && module.exports) module.exports = {rideKwsCreate, rideInt16ToFloat, RIDE_KWS_FILES};
