import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ride_logic as ride  # noqa: E402


class NoiseFilter(unittest.TestCase):
    def test_drops(self):
        cases = {
            '': 'empty',
            '   ': 'empty',
            '(wind)': 'noise_tags_only',
            '[noise]': 'noise_tags_only',
            '[music] (wind noise)  [engine revving]': 'noise_tags_only',
            '...': 'empty',
            'Yeah.': 'too_short',
            '(wind) okay (wind)': 'too_short',
            '[noise] hmm [noise]': 'too_short',
        }
        for text, reason in cases.items():
            with self.subTest(text=text):
                self.assertEqual(ride.filter_transcript(text, 2), (None, reason))

    def test_keeps_and_cleans(self):
        self.assertEqual(ride.filter_transcript('How far to Bright?', 2), ('How far to Bright?', None))
        self.assertEqual(ride.filter_transcript('(wind) Turn left here [noise]', 2), ('Turn left here', None))
        self.assertEqual(ride.filter_transcript("I'm cold", 2), ("I'm cold", None))

    def test_min_words_config(self):
        self.assertEqual(ride.filter_transcript('three word phrase', 4), (None, 'too_short'))
        self.assertEqual(ride.filter_transcript('Stop', 1), ('Stop', None))


class BargeIn(unittest.TestCase):
    def test_rule(self):
        self.assertFalse(ride.should_barge_in('wait Lana', True))      # 2 words: v2 needs 3 (W6.4)
        self.assertTrue(ride.should_barge_in('wait Lana stop', True))
        self.assertTrue(ride.should_barge_in('hold on a second', True))
        self.assertFalse(ride.should_barge_in('yeah okay right', True))   # a backchannel, however long
        self.assertFalse(ride.should_barge_in('mm-hm yeah sure cool', True))
        self.assertTrue(ride.should_barge_in('yeah but wait', True))       # not only backchannel words
        self.assertFalse(ride.should_barge_in('hold on a second', True, started_during=False))  # began before
        self.assertFalse(ride.should_barge_in('wait', True))          # one word
        self.assertFalse(ride.should_barge_in('(wind) [noise]', True))  # tags are not words
        self.assertFalse(ride.should_barge_in('(wind) wait', True))
        self.assertFalse(ride.should_barge_in('hold on a second', False))  # Lana not speaking
        self.assertFalse(ride.should_barge_in('', True))


class EnglishTypography(unittest.TestCase):
    """Fix round 2 (R5): typographic apostrophes/hyphens count exactly like their ASCII forms (Scribe or a phone
    keyboard may produce either), so a contraction never inflates a word count."""
    PAIRS = [("don't stop", 'don\u2019t stop'), ("I'm fine", 'I\u2019m fine'), ("don't", 'don\u00b4t'),
             ("can't go", 'can\u02bct go'), ("won't", '\uff57\uff4f\uff4e\uff07\uff54'), ('mm-hm', 'mm\u2011hm')]

    def test_counts_match_ascii(self):
        for ascii_form, typo in self.PAIRS:
            self.assertEqual(ride.count_words(typo), ride.count_words(ascii_form), typo)

    def test_min_words_and_barge_in_match_ascii(self):
        for ascii_form, typo in self.PAIRS:
            self.assertEqual(ride.filter_transcript(typo, 2)[1], ride.filter_transcript(ascii_form, 2)[1], typo)
            self.assertEqual(ride.should_barge_in(typo, True, 3), ride.should_barge_in(ascii_form, True, 3), typo)
        self.assertFalse(ride.should_barge_in('don\u2019t stop', True, 3))      # two words, below three
        self.assertEqual(ride.filter_transcript('don\u2019t', 2), (None, 'too_short'))
        self.assertTrue(ride.is_backchannel('mm\u2011hm'))


