"""hermes-ride: Whistle speech-to-text on the Hermes host (round 3, B2). Standard library + asyncio only.

``WhistleEngine`` keeps one helper process alive (``tools/whistle_worker.py``, run by the interpreter that has the
``needle`` package: setting ``whistle_python``). The helper loads the model once; every finished phrase is written
as a 16 kHz mono WAV into a private scratch directory, transcribed, and the file is deleted again. Nothing leaves
the host. ``RideCore`` only needs ``start()``, ``transcribe(pcm) -> (text, ms)`` and ``close()``, so tests inject a
fake with those three coroutines.
"""
import asyncio
import json
import logging
import os
import secrets
import tempfile
import time
import wave

log = logging.getLogger('hermes_ride')

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools', 'whistle_worker.py')
SAMPLE_RATE = 16000
MAX_AUDIO_S = 30.0               # one call takes at most this much audio (the core cuts a phrase at 28 s)


class WhistleError(Exception):
    """The helper could not be started, timed out, or failed twice in a row."""


def hermes_whistle_paths(hermes_cfg):
    """(python, weights) from Hermes's own ``stt.providers.whistle.command`` when that provider exists: the first
    token if it is a file, and ``whistle.cact`` next to the command's script if that exists. ('', '') otherwise."""
    try:
        cmd = (((hermes_cfg or {}).get('stt') or {}).get('providers') or {}).get('whistle', {}).get('command')
    except AttributeError:
        cmd = None
    parts = str(cmd).split() if isinstance(cmd, str) else []
    if not parts or not os.path.isfile(parts[0]):
        return '', ''
    weights = ''
    for p in parts[1:]:
        if p.endswith('.py') and os.path.isfile(p):
            cand = os.path.join(os.path.dirname(os.path.abspath(p)), 'whistle.cact')
            weights = cand if os.path.isfile(cand) else ''
            break
    return parts[0], weights


class WhistleEngine:
    def __init__(self, python='python3', weights='', language='en', timeout_s=20.0, scratch_dir=None,
                 worker=WORKER, start_timeout_s=60.0, worker_args=()):
        self.python, self.weights, self.language = python or 'python3', weights or '', language or ''
        self.timeout_s, self.start_timeout_s = float(timeout_s), float(start_timeout_s)
        self.scratch_dir = scratch_dir or os.path.join(tempfile.gettempdir(), 'hermes-ride-whistle')
        self.worker = worker
        self.worker_args = list(worker_args or ())
        self.proc = None
        self.lock = asyncio.Lock()       # one request at a time (the model is not thread-safe), in arrival order
        self.seq = 0
        self.starts = 0                  # helper processes started (tests, the log)

    # ------------------------------------------------------------ the helper process
    def alive(self):
        return self.proc is not None and self.proc.returncode is None

    async def start(self):
        """Start the helper and wait for its ready line. Idempotent. Raises WhistleError if it can't start."""
        async with self.lock:
            await self._start()

    async def _start(self):
        if self.alive():
            return
        await self._kill()
        env = dict(os.environ, NEEDLE_TELEMETRY='0', DO_NOT_TRACK='1', PYTHONUNBUFFERED='1')
        argv = [self.python, self.worker] + (['--weights', self.weights] if self.weights else []) + self.worker_args
        try:
            os.makedirs(self.scratch_dir, mode=0o700, exist_ok=True)
            self.proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, env=env, limit=1 << 20)
        except (OSError, ValueError) as e:
            self.proc = None
            raise WhistleError(f'cannot start the Whistle helper: {type(e).__name__}') from e
        self.starts += 1
        try:
            msg = await asyncio.wait_for(self._read(), self.start_timeout_s)
        except asyncio.TimeoutError:
            await self._kill()
            raise WhistleError('the Whistle helper did not get ready in time') from None
        if not (isinstance(msg, dict) and msg.get('ready') is True):
            why = str((msg or {}).get('error') or 'it exited')[:200]
            await self._kill()
            raise WhistleError(f'the Whistle helper did not start: {why}')

    async def _read(self):
        """One JSON line from the helper (None at end of file; non-JSON lines are skipped)."""
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                return None
            try:
                return json.loads(line)
            except ValueError:
                continue

    async def _kill(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except (asyncio.TimeoutError, Exception):
            pass

    # ------------------------------------------------------------ one phrase
    def _write_wav(self, pcm):
        path = os.path.join(self.scratch_dir, f'phrase-{os.getpid()}-{secrets.token_hex(6)}.wav')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as f, wave.open(f, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm)
        return path

    async def transcribe(self, pcm):
        """PCM16 LE mono 16 kHz -> (text, helper milliseconds). One request at a time. A helper that died is
        restarted once; a timeout, or a second failure, raises WhistleError."""
        pcm = bytes(pcm)[:int(MAX_AUDIO_S * SAMPLE_RATE) * 2]
        pcm = pcm[:len(pcm) - len(pcm) % 2]
        if not pcm:
            return '', 0
        async with self.lock:
            path = None
            try:
                os.makedirs(self.scratch_dir, mode=0o700, exist_ok=True)
                path = self._write_wav(pcm)
                last = None
                for attempt in (1, 2):
                    try:
                        await self._start()
                        return await self._ask(path)
                    except _HelperDied as e:          # restart once
                        last = e
                        await self._kill()
                        log.warning('whistle: helper died (%s), attempt %d', e, attempt)
                raise WhistleError(f'the Whistle helper failed twice: {last}')
            except OSError as e:
                raise WhistleError(f'whistle scratch file: {type(e).__name__}') from e
            finally:
                if path:
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    async def _ask(self, path):
        self.seq += 1
        rid = self.seq
        t0 = time.monotonic()
        try:
            self.proc.stdin.write((json.dumps({'id': rid, 'wav': path, 'language': self.language}) + '\n').encode())
            await self.proc.stdin.drain()
        except (ConnectionError, RuntimeError, AttributeError) as e:
            raise _HelperDied(type(e).__name__) from e
        try:
            while True:
                left = self.timeout_s - (time.monotonic() - t0)
                if left <= 0:
                    raise asyncio.TimeoutError
                msg = await asyncio.wait_for(self._read(), left)
                if msg is None:
                    raise _HelperDied('end of output')
                if isinstance(msg, dict) and msg.get('id') == rid:
                    break
        except asyncio.TimeoutError:
            await self._kill()           # it may be stuck in the model: the next phrase gets a fresh helper
            raise WhistleError(f'Whistle took longer than {self.timeout_s:g} s') from None
        if 'error' in msg or not isinstance(msg.get('text'), str):
            raise WhistleError('Whistle could not transcribe: ' + str(msg.get('error'))[:200])
        ms = msg.get('ms')
        return msg['text'], (int(ms) if isinstance(ms, (int, float)) and not isinstance(ms, bool)
                             else round((time.monotonic() - t0) * 1000))

    async def close(self):
        async with self.lock:
            proc = self.proc
            if proc is not None and proc.returncode is None:
                try:
                    proc.stdin.close()               # end of input: the helper exits on its own
                    await asyncio.wait_for(proc.wait(), 3)
                except (asyncio.TimeoutError, Exception):
                    pass
            await self._kill()


class _HelperDied(Exception):
    pass
