"""Render the v2 live-test speech fixtures once (16 kHz mono PCM16 WAV) with ElevenLabs TTS, ELEVENLABS_API_KEY from env.

    timeout 120 python3 tools/render_fixtures.py [only these fixture names]

stale_pause.wav  "Hey Lana, can you find me a good place." + 2.5 s silence + "To stop for coffee near Bright."
search_clean.wav "Hey Lana, search the web for the weather forecast in Bright tomorrow."
stretch_clean.wav "Hey Lana, how hard did I brake back there, and how far have I ridden since I last asked?"
The speaker is an ElevenLabs stock voice (not Lana's), like a rider talking to her.
"""
import json
import os
import sys
import urllib.request
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FX = os.path.join(ROOT, 'tests', 'fixtures')
VOICE = 'pNInz6obpgDQGcFmaJgB'      # stock voice "Adam"


def pcm(key, text):
    req = urllib.request.Request(f'https://api.elevenlabs.io/v1/text-to-speech/{VOICE}?output_format=pcm_16000',
                                 data=json.dumps({'text': text, 'model_id': 'eleven_flash_v2_5'}).encode(),
                                 headers={'xi-api-key': key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def write(name, data):
    with wave.open(os.path.join(FX, name), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(data)
    print(json.dumps({'fixture': name, 'seconds': round(len(data) / 32000, 2)}))


ONLY = set(sys.argv[1:])


def main():
    key = os.environ.get('ELEVENLABS_API_KEY')
    if not key:
        sys.exit('ELEVENLABS_API_KEY is not set')
    gap = b'\x00\x00' * int(16000 * 2.5)
    if not ONLY or 'stale_pause.wav' in ONLY:
        write('stale_pause.wav', b'\x00\x00' * 8000 + pcm(key, 'Hey Lana, can you find me a good place.') + gap +
              pcm(key, 'To stop for coffee near Bright.') + b'\x00\x00' * 8000)
    if not ONLY or 'search_clean.wav' in ONLY:
        write('search_clean.wav', b'\x00\x00' * 8000 +
              pcm(key, 'Hey Lana, search the web for the weather forecast in Bright tomorrow.') + b'\x00\x00' * 8000)
    if not ONLY or 'stretch_clean.wav' in ONLY:
        write('stretch_clean.wav', b'\x00\x00' * 8000 + pcm(
            key, 'Hey Lana, how hard did I brake back there, and how far have I ridden since I last asked?') +
            b'\x00\x00' * 8000)
    # 10 Nov (Hermes-Ride): the public default wake word. hermes_road = hermes_clean + seeded brown noise (wind/engine
    # stand-in, about -30 dBFS), not a road recording.
    if not ONLY or 'hermes_clean.wav' in ONLY or 'hermes_road.wav' in ONLY:
        clean = b'\x00\x00' * 8000 + pcm(key, 'Hey Hermes, how fast am I going?') + b'\x00\x00' * 8000
        write('hermes_clean.wav', clean)
        write('hermes_road.wav', with_noise(clean, seed=11, rms_db=-30.0))
    if not ONLY or 'hermes_search.wav' in ONLY:
        write('hermes_search.wav', b'\x00\x00' * 8000 +
              pcm(key, 'Hermes, search the web for the weather forecast in Bright tomorrow.') + b'\x00\x00' * 8000)


def with_noise(data, seed, rms_db):
    """PCM16 + seeded brown noise at rms_db dBFS (clipped)."""
    import array
    import random
    rnd = random.Random(seed)
    s = array.array('h', data)
    b, raw = 0.0, []
    for _ in range(len(s)):
        b = 0.98 * b + rnd.uniform(-1, 1)
        raw.append(b)
    rms = (sum(x * x for x in raw) / len(raw)) ** 0.5 or 1.0
    k = 32768 * 10 ** (rms_db / 20) / rms
    out = array.array('h', (max(-32768, min(32767, int(v + k * n))) for v, n in zip(s, raw)))
    return out.tobytes()


if __name__ == '__main__':
    main()
