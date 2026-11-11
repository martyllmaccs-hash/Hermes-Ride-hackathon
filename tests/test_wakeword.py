"""P1 (10 Nov, Hermes-Ride): the wake word is a setting. Public default "Hey Hermes"; "Hey Lana" only by config."""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))
from harness import FakeAgent, Phone, start_core, stop_core, tmpdir, FakeElevenLabs, read_jsonl  # noqa: E402
import ride_core  # noqa: E402
import ride_logic as rl  # noqa: E402


class WakeWordSetting(unittest.TestCase):
    def test_default_is_hey_hermes(self):
        w = rl.wake_word(None)
        self.assertEqual((w['key'], w['word'], w['name'], w['keywords']),
                         ('hey hermes', 'Hey Hermes', 'hermes', 'kws-words-hey-hermes.txt'))
        self.assertEqual(ride_core.DEFAULTS['wake_word'], 'hey hermes')

    def test_lana_by_setting_any_spelling(self):
        for s in ('Hey Lana', 'hey lana', ' HEY,  LANA ', 'lana', 'Hey Lana!'):
            w = rl.wake_word(s)
            self.assertEqual((w['key'], w['name'], w['keywords']), ('hey lana', 'lana', 'kws-words-hey-lana.txt'), s)

    def test_unknown_falls_back_to_default(self):
        for s in ('hey siri', 'jarvis', 123, ''):
            self.assertEqual(rl.wake_word(s)['key'], 'hey hermes', s)

    def test_strip_wake_default_hermes(self):
        self.assertEqual(rl.strip_wake('Hey Hermes, how fast am I going?'), ('how fast am I going?', True))
        self.assertEqual(rl.strip_wake('Hermes. Where is fuel?'), ('Where is fuel?', True))
        self.assertEqual(rl.strip_wake('Hey Lana, how fast?'), ('Hey Lana, how fast?', False))
        self.assertEqual(rl.strip_wake('I told Hermes about it'), ('I told Hermes about it', False))

    def test_strip_wake_lana_alts(self):
        alts = rl.wake_word('hey lana')['alts']
        self.assertEqual(rl.strip_wake('Hey, Lana. Where is the next fuel stop?', alts),
                         ('Where is the next fuel stop?', True))
        self.assertEqual(rl.strip_wake('Alana, hi there', alts), ('hi there', True))
        self.assertEqual(rl.strip_wake('Hey Hermes, hi', alts), ('Hey Hermes, hi', False))


class Keywords(unittest.TestCase):
    def test_one_file_per_wake_word(self):
        import make_keywords as mk
        self.assertEqual(sorted(mk.SETS), sorted(w['keywords'] for w in rl.WAKE_WORDS.values()))
        lex = {'HEY': [['HH', 'EY1']], 'HERMES': [['HH', 'ER1', 'M', 'IY0', 'Z']]}
        toks = {'HH', 'EY1', 'ER1', 'M', 'IY0', 'Z'}
        phrases, extra = mk.SETS['kws-words-hey-hermes.txt']
        self.assertEqual(mk.keyword_lines(lex, toks, phrases, extra),
                         ['HH EY1 HH ER1 M IY0 Z @HEY_HERMES', 'HH ER1 M IY0 Z #0.35 @HERMES'])

    def test_write_all(self):
        import make_keywords as mk
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'en.phone'), 'w') as f:
                f.write('HEY HH EY1\nHERMES HH ER1 M IY0 Z\nLANA L AE1 N AH0\n')
            with open(os.path.join(d, 'tokens.txt'), 'w') as f:
                f.write('\n'.join(f'{t} {i}' for i, t in enumerate(['HH', 'EY1', 'ER1', 'M', 'IY0', 'Z', 'L', 'AE1',
                                                                      'AA1', 'N', 'AH0'])))
            out = os.path.join(d, 'out')
            os.mkdir(out)
            mk.write_all(d, out)
            self.assertEqual(sorted(os.listdir(out)), ['kws-words-hey-hermes.txt', 'kws-words-hey-lana.txt'])
            with open(os.path.join(out, 'kws-words-hey-lana.txt')) as f:
                self.assertIn('@HEY_LANA', f.read())


class CoreWake(unittest.IsolatedAsyncioTestCase):
    base_cfg = {'stt_route': 'server', 'stt_idle_close_s': 60, 'eot_question_s': 0, 'eot_default_s': 0,
                'eot_open_s': 0, 'hold_resume_s': 0.3}

    async def start(self, **cfg):
        self._tmp = tmpdir()
        self.fake = await FakeElevenLabs(0.0).start()
        self.srv, self.core, self.agent = await start_core(self._tmp.name, self.fake, FakeAgent(),
                                                           **{**self.base_cfg, **cfg})
        self.phone = await Phone(str(self.srv.make_url('')).rstrip('/')).connect()

    async def asyncTearDown(self):
        await self.phone.close()
        await stop_core(self.srv, self.core)
        await self.fake.close()
        self._tmp.cleanup()

    async def test_hello_carries_the_wake_word(self):
        await self.start()
        w = self.core.client_cfg()['wake']
        self.assertEqual((w['word'], w['name'], w['kws']), ('Hey Hermes', 'hermes', 'kws-words-hey-hermes.txt'))
        self.assertIn('hermes', w['alts'])

    async def test_hello_lana_by_config(self):
        await self.start(wake_word='hey lana')
        w = self.core.client_cfg()['wake']
        self.assertEqual((w['word'], w['name'], w['kws']), ('Hey Lana', 'lana', 'kws-words-hey-lana.txt'))

    async def wake_word_logged(self, sent):
        await self.phone.send({'t': 'wake', 'w': sent, 'score': 1})
        for _ in range(100):
            await asyncio.sleep(0.02)
            wakes = [r for r in read_jsonl(self.core.turns.path) if r.get('event') == 'wake']
            if wakes:
                return wakes[-1]['word']
        return None

    async def test_wake_frame_default_hermes(self):
        await self.start()
        self.assertEqual(await self.wake_word_logged('hermes'), 'hermes')

    async def test_wake_frame_lana_is_other_by_default(self):
        await self.start()
        self.assertEqual(await self.wake_word_logged('lana'), 'other')

    async def test_wake_frame_lana_by_config(self):
        await self.start(wake_word='hey lana')
        self.assertEqual(await self.wake_word_logged('lana'), 'lana')


if __name__ == '__main__':
    unittest.main()
