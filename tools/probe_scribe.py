"""One-off probe of ElevenLabs Scribe v2 realtime with a single-use token (plan W4 step 1).

Mints one token per attempt (POST /v1/single-use-token/realtime_scribe with ELEVENLABS_API_KEY from env),
connects with ?token= like the phone page will, streams one fixture at real-time pace (then silence) and prints
what the server says. Tokens and keys are never printed: only whether a token was returned and its prefix shape.

    timeout 120 python3 tools/probe_scribe.py tests/fixtures/speech_clean.wav
"""
import asyncio
import base64
import json
import os
import re
import sys
import time
import wave
from urllib.parse import urlencode

from aiohttp import ClientSession, WSMsgType

API = 'https://api.elevenlabs.io'
WSS = 'wss://api.elevenlabs.io/v1/speech-to-text/realtime'

ATTEMPTS = [
    ('turn_prediction', [('commit_strategy', 'turn_prediction')]),
    ('vad+langs(repeat)+filter+langdet', [('commit_strategy', 'vad'), ('vad_silence_threshold_secs', '1.2'),
                                          ('filter_background_audio', 'true'),
                                          ('include_language_detection', 'true'),
                                          ('secondary_languages', 'zh'), ('secondary_languages', 'yue'),
                                          ('secondary_languages', 'fr'), ('secondary_languages', 'ja')]),
]


def shape(tok):
    m = re.match(r'([a-z]+_)?', tok or '')
    return {'returned': bool(tok), 'prefix': m.group(1) if m and m.group(1) else None, 'len': len(tok or '')}


async def attempt(http, key, name, params, pcm, max_s):
    out = {'attempt': name, 'params': [p for p in params]}
    async with http.post(API + '/v1/single-use-token/realtime_scribe', headers={'xi-api-key': key}) as r:
        out['mint_status'] = r.status
        body = await r.json(content_type=None)
        tok = body.get('token') if isinstance(body, dict) else None
        out['token'] = shape(tok)
        out['mint_keys'] = sorted(body) if isinstance(body, dict) else None
    if not tok:
        return out
    q = [('model_id', 'scribe_v2_realtime'), ('audio_format', 'pcm_16000'), ('token', tok)] + params
    seen, t0 = [], time.monotonic()
    try:
        async with http.ws_connect(WSS + '?' + urlencode(q), heartbeat=None) as ws:
            async def feed():
                i, frame = 0, 3200
                while time.monotonic() - t0 < max_s:
                    chunk = pcm[i:i + frame] or b'\x00' * frame
                    i += frame
                    await ws.send_str(json.dumps({'message_type': 'input_audio_chunk', 'commit': False,
                                                  'sample_rate': 16000,
                                                  'audio_base_64': base64.b64encode(chunk).decode()}))
                    await asyncio.sleep(0.1)
                await ws.close()
            f = asyncio.create_task(feed())
            async for m in ws:
                if m.type != WSMsgType.TEXT:
                    continue
                d = json.loads(m.data)
                mt = d.get('message_type')
                rec = {'at_s': round(time.monotonic() - t0, 2), 'type': mt}
                for k in ('text', 'language_code', 'error', 'warning', 'config'):
                    if k in d:
                        rec[k] = d[k]
                if mt == 'partial_transcript' and seen and seen[-1]['type'] == mt:
                    seen[-1] = rec
                else:
                    seen.append(rec)
            f.cancel()
            out['close_code'] = ws.close_code
    except Exception as e:
        out['ws_error'] = repr(e).replace(tok, '<token>')[:300]
    out['events'] = seen
    # Token reuse must fail (single-use): try once more with the same token, no audio.
    try:
        async with http.ws_connect(WSS + '?' + urlencode(q), heartbeat=None) as ws2:
            m = await asyncio.wait_for(ws2.receive(), 5)
            out['reuse'] = str(m.data)[:200].replace(tok, '<token>')
    except Exception as e:
        out['reuse'] = ('refused: ' + repr(e)).replace(tok, '<token>')[:200]
    return out


async def main(path, max_s=14):
    key = os.environ.get('ELEVENLABS_API_KEY')
    if not key:
        sys.exit('ELEVENLABS_API_KEY not set')
    with wave.open(path) as w:
        pcm = w.readframes(w.getnframes())
    async with ClientSession() as http:
        for name, params in ATTEMPTS:
            res = await attempt(http, key, name, params, pcm, max_s)
            print(json.dumps(res, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1]))
