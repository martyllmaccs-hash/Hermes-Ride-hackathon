"""Round 3, B2: Whistle speech-to-text on the Hermes host as a selectable stt_engine (Scribe stays the default and
the fallback). Fake engine for the turn-taking rules, a fake helper process for WhistleEngine, and the real model on
the fixtures when it is installed on this host.

Measured here (10 Nov 2026, 4-core VPS, load about 3, model kept loaded in tools/whistle_worker.py; two passes):
hermes_search.wav 4.4 s -> 496 / 1026 ms; hermes_clean.wav 2.9 s -> 673 / 1904 ms; hermes_road.wav 2.9 s -> 365 /
1161 ms; speech_clean.wav 9.4 s -> 5411 / 7245 ms; speech_road.wav 9.4 s -> 14852 / 8697 ms. Helper load 0.4 s.
"""
import asyncio
import json
import os
import sys
import time
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (FakeAgent, FakeElevenLabs, FakeWhistle, Phone, read_jsonl, start_core,  # noqa: E402
                     stop_core, tmpdir)
import ride_core  # noqa: E402
import ride_logic as rl  # noqa: E402
import ride_whistle  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, 'fixtures')
FRAME = b'\x01\x00' * 1600            # 100 ms of PCM16 mono 16 kHz, as the page batches it
SEC = 32000                           # bytes per second


class WakeSpellings(unittest.TestCase):
    """What Whistle wrote for the wake phrase in the fixtures (measured), stripped at the start only."""

    def strip(self, text, word='hey hermes'):
        return rl.strip_wake(rl.whistle_text(text), rl.wake_word(word)['alts'])

    def test_whistle_wrote_quote_Hey_comma_Hermes_comma_for_hermes_clean_and_hermes_road(self):
        self.assertEqual(self.strip('"Hey, Hermes, how fast am I going?"'), ('how fast am I going?', True))

    def test_whistle_wrote_Hermes_searched_for_hermes_search(self):
        self.assertEqual(self.strip('Hermes searched the web for the weather forecast in bright to morrow.'),
                         ('searched the web for the weather forecast in bright to morrow.', True))

    def test_whistle_wrote_HELANA_dot_for_speech_clean(self):
        self.assertEqual(self.strip('HELANA. This is a test from the bike. What is a good place to stop for coffee '
                                    'in about half an hour?', 'hey lana'),
                         ('This is a test from the bike. What is a good place to stop for coffee in about half an '
                          'hour?', True))

    def test_whistle_wrote_quote_Hey_lana_bang_for_speech_road(self):
        self.assertEqual(self.strip('"Hey lana! This is a test from the bike.', 'hey lana'),
                         ('This is a test from the bike.', True))

    def test_whistle_wrote_Hey_comma_Lana_comma_for_search_clean_and_two_quoted_parts_for_stale_pause(self):
        self.assertEqual(self.strip('Hey, Lana, search the web for the weather forecast in bright to morrow.',
                                    'hey lana')[0], 'search the web for the weather forecast in bright to morrow.')
        self.assertEqual(self.strip('"Hey, Lana, can you find me a good place?" "To stop for coffee near bright."',
                                    'hey lana'), ('can you find me a good place? To stop for coffee near bright.', True))

    def test_the_match_inside_a_sentence_is_not_loosened(self):
        alts = rl.wake_word('hey lana')['alts']
        for text in ('I told Helana about it', 'Then hey lana said so', 'Helanaville is far', 'The helana road'):
            self.assertEqual(rl.strip_wake(text, alts), (text, False), text)
        self.assertEqual(rl.strip_wake('I told Hermes about it'), ('I told Hermes about it', False))
        self.assertEqual(rl.strip_wake('HELANA. How fast?'), ('HELANA. How fast?', False))   # not the default word

    def test_whistle_failed_is_a_known_code_without_backoff(self):
        self.assertIn('whistle_failed', rl.STT_ERROR_CODES)
        self.assertNotIn('whistle_failed', rl.STT_BACKOFF_CODES)


