"""Adapter tests against the real Hermes gateway base class (skipped when Hermes isn't importable).

Hermes is found through $HERMES_ROOT (default /opt/hermes) and its venv site-packages; nothing is installed.
"""
import asyncio
import glob
import importlib
import importlib.util
import os
import re
import site
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HERMES_ROOT = os.environ.get('HERMES_ROOT', '/opt/hermes')


def _load():
    """Import the plugin as a package (the way Hermes loads it), with Hermes on sys.path."""
    if os.path.isdir(HERMES_ROOT) and HERMES_ROOT not in sys.path:
        sys.path.insert(0, HERMES_ROOT)
        for sp in glob.glob(os.path.join(HERMES_ROOT, '.venv', 'lib', 'python3*', 'site-packages')):
            site.addsitedir(sp)
    try:
        importlib.import_module('gateway.platforms.base')
    except Exception as e:  # pragma: no cover - environment dependent
        return None, f'Hermes not importable: {e!r}'
    if 'hermes_ride_pkg' not in sys.modules:
        spec = importlib.util.spec_from_file_location('hermes_ride_pkg', os.path.join(ROOT, '__init__.py'),
                                                      submodule_search_locations=[ROOT])
        mod = importlib.util.module_from_spec(spec)
        sys.modules['hermes_ride_pkg'] = mod
        spec.loader.exec_module(mod)
    return importlib.import_module('hermes_ride_pkg.adapter'), None


adapter, SKIP = _load()
if adapter is not None:   # what register(ctx) does at plugin load: makes Platform('ride') resolvable
    from gateway.platform_registry import PlatformEntry, platform_registry
    if not platform_registry.is_registered('ride'):
        platform_registry.register(PlatformEntry(name='ride', label='Ride', adapter_factory=adapter.RideAdapter,
                                                 check_fn=adapter.check_requirements, source='plugin'))
SECRETS = {'RIDE_ACCESS_KEY': 'test-access-key-0123456789', 'ELEVENLABS_API_KEY': 'dummy-test-value'}