class Queue(unittest.TestCase):
    def test_join(self):
        q = ride.TurnQueue()
        self.assertIsNone(q.take())
        q.add('first bit', 10.0, 12.0)
        q.add('and the second bit', 20.0, 22.0)
        self.assertEqual(len(q), 2)
        t = q.take()
        self.assertEqual(t['text'], 'first bit and the second bit')
        self.assertEqual(t['parts'], 2)
        self.assertEqual((t['t_end_speech'], t['t_final']), (20.0, 22.0))
        self.assertEqual(len(q), 0)
        self.assertIsNone(q.take())

    def test_drop_other_connections(self):
        q = ride.TurnQueue()
        q.add('old page words', 1.0, 2.0, epoch=1)
        q.add('new page words', 3.0, 4.0, epoch=2)
        self.assertTrue(q.has_epoch(2))
        self.assertEqual([i['text'] for i in q.drop_except(2)], ['old page words'])
        t = q.take()
        self.assertEqual((t['text'], t['parts'], t['epoch']), ('new page words', 1, 2))
        q.add('nobody attached', epoch=2)
        self.assertEqual(len(q.drop_except(None)), 1)   # no page attached: everything is stale
        self.assertEqual(len(q), 0)


class Sensors(unittest.TestCase):
    full = {'spd': 62.4, 'hdg': 44, 'lat': 51.507351, 'lon': -0.127758, 'acc': 15.2, 'lean': -12.3, 'peak': 3.14}
    none = 'Sensors: none (screen locked or no fix)'

    def test_full_line(self):
        self.assertEqual(
            ride.describe_sensors(self.full, {'gps': 2.2, 'mot': 0.4}),
            'Sensors (2 s old): 62 km/h, heading NE, 51.5074,-0.1278 ±15 m, lean 12° left, '
            'peak accel 3.1 m/s² in last 5 s')   # age shown = oldest component used

    def test_stale_or_missing(self):
        self.assertEqual(ride.describe_sensors(self.full, {'gps': 10.5, 'mot': 10.5}), self.none)
        self.assertEqual(ride.describe_sensors(None, None), self.none)
        self.assertEqual(ride.describe_sensors({}, {'gps': 1, 'mot': 1}), self.none)
        self.assertEqual(ride.describe_sensors(self.full, {'gps': None, 'mot': None}), self.none)

    def test_partial(self):
        self.assertEqual(ride.describe_sensors({'lean': 7.6, 'peak': 0.4}, {'gps': None, 'mot': 0.4}),
                         'Sensors (0 s old): lean 8° right, peak accel 0.4 m/s² in last 5 s')
        self.assertEqual(ride.describe_sensors({'lean': 0.2}, {'mot': 1}), 'Sensors (1 s old): lean 0°')

    def test_fresh_motion_stale_gps(self):
        s = dict(self.full, chdg=181)
        self.assertEqual(ride.describe_sensors(s, {'gps': 12.0, 'mot': 0.5}),
                         'Sensors (0 s old): heading S, lean 12° left, peak accel 3.1 m/s² in last 5 s')
        # GPS course wins over the compass while the fix is fresh
        self.assertEqual(ride.describe_sensors(s, {'gps': 1.0, 'mot': 0.5})[:42],
                         'Sensors (1 s old): 62 km/h, heading NE, 51')

    def test_source_ages(self):
        msg = dict(self.full, gps_age=3000, mot_age=200)
        a = ride.sensor_ages(msg, received_at=100.0, now=102.0)        # received 2 s ago
        self.assertAlmostEqual(a['gps'], 5.0)
        self.assertAlmostEqual(a['mot'], 2.2)
        self.assertEqual(ride.describe_sensors(msg, a)[:18], 'Sensors (5 s old):')   # source age, not receipt age
        # a cached fix resent seconds ago but sampled 9 s before send is past 10 s now
        stale = ride.sensor_ages({'lat': 1.0, 'lon': 2.0, 'gps_age': 9000}, 100.0, 101.5)
        self.assertEqual(ride.describe_sensors({'lat': 1.0, 'lon': 2.0, 'gps_age': 9000}, stale), self.none)
        # no age field -> component absent (no guessing); invalid ages too
        self.assertEqual(ride.sensor_ages(self.full, 100.0, 100.0), {'gps': None, 'mot': None})
        for bad in (-1, 'x', True, float('nan'), None):
            self.assertIsNone(ride.sensor_ages({'gps_age': bad}, 100.0, 100.0)['gps'], bad)
        self.assertEqual(ride.sensor_ages(None, None, 100.0), {'gps': None, 'mot': None})

    def test_compass(self):
        for deg, p in [(0, 'N'), (22, 'N'), (23, 'NE'), (90, 'E'), (180, 'S'), (270, 'W'), (338, 'N'), (-90, 'W')]:
            self.assertEqual(ride.compass_point(deg), p)

    def test_turn_text(self):
        t = ride.build_turn('How far to Bright?', self.full, {'gps': 2.0, 'mot': 2.0})
        self.assertEqual(t, (
            '[Ride voice mode: the rider is riding a motorcycle and hears your reply through the helmet. '
            'Sensors (2 s old): 62 km/h, heading NE, 51.5074,-0.1278 ±15 m, lean 12° left, '
            'peak accel 3.1 m/s² in last 5 s]\nHow far to Bright?'))
        t3 = ride.build_turn('hi there', None, None, rider_name='Alex')
        self.assertTrue(t3.startswith('[Ride voice mode: Alex is riding'))
        t2 = ride.build_turn('hi there', None, None, interrupted=True)
        self.assertIn('interrupted your previous reply', t2)
        self.assertIn('Sensors: none (screen locked or no fix)]', t2)


