"""Turn-taking (W6), failure voice (W8), usage (W9), hint rules (W10), tool states (W11): real core + phone server,
fake phone (phone STT route: it sends vad edges, partials and committed text like the page), fake ElevenLabs, fake
agent. Timings are scaled down (EOT 0.3 / 0.6 / 1.2 s instead of 1.2 / 1.8 / 3.5 s)."""
import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import FakeAgent, FakeElevenLabs, Phone, read_jsonl, start_core, stop_core, tmpdir  # noqa: E402
import ride_core  # noqa: E402
import ride_logic as rl  # noqa: E402


class Rules(unittest.TestCase):
    def test_eot_wait(self):
        cases = {'How fast am I going?': 1.2, 'Take the next left.': 1.8, 'That was great!': 1.8,
                 'I want to go to the': 3.5, 'Find me a servo and': 3.5, 'so um': 3.5, 'Left, then right': 3.5,
                 'Wait -': 3.5, 'Let me think...': 3.5, '"Is it open?"': 1.2, '': 3.5, 'and.': 3.5}
        for t, want in cases.items():
            self.assertEqual(rl.eot_wait(t), want, t)
        self.assertEqual(rl.eot_wait('Why?', 0.3, 0.6, 1.2), 0.3)

    def test_filler(self):
        for t in ['Mm-hm.', 'Go on.', "I'm listening.", 'Yeah.', 'Sure!', 'Uh-huh', 'Okay.', '**Okay**', 'yeah, go on']:
            self.assertTrue(rl.is_filler(t), t)
        for t in ['Okay, turn left now.', 'Sure, it is 20 minutes away.', 'Go on to the next exit', 'No.',
                  'Yeah, the servo is open']:
            self.assertFalse(rl.is_filler(t), t)

    def test_usage_totals(self):
        tot = rl.usage_totals([{'stt_audio_s': 1800, 'tts_chars': 500, 'tts_audio_s': 30.5, 'llm_input_tokens': 10},
                               {'stt_audio_s': 1800, 'tts_chars': 500, 'llm_output_tokens': 7, 'llm_requests': 2}])
        self.assertEqual((tot['stt_audio_s'], tot['tts_chars'], tot['tts_audio_s'], tot['llm_input_tokens'],
                          tot['llm_output_tokens'], tot['llm_requests'], tot['turns']),
                         (3600, 1000, 30.5, 10, 7, 2, 2))
        self.assertEqual(tot['cost_usd']['stt'], 0.39)      # one hour of Scribe realtime
        self.assertEqual(tot['cost_usd']['tts'], 0.04)      # 1000 flash characters
        self.assertEqual(tot['cost_usd']['total'], 0.43)

    def test_hint_rules(self):                              # W10
        h = rl.PLATFORM_HINT
        for rule in ('1 to 3 short spoken sentences', 'No markdown, lists, tables, emoji, URLs',
                     'one direction at a time', 'from their current position only', 'ask for the destination',
                     'Never answer with a filler alone', 'if there is nothing to say, say nothing',
                     "Don't claim anything the sensors can't show", "don't give a false 'I can't'",
                     'offer to log it as a to-do'):
            self.assertIn(rule, h)


