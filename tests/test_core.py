"""RideCore integration tests: real phone server + core, fake phone, fake ElevenLabs, fake agent (no network)."""
import asyncio
import json
import os
import sys
import unittest

from aiohttp import ClientSession, WSServerHandshakeError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (KEY, FakeAgent, FakeElevenLabs, Phone, b64audio, read_jsonl, start_core,  # noqa: E402
                     stop_core, tmpdir)
import ride_core  # noqa: E402
import ride_logic  # noqa: E402


class Base(unittest.IsolatedAsyncioTestCase):
    """Server STT route (the v0.1 relay, now gated): the fake phone sends one audio frame so the core opens
    the (fake) Scribe session, then the test drives Scribe events directly."""
    tts_delay = 0.0
    cfg = {}
    base_cfg = {'stt_route': 'server', 'stt_idle_close_s': 60, 'eot_question_s': 0, 'eot_default_s': 0,
                'eot_open_s': 0, 'hold_resume_s': 0.3}

    def make_agent(self):
        return FakeAgent()

    async def asyncSetUp(self):
        self._tmp = tmpdir()
        self.tmp = self._tmp.name
        self.fake = await FakeElevenLabs(self.tts_delay).start()
        self.srv, self.core, self.agent = await start_core(self.tmp, self.fake, self.make_agent(),
                                                           **{**self.base_cfg, **self.cfg})
        self.base = str(self.srv.make_url('')).rstrip('/')
        self.phone = await Phone(self.base).connect()
        await self.phone.wait({'t': 'state', 's': 'listening'})
        await self.open_scribe()

    async def open_scribe(self):
        self.fake.connected.clear()
        await self.until(lambda: self.core.route == 'server')
        await self.phone.ws.send_bytes(b'\x00\x00' * 1600)
        await asyncio.wait_for(self.fake.connected.wait(), 5)
        await self.until(lambda: self.core.stt_ok)

    async def asyncTearDown(self):
        await self.phone.close()
        await stop_core(self.srv, self.core)
        await self.fake.close()
        self._tmp.cleanup()

    def turns(self):
        return read_jsonl(os.path.join(self.tmp, 'logs', 'turns.jsonl'))

    async def say(self, text):
        n = len(self.phone.msgs)
        await self.fake.emit(message_type='partial_transcript', text=text[:10])
        await self.fake.emit(message_type='committed_transcript', text=text)
        return n

    async def finish_turn(self, turn, start=0):
        """Act like the page: report play start, then idle when 'end' arrives."""
        await self.phone.wait({'t': 'audio', 'turn': turn, 'seq': 0}, start=start)
        await self.phone.send({'t': 'play', 'turn': turn, 'seq': 0})
        await self.phone.wait({'t': 'end', 'turn': turn}, start=start)
        await self.phone.send({'t': 'idle', 'turn': turn})

    async def until(self, cond, timeout=5.0):
        for _ in range(int(timeout / 0.02)):
            if cond():
                return
            await asyncio.sleep(0.02)
        self.fail('condition not reached')


