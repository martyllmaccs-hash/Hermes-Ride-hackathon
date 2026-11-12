"""Protocol v2 (W1): binary audio frames, hello/caps, single-use token minting, redaction, STT backoff (W4)."""
import asyncio
import json
import logging
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import FakeAgent, FakeElevenLabs, Phone, read_jsonl, start_core, stop_core, tmpdir  # noqa: E402
import ride_logic as rl  # noqa: E402
import ride_logic  # noqa: E402
import ride_core  # noqa: E402


class Frames(unittest.TestCase):
    def test_round_trip(self):
        for mime in ('audio/mpeg', 'audio/wav', 'audio/mp4', 'audio/ogg'):
            f = rl.encode_audio_frame(mime, 70000, 513, b'ID3data')
            self.assertEqual(len(f), 7 + 7)
            self.assertEqual(rl.decode_audio_frame(f), (mime, 70000, 513, b'ID3data'))

    def test_layout_is_big_endian_type_turn_seq(self):
        f = rl.encode_audio_frame('audio/wav', 1, 2, b'RIFF')
        self.assertEqual(f[:7], bytes([2, 0, 0, 0, 1, 0, 2]))
        self.assertEqual(rl.encode_audio_frame('audio/mpeg', 3, 0, b'')[0], 1)

    def test_bad_frames(self):
        with self.assertRaises(ValueError):
            rl.decode_audio_frame(b'\x01\x00')
        with self.assertRaises(ValueError):
            rl.decode_audio_frame(bytes([9, 0, 0, 0, 1, 0, 0]) + b'x')

    def test_smaller_than_base64(self):
        import base64
        import json
        audio = os.urandom(6000)
        v1 = json.dumps({'t': 'audio', 'turn': 12, 'seq': 3, 'mime': 'audio/mpeg', 'epoch': 'abcd1234-7',
                         'b64': base64.b64encode(audio).decode()})
        v2 = rl.encode_audio_frame('audio/mpeg', 12, 3, audio)
        self.assertLess(len(v2), len(v1) * 0.76)


class Redaction(unittest.TestCase):
    def test_token_key_email(self):
        s = ('token sutkn_AbC123xyz0987654321qwertyuio sent; key sk_0123456789abcdef0123456789 '
             'mail a.b@example.com short_ok')
        r = rl.redact(s)
        self.assertNotIn('sutkn_AbC', r)
        self.assertIn('<token>', r)
        self.assertNotIn('sk_0123456789', r)
        self.assertIn('<email>', r)
        self.assertIn('short_ok', r)

    def test_short_token_still_redacted(self):
        self.assertEqual(rl.redact('sutkn_abc'), '<token>')

    def test_any_24_char_run(self):
        self.assertEqual(rl.redact('x' * 24), '<redacted>')
        self.assertEqual(rl.redact('x' * 23), 'x' * 23)
        for label in ('dropped_stale_connection', 'insufficient_audio_activity', 'session_time_limit_exceeded'):
            self.assertEqual(rl.redact(label), label)                 # own labels survive the sanitizer
        self.assertEqual(rl.redact('sk_' + 'ab12' * 6), '<redacted>')


class EnglishDefault(unittest.TestCase):
    def test_default_hints_are_english_only(self):     # AUD-V2-05 deferred: English only, by decision
        p = ride_core.RideCore({}, None, lambda k: None).stt_params()
        self.assertEqual(p['secondary_languages'], ['en'])
        self.assertEqual(p.get('language_code'), 'en')   # English locked
        self.assertEqual(ride_logic.filter_transcript('How fast am I going?'), ('How fast am I going?', None))


class Backoff(unittest.TestCase):
    def test_schedule_and_episode(self):
        b = rl.SttBackoff()
        got = [b.fail(100.0) for _ in range(6)]
        self.assertEqual([d for d, _ in got], [5, 15, 60, 300, 300, 300])
        self.assertEqual([f for _, f in got], [True, False, False, False, False, False])
        self.assertEqual(b.wait_left(150.0), 250.0)
        b.ok()
        self.assertEqual(b.fail(0.0), (5, True))


class LiveBase(unittest.IsolatedAsyncioTestCase):
    """Real core + phone server, fake ElevenLabs (token endpoint, Scribe, TTS); phone STT route."""
    cfg = {}

    async def asyncSetUp(self):
        self._tmp = tmpdir()
        self.tmp = self._tmp.name
        self.fake = await FakeElevenLabs().start()
        self.srv, self.core, self.agent = await start_core(self.tmp, self.fake, FakeAgent(),
                                                           **{'eot_question_s': 0, 'eot_default_s': 0, 'eot_open_s': 0,
                                                              **self.cfg})
        self.base = str(self.srv.make_url('')).rstrip('/')
        self.phone = await Phone(self.base, caps={'wake': 'sherpa', 'stt': 'phone'}).connect()

    async def asyncTearDown(self):
        await self.phone.close()
        await stop_core(self.srv, self.core)
        await self.fake.close()
        self._tmp.cleanup()

    def log(self):
        return read_jsonl(os.path.join(self.tmp, 'logs', 'turns.jsonl'))

    async def until(self, cond, timeout=5.0):
        for _ in range(int(timeout / 0.02)):
            if cond():
                return
            await asyncio.sleep(0.02)
        self.fail('condition not reached')


