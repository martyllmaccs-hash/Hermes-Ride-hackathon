"""Round 3, B3: ride character against the rider's own baseline. Synthetic ride logs only (never the rider's real
GPS positions: the logs under ~/rides/ are read-only evidence, not fixtures)."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ride_character as rc  # noqa: E402

T0 = 1794000000000          # a synthetic epoch ms


def ride(path, profile='calm', seconds=600, motion=True, speed=True, cal_ok=True, d_m=14.0):
    """One synthetic 1 Hz ride log. Speeds in km/h, lon_g in G (negative = braking), lean in degrees."""
    rows = []
    for i in range(seconds):
        r = {'t': T0 + i * 1000, 'acc': 6, 'd_m': d_m,
             'lat': 51.50 + i * 0.00012, 'lon': -0.12 + i * 0.00030}
        if speed:
            if profile == 'calm':
                r['spd'] = 38 + (i % 20) * 0.4
            elif profile == 'spirited':
                r['spd'] = 55 + (i % 20) * 0.8
            elif profile == 'stop-start':
                r['spd'] = 0.0 if (i % 60) < 25 else 28.0
            elif profile == 'congested':
                r['spd'] = 8 + (i % 7) * 0.5
            else:
                r['spd'] = 30.0
        if motion:
            if not cal_ok and i < 30:
                r['cal'] = {'fwd': False, 'sign': 0, 'n': i}
            else:
                r['cal'] = {'fwd': True, 'sign': 1, 'n': 60}
            if profile == 'spirited':
                r['lon_g'] = -0.35 if i % 30 == 0 else (0.3 if i % 30 == 15 else 0.0)
                r['lean'] = 28.0 if i % 10 < 3 else 6.0
            elif profile == 'calm':
                r['lon_g'] = -0.08 if i % 30 == 0 else 0.06
                r['lean'] = 9.0
            elif profile == 'congested':
                r['lon_g'] = -0.12 if i % 20 == 0 else 0.1
                r['lean'] = 14.0
            else:
                r['lon_g'] = -0.15 if i % 30 == 0 else 0.12
                r['lean'] = 12.0
        rows.append(r)
    with open(path, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')
    return path


class Features(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_calm_ride_numbers(self):
        f = rc.features(ride(os.path.join(self.tmp, 'c.jsonl'), 'calm', 300))
        self.assertEqual((f['duration_s'], f['records']), (299.0, 300))
        self.assertAlmostEqual(f['moving_s'], 300.0, places=1)
        self.assertEqual(f['distance_km'], 4.2)                      # 300 x 14 m
        self.assertEqual((f['stopped_share'], f['slow_share'], f['top_kmh']), (0.0, 0.0, 45.6))
        self.assertTrue(f['has_motion'] and f['has_gps'] and f['has_speed'])
        self.assertFalse(f['cal_learning'])
        self.assertAlmostEqual(f['brake_p95'], 0.08, places=3)
        self.assertAlmostEqual(f['accel_p95'], 0.06, places=3)
        self.assertAlmostEqual(f['stops_per_km'], 0.0, places=2)

    def test_stop_start_ride_counts_stops_and_standing(self):
        f = rc.features(ride(os.path.join(self.tmp, 's.jsonl'), 'stop-start', 600, d_m=6.0))
        self.assertGreater(f['stopped_share'], 0.35)
        self.assertEqual(f['stops_per_km'], round(10 / 3.6, 2))      # 600 s: 10 stops over 3.6 km
        self.assertLess(f['slow_share'], 0.01)

    def test_congested_ride_is_slow(self):
        f = rc.features(ride(os.path.join(self.tmp, 'g.jsonl'), 'congested', 600))
        self.assertGreater(f['slow_share'], 0.9)
        self.assertLess(f['top_kmh'], 15)

    def test_a_ride_without_motion_data_says_so(self):
        f = rc.features(ride(os.path.join(self.tmp, 'n.jsonl'), 'calm', 400, motion=False))
        self.assertFalse(f['has_motion'])
        self.assertIsNone(f['lean_share'])
        self.assertIsNone(f['brake_p95'])

    def test_motion_before_calibration_is_flagged(self):
        f = rc.features(ride(os.path.join(self.tmp, 'k.jsonl'), 'calm', 400, cal_ok=False))
        self.assertTrue(f['cal_learning'])

    def test_several_logs_of_one_session_are_joined(self):
        a = ride(os.path.join(self.tmp, 'a.jsonl'), 'calm', 200)
        b = ride(os.path.join(self.tmp, 'b.jsonl'), 'calm', 200)
        b_rows = [json.loads(x) for x in open(b)]
        for r in b_rows:                                             # the second page load continues in time
            r['t'] += 200 * 1000
        with open(b, 'w') as f:
            for r in b_rows:
                f.write(json.dumps(r) + '\n')
        one, both = rc.features(a), rc.features([a, b])
        self.assertEqual(both['records'], 400)
        self.assertAlmostEqual(both['moving_s'], 400.0, places=1)
        self.assertAlmostEqual(both['distance_km'], one['distance_km'] * 2, places=2)

    def test_the_phones_minute_summary_rows_are_not_ride_records(self):
        p = ride(os.path.join(self.tmp, 'm.jsonl'), 'calm', 60)
        with open(p, 'a') as f:
            f.write(json.dumps({'summary': True, 't': T0 + 59000, 'max_lean': 40.0, 'dist_m': 900}) + '\n')
        f = rc.features(p)
        self.assertEqual(f['records'], 60)
        self.assertAlmostEqual(f['distance_km'], 0.84, places=2)


class Character(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.base = []
        for i, prof in enumerate(('calm', 'calm', 'calm', 'calm', 'calm', 'stop-start')):
            self.base.append(ride(os.path.join(self.tmp, f'ride-2026110{i}-090000.jsonl'), prof, 420))

    def tearDown(self):
        self._tmp.cleanup()

    def test_spirited_against_a_calm_baseline(self):
        p = ride(os.path.join(self.tmp, 'now.jsonl'), 'spirited', 600)
        out = rc.ride_character([p], self.tmp)
        self.assertEqual(out['label'], 'spirited')
        self.assertEqual(out['baseline_rides'], 6)
        self.assertGreaterEqual(out['vs']['brake_p95']['z'], 1.0)
        self.assertEqual(len(out['vs']['brake_p95']), 3)             # value, baseline, z
        self.assertTrue(out['why'])
        self.assertEqual(out['caveats'], [])

    def test_calm_against_a_calm_baseline(self):
        p = ride(os.path.join(self.tmp, 'now.jsonl'), 'calm', 600)
        out = rc.ride_character([p], self.tmp)
        self.assertEqual(out['label'], 'calm')

    def test_stop_start_and_congested_against_the_baseline(self):
        p = ride(os.path.join(self.tmp, 'now.jsonl'), 'stop-start', 600)
        self.assertEqual(rc.ride_character([p], self.tmp)['label'], 'stop-start')
        p = ride(os.path.join(self.tmp, 'now2.jsonl'), 'congested', 600)
        self.assertEqual(rc.ride_character([p], self.tmp)['label'], 'congested filtering')

    def test_another_calm_ride_is_not_called_spirited(self):
        p = ride(os.path.join(self.tmp, 'now.jsonl'), 'calm', 700, d_m=15.0)
        out = rc.ride_character([p], self.tmp)
        self.assertIn(out['label'], ('calm', 'mixed'))

    def test_cold_start_below_five_baseline_rides_says_not_enough_history(self):
        p = ride(os.path.join(self.tmp, 'now.jsonl'), 'spirited', 600)
        cold = os.path.join(self.tmp, 'cold')
        os.makedirs(cold)
        ride(os.path.join(cold, 'old.jsonl'), 'calm', 420)
        out = rc.ride_character([p], cold)
        self.assertEqual(out['baseline_rides'], 1)
        self.assertTrue(any('not enough history' in c for c in out['caveats']))
        self.assertEqual(out['label'], 'spirited')                   # described on its own terms
        out = rc.ride_character([p], None)
        self.assertEqual((out['label'], out['baseline_rides']), ('spirited', 0))

    def test_a_short_ride_is_too_short(self):
        p = ride(os.path.join(self.tmp, 'now.jsonl'), 'spirited', 120)
        out = rc.ride_character([p], self.tmp)
        self.assertEqual((out['label'], out['caveats']), ('too short', ['too short']))

    def test_no_telemetry_at_all(self):
        out = rc.ride_character(['/no/such/ride.jsonl'], self.tmp)
        self.assertEqual((out['label'], out['caveats']), ('too short', ['no telemetry']))

    def test_missing_motion_is_a_caveat_not_a_guess(self):
        p = ride(os.path.join(self.tmp, 'now.jsonl'), 'calm', 600, motion=False)
        out = rc.ride_character([p], self.tmp)
        self.assertTrue(any('no motion data' in c for c in out['caveats']))
        self.assertEqual(out['label'], 'calm')
        self.assertNotIn('brake_p95', out['vs'])

    def test_calibration_still_learning_is_a_caveat(self):
        p = ride(os.path.join(self.tmp, 'now.jsonl'), 'calm', 600, cal_ok=False)
        out = rc.ride_character([p], self.tmp)
        self.assertTrue(any('calibrating' in c for c in out['caveats']))

    def test_the_baseline_excludes_this_rides_own_logs(self):
        p = os.path.join(self.tmp, 'ride-2026110X.jsonl')
        ride(p, 'spirited', 600)
        out = rc.ride_character([p], self.tmp)
        self.assertEqual(out['baseline_rides'], 6)                   # not 7: its own log is left out

    def test_short_baseline_rides_are_not_a_baseline(self):
        short = os.path.join(self.tmp, 'short')
        os.makedirs(short)
        for i in range(8):
            ride(os.path.join(short, f'r{i}.jsonl'), 'calm', 100)
        out = rc.ride_character([ride(os.path.join(self.tmp, 'now.jsonl'), 'calm', 600)], short)
        self.assertEqual(out['baseline_rides'], 0)

    def test_the_cli_prints_json_for_a_session(self):
        import subprocess
        p = ride(os.path.join(self.tmp, 'now.jsonl'), 'spirited', 600)
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'ride_character.py'), p,
                            '--rides-dir', self.tmp, '--json'], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out['label'], 'spirited')
        self.assertEqual(out['ride']['records'], 600)
        self.assertTrue(out['ride']['logs'][0].endswith('now.jsonl'))


class RealRidesDir(unittest.TestCase):
    """One run over the real rides folder (read-only): it must not crash, whatever is in it."""

    def test_the_real_rides_folder(self):
        import glob
        d = os.environ.get('RIDE_RIDES_DIR', '~/rides')
        files = sorted(glob.glob(os.path.join(d, '*.jsonl')))
        if not os.path.isdir(d):
            self.skipTest(f'no rides folder at {d}')
        if not files:
            self.skipTest(f'no ride logs in {d}')
        for p in files:
            f = rc.features(p)
            out = rc.character(f, rc.baseline(files, exclude=[p]))
            self.assertIn(out['label'], rc.LABELS)
            self.assertIsInstance(out['why'], list)
            self.assertIsInstance(out['caveats'], list)
        out = rc.ride_character(files[:1], d)
        self.assertIn(out['label'], rc.LABELS)
        self.assertLessEqual(out['baseline_rides'], 20)


if __name__ == '__main__':
    unittest.main()