class WhistleBase(unittest.IsolatedAsyncioTestCase):
    cfg = {}
    texts = ()
    tts_delay = 0.0

    async def asyncSetUp(self):
        self._tmp = tmpdir()
        self.tmp = self._tmp.name
        self.fake = await FakeElevenLabs(self.tts_delay).start()
        self.wh = FakeWhistle(self.texts)
        self.agent = FakeAgent()
        self.srv, self.core, _ = await start_core(
            self.tmp, self.fake, self.agent, whistle=self.wh,
            **{'stt_engine': 'whistle', 'whistle_end_s': 0.3, 'eot_question_s': 0, 'eot_default_s': 0, 'eot_open_s': 0,
               'hold_resume_s': 0.3, **self.cfg})
        self.base = str(self.srv.make_url('')).rstrip('/')
        self.phone = await Phone(self.base, caps={'wake': 'sherpa', 'stt': 'phone'}).connect()
        await self.phone.wait({'t': 'state', 's': 'listening'})
        await self.until(lambda: any(r.get('event') == 'hello' for r in self.log()))

    async def asyncTearDown(self):
        await self.phone.close()
        await stop_core(self.srv, self.core)
        await self.fake.close()
        self._tmp.cleanup()

    def log(self, event=None):
        recs = read_jsonl(os.path.join(self.tmp, 'logs', 'turns.jsonl'))
        return recs if event is None else [r for r in recs if r.get('event') == event]

    async def until(self, cond, timeout=5.0):
        for _ in range(int(timeout / 0.02)):
            if cond():
                return
            await asyncio.sleep(0.02)
        self.fail('condition not reached')

    async def frames(self, n, pace=0.0):
        for _ in range(n):
            await self.phone.ws.send_bytes(FRAME)
            if pace:
                await asyncio.sleep(pace)

    async def vad(self, s):
        await self.phone.send({'t': 'vad', 's': s})

    async def gate(self, s):
        await self.phone.send({'t': 'gate', 's': s})

    async def wake_and_say(self, speech_frames=10):
        """The sherpa wake as the page does it: gate awake, the 1.5 s pre-roll at once, speech still running (so no
        VAD-on edge), then the VAD-off edge."""
        await self.gate('awake')
        await self.frames(15 + speech_frames)
        await self.vad(0)

    async def phrase(self, frames=10):
        await self.vad(1)
        await self.frames(frames)
        await self.vad(0)

    def stt(self):
        return [(m.get('s'), m.get('why')) for m in self.phone.of('stt')]


class Hello(WhistleBase):
    cfg = {'stt_route': 'phone'}

    async def test_hello_forces_the_server_route_and_names_the_engine(self):
        cfg = self.phone.hello['cfg']
        self.assertEqual((cfg['stt_route'], cfg['stt_engine']), ('server', 'whistle'))
        self.assertNotIn('key', str(cfg).lower().replace('keyterms', ''))
        self.assertEqual(self.core.route, 'server')          # whatever stt_route and the page's caps say
        h = self.log('hello')[0]
        self.assertEqual((h['stt_engine'], h['route'], h['stt']), ('whistle', 'server', 'phone'))
        self.assertNotIn('key', json.dumps(h).lower())
        self.assertEqual(self.wh.started, 1)                 # loaded at the first hello, before the first phrase
        await self.phone.send({'t': 'need_token'})           # no token is needed; and no Scribe session opens
        await self.gate('awake')
        await self.frames(3)
        await self.phone.wait({'t': 'stt', 's': 'ready'})
        self.assertEqual(self.fake.sessions, 0)