class Live(LiveBase):
    async def test_hello_v2_cfg_and_caps(self):
        h = self.phone.hello
        self.assertEqual(h['v'], 2)
        cfg = h['cfg']
        self.assertEqual(cfg['stt_route'], 'phone')
        self.assertEqual(cfg['window_s'], 10)                 # fix round 3, R3-5 (was 30)
        self.assertEqual(cfg.get('cue_gain'), 0.5)           # R3-2: cue/clip loudness, separate from the voice
        self.assertEqual(cfg.get('tone_first_s'), 1.2)       # WP2: doo-doo lead-in after 'thinking'
        self.assertEqual(cfg.get('double_tap_ms'), 350)      # WP4: the mute double tap
        self.assertEqual(cfg.get('play_buffer_ms'), 250)     # the first segment of a reply is held this long
        self.assertEqual((cfg.get('orb_design'), cfg.get('word')), ('simple', False))   # WP5: the orb; word = debug
        self.assertEqual(cfg.get('orb_motion'), 1.0)         # P4: the orb moves with the bike (gain, 0 = off)
        self.assertEqual((cfg['cues'].get('mute'), cfg['cues'].get('unmute')), ('cues/mute.m4a', 'cues/unmute.m4a'))
        self.assertEqual(cfg['vad'], {'base_db': -38.0, 'k': 0.15, 'cap_db': -25.0, 'hangover_ms': 2000,
                                      'preroll_ms': 800, 'wake_preroll_ms': 1500})
        self.assertEqual(cfg['wake']['engine'], 'sherpa')
        self.assertEqual(cfg['cues']['wake'], ['cues/wake_a.m4a', 'cues/wake_b.m4a'])
        self.assertNotIn('key', str(cfg).lower().replace('keyterms', ''))
        await asyncio.sleep(0.05)
        self.assertEqual((self.core.route, self.core.caps), ('phone', {'wake': 'sherpa', 'stt': 'phone'}))

    async def test_hello_is_recorded_with_browser_and_wake_engine(self):   # WP3 / WP0: which browser and engine was it?
        await asyncio.sleep(0.05)
        hellos = [r for r in read_jsonl(os.path.join(self.tmp, 'logs', 'turns.jsonl')) if r.get('event') == 'hello']
        self.assertEqual(len(hellos), 1)
        h = hellos[0]
        self.assertEqual((h.get('wake'), h.get('stt'), h.get('route'), h.get('ua'), h.get('v')), ('sherpa', 'phone', 'phone', 'harness', 2))
        self.assertNotIn('key', json.dumps(h).lower())

    async def test_hello_logs_wake_load_ms(self):     # Fix D: how long the sherpa load took (caps.wake_ms); junk dropped
        for ms in (4321, 'abc', -5, 600001, True, None, 12.6):
            await self.phone.send({'t': 'hello', 'v': 2, 'ua': 'harness', 'caps': {'wake': 'sherpa', 'stt': 'phone', 'wake_ms': ms}})
        await self.phone.send({'t': 'hello', 'v': 2, 'ua': 'harness', 'caps': {'wake': 'transcript', 'stt': 'phone'}})
        await asyncio.sleep(0.1)
        hellos = [r for r in read_jsonl(os.path.join(self.tmp, 'logs', 'turns.jsonl')) if r.get('event') == 'hello']
        self.assertEqual([h.get('wake_ms') for h in hellos[1:]], [4321, None, None, None, None, None, 13, None])
        self.assertNotIn('wake_ms', self.core.caps)

    async def test_v1_page_falls_back_to_server_route(self):
        await self.phone.close()
        p = await Phone(self.base, v=None).connect()
        await p.send({'t': 'hello', 'ua': 'old page'})
        await asyncio.sleep(0.05)
        self.assertEqual(self.core.route, 'server')
        self.phone = p

    async def test_token_mint_rate_limit_and_no_logging(self):
        with self.assertLogs('hermes_ride', level='DEBUG') as cm:
            logging.getLogger('hermes_ride').debug('start')
            await self.phone.send({'t': 'need_token'})
            tok = await self.phone.wait({'t': 'token'})
            await self.phone.send({'t': 'need_token'})
            err = await self.phone.wait({'t': 'token_error'})
        self.assertEqual(tok['kind'], 'realtime_scribe')
        self.assertEqual(tok['token'], self.fake.tokens[0])
        self.assertTrue(tok['url'].endswith('/v1/speech-to-text/realtime'))
        p = tok['params']
        self.assertEqual((p['model_id'], p['commit_strategy'], p['vad_silence_threshold_secs'],
                          p['secondary_languages'], p['filter_background_audio']),
                         ('scribe_v2_realtime', 'vad', 1.2, ['en'], 'true'))
        self.assertNotIn('keyterms', p)
        self.assertEqual(err['code'], 'rate_limited')
        self.assertGreater(err['retry_in'], 4)
        self.assertEqual(len(self.fake.tokens), 1)            # the second request never reached ElevenLabs
        with open(os.path.join(self.tmp, 'logs', 'turns.jsonl')) as f:
            everything = '\n'.join(cm.output) + f.read()
        self.assertNotIn(self.fake.tokens[0], everything)
        self.assertIn('token', [r['event'] for r in self.log()])

    async def test_mint_failure_backs_off_and_announces_once(self):
        self.fake.token_status = 401
        self.core.cfg['token_min_interval_s'] = 0
        await self.phone.send({'t': 'need_token'})
        b = await self.phone.wait({'t': 'stt_backoff'})
        self.assertEqual((b['code'], b['retry_in']), ('token_mint_failed', 5))
        await self.phone.wait({'t': 'state', 's': 'quota'})
        await self.phone.send({'t': 'need_token'})        # during backoff: refused locally
        e = await self.phone.wait({'t': 'token_error'})
        self.assertGreater(e['retry_in'], 3)

    async def test_phone_stt_errors_backoff_one_notice_per_episode(self):
        for code in ('quota_exceeded', 'quota_exceeded', 'rate_limited'):
            await self.phone.send({'t': 'stt_error', 'code': code})
            await asyncio.sleep(0.05)
        await self.phone.send({'t': 'stt_error', 'code': 'session_time_limit_exceeded'})   # not a backoff code
        await asyncio.sleep(0.1)
        self.assertEqual([m['retry_in'] for m in self.phone.of('stt_backoff')], [5, 15, 60])
        self.assertEqual(len(self.phone.of('state', s='quota')), 1)
        await self.phone.send({'t': 'stt_ok'})
        await asyncio.sleep(0.05)
        await self.phone.send({'t': 'stt_error', 'code': 'quota_exceeded'})
        await asyncio.sleep(0.05)
        self.assertEqual([m['retry_in'] for m in self.phone.of('stt_backoff')], [5, 15, 60, 5])
        self.assertEqual(len(self.phone.of('state', s='quota')), 2)       # a new episode
        self.assertEqual([r['notice'] for r in self.log() if r['event'] == 'stt_error'], [True, False, False, True])

    async def test_phone_route_ignores_pcm_and_never_opens_scribe(self):
        await self.phone.ws.send_bytes(b'\x00\x00' * 1600)
        await asyncio.sleep(0.2)
        self.assertEqual(self.fake.sessions, 0)

    async def test_phone_committed_joins_the_same_queue(self):
        self.agent.hold = asyncio.Event()
        await self.phone.send({'t': 'wake', 'w': 'lana', 'score': 1})
        await self.phone.send({'t': 'committed', 'text': 'first thing to say', 'lang': 'en', 'stt_s': 2.5})
        await self.until(lambda: len(self.agent.turns) == 1)
        await self.phone.send({'t': 'committed', 'text': 'second thing here', 'lang': 'en', 'stt_s': 1.0})
        await self.phone.send({'t': 'committed', 'text': 'and a third one', 'lang': 'fr', 'stt_s': 1.0})
        await self.phone.send({'t': 'committed', 'text': '(wind)'})
        await self.phone.send({'t': 'no_wake', 'words': 4})
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.agent.turns), 1)
        self.agent.hold.set()
        await self.until(lambda: len(self.agent.turns) == 2)
        # turn 1's reply became ready while two more utterances were queued: it is stale, never played, and
        # turn 2 answers everything (W6.1)
        text = self.agent.turns[1][1]
        self.assertTrue(text.endswith('\nfirst thing to say second thing here and a third one'), text)
        self.assertIn(ride_logic.STALE_NOTE, text)
        ev = [(r['event'], r.get('reason')) for r in self.log()]
        self.assertIn(('wake', None), ev)
        self.assertIn(('dropped', 'noise_tags_only'), ev)
        self.assertIn(('dropped', 'no_wake'), ev)
        self.assertNotIn('text', [r for r in self.log() if r.get('reason') == 'no_wake'][0])

    async def test_binary_audio_out(self):
        await self.phone.send({'t': 'committed', 'text': 'how fast am I going'})
        a = await self.phone.wait({'t': 'audio', 'turn': 1, 'seq': 0})
        self.assertTrue(a['bin'])
        self.assertEqual(a['mime'], 'audio/mpeg')
        self.assertTrue(a['data'].startswith(b'ID3'))
        self.assertEqual([m for m in self.phone.msgs if 'b64' in m], [])