class Auth(unittest.IsolatedAsyncioTestCase):
    async def test_key_gate(self):
        with tmpdir() as tmp:
            srv, core, _ = await start_core(tmp)
            base = str(srv.make_url('')).rstrip('/')
            try:
                async with ClientSession() as s:
                    async def get(path, **kw):
                        async with s.get(base + path, allow_redirects=False, **kw) as r:
                            return r.status, r.headers, await r.read()
                    self.assertEqual((await get('/healthz'))[0], 200)
                    st, _, body = await get('/')                  # R3-7: the key form, not the app
                    self.assertEqual(st, 200)
                    self.assertNotIn(b'client_state.js', body)
                    self.assertEqual((await get('/worklet.js'))[0], 403)
                    self.assertEqual((await get('/?k=wrong'))[0], 403)
                    st, h, _ = await get('/?k=' + KEY)                 # AUD-R3-05: key links are retired
                    self.assertEqual((st, 'Set-Cookie' in h), (403, False))
                    async with s.post(base + '/auth', data={'k': KEY}, allow_redirects=False) as r:
                        self.assertEqual(r.status, 303)
                        for part in ('rk=' + KEY, 'HttpOnly', 'Secure', 'SameSite=Strict'):
                            self.assertIn(part, r.headers['Set-Cookie'])
                    st, _, body = await get('/', headers={'Cookie': 'rk=' + KEY})
                    self.assertEqual(st, 200)
                    self.assertIn(b'client_state.js', body)
                    self.assertIn(b'pcm16', (await get('/worklet.js', headers={'Cookie': 'rk=' + KEY}))[2])
                    st, h, js = await get('/client_state.js', headers={'Cookie': 'rk=' + KEY})
                    self.assertEqual((st, h['Content-Type']), (200, 'text/javascript'))
                    self.assertIn(b'function RideClient', js)
                    self.assertNotIn(b'client_state.js', (await get('/', headers={'Cookie': 'rk=nope'}))[2])
                    self.assertEqual((await get('/worklet.js', headers={'Cookie': 'rk=nope'}))[0], 403)
                    with self.assertRaises(WSServerHandshakeError) as cm:
                        await s.ws_connect(base.replace('http', 'ws') + '/ws')
                    self.assertEqual(cm.exception.status, 403)
            finally:
                await stop_core(srv, core)

    async def test_empty_key_never_opens(self):
        with tmpdir() as tmp:
            agent = FakeAgent()
            http = ClientSession()
            core = ride_core.RideCore({'log_dir': None}, agent.submit, lambda n: None, http)
            from aiohttp.test_utils import TestServer
            srv = TestServer(ride_core.make_app(core, ''), host='127.0.0.1')
            await srv.start_server()
            try:
                async with ClientSession() as s:
                    async with s.get(str(srv.make_url('/?k=')), allow_redirects=False) as r:
                        self.assertEqual(r.status, 403)
                    async with s.get(str(srv.make_url('/')), headers={'Cookie': 'rk='}) as r:
                        self.assertNotIn(b'client_state.js', await r.read())    # the key form at most
                    async with s.post(str(srv.make_url('/auth')), data={'k': ''}, allow_redirects=False) as r:
                        self.assertEqual(r.status, 403)
            finally:
                await srv.close()
                await http.close()
            del tmp