class Base(unittest.IsolatedAsyncioTestCase):
    cfg = {}
    agent_delay = 0.05

    async def asyncSetUp(self):
        self._tmp = tmpdir()
        self.tmp = self._tmp.name
        self.fake = await FakeElevenLabs().start()
        cfg = {'eot_question_s': 0.3, 'eot_default_s': 0.6, 'eot_open_s': 1.2, 'eot_max_s': 3.0,
               'hold_resume_s': 0.3, **self.cfg}
        self.srv, self.core, self.agent = await start_core(self.tmp, self.fake, FakeAgent(delay=self.agent_delay),
                                                           **cfg)
        self.base = str(self.srv.make_url('')).rstrip('/')
        self.phone = await Phone(self.base, caps={'wake': 'sherpa', 'stt': 'phone'}).connect()
        await self.until(lambda: self.core.route == 'phone')

    async def asyncTearDown(self):
        await self.phone.close()
        await stop_core(self.srv, self.core)
        await self.fake.close()
        self._tmp.cleanup()

    def log(self, event=None):
        recs = read_jsonl(os.path.join(self.tmp, 'logs', 'turns.jsonl'))
        return [r for r in recs if event is None or r.get('event') == event]

    async def until(self, cond, timeout=5.0):
        for _ in range(int(timeout / 0.01)):
            if cond():
                return
            await asyncio.sleep(0.01)
        self.fail('condition not reached')

    async def vad(self, s):
        await self.phone.send({'t': 'vad', 's': s, 'at': 0})

    async def say(self, text, stt_s=None, partial=True):
        """The phone's view of one utterance: speech starts, partials, speech stops, Scribe commits."""
        await self.vad(1)
        if partial:
            await self.phone.send({'t': 'partial', 'text': text[:12]})
        await self.vad(0)
        await self.phone.send({'t': 'committed', 'text': text, 'lang': 'en', 'stt_s': stt_s})

    def audio(self, turn):
        return self.phone.of('audio', turn=turn)

    async def play_out(self, turn):
        await self.phone.wait({'t': 'audio', 'turn': turn, 'seq': 0})
        await self.phone.send({'t': 'play', 'turn': turn, 'seq': 0})
        await self.phone.wait({'t': 'end', 'turn': turn})
        await self.phone.send({'t': 'idle', 'turn': turn})


class EndOfTurn(Base):
    async def test_question_goes_fast_open_sentence_waits(self):
        t0 = time.monotonic()
        await self.say('How fast am I going?')
        await self.until(lambda: self.agent.turns)
        self.assertLess(time.monotonic() - t0, 0.6)             # eot_question_s 0.3 after the end of speech
        await self.play_out(1)
        await self.say('I want to go to the')
        await asyncio.sleep(0.8)
        self.assertEqual(len(self.agent.turns), 1)              # mid-sentence: still waiting (eot_open_s 1.2)
        await self.say('petrol station near Bright.')
        await self.until(lambda: len(self.agent.turns) == 2)
        self.assertTrue(self.agent.turns[1][1].endswith('\nI want to go to the petrol station near Bright.'))
        self.assertEqual([r['parts'] for r in self.log('eot_joined')], [2])

    async def test_speech_during_the_wait_restarts_it_cap_ends_it(self):
        await self.say('Find me a servo.')
        await asyncio.sleep(0.3)
        await self.vad(1)                                       # talking again: VAD on and Scribe partials flowing
        for i in range(6):
            await self.phone.send({'t': 'partial', 'text': 'and one with ' + 'x' * (i + 1)})
            await asyncio.sleep(0.25)
        self.assertEqual(self.agent.turns, [])                  # never while he talks ...
        for i in range(12):
            await self.phone.send({'t': 'partial', 'text': 'still going ' + 'y' * (i + 1)})
            if self.agent.turns:
                break
            await asyncio.sleep(0.25)
        self.assertEqual(len(self.agent.turns), 1)              # ... but at most eot_max_s (3 s) after the commit
        await self.vad(0)

    async def test_vad_stuck_on_noise_does_not_block(self):
        """Live run 2: road noise above the energy threshold kept vad=1 and Scribe sent no partials for it."""
        await self.say('Find me a servo.')
        await self.vad(1)                                       # noise: VAD on, no partials ever
        t0 = time.monotonic()
        await self.until(lambda: self.agent.turns, timeout=3)
        self.assertLess(time.monotonic() - t0, 2.2)             # vad_trust_s (1.5 s), not eot_max_s
        await self.play_out(1)                                  # and the reply is not held forever either
        await self.vad(0)

    async def test_wait_counts_from_the_end_of_speech(self):
        await self.vad(1)
        await self.vad(0)
        await asyncio.sleep(0.5)                                # Scribe commits ~1.2 s after silence in real life
        t0 = time.monotonic()
        await self.phone.send({'t': 'committed', 'text': 'Turn left here.'})
        await self.until(lambda: self.agent.turns)
        self.assertLess(time.monotonic() - t0, 0.35)            # 0.6 s wait, 0.5 s of it already silent