class MicEvidence(LiveBase):
    """Round 3, B1: the page reports each mic revive attempt as {t:'mic'}; the server logs it as a `mic` event with
    strings from fixed sets only, at most 30 per connection."""

    def mics(self):
        return [r for r in self.log() if r.get('event') == 'mic']

    async def test_good_mic_frame_is_logged(self):
        await self.phone.send({'t': 'mic', 'ev': 'revive', 'why': 'statechange', 'ok': True, 'ac': 'interrupted',
                               'track': 'muted', 'gap_s': 4.2, 'n': 2})
        await self.until(lambda: self.mics())
        m = self.mics()[0]
        self.assertIsInstance(m.pop('at'), float)
        self.assertEqual(m, {'event': 'mic', 'ev': 'revive', 'why': 'statechange', 'ok': True, 'ac': 'interrupted',
                             'track': 'muted', 'gap_s': 4.2, 'n': 2})
        for why in ('return', 'pageshow', 'no frames', 'tap', 'unmute', 'muted', 'retry'):
            await self.phone.send({'t': 'mic', 'ev': 'revive', 'why': why, 'ok': False, 'ac': 'none', 'track': 'none'})
        await self.until(lambda: len(self.mics()) == 8)
        self.assertEqual([m['why'] for m in self.mics()[1:]],
                         ['return', 'pageshow', 'no frames', 'tap', 'unmute', 'muted', 'retry'])
        self.assertEqual({m['ok'] for m in self.mics()[1:]}, {False})

    async def test_junk_strings_and_numbers_become_none(self):
        secret = 'my address is 12 Some Street'
        await self.phone.send({'t': 'mic', 'ev': secret, 'why': secret, 'ok': 'yes', 'ac': {'x': secret},
                               'track': [secret], 'gap_s': secret, 'n': True, 'extra': secret})
        await self.until(lambda: self.mics())
        m = self.mics()[0]
        self.assertEqual({k: m[k] for k in ('ev', 'why', 'ac', 'track', 'gap_s', 'n')},
                         {'ev': None, 'why': None, 'ac': None, 'track': None, 'gap_s': None, 'n': None})
        self.assertIs(m['ok'], False)                      # only a real true is ok
        self.assertNotIn('extra', m)
        self.assertNotIn('Some Street', json.dumps(self.log()))

    async def test_at_most_30_mic_events_per_connection(self):
        for i in range(45):
            await self.phone.send({'t': 'mic', 'ev': 'revive', 'why': 'retry', 'ok': False, 'ac': 'suspended',
                                   'track': 'live', 'gap_s': 3.0, 'n': i + 1})
        await self.phone.send({'t': 'wake', 'w': 'hermes', 'score': 1})        # a marker after the flood
        await self.until(lambda: any(r.get('event') == 'wake' for r in self.log()))
        self.assertEqual([m['n'] for m in self.mics()], list(range(1, 31)))
        await self.phone.close()                           # a new page load gets a fresh allowance
        self.phone = await Phone(self.base, caps={'wake': 'sherpa', 'stt': 'phone'}).connect()
        await self.phone.send({'t': 'mic', 'ev': 'revive', 'why': 'tap', 'ok': True, 'ac': 'running', 'track': 'live'})
        await self.until(lambda: len(self.mics()) == 31)