class Pipeline(Base):
    async def test_turn_end_to_end_with_fakes(self):
        await self.phone.ws.send_bytes(b'\x01\x00' * 1600)
        await self.until(lambda: len(self.fake.chunks) >= 2)
        c = self.fake.chunks[0]
        self.assertEqual((c['message_type'], c['sample_rate'], c['commit']), ('input_audio_chunk', 16000, False))
        q = self.fake.scribe_query
        self.assertEqual((q['model_id'], q['audio_format'], q['commit_strategy'], q['vad_silence_threshold_secs']),
                         ('scribe_v2_realtime', 'pcm_16000', 'vad', '1.2'))
        self.assertEqual([v for k, v in self.fake.scribe_queries[0] if k == 'secondary_languages'],
                         ['en'])
        self.assertEqual(q.get('language_code'), 'en')   # English locked
        self.assertEqual(q['filter_background_audio'], 'true')
        await self.phone.send({'t': 'sensors', 'spd': 61.6, 'hdg': 44, 'lat': 51.5074, 'lon': -0.1278, 'acc': 15,
                               'gps_age': 500, 'lean': -12.2, 'peak': 3.14, 'mot_age': 100})
        await asyncio.sleep(0.05)
        await self.say('How fast am I going right now?')
        await self.phone.wait({'t': 'state', 's': 'thinking'})
        await self.finish_turn(1)
        turn, text, meta = self.agent.turns[0]
        self.assertTrue(text.startswith('[Ride voice mode: the rider is riding a motorcycle'))
        self.assertIn('Sensors (1 s old): 62 km/h, heading NE, 51.5074,-0.1278 ±15 m, lean 12° left', text)
        self.assertTrue(text.endswith(']\nHow fast am I going right now?'))
        self.assertEqual(meta['rider_text'], 'How fast am I going right now?')
        # the reply is spoken sentence by sentence, in order, as MP3 segments
        self.assertEqual(self.fake.tts_texts, ['You are doing 62 km/h.', 'Keep it steady.'])
        audio = self.phone.of('audio', turn=1)
        self.assertEqual([m['seq'] for m in audio], [0, 1])
        self.assertEqual({m['mime'] for m in audio}, {'audio/mpeg'})
        self.assertTrue(b64audio(audio[0]).startswith(b'ID3'))
        self.assertEqual(self.phone.of('end', turn=1)[0]['segs'], 2)
        await self.phone.wait({'t': 'state', 's': 'listening'}, start=len(self.phone.msgs) - 3)
        await self.until(lambda: any(t.get('event') == 'turn' for t in self.turns()))
        rec = [t for t in self.turns() if t.get('event') == 'turn'][0]
        self.assertEqual((rec['engine'], rec['delivery'], rec['reply']),
                         ('direct', 'ok', 'You are doing 62 km/h. Keep it steady.'))
        for k in ('endpoint', 'agent_total', 'tts_first_byte', 'speech_end_to_play'):
            self.assertIsNotNone(rec['ms'][k], k)
        self.assertTrue(all(m['epoch'] == self.phone.epoch for m in self.phone.msgs))

    async def test_noise_never_costs_a_turn(self):
        for t in ['(wind)', '[noise] [music]', 'yeah', '', '(engine revving) um']:
            await self.fake.emit(message_type='committed_transcript', text=t)
        await self.until(lambda: len(self.phone.of('dropped')) == 5)
        self.assertEqual(self.agent.turns, [])
        reasons = [t['reason'] for t in self.turns() if t.get('event') == 'dropped']
        self.assertEqual(reasons, ['noise_tags_only', 'noise_tags_only', 'too_short', 'empty', 'too_short'])

    async def test_server_route_strips_the_wake_word(self):
        await self.say('Hey, Hermes. Where is the next fuel stop?')
        await self.finish_turn(1)
        self.assertTrue(self.agent.turns[0][1].endswith(']\nWhere is the next fuel stop?'))
        n = len(self.agent.turns)
        await self.fake.emit(message_type='committed_transcript', text='Hey Hermes.')
        await asyncio.sleep(0.2)
        self.assertEqual((len(self.agent.turns), self.phone.of('dropped')), (n, []))   # wake word alone: nothing

    async def test_server_route_probe_needs_the_wake_word(self):        # AUD-V2-06
        # transcript wake engine on the server route: the gate is probing (VAD speech while asleep). Bystander
        # speech is a miss (no turn, no text logged), a leading wake word is a hit and the rest is the turn.
        await self.phone.send({'t': 'gate', 's': 'probe'})
        await self.until(lambda: self.core.gate == 'probe')
        await self.say('Can you pass me the salt please')
        await self.phone.wait({'t': 'wake_miss'})
        await asyncio.sleep(0.2)
        self.assertEqual(self.agent.turns, [])
        miss = [t for t in self.turns() if t.get('reason') == 'no_wake']
        self.assertEqual(len(miss), 1)
        self.assertNotIn('text', miss[0])
        self.assertNotIn('salt', json.dumps(self.turns()))
        await self.phone.send({'t': 'gate', 's': 'probe'})
        await self.until(lambda: self.core.gate == 'probe')
        await self.reopen_scribe()                  # R4: the miss closed that session; the new probe opens one
        await self.say('Hey Hermes, how fast am I going?')
        await self.phone.wait({'t': 'wake_hit'})
        await self.finish_turn(1)
        self.assertTrue(self.agent.turns[0][1].endswith(']\nhow fast am I going?'))
        self.assertEqual([t['engine'] for t in self.turns() if t.get('event') == 'wake'], ['transcript-server'])

    async def reopen_scribe(self):
        self.fake.connected.clear()
        n = self.fake.sessions
        await self.phone.ws.send_bytes(b'\x00\x00' * 1600)
        await self.until(lambda: self.fake.sessions == n + 1 and self.core.stt_ok)

    async def scribe_emit(self, **msg):
        """Scribe delivers late: the session may already be closed by the core (then nothing arrives)."""
        try:
            await self.fake.emit(**msg)
        except Exception:
            pass

    async def test_late_final_after_a_wake_miss_never_becomes_a_turn(self):     # fix round 2, R4
        await self.phone.send({'t': 'gate', 's': 'probe'})
        await self.until(lambda: self.core.gate == 'probe')
        await self.say('Can you pass me the salt please')
        await self.phone.wait({'t': 'wake_miss'})
        await self.scribe_emit(message_type='committed_transcript', text='turn left at the next junction please')
        await asyncio.sleep(0.3)
        self.assertEqual(self.agent.turns, [])
        self.assertEqual(self.phone.of('final'), [])
        self.assertNotIn('junction', json.dumps(self.turns()))
        self.assertEqual(self.fake.closed_sessions, 1)            # the probe's session ended with the miss

    async def test_late_final_after_a_probe_timeout_never_becomes_a_turn(self):  # fix round 2, R4
        await self.phone.send({'t': 'gate', 's': 'probe'})
        await self.until(lambda: self.core.gate == 'probe')
        await self.phone.send({'t': 'gate', 's': 'sleep'})        # the page's probe timed out
        await self.until(lambda: self.core.gate == 'sleep')
        await self.scribe_emit(message_type='committed_transcript', text='Can you pass me the salt please')
        await asyncio.sleep(0.3)
        self.assertEqual(self.agent.turns, [])
        self.assertEqual(self.phone.of('final'), [])

    async def test_partial_after_sleep_is_not_talking(self):                     # fix round 2, R4
        await self.phone.send({'t': 'gate', 's': 'probe'})
        await self.phone.send({'t': 'gate', 's': 'sleep'})
        await self.until(lambda: self.core.gate == 'sleep')
        await self.scribe_emit(message_type='partial_transcript', text='hello there my friend')
        await asyncio.sleep(0.2)
        self.assertEqual(self.core.partial, '')
        self.assertFalse(self.core.rider_talking())

    async def test_partials_while_awake_keep_the_page_window_open(self):            # fix round 3, R3-5
        await self.phone.send({'t': 'gate', 's': 'awake'})
        await self.until(lambda: self.core.gate == 'awake')
        await self.scribe_emit(message_type='partial_transcript', text='so I was thinking')
        await asyncio.sleep(0.3)
        self.assertEqual(len(self.phone.of('heard')), 1)
        await self.scribe_emit(message_type='partial_transcript', text='so I was thinking about')
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.phone.of('heard')), 1)        # throttled: at most one per 0.5 s

    async def test_markdown_and_links_not_spoken(self):
        self.agent.reply = '**Turn left** at the [servo](https://maps.example/x). Then `straight` on.'
        await self.say('which way do I go')
        await self.finish_turn(1)
        self.assertEqual(self.fake.tts_texts, ['Turn left at the servo.', 'Then straight on.'])


