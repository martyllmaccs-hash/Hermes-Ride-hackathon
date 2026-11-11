"""hermes-ride telemetry, server side (W7): the authoritative ride log, the "since last turn" block for the context
line, and the /graph page. No Hermes imports; unit-tested in tests/test_telemetry.py.

The phone does the processing (calibration, lean, G, events) and uploads 1 Hz records in batches every 30 s; this
module appends them to ``<plugin data>/rides/<ride_id>.jsonl``, idempotent by the record time ``t`` (epoch ms), so
re-sent batches after a reconnect are harmless.
"""
import html
import json
import math
import os
import re
import time

try:
    from .ride_logic import redact, sanitize
except ImportError:          # top-level module (tests, tools)
    from ride_logic import redact, sanitize

RIDE_ID = re.compile(r'[A-Za-z0-9_-]{1,40}')
_NUM = (int, float)
EVENT_WORDS = {'braking': 'hard braking', 'acceleration': 'hard acceleration', 'lean': 'lean', 'stopped': 'stopped',
               'pocket_shift': 'phone moved in its mount', 'gps_lost': 'GPS lost'}


def _num(v):
    return isinstance(v, _NUM) and not isinstance(v, bool) and math.isfinite(v)


def haversine_m(a, b):
    R, p = 6371000.0, math.pi / 180
    dlat, dlon = (b['lat'] - a['lat']) * p, (b['lon'] - a['lon']) * p
    h = math.sin(dlat / 2) ** 2 + math.cos(a['lat'] * p) * math.cos(b['lat'] * p) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(min(1.0, h)))


def clean_record(r):
    """Keep a phone record only if it is a dict with a numeric t; drop oversized or non-JSON values."""
    if not isinstance(r, dict) or not _num(r.get('t')):
        return None
    out = {}
    for k, v in list(r.items())[:40]:
        if not isinstance(k, str) or len(k) > 24:
            continue
        if _num(v) or isinstance(v, bool) or v is None or (isinstance(v, str) and len(v) <= 64):
            out[k] = v
        elif isinstance(v, list) and len(v) <= 12 and all(_num(x) or isinstance(x, dict) for x in v):
            out[k] = [x if _num(x) else {kk: vv for kk, vv in x.items() if isinstance(kk, str) and
                                         (_num(vv) or isinstance(vv, str) and len(vv) <= 32)} for x in v]
        elif isinstance(v, dict) and len(v) <= 8:
            out[k] = {kk: vv for kk, vv in v.items() if isinstance(kk, str) and (_num(vv) or isinstance(vv, bool))}
    out['t'] = int(r['t'])
    return sanitize(out)             # AUD-V2-08: telemetry strings are labels; nothing key/token-shaped is stored


class RideLog:
    def __init__(self, root):
        self.root = root
        self.seen = {}           # ride id -> set of t already stored
        if root:
            os.makedirs(root, exist_ok=True)

    def path(self, ride):
        ok = RIDE_ID.fullmatch(ride or '') and redact(ride) == ride      # a token-shaped id is no ride id
        return os.path.join(self.root, f'{ride}.jsonl') if self.root and ok else None

    def _seen(self, ride):
        if ride not in self.seen:
            s = set()
            p = self.path(ride)
            if p and os.path.exists(p):
                for rec in self.read(ride):
                    if 'summary' not in rec:
                        s.add(rec['t'])
            self.seen[ride] = s
        return self.seen[ride]

    def append(self, ride, batch):
        """-> (accepted count, highest t in the batch) ; records already stored (same t) are skipped."""
        p = self.path(ride)
        if not p or not isinstance(batch, list):
            return 0, None
        seen, lines, top = self._seen(ride), [], None
        for r in batch[:2000]:
            c = clean_record(r)
            if c is None:
                continue
            top = c['t'] if top is None else max(top, c['t'])
            if c['t'] in seen:
                continue
            seen.add(c['t'])
            lines.append(json.dumps(c, separators=(',', ':')))
        if lines:
            with open(p, 'a') as f:
                f.write('\n'.join(lines) + '\n')
        return len(lines), top

    def append_summary(self, ride, summary):
        p = self.path(ride)
        c = clean_record(summary) if isinstance(summary, dict) else None
        if not p or c is None:
            return False
        with open(p, 'a') as f:
            f.write(json.dumps({'summary': True, **c}, separators=(',', ':')) + '\n')
        return True

    def read(self, ride, since_t=None):
        p = self.path(ride)
        out = []
        if not p or not os.path.exists(p):
            return out
        with open(p) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and _num(r.get('t')) and (since_t is None or r['t'] > since_t):
                    out.append(r)
        return out

    def rides(self):
        if not self.root or not os.path.isdir(self.root):
            return []
        names = [n[:-6] for n in os.listdir(self.root) if n.endswith('.jsonl') and RIDE_ID.fullmatch(n[:-6])]
        return sorted(names, key=lambda n: os.path.getmtime(os.path.join(self.root, n + '.jsonl')), reverse=True)


def _dur(s):
    s = int(round(s))
    if s < 90:
        return f'{s} s'
    m = s // 60
    return f'{m} min' if m < 90 else f'{m // 60} h {m % 60} min'