@unittest.skipIf(adapter is None, SKIP or '')
class Registration(unittest.TestCase):
    def test_register_kwargs(self):
        calls = []

        hooks = []

        class Ctx:
            def register_platform(self, **kw):
                calls.append(kw)

            def register_hook(self, name, fn):
                hooks.append(name)
        adapter.register(Ctx())
        kw = calls[0]
        self.assertEqual(hooks, ['pre_llm_call', 'pre_tool_call', 'post_tool_call', 'post_api_request'])
        import re
        with open(os.path.join(ROOT, 'plugin.yaml')) as f:
            block = re.search(r'^provides_hooks:\n((?:  - .+\n)+)', f.read(), re.M).group(1)
        self.assertEqual(sorted(re.findall(r'- (\S+)', block)), sorted(hooks))
        self.assertEqual((kw['name'], kw['label'], kw['adapter_factory']), ('ride', 'Ride', adapter.RideAdapter))
        self.assertEqual(kw['allowed_users_env'], 'RIDE_ALLOWED_USERS')
        self.assertEqual(kw['display_tier'], 'minimal')
        self.assertEqual(kw['display_overrides'], {'interim_assistant_messages': True, 'streaming': False})
        self.assertIn('1 to 3 short spoken sentences', kw['platform_hint'])
        self.assertNotIn('trusted_inbound', kw)
        self.assertTrue(kw['check_fn']())

    def test_engine_resolution(self):
        self.assertEqual(adapter.resolve_engine('direct', {'provider': 'elevenlabs'}), 'direct')
        self.assertEqual(adapter.resolve_engine('HERMES', {}), 'hermes')
        self.assertEqual(adapter.resolve_engine('auto', {'provider': 'edge'}), 'direct')
        with mock.patch.object(adapter.importlib.util, 'find_spec', return_value=object()):
            self.assertEqual(adapter.resolve_engine(None, {'provider': 'elevenlabs'}), 'hermes')
        with mock.patch.object(adapter.importlib.util, 'find_spec', return_value=None):
            self.assertEqual(adapter.resolve_engine('auto', {'provider': 'elevenlabs'}), 'direct')

    def test_config_follows_hermes_voice(self):
        cfg = adapter.core_config({}, {'tts': {'elevenlabs': {'voice_id': 'V123', 'streaming_model_id': 'M9'}}})
        self.assertEqual((cfg['voice_id'], cfg['tts_model_id'], cfg['port'], cfg['host']),
                         ('V123', 'M9', 8790, '127.0.0.1'))
        cfg = adapter.core_config({'voice_id': 'X', 'port': '9999', 'language': 'auto', 'end_of_turn_s': 9}, {})
        self.assertEqual((cfg['voice_id'], cfg['port'], cfg['stt_language'], cfg['end_of_turn_silence_s']),
                         ('X', 9999, '', 5.0))

    def test_config_v2_defaults_and_overrides(self):
        cfg = adapter.core_config({}, {})
        self.assertEqual((cfg['stt_route'], cfg['stt_language'], cfg['languages'], cfg['keyterms'],
                          cfg['end_of_turn_silence_s'], cfg['window_s'], cfg['wake_engine'], cfg['cue_dir']),
                         ('phone', 'en', 'en', '', 1.2, 10.0, 'sherpa', None))   # English locked
        cfg = adapter.core_config({'stt_route': 'SERVER', 'keyterms': 'Lana', 'wake_engine': 'transcript',
                                   'window_s': 999, 'language': 'fr'}, {})
        self.assertEqual((cfg['stt_route'], cfg['keyterms'], cfg['wake_engine'], cfg['window_s'], cfg['stt_language']),
                         ('server', 'Lana', 'transcript', 300.0, 'fr'))
        self.assertEqual(adapter.core_config({'stt_route': 'bogus', 'wake_engine': 'oww'}, {})['stt_route'], 'phone')

    def test_wake_word_setting(self):                 # 10 Nov, Hermes-Ride: public default "Hey Hermes"
        self.assertEqual(adapter.core_config({}, {})['wake_word'], 'hey hermes')
        self.assertEqual(adapter.core_config({'wake_word': 'Hey Lana'}, {})['wake_word'], 'hey lana')
        self.assertEqual(adapter.core_config({'wake_word': 'hey bogus'}, {})['wake_word'], 'hey hermes')

    def test_stt_engine_settings(self):               # round 3, B2: Whistle on the host, Scribe stays the default
        cfg = adapter.core_config({}, {})
        self.assertEqual((cfg['stt_engine'], cfg['stt_fallback'], cfg['whistle_python'], cfg['whistle_weights'],
                          cfg['whistle_language'], cfg['whistle_end_s'], cfg['whistle_timeout_s']),
                         ('scribe', 'scribe', 'python3', '', 'en', 0.9, 20.0))
        self.assertEqual(adapter.core_config({'whistle_end_s': '9'}, {})['whistle_end_s'], 3.0)   # clamped
        self.assertEqual(adapter.core_config({'whistle_end_s': '0'}, {})['whistle_end_s'], 0.3)
        cfg = adapter.core_config({'stt_engine': 'WHISTLE', 'stt_fallback': 'NONE', 'whistle_python': '/opt/x/python',
                                   'whistle_weights': '/w.cact', 'whistle_language': 'de',
                                   'whistle_timeout_s': '0'}, {})
        self.assertEqual((cfg['stt_engine'], cfg['stt_fallback'], cfg['whistle_python'], cfg['whistle_weights'],
                          cfg['whistle_language'], cfg['whistle_timeout_s']),
                         ('whistle', 'none', '/opt/x/python', '/w.cact', 'de', 1.0))
        with mock.patch.dict(os.environ, {'RIDE_STT_ENGINE': 'whistle', 'RIDE_WHISTLE_PYTHON': '/env/python'}):
            cfg = adapter.core_config({}, {})
            self.assertEqual((cfg['stt_engine'], cfg['whistle_python']), ('whistle', '/env/python'))
        self.assertEqual(adapter.core_config({'stt_engine': 'vosk'}, {})['stt_engine'], 'vosk')   # RideCore: scribe

    def test_whistle_paths_come_from_hermes_own_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('py', 'transcribe.py', 'whistle.cact'):
                open(os.path.join(tmp, name), 'w').close()
            cmd = f"{tmp}/py {tmp}/transcribe.py {{input_path}} {{output_path}} --language {{language}}"
            hermes = {'stt': {'providers': {'whistle': {'type': 'command', 'command': cmd}}}}
            cfg = adapter.core_config({}, hermes)
            self.assertEqual((cfg['whistle_python'], cfg['whistle_weights']),
                             (f'{tmp}/py', f'{tmp}/whistle.cact'))
            cfg = adapter.core_config({'whistle_weights': '/mine.cact'}, hermes)   # the setting wins
            self.assertEqual(cfg['whistle_weights'], '/mine.cact')
            os.remove(os.path.join(tmp, 'whistle.cact'))
            self.assertEqual(adapter.core_config({}, hermes)['whistle_weights'], '')

    def test_summary_prompt_setting(self):            # P3 (10 Nov): the Ride Summary question at reset, default on
        self.assertIs(adapter.core_config({}, {})['summary_prompt'], True)
        self.assertIs(adapter.core_config({'summary_prompt': 'false'}, {})['summary_prompt'], False)
        self.assertIs(adapter.core_config({'summary_prompt': 'off'}, {})['summary_prompt'], False)

    def test_cue_gain_setting(self):                  # fix round 3, R3-2
        self.assertEqual(adapter.core_config({}, {}).get('cue_gain'), 0.5)
        self.assertEqual(adapter.core_config({'cue_gain': '0.3'}, {}).get('cue_gain'), 0.3)
        self.assertEqual(adapter.core_config({'cue_gain': 7}, {}).get('cue_gain'), 1.0)
        self.assertEqual(adapter.core_config({'cue_gain': -1}, {}).get('cue_gain'), 0.0)

    def test_mic_base_db_setting(self):               # Fix E: "mic is too damn sensitive": the gate's loud threshold
        cc = lambda e: adapter.core_config(e, {}).get('vad_base_db')
        self.assertEqual(cc({}), -38.0)
        self.assertEqual(cc({'mic_base_db': '-42'}), -42.0)
        self.assertEqual(cc({'mic_base_db': -90}), -70.0)
        self.assertEqual(cc({'mic_base_db': 0}), -20.0)
        self.assertEqual(cc({'mic_base_db': 'junk'}), -38.0)
        self.assertEqual(cc({'mic_base_db': 'nan'}), -38.0)
        with mock.patch.dict(os.environ, {'RIDE_MIC_BASE_DB': '-33'}):
            self.assertEqual(cc({}), -33.0)

    def test_tone_first_setting(self):                # WP2, P3: the doo-doo lead-in after 'thinking'
        self.assertEqual(adapter.core_config({}, {}).get('tone_first_s'), 1.2)
        self.assertEqual(adapter.core_config({'tone_first_s': '3'}, {}).get('tone_first_s'), 3.0)
        self.assertEqual(adapter.core_config({'tone_first_s': 99}, {}).get('tone_first_s'), 30.0)
        self.assertEqual(adapter.core_config({'tone_first_s': -1}, {}).get('tone_first_s'), 0.0)
        self.assertEqual(adapter.core_config({'tone_first_s': 'junk'}, {}).get('tone_first_s'), 1.2)

    def test_play_buffer_setting(self):               # the phone holds the first segment of a reply this long (ms)
        cc = lambda e: adapter.core_config(e, {}).get('play_buffer_ms')
        self.assertEqual(cc({}), 250)
        self.assertEqual(cc({'play_buffer_ms': '400'}), 400)
        self.assertEqual(cc({'play_buffer_ms': 0}), 0)
        self.assertEqual(cc({'play_buffer_ms': 99999}), 1000)
        self.assertEqual(cc({'play_buffer_ms': -5}), 0)
        self.assertEqual(cc({'play_buffer_ms': 'junk'}), 250)
        self.assertEqual(cc({'play_buffer_ms': 'nan'}), 250)
        self.assertIsInstance(cc({'play_buffer_ms': '120.7'}), int)
        with mock.patch.dict(os.environ, {'RIDE_PLAY_BUFFER_MS': '600'}):
            self.assertEqual(cc({}), 600)

    def test_orb_settings(self):                     # WP5: orb design (unknown names fall back on the phone), debug word
        self.assertEqual(adapter.core_config({}, {}).get('orb_design'), 'simple')
        self.assertEqual(adapter.core_config({'orb_design': ' Plasma '}, {}).get('orb_design'), 'plasma')
        self.assertEqual(adapter.core_config({'orb_design': ''}, {}).get('orb_design'), 'simple')
        self.assertIs(adapter.core_config({}, {}).get('show_word'), False)
        self.assertIs(adapter.core_config({'show_word': 'true'}, {}).get('show_word'), True)

    def test_orb_motion_setting(self):                # P4: how much the orb moves with the bike (0 = off, 1, up to 2)
        cc = lambda e: adapter.core_config(e, {}).get('orb_motion')
        self.assertEqual(cc({}), 1.0)
        self.assertEqual(cc({'orb_motion': '0'}), 0.0)
        self.assertEqual(cc({'orb_motion': 1.5}), 1.5)
        self.assertEqual(cc({'orb_motion': 9}), 2.0)
        self.assertEqual(cc({'orb_motion': -1}), 0.0)
        self.assertEqual(cc({'orb_motion': 'junk'}), 1.0)
        self.assertEqual(cc({'orb_motion': 'nan'}), 1.0)
        from unittest import mock
        with mock.patch.dict(os.environ, {'RIDE_ORB_MOTION': '0.5'}):
            self.assertEqual(cc({}), 0.5)

    def test_double_tap_setting(self):               # WP4, D4: two taps within this many ms toggle the mute
        self.assertEqual(adapter.core_config({}, {}).get('double_tap_ms'), 350)
        self.assertEqual(adapter.core_config({'double_tap_ms': '500'}, {}).get('double_tap_ms'), 500)
        self.assertEqual(adapter.core_config({'double_tap_ms': 5000}, {}).get('double_tap_ms'), 1000)
        self.assertEqual(adapter.core_config({'double_tap_ms': 10}, {}).get('double_tap_ms'), 150)
        self.assertEqual(adapter.core_config({'double_tap_ms': 'junk'}, {}).get('double_tap_ms'), 350)