class Cues(LiveBase):
    async def test_ready_state_precedes_first_audio_and_thinking_has_turn(self):
        await self.phone.send({'t': 'committed', 'text': 'how fast am I going'})
        await self.phone.wait({'t': 'audio', 'turn': 1, 'seq': 0})
        kinds = [(m['t'], m.get('s'), m.get('turn')) for m in self.phone.msgs if m['t'] in ('state', 'audio')]
        i_ready = kinds.index(('state', 'ready', 1))
        self.assertLess(kinds.index(('state', 'thinking', 1)), i_ready)
        self.assertEqual(kinds[i_ready + 1], ('audio', None, 1))
        self.assertEqual(len([k for k in kinds if k[1] == 'ready']), 1)
        self.assertEqual([k for k in kinds if k[1] == 'thinking'], [('state', 'thinking', 1)])   # one cue per turn

    async def test_cue_and_clip_assets_gated_and_served(self):
        from aiohttp import ClientSession
        from harness import KEY
        names = ['cues/wake_a.m4a', 'cues/wake_b.m4a', 'cues/thinking.m4a', 'cues/done.m4a',
                 'cues/mute.m4a', 'cues/unmute.m4a', 'cues/reset.m4a', 'cues/reset_failed.m4a', 'clips/reconnecting.m4a', 'clips/back.m4a', 'clips/quota.m4a', 'clips/working.m4a',
                 'clips/offline.m4a', 'clips/nomic.m4a']
        self.assertEqual(sorted(names), sorted(self.phone.hello['cfg']['cues']['wake'] + [
            self.phone.hello['cfg']['cues'][k] for k in ('thinking', 'done', 'mute', 'unmute', 'reset', 'reset_failed')] +
            list(self.phone.hello['cfg']['clips'].values())))
        async with ClientSession() as s:
            for n in names:
                async with s.get(f'{self.base}/{n}') as r:
                    self.assertEqual(r.status, 403, n)
                async with s.get(f'{self.base}/{n}', headers={'Cookie': 'rk=' + KEY}) as r:
                    body = await r.read()
                    self.assertEqual((r.status, r.headers['Content-Type']), (200, 'audio/mp4'), n)
                    self.assertEqual(body[4:8], b'ftyp', n)
            for bad in ('cues/..%2Fadapter.py', 'cues/CREDITS.md', 'vendor/missing.wasm', 'cues/.hidden.m4a'):
                async with s.get(f'{self.base}/{bad}', headers={'Cookie': 'rk=' + KEY}) as r:
                    self.assertEqual(r.status, 404, bad)

    def test_no_file_named_after_the_film(self):
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static')
        for d, _, files in os.walk(root):
            for f in files:
                self.assertNotRegex(f.lower(), r'blade|runner|2049|k\.?joi|joi[_.]', f)
        with open(os.path.join(root, 'cues', 'CREDITS.txt')) as f:
            credits = f.read()
        self.assertIn('Prokofiev', credits)
        self.assertIn('Blade Runner 2049', credits)


