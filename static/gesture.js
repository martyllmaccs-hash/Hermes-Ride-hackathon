// WP4 (D4, P1; since 10 Nov a double tap wakes Lana; the mute is the MUTE button): a double tap anywhere on the page. Pure: index.html feeds it the document's pointer
// events ({pointerId, x, y, t, target}); tests/page_tapwake_test.js drives it through the page.
//   tap         one finger, down and up within tap_ms (500) without moving more than slop_px (12 CSS px), the lift
//               position included (a flick is not a tap)
//   double tap  a second tap whose down comes within interval_ms (hello.cfg.double_tap_ms, default 350) of the first
//               tap's up
// Not taps, and they also cancel a first tap: a second finger (multi-touch, until every finger is up again), a drag
// or swipe, a long press, a pointercancel (the browser took the touch), and a press on an interactive control
// (button, a, input, select, textarea, label) or inside any element marked data-no-dbltap (the setup panel; WP6
// puts the reset reveal there).
'use strict';
const RIDE_DBLTAP_DEFAULTS = {interval_ms: 350, tap_ms: 500, slop_px: 12};
const RIDE_NO_TAP_TAGS = ['BUTTON', 'A', 'INPUT', 'SELECT', 'TEXTAREA', 'LABEL'];

function rideDoubleTapMs(cfg) {          // hello.cfg.double_tap_ms -> ms, 150..1000, default 350
  const raw = cfg && cfg.double_tap_ms, v = raw == null || raw === '' ? NaN : Number(raw);
  return Number.isFinite(v) ? Math.max(150, Math.min(1000, v)) : RIDE_DBLTAP_DEFAULTS.interval_ms;
}
function rideNoDoubleTap(node) {         // the target or an ancestor is a control or carries data-no-dbltap
  for (let n = node, i = 0; n && i < 64; n = n.parentElement || n.parentNode, i++) {
    if (n.tagName && RIDE_NO_TAP_TAGS.includes(String(n.tagName).toUpperCase())) return true;
    if (typeof n.hasAttribute === 'function' && n.hasAttribute('data-no-dbltap')) return true;
  }
  return false;
}

function RideDoubleTap(opts, onDouble) {
  const o = Object.assign({}, RIDE_DBLTAP_DEFAULTS, opts || {});
  const down = new Map();                // pointerId -> {x, y, t, ok}
  let multi = false, first = null;       // multi: a second finger came down; first: the first tap's up time
  const moved = (d, e) => Math.hypot(e.x - d.x, e.y - d.y) > o.slop_px;
  function pointerdown(e) {
    if (down.size) { multi = true; first = null; }
    const ok = !rideNoDoubleTap(e.target);
    down.set(e.pointerId, {x: e.x, y: e.y, t: e.t, ok});
    if (!ok) first = null;
  }
  function pointermove(e) { const d = down.get(e.pointerId); if (d && moved(d, e)) d.ok = false; }
  function pointerup(e) {
    const d = down.get(e.pointerId);
    if (!d) return;
    down.delete(e.pointerId);
    const wasMulti = multi;
    if (!down.size) multi = false;
    if (wasMulti || !d.ok || moved(d, e) || e.t - d.t > o.tap_ms) { first = null; return; }
    if (first !== null && d.t - first <= o.interval_ms) { first = null; onDouble(); return; }
    first = e.t;
  }
  function pointercancel(e) { down.delete(e.pointerId); if (!down.size) multi = false; first = null; }
  function configure(x) { for (const [k, v] of Object.entries(x || {})) if (v != null) o[k] = v; }
  return {pointerdown, pointermove, pointerup, pointercancel, configure, opts: () => Object.assign({}, o)};
}
if (typeof module === 'object' && module.exports) module.exports = {RideDoubleTap, rideDoubleTapMs, rideNoDoubleTap, RIDE_DBLTAP_DEFAULTS};