def since_text(records, t_from, t_now):
    """'Since last turn (...)' block from 1 Hz records with t in (t_from, t_now]; None without records."""
    recs = sorted((r for r in records if 'summary' not in r and t_from < r['t'] <= t_now), key=lambda r: r['t'])
    if not recs:
        return None
    dist, prev = 0.0, None
    for r in recs:
        if _num(r.get('lat')) and _num(r.get('lon')) and (not _num(r.get('acc')) or r['acc'] < 50):
            if prev is not None:
                d = haversine_m(prev, r)
                if d < 200:                   # one second: anything larger is a GPS jump
                    dist += d
            prev = r
    spd = [r['spd'] for r in recs if _num(r.get('spd'))]
    leans = [r['lean'] for r in recs if _num(r.get('lean'))]
    parts = [f'{dist / 1000:.1f} km' if dist >= 100 else f'{round(dist)} m']
    if spd:
        parts.append(f'avg {round(sum(spd) / len(spd))} km/h, max {round(max(spd))} km/h')
    if leans:
        m = max(leans, key=abs)
        parts.append(f"max lean {abs(round(m))}° {'right' if m > 0 else 'left'}" if round(m) else 'max lean 0°')
    lon = [r['lon_g'] for r in recs if _num(r.get('lon_g'))]
    if lon:
        parts.append(f'peak braking {min(0, min(lon)):.1f} G, peak acceleration {max(0, max(lon)):.1f} G')
    evs = []
    for r in recs:
        for e in r.get('ev') or []:
            if isinstance(e, dict) and e.get('ev') in EVENT_WORDS:
                w = EVENT_WORDS[e['ev']]
                v = e.get('v')
                if e['ev'] in ('braking', 'acceleration') and _num(v):
                    w += f' {v:.1f} G'
                elif e['ev'] == 'lean' and _num(v):
                    w += f" {abs(round(v))}° {'right' if v > 0 else 'left'}"
                evs.append(f"{w} ({_dur((t_now - e.get('t', r['t'])) / 1000)} ago)")
    if evs:
        parts.append('events: ' + '; '.join(evs[-6:]))
    last = recs[-1]
    cal = last.get('cal') or {}
    if not (cal.get('fwd') and cal.get('sign')):
        parts.append('lean/G calibration still learning')
    return f"Since last turn ({_dur((t_now - t_from) / 1000)}): " + ', '.join(parts) + '.'


# ---------------------------------------------------------------- /graph
def _poly(points, w, h, ymin, ymax, x0, x1):
    if not points or x1 <= x0 or ymax <= ymin:
        return ''
    xy = [f'{(t - x0) / (x1 - x0) * w:.1f},{h - (v - ymin) / (ymax - ymin) * h:.1f}' for t, v in points]
    return ' '.join(xy)


def graph_html(ride, records, refresh_s=30):
    recs = sorted((r for r in records if 'summary' not in r), key=lambda r: r['t'])
    title = f'Ride {html.escape(ride)}'
    if not recs:
        body = '<p>No telemetry yet.</p>'
    else:
        x0, x1 = recs[0]['t'], max(recs[-1]['t'], recs[0]['t'] + 1)
        w, h = 900, 160
        series = [
            ('Speed km/h', [(r['t'], r['spd']) for r in recs if _num(r.get('spd'))], 0, None, '#4a9'),
            ('Lean ° (+ right)', [(r['t'], r['lean']) for r in recs if _num(r.get('lean'))], -60, 60, '#c84'),
            ('Lean from turn rate °', [(r['t'], r['lean_turn']) for r in recs if _num(r.get('lean_turn'))], -60, 60,
             '#866'),
            ('Longitudinal G', [(r['t'], r['lon_g']) for r in recs if _num(r.get('lon_g'))], -1.2, 1.2, '#69c'),
            ('Lateral G', [(r['t'], r['lat_g']) for r in recs if _num(r.get('lat_g'))], -1.2, 1.2, '#96c'),
        ]
        blocks = []
        for name, pts, lo, hi, col in series:
            if not pts:
                continue
            vals = [v for _, v in pts]
            lo = min(vals) if lo is None else lo
            hi = max(max(vals), lo + 1) if hi is None else hi
            zero = '' if not (lo < 0 < hi) else \
                f'<line x1="0" x2="{w}" y1="{h - (0 - lo) / (hi - lo) * h:.1f}" y2="{h - (0 - lo) / (hi - lo) * h:.1f}" ' \
                'stroke="#333"/>'
            blocks.append(
                f'<h2>{html.escape(name)} <small>{min(vals):.1f} … {max(vals):.1f}</small></h2>'
                f'<svg viewBox="0 0 {w} {h}" width="100%" preserveAspectRatio="none" role="img" '
                f'aria-label="{html.escape(name)}">{zero}'
                f'<polyline fill="none" stroke="{col}" stroke-width="1.5" points="{_poly(pts, w, h, lo, hi, x0, x1)}"/>'
                '</svg>')
        span = _dur((x1 - x0) / 1000)
        start = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(x0 / 1000))
        body = f'<p>{len(recs)} records, {span}, from {start}.</p>' + ''.join(blocks)
    return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            f'<meta http-equiv="refresh" content="{int(refresh_s)}"><meta name="robots" content="noindex">'
            f'<title>{title}</title><style>body{{background:#000;color:#aaa;font:14px system-ui;margin:16px}}'
            'h2{font-size:14px;margin:18px 0 4px}small{color:#666}svg{background:#0a0a0a}</style></head>'
            f'<body><h1 style="font-size:16px">{title}</h1>{body}</body></html>')