class ServerRouteIdle(unittest.IsolatedAsyncioTestCase):
    async def test_scribe_opens_on_audio_and_closes_when_idle(self):
        with tmpdir() as tmp:
            fake = await FakeElevenLabs().start()
            srv, core, _ = await start_core(tmp, fake, FakeAgent(), stt_route='server', stt_idle_close_s=0.4)
            phone = await Phone(str(srv.make_url('')).rstrip('/')).connect()
            try:
                await asyncio.sleep(0.3)
                self.assertEqual(fake.sessions, 0)                   # no audio, no Scribe session
                for _ in range(3):
                    await phone.ws.send_bytes(b'\x01\x00' * 1600)
                await asyncio.wait_for(fake.connected.wait(), 5)
                for _ in range(50):
                    if len(fake.chunks) >= 3:
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(len(fake.chunks), 3)               # frames before session_started not lost
                for _ in range(100):                                # WP6: wait for both sides; the core clears
                    if fake.closed_sessions and core.stt_ws is None:  # stt_ws in its reader's finally, which can
                        break                                       # run just after the fake saw the close (a
                    await asyncio.sleep(0.02)                       # pre-existing flake, 3 of 10 at e2b3816)
                self.assertEqual(fake.closed_sessions, 1)           # closed after the idle time
                self.assertIsNone(core.stt_ws)
                fake.connected.clear()
                await phone.ws.send_bytes(b'\x01\x00' * 1600)       # the gate opens again: a new session
                await asyncio.wait_for(fake.connected.wait(), 5)
                self.assertEqual(fake.sessions, 2)
            finally:
                await phone.close()
                await stop_core(srv, core)
                await fake.close()


