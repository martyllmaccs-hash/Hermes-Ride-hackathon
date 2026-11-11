"""hermes-ride: the ride's character against the rider's own baseline (round 3, B3). Pure standard library.

One ride log is the phone's 1 Hz upload (see ride_telemetry.py): ``t`` epoch ms, ``spd`` km/h, ``lon_g`` forward
acceleration in G (negative = braking), ``lean`` degrees, ``d_m`` metres since the previous fix, ``acc`` GPS accuracy,
``cal`` = {'fwd', 'sign', ...}. Rows with ``summary`` are the phone's own minute totals and are ignored.

    features(path)            one ride (a list of paths = several page loads of one session, joined by time)
    baseline(paths)           the rider's other rides: medians of each feature over up to 20 rides with 3 min moving
    character(features, base) a label, plain-words reasons, the z-scores behind them, and the caveats

Labels are fixed: ``calm``, ``spirited``, ``stop-start``, ``congested filtering``, ``mixed``, ``too short``.
Nothing is invented: a missing part is a caveat, and with fewer than 5 rides in the baseline the ride is described on
its own terms (label from absolute thresholds, ``baseline_rides`` says how many there were).
"""
import json
import math
import os
import statistics
import time

MOVING_KMH = 2.0          # below this the bike counts as stopped
SLOW_KMH = 15.0           # filtering/congestion threshold
LEAN_DEG = 20.0
MIN_MOVING_S = 180.0      # a ride shorter than this is 'too short'
MIN_BASELINE = 5          # fewer rides than this: not a baseline
BASELINE_MAX = 20
CAL_LEARN_S = 120.0       # lean/G calibration is expected to settle in the first two minutes
EPS = 1e-9

LABELS = ('calm', 'spirited', 'stop-start', 'congested filtering', 'mixed', 'too short')
FEATURES = ('duration_s', 'moving_s', 'distance_km', 'stopped_share', 'slow_share', 'brake_mean', 'brake_p95',
            'accel_mean', 'accel_p95', 'stops_per_km', 'top_kmh', 'speed_sd', 'lean_share')


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _pct(values, q):
    """q-th percentile (0..1) by linear interpolation; None for no values."""
    v = sorted(values)
    if not v:
        return None
    if len(v) == 1:
        return v[0]
    pos = q * (len(v) - 1)
    i = int(pos)
    f = pos - i
    return v[i] if i + 1 >= len(v) else v[i] + (v[i + 1] - v[i]) * f


def _haversine_m(a, b):
    R, p = 6371000.0, math.pi / 180
    dlat, dlon = (b['lat'] - a['lat']) * p, (b['lon'] - a['lon']) * p
    h = math.sin(dlat / 2) ** 2 + math.cos(a['lat'] * p) * math.cos(b['lat'] * p) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(min(1.0, h)))


