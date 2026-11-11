"""Render the six status clips (Lana's voice) once, into static/clips/<name>.m4a. Uses ELEVENLABS_API_KEY from the
environment (never printed). The page plays these locally, so "connection lost" works with no connection.

    timeout 120 python3 tools/render_status_clips.py [--voice hLjwV7lYzk15SWLUmhEH] [--model eleven_flash_v2_5]

Needs ffmpeg (mp3 from ElevenLabs -> AAC m4a, mono 44.1 kHz, the same container as the cues).
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIPS = {
    'reconnecting': 'Lost the connection, reconnecting.',
    'back': "I'm back.",
    'quota': "Speech recognition's out of quota. Switching to backup.",
    'working': 'Still working on it.',
    'offline': "I'm offline. I'll keep trying.",
    'nomic': "I can't hear the mic.",
}


def tts(key, voice, model, text):
    req = urllib.request.Request(
        f'https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128',
        data=json.dumps({'text': text, 'model_id': model}).encode(),
        headers={'xi-api-key': key, 'Content-Type': 'application/json', 'Accept': 'audio/mpeg'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--voice', default='hLjwV7lYzk15SWLUmhEH')
    ap.add_argument('--model', default='eleven_flash_v2_5')
    ap.add_argument('--out', default=os.path.join(ROOT, 'static', 'clips'))
    a = ap.parse_args()
    key = os.environ.get('ELEVENLABS_API_KEY')
    if not key:
        sys.exit('ELEVENLABS_API_KEY is not set')
    os.makedirs(a.out, exist_ok=True)
    chars = 0
    for name, text in CLIPS.items():
        mp3 = tts(key, a.voice, a.model, text)
        chars += len(text)
        with tempfile.NamedTemporaryFile(suffix='.mp3') as t:
            t.write(mp3)
            t.flush()
            dst = os.path.join(a.out, name + '.m4a')
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', t.name, '-ac', '1', '-ar', '44100', '-c:a', 'aac',
                            '-b:a', '64k', '-movflags', '+faststart', dst], check=True, timeout=60)
        print(json.dumps({'clip': name, 'text': text, 'mp3_bytes': len(mp3), 'm4a_bytes': os.path.getsize(dst)}))
    print(json.dumps({'total_chars': chars, 'voice': a.voice, 'model': a.model}))


if __name__ == '__main__':
    main()