@unittest.skipIf(adapter is None, SKIP or '')
class ToolHooks(unittest.IsolatedAsyncioTestCase):
    """W11 + AUD-V2-13: hooks with fake payloads; only ride sessions reach a core, only the core that owns the
    session, and only the tool name."""

    def fake(self, name, user='rider', active=True):
        test = self

        class Core:
            loop = asyncio.get_running_loop()

            def __init__(self):
                self.active = object() if active else None

            def bind_hook_turn(self, turn_id, turn=None):
                test.calls.append((name, 'bind', turn_id))

            def on_tool(self, tool, turn_id=None):
                test.calls.append((name, 'tool', tool, turn_id))

            def on_tool_done(self, tool, turn_id=None):
                test.calls.append((name, 'done', tool, turn_id))

            def on_llm_usage(self, u, turn_id=None):
                test.calls.append((name, 'usage', u, turn_id))

        class FakeAdapter:
            core = Core()
            user_id = user
        a = FakeAdapter()
        self.adapters.append(a)
        adapter._ADAPTERS.add(a)
        return a

    async def asyncSetUp(self):
        self.calls, self.adapters = [], []
        adapter._RIDE_SESSIONS.clear()

    async def asyncTearDown(self):
        for a in self.adapters:
            adapter._ADAPTERS.discard(a)
        adapter._RIDE_SESSIONS.clear()

    async def flush(self):
        for _ in range(3):
            await asyncio.sleep(0)

    async def test_only_ride_sessions(self):
        self.fake('A')
        secret_args = {'query': 'private search terms', 'api_key': 'sk-should-never-travel'}
        adapter._on_pre_tool_call(tool_name='web_search', args=secret_args, session_id='s-tg', turn_id='t1')
        adapter._on_pre_llm_call(session_id='s-tg', platform='telegram', turn_id='t1')
        adapter._on_pre_tool_call(tool_name='web_search', args=secret_args, session_id='s-tg', turn_id='t1')
        await self.flush()
        self.assertEqual(self.calls, [])                       # unknown / non-ride sessions are ignored
        adapter._on_pre_llm_call(session_id='s-ride', platform='ride', turn_id='t2', sender_id='rider')
        adapter._on_pre_tool_call(tool_name='delegate_task', args=secret_args, session_id='s-ride', turn_id='t2')
        adapter._on_post_tool_call(tool_name='delegate_task', args=secret_args, result='secret result',
                                   session_id='s-ride', turn_id='t2', status='ok')
        adapter._on_post_api_request(session_id='s-ride', platform='ride', turn_id='t2', usage={
            'input_tokens': 1200, 'output_tokens': 40, 'cache_read_tokens': 800, 'raw': 'x'})
        adapter._on_post_api_request(session_id='s-tg', platform='telegram', usage={'input_tokens': 5})
        await self.flush()
        self.assertEqual(self.calls, [('A', 'bind', 't2'), ('A', 'tool', 'delegate_task', 't2'),
                                      ('A', 'done', 'delegate_task', 't2'),
                                      ('A', 'usage', {'input_tokens': 1200, 'output_tokens': 40,
                                                      'cache_read_tokens': 800}, 't2')])
        self.assertNotIn('private', repr(self.calls))

    async def test_learned_session_with_explicit_other_platform_is_rejected(self):     # AUD-V2-13
        self.fake('A')
        adapter._on_pre_llm_call(session_id='s1', platform='ride', turn_id='t1', sender_id='rider')
        await self.flush()
        self.calls.clear()
        adapter._on_post_api_request(session_id='s1', platform='telegram', turn_id='t1', usage={'input_tokens': 5})
        adapter._on_pre_tool_call(tool_name='terminal', session_id='s1', platform='telegram', turn_id='t1')
        await self.flush()
        self.assertEqual(self.calls, [])

    async def test_events_go_to_the_owning_core_only(self):                            # AUD-V2-13
        self.fake('A', user='rider')
        self.fake('B', user='rider', active=False)               # another profile's ride adapter, idle
        self.fake('C', user='someone_else')                       # busy, but not this sender's
        adapter._on_pre_llm_call(session_id='s1', platform='ride', turn_id='t1', sender_id='rider')
        adapter._on_pre_tool_call(tool_name='web_search', session_id='s1', turn_id='t1')
        await self.flush()
        self.assertEqual([c[0] for c in self.calls], ['A', 'A'])
        self.calls.clear()
        self.adapters[1].core.active = object()                  # now ambiguous: two busy adapters for 'rider'
        adapter._on_pre_llm_call(session_id='s2', platform='ride', turn_id='t9', sender_id='rider')
        adapter._on_pre_tool_call(tool_name='web_search', session_id='s2', turn_id='t9')
        await self.flush()
        self.assertEqual(self.calls, [])                         # never broadcast
        adapter._on_pre_tool_call(tool_name='terminal', session_id='s1', turn_id='t1')
        await self.flush()
        self.assertEqual([c[0] for c in self.calls], ['A'])      # the bound session still goes to its owner

    async def test_hook_from_another_thread(self):
        import threading
        self.fake('A')
        adapter._on_pre_llm_call(session_id='s-r', platform='ride', sender_id='rider')
        th = threading.Thread(target=lambda: adapter._on_pre_tool_call(tool_name='web_extract', session_id='s-r'))
        th.start()
        th.join()
        for _ in range(20):
            if any(c[1] == 'tool' for c in self.calls):
                break
            await asyncio.sleep(0.01)
        self.assertEqual([c for c in self.calls if c[1] == 'tool'], [('A', 'tool', 'web_extract', None)])