class Queueing(Base):
    async def test_queue_joins_and_never_runs_two_turns(self):
        self.agent.hold = asyncio.Event()
        await self.say('first thing to say')
        await self.until(lambda: len(self.agent.turns) == 1)
        await self.say('second thing here')
        await self.say('and a third one')
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.agent.turns), 1)     # held: nothing else is submitted
        self.agent.hold.set()
        await self.until(lambda: len(self.agent.turns) == 2)
        # turn 1's reply became ready while two more utterances were queued: it is stale, never played, and
        # turn 2 answers everything (W6.1)
        text = self.agent.turns[1][1]
        self.assertTrue(text.endswith('\nfirst thing to say second thing here and a third one'), text)
        self.assertIn(ride_logic.STALE_NOTE, text)
        self.assertEqual(self.agent.max_running, 1)
        await self.until(lambda: self.core.worker is None)


class BargeIn(Base):
    tts_delay = 0.15
    cfg = {}

    async def test_barge_in_stops_audio_and_drops_rest(self):
        self.agent.reply = 'One sentence here. Two sentences here. Three sentences here. Four sentences here.'
        await self.say('tell me a story')
        await self.phone.wait({'t': 'audio', 'turn': 1, 'seq': 0})
        await self.phone.send({'t': 'play', 'turn': 1, 'seq': 0})      # the page: the answer is playing (AUD-V2-07)
        await self.until(lambda: self.core.audio_turn is not None and self.core.audio_turn.play_start)
        await self.fake.emit(message_type='partial_transcript', text='no wait stop there')
        await self.phone.wait({'t': 'stop', 'turn': 1})
        await self.phone.wait({'t': 'end', 'turn': 1})
        self.assertLess(len(self.phone.of('audio', turn=1)), 4)
        await self.say('what about the weather')
        await self.until(lambda: len(self.agent.turns) == 2)
        self.assertIn('They interrupted your previous reply', self.agent.turns[1][1])
        self.assertIn('barge_in', [t.get('event') for t in self.turns()])

    async def test_one_word_partial_does_not_barge_in(self):
        await self.say('tell me something')
        await self.phone.wait({'t': 'audio', 'turn': 1, 'seq': 0})
        await self.fake.emit(message_type='partial_transcript', text='(wind) um')
        await self.phone.wait({'t': 'end', 'turn': 1})
        self.assertEqual(self.phone.of('stop'), [])