class Phrases(WhistleBase):
    texts = ['"Hey, Hermes, how fast am I going?"', 'And how far is the next fuel stop?', 'unused']

    async def test_one_phrase_with_the_default_0_9_s_end_gives_exactly_one_turn_with_the_wake_phrase_stripped(self):
        self.core.cfg['whistle_end_s'] = ride_core.DEFAULTS['whistle_end_s']
        self.assertEqual(ride_core.DEFAULTS['whistle_end_s'], 0.9)
        await self.wake_and_say(10)
        t_off = time.monotonic()
        await asyncio.sleep(0.6)
        self.assertEqual(self.wh.calls, [])                  # still inside the 0.9 s of silence
        fin = await self.phone.wait({'t': 'final'})
        self.assertGreaterEqual(self.wh.at[0] - t_off, 0.85)
        self.assertEqual(fin['text'], 'how fast am I going?')
        self.assertEqual(self.wh.calls, [25 * 3200])         # pre-roll + speech, one call
        await self.phone.wait({'t': 'reply', 'turn': 1})
        await asyncio.sleep(0.4)
        self.assertEqual(len(self.phone.of('final')), 1)
        self.assertEqual([t[2]['rider_text'] for t in self.agent.turns], ['how fast am I going?'])
        self.assertEqual(self.stt(), [('ready', None)])      # once per gate opening
        (s,) = self.log('stt')
        self.assertEqual((s['engine'], s['audio_s'], s['whistle_ms'], s['words']), ('whistle', 2.5, 7, 7))
        self.assertNotIn('text', s)

    async def test_two_phrases_give_two_finals_and_no_scribe_seconds(self):
        await self.wake_and_say()
        await self.phone.wait({'t': 'final', 'text': 'how fast am I going?'})
        await self.frames(4)                                 # silence between the phrases
        await self.phrase(8)
        await self.phone.wait({'t': 'final', 'text': 'And how far is the next fuel stop?'})
        self.assertEqual(len(self.wh.calls), 2)
        self.assertLessEqual(self.wh.calls[1], int(0.5 * SEC) + 8 * 3200)     # the silence before it was trimmed
        await self.phone.send({'t': 'idle', 'turn': 1})
        await self.until(lambda: self.log('turn'))
        rec = self.log('turn')[0]
        self.assertEqual((rec['stt_engine'], rec['route'], rec['stt_audio_s']), ('whistle', 'server', 0))
        self.assertEqual(self.fake.sessions, 0)
        self.assertEqual(len(self.stt()), 1)

    async def test_no_vad_on_after_the_first_buffer_means_nothing_is_transcribed(self):
        await self.wake_and_say()
        await self.phone.wait({'t': 'final'})
        self.wh.texts = ['road noise, never sent']           # would be used if the noise were transcribed
        await self.frames(30)                                # 3 s of road noise below the gate: no VAD edge
        await asyncio.sleep(0.8)                             # past whistle_end_s: still no phrase ends
        self.assertEqual(len(self.wh.calls), 1)
        self.assertLessEqual(len(self.core.wh['pcm']), 16000)   # only half a second of onset is kept
        self.assertEqual(self.wh.texts, ['road noise, never sent'])
        self.assertEqual(len(self.phone.of('final')), 1)
        await self.gate('sleep')
        await self.until(lambda: [r for r in self.log('dropped') if r.get('reason') == 'stale_gate'])
        (drop,) = [r for r in self.log('dropped') if r.get('reason') == 'stale_gate']
        self.assertEqual((drop['cut'], drop['audio_s']), ('gate_sleep', 0.5))

    async def test_a_first_buffer_without_any_vad_edge_waits_instead_of_cutting_running_speech(self):
        await self.gate('awake')
        await self.frames(15 + 20)                           # the rider talks through the gate opening for 2 s more
        await asyncio.sleep(0.6)                             # longer than whistle_end_s: nothing is cut
        self.assertEqual(self.wh.calls, [])
        await self.frames(5)
        await self.vad(0)
        await self.phone.wait({'t': 'final', 'text': 'how fast am I going?'})
        self.assertEqual(self.wh.calls, [40 * 3200])

    async def test_gate_sleep_mid_phrase_drops_the_late_result_as_stale(self):
        self.wh.hold = asyncio.Event()
        await self.wake_and_say()
        await self.until(lambda: self.wh.calls)              # being transcribed
        self.assertTrue(self.core.rider_talking())           # the turn must not start before its text
        await self.phone.wait({'t': 'heard'})                # the page's window is kept open meanwhile
        await self.phrase(6)                                 # a second phrase, still buffered ...
        await self.gate('sleep')                             # ... when the gate closes
        await self.until(lambda: self.core.gate == 'sleep')
        self.wh.hold.set()
        await self.until(lambda: [r for r in self.log('dropped') if r.get('reason') == 'stale_gate' and 'words' in r])
        await asyncio.sleep(0.5)
        self.assertEqual(self.phone.of('final'), [])
        self.assertEqual(self.agent.turns, [])
        self.assertEqual(len(self.wh.calls), 1)              # the buffered phrase was dropped, not transcribed
        dropped = [r for r in self.log('dropped') if r.get('reason') == 'stale_gate']
        self.assertEqual(sorted(('audio_s' in r, 'words' in r) for r in dropped), [(False, True), (True, False)])
        self.assertFalse(self.core.rider_talking())
        await self.frames(3)                                 # a late batch after the gate closed opens nothing
        await asyncio.sleep(0.2)
        self.assertIsNone(self.core.wh)
        self.assertEqual(len(self.stt()), 1)

    async def test_a_phrase_is_cut_at_28_s_under_the_30_s_cap(self):
        await self.gate('awake')
        await self.vad(1)
        await self.frames(290)                               # 29 s with the VAD stuck on
        await self.until(lambda: self.wh.calls)
        self.assertEqual(self.wh.calls, [28 * SEC])
        await self.until(lambda: self.core.wh is not None and len(self.core.wh['pcm']) == SEC)   # the rest waits
        await self.until(lambda: self.log('stt'))
        self.assertEqual(self.log('stt')[0]['cut'], 'cap')

    async def test_rider_talking_trusts_the_vad_for_15_s_then_calls_it_noise(self):
        await self.gate('awake')
        await self.vad(1)
        await self.until(lambda: self.core.vad == 1)
        self.core.vad_on_at = time.monotonic() - 5           # far past vad_trust_s (1.5 s): no partials to confirm
        self.assertTrue(self.core.rider_talking())
        self.core.vad_on_at = time.monotonic() - 16
        self.assertFalse(self.core.rider_talking())