class ServerRouteStart(unittest.IsolatedAsyncioTestCase):
    async def test_session_that_never_starts_is_closed_and_retried(self):
        """Fix round 2 (R2): a server-route Scribe socket without session_started is closed at the deadline; the
        next audio after a hold opens a new one, and the audio sent before it is only relayed once it started."""
        with tmpdir() as tmp:
            fake = await FakeElevenLabs().start()
            fake.start_sessions = False
            srv, core, _ = await start_core(tmp, fake, FakeAgent(), stt_route='server', stt_idle_close_s=30,
                                            stt_start_timeout_s=0.5)
            phone = await Phone(str(srv.make_url('')).rstrip('/')).connect()
            try:
                async def feed(seconds):
                    for _ in range(int(seconds / 0.1)):
                        await phone.ws.send_bytes(b'\x01\x00' * 1600)
                        await asyncio.sleep(0.1)
                await feed(0.3)
                await asyncio.wait_for(fake.connected.wait(), 5)
                await feed(1.0)                                    # past the 0.5 s deadline, audio still flowing
                self.assertEqual(fake.closed_sessions, 1)          # the stuck socket was closed
                self.assertIsNone(core.stt_ws)
                self.assertEqual(fake.sessions, 1)                 # held back: no immediate reconnect
                fake.start_sessions = True
                fake.connected.clear()
                await feed(2.5)                                    # after the 2 s hold: a new, working session
                self.assertEqual(fake.sessions, 2)
                self.assertTrue(core.stt_ok)
                self.assertGreater(len(fake.chunks), 0)
            finally:
                await phone.close()
                await stop_core(srv, core)
                await fake.close()


    # AUD-R3-06 (fix round 4): the page learns when the server's Scribe session is ready or failed, and the
    # deadline covers connect + start, not only the wait for session_started after the socket is up.
    async def test_the_page_hears_scribe_connecting_ready_and_failed(self):
        with tmpdir() as tmp:
            fake = await FakeElevenLabs().start()
            fake.start_sessions = False
            srv, core, _ = await start_core(tmp, fake, FakeAgent(), stt_route='server', stt_idle_close_s=30,
                                            stt_start_timeout_s=0.5)
            phone = await Phone(str(srv.make_url('')).rstrip('/')).connect()
            try:
                async def feed(seconds):
                    for _ in range(int(seconds / 0.1)):
                        await phone.ws.send_bytes(b'\x01\x00' * 1600)
                        await asyncio.sleep(0.1)
                await feed(1.3)                                    # never starts: failed at the deadline
                states = lambda: [(m.get('s'), m.get('why')) for m in phone.msgs if m.get('t') == 'stt']  # noqa: E731
                self.assertEqual(states(), [('connecting', None), ('failed', 'start_timeout')])
                fake.start_sessions = True
                await feed(2.5)                                    # after the 2 s hold: a session that starts
                self.assertEqual(states()[2:], [('connecting', None), ('ready', None)])
                self.assertTrue(all(m.get('epoch') == phone.epoch for m in phone.msgs if m.get('t') == 'stt'))
            finally:
                await phone.close()
                await stop_core(srv, core)
                await fake.close()

    async def test_connect_plus_start_is_bounded(self):
        with tmpdir() as tmp:
            fake = await FakeElevenLabs().start()
            fake.hang_connect = 5.0                                # the handshake itself never completes in time
            srv, core, _ = await start_core(tmp, fake, FakeAgent(), stt_route='server', stt_idle_close_s=30,
                                            stt_start_timeout_s=0.5)
            phone = await Phone(str(srv.make_url('')).rstrip('/')).connect()
            try:
                for _ in range(12):                                # 1.2 s of audio
                    await phone.ws.send_bytes(b'\x01\x00' * 1600)
                    await asyncio.sleep(0.1)
                self.assertTrue(core.stt_task is None or core.stt_task.done(), 'connect still pending past 0.5 s')
                self.assertEqual([(m.get('s'), m.get('why')) for m in phone.msgs if m.get('t') == 'stt'],
                                 [('connecting', None), ('failed', 'connect_timeout')])
                self.assertGreater(core.stt_start_retry_at, time.monotonic())   # the same growing hold applies
                self.assertEqual(getattr(fake, 'connect_attempts', 0), 1)
            finally:
                await phone.close()
                await stop_core(srv, core)
                await fake.close()


class ServerRouteUsage(unittest.IsolatedAsyncioTestCase):
    async def test_billed_seconds_are_audio_relayed_not_received(self):     # fix round 2, R8
        """Audio that never reached Scribe (no session started; dropped from the capped pending queue) is not
        reported as Scribe seconds in the ride summary."""
        with tmpdir() as tmp:
            fake = await FakeElevenLabs().start()
            fake.start_sessions = False
            srv, core, _ = await start_core(tmp, fake, FakeAgent(), stt_route='server', stt_idle_close_s=30,
                                            stt_start_timeout_s=30)
            phone = await Phone(str(srv.make_url('')).rstrip('/')).connect()
            try:
                for _ in range(80):                                # 8 s of 100 ms frames, received, never relayed
                    await phone.ws.send_bytes(b'\x01\x00' * 1600)
                await asyncio.wait_for(fake.connected.wait(), 5)
                await asyncio.sleep(0.2)
                self.assertEqual(fake.chunks, [])
                await phone.close()
                path = os.path.join(tmp, 'logs', 'turns.jsonl')
                for _ in range(100):
                    if os.path.exists(path) and 'ride_summary' in open(path).read():
                        break
                    await asyncio.sleep(0.02)
                s = [r for r in read_jsonl(path) if r.get('event') == 'ride_summary']
                self.assertEqual(s[0]['stt_audio_s'] if s else 0, 0)
            finally:
                await stop_core(srv, core)
                await fake.close()