class StaleReply(Base):
    agent_delay = 0.4

    async def test_hold_then_play_when_he_stops(self):
        await self.say('Where is the next fuel stop?')
        await self.until(lambda: self.agent.turns)
        await self.vad(1)                                       # he keeps talking while Lana thinks
        await self.phone.send({'t': 'partial', 'text': 'and also'})
        await self.phone.wait({'t': 'hold', 'turn': 1})
        self.assertEqual(self.audio(1), [])                     # kept back
        await asyncio.sleep(0.3)
        await self.vad(0)                                       # stops without saying anything committed
        await self.play_out(1)
        self.assertEqual(self.phone.of('discard'), [])
        rec = (await self._rec(1))
        self.assertIn('held', rec)
        self.assertIn('hold_released', rec)

    async def _rec(self, turn):
        await self.until(lambda: [r for r in self.log('turn') if r['turn'] == turn])
        return [r for r in self.log('turn') if r['turn'] == turn][0]

    async def test_noise_hold_is_released(self):
        await self.say('Where is the next fuel stop?')
        await self.until(lambda: self.agent.turns)
        await self.vad(1)                                       # noise right when the reply is ready: held ...
        await self.phone.wait({'t': 'hold', 'turn': 1})
        await self.play_out(1)                                  # ... then played: no partial ever confirmed speech
        self.assertEqual(self.phone.of('discard'), [])
        await self.vad(0)

    async def test_hold_then_discard_and_combine(self):
        await self.say('Where is the next fuel stop?')
        await self.until(lambda: self.agent.turns)
        await self.vad(1)
        await self.phone.wait({'t': 'hold', 'turn': 1})
        await self.phone.send({'t': 'partial', 'text': 'one with a'})
        await self.vad(0)
        await self.phone.send({'t': 'committed', 'text': 'One with a cafe please.'})
        await self.phone.wait({'t': 'discard', 'turn': 1})
        await self.play_out(2)
        self.assertEqual(self.audio(1), [])                     # the stale reply never reached the phone
        self.assertEqual(len(self.agent.turns), 2)
        text = self.agent.turns[1][1]
        self.assertTrue(text.endswith('\nWhere is the next fuel stop? One with a cafe please.'), text)
        self.assertIn(rl.STALE_NOTE, text)
        d = self.log('discarded_stale')[0]
        self.assertEqual((d['turn'], d['rider_text'], d['new_text'], d['why']),
                         (1, 'Where is the next fuel stop?', 'One with a cafe please.', 'kept_talking'))
        self.assertTrue(d['reply'])
        self.assertEqual((await self._rec(1))['delivery'], 'discarded_stale')

    async def test_replay_fragment_pause_fragment(self):
        """Fragment, pause, fragment: one combined answer is played, the stale one never is."""
        await self.say('Can you find me a good place.')        # ends with '.': submitted after 0.6 s
        await self.until(lambda: self.agent.turns)
        await asyncio.sleep(0.1)
        await self.vad(1)                                       # the second fragment starts while Lana thinks
        await self.phone.send({'t': 'partial', 'text': 'to stop for'})
        await asyncio.sleep(0.6)
        await self.vad(0)
        await self.phone.send({'t': 'committed', 'text': 'To stop for coffee near Bright.'})
        await self.play_out(2)
        self.assertEqual(len(self.agent.turns), 2)
        self.assertIn('Can you find me a good place. To stop for coffee near Bright.', self.agent.turns[1][1])
        self.assertEqual(self.audio(1), [])
        played = [m['turn'] for m in self.phone.msgs if m['t'] == 'audio']
        self.assertEqual(set(played), {2})
        self.assertEqual(len(self.log('discarded_stale')), 1)

    async def test_mid_sentence_pause_is_one_turn_without_any_discard(self):
        await self.say('Can you find me a good place to')       # open: waits 1.2 s
        await asyncio.sleep(0.7)
        await self.say('stop for coffee?')
        await self.play_out(1)
        self.assertEqual(len(self.agent.turns), 1)
        self.assertEqual(self.log('discarded_stale'), [])


