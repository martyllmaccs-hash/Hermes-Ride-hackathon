"""Fix round 3, R3-7 + fix round 4, AUD-R3-05: authorising another browser (Safari) without the key in a URL.

- GET / without the cookie: a small form; POST /auth with the key in the body sets the cookie (303 -> ./).
- AUD-R3-05: key links (?k=) are retired on every path: refused without the key even being compared, no cookie.
- AUD-R3-05: wrong keys are bounded per client (5, then a lock from 60 s, doubling, max 1 h) and in aggregate
  (30 in 15 min for all clients); while locked even the right key gets 429 and no cookie.
- Every auth response: no-store, Referrer-Policy no-referrer, and the key is never in a body, header or log line.
"""
import asyncio
import logging
import os
import sys
import unittest

from aiohttp import ClientSession

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import KEY, start_core, stop_core, tmpdir  # noqa: E402


class Auth(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tmpdir()
        self.srv, self.core, _ = await start_core(self._tmp.name)
        self.base = str(self.srv.make_url('')).rstrip('/')
        self.s = ClientSession()
        self.logs = []
        h = logging.Handler()
        # the harness's TestServer has aiohttp's access log on; the plugin's RideServer runs without it (checked below)
        h.emit = lambda rec: rec.name == 'aiohttp.access' or self.logs.append(rec.getMessage())
        self.handler = h
        logging.getLogger().addHandler(h)
        logging.getLogger().setLevel(logging.INFO)

    async def asyncTearDown(self):
        logging.getLogger().removeHandler(self.handler)
        await self.s.close()
        await stop_core(self.srv, self.core)
        self._tmp.cleanup()

    async def req(self, method, path, **kw):
        async with self.s.request(method, self.base + path, allow_redirects=False, **kw) as r:
            return r.status, r.headers, await r.text()

    def no_key_anywhere(self, *responses):
        for st, h, body in responses:
            self.assertNotIn(KEY, body)
            self.assertNotIn(KEY, h.get('Location', ''))
        self.assertFalse([m for m in self.logs if KEY in m])

    async def test_new_browser_gets_a_key_form_not_no(self):
        st, h, body = await self.req('GET', '/')
        self.assertEqual(st, 200)
        self.assertIn('<form', body)
        self.assertIn('method="post"', body)
        self.assertIn('action="auth"', body)
        self.assertIn('type="password"', body)
        self.assertNotIn('client_state.js', body)          # not the app itself
        self.assertEqual((h.get('Cache-Control'), h.get('Referrer-Policy')), ('no-store', 'no-referrer'))
        self.assertEqual((await self.req('GET', '/worklet.js'))[0], 403)     # everything else stays closed

    async def test_the_key_form_shows_the_orb(self):                 # WP7 (D2): the login speaks the orb's language
        import re
        for st, _, body in (await self.req('GET', '/'), await self.req('POST', '/auth', data={'k': 'not-the-key-0000000000'})):
            self.assertIn('<div class="orb" aria-hidden="true"></div>', body)
            self.assertLess(body.index('class="orb"'), body.index('<form'))      # the orb first, the form beneath
            self.assertIn('#1d6cff', re.search(r'\.orb\{[^}]*\}', body).group(0))  # the running orb's blue core
            self.assertNotIn('<script', body)                                     # still no script on the form
        self.assertEqual(st, 403)

    async def test_form_post_sets_the_cookie_and_leaves_no_key_in_the_url(self):
        st, h, body = await self.req('POST', '/auth', data={'k': KEY})
        self.assertEqual((st, h.get('Location')), (303, './'))
        for part in ('rk=' + KEY, 'HttpOnly', 'Secure', 'SameSite=Strict', 'Path=/'):
            self.assertIn(part, h.get('Set-Cookie', ''))
        self.assertEqual((h.get('Cache-Control'), h.get('Referrer-Policy')), ('no-store', 'no-referrer'))
        self.no_key_anywhere((st, h, body))
        st2, _, page = await self.req('GET', '/', headers={'Cookie': 'rk=' + KEY})
        self.assertEqual(st2, 200)
        self.assertIn('client_state.js', page)

    async def test_wrong_key_in_the_form(self):
        st, h, body = await self.req('POST', '/auth', data={'k': 'not-the-key-0000000000'})
        self.assertEqual(st, 403)
        self.assertNotIn('Set-Cookie', h)
        self.assertIn('<form', body)                       # try again
        self.assertEqual((await self.req('POST', '/auth', data={}))[0], 403)

    async def test_key_links_are_retired(self):                      # AUD-R3-05
        for path in ('/?k=' + KEY, '/?k=wrong', '/worklet.js?k=' + KEY, '/auth?k=' + KEY):
            st, h, body = await self.req('GET', path)
            self.assertEqual(st, 403, path)
            self.assertNotIn('Set-Cookie', h)
            self.assertNotIn('client_state.js', body)
            self.assertNotIn('location.replace', body)
            self.no_key_anywhere((st, h, body))
        st, h, body = await self.req('POST', '/auth?k=' + KEY, data={})   # a key in the URL never counts
        self.assertEqual((st, 'Set-Cookie' in h), (403, False))

    def has_limiter(self):
        return any('ride_auth' in str(k) for k in self.srv.app)

    def limiter(self):
        lim = next((v for k, v in self.srv.app.items() if 'ride_auth' in str(k)), None)
        self.assertIsNotNone(lim, 'no auth limiter')
        lim.delay_s = 0                                    # the 1 s wrong-key delay, off for the test
        now = [1000.0]
        lim.clock = lambda: now[0]
        return lim, now

    async def post_key(self, k, client):
        st, h, body = await self.req('POST', '/auth', data={'k': k}, headers={'X-Forwarded-For': client})
        return st, 'Set-Cookie' in h

    async def test_wrong_keys_lock_the_client_out_then_back_off(self):   # AUD-R3-05
        lim, now = self.limiter() if self.has_limiter() else (None, [0])
        for _ in range(5):
            self.assertEqual(await self.post_key('not-the-key-0000000000', '203.0.113.5'), (403, False))
        self.assertEqual(await self.post_key(KEY, '203.0.113.5'), (429, False))      # locked: not even compared
        self.assertEqual(await self.post_key(KEY, '198.51.100.7'), (303, True))      # another client is not
        now[0] += 61
        self.assertEqual(await self.post_key('not-the-key-0000000000', '203.0.113.5'), (403, False))
        now[0] += 61
        self.assertEqual(await self.post_key(KEY, '203.0.113.5'), (429, False))      # the lock doubled (120 s)
        now[0] += 60
        self.assertEqual(await self.post_key(KEY, '203.0.113.5'), (303, True))       # then the right key works
        self.assertEqual(await self.post_key('not-the-key-0000000000', '203.0.113.5'), (403, False))   # reset
        self.no_key_anywhere()

    async def test_parallel_guesses_are_bounded_too(self):               # AUD-R3-05
        if self.has_limiter():
            self.limiter()
        res = await asyncio.gather(*[self.post_key('guess-%016d' % i, '203.0.113.9') for i in range(30)])
        self.assertEqual(sorted(st for st, _ in res).count(403), 5)
        self.assertEqual([st for st, _ in res].count(429), 25)

    async def test_aggregate_bound_across_clients(self):                 # AUD-R3-05
        lim, now = self.limiter() if self.has_limiter() else (None, [0])
        res = await asyncio.gather(*[self.post_key('guess-%016d' % i, '192.0.2.%d' % i) for i in range(1, 31)])
        self.assertEqual([st for st, _ in res].count(403), 30)
        self.assertEqual(await self.post_key(KEY, '198.51.100.20'), (429, False))     # everyone waits now
        now[0] += 15 * 60 + 1
        self.assertEqual(await self.post_key(KEY, '198.51.100.20'), (303, True))

    async def test_non_ascii_wrong_key_is_a_wrong_key(self):             # D5: 403 and counted, not a 500
        lim, now = self.limiter()
        for _ in range(5):
            self.assertEqual(await self.post_key('caf\u00e9-gu\u00e9ss-0123456789', '203.0.113.31'), (403, False))
        self.assertEqual((lim.clients.get('203.0.113.31') or [0])[0], 5)             # the limiter recorded each one
        self.assertEqual(await self.post_key(KEY, '203.0.113.31'), (429, False))     # and locks the client out
        self.assertEqual(await self.post_key('\u043a\u043b\u044e\u0447-\u9375', '203.0.113.32'), (403, False))
        self.no_key_anywhere()

    async def test_non_ascii_cookie_is_not_authed(self):                 # D5: refused like any wrong cookie, no 500
        for cookie in ('rk=caf\u00e9', 'rk=\u043a\u043b\u044e\u0447'):
            st, _, body = await self.req('GET', '/client_state.js', headers={'Cookie': cookie})
            self.assertEqual(st, 403)
            st, _, body = await self.req('GET', '/', headers={'Cookie': cookie})
            self.assertEqual(st, 200)
            self.assertIn('<form', body)
            self.assertNotIn('client_state.js', body)

    async def test_non_ascii_access_key_logs_in(self):                   # D5: such a key could never log in
        import ride_core
        from aiohttp.test_utils import TestServer
        key = 'cl\u00e9-caf\u00e9-0123456789'
        srv = TestServer(ride_core.make_app(self.core, key), host='127.0.0.1')
        await srv.start_server()
        try:
            base = str(srv.make_url('')).rstrip('/')
            async with self.s.post(base + '/auth', data={'k': key}, allow_redirects=False) as r:
                self.assertEqual((r.status, r.headers.get('Location')), (303, './'))
                cookie = r.headers.get('Set-Cookie', '').split(';')[0]
            self.assertTrue(cookie.startswith('rk='))
            async with self.s.get(base + '/', headers={'Cookie': cookie}, allow_redirects=False) as r:
                self.assertEqual(r.status, 200)
                self.assertIn('client_state.js', await r.text())                 # the cookie it set opens the app
            async with self.s.post(base + '/auth', data={'k': KEY}, allow_redirects=False) as r:
                self.assertEqual(r.status, 403)                                  # an ASCII wrong key stays wrong
            async with self.s.get(base + '/', headers={'Cookie': 'rk=' + KEY}, allow_redirects=False) as r:
                self.assertNotIn('client_state.js', await r.text())
        finally:
            await srv.close()

    def test_the_plugin_server_has_no_access_log(self):     # an access line would carry a ?k= link
        import inspect
        import ride_core
        self.assertIn('access_log=None', inspect.getsource(ride_core.RideServer.start))


if __name__ == '__main__':
    unittest.main()
