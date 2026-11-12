"""Runs the page-side node tests (tests/*_test.js) under node, when node is installed. Each prints one JSON line
{checks, failures, ...}. The wake test also runs the real sherpa-onnx WASM spotter when static/vendor/ is fetched."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'tools'))


def run_node(name, timeout=120):
    out = subprocess.run(['node', os.path.join(HERE, name)], capture_output=True, text=True, timeout=timeout)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
class Page(unittest.TestCase):
    def check(self, name, min_checks):
        res = run_node(name)
        self.assertEqual(res['failures'], [])
        self.assertGreaterEqual(res['checks'], min_checks)
        return res

    def test_client_state_machine(self):          # W2: cues, clips, colours, audio queue, epochs
        self.check('client_state_test.js', 151)

    def test_gate(self):                          # W3: VAD gate on synthetic PCM
        self.check('gate_test.js', 82)

    def test_wake(self):                          # W5: regex, refractory, fallback, real spotter
        res = self.check('wake_test.js', 30)
        kws = res.get('kws') or {}
        if os.path.isdir(os.path.join(ROOT, 'static', 'vendor')):
            self.assertNotIn('skipped', kws)
            self.assertEqual(kws['results']['speech_clean.wav']['hits'][0][0], 'HEY_LANA')
            self.assertEqual(kws['hermes']['hermes_clean.wav'][0][0], 'HEY_HERMES')   # 10 Nov: the public default

    def test_stt_client(self):                    # W4: phone Scribe client against a fake Scribe socket
        self.check('stt_test.js', 49)

    def test_telemetry(self):                     # W7: calibration on synthetic motion
        self.check('telemetry_test.js', 23)

    def test_page_glue(self):                     # index.html scripts in node: fallback, probe, token, wake, reply
        self.check('page_test.js', 27)

    def test_page_startup_after_motion(self):     # tap 2 removes setup before asynchronous wake completion
        self.check('page_startup_test.js', 28)

    def test_page_wake_engine(self):              # index.html with a fake sherpa Worker: runtime fallback, cfg
        self.check('page_wake_test.js', 19)

    def test_page_server_route(self):             # index.html on stt_route=server: PCM backlog, wake handshake
        self.check('page_server_test.js', 15)

    def test_page_setup_bounds(self):             # index.html tap 1: pending unlock/resume, failed-attempt cleanup
        self.check('page_setup_test.js', 9)

    def test_page_audio_settings(self):           # fix round 3: cue_gain, tone, window, mic indicator on index.html
        self.check('page_audio_test.js', 21)

    def test_page_privacy(self):                  # fix round 3, R3-6: mic indicator, stop + confirm, Media Session
        self.check('page_privacy_test.js', 16)

    def test_page_webkit(self):                   # fix round 3, R3-7: motion permission in the gesture, prefixed
        self.check('page_webkit_test.js', 9)      # AudioContext, no AudioWorklet / Cache Storage / Wake Lock

    def test_page_stop_is_terminal(self):         # fix round 4, AUD-R3-01/02: no recovery or wake reopens after Stop
        self.check('page_stop_test.js', 28)

    def test_page_mute(self):                     # round 3, A2: one-tap MUTE / UNMUTE replaces the stop button
        self.check('page_mute_test.js', 64)

    def test_page_tap_to_wake(self):              # 10 Nov: double tap wakes like "Hey Lana"; no mute; #stop as big as #resetbar
        self.check('page_tapwake_test.js', 38)

    def test_page_orb(self):                      # WP5: orb states, static sleep, 20 Hz level, thinking beats, layout
        res = self.check('page_orb_test.js', 100)
        for state, b in res['battery'].items():
            self.assertEqual((b['js_timers_per_s'], b['animation_frames_per_s']), (0, 0), state)
            self.assertLessEqual(b['style_ticks_per_s'], 20, state)      # P4: level ring + motion frame share the grid
        self.assertLessEqual(res['battery']['listening']['level_writes_per_s'], 20)
        self.assertEqual(res['battery']['sleep']['motion_frames_per_s'], 0)

    def test_orb_motion(self):                    # P4: lean from gyro + turn rate (not accel roll), brake, bump, gain
        self.check('orb_motion_test.js', 77)

    def test_page_reset(self):                    # WP6: grabber reveal, 3 s hold sends once, reload, #stop inside
        self.check('page_reset_test.js', 56)

    def test_page_setup_orb(self):                # WP7: the two setup taps are the orb, text beneath, flow unchanged
        self.check('page_setup_orb_test.js', 20)

    def test_page_recognition_recovery(self):     # index.html phone route: session-start deadline, truthful back
        self.check('page_recovery_test.js', 20)

    def test_page_cue_hold(self):                 # Fix A: our own cues/clips reach neither Scribe nor the spotter
        self.check('page_cue_hold_test.js', 11)

    def test_page_background(self):               # Fix B: mic back after the home screen; Tap overlay as the fallback
        self.check('page_background_test.js', 48)


class Keywords(unittest.TestCase):
    def test_keywords_from_lexicon(self):
        import make_keywords as mk
        lex = {'HEY': [['HH', 'EY1']], 'LANA': [['L', 'AE1', 'N', 'AH0']]}
        toks = {'HH', 'EY1', 'L', 'AE1', 'AA1', 'N', 'AH0'}
        self.assertEqual(mk.keyword_lines(lex, toks), [
            'HH EY1 L AE1 N AH0 @HEY_LANA', 'HH EY1 L AA1 N AH0 @HEY_LANA',
            'L AE1 N AH0 #0.35 @LANA', 'L AA1 N AH0 #0.35 @LANA'])
        with self.assertRaises(SystemExit):
            mk.keyword_lines(lex, toks - {'AH0'})       # a token the model lacks is an error, not a silent miss

    def test_lexicon_parsing(self):
        import make_keywords as mk
        with tempfile.NamedTemporaryFile('w', suffix='.phone', delete=False) as f:
            f.write('HEY HH EY1\nLANA L AE1 N AH0\nLANA(2) L AA1 N AH0\nOTHER AA1\n')
        try:
            self.assertEqual(mk.load_lexicon(f.name, {'HEY', 'LANA'}),
                             {'HEY': [['HH', 'EY1']], 'LANA': [['L', 'AE1', 'N', 'AH0'], ['L', 'AA1', 'N', 'AH0']]})
        finally:
            os.unlink(f.name)

    def test_fetch_script_pins_everything(self):
        with open(os.path.join(ROOT, 'tools', 'fetch_assets.sh')) as f:
            s = f.read()
        self.assertEqual(s.count('get "'), 1)                 # official model archive only
        self.assertEqual(s.count('check "$M/'), 5)            # every extracted model file
        self.assertNotIn('huggingface.co', s)
        self.assertNotIn('SHERPA_SKIP_RUNTIME', s)
        self.assertIn('build_wake_runtime.sh', s)
        with open(os.path.join(ROOT, 'tools', 'build_wake_runtime.sh')) as f:
            build = f.read()
        self.assertIn('bec3acea719faafa6c86c7022d98f19663b82a76', build)
        self.assertIn('64b9fd63efe0501a577bd6760c23b7892806115ab31a9051b68da1c63ec5da56', build)
        self.assertIn('https://codeload.github.com/k2-fsa/sherpa-onnx/', build)
        self.assertIn('4.0.23', build)
        with open(os.path.join(ROOT, '.gitignore')) as f:
            self.assertIn('static/vendor/', f.read())


if __name__ == '__main__':
    unittest.main()
