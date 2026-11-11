"""Test harness: the real RideCore + phone server in-process, a fake phone, a fake ElevenLabs and a fake agent."""
import asyncio
import base64
import json
import os
import sys
import tempfile

from aiohttp import ClientSession, WSMsgType, web
from aiohttp.test_utils import TestServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import ride_core  # noqa: E402
import ride_logic  # noqa: E402

KEY = 'test-access-key-0123456789'


class FakeElevenLabs:
    """Scribe realtime WS + TTS stream endpoint, controlled by the test."""

    def __init__(self, tts_delay=0.0):
        self.scribe = None
        self.scribe_query = None
        self.chunks = []
        self.tts_texts = []
        self.tts_delay = tts_delay
        self.connected = asyncio.Event()
        self.sessions = 0
        self.closed_sessions = 0
        self.scribe_queries = []
        self.token_status = 200
        self.tokens = []
        self.start_sessions = True       # False: accept the socket but never send session_started
        self.hang_connect = 0.0          # > 0: the Scribe handshake hangs this long first (an unreachable provider)
        self.tts_error = None            # (status, body): the TTS endpoint fails with this response

    async def _scribe(self, request):
        assert request.headers.get('xi-api-key'), 'scribe: no api key header'
        if self.hang_connect:
            self.connect_attempts = getattr(self, 'connect_attempts', 0) + 1
            await asyncio.sleep(self.hang_connect)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.scribe, self.scribe_query = ws, dict(request.query)
        self.scribe_queries.append(list(request.query.items()))
        self.sessions += 1
        if self.start_sessions:
            await ws.send_json({'message_type': 'session_started', 'session_id': 's1', 'config': {}})
        self.connected.set()
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                self.chunks.append(json.loads(msg.data))
        self.closed_sessions += 1
        return ws

    async def _tts(self, request):
        assert request.headers.get('xi-api-key'), 'tts: no api key header'
        body = await request.json()
        self.tts_texts.append(body['text'])
        if self.tts_error:
            return web.Response(status=self.tts_error[0], text=self.tts_error[1])
        await asyncio.sleep(self.tts_delay)
        resp = web.StreamResponse()
        await resp.prepare(request)
        await resp.write(b'ID3')
        await resp.write(b'MP3:' + body['text'].encode())
        return resp

    async def _token(self, request):
        assert request.headers.get('xi-api-key'), 'token: no api key header'
        if self.token_status != 200:
            return web.json_response({'detail': 'nope'}, status=self.token_status)
        tok = 'sutkn_' + 'x' * 22 + str(len(self.tokens)).zfill(6)
        self.tokens.append(tok)
        return web.json_response({'token': tok})

    async def emit(self, **msg):
        await self.scribe.send_json(msg)

    async def start(self):
        app = web.Application()
        app.router.add_get('/v1/speech-to-text/realtime', self._scribe)
        app.router.add_post('/v1/text-to-speech/{voice}/stream', self._tts)
        app.router.add_post('/v1/single-use-token/realtime_scribe', self._token)
        self.srv = TestServer(app, host='127.0.0.1')
        await self.srv.start_server()
        return self

    def url(self, path=''):
        return str(self.srv.make_url(path))

    async def close(self):
        await self.srv.close()


class FakeAgent:
    """Stands in for adapter + gateway: records each submitted turn, then (unless told to hold) replies through
    the core like the gateway's send() would and finishes the turn like on_processing_complete."""

    def __init__(self, reply='You are doing 62 km/h. Keep it steady.', delay=0.05):
        self.reply, self.delay = reply, delay
        self.turns = []        # (turn, text, meta)
        self.running = 0
        self.max_running = 0
        self.hold = None       # asyncio.Event: turns wait for it before replying
        self.core = None

    async def submit(self, turn, text, meta):
        self.turns.append((turn, text, meta))
        if meta.get('clarify_answer'):
            return
        asyncio.create_task(self._run(turn))

    async def _run(self, turn):
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            if self.hold is not None:
                await self.hold.wait()
            await asyncio.sleep(self.delay)
            if self.reply is not None:
                await self.core.reply_text(self.reply)
        finally:
            self.running -= 1
            self.core.turn_done(turn)