class BargeIn(WhistleBase):
    texts = ['"Hey, Hermes, how fast am I going?"', 'Stop right there.']
    tts_delay = 0.05

    async def test_a_3_word_phrase_while_the_reply_plays_stops_it(self):
        await self.wake_and_say()
        await self.phone.wait({'t': 'audio', 'turn': 1, 'seq': 0})
        await self.phone.send({'t': 'play', 'turn': 1, 'seq': 0})
        await self.gate('speaking')
        await asyncio.sleep(0.3)
        await self.phrase(6)
        stop = await self.phone.wait({'t': 'stop'})
        self.assertEqual(stop['turn'], 1)
        (b,) = self.log('barge_in')
        self.assertEqual((b['turn'], b['partial']), (1, 'Stop right there.'))


class Failure(WhistleBase):
    texts = ['never used']

    async def test_engine_failure_is_logged_and_the_connection_switches_to_the_scribe_relay(self):
        self.wh.fail = True
        await self.wake_and_say()
        await self.phone.wait({'t': 'stt', 's': 'failed', 'why': 'whistle'})
        (e,) = self.log('stt_error')
        self.assertEqual((e['code'], e['why']), ('whistle_failed', 'whistle'))
        (f,) = self.log('stt_fallback')
        self.assertEqual((f['from'], f['to']), ('whistle', 'scribe'))
        self.assertEqual(self.core.conn_stt, 'scribe')
        await self.frames(2)                                 # the same page's audio now goes to (fake) Scribe
        await asyncio.wait_for(self.fake.connected.wait(), 5)
        await self.until(lambda: self.core.stt_ok)
        await self.fake.emit(message_type='committed_transcript', text='Hey Hermes, how fast am I going?')
        await self.phone.wait({'t': 'final', 'text': 'how fast am I going?'})
        self.assertEqual(len(self.wh.calls), 1)
        self.assertEqual(len(self.log('stt_fallback')), 1)   # logged once
        await self.phone.close()                             # the next page load tries Whistle again
        self.phone = await Phone(self.base).connect()
        await self.until(lambda: self.core.conn_stt == 'whistle')

    async def test_a_helper_that_cannot_start_says_failed_whistle_start(self):
        self.core.cfg['stt_fallback'] = 'none'
        self.wh.fail_start = True
        await self.gate('awake')
        await self.frames(3)
        await self.phone.wait({'t': 'stt', 's': 'failed', 'why': 'whistle_start'})
        self.assertEqual(self.log('stt_error')[0]['code'], 'whistle_failed')
        self.assertNotIn(('ready', None), self.stt())