class _Turn:
    def __init__(self, tid=1):
        self.id, self.rec = tid, {}


@unittest.skipIf(adapter is None, SKIP or '')
class Lifecycle(unittest.IsolatedAsyncioTestCase):
    engine = 'direct'

    async def asyncSetUp(self):
        from gateway.config import PlatformConfig
        self._tmp = tempfile.TemporaryDirectory(prefix='hermes-ride-adapter-', ignore_cleanup_errors=True)
        self.secrets = dict(SECRETS)
        self.patches = [
            mock.patch.object(adapter, '_secret', lambda n: self.secrets.get(n)),
            mock.patch.object(adapter, '_hermes_config', lambda: {'approvals': {'timeout': 120}}),
            mock.patch.dict(os.environ, {'HERMES_HOME': self._tmp.name}),
        ]
        for p in self.patches:
            p.start()
        self.a = adapter.RideAdapter(PlatformConfig(enabled=True, extra={'port': 0, 'tts_engine': self.engine}))
        self.events = []

        async def capture(event):
            self.events.append(event)
        self.a.handle_message = capture

    async def asyncTearDown(self):
        await self.a.disconnect()
        for p in self.patches:
            p.stop()
        self._tmp.cleanup()   # TemporaryDirectory(ignore_cleanup_errors=True)

    async def test_connect_serves_page_and_stops(self):
        self.assertTrue(await self.a.connect())
        from aiohttp import ClientSession
        async with ClientSession() as s:
            async with s.get(f'http://127.0.0.1:{self.a.server.port}/healthz') as r:
                self.assertEqual(r.status, 200)
            async with s.get(f'http://127.0.0.1:{self.a.server.port}/worklet.js') as r:
                self.assertEqual(r.status, 403)
        self.assertEqual(self.a.engine, self.engine)
        self.assertTrue(os.path.isdir(os.path.join(self._tmp.name, 'plugin-data', 'hermes-ride', 'logs')))

    async def test_missing_access_key_is_fatal(self):
        self.secrets.pop('RIDE_ACCESS_KEY')
        self.assertFalse(await self.a.connect())
        self.assertIsNone(self.a.server)

    async def test_submit_builds_gateway_event_and_completion_ends_turn(self):
        from gateway.platforms.event import MessageType, ProcessingOutcome
        self.assertTrue(await self.a.connect())
        turn = _Turn(7)
        done = []
        self.a.core.turn_done = lambda t, ok=True: done.append((t, ok))
        await self.a._submit(turn, '[Ride voice mode: ...]\nhow fast', {'rider_text': 'how fast', 'sensors': {}})
        ev = self.events[0]
        self.assertEqual((ev.text, ev.source.chat_id, ev.source.user_id, ev.source.chat_type),
                         ('[Ride voice mode: ...]\nhow fast', 'ride', 'rider', 'dm'))
        self.assertEqual(ev.message_type, MessageType.VOICE if self.engine == 'hermes' else MessageType.TEXT)
        self.assertEqual(ev.metadata, {})     # native admission refuses unknown metadata keys
        await self.a.on_processing_start(ev)
        self.assertIn('agent_start', turn.rec)
        await self.a.on_processing_complete(ev, ProcessingOutcome.SUCCESS)
        self.assertEqual(done, [(turn, True)])
        await self.a._submit(turn, 'two', {'clarify_answer': True})     # clarify answers are not turns
        self.assertEqual(self.events[1].message_type, MessageType.TEXT)
        self.assertNotIn(self.events[1].message_id, self.a._event_turns)

    async def test_approval_is_spoken_never_resolved(self):
        self.assertTrue(await self.a.connect())
        said = []

        async def reply_text(text, speak=True):
            said.append((text, speak))
            return True
        self.a.core.reply_text = reply_text
        from gateway.platforms.base import ExecApprovalPrompt
        self.assertTrue(type(self.a).supports_exec_approval_buttons())
        prompt = ExecApprovalPrompt(chat_id='ride', session_key='k', text='rm -rf x', actions=[], command='rm -rf x',
                                    description='dangerous', smart_denied=False, request_id='r1')
        with mock.patch('tools.approval.resolve_gateway_approval') as resolve:
            res = await self.a._send_exec_approval_prompt(prompt)
            resolve.assert_not_called()
        self.assertTrue(res.success)
        self.assertIn("can't take approvals by voice", said[0][0])
        self.assertIn('about 2 minutes', said[0][0])
        self.assertTrue(said[0][1])

    async def test_clarify_is_spoken_and_opens_passthrough(self):
        self.assertTrue(await self.a.connect())
        said = []

        async def reply_text(text, speak=True):
            said.append(text)
            return True
        self.a.core.reply_text = reply_text
        with mock.patch('tools.clarify_gateway.mark_awaiting_text') as mark:
            res = await self.a.send_clarify('ride', 'Which **route**?', ['Coast', 'Hills'], 'c1', 'k')
            mark.assert_called_once_with('c1')
        self.assertTrue(res.success)
        self.assertEqual(said, ['Which route? Options: 1: Coast; 2: Hills. Say the one you want.'])
        self.assertTrue(self.a.core.passthrough)

    async def test_send_and_voice_files(self):
        self.assertTrue(await self.a.connect())
        got = []

        async def reply_text(text, speak=True):
            got.append(('text', text, speak))
            return True

        async def audio_segment(data, mime):
            got.append(('seg', data, mime))
            return True
        self.a.core.reply_text, self.a.core.audio_segment = reply_text, audio_segment
        self.a.core.active = object()     # a turn is running
        self.assertTrue((await self.a.send('ride', 'Hello there.')).success)
        p = os.path.join(self._tmp.name, 'reply.mp3')
        with open(p, 'wb') as f:
            f.write(b'ID3fake')
        self.assertTrue((await self.a.send_voice('ride', p)).success)
        self.assertFalse((await self.a.send_voice('ride', p + '.missing')).success)
        self.assertEqual(got, [('text', 'Hello there.', self.engine != 'hermes'), ('seg', b'ID3fake', 'audio/mpeg')])

    async def test_tts_gates(self):
        from gateway.platforms.base import AudioFormat
        self.assertTrue(await self.a.connect())
        hermes = self.engine == 'hermes'
        self.assertEqual(self.a._should_auto_tts_for_chat('ride'), hermes)
        self.assertFalse(self.a._should_auto_tts_for_chat('other'))
        self.assertEqual(self.a.supports_streaming_tts('ride', AudioFormat()), hermes)
        self.assertIsNone(await self.a.begin_streaming_tts('ride', AudioFormat()))   # no turn running

    async def test_not_connected_send_fails_cleanly(self):
        res = await self.a.send('ride', 'hi')
        self.assertFalse(res.success)


