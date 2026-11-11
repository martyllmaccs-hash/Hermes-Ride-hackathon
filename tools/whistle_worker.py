#!/usr/bin/env python3
"""Long-lived Whistle speech-to-text helper for hermes-ride (round 3, B2).

Run by ``ride_whistle.WhistleEngine`` with the interpreter that has the ``needle`` package (setting
``whistle_python``), not with Hermes's own Python. Imports only the standard library and ``needle``.

    whistle_worker.py [--weights /path/to/whistle.cact]

Loads the model once, then:
    stdout  {"ready": true}                              one line, after the model is loaded
    stdin   {"id": 7, "wav": "/path/x.wav", "language": "en"}     one JSON line per request
    stdout  {"id": 7, "text": "...", "ms": 612}          or  {"id": 7, "error": "..."}
Exits on end of input. No network: telemetry is switched off before ``needle`` is imported, and with ``--weights``
nothing is fetched. The helper never deletes or keeps the WAV; the caller owns it.
"""
import argparse
import json
import os
import sys
import time

os.environ['NEEDLE_TELEMETRY'] = '0'      # before the import: needle reads these when it loads
os.environ['DO_NOT_TRACK'] = '1'


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--weights', default='', help='Whistle weights file (.cact); empty = the package default')
    args = ap.parse_args()
    # The answers go to the stdout this process was started with. File descriptor 1 is then pointed at stderr, so
    # anything the native library or an import prints can never break the one-JSON-line-per-answer protocol.
    out = os.fdopen(os.dup(1), 'w', buffering=1, encoding='utf-8')
    os.dup2(2, 1)

    def answer(obj):
        out.write(json.dumps(obj, ensure_ascii=False) + '\n')
        out.flush()

    try:
        import needle
        model = needle.Whistle(weights=args.weights or None)
    except Exception as e:                # no needle, no weights: say why once and stop (the engine reports it)
        answer({'ready': False, 'error': f'{type(e).__name__}: {e}'[:300]})
        return 2
    answer({'ready': True})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        rid = None
        try:
            req = json.loads(line)
            rid = req.get('id')
            t0 = time.monotonic()
            res = model.transcribe(str(req['wav']), language=(req.get('language') or None))
            text = res.get('text') if isinstance(res, dict) else None
            if not isinstance(text, str):
                raise RuntimeError('Whistle returned no text')
            answer({'id': rid, 'text': text, 'ms': round((time.monotonic() - t0) * 1000)})
        except Exception as e:
            answer({'id': rid, 'error': f'{type(e).__name__}: {e}'[:300]})
    return 0


if __name__ == '__main__':
    sys.exit(main())