class FailureNoFallback(WhistleBase):
    cfg = {'stt_fallback': 'none'}

    async def test_with_stt_fallback_none_nothing_switches_and_the_page_keeps_hearing_failed(self):
        self.wh.fail = True
        await self.wake_and_say()
        await self.phone.wait({'t': 'stt', 's': 'failed', 'why': 'whistle'})
        await self.frames(4)
        await self.phrase(6)
        await self.until(lambda: self.stt().count(('failed', 'whistle')) == 2)
        self.assertEqual((self.core.conn_stt, self.fake.sessions), ('whistle', 0))
        self.assertEqual(self.log('stt_fallback'), [])
        self.assertEqual([e['code'] for e in self.log('stt_error')], ['whistle_failed', 'whistle_failed'])


class FailureNoCredential(unittest.IsolatedAsyncioTestCase):
    async def test_no_fallback_without_an_elevenlabs_credential(self):
        with tmpdir() as tmp:
            wh = FakeWhistle()
            wh.fail = True
            srv, core, _ = await start_core(tmp, None, FakeAgent(), secret=lambda name: None, whistle=wh,
                                            stt_engine='whistle', whistle_end_s=0.3)
            phone = await Phone(str(srv.make_url('')).rstrip('/')).connect()
            try:
                await phone.send({'t': 'gate', 's': 'awake'})
                for _ in range(20):
                    await phone.ws.send_bytes(FRAME)
                await phone.send({'t': 'vad', 's': 0})
                await phone.wait({'t': 'stt', 's': 'failed', 'why': 'whistle'})
                self.assertEqual(core.conn_stt, 'whistle')
            finally:
                await phone.close()
                await stop_core(srv, core)
        self.assertEqual(wh.closed, 1)                       # shutdown closes the helper


class ScribeUnchanged(unittest.IsolatedAsyncioTestCase):
    async def run_core(self, **cfg):
        with tmpdir() as tmp:
            fake = await FakeElevenLabs().start()
            wh = FakeWhistle()
            srv, core, _ = await start_core(tmp, fake, FakeAgent(), whistle=wh, **cfg)
            phone = await Phone(str(srv.make_url('')).rstrip('/'), caps={'wake': 'sherpa', 'stt': 'phone'}).connect()
            try:
                await asyncio.sleep(0.05)
                await phone.send({'t': 'need_token'})
                await phone.wait({'t': 'token'})
                hello = [r for r in read_jsonl(os.path.join(tmp, 'logs', 'turns.jsonl')) if r.get('event') == 'hello']
                return phone.hello['cfg'], core.route, core.conn_stt, wh.started, hello[0]
            finally:
                await phone.close()
                await stop_core(srv, core)
                await fake.close()

    async def test_stt_engine_unset_or_scribe_keeps_the_phone_route_and_never_starts_the_helper(self):
        for cfg in ({}, {'stt_engine': 'scribe'}):
            c, route, engine, started, hello = await self.run_core(**cfg)
            self.assertEqual((c['stt_route'], c['stt_engine'], route, engine, started),
                             ('phone', 'scribe', 'phone', 'scribe', 0))
            self.assertEqual(hello['stt_engine'], 'scribe')
        self.assertEqual(ride_core.DEFAULTS['stt_engine'], 'scribe')

    async def test_an_unknown_stt_engine_is_scribe_with_a_logged_warning(self):
        with self.assertLogs('hermes_ride', level='WARNING') as cm:
            c, route, engine, started, _ = await self.run_core(stt_engine='vosk')
        self.assertEqual((c['stt_engine'], route, engine, started), ('scribe', 'phone', 'scribe', 0))
        self.assertTrue(any('stt_engine' in line for line in cm.output))


# ---------------------------------------------------------------- WhistleEngine with a fake helper process
FAKE_HELPER = r"""
import json, os, sys, time
mode = sys.argv[1]
state = sys.argv[2]
n = int(open(state).read()) + 1 if os.path.exists(state) else 1
open(state, 'w').write(str(n))
if mode == 'nostart':
    print(json.dumps({'ready': False, 'error': 'no model'}), flush=True)
    sys.exit(2)
print('noise before the ready line', flush=True)
print(json.dumps({'ready': True}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    if mode == 'die_once' and n == 1:
        sys.exit(1)
    if mode == 'die':
        sys.exit(1)
    if mode == 'hang':
        time.sleep(30)
    size = os.path.getsize(req['wav'])
    print(json.dumps({'id': req['id'], 'text': 'run %d size %d lang %s' % (n, size, req['language']), 'ms': 3}),
          flush=True)
"""


