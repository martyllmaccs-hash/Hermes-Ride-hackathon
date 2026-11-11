"""WP8: the docs match the code. Every setting adapter.core_config reads is in README.md; this round's phone settings
are in plugin.yaml; no shipped file advertises a model setting for the Ride lane that no shipped code reads (Ride
follows the Hermes profile default, and a /model switch made in Conduit/Desktop does not reach it); the README's wiring
section says what is true today; shipped text carries no personal name."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    with open(os.path.join(ROOT, name)) as f:
        return f.read()


class Docs(unittest.TestCase):
    def test_every_core_config_setting_is_in_the_readme(self):
        src = read('adapter.py')
        body = src[src.index('def core_config'):src.index('class RideAdapter')]
        envs = sorted(set(re.findall(r"'(RIDE_[A-Z0-9_]+)'", body)))
        self.assertIn('RIDE_TONE_FIRST_S', envs)
        readme = read('README.md')
        self.assertEqual([e for e in envs if e not in readme], [])

    def test_round_5_settings_are_in_plugin_yaml(self):
        y = read('plugin.yaml')
        for env in ('RIDE_TONE_FIRST_S', 'RIDE_DOUBLE_TAP_MS', 'RIDE_ORB_DESIGN', 'RIDE_SHOW_WORD', 'RIDE_PLAY_BUFFER_MS',
                    'RIDE_ORB_MOTION'):
            self.assertIn(f'- name: {env}', y)
        self.assertNotIn('?k=', y)                          # key links are retired (AUD-R3-05)

    def test_no_model_setting_is_advertised(self):          # D6: no shipped code reads one
        y = read('plugin.yaml')
        block = re.search(r'^config_schema:\n((?:  .*\n)+)', y, re.M).group(1)
        for key in ('model', 'provider', 'reasoning'):
            self.assertIsNone(re.search(rf'^  {key}:\n', block, re.M), key)
        for name in ('plugin.yaml', 'README.md'):
            text = read(name)
            for env in ('RIDE_MODEL', 'RIDE_PROVIDER', 'RIDE_REASONING', '_apply_pin', 'model pin'):
                self.assertNotIn(env, text, name)
        self.assertIn('while the assistant thinks', y)

    def test_wiring_section_says_what_is_true_today(self):
        readme = read('README.md')
        sec = readme[readme.index('## Wiring your own voice and model'):]
        sec = sec[:sec.index('\n## ', 4)]
        for must in ('RIDE_TTS_ENGINE=hermes', 'ElevenLabs Scribe', 'ELEVENLABS_API_KEY', 'Whistle', 'Apple',
                     'untested', 'not shipped in this repository'):
            self.assertIn(must, sec)
        self.assertNotRegex(sec.lower(), r'uses whatever you already have')
        model = sec[sec.index('### 3. The language model'):]
        flat = ' '.join(model.split())
        self.assertIn("Ride follows the Hermes profile's default model and reasoning", flat)   # the default-follow rule
        self.assertIn('`/model`', flat)
        self.assertIn('Conduit', flat)
        self.assertIn('does not reach', flat)
        self.assertNotRegex(flat, r'(?i)\bpin\b|working tree')

    def test_no_personal_name_in_shipped_files(self):       # D6: comments, docstrings and README attributions
        name = 'Mar' + 'tin'                                # in pieces: this file is shipped too
        skip_dirs = {'.git', '__pycache__', os.path.join('static', 'vendor'), os.path.join('docs', 'evidence')}
        # not shipped (tools/export_public.py drops them)
        skip_files = {'HANDOFF.md', os.path.join('tests', 'live_phone.py')}
        hits = []
        for base, dirs, files in os.walk(ROOT):
            rel = os.path.relpath(base, ROOT)
            dirs[:] = [d for d in dirs if os.path.normpath(os.path.join(rel, d)) not in skip_dirs and d != '__pycache__']
            for f in files:
                path = os.path.normpath(os.path.join(rel, f))
                if path in skip_files or not f.endswith(('.py', '.js', '.html', '.yaml', '.md', '.txt', '.sh', '.json')):
                    continue
                with open(os.path.join(ROOT, path), encoding='utf-8', errors='replace') as fh:
                    hits += [f'{path}:{i}' for i, line in enumerate(fh, 1) if name in line]
        self.assertEqual(hits, [])


if __name__ == '__main__':
    unittest.main()
