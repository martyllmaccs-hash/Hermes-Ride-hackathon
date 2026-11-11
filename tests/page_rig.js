// Shared rig for the page glue tests: loads static/index.html's scripts (the real files, in page order) into a fresh
// node vm context with stubbed browser APIs. No Worker -> sherpa can't load -> the transcript wake engine.
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm');
const S = path.join(__dirname, '..', 'static');

function loadPage(over) {
  const r = {sockets: [], els: {}, workletPort: null};
  class FakeWS {
    constructor(url) { this.url = url; this.sent = []; this.readyState = 0; this.bufferedAmount = 0; r.sockets.push(this); }
    send(d) { this.sent.push(typeof d === 'string' ? JSON.parse(d) : {binary: d.byteLength}); }
    close() { this.closed = true; }
  }
  const html = fs.readFileSync(path.join(S, 'index.html'), 'utf8');
  // Elements: tag name and attributes from the markup, a working classList over className, a parentElement (the body
  // unless nested in #setup), so the page's gesture code can see what was tapped (WP4) and the orb its classes (WP5).
  function el(id, tag, attrs) {
    const e = {id, tagName: (tag || 'div').toUpperCase(), attrs: attrs || {}, style: {}, textContent: '', className: '',
      disabled: false, children: [], parentElement: r.body,
      getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; },
      hasAttribute(n) { return n in this.attrs; },
      setAttribute(n, v) { this.attrs[n] = String(v); },
      remove() {
        this.removed = true;
        delete r.els[id];
        this.children.forEach(child => child.remove());
      }};
    // P4: custom properties (the orb's motion frame): kept in e.props, counted in e.propWrites
    e.props = {}; e.propWrites = 0;
    Object.defineProperty(e.style, 'setProperty', {enumerable: false, value(k, v) { e.props[k] = String(v); e.propWrites++; }});
    const set = () => new Set(String(e.className).split(/\s+/).filter(Boolean));
    e.classList = {
      contains: c => set().has(c),
      add(...cs) { const s = set(); cs.forEach(c => s.add(c)); e.className = [...s].join(' '); },
      remove(...cs) { const s = set(); cs.forEach(c => s.delete(c)); e.className = [...s].join(' '); },
      toggle(c, on) { const s = set(), want = on === undefined ? !s.has(c) : !!on; if (want) s.add(c); else s.delete(c);
                      e.className = [...s].join(' '); return want; },
    };
    return e;
  }
  r.body = {tagName: 'BODY', parentElement: null, getAttribute: () => null, hasAttribute: () => false};
  // WP6: the real nesting of the markup (a tag stack), so an element's parentElement is its nearest ancestor with an id
  // (else the body) and removing an element (tap 2 removes #setup) makes every id inside it undiscoverable too.
  const markup = html.replace(/<script[\s\S]*?<\/script>/g, '').replace(/<style[\s\S]*?<\/style>/g, '');
  const VOID = new Set(['meta', 'br', 'input', 'img', 'link', 'hr', 'source', 'wbr']);
  const stack = [];
  for (const m of markup.matchAll(/<(\/?)([a-zA-Z][\w-]*)([^>]*)>/g)) {
    const tag = m[2].toLowerCase();
    if (m[1]) { for (let i = stack.length - 1; i >= 0; i--) if (stack[i].tag === tag) { stack.length = i; break; } continue; }
    const attrs = {};
    for (const a of m[3].matchAll(/([\w-]+)(?:="([^"]*)")?/g)) attrs[a[1]] = a[2] === undefined ? '' : a[2];
    let e = null;
    if (attrs.id) {
      const up = [...stack].reverse().find(s => s.el);
      e = r.els[attrs.id] = el(attrs.id, tag, attrs);
      if (attrs.class) e.className = attrs.class;
      e.hidden = 'hidden' in attrs;
      if (up) { e.parentElement = up.el; up.el.children.push(e); }
    }
    if (!VOID.has(tag) && !/\/\s*$/.test(m[3])) stack.push({tag, el: e});
  }
  // document listeners (pointer events for the WP4 double tap, visibilitychange): r.fire(type, event) dispatches
  r.docL = {};
  r.fire = (type, ev) => (r.docL[type] || []).forEach(f => f(ev));
  r.audio = {src: '', l: {}, addEventListener(t, f) { (this.l[t] = this.l[t] || []).push(f); },
             fire(t) { (this.l[t] || []).forEach(f => f()); }, play() { return Promise.resolve(); }, pause() {}};
  const ctx = {
    console, Promise, Set, Map, Math, Date, JSON, Object, Array, String, Number, Uint8Array, Int16Array, Float32Array,
    ArrayBuffer, DataView, Error, encodeURIComponent, isNaN, Infinity,
    setTimeout, clearTimeout, setInterval, clearInterval,
    btoa: s => Buffer.from(s, 'binary').toString('base64'),
    location: {href: 'https://ride.test/'},
    URL: Object.assign(function (u, b) { return new URL(u, b); }, {createObjectURL: () => 'blob:' + Math.random(), revokeObjectURL() {}}),
    Blob: class { constructor(parts, o) { this.parts = parts; this.type = o && o.type; } },
    WebSocket: FakeWS,
    MediaMetadata: class {},
    fetch: async p => ({ok: true, clone() { return this; }, blob: async () => ({p})}),
    caches: {open: async () => ({match: async () => null, put: async () => {}})},
    navigator: {userAgent: 'node-page-test', mediaDevices: {getUserMedia: async () => ({getAudioTracks: () => [{label: 'fake mic', addEventListener() {}}]})},
                geolocation: {watchPosition() {}}},
    AudioContext: class { constructor() { this.audioWorklet = {addModule: async () => {}}; this.state = 'running'; }
                          createMediaStreamSource() { return {connect() {}}; } resume() { return Promise.resolve(); } },
    AudioWorkletNode: class { constructor() { this.port = {}; r.workletPort = this.port; } },
    addEventListener() {}, removeEventListener() {},
    document: {getElementById: id => id === 'a' ? r.audio : (r.els[id] || null), body: r.body,
               addEventListener(t, f) { (r.docL[t] = r.docL[t] || []).push(f); }},
  };
  Object.assign(ctx, over || {});
  ctx.window = ctx;
  vm.createContext(ctx);
  for (const src of [...html.matchAll(/<script src="([^"]+)"><\/script>/g)].map(m => m[1])) {
    vm.runInContext(fs.readFileSync(path.join(S, src), 'utf8'), ctx, {filename: src});
  }
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], ctx, {filename: 'index.html'});
  r.ctx = ctx;
  r.run = expr => vm.runInContext(expr, ctx);              // page-level bindings (gate, stt, C, ...)
  r.feed = (db, n) => { for (let i = 0; i < (n || 1); i++) r.workletPort.onmessage({data: frame(db)}); };
  r.msg = (ws, m) => ws.onmessage({data: JSON.stringify(m)});
  r.pages = () => r.sockets.filter(s => s.url.endsWith('/ws'));
  return r;
}
function frame(db) {
  const a = Math.sqrt(2) * 32768 * Math.pow(10, db / 20), f = new Int16Array(320);
  for (let i = 0; i < 320; i++) f[i] = Math.round(a * Math.sin(2 * Math.PI * 300 * i / 16000));
  return {pcm: f.buffer, db};
}
const tick = ms => new Promise(res => setTimeout(res, ms || 0));
function checker() {
  const c = {checks: 0, failures: []};
  c.eq = (name, got, want) => { c.checks++; if (JSON.stringify(got) !== JSON.stringify(want)) c.failures.push({name, got, want}); };
  c.done = () => { console.log(JSON.stringify({checks: c.checks, failures: c.failures})); process.exit(0); };
  c.crash = e => { c.failures.push({name: 'crash', got: String(e.stack || e)}); c.done(); };
  return c;
}
module.exports = {loadPage, frame, tick, checker};