class Engine(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tmpdir()
        self.tmp = self._tmp.name
        self.helper = os.path.join(self.tmp, 'helper.py')
        with open(self.helper, 'w') as f:
            f.write(FAKE_HELPER)
        self.scratch = os.path.join(self.tmp, 'scratch')

    def tearDown(self):
        self._tmp.cleanup()

    def engine(self, mode, timeout_s=10.0):
        return ride_whistle.WhistleEngine(sys.executable, '', 'en', timeout_s, self.scratch, worker=self.helper,
                                          worker_args=[mode, os.path.join(self.tmp, 'starts')])

    async def test_transcribes_serially_and_deletes_the_wav(self):
        e = self.engine('ok')
        try:
            await e.start()
            res = await asyncio.gather(e.transcribe(FRAME * 10), e.transcribe(FRAME * 5), e.transcribe(b''))
            self.assertEqual(res, [('run 1 size 32044 lang en', 3), ('run 1 size 16044 lang en', 3), ('', 0)])
            self.assertEqual(os.listdir(self.scratch), [])   # every phrase file is gone
            self.assertEqual(e.starts, 1)
        finally:
            await e.close()
        self.assertFalse(e.alive())

    async def test_a_helper_that_died_is_restarted_once(self):
        e = self.engine('die_once')
        try:
            self.assertEqual(await e.transcribe(FRAME), ('run 2 size 3244 lang en', 3))
            self.assertEqual(e.starts, 2)
        finally:
            await e.close()

    async def test_a_second_failure_raises_whistle_error(self):
        e = self.engine('die')
        try:
            with self.assertRaises(ride_whistle.WhistleError):
                await e.transcribe(FRAME)
            self.assertEqual(e.starts, 2)
            self.assertEqual(os.listdir(self.scratch), [])
        finally:
            await e.close()

    async def test_a_timeout_raises_whistle_error_and_kills_the_helper(self):
        e = self.engine('hang', timeout_s=0.4)
        try:
            t0 = time.monotonic()
            with self.assertRaises(ride_whistle.WhistleError):
                await e.transcribe(FRAME)
            self.assertLess(time.monotonic() - t0, 5)
            self.assertFalse(e.alive())
            self.assertEqual(os.listdir(self.scratch), [])
        finally:
            await e.close()

    async def test_a_helper_that_cannot_load_or_a_missing_interpreter_raises_at_start(self):
        e = self.engine('nostart')
        with self.assertRaises(ride_whistle.WhistleError):
            await e.start()
        missing = ride_whistle.WhistleEngine(os.path.join(self.tmp, 'no-such-python'), '', 'en', 5, self.scratch)
        with self.assertRaises(ride_whistle.WhistleError):
            await missing.start()

    def test_paths_from_the_hermes_whistle_command(self):
        tool = os.path.join(self.tmp, 'tool')
        os.makedirs(tool)
        for name in ('py', 'transcribe.py', 'whistle.cact'):
            open(os.path.join(tool, name), 'w').close()
        cmd = f"{tool}/py {tool}/transcribe.py {{input_path}} {{output_path}} --language {{language}}"
        cfg = {'stt': {'providers': {'whistle': {'type': 'command', 'command': cmd}}}}
        self.assertEqual(ride_whistle.hermes_whistle_paths(cfg), (f'{tool}/py', f'{tool}/whistle.cact'))
        os.remove(os.path.join(tool, 'whistle.cact'))
        self.assertEqual(ride_whistle.hermes_whistle_paths(cfg), (f'{tool}/py', ''))
        for bad in ({}, None, {'stt': {'providers': {'whistle': {'command': '/no/such/python x.py'}}}},
                    {'stt': {'providers': {'whistle': 'junk'}}}, {'stt': 'junk'}):
            self.assertEqual(ride_whistle.hermes_whistle_paths(bad), ('', ''))


# ---------------------------------------------------------------- the real model, when this host has it
def _real_paths():
    py = os.environ.get('RIDE_WHISTLE_PYTHON') or '~/whistle/venv/bin/python'
    weights = os.environ.get('RIDE_WHISTLE_WEIGHTS') or '~/whistle/whistle.cact'
    return py, weights


PY, WEIGHTS = _real_paths()
NO_MODEL = None if os.path.isfile(PY) and os.path.isfile(WEIGHTS) else (
    f'no Whistle on this host (interpreter {PY} or weights {WEIGHTS} missing; set RIDE_WHISTLE_PYTHON / '
    'RIDE_WHISTLE_WEIGHTS to run these)')


def pcm_of(name):
    with wave.open(os.path.join(FIX, name)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        return w.readframes(w.getnframes())


@unittest.skipIf(NO_MODEL, NO_MODEL or '')
class RealModel(unittest.IsolatedAsyncioTestCase):
    async def test_the_helper_transcribes_hermes_search_4_4_s_and_speech_clean_9_4_s(self):
        with tmpdir() as tmp:
            e = ride_whistle.WhistleEngine(PY, WEIGHTS, 'en', 60, os.path.join(tmp, 'scratch'))
            try:
                t0 = time.monotonic()
                await e.start()
                load = time.monotonic() - t0
                text1, ms1 = await e.transcribe(pcm_of('hermes_search.wav'))
                text2, ms2 = await e.transcribe(pcm_of('speech_clean.wav'))
            finally:
                await e.close()
            self.assertIn('weather', text1.lower())
            self.assertIn('coffee', text2.lower())
            self.assertEqual(os.listdir(os.path.join(tmp, 'scratch')), [])
            print(f'\n[whistle] helper load {load:.2f} s; hermes_search.wav 4.4 s -> {ms1} ms {text1!r}; '
                  f'speech_clean.wav 9.4 s -> {ms2} ms {text2!r}', file=sys.stderr)

    async def test_full_core_run_hermes_search_in_100_ms_frames_gives_a_final_with_weather_and_no_wake_word(self):
        pcm = pcm_of('hermes_search.wav')
        with tmpdir() as tmp:
            fake = await FakeElevenLabs().start()
            agent = FakeAgent()
            srv, core, _ = await start_core(tmp, fake, agent, stt_engine='whistle', whistle_python=PY,
                                            whistle_weights=WEIGHTS, whistle_timeout_s=60, eot_question_s=0,
                                            eot_default_s=0, eot_open_s=0)
            phone = await Phone(str(srv.make_url('')).rstrip('/'), caps={'wake': 'sherpa', 'stt': 'server'}).connect()
            try:
                await phone.send({'t': 'gate', 's': 'awake'})
                for i in range(0, len(pcm), 3200):               # the page's 100 ms binary frames
                    await phone.ws.send_bytes(pcm[i:i + 3200])
                await phone.wait({'t': 'stt', 's': 'ready'}, timeout=60)
                await phone.send({'t': 'vad', 's': 0})
                fin = await phone.wait({'t': 'final'}, timeout=90)
                self.assertIn('weather', fin['text'].lower())
                self.assertNotRegex(fin['text'].lower(), r'^\W*(hey\W+)?hermes')
                await phone.wait({'t': 'reply', 'turn': 1}, timeout=30)
                self.assertIn('weather', agent.turns[0][2]['rider_text'].lower())
                recs = read_jsonl(os.path.join(tmp, 'logs', 'turns.jsonl'))
                (s,) = [r for r in recs if r.get('event') == 'stt']
                self.assertEqual((s['engine'], s['audio_s']), ('whistle', round(len(pcm) / SEC, 2)))
                self.assertGreater(s['whistle_ms'], 0)
                print(f"\n[whistle] core run hermes_search.wav {s['audio_s']} s -> {s['whistle_ms']} ms "
                      f"{fin['text']!r}", file=sys.stderr)
                self.assertEqual(fake.sessions, 0)               # nothing went to Scribe
            finally:
                await phone.close()
                await stop_core(srv, core)
                await fake.close()
            self.assertEqual(os.listdir(os.path.join(tmp, 'whistle')), [])   # the scratch dir holds no audio


if __name__ == '__main__':
    unittest.main()