class Fillers(Base):
    async def test_filler_reply_not_spoken_no_fallback(self):
        self.agent.reply = 'Mm-hm.'
        await self.say('So I was riding along the river')
        await self.phone.wait({'t': 'end', 'turn': 1})
        self.assertEqual(self.fake.tts_texts, [])
        self.assertEqual(self.phone.of('end', turn=1)[0]['segs'], 0)
        self.assertEqual([r['text'] for r in self.log('filler_suppressed')], ['Mm-hm.'])
        await self.until(lambda: self.log('turn'))
        self.assertNotIn('fallback', self.log('turn')[0])


class FailedTurn(Base):                                     # fix round 3, R3-1 (guard)
    async def test_a_failed_turn_with_only_internal_notices_still_says_a_short_error(self):
        """Guard (passes before and after): with the notices dropped by the adapter, a failed turn has no text and
        the core speaks its short fallback error."""
        self.agent.reply = None
        self.agent.hold = asyncio.Event()
        await self.say('What is the weather tomorrow?')
        await self.until(lambda: self.agent.turns)
        self.core.turn_done(self.agent.turns[0][0], ok=False)
        await self.phone.wait({'t': 'end', 'turn': 1})
        self.assertEqual(self.fake.tts_texts, [ride_core.FALLBACK_REPLY])
        self.agent.hold.set()


class BargeIn(Base):
    cfg = {}

    async def start_reply(self):
        self.agent.reply = 'One sentence here. Two sentences here. Three sentences here. Four sentences here.'
        self.fake.tts_delay = 0.15
        await self.say('Tell me about the road ahead.')
        await self.phone.wait({'t': 'audio', 'turn': 1, 'seq': 0})
        await self.phone.send({'t': 'play', 'turn': 1, 'seq': 0})

    async def test_three_words_started_during_audio_barge_in(self):
        await self.start_reply()
        await self.vad(1)
        await self.phone.send({'t': 'partial', 'text': 'wait stop there'})
        await self.phone.wait({'t': 'stop', 'turn': 1})
        await self.until(lambda: self.log('turn'))
        self.assertTrue(self.log('turn')[0]['barged_in'])

    async def test_backchannel_and_two_words_do_not(self):
        await self.start_reply()
        for p in ('yeah', 'yeah okay', 'yeah okay right', 'mm-hm sure cool'):
            await self.phone.send({'t': 'partial', 'text': p})
        await self.phone.send({'t': 'partial', 'text': 'hold on'})
        await self.phone.wait({'t': 'end', 'turn': 1})
        self.assertEqual(self.phone.of('stop'), [])

    async def test_speech_that_began_before_the_audio_does_not(self):
        self.agent.reply = 'One sentence here. Two sentences here. Three sentences here.'
        self.fake.tts_delay = 0.15
        await self.say('Tell me about the road ahead.')
        await self.phone.send({'t': 'partial', 'text': 'and'})      # a new utterance starts before the reply ...
        await asyncio.sleep(0.8)                                     # ... (quiet long enough: no hold)
        await self.phone.wait({'t': 'audio', 'turn': 1, 'seq': 0})
        await self.phone.send({'t': 'partial', 'text': 'and the weather there too'})
        await self.phone.wait({'t': 'end', 'turn': 1})
        self.assertEqual(self.phone.of('stop'), [])

    async def test_speech_during_the_cues_before_playback_does_not(self):     # AUD-V2-07
        # the audio has been sent but the phone is still playing the ready/done cue: no 'play' report yet
        self.agent.reply = 'One sentence here. Two sentences here. Three sentences here.'
        self.fake.tts_delay = 0.15
        await self.say('Tell me about the road ahead.')
        await self.phone.wait({'t': 'audio', 'turn': 1, 'seq': 0})
        await asyncio.sleep(0.4)                                     # quiet first: the reply is not held
        await self.vad(1)
        await self.phone.send({'t': 'partial', 'text': 'and the weather there too'})
        await asyncio.sleep(0.6)                 # well beyond the rule's 0.2 s slack (0.2 here was a coin-flip)
        self.assertEqual(self.phone.of('stop'), [])
        await self.phone.send({'t': 'play', 'turn': 1, 'seq': 0})   # the answer starts: same utterance still no
        await self.phone.send({'t': 'partial', 'text': 'and the weather there too please'})
        await asyncio.sleep(0.2)
        self.assertEqual(self.phone.of('stop'), [])
        await self.vad(0)
        await self.phone.send({'t': 'committed', 'text': 'and the weather there too please'})
        await self.vad(1)                                            # new speech after playback began: yes
        await self.phone.send({'t': 'partial', 'text': 'wait stop there'})
        await self.phone.wait({'t': 'stop', 'turn': 1})

    async def test_turn_logged_after_playback_ends(self):
        await self.say('How fast am I going?')
        await self.phone.wait({'t': 'end', 'turn': 1})
        await self.phone.send({'t': 'play', 'turn': 1, 'seq': 0})
        await asyncio.sleep(0.3)
        self.assertEqual(self.log('turn'), [])                  # still playing on the phone: not logged yet
        await self.phone.send({'t': 'idle', 'turn': 1})
        await self.until(lambda: self.log('turn'))
        self.assertFalse(self.log('turn')[0]['barged_in'])

    async def test_failed_playback_is_not_logged_as_delivered(self):    # AUD-V2-04
        await self.say('How fast am I going?')
        await self.phone.wait({'t': 'end', 'turn': 1})
        await self.phone.send({'t': 'play_error', 'turn': 1, 'seq': 0, 'why': 'NotAllowedError'})
        await self.phone.send({'t': 'idle', 'turn': 1, 'heard': False})
        await self.until(lambda: self.log('turn'))
        rec = self.log('turn')[0]
        self.assertEqual(rec['delivery'], 'play_failed')
        self.assertIn('play: NotAllowedError', rec['errors'])


