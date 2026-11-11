"""AUD-V2-08: a key or token never reaches a sink (logger, turns.jsonl, ride logs, live-driver dumps), whichever
string field carries it. Synthetic sentinels only; no real credential is used."""
import asyncio
import json
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import FakeAgent, FakeElevenLabs, Phone, start_core, stop_core, tmpdir  # noqa: E402
import ride_logic as rl  # noqa: E402
import ride_telemetry as rt  # noqa: E402

TOKEN = 'sutkn_' + 'Ab9' * 9 + 'Qz'                 # the 34-char single-use token shape
KEY = 'sk_' + 'x1Y2' * 8                            # an API-key shape
PIECES = ('Ab9Ab9Ab9', 'x1Y2x1Y2')                  # no fragment survives truncation either


def leaks(text):
    return [p for p in PIECES if p in text]


class Sanitize(unittest.TestCase):
    def test_recursive(self):
        rec = {'a': TOKEN, 'n': [1, {'deep': ['x ' + KEY]}], TOKEN: 2, 't': 1.5}
        self.assertEqual(leaks(json.dumps(rl.sanitize(rec))), [])
        self.assertEqual(rl.sanitize({'t': 1.5, 'ok': True})['t'], 1.5)

    def test_ride_log_records_and_ids(self):
        with tmpdir() as d:
            log = rt.RideLog(d)
            log.append('ride-20261108-101010', [{'t': 1, 'note': TOKEN, 'ev': [{'k': KEY}]}])
            log.append_summary('ride-20261108-101010', {'t': 2, 'x': 'k ' + KEY})
            self.assertEqual(log.append(TOKEN[:30], [{'t': 3}]), (0, None))      # a token is no ride id
            body = ''.join(open(os.path.join(d, f)).read() for f in os.listdir(d))
            self.assertEqual(leaks(body + ' '.join(os.listdir(d))), [])

    def test_live_driver_dump(self):
        try:
            import live_phone
        except ImportError:
            self.skipTest('tests/live_phone.py is not shipped in this tree')
        res = {'seen': [{'t': 'token', 'token': TOKEN}, {'t': 'stt_error', 'detail': {'echo': [KEY]}}], 'err': TOKEN}
        self.assertEqual(leaks(live_phone.dump_line(res)), [])


class Sinks(unittest.IsolatedAsyncioTestCase):
    async def test_every_sink(self):
        with tmpdir() as tmp:
            fake = await FakeElevenLabs().start()
            agent = FakeAgent(reply='Noted: ' + KEY)
            srv, core, _ = await start_core(tmp, fake, agent, eot_question_s=0, eot_default_s=0, eot_open_s=0)
            base = str(srv.make_url('')).rstrip('/')
            with self.assertLogs('hermes_ride', level='DEBUG') as cap:
                logging.getLogger('hermes_ride').info('direct %s', TOKEN)
                phone = await Phone(base, caps={'wake': TOKEN, 'stt': 'phone'}).connect()
                await phone.send({'t': 'hello', 'v': 2, 'ua': 'Mozilla ' + TOKEN, 'caps': {'wake': KEY, 'stt': 'phone'}})
                await phone.send({'t': 'stt_error', 'code': TOKEN})
                await phone.send({'t': 'stt_error', 'code': KEY})
                await phone.send({'t': 'wake', 'w': KEY, 'score': TOKEN})
                await phone.send({'t': 'no_wake', 'words': TOKEN})
                await phone.send({'t': 'committed', 'text': TOKEN})                      # dropped: too short
                await phone.send({'t': 'telemetry', 'ride': 'ride-20261108-101010',
                                  'batch': [{'t': 1000, 'note': TOKEN, 'ev': [{'k': KEY}]}]})
                await phone.send({'t': 'summary', 'ride': 'ride-20261108-101010', 's': {'t': 2000, 'x': KEY}})
                await phone.send({'t': 'committed', 'text': 'please remember ' + TOKEN + ' for me'})
                await phone.wait({'t': 'end', 'turn': 1})
                await phone.send({'t': 'play', 'turn': 1, 'seq': 0})
                await phone.send({'t': 'partial', 'text': 'stop it now ' + KEY})
                await phone.send({'t': 'idle', 'turn': 1})
                for _ in range(200):
                    if os.path.exists(os.path.join(tmp, 'logs', 'turns.jsonl')) and \
                            '"event": "turn"' in open(os.path.join(tmp, 'logs', 'turns.jsonl')).read():
                        break
                    await asyncio.sleep(0.02)
                await phone.close()
                await stop_core(srv, core)
                await fake.close()
            body = ''
            for root, _, files in os.walk(tmp):
                for f in files:
                    body += f + open(os.path.join(root, f), errors='replace').read()
            self.assertIn('"event": "turn"', body)
            self.assertEqual(leaks(body), [])
            self.assertEqual(leaks('\n'.join(cap.output)), [])


class Boundaries(unittest.IsolatedAsyncioTestCase):
    """Fix round 2 (R6): a key that is lowercase snake_case is not mistaken for one of our labels, and provider text
    is redacted before it is cut, so a key crossing a length limit leaves no fragment."""
    def test_snake_case_access_key_is_redacted(self):
        snake = 'ab' + '_cdefghij' * 3                     # 29 chars, lowercase snake_case, key-like length
        self.assertNotIn(snake, rl.redact('access key ' + snake))
        self.assertNotIn(snake, json.dumps(rl.sanitize({'k': [snake], snake: 1})))
        self.assertEqual(rl.redact('reason dropped_stale_connection'), 'reason dropped_stale_connection')

    async def test_provider_error_cut_at_the_limit_leaves_no_key_fragment(self):
        api_key = 'sk_' + 'Zq7W' * 10                       # synthetic; echoed by the provider's error text
        frags = [api_key[i:i + 8] for i in range(len(api_key) - 7)]
        with tmpdir() as tmp:
            fake = await FakeElevenLabs().start()
            pad = ('the api key ' * 12)[:139] + ' '                            # words: the cut piece stands alone
            fake.tts_error = (401, pad + api_key + ' is not valid')           # the key crosses the 150-char cut
            srv, core, _ = await start_core(tmp, fake, FakeAgent(), secret=lambda name: api_key,
                                            eot_question_s=0, eot_default_s=0, eot_open_s=0)
            with self.assertLogs('hermes_ride', level='DEBUG') as cap:
                phone = await Phone(str(srv.make_url('')).rstrip('/')).connect()
                await phone.send({'t': 'committed', 'text': 'how fast am I going'})
                path = os.path.join(tmp, 'logs', 'turns.jsonl')
                for _ in range(250):
                    if os.path.exists(path) and '"event": "turn"' in open(path).read():
                        break
                    await asyncio.sleep(0.02)
                await phone.close()
                await stop_core(srv, core)
                await fake.close()
            body = '\n'.join(cap.output)
            for root, _, files in os.walk(tmp):
                for f in files:
                    body += open(os.path.join(root, f), errors='replace').read()
            self.assertGreater(len(fake.tts_texts), 0)                         # the provider was really called
            self.assertEqual([f for f in frags if f in body], [])


if __name__ == '__main__':
    unittest.main()