class Failures(Base):
    async def test_no_reply_speaks_fallback(self):
        self.agent.reply = None
        await self.say('are you there at all')
        await self.finish_turn(1)
        self.assertEqual(self.fake.tts_texts, [ride_core.FALLBACK_REPLY])

    async def test_submit_error_ends_turn(self):
        async def broken(turn, text, meta):
            raise RuntimeError('gateway down')
        self.core.submit = broken
        await self.say('anyone listening there')
        await self.finish_turn(1)
        rec = await self._turn_rec()
        self.assertIn('submit', ' '.join(rec['errors']))

    async def _turn_rec(self):
        await self.until(lambda: any(t.get('event') == 'turn' for t in self.turns()))
        return [t for t in self.turns() if t.get('event') == 'turn'][0]

    async def test_tts_failure_is_logged_not_fatal(self):
        async def bad_tts(text, prev):
            raise RuntimeError('quota')
        self.core.tts = bad_tts
        await self.say('say something please')
        await self.phone.wait({'t': 'end', 'turn': 1})
        rec = await self._turn_rec()
        self.assertTrue(any('quota' in e for e in rec['errors']))
        await self.phone.wait({'t': 'state', 's': 'listening'}, start=1)


class Notices(Base):
    async def test_send_outside_a_turn_is_spoken(self):
        await self.core.reply_text('Reminder: fuel stop in ten kilometres.')
        await self.finish_turn(1)
        self.assertEqual(self.fake.tts_texts, ['Reminder: fuel stop in ten kilometres.'])
        await self.until(lambda: any(t.get('event') == 'notice' for t in self.turns()))
        self.assertEqual(self.agent.turns, [])

    async def test_notice_inside_turn_is_spoken_in_order(self):
        self.agent.hold = asyncio.Event()
        await self.say('run that command for me')
        await self.until(lambda: len(self.agent.turns) == 1)
        await self.core.speak_notice('That needs approval.')
        self.agent.hold.set()
        await self.finish_turn(1)
        self.assertEqual(self.fake.tts_texts[0], 'That needs approval.')

    async def test_no_phone_no_speech(self):
        await self.phone.close()
        await self.until(lambda: self.core.ws is None)
        self.assertFalse(await self.core.reply_text('nobody hears this'))
        self.phone = await Phone(self.base).connect()   # for tearDown


class Clarify(Base):
    async def test_answer_goes_into_running_turn(self):
        self.agent.hold = asyncio.Event()
        await self.say('book me a table somewhere')
        await self.until(lambda: len(self.agent.turns) == 1)
        self.core.passthrough = True
        await self.say('two')                        # one word is enough for an answer
        await self.until(lambda: len(self.agent.turns) == 2)
        turn, text, meta = self.agent.turns[1]
        self.assertEqual((text, meta), ('two', {'clarify_answer': True}))
        self.assertIs(turn, self.agent.turns[0][0])
        self.assertFalse(self.core.passthrough)
        self.agent.hold.set()
        await self.finish_turn(1)
        self.assertEqual(len(self.agent.turns), 2)