class SpeechText(unittest.TestCase):
    def test_strip_markdown(self):
        self.assertEqual(ride.strip_markdown('**Turn left** at the `servo`, see [map](https://x.y) \U0001F3CD\uFE0F'),
                         'Turn left at the servo, see map')
        self.assertEqual(ride.strip_markdown('# Plan\n- fuel\n- coffee\n1. go'), 'Plan fuel coffee go')
        self.assertEqual(ride.strip_markdown('Check https://example.com now'), 'Check a link now')

    def test_splitter(self):
        s = ride.SentenceSplitter()
        out = []
        for d in ['Hi', ' Alex', ',', ' lovely', ' to', ' hear', ' you', '.', ' Your', ' test', ' works', '.']:
            out += s.feed(d)
        self.assertEqual(out, ['Hi Alex, lovely to hear you.'])
        self.assertEqual(s.flush(), ['Your test works.'])

    def test_splitter_merges_short(self):
        s = ride.SentenceSplitter()
        self.assertEqual(s.feed('Hi. Okay. That is the way to go. Next'), ['Hi. Okay. That is the way to go.'])
        self.assertEqual(s.flush(), ['Next'])


class SensorJunk(unittest.TestCase):
    def test_non_numeric_values_ignored(self):
        s = {'spd': 'fast', 'hdg': None, 'lat': 51.5, 'lon': float('nan'), 'lean': True, 'peak': 2.0}
        self.assertEqual(ride.describe_sensors(s, {'gps': 1.0, 'mot': 1.0}),
                         'Sensors (1 s old): peak accel 2.0 m/s² in last 5 s')


