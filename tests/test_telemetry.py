"""W7 server side: ride log (idempotent by t), the 'since last turn' block, /graph (auth required), and the turn's
context line built from uploaded telemetry."""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import KEY, FakeAgent, FakeElevenLabs, Phone, start_core, stop_core, tmpdir  # noqa: E402
import ride_logic as rl  # noqa: E402
import ride_telemetry as rt  # noqa: E402

T0 = 1_762_600_000_000


def track(n=300, start=T0, lat=-36.7271, lon=146.958, spd=72.0, lean=0.0, ev_at=None):
    """n seconds heading east at `spd` km/h (1 Hz records the phone would upload)."""
    recs = []
    for i in range(n):
        r = {'t': start + i * 1000, 'lat': round(lat, 6), 'lon': round(lon + i * spd / 3.6 / 89000, 6), 'acc': 5,
             'spd': spd, 'hdg': 90, 'lean': lean if i % 60 else 31.4 * (1 if (i // 60) % 2 else -1),
             'lon_g': 0.05, 'lat_g': 0.0, 'cal': {'fwd': True, 'sign': 1, 'n': 120}}
        if ev_at is not None and i == ev_at:
            r['ev'] = [{'t': r['t'], 'ev': 'braking', 'v': -0.72}]
            r['lon_g'] = -0.72
        recs.append(r)
    return recs


class Log(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log = rt.RideLog(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_append_is_idempotent_by_t(self):
        recs = track(90)
        self.assertEqual(self.log.append('r1', recs[:60]), (60, T0 + 59000))
        self.assertEqual(self.log.append('r1', recs[30:]), (30, T0 + 89000))     # re-sent overlap is skipped
        self.assertEqual(len(self.log.read('r1')), 90)
        again = rt.RideLog(self._tmp.name)                                       # a restarted server too
        self.assertEqual(again.append('r1', recs), (0, T0 + 89000))
        self.assertEqual([r['t'] for r in again.read('r1')], [r['t'] for r in recs])

    def test_bad_input_rejected(self):
        self.assertEqual(self.log.append('../etc', track(2)), (0, None))
        self.assertEqual(self.log.append('ok', 'not a list'), (0, None))
        n, _ = self.log.append('ok', [{'t': 'x'}, 5, None, {'t': T0, 'big': 'x' * 500, 'lat': 1.5, 'nested': {'a': 1}}])
        self.assertEqual(n, 1)
        rec = self.log.read('ok')[0]
        self.assertEqual(rec, {'t': T0, 'lat': 1.5, 'nested': {'a': 1}})
        self.assertIsNone(self.log.path('a/b'))

    def test_summary_and_rides(self):
        self.log.append('r1', track(3))
        self.assertTrue(self.log.append_summary('r1', {'t': T0 + 60000, 'la_mean': 1.2, 'events': []}))
        self.assertEqual(self.log.read('r1')[-1]['summary'], True)
        self.assertEqual(self.log.rides(), ['r1'])


class Since(unittest.TestCase):
    def test_block(self):
        recs = track(300, ev_at=200)
        s = rt.since_text(recs, T0 - 1, T0 + 299000)
        self.assertTrue(s.startswith('Since last turn (4 min): '), s)
        km = float(s.split('): ')[1].split(' km')[0])
        self.assertAlmostEqual(km, 72 / 3.6 * 299 / 1000, delta=0.3)              # ~6.0 km
        self.assertIn('avg 72 km/h, max 72 km/h', s)
        self.assertIn('max lean 31° ', s)
        self.assertIn('peak braking -0.7 G', s)
        self.assertIn('events: hard braking -0.7 G (1 min ago)', s)
        self.assertNotIn('learning', s)

    def test_only_records_since_the_last_turn(self):
        recs = track(300, ev_at=50)
        s = rt.since_text(recs, T0 + 120000, T0 + 299000)
        self.assertNotIn('braking -0.7', s)                     # the event was before the last turn
        self.assertTrue(s.startswith('Since last turn (2 min): '), s)
        self.assertIsNone(rt.since_text(recs, T0 + 400000, T0 + 500000))

    def test_learning_and_gps_jumps(self):
        recs = track(30)
        for r in recs:
            r['cal'] = {'fwd': False, 'sign': 0}
            r.pop('lean'), r.pop('lon_g')
        recs[10]['lat'] += 1.0                                   # a GPS jump of ~111 km in one second
        s = rt.since_text(recs, T0 - 1, T0 + 29000)
        self.assertIn('calibration still learning', s)
        self.assertLess(float(s.split('): ')[1].split(' ')[0]), 1000)   # metres, not 111 km


class Server(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tmpdir()
        self.tmp = self._tmp.name
        self.fake = await FakeElevenLabs().start()
        self.srv, self.core, self.agent = await start_core(self.tmp, self.fake, FakeAgent(),
                                                           eot_question_s=0, eot_default_s=0, eot_open_s=0)
        self.base = str(self.srv.make_url('')).rstrip('/')
        self.phone = await Phone(self.base, caps={'wake': 'sherpa', 'stt': 'phone'}).connect()

    async def asyncTearDown(self):
        await self.phone.close()
        await stop_core(self.srv, self.core)
        await self.fake.close()
        self._tmp.cleanup()

    async def test_batches_acked_and_stored_turn_gets_since_block(self):
        now = int(rl.now() * 1000)
        recs = track(240, start=now - 240000, ev_at=200)
        await self.phone.send({'t': 'telemetry', 'ride': 'ride-20261108-a', 'batch': recs[:120]})
        ack = await self.phone.wait({'t': 'telemetry_ack'})
        self.assertEqual((ack['ride'], ack['upto'], ack['n']), ('ride-20261108-a', recs[119]['t'], 120))
        n0 = len(self.phone.msgs)
        await self.phone.send({'t': 'telemetry', 'ride': 'ride-20261108-a', 'batch': recs})     # re-send: idempotent
        ack = await self.phone.wait({'t': 'telemetry_ack'}, start=n0)
        self.assertEqual((ack['n'], ack['upto']), (120, recs[-1]['t']))
        path = os.path.join(self.tmp, 'rides', 'ride-20261108-a.jsonl')
        with open(path) as f:
            self.assertEqual(len(f.readlines()), 240)
        await self.phone.send({'t': 'summary', 'ride': 'ride-20261108-a', 's': {'t': now, 'la_mean': 1.1}})
        await self.phone.send({'t': 'committed', 'text': 'how was that last stretch?'})
        await self.phone.wait({'t': 'audio', 'turn': 1, 'seq': 0})
        text = self.agent.turns[0][1]
        self.assertIn('Since last turn (4 min): ', text)
        self.assertIn('hard braking -0.7 G', text)
        self.assertIn('Full 1 Hz ride log (JSONL, read it with your tools only if asked about the route): ' + path,
                      text)
        await self.phone.send({'t': 'idle', 'turn': 1})
        await self.phone.send({'t': 'committed', 'text': 'and now?'})
        await self.phone.wait({'t': 'audio', 'turn': 2, 'seq': 0})
        self.assertNotIn('Since last turn', self.agent.turns[1][1].split('Full 1 Hz')[0].split('Sensors')[0])

    async def ask(self, n, text):
        await self.phone.send({'t': 'committed', 'text': text})
        await self.phone.wait({'t': 'audio', 'turn': n, 'seq': 0})
        await self.phone.send({'t': 'idle', 'turn': n})
        return self.agent.turns[n - 1][1]

    async def test_late_upload_is_not_lost(self):                     # AUD-V2-11
        # the event happens between uploads, a follow-up turn comes first, the delayed batch arrives, next turn
        now = int(rl.now() * 1000)
        recs = track(240, start=now - 240000, ev_at=200)
        await self.phone.send({'t': 'telemetry', 'ride': 'ride-20261108-b', 'batch': recs[:150]})
        await self.phone.wait({'t': 'telemetry_ack'})
        self.assertNotIn('hard braking', await self.ask(1, 'how is the road so far?'))
        self.assertNotIn('Since last turn', (await self.ask(2, 'and the next bit please?')).split('Full 1 Hz')[0])
        n0 = len(self.phone.msgs)
        await self.phone.send({'t': 'telemetry', 'ride': 'ride-20261108-b', 'batch': recs[150:]})
        await self.phone.wait({'t': 'telemetry_ack'}, start=n0)
        text = await self.ask(3, 'did I brake hard back there?')
        self.assertIn('hard braking -0.7 G', text)
        self.assertNotIn('hard braking', (await self.ask(4, 'anything else to report?')))   # delivered once

    async def test_new_ride_first_turn_covers_the_whole_ride(self):
        now = int(rl.now() * 1000)
        await self.phone.send({'t': 'telemetry', 'ride': 'ride-a', 'batch': track(60, start=now - 60000)})
        await self.phone.wait({'t': 'telemetry_ack'})
        await self.phone.send({'t': 'committed', 'text': 'first ride question here'})
        await self.phone.wait({'t': 'audio', 'turn': 1, 'seq': 0})
        await self.phone.send({'t': 'idle', 'turn': 1})
        n0 = len(self.phone.msgs)                               # a new ride (new page) with 5 min of telemetry
        await self.phone.send({'t': 'telemetry', 'ride': 'ride-b', 'batch': track(300, start=now - 300000)})
        await self.phone.wait({'t': 'telemetry_ack'}, start=n0)
        await self.phone.send({'t': 'committed', 'text': 'how far since I started?'})
        await self.phone.wait({'t': 'audio', 'turn': 2, 'seq': 0})
        self.assertIn('Since last turn (5 min): ', self.agent.turns[1][1])

    async def test_graph_requires_auth_and_renders(self):
        from aiohttp import ClientSession
        await self.phone.send({'t': 'telemetry', 'ride': 'g1', 'batch': track(120)})
        await self.phone.wait({'t': 'telemetry_ack'})
        async with ClientSession() as s:
            async with s.get(self.base + '/graph?ride=g1') as r:
                self.assertEqual(r.status, 403)
            async with s.get(self.base + '/graph?ride=g1', headers={'Cookie': 'rk=' + KEY}) as r:
                body = await r.text()
                self.assertEqual(r.status, 200)
                self.assertIn('<meta http-equiv="refresh" content="30">', body)
                self.assertEqual(body.count('<svg'), 4)            # speed, lean, longitudinal G, lateral G
                self.assertIn('Speed km/h', body)
                self.assertIn('120 records', body)
            async with s.get(self.base + '/graph', headers={'Cookie': 'rk=' + KEY}) as r:
                self.assertIn('Ride g1', await r.text())         # newest ride by default
            async with s.get(self.base + '/graph?ride=../x', headers={'Cookie': 'rk=' + KEY}) as r:
                self.assertEqual(r.status, 400)


if __name__ == '__main__':
    unittest.main()