def read_jsonl(path):
    import json
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


async def _gateway_status_send(a, chat_id, status_key, content, metadata=None):
    """Hermes's agent-status egress, as in gateway/run.py ``_send_or_update_status_coro`` (copied, not imported:
    importing gateway.run loads the host's secret manager into the test process). The source check below keeps
    this copy honest."""
    sender = getattr(a, 'send_or_update_status', None)
    if callable(sender):
        return await sender(chat_id, status_key, content, metadata=metadata)
    return await a.send(chat_id, content, metadata=metadata)


@unittest.skipIf(adapter is None, SKIP or '')
class InternalNotices(unittest.IsolatedAsyncioTestCase):      # fix round 3, R3-1
    """Status lines (memory recall indicator, retry diagnostics) and the background review summary are never
    spoken; the agent's answers, its interim lines and real errors are."""

    async def asyncSetUp(self):
        from gateway.config import PlatformConfig
        self._tmp = tempfile.TemporaryDirectory(prefix='hermes-ride-adapter-', ignore_cleanup_errors=True)
        self.patches = [mock.patch.object(adapter, '_secret', lambda n: SECRETS.get(n)),
                        mock.patch.object(adapter, '_hermes_config', lambda: {}),
                        mock.patch.dict(os.environ, {'HERMES_HOME': self._tmp.name})]
        for p in self.patches:
            p.start()
        self.a = adapter.RideAdapter(PlatformConfig(enabled=True, extra={'port': 0, 'tts_engine': 'direct'}))
        self.assertTrue(await self.a.connect())
        self.said = []

        async def reply_text(text, speak=True):
            self.said.append(text)
            return True
        self.a.core.reply_text = reply_text

    async def asyncTearDown(self):
        await self.a.disconnect()
        for p in self.patches:
            p.stop()
        self._tmp.cleanup()

    def test_hermes_still_routes_status_through_send_or_update_status(self):
        with open(os.path.join(HERMES_ROOT, 'gateway', 'run.py')) as f:
            src = f.read()
        self.assertRegex(src, r'getattr\(adapter, "send_or_update_status", None\)')
        with open(os.path.join(HERMES_ROOT, 'gateway', 'run_turn_progress.py')) as f:
            self.assertIn('_send_or_update_status_coro(ctx._status_adapter', f.read())

    async def test_status_lines_and_review_are_not_spoken(self):
        from gateway.warning_notifications import DiagnosticText
        await _gateway_status_send(self.a, 'ride', 'lifecycle', '\U0001f9e0 Hindsight — recalled 3 memories')
        await _gateway_status_send(self.a, 'ride', 'lifecycle', DiagnosticText(
            '⚠️ Empty response from model — retrying (1/3) in 5s'))
        send, release, scheduled = self._review_producer()      # AUD-R3-04: the review through its real producer
        release()
        send("\U0001f4be Self-improvement review: Skill 'hermes-agent' patched")
        send('\U0001f4be Memory updated')
        r = [await c for c in scheduled]
        self.assertTrue(r and all(x.success for x in r))
        self.assertEqual(self.said, [])
        notes = [rec for rec in read_jsonl(os.path.join(self._tmp.name, 'plugin-data', 'hermes-ride', 'logs',
                                                        'turns.jsonl')) if rec.get('event') == 'internal_notice']
        self.assertEqual([n['kind'] for n in notes], ['status', 'status', 'review', 'review'])
        self.assertTrue(all('text' not in n and 'reply' not in n for n in notes))   # never the text itself

    async def test_answers_interim_lines_and_errors_are_spoken(self):
        await self.a.send('ride', 'Let me check the forecast.', metadata={'_interim_send': True})
        await self.a.send('ride', 'Tomorrow is sunny, high of 24.')
        await self.a.send('ride', '⚠️ Sorry, the model provider failed. Try again in a minute.')
        await self.a.send('ride', 'Saving to disk \U0001f4be is done.')     # glyph not at the start: an answer
        self.assertEqual(len(self.said), 4)

    # ---- AUD-R3-04 (fix round 4): the review is recognised by its producer, not by its leading glyph
    def _review_producer(self):
        """The real gateway producer, gateway.run_turn_runner.TurnRunner._make_bg_review_callbacks, on a minimal
        runner whose _send_status_text/_schedule do what the real ones do (the coroutine is created inside the
        producer and awaited later on the loop). gateway.run is stubbed with copies of its two metadata helpers
        (importing it loads the host's secret manager); the source check below pins them."""
        import types
        from gateway.run_turn_runner import TurnRunner
        run_stub = types.ModuleType('gateway.run')
        run_stub._interim_metadata = lambda md=None: dict(md or {}, _interim_send=True)
        run_stub._non_conversational_metadata = lambda md=None, *, platform=None: md
        scheduled = []
        ctx = types.SimpleNamespace(source=types.SimpleNamespace(platform='ride'), _status_thread_metadata=None,
                                    _status_adapter=self.a, _status_chat_id='ride')
        fake = types.SimpleNamespace(_ctx=ctx, _approval_owner=None, _status_live=lambda: True,
                                     _schedule=lambda coro, log_message, loop=None: scheduled.append(coro))
        fake._send_status_text = lambda text, md, log: TurnRunner._send_status_text(fake, text, md, log)
        with mock.patch.dict(sys.modules, {'gateway.run': run_stub}):
            send, release = TurnRunner._make_bg_review_callbacks(fake)
        return send, release, scheduled

    async def test_review_from_the_gateway_producer_is_never_spoken_whatever_its_text(self):
        send, release, scheduled = self._review_producer()
        send('Memory updated')                                  # no glyph: held until the reply is delivered
        release()                                               # the adapter's post-delivery hook
        send("Self-improvement review: Skill 'hermes-agent' patched")   # after release: delivered directly
        send('\U0001f4be Memory updated')
        self.assertEqual(len(scheduled), 3)
        results = [await c for c in scheduled]
        self.assertTrue(all(r.success for r in results))
        self.assertEqual(self.said, [])
        notes = [rec for rec in read_jsonl(os.path.join(self._tmp.name, 'plugin-data', 'hermes-ride', 'logs',
                                                        'turns.jsonl')) if rec.get('event') == 'internal_notice']
        self.assertEqual([n['kind'] for n in notes], ['review', 'review', 'review'])

    async def test_a_genuine_interim_line_starting_with_the_glyph_is_spoken(self):
        await self.a.send('ride', '\U0001f4be Saved your route for tomorrow.', metadata={'_interim_send': True})
        self.assertEqual(self.said, ['\U0001f4be Saved your route for tomorrow.'])

    def test_glyph_rule_is_only_the_backup_when_the_producer_is_unknown(self):
        self.assertTrue(getattr(adapter, '_review_producer_known', lambda: False)())
        with mock.patch.object(adapter, '_review_producer_known', lambda: False, create=True):
            self.assertEqual(adapter._notice_kind({'_interim_send': True}, '\U0001f4be Memory updated')
                             if hasattr(adapter, '_notice_kind') else None, 'review')
            self.assertIsNone(adapter._notice_kind({'_interim_send': True}, 'Memory updated')
                              if hasattr(adapter, '_notice_kind') else 'x')

    def test_the_producer_and_its_metadata_are_still_what_the_adapter_expects(self):
        with open(os.path.join(HERMES_ROOT, 'gateway', 'run.py')) as f:
            src = f.read()
        self.assertIn('merged["_interim_send"] = True', src)
        self.assertRegex(src, r'if _gateway_platform_value\(platform\) != "discord":\n\s+return metadata')
        with open(os.path.join(HERMES_ROOT, 'gateway', 'run_turn_runner.py')) as f:
            src = f.read()
        self.assertIn('def _make_bg_review_callbacks(self):', src)
        self.assertIn('def deliver(message: str) -> None:', src)


