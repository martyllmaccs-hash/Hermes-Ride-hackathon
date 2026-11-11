"""Shipped cue files (fix round 3). R3-3: violin only on the wake answers and the done cue; thinking and extended are
the voice alone (the violin cues start with a 1.8 s phrase before the voice, so their length tells them apart)."""
import hashlib
import os
import re
import struct
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CUES = os.path.join(ROOT, 'static', 'cues')


def m4a_seconds(path):
    """Duration from the mp4 movie header (mvhd), no ffprobe needed."""
    with open(path, 'rb') as f:
        data = f.read()
    i = data.find(b'mvhd')
    version = data[i + 4]
    if version == 1:
        scale, dur = struct.unpack('>IQ', data[i + 24:i + 36])
    else:
        scale, dur = struct.unpack('>II', data[i + 16:i + 24])
    return dur / scale


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


class Cues(unittest.TestCase):
    def test_thinking_has_no_violin_and_extended_is_gone(self):          # R3-3
        self.assertLess(m4a_seconds(os.path.join(CUES, 'thinking.m4a')), 1.2)
        self.assertFalse(os.path.exists(os.path.join(CUES, 'extended.m4a')))   # WP1: no extended cue any more

    def test_wake_and_done_keep_the_violin(self):                 # R3-3: unchanged files (the d137eec renders)
        self.assertEqual({n: sha(os.path.join(CUES, n + '.m4a')) for n in ('wake_a', 'wake_b', 'done')},
                         {'wake_a': '2f307edf6f88cb69', 'wake_b': '694d45279eacef4f', 'done': 'efb778803455295b'})
        for n in ('wake_a', 'wake_b', 'done'):
            self.assertGreater(m4a_seconds(os.path.join(CUES, n + '.m4a')), 1.7)

    def test_mute_cues_are_short_and_ours(self):                 # WP4: one short cue per double-tap toggle
        for n in ('mute', 'unmute'):
            self.assertLess(m4a_seconds(os.path.join(CUES, n + '.m4a')), 0.8)
        self.assertNotEqual(sha(os.path.join(CUES, 'mute.m4a')), sha(os.path.join(CUES, 'unmute.m4a')))
        with open(os.path.join(CUES, 'CREDITS.txt')) as f:
            credits = f.read()
        self.assertIn('mute.m4a', credits)
        self.assertIn('unmute.m4a', credits)

    def test_reset_cues_are_short_distinct_and_ours(self):       # WP6: the reset bar and its failure
        for n in ('reset', 'reset_failed'):
            self.assertLess(m4a_seconds(os.path.join(CUES, n + '.m4a')), 0.8)
        names = ('reset', 'reset_failed', 'mute', 'unmute')
        self.assertEqual(len({sha(os.path.join(CUES, n + '.m4a')) for n in names}), 4)
        with open(os.path.join(CUES, 'CREDITS.txt')) as f:
            credits = f.read()
        self.assertIn('reset.m4a, reset_failed.m4a', credits)
        self.assertIn('synthesised with ffmpeg only', credits.split('reset.m4a, reset_failed.m4a')[1])

    def test_phones_fetch_the_new_cues(self):                     # the page's Cache Storage name changes with them
        with open(os.path.join(ROOT, 'static', 'index.html')) as f:
            name = re.search(r"caches\.open\('([^']+)'\)", f.read()).group(1)
        self.assertNotIn(name, ('hermes-ride-cues-v3-quiet', 'hermes-ride-cues-v3'))


if __name__ == '__main__':
    unittest.main()
