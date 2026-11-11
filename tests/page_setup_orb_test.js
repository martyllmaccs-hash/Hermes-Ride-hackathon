// WP7 (D2): the orb on the permission screen. The setup tap is a real <button> (#b1) drawn as the orb in the middle of
// the black screen. One tap, not two (motion permission + audio unlock inside the gesture, then mic, GPS, link,
// wake engine; #setup goes when motion is answered), and no instruction text (the pulsing orb says "tap me"); #note
// keeps only failures. The real index.html scripts in node (tests/page_rig.js).
// Prints {checks, failures}.
'use strict';
const fs = require('fs'), path = require('path');
const {loadPage, tick, checker} = require('./page_rig.js');
const T = checker(), {eq} = T;
const HTML = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const rule = sel => (HTML.match(new RegExp(sel.replace(/[.#]/g, '\\$&') + '\\{[^}]*\\}')) || [''])[0];
const inside = (P, id, anc) => { for (let n = P.els[id]; n; n = n.parentElement) if (n === P.els[anc]) return true; return false; };

(async () => {
  {   // 1. markup + look
    const P = loadPage({RIDE_SETUP_WAIT: {hello: 50}});
    eq('1 one setup tap: a real button drawn as the orb, no second orb', [P.els.b1.tagName, P.els.b1.classList.contains('sorb'), 'b2' in P.els],
       ['BUTTON', true, false]);
    eq('1 orb button and note are inside #setup (data-no-dbltap)', [['b1', 'note'].every(i => inside(P, i, 'setup')),
       P.els.setup.hasAttribute('data-no-dbltap')], [true, true]);
    eq('1 the orb button has an accessible name', /\w/.test(P.els.b1.getAttribute('aria-label') || ''), true);
    eq('1 at load: the orb shown and enabled', [P.els.b1.hidden, P.els.b1.disabled], [false, false]);
    eq('1 no instruction text on the setup screen (no #stext, empty note at load)', [/id="stext"/.test(HTML), P.els.note.textContent], [false, '']);
    const s = rule('.sorb');
    eq('1 the orb button is round, centred size min(60vw,60vh) (<= the running orb), no button chrome',
       [/border-radius:50%/.test(s), /width:min\(60vw,60vh\)/.test(s), /height:min\(60vw,60vh\)/.test(s), /border:0|border:none/.test(s), /padding:0/.test(s)],
       [true, true, true, true, true]);
    // Fix G (item 8): the holodeck blue (holodeck-release-sol shell.qml / HoloNode.qml) in both cores, the same gradient
    const G = 'radial-gradient(circle,#e8fdff 0%,#a8f7ff 14%,#7ff0ff 30%,rgba(47,124,140,.6) 48%,rgba(47,124,140,.2) 62%,rgba(0,0,0,0) 71%)';
    eq('1 G the same holodeck core as the running orb (d-simple), old blue gone', [rule('.sorb .core').includes(G), rule('.d-simple .core').includes(G),
       /#7ff0ff/.test(rule('.sorb .core')) && /#7ff0ff/.test(rule('.d-simple .core')), /#1d6cff|#0b2f7a|#04123a/.test(HTML)], [true, true, true, false]);
    eq('1 G listening ring #a8f7ff (was #3a9bff); no box-shadow or blur on the cores', [/border:3px solid #a8f7ff/.test(rule('.d-simple.s-listening .lvl')),
       /#3a9bff/.test(HTML), /box-shadow|blur\(/.test(rule('.d-simple .core') + rule('.sorb .core'))], [true, false, false]);
    eq('1 the orb waiting for a tap pulses (infinite), a disabled orb is dim and still',
       [/animation:[^;}]*infinite/.test(rule('.sorb:enabled .core')), /animation/.test(rule('.sorb .core')),
        /opacity:\.?[0-4]/.test(rule('.sorb:disabled .core')), /animation:none/.test(rule('.sorb:disabled .core'))], [true, false, true, true]);
    eq('1 hidden orbs are not drawn', /\.sorb\[hidden\]\{display:none\}/.test(HTML), true);
    // Fix C (item 1): one word "Tap" under the orb, 3x larger (60px), until the tap; the revive overlay uses the same style
    eq('C "Tap" under the orb at load, inside #setup, shown', [/<div id="tap">Tap<\/div>/.test(HTML), !!P.els.tap && inside(P, 'tap', 'setup'),
       !!P.els.tap && P.els.tap.classList.contains('gone')], [true, true, false]);
    const tr = (HTML.match(/#tap[^{]*\{[^}]*\}/) || [''])[0];
    eq('C the Tap rule: 60px, holodeck rim colour, fades', [/font-size:60px/.test(tr), /#a8f7ff/.test(tr), /transition:opacity/.test(tr), /\.tapw/.test(tr)],
       [true, true, true, true]);
    eq('C .gone fades it out', /#tap\.gone\{opacity:0\}/.test(HTML), true);
  }
  {   // 2. one tap on the orb runs the whole setup
    const P = loadPage({RIDE_SETUP_WAIT: {hello: 50}});
    const p1 = P.els.b1.onclick();
    eq('C the tap: "Tap" fades out at once', !!P.els.tap && P.els.tap.classList.contains('gone'), true);
    eq('2 while the tap runs, the orb is disabled (dim, still) and no "starting" text', [P.els.b1.disabled, P.els.note.textContent], [true, '']);
    const note = P.els.note;
    await p1;
    eq('2 the tap connected and logged the mic (diag only)', [P.pages().length, /mic: fake mic/.test(P.els.diag.textContent) && note.textContent === ''], [1, true]);
    const page = P.pages()[0];
    page.readyState = 1; page.onopen();
    P.msg(page, {t: 'hello', epoch: 'S1', cfg: {stt_route: 'phone', wake: {engine: 'transcript'}}});
    await tick(20);
    eq('2 after the one tap: #setup removed with the orb', P.run("$('setup') === null && $('b1') === null"), true);
    eq('2 the running orb takes over (sleep, static)', P.els.orb.className, 'orb d-simple s-sleep');
  }
  {   // 3. a failed tap 1 keeps orb 1 for another try; orb 2 never shows
    const P = loadPage({RIDE_SETUP_WAIT: {hello: 50, unlock: 50}});
    P.audio.play = () => new Promise(() => {});
    await P.els.b1.onclick();
    eq('C failed tap: "Tap" back', !!P.els.tap && !P.els.tap.classList.contains('gone'), true);
    eq('3 failed tap: the orb shown and enabled again, "tap 1 again", setup stays',
       [P.els.b1.hidden, P.els.b1.disabled, /tap 1 again/.test(P.els.note.textContent), P.run("$('setup') !== null")], [false, false, true, true]);
  }
  T.done();
})().catch(e => T.crash(e));