class Reset(LiveBase):
    """WP6 (D5, P6): {t:'reset', epoch} -> the gateway steps (adapter hook) -> RideCore state reset -> {t:'reset_done'}.
    Refused (logged, never run) with a missing or stale epoch, from a page that is not the attached one, while a reset
    is in flight and when this connection already reset. A failure is {t:'reset_failed', step} and the lane works."""
    cfg = {'reset_timeout_s': 1.0}

    def gateway(self, result=None, hold=None):
        calls = []

        async def reset_gateway():
            calls.append('gateway')
            if hold is not None:
                await hold.wait()
            return dict(result or {'ok': True, 'old': 'S1', 'new': 'S2'})
        self.core.reset_gateway = reset_gateway
        return calls

    def events(self, name):
        return [r for r in self.log() if r.get('event') == name]

    async def test_reset_runs_once_and_answers_done(self):
        calls = self.gateway()
        await self.phone.send({'t': 'reset', 'epoch': self.phone.epoch})
        done = await self.phone.wait({'t': 'reset_done'})
        self.assertEqual((calls, done['epoch']), (['gateway'], self.phone.epoch))
        ev = self.events('reset')
        self.assertEqual([(e['ok'], e.get('old'), e.get('new')) for e in ev], [(True, 'S1', 'S2')])
        self.assertEqual(self.phone.of('reset_failed'), [])

    async def test_missing_or_stale_epoch_is_refused(self):
        calls = self.gateway()
        await self.phone.send({'t': 'reset'})
        await self.phone.send({'t': 'reset', 'epoch': self.phone.epoch.split('-')[0] + '-999'})
        await self.phone.send({'t': 'reset', 'epoch': 'other-' + self.phone.epoch.split('-')[1]})
        await self.until(lambda: len(self.events('reset_refused')) == 3)
        self.assertEqual(calls, [])
        self.assertEqual([e['why'] for e in self.events('reset_refused')], ['stale_epoch'] * 3)
        self.assertEqual((self.phone.of('reset_done'), self.phone.of('reset_failed')), ([], []))

    async def test_never_twice_in_flight_or_replayed(self):
        hold = asyncio.Event()
        calls = self.gateway(hold=hold)
        msg = {'t': 'reset', 'epoch': self.phone.epoch}
        await self.phone.send(msg)
        await self.until(lambda: calls == ['gateway'])
        await self.phone.send(msg)                                   # a second one while the first runs
        await self.until(lambda: len(self.events('reset_refused')) == 1)
        hold.set()
        await self.phone.wait({'t': 'reset_done'})
        await self.phone.send(msg)                                   # replayed after it finished
        await self.until(lambda: len(self.events('reset_refused')) == 2)
        self.assertEqual(calls, ['gateway'])
        self.assertEqual([e['why'] for e in self.events('reset_refused')], ['in_flight', 'already_reset'])
        self.assertEqual(len(self.phone.of('reset_done')), 1)

    async def test_an_old_page_cannot_reset(self):
        calls = self.gateway()
        old = self.phone
        self.phone = await Phone(self.base, caps={'wake': 'sherpa', 'stt': 'phone'}).connect()   # replaces it
        old_cid = int(old.epoch.split('-')[1])         # the server closed the old socket; its frames still in flight:
        self.core.on_client_msg({'t': 'reset', 'epoch': old.epoch}, old_cid)
        await asyncio.sleep(0.2)
        await old.close()
        self.assertEqual(calls, [])
        self.assertEqual(self.phone.of('reset_done') + self.phone.of('reset_failed'), [])

    async def test_unauthenticated_socket_cannot_reset(self):
        from aiohttp import ClientSession, WSServerHandshakeError
        calls = self.gateway()
        async with ClientSession() as s:
            with self.assertRaises(WSServerHandshakeError) as cm:
                await s.ws_connect(self.base.replace('http', 'ws', 1) + '/ws')
            self.assertEqual(cm.exception.status, 403)
            with self.assertRaises(WSServerHandshakeError):
                await s.ws_connect(self.base.replace('http', 'ws', 1) + '/ws', headers={'Cookie': 'rk=wrong-key-0123456789'})
        self.assertEqual(calls, [])

    async def test_failure_is_reported_and_the_lane_still_works(self):
        self.gateway({'ok': False, 'step': 'new', 'old': 'S1'})
        await self.phone.send({'t': 'reset', 'epoch': self.phone.epoch})
        f = await self.phone.wait({'t': 'reset_failed'})
        self.assertEqual(f['step'], 'new')
        self.assertEqual(self.phone.of('reset_done'), [])
        self.assertEqual([(e['ok'], e['step']) for e in self.events('reset')], [(False, 'new')])
        await self.phone.send({'t': 'committed', 'text': 'how fast am I going'})
        await self.phone.wait({'t': 'audio', 'turn': 1, 'seq': 0})      # not frozen: a turn runs and is spoken
        self.assertEqual(len(self.agent.turns), 1)

    async def test_without_a_gateway_hook_it_fails_cleanly(self):
        await self.phone.send({'t': 'reset', 'epoch': self.phone.epoch})
        self.assertEqual((await self.phone.wait({'t': 'reset_failed'}))['step'], 'unsupported')

    async def test_a_gateway_that_hangs_times_out_and_the_lane_works(self):
        self.gateway(hold=asyncio.Event())                            # never released; reset_timeout_s = 1
        await self.phone.send({'t': 'reset', 'epoch': self.phone.epoch})
        self.assertEqual((await self.phone.wait({'t': 'reset_failed'}, timeout=5))['step'], 'timeout')
        await self.phone.send({'t': 'committed', 'text': 'how fast am I going'})
        await self.phone.wait({'t': 'audio', 'turn': 1, 'seq': 0})

    async def test_state_is_reset_and_the_old_turn_is_never_spoken(self):
        self.agent.hold = asyncio.Event()
        calls = self.gateway()
        await self.phone.send({'t': 'committed', 'text': 'how fast am I going'})
        await self.until(lambda: self.core.active is not None)
        await self.phone.send({'t': 'committed', 'text': 'and where am I'})
        await self.until(lambda: len(self.core.queue) == 1)
        old_turn, gate_epoch = self.core.active, self.core.gate_epoch
        await self.phone.send({'t': 'reset', 'epoch': self.phone.epoch})
        await self.phone.wait({'t': 'reset_done'})
        self.assertEqual(calls, ['gateway'])
        self.assertTrue(old_turn.cancelled and old_turn.stale)
        self.assertEqual(old_turn.rec.get('delivery'), 'reset')
        self.assertEqual((len(self.core.queue), self.core.pending, self.core.audio_turn, self.core.held_turn,
                          self.core.stale_prefix, self.core.passthrough, self.core.last_state),
                         (0, [], None, None, [], False, ('listening', None)))   # cleared, then a fresh listening
        self.assertGreater(self.core.gate_epoch, gate_epoch)
        self.agent.hold.set()                                         # the old turn's late reply
        await asyncio.sleep(0.3)
        self.assertEqual(self.phone.of('audio'), [])
        self.assertEqual(len(self.agent.turns), 1)                    # the queued utterance never became a turn
        self.assertEqual([e['reason'] for e in self.events('dropped')], ['reset', 'after_reset'])   # queued; straggler

    async def test_reset_while_thinking_says_nothing(self):
        """D3: the gateway step cancels the running turn (turn_done ok=False) before reset_state runs. That turn is
        part of the reset: no "Sorry, I couldn't get an answer" (no TTS, no audio frame) before reset_done."""
        self.agent.hold = asyncio.Event()                             # the turn never answers by itself

        async def reset_gateway():
            self.core.turn_done(self.core.active, ok=False)           # the adapter's interrupt step
            await asyncio.sleep(0.4)                                  # the summary prompt, the gateway's /new
            return {'ok': True, 'old': 'S1', 'new': 'S2'}
        self.core.reset_gateway = reset_gateway
        await self.phone.send({'t': 'committed', 'text': 'what is the weather like today'})
        await self.until(lambda: self.core.active is not None and len(self.agent.turns) == 1)
        old_turn = self.core.active
        await self.phone.send({'t': 'reset', 'epoch': self.phone.epoch})
        await self.phone.wait({'t': 'reset_done'})
        self.assertEqual((self.fake.tts_texts, self.phone.of('audio')), ([], []))
        await self.until(lambda: self.core.active is None)
        await asyncio.sleep(0.2)
        self.assertEqual((self.fake.tts_texts, self.phone.of('audio')), ([], []))
        self.assertTrue(old_turn.cancelled and old_turn.stale)
        self.assertEqual((old_turn.rec.get('delivery'), old_turn.rec.get('fallback')), ('reset', None))

    async def test_utterances_during_a_reset_are_dropped(self):
        hold = asyncio.Event()
        self.gateway(hold=hold)
        await self.phone.send({'t': 'reset', 'epoch': self.phone.epoch})
        await self.until(lambda: self.core.resetting is not None)
        await self.phone.send({'t': 'committed', 'text': 'how fast am I going'})
        await self.until(lambda: any(e.get('reason') == 'resetting' for e in self.events('dropped')))
        hold.set()
        await self.phone.wait({'t': 'reset_done'})
        await asyncio.sleep(0.2)
        self.assertEqual(self.agent.turns, [])


class PageScripts(LiveBase):
    async def test_every_script_the_page_loads_is_served(self):     # WP6: gesture.js / orb.js were never routed
        import re
        from aiohttp import ClientSession
        from harness import KEY
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'index.html')) as f:
            srcs = re.findall(r'<script src="([^"]+)"></script>', f.read())
        self.assertIn('gesture.js', srcs)
        self.assertIn('orb_motion.js', srcs)                 # P4
        async with ClientSession() as s:
            for name in srcs:
                async with s.get(f'{self.base}/{name}', headers={'Cookie': 'rk=' + KEY}) as r:
                    self.assertEqual((r.status, r.headers.get('Content-Type')), (200, 'text/javascript'), name)
                async with s.get(f'{self.base}/{name}') as r:
                    self.assertEqual(r.status, 403, name)


if __name__ == '__main__':
    unittest.main()