class HermesEngine(Base):
    cfg = {'tts_engine': 'hermes'}

    async def test_pcm_stream_becomes_wav_segments(self):
        self.agent.hold = asyncio.Event()
        self.agent.reply = None
        await self.say('what is my lean angle')
        await self.until(lambda: self.core.active is not None)
        stream = self.core.pcm_begin(24000, 1, 2)
        self.core.pcm_write(stream, b'\x00\x00' * 12000)    # 0.5 s, then a pause -> one segment
        await asyncio.sleep(0.3)
        self.core.pcm_write(stream, b'\x00\x00' * 6000)
        self.core.pcm_end(stream)
        await self.core.reply_text('Twelve degrees left.', speak=False)   # final text: not spoken twice
        self.agent.hold.set()
        self.core.turn_done()
        await self.finish_turn(1)
        audio = self.phone.of('audio', turn=1)
        self.assertEqual([m['mime'] for m in audio], ['audio/wav', 'audio/wav'])
        self.assertEqual(b64audio(audio[0])[:4], b'RIFF')
        self.assertEqual(len(b64audio(audio[0])), 44 + 24000)
        self.assertEqual(self.fake.tts_texts, [])

    async def test_safety_net_speaks_text_when_hermes_made_no_audio(self):
        self.agent.hold = asyncio.Event()
        self.agent.reply = None
        await self.say('where am I right now')
        await self.until(lambda: self.core.active is not None)
        await self.core.reply_text('You are near Bright.', speak=False)
        self.agent.hold.set()
        self.core.turn_done()
        await self.finish_turn(1)
        self.assertEqual(self.fake.tts_texts, ['You are near Bright.'])
        await self.until(lambda: any(t.get('event') == 'turn' for t in self.turns()))
        self.assertEqual([t for t in self.turns() if t.get('event') == 'turn'][0]['fallback'], 'direct_tts')


class Links(Base):
    async def test_new_page_replaces_old_and_scribe_follows(self):
        old = self.phone
        self.phone = await Phone(self.base).connect()
        await old.reader
        self.assertEqual(old.ws.close_code, 4000)
        await old.http.close()
        self.assertNotEqual(old.epoch, self.phone.epoch)
        await self.phone.wait({'t': 'state', 's': 'listening'})
        await self.open_scribe()
        self.assertEqual(self.fake.sessions, 2)

    async def test_close_while_thinking_drops_delivery(self):
        self.agent.hold = asyncio.Event()
        await self.say('long question coming up')
        await self.until(lambda: len(self.agent.turns) == 1)
        await self.phone.close()
        await self.until(lambda: self.core.ws is None)
        self.agent.hold.set()
        await self.until(lambda: any(t.get('event') == 'turn' for t in self.turns()))
        rec = [t for t in self.turns() if t.get('event') == 'turn'][0]
        self.assertEqual(rec['delivery'], 'dropped_stale_connection')
        self.assertEqual(self.fake.tts_texts, [])      # no TTS money for a page that is gone
        self.phone = await Phone(self.base).connect()


class ClientCfg(unittest.IsolatedAsyncioTestCase):
    async def test_play_buffer_ms_reaches_the_phone(self):     # hello.cfg carries the playback buffer, default 250
        from aiohttp import ClientSession
        async with ClientSession() as http:
            core = ride_core.RideCore({'log_dir': None}, FakeAgent().submit, lambda n: None, http)
            self.assertEqual(core.client_cfg().get('play_buffer_ms'), 250)
            core = ride_core.RideCore({'log_dir': None, 'play_buffer_ms': 400}, FakeAgent().submit, lambda n: None, http)
            self.assertEqual(core.client_cfg().get('play_buffer_ms'), 400)


class SensorAge(unittest.TestCase):
    def test_source_age_semantics(self):
        s = {'spd': 50, 'gps_age': 12000, 'lean': 5, 'mot_age': 300}
        ages = ride_logic.sensor_ages(s, 100.0, 101.0)
        self.assertAlmostEqual(ages['gps'], 13.0)
        self.assertAlmostEqual(ages['mot'], 1.3)
        self.assertEqual(ride_logic.describe_sensors(s, ages), 'Sensors (1 s old): lean 5° right')


if __name__ == '__main__':
    unittest.main()