class Timers(Base):
    cfg = {'extended_after_s': 0.2, 'working_after_s': 0.5}

    async def test_extended_then_working_once(self):
        self.agent.hold = asyncio.Event()
        await self.say('Plan me a route over the alps?')
        await self.phone.wait({'t': 'state', 's': 'working', 'turn': 1})
        await asyncio.sleep(0.3)
        self.agent.hold.set()
        await self.play_out(1)
        st = [(m['s'], m.get('why')) for m in self.phone.of('state') if m.get('turn') == 1]
        self.assertEqual(st.count(('extended', 'long')), 1)
        self.assertEqual(len([s for s in st if s[0] == 'working']), 1)
        self.assertLess(st.index(('extended', 'long')), st.index(('working', None)))

    async def test_tool_states_once_per_kind_before_audio(self):
        self.agent.hold = asyncio.Event()
        await self.say('Search for the weather in Bright?')
        await self.until(lambda: self.core.active is not None)
        self.core.bind_hook_turn('h1')                           # pre_llm_call (hook events carry turn_id)
        for name in ('web_search', 'web_search', 'browser_navigate', 'delegate_task', 'terminal'):
            self.core.on_tool(name, 'h1')
            self.core.on_tool_done(name, 'h1')
        await asyncio.sleep(0.05)
        self.agent.hold.set()
        await self.play_out(1)
        whys = [m.get('why') for m in self.phone.of('state', s='extended') if m.get('turn') == 1]
        self.assertEqual(whys, ['search', 'subagent', 'tool'])
        kinds = [(m['t'], m.get('s')) for m in self.phone.msgs if m.get('turn') == 1 and m['t'] in ('state', 'audio')]
        self.assertLess(kinds.index(('state', 'extended')), kinds.index(('audio', None)))
        await self.until(lambda: self.log('turn'))
        self.assertEqual(self.log('turn')[0]['tools_done'], 5)

    async def test_delayed_hook_event_of_a_previous_turn_is_ignored(self):    # AUD-V2-13
        self.agent.hold = asyncio.Event()
        await self.say('Search for the weather in Bright?')
        await self.until(lambda: self.core.active is not None)
        self.core.bind_hook_turn('t2')
        self.core.on_tool('web_search', 't1')                    # late event from the previous turn
        self.core.on_llm_usage({'input_tokens': 999}, 't1')
        self.core.on_tool('terminal', 't2')
        self.core.on_llm_usage({'input_tokens': 7}, 't2')
        await asyncio.sleep(0.05)
        self.assertEqual(self.core.active.rec.get('tools'), ['terminal'])
        self.assertEqual(self.core.active.usage.get('llm_input_tokens'), 7)
        self.agent.hold.set()
        await self.play_out(1)

    async def test_hook_event_without_a_turn_id_is_ignored(self):           # fix round 2, R9: fail closed
        self.agent.hold = asyncio.Event()
        await self.say('Search for the weather in Bright?')
        await self.until(lambda: self.core.active is not None)
        self.core.bind_hook_turn('h1')
        self.core.on_tool('web_search')                          # no turn_id: whose is it? nobody's
        self.core.on_llm_usage({'input_tokens': 999})
        self.core.on_tool('terminal', 'h1')
        await asyncio.sleep(0.05)
        self.assertEqual(self.core.active.rec.get('tools'), ['terminal'])
        self.assertIsNone(self.core.active.usage.get('llm_input_tokens'))
        self.agent.hold.set()
        await self.play_out(1)

    async def test_events_before_the_turn_is_bound_are_ignored(self):      # fix round 2, R9
        self.agent.hold = asyncio.Event()
        await self.say('Search for the weather in Bright?')
        await self.until(lambda: self.core.active is not None)
        self.core.on_tool('web_search', 'h-old')                 # a previous turn's event, current turn unbound
        self.core.on_llm_usage({'input_tokens': 999}, 'h-old')
        self.core.bind_hook_turn('h1')
        self.core.on_llm_usage({'input_tokens': 7}, 'h1')
        await asyncio.sleep(0.05)
        self.assertIsNone(self.core.active.rec.get('tools'))
        self.assertEqual(self.core.active.usage.get('llm_input_tokens'), 7)
        self.agent.hold.set()
        await self.play_out(1)

    async def test_a_late_pre_llm_call_of_the_previous_turn_cannot_claim_the_next(self):   # fix round 2, R9
        await self.say('How fast am I going?')
        await self.until(lambda: self.core.active is not None)
        self.core.bind_hook_turn('h1')
        await self.play_out(1)
        await self.until(lambda: self.core.active is None)
        self.agent.hold = asyncio.Event()
        await self.say('And how far to Bright?')
        await self.until(lambda: self.core.active is not None)
        self.core.bind_hook_turn('h1')                           # turn 1's pre_llm_call, delivered late
        self.core.on_tool('web_search', 'h1')
        await asyncio.sleep(0.05)
        self.assertIsNone(self.core.active.hook_turn)
        self.assertIsNone(self.core.active.rec.get('tools'))
        self.agent.hold.set()
        await self.play_out(2)

    async def test_binding_needs_the_turn_that_was_running_at_hook_time(self):   # fix round 2, R9 (new API)
        await self.say('How fast am I going?')
        await self.until(lambda: self.core.active is not None)
        t1 = self.core.active                                    # captured when turn 1's pre_llm_call fired
        await self.play_out(1)
        await self.until(lambda: self.core.active is None)
        self.agent.hold = asyncio.Event()
        await self.say('And how far to Bright?')
        await self.until(lambda: self.core.active is not None)
        self.core.bind_hook_turn('h-t1', t1)                     # the callback runs only now
        self.assertIsNone(self.core.active.hook_turn)
        self.agent.hold.set()
        await self.play_out(2)

    async def test_no_tool_state_outside_a_turn(self):
        self.core.on_tool('web_search')
        await asyncio.sleep(0.05)
        self.assertEqual(self.phone.of('state', s='extended'), [])