@unittest.skipIf(adapter is None, SKIP or '')
class LifecycleHermesEngine(Lifecycle):
    engine = 'hermes'

    async def test_streaming_handle_round_trip(self):
        from gateway.platforms.base import AudioFormat
        self.assertTrue(await self.a.connect())
        core = self.a.core
        stream = {'turn': None}
        calls = []
        core.pcm_begin = lambda r, c, w: calls.append(('begin', r, c, w)) or stream
        core.pcm_write = lambda s, chunk: calls.append(('write', len(chunk)))
        core.pcm_end = lambda s, flush=True: calls.append(('end', flush))
        h = await self.a.begin_streaming_tts('ride', AudioFormat(sample_rate=24000))
        await self.a.write_streaming_tts(h, b'\x00' * 480)
        await self.a.finish_streaming_tts(h)
        await self.a.abort_streaming_tts(h, error='late')      # idempotent after finish
        self.assertEqual(calls, [('begin', 24000, 1, 2), ('write', 480), ('end', True)])


class _Store:
    """The gateway session store, as far as the reset reads it: key -> session id."""
    def __init__(self):
        self.ids = {}

    def peek_session_id(self, key):
        return self.ids.get(key)


class _Runner:
    """Fake GatewayRunner: the /new path the reset calls (``_handle_reset_command``) records the order and does what
    the real one does to what the adapter can see: a new session id, the lane's overrides cleared (the funnel)."""
    def __init__(self, order, fail=False):
        self.order, self.fail, self.events = order, fail, []
        self.session_store = _Store()
        self.overrides = {}

    def _session_key_for_source(self, source):
        return f'agent:main:ride:dm:{source.chat_id}'

    def _profile_name_for_source(self, source, adapter_profile=None):   # build_source asks the runner
        return adapter_profile

    def _session_model_override(self, key):
        return self.overrides.get(key)

    async def _handle_reset_command(self, event):
        self.order.append('new')
        self.new_saw_rows = dict(self.async_session_store.rows) if hasattr(self, 'async_session_store') else None
        self.events.append(event)
        if self.fail:
            raise RuntimeError('store locked')
        if getattr(self, 'late_reply', None) and hasattr(self, 'async_session_store'):   # the interrupted turn's reply,
            self.async_session_store.rows.setdefault('S1', []).append(              # flushed while /new stops it
                {'role': 'assistant', 'content': self.late_reply})
        key = self._session_key_for_source(event.source)
        self.session_store.ids[key] = 'S2'
        self.overrides.pop(key, None)
        return 'Session reset! (banner the rider must not hear)'


class _AsyncStore:
    """The gateway's async session store, as far as the Ride Summary prompt uses it: append_to_transcript."""
    def __init__(self, order, fail=False):
        self.order, self.fail, self.rows = order, fail, {}

    async def append_to_transcript(self, session_id, message, skip_db=False):
        self.order.append('prompt')
        if self.fail:
            raise RuntimeError('db locked')
        self.rows.setdefault(session_id, []).append(message)