class Phone:
    """Fake phone page: WebSocket with the access cookie, collects server messages."""

    def __init__(self, base_url, key=KEY, caps=None, v=2):
        self.base, self.key = base_url, key
        self.caps = caps if caps is not None else {'wake': 'sherpa', 'stt': 'server'}
        self.v = v
        self.msgs = []
        self.new = asyncio.Condition()

    async def connect(self):
        self.http = ClientSession()
        self.ws = await self.http.ws_connect(self.base.replace('http', 'ws', 1) + '/ws',
                                             headers={'Cookie': f'rk={self.key}'})
        self.reader = asyncio.create_task(self._read())
        self.hello = await self.wait({'t': 'hello'})
        self.epoch = self.hello['epoch']
        if self.v is not None:
            await self.send({'t': 'hello', 'v': self.v, 'ua': 'harness', 'caps': self.caps})
        return self

    async def _read(self):
        async for msg in self.ws:
            if msg.type == WSMsgType.TEXT:
                m = json.loads(msg.data)
            elif msg.type == WSMsgType.BINARY:   # v2 audio frame -> a synthetic 'audio' message
                mime, turn, seq, data = ride_logic.decode_audio_frame(msg.data)
                m = {'t': 'audio', 'turn': turn, 'seq': seq, 'mime': mime, 'data': data, 'bin': True,
                     'epoch': self.epoch}
            else:
                continue
            async with self.new:
                self.msgs.append(m)
                self.new.notify_all()

    async def wait(self, pred, timeout=10.0, start=0):
        if isinstance(pred, dict):
            want = pred
            pred = lambda m: all(m.get(k) == v for k, v in want.items())  # noqa: E731

        async def find():
            i = start
            async with self.new:
                while True:
                    while i < len(self.msgs):
                        if pred(self.msgs[i]):
                            return self.msgs[i]
                        i += 1
                    await self.new.wait()
        return await asyncio.wait_for(find(), timeout)

    def of(self, t, **kw):
        return [m for m in self.msgs if m.get('t') == t and all(m.get(k) == v for k, v in kw.items())]

    async def send(self, obj):
        await self.ws.send_str(json.dumps(obj))

    async def close(self):
        await self.ws.close()
        await self.http.close()


class FakeWhistle:
    """Stands in for ride_whistle.WhistleEngine (round 3, B2): the same three coroutines, texts handed out in order."""

    def __init__(self, texts=(), ms=7):
        self.texts, self.ms = list(texts), ms
        self.calls = []            # bytes of PCM per transcribe() call
        self.at = []               # monotonic time of each call
        self.started = self.closed = 0
        self.fail = False          # transcribe() raises
        self.fail_start = False    # start() raises
        self.hold = None           # asyncio.Event: transcribe() waits for it before answering
        self.lock = asyncio.Lock()

    async def start(self):
        self.started += 1
        if self.fail_start:
            raise RuntimeError('fake helper cannot start')

    async def transcribe(self, pcm):
        import time
        self.calls.append(len(pcm))
        self.at.append(time.monotonic())
        async with self.lock:
            if self.hold is not None:
                await self.hold.wait()
            if self.fail:
                raise RuntimeError('fake helper failed')
            return (self.texts.pop(0) if self.texts else ''), self.ms

    async def close(self):
        self.closed += 1


async def start_core(tmp, fake=None, agent=None, secret=None, whistle=None, **cfg_over):
    """Real RideCore + make_app in a TestServer. Returns (server, core, agent). `whistle`: an injected engine."""
    agent = agent or FakeAgent()
    cfg = dict(log_dir=os.path.join(tmp, 'logs'), **cfg_over)
    if fake:
        cfg['scribe_url'] = fake.url('/v1/speech-to-text/realtime').replace('http', 'ws', 1)
        cfg['tts_base_url'] = fake.url().rstrip('/')
    http = ClientSession()
    core = ride_core.RideCore(cfg, agent.submit, secret or (lambda name: 'test-elevenlabs-key'), http,
                              **({'whistle': whistle} if whistle is not None else {}))
    agent.core = core
    srv = TestServer(ride_core.make_app(core, KEY), host='127.0.0.1')
    await srv.start_server()
    srv._ride_http = http
    return srv, core, agent


async def stop_core(srv, core):
    await core.shutdown()
    await srv.close()
    await srv._ride_http.close()


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def b64audio(m):
    return m['data'] if 'data' in m else base64.b64decode(m['b64'])


def tmpdir():
    return tempfile.TemporaryDirectory(prefix='hermes-ride-test-')