class Wav(unittest.TestCase):
    def test_header(self):
        import struct
        w = ride.wav_bytes(b'\x01\x00' * 100, 24000)
        self.assertEqual(w[:4], b'RIFF')
        self.assertEqual(w[8:16], b'WAVEfmt ')
        self.assertEqual(struct.unpack('<I', w[24:28])[0], 24000)
        self.assertEqual(struct.unpack('<I', w[40:44])[0], 200)
        self.assertEqual(len(ride.wav_bytes(b'\x01\x00\x02', 24000)), 44 + 2)   # odd byte dropped

    def test_segmenter_cuts_on_gap_and_size(self):
        seg = ride.PcmSegmenter(24000, gap_s=0.15, max_s=1.0, min_s=0.1)
        half = b'\x00\x00' * 12000              # 0.5 s
        self.assertEqual(seg.add(half, 0.0), [])
        self.assertEqual(seg.poll(0.1), [])       # no pause yet
        out = seg.poll(0.2)                       # pause after a clause -> one segment
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]), 44 + len(half))
        out = seg.add(half, 1.0) + seg.add(half, 1.01)   # 1.0 s reached -> cut by size
        self.assertEqual(len(out), 1)
        self.assertEqual(seg.flush(), [])
        seg.add(b'\x00\x00' * 10, 2.0)
        self.assertEqual(seg.poll(3.0), [])       # too short to cut on a pause...
        self.assertEqual(len(seg.flush()), 1)     # ...but flushed at the end

    # Fix H (item 4): the pop while Lana speaks. A long clause streamed with no producer pause was cut hard at max_s
    # (4 s) at an arbitrary sample: measured (docs/evidence/pop_measure.md) 30 of 40 random-phase cuts ended on a
    # sample over 10 % of the segment peak, a step the <audio> element plays as a click. Now: the size cut goes to the
    # quietest 10 ms in the last 1 s (the rest is carried), every segment edge has a 2 ms linear fade, max_s 6 s.
    @staticmethod
    def _voiced(seconds, rate=24000, f0=137.0, dip_at=None):
        import math
        out = []
        for i in range(int(seconds * rate)):
            t = i / rate
            a = 9000 * (0.55 + 0.45 * math.sin(2 * math.pi * 4 * t + 0.3))
            if dip_at is not None and dip_at <= t < dip_at + 0.012:
                a = 40                                    # a 12 ms near-silence between two syllables
            out.append(int(a * sum(math.sin(2 * math.pi * f0 * h * t + 0.7 * h) / h for h in (1, 2, 3))))
        return out

    @staticmethod
    def _stream(samples, seg, chunk=480):
        import struct
        out, t = [], 0.0
        for i in range(0, len(samples), chunk):
            part = samples[i:i + chunk]
            out += seg.add(struct.pack('<%dh' % len(part), *part), t)
            t += 0.005                                    # faster than real time, never a pause
        out += seg.flush()
        return [list(struct.unpack('<%dh' % ((len(w) - 44) // 2), w[44:])) for w in out]

    def test_segmenter_size_cut_has_no_click(self):
        self.assertEqual(ride.PcmSegmenter(24000).max_bytes, 6 * 24000 * 2)       # max_s 6.0 (was 4.0)
        for f0 in (113.0, 137.0, 151.7, 188.3, 211.9):
            segs = self._stream(self._voiced(14.0, f0=f0), ride.PcmSegmenter(24000))
            self.assertGreaterEqual(len(segs), 3)
            self.assertEqual(sum(len(x) for x in segs), 14 * 24000)             # every sample kept, in order
            for x in segs:
                self.assertEqual((x[0], x[-1]), (0, 0))                       # faded to zero at both edges
                self.assertLessEqual(len(x), 6 * 24000 + 480)
            for x in segs[:-1]:
                self.assertGreater(len(x), 5 * 24000 - 480)                    # cut inside the last 1 s

    def test_segmenter_size_cut_finds_the_quiet_spot(self):
        segs = self._stream(self._voiced(9.0, dip_at=5.4), ride.PcmSegmenter(24000))
        cut = len(segs[0]) / 24000
        self.assertTrue(5.4 <= cut <= 5.42, cut)                                # in the dip, not at 6.0 s
        self.assertEqual(sum(len(x) for x in segs), 9 * 24000)

    def test_segmenter_gap_cut_is_faded_too(self):
        import struct
        seg = ride.PcmSegmenter(24000, gap_s=0.15, min_s=0.1)
        x = self._voiced(0.5)
        seg.add(struct.pack('<%dh' % len(x), *x), 0.0)
        w = seg.poll(0.2)[0]
        y = list(struct.unpack('<%dh' % ((len(w) - 44) // 2), w[44:]))
        self.assertEqual((len(y), y[0], y[-1]), (len(x), 0, 0))
        self.assertEqual(y[48:-48], x[48:-48])                                    # only the 2 ms edges change


class Sentences(unittest.TestCase):
    def test_whole_reply(self):
        self.assertEqual(ride.sentences('You are doing 62 km/h. Lean is 12 degrees left. Ok'),
                         ['You are doing 62 km/h.', 'Lean is 12 degrees left.', 'Ok'])


if __name__ == '__main__':
    unittest.main()