class Usage(Base):
    async def test_turn_usage_and_ride_summary(self):
        self.agent.hold = asyncio.Event()
        await self.say('How fast am I going?', stt_s=2.5)
        await self.until(lambda: self.core.active is not None)
        self.core.bind_hook_turn('h1')
        self.core.on_llm_usage({'input_tokens': 1500, 'output_tokens': 30, 'cache_read_tokens': 1000}, 'h1')
        self.core.on_llm_usage({'input_tokens': 200, 'output_tokens': 12}, 'h1')
        self.agent.hold.set()
        await self.play_out(1)
        await self.until(lambda: self.log('turn'))
        rec = self.log('turn')[0]
        reply = 'You are doing 62 km/h. Keep it steady.'
        self.assertEqual((rec['stt_audio_s'], rec['tts_chars'], rec['llm_input_tokens'], rec['llm_output_tokens'],
                          rec['llm_cache_read_tokens'], rec['llm_requests']),
                         (2.5, len(reply) - 1, 1700, 42, 1000, 2))     # two sentences, joined without the space
        self.assertGreater(rec['tts_audio_s'], 0)
        await self.phone.close()
        await self.until(lambda: self.log('ride_summary'))
        s = self.log('ride_summary')[0]
        self.assertEqual((s['turns'], s['stt_audio_s'], s['tts_chars'], s['why']), (1, 2.5, len(reply) - 1,
                                                                                    'page_closed'))
        self.assertGreater(s['cost_usd']['total'], 0)
        self.phone = await Phone(self.base).connect()