@unittest.skipIf(adapter is None, SKIP or '')
class ResetLane(unittest.IsolatedAsyncioTestCase):
    """WP6 (D5, P6) through the adapter: the phone's {t:'reset'} on the real page socket -> interrupt the lane's turn
    (cancel_session_processing) -> the gateway's own /new -> RideCore state reset -> {t:'reset_done'}."""

    engine = 'direct'

    async def asyncSetUp(self):
        await Lifecycle.asyncSetUp(self)               # the same adapter, secrets and data dir as Lifecycle
        self.order = []
        self.cancels = []

        async def cancel(key, **kw):
            self.order.append('interrupt')
            self.cancels.append((key, kw))
            if getattr(self, 'cancel_fails', False):
                raise RuntimeError('task stuck')
        self.a.cancel_session_processing = cancel
        self.runner = _Runner(self.order)
        self.a.gateway_runner = self.runner
        self.assertTrue(await self.a.connect())
        key = 'agent:main:ride:dm:ride'
        self.runner.session_store.ids[key] = 'S1'
        self.runner.overrides[key] = {'model': 'pinned-model', 'provider': 'p'}
        real_reset = self.a.core.reset_state

        def reset_state():
            self.order.append('state')
            return real_reset()
        self.a.core.reset_state = reset_state
        self.said = []

        async def reply_text(text, speak=True):
            self.said.append(text)
            return True
        self.a.core.reply_text = reply_text

    async def page(self):
        from aiohttp import ClientSession
        self.http = ClientSession()
        port = self.a.server.port
        ws = await self.http.ws_connect(f'ws://127.0.0.1:{port}/ws', headers={'Cookie': 'rk=' + SECRETS['RIDE_ACCESS_KEY']})
        hello = await ws.receive_json(timeout=5)
        return ws, hello['epoch']

    async def asyncTearDown(self):
        if getattr(self, 'http', None) is not None:
            await self.http.close()
        await Lifecycle.asyncTearDown(self)

    async def answer(self, ws, timeout=5.0):
        while True:
            m = await ws.receive_json(timeout=timeout)
            if m.get('t') in ('reset_done', 'reset_failed'):
                self.order.append(m['t'])
                return m

    def resets(self):
        return [r for r in read_jsonl(os.path.join(self._tmp.name, 'plugin-data', 'hermes-ride', 'logs', 'turns.jsonl'))
                if r.get('event') in ('reset', 'reset_refused')]

    async def test_order_interrupt_new_state_done(self):
        ws, epoch = await self.page()
        await ws.send_json({'t': 'reset', 'epoch': epoch})
        m = await self.answer(ws)
        self.assertEqual(m['t'], 'reset_done')
        self.assertEqual(self.order, ['interrupt', 'new', 'state', 'reset_done'])
        self.assertEqual(self.cancels, [('agent:main:ride:dm:ride', {'release_guard': True, 'discard_pending': True})])
        ev = self.runner.events[0]                       # the gateway's own /new, for the Ride lane, no title
        self.assertEqual((ev.text, ev.get_command(), ev.get_command_args(), ev.source.chat_id, ev.source.user_id,
                          ev.source.chat_type), ('/new', 'new', '', 'ride', 'rider', 'dm'))
        self.assertEqual(self.said, [])                  # the /new banner is never spoken
        r = self.resets()
        self.assertEqual([(x['event'], x['ok'], x['old'], x['new'], x['override_left']) for x in r],
                         [('reset', True, 'S1', 'S2', False)])   # new session, the lane's model override gone

    async def test_stale_epoch_runs_nothing(self):
        ws, epoch = await self.page()
        await ws.send_json({'t': 'reset', 'epoch': epoch + '0'})
        await ws.send_json({'t': 'reset'})
        for _ in range(100):
            if len(self.resets()) == 2:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(self.order, [])
        self.assertEqual([x['why'] for x in self.resets()], ['stale_epoch', 'stale_epoch'])
        self.assertEqual(self.runner.session_store.ids['agent:main:ride:dm:ride'], 'S1')

    async def test_interrupt_failure_stops_before_new(self):
        self.cancel_fails = True
        ws, epoch = await self.page()
        await ws.send_json({'t': 'reset', 'epoch': epoch})
        m = await self.answer(ws)
        self.assertEqual((m['t'], m['step']), ('reset_failed', 'interrupt'))
        self.assertEqual(self.order, ['interrupt', 'state', 'reset_failed'])   # the lane is freed, /new never ran
        self.assertEqual(self.runner.session_store.ids['agent:main:ride:dm:ride'], 'S1')   # old session as it was

    async def test_new_failure_is_reported(self):
        self.runner.fail = True
        ws, epoch = await self.page()
        await ws.send_json({'t': 'reset', 'epoch': epoch})
        m = await self.answer(ws)
        self.assertEqual((m['t'], m['step']), ('reset_failed', 'new'))
        self.assertEqual(self.order, ['interrupt', 'new', 'state', 'reset_failed'])
        self.assertEqual([(x['ok'], x['step'], x['old']) for x in self.resets()], [(False, 'new', 'S1')])

    async def test_no_gateway_runner_interrupts_nothing(self):
        self.a.gateway_runner = None
        ws, epoch = await self.page()
        await ws.send_json({'t': 'reset', 'epoch': epoch})
        m = await self.answer(ws)
        self.assertEqual((m['t'], m['step']), ('reset_failed', 'unsupported'))
        self.assertEqual(self.order, ['state', 'reset_failed'])

    # ---- P3 (10 Nov): just before the session switch, no summary is written: a 'Ride Summary' yes / no question is
    #      left as the old session's last message, and the rider can click to confirm
    def ridden(self, rides=('ride-20261110-101010',)):
        self.store = _AsyncStore(self.order)
        self.runner.async_session_store = self.store
        self.a.core.session_turns = 3
        self.a.core.session_rides = list(rides)

    def prompt_logs(self):
        return [r for r in read_jsonl(os.path.join(self._tmp.name, 'plugin-data', 'hermes-ride', 'logs', 'turns.jsonl'))
                if r.get('event') == 'summary_prompt']

    async def test_summary_prompt_is_the_old_sessions_last_row_after_new(self):
        self.ridden()
        ws, epoch = await self.page()
        await ws.send_json({'t': 'reset', 'epoch': epoch})
        m = await self.answer(ws)
        self.assertEqual(m['t'], 'reset_done')
        self.assertEqual(self.order, ['interrupt', 'new', 'prompt', 'state', 'reset_done'])
        self.assertEqual(self.runner.new_saw_rows, {})                    # hackathon: written after /new stopped the turn
        (row,) = self.store.rows['S1']
        self.assertEqual(row['role'], 'assistant')
        self.assertIn('**Ride Summary?**', row['content'])
        f = re.search(r'::preview\{file="([^"]+)"\}', row['content']).group(1)
        self.assertTrue(os.path.isfile(f))
        self.assertTrue(f.startswith(os.path.join(self._tmp.name, 'plugin-data', 'hermes-ride', 'summaries')))
        with open(f, encoding='utf-8') as fh:
            html = fh.read()
        sends = re.findall(r'data-hermes-send="([^"]*)"', html)
        self.assertEqual(len(sends), 2)                                   # Yes and No, nothing else
        self.assertIn('ride-20261110-101010.jsonl', sends[0])             # Yes names this session's ride log
        self.assertIn('tools/ride_character.py', sends[0])                # ... and the installed tool to run
        self.assertIn('--rides-dir', sends[0])
        self.assertNotIn('::preview', sends[0])                           # no HTML poster any more
        self.assertNotIn('poster', sends[0].lower())
        self.assertIn('No ride summary', sends[1])
        self.assertIn('ride-20261110-101010.jsonl', row['content'])       # typing "yes" works too: the paths are there
        self.assertIn('Want a summary of it?', row['content'])
        self.assertNotIn('poster', row['content'].lower())
        self.assertEqual(self.said, [])                                   # nothing is spoken
        self.assertEqual([(x['ok'], x['session'], x['rides']) for x in self.prompt_logs()], [(True, 'S1', 1)])
        self.assertEqual((self.a.core.session_turns, self.a.core.session_rides), (0, []))   # the new session starts clean

    def test_summary_yes_prompt_has_the_three_parts_and_no_names(self):
        text = adapter.summary_yes_prompt(['/x/ride-1.jsonl'], '/x/poster.html')
        self.assertIn(adapter.ride_character_tool_path(), text)
        self.assertTrue(adapter.ride_character_tool_path().endswith(os.path.join('tools', 'ride_character.py')))
        for part in ('1. **Ride character**', '2. **The ride**', '3. **Ideas / Fix / Maintain**'):
            self.assertIn(part, text)
        self.assertIn('--rides-dir', text)
        self.assertIn('baseline', text.lower())
        self.assertIn('nothing to any file', text)
        self.assertNotIn('poster', text.lower())
        self.assertNotIn('::preview', text)
        self.assertNotIn('Mar' + 'tin', text)                                  # the public repo says "the rider"
        self.assertIn('the rider', text)
        none = adapter.summary_yes_prompt([], None)
        self.assertIn('no telemetry', none.lower())
        self.assertIn('parts 2 and 3', none)

    async def test_summary_prompt_comes_after_the_interrupted_turns_late_reply(self):
        # 11 Nov ride: the reset was held while a reply was in flight; that reply was stored 0.07 s AFTER the prompt
        # (two assistant rows in a row), so Desktop showed the prompt merged into a reply and the Yes card never worked
        self.ridden()
        self.runner.late_reply = 'That explains it: the reset had not happened yet'
        ws, epoch = await self.page()
        await ws.send_json({'t': 'reset', 'epoch': epoch})
        self.assertEqual((await self.answer(ws))['t'], 'reset_done')
        rows = self.store.rows['S1']
        self.assertEqual(len(rows), 2)
        self.assertIn('**Ride Summary?**', rows[-1]['content'])            # the prompt is the old session's LAST row
        self.assertTrue(rows[-1]['content'].startswith('**Ride Summary?**'))

    async def test_no_prompt_when_new_fails(self):                          # the old session lives on: no 'closed' card
        self.ridden()
        self.runner.fail = True
        ws, epoch = await self.page()
        await ws.send_json({'t': 'reset', 'epoch': epoch})
        self.assertEqual((await self.answer(ws))['t'], 'reset_failed')
        self.assertEqual(self.store.rows, {})
        self.assertNotIn('prompt', self.order)

    async def test_summary_prompt_failure_never_blocks_the_reset(self):
        self.ridden()
        self.store.fail = True
        ws, epoch = await self.page()
        await ws.send_json({'t': 'reset', 'epoch': epoch})
        m = await self.answer(ws)
        self.assertEqual(m['t'], 'reset_done')
        self.assertEqual(self.order, ['interrupt', 'new', 'prompt', 'state', 'reset_done'])
        self.assertEqual([(x['ok'], x['session']) for x in self.prompt_logs()], [(False, 'S1')])

    async def test_no_prompt_for_a_session_without_turns(self):
        self.ridden()
        self.a.core.session_turns = 0
        ws, epoch = await self.page()
        await ws.send_json({'t': 'reset', 'epoch': epoch})
        await self.answer(ws)
        self.assertEqual(self.order, ['interrupt', 'new', 'state', 'reset_done'])
        self.assertEqual(self.store.rows, {})

    async def test_no_prompt_when_switched_off(self):
        self.ridden()
        self.a._summary_prompt = False
        ws, epoch = await self.page()
        await ws.send_json({'t': 'reset', 'epoch': epoch})
        await self.answer(ws)
        self.assertEqual(self.order, ['interrupt', 'new', 'state', 'reset_done'])

    async def test_prompt_without_telemetry_says_so(self):
        self.ridden(rides=())
        ws, epoch = await self.page()
        await ws.send_json({'t': 'reset', 'epoch': epoch})
        await self.answer(ws)
        (row,) = self.store.rows['S1']
        f = re.search(r'::preview\{file="([^"]+)"\}', row['content']).group(1)
        with open(f, encoding='utf-8') as fh:
            yes = re.findall(r'data-hermes-send="([^"]*)"', fh.read())[0]
        self.assertIn('no telemetry', yes.lower())

    async def test_the_gateway_still_has_what_the_reset_calls(self):   # guards the private APIs P6 relies on
        import inspect as _inspect
        from gateway.platforms.base import BasePlatformAdapter
        sig = _inspect.signature(BasePlatformAdapter.cancel_session_processing)
        self.assertIn('release_guard', sig.parameters)
        self.assertIn('discard_pending', sig.parameters)
        path = os.path.join(HERMES_ROOT, 'gateway', 'slash_commands_session.py')
        with open(path) as f:
            src = f.read()
        self.assertIn('async def _handle_reset_command(self, event: MessageEvent)', src)
        self.assertIn('self._clear_conversation_scope(session_key, reason="session_reset")', src)
        self.assertIn('await self.async_session_store.reset_session(session_key)', src)
        path = os.path.join(HERMES_ROOT, 'gateway', 'session_transcript.py')     # P3: the Ride Summary row
        with open(path) as f:
            self.assertIn('def append_to_transcript(self, session_id: str, message: dict[str, Any], skip_db: bool = False)',
                          f.read())


if __name__ == '__main__':
    unittest.main()
