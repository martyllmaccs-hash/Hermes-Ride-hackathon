#!/usr/bin/env python3
"""Print the AI Ride Summary's ride-character part for one ride (round 3, B3).

    tools/ride_character.py /path/to/ride-20261110-101010.jsonl [--rides-dir DIR] [--json]

Several logs of ONE session are given together and joined as one ride; the baseline is the other rides in
``--rides-dir`` (default: the folder of the first log). Output is JSON: label, plain-words reasons, the z-scores
behind them, how many baseline rides there were, caveats and the features. Nothing is written anywhere.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ride_character  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description='Ride character against the rider\'s own baseline (JSON).')
    ap.add_argument('logs', nargs='+', help='this ride\'s 1 Hz JSONL log(s); several = one session')
    ap.add_argument('--rides-dir', default=None, help='folder of past ride logs (default: the first log\'s folder)')
    ap.add_argument('--json', action='store_true', help='JSON (the only format today)')
    args = ap.parse_args(argv)
    rides_dir = args.rides_dir or os.path.dirname(os.path.abspath(args.logs[0]))
    out = ride_character.ride_character(args.logs, rides_dir)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