def read_records(paths):
    """The ride's 1 Hz rows, sorted by t, without the phone's minute summaries. A list joins page loads."""
    if isinstance(paths, (str, bytes, os.PathLike)):
        paths = [paths]
    rows = []
    for p in paths:
        try:
            with open(p, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(r, dict) and _num(r.get('t')) and not r.get('summary'):
                        rows.append(r)
        except OSError:
            continue
    rows.sort(key=lambda r: r['t'])
    return rows


def features(paths):
    """One ride's numbers (km/h, G, degrees; see the module docstring). None where the phone had no such data."""
    one = isinstance(paths, (str, bytes, os.PathLike))
    rows = read_records(paths)
    out = {k: None for k in FEATURES}
    out.update({'rides': 1 if one else len(paths), 'records': len(rows), 'has_speed': False, 'has_motion': False,
                'has_gps': False, 'cal_learning': False, 't_start': None, 't_end': None})
    if not rows:
        return out
    t0, t1 = rows[0]['t'], rows[-1]['t']
    out.update({'t_start': t0, 't_end': t1, 'duration_s': round((t1 - t0) / 1000.0, 1)})
    # each record stands for the gap to the next one (1 s; capped at 5 s: a connection gap is not riding time)
    for i, r in enumerate(rows):
        gap = (rows[i + 1]['t'] - r['t']) / 1000.0 if i + 1 < len(rows) else 1.0
        r['_dt'] = max(0.0, min(5.0, gap))
    timed = [(r['_dt'], r['spd']) for r in rows if _num(r.get('spd'))]
    out['has_speed'] = bool(timed)
    if timed:
        out['moving_s'] = round(sum(dt for dt, s in timed if s >= MOVING_KMH), 1)
        spd = [s for _, s in timed]
        stopped = [s for s in spd if s < MOVING_KMH]
        slow = [s for s in spd if MOVING_KMH <= s < SLOW_KMH]
        moving = [s for s in spd if s >= MOVING_KMH]
        out['stopped_share'] = round(len(stopped) / len(spd), 3)
        out['slow_share'] = round(len(slow) / len(moving), 3) if moving else None
        out['top_kmh'] = round(max(spd), 1)
        out['speed_sd'] = round(statistics.pstdev(spd), 2) if len(spd) > 1 else 0.0
    dist = 0.0
    prev = None
    for r in rows:
        if _num(r.get('lat')) and _num(r.get('lon')):
            out['has_gps'] = True
        if _num(r.get('d_m')):
            dist += max(0.0, float(r['d_m']))
        elif _num(r.get('lat')) and _num(r.get('lon')) and (not _num(r.get('acc')) or r['acc'] < 50):
            if prev is not None:
                d = _haversine_m(prev, r)
                if d < 200:
                    dist += d
            prev = r
    out['distance_km'] = round(dist / 1000.0, 3)
    rolling = [r for r in rows if _num(r.get('spd'))]
    stops = sum(1 for a, b in zip(rolling, rolling[1:]) if a['spd'] < MOVING_KMH <= b['spd'])
    out['stops_per_km'] = round(stops / dist * 1000.0, 2) if dist >= 200 else None
    lon = [r['lon_g'] for r in rows if _num(r.get('lon_g'))]
    if lon:
        out['has_motion'] = True
        brakes = [-v for v in lon if v < 0]
        accels = [v for v in lon if v > 0]
        out['brake_mean'] = round(statistics.fmean(brakes), 3) if brakes else 0.0
        out['brake_p95'] = round(_pct(brakes, 0.95), 3) if brakes else 0.0     # 95th percentile of braking
        out['accel_mean'] = round(statistics.fmean(accels), 3) if accels else 0.0
        out['accel_p95'] = round(_pct(accels, 0.95), 3) if accels else 0.0
    leans = [abs(r['lean']) for r in rows if _num(r.get('lean'))]
    if leans:
        out['has_motion'] = True
        out['lean_share'] = round(sum(1 for v in leans if v > LEAN_DEG) / len(leans), 3)
    cal = [r.get('cal') for r in rows if r['t'] - t0 <= CAL_LEARN_S * 1000 and isinstance(r.get('cal'), dict)]
    out['cal_learning'] = bool(cal) and any(not (c.get('fwd') and c.get('sign')) for c in cal)
    return out


def baseline(paths, exclude=()):
    """The rider's own baseline: per feature, the median over up to 20 other rides with 3 min moving."""
    skip = {os.path.abspath(p) for p in (exclude or ())}
    cands = []
    for p in paths or ():
        if os.path.abspath(str(p)) in skip or not os.path.isfile(p):
            continue
        try:
            m = os.path.getmtime(p)
        except OSError:
            m = 0.0
        cands.append((m, p))
    cands.sort(reverse=True)
    rides = [features(p) for _, p in cands]
    rides = [f for f in rides if (f.get('moving_s') or 0) >= MIN_MOVING_S]
    rides = rides[:BASELINE_MAX]
    out = {'rides': len(rides), 'features': {}}
    for k in FEATURES:
        vals = [f[k] for f in rides if _num(f.get(k))]
        out['features'][k] = round(statistics.median(vals), 3) if vals else None
    return out


def _z(value, base):
    if not _num(value) or not _num(base):
        return None
    return round((value - base) / max(0.02, abs(base) * 0.25), 2)      # a quarter of the baseline as one step


def character(feat, base=None):
    """-> {'label', 'why', 'vs', 'baseline_rides', 'caveats'} (see the module docstring)."""
    base = base or {'rides': 0, 'features': {}}
    bf = base.get('features') or {}
    n = int(base.get('rides') or 0)
    caveats = []
    if not feat.get('records'):
        return {'label': 'too short', 'why': ['there is no ride log to read'], 'vs': {}, 'baseline_rides': n,
                'caveats': ['no telemetry']}
    if (feat.get('moving_s') or 0) < MIN_MOVING_S:
        return {'label': 'too short', 'why': ['less than three minutes of moving time: not enough to describe'],
                'vs': {}, 'baseline_rides': n, 'caveats': ['too short']}
    if not feat.get('has_motion'):
        caveats.append('no motion data (lean and acceleration missing): pace only')
    if feat.get('cal_learning'):
        caveats.append('lean and acceleration were still calibrating at the start')
    if not feat.get('has_speed'):
        caveats.append('no speed data')
    if feat.get('brake_p95') is None and feat.get('has_motion'):
        caveats.append('no braking/acceleration data')
    vs, why = {}, []
    for k in ('brake_p95', 'accel_p95', 'stopped_share', 'slow_share', 'stops_per_km', 'lean_share', 'top_kmh'):
        z = _z(feat.get(k), bf.get(k))
        if z is not None:
            vs[k] = {'value': feat.get(k), 'baseline': bf.get(k), 'z': z}
    if n < MIN_BASELINE:
        caveats.insert(0, f'not enough history for a baseline ({n} ride{"s" if n != 1 else ""} with 3+ min moving)')
        label, why = _own_terms(feat)
    else:
        label, why = _against_baseline(feat, vs)
    return {'label': label, 'why': why, 'vs': vs, 'baseline_rides': n, 'caveats': caveats}


def _own_terms(f):
    """Fewer than 5 rides behind us: absolute thresholds, the ride described on its own terms."""
    if (f.get('stopped_share') or 0) >= 0.35:
        return 'stop-start', ['a lot of the ride was standing still']
    if (f.get('slow_share') or 0) >= 0.5:
        return 'congested filtering', ['most of the moving time was slow, stop-and-go traffic pace']
    if (f.get('brake_p95') or 0) >= 0.35 or (f.get('accel_p95') or 0) >= 0.3:
        return 'spirited', ['hard braking or hard acceleration for a road ride']
    return 'calm', ['steady pace without hard braking or acceleration']


def _against_baseline(f, vs):
    z = {k: v['z'] for k, v in vs.items()}

    def zz(k):
        return z.get(k) or 0.0

    scores = {'spirited': max(zz('brake_p95'), zz('accel_p95')),
              'stop-start': max(zz('stopped_share'), zz('stops_per_km')),
              'congested filtering': zz('slow_share')}
    best = max(scores, key=lambda k: scores[k])
    why = []
    if scores[best] >= 1.0:
        if best == 'spirited':
            why.append('braking and acceleration are harder than your usual rides')
        elif best == 'stop-start':
            why.append('you stopped more often than on your usual rides')
        else:
            why.append('more of the ride was slow filtering than usual')
        if zz('lean_share') >= 1.0:
            why.append('and you leaned further than usual')
        return best, why
    if zz('brake_p95') <= 0.25 and zz('accel_p95') <= 0.25:
        if zz('brake_p95') <= -0.5 and zz('accel_p95') <= -0.5:
            return 'calm', ['gentler braking and acceleration than your usual rides']
        return 'calm', ['in line with your usual rides: no hard braking or acceleration']
    return 'mixed', ['a mix: more than usual of some things, less of others']


def ride_character(paths, rides_dir=None):
    """features + character for one ride (one path or a session's logs), its baseline from ``rides_dir``."""
    if isinstance(paths, (str, bytes, os.PathLike)):
        paths = [paths]
    feat = features(paths)
    cands = []
    if rides_dir and os.path.isdir(rides_dir):
        cands = [os.path.join(rides_dir, n) for n in sorted(os.listdir(rides_dir)) if n.endswith('.jsonl')]
    base = baseline(cands, exclude=paths)
    out = character(feat, base)
    out['ride'] = {'logs': [str(os.path.abspath(p)) for p in paths], 'records': feat.get('records'),
                   't_start': feat.get('t_start'), 't_end': feat.get('t_end'),
                   'duration_s': feat.get('duration_s'), 'moving_s': feat.get('moving_s'),
                   'distance_km': feat.get('distance_km')}
    out['features'] = {k: feat.get(k) for k in FEATURES}
    out['generated'] = int(time.time() * 1000)
    return out