class UsageV2(Base):                                       # AUD-V2-12
    async def test_cumulative_audio_tail_and_in_flight_turns(self):
        await self.phone.send({'t': 'stt_usage', 's': 3.0})          # a probe that was no wake: billed, no turn
        await self.say('How fast am I going?')
        await self.phone.send({'t': 'stt_usage', 's': 2.5})
        await self.play_out(1)
        await self.until(lambda: self.log('turn'))
        self.assertEqual(self.log('turn')[0]['stt_audio_s'], 5.5)
        await self.phone.send({'t': 'stt_usage', 's': 1.0})          # the tail after the last commit
        self.agent.hold = asyncio.Event()
        await self.say('And how far to Bright?')
        await self.until(lambda: self.core.active is not None)
        self.core.bind_hook_turn('h2')
        self.core.on_llm_usage({'input_tokens': 100, 'output_tokens': 5}, 'h2')
        await self.phone.close()                                      # the page goes with turn 2 in flight
        await self.until(lambda: self.log('ride_summary'))
        s = self.log('ride_summary')[0]
        self.assertEqual((s['turns'], s['in_flight'], s['stt_audio_s'], s['llm_input_tokens']), (2, 1, 6.5, 100))
        self.agent.hold.set()
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.log('ride_summary')), 1)            # its late finish is not summarised again
        self.phone = await Phone(self.base).connect()

    async def test_shutdown_summary_includes_the_running_turn(self):
        self.agent.hold = asyncio.Event()
        await self.phone.send({'t': 'stt_usage', 's': 2.0})
        await self.say('How fast am I going?')
        await self.until(lambda: self.core.active is not None)
        await self.core.shutdown()
        s = self.log('ride_summary')[0]
        self.assertEqual((s['why'], s['turns'], s['in_flight'], s['stt_audio_s']), ('server_stopping', 1, 1, 2.0))

    async def test_usage_after_the_summary_is_reconciled(self):     # fix round 2, R8
        self.agent.hold = asyncio.Event()
        await self.say('How fast am I going?')
        await self.until(lambda: self.core.active is not None)
        self.core.bind_hook_turn('h2')
        self.core.on_llm_usage({'input_tokens': 100, 'output_tokens': 5}, 'h2')
        await self.phone.close()                                      # summary with the turn in flight
        await self.until(lambda: self.log('ride_summary'))
        self.assertEqual(self.log('ride_summary')[0]['llm_input_tokens'], 100)
        self.core.on_llm_usage({'input_tokens': 40, 'output_tokens': 7}, 'h2')   # usage that arrives after the summary
        self.agent.hold.set()
        await self.until(lambda: self.log('turn'))
        await self.until(lambda: self.log('ride_summary_reconciled'))
        r = self.log('ride_summary_reconciled')
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]['turn'], r[0]['llm_input_tokens'], r[0]['llm_output_tokens']), (1, 40, 7))
        self.assertEqual(self.log('turn')[0]['llm_input_tokens'], 140)    # the turn record has the whole turn
        self.phone = await Phone(self.base).connect()

    def test_bitrate_and_prices_from_config(self):
        core = self.core.__class__({'tts_output_format': 'mp3_44100_128', 'price_stt_per_hour': 1.0,
                                    'price_tts_per_1k_chars': 0.5}, None, lambda k: None)
        self.assertEqual(core.mp3_kbps(), 128)
        self.assertEqual(core.prices(), {'scribe_realtime_per_hour': 1.0, 'tts_flash_per_1k_chars': 0.5})


if __name__ == '__main__':
    unittest.main()
