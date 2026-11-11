// WP6 (D5, P6): the reset gesture. Pure: index.html feeds it the document's pointer events ({pointerId, x, y, t,
// target}); tests/page_reset_test.js drives it through the page.
//   reveal  an upward drag of at least reveal_px (40 CSS px) that starts on the grabber (io.isGrab) opens the panel
//           (io.show(true)): the long red bar and the MUTE button. Left alone for hide_ms (10 s) it closes again; a
//           downward drag of at least reveal_px anywhere while it is open closes it at once (and cancels a hold).
//   hold    a finger on the bar (io.isBar) while the panel is open starts one hold_ms (3 s) timer and io.fill(true)
//           (the CSS fill runs); a release, a pointercancel, a second finger or moving more than slop_px (24 CSS px,
//           a gloved thumb wobbles) cancels it (io.fill(false)). When the timer fires: io.fire(), once. Further holds
//           do nothing until rearm() (the page calls it when the server says the reset failed).
// No timer runs while nothing is held or revealed; no animation frame ever (the fill is a CSS transition).
'use strict';
const RIDE_RESET_DEFAULTS = {hold_ms: 3000, reload_ms: 5000, hide_ms: 10000, reveal_px: 40, slop_px: 24};

function RideReset(opts, io) {
  const o = Object.assign({}, RIDE_RESET_DEFAULTS, opts || {});
  const timer = io.timer || ((f, ms) => setTimeout(f, ms)), clearTimer = io.clearTimer || (id => clearTimeout(id));
  let open = false, fired = false, grab = null, hold = null, holdT = null, hideT = null, down = null;
  const fingers = new Set();
  function armHide() { if (hideT !== null) clearTimer(hideT); hideT = timer(() => { hideT = null; if (!hold) show(false); }, o.hide_ms); }
  function show(on) {
    open = !!on;
    if (!open && hideT !== null) { clearTimer(hideT); hideT = null; }
    io.show(open);
    if (open) armHide();
  }
  function cancelHold() {
    if (!hold) return;
    hold = null;
    if (holdT !== null) { clearTimer(holdT); holdT = null; }
    io.fill(false);
    if (open) armHide();
  }
  function pointerdown(e) {
    fingers.add(e.pointerId);
    if (fingers.size > 1) { cancelHold(); grab = null; down = null; return; }
    if (open) down = {id: e.pointerId, y: e.y};
    if (!open && io.isGrab(e.target)) { grab = {id: e.pointerId, x: e.x, y: e.y}; return; }
    if (open && !fired && io.isBar(e.target)) {
      hold = {id: e.pointerId, x: e.x, y: e.y};
      if (hideT !== null) { clearTimer(hideT); hideT = null; }
      io.fill(true);
      holdT = timer(() => { holdT = null; hold = null; fired = true; io.fill(false); io.fire(); }, o.hold_ms);
    }
  }
  function pointermove(e) {
    if (grab && grab.id === e.pointerId && grab.y - e.y >= o.reveal_px) { grab = null; show(true); }
    if (hold && hold.id === e.pointerId && Math.hypot(e.x - hold.x, e.y - hold.y) > o.slop_px) cancelHold();
    if (open && down && down.id === e.pointerId && e.y - down.y >= o.reveal_px) { down = null; cancelHold(); show(false); }
  }
  function pointerup(e) {
    fingers.delete(e.pointerId);
    if (grab && grab.id === e.pointerId) grab = null;
    if (down && down.id === e.pointerId) down = null;
    if (hold && hold.id === e.pointerId) cancelHold();
  }
  function pointercancel(e) { pointerup(e); }
  function rearm() { fired = false; if (open) armHide(); }
  return {pointerdown, pointermove, pointerup, pointercancel, rearm, show,
          state: () => ({open, fired, holding: !!hold, timers: (holdT !== null) + (hideT !== null)}), opts: () => Object.assign({}, o)};
}
if (typeof module === 'object' && module.exports) module.exports = {RideReset, RIDE_RESET_DEFAULTS};
