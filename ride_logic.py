"""hermes-ride: pure turn logic (no I/O, no Hermes imports). Unit-tested in tests/test_logic.py.

Ported from the standalone Hermes Ride app (noise filter, barge-in rule, turn queue, sensor context line,
speech text, sentence splitter) plus the WAV packing used for Hermes's streaming-TTS PCM.
"""
import array
import math
import re
import struct
import sys
import time
import unicodedata

# ---------------------------------------------------------------- noise filter
# Scribe marks non-speech as (wind), [noise], [music], (engine revving) etc.
_TAG = re.compile(r'\([^)]*\)|\[[^\]]*\]|\*[^*]*\*')
_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*")
# Fix round 2 (R5): typographic apostrophes and hyphens count like ASCII ones, so "don\u2019t stop" is two words
# like "don't stop" and "mm\u2011hm" is a backchannel like "mm-hm". NFKC (fullwidth forms, compatibility
# characters) plus the apostrophe/hyphen look-alikes NFKC keeps.
_APOS = re.compile('[\u2018\u2019\u201a\u201b\u02b9\u02bb\u02bc\u02bd\u0060\u00b4\u2032\u2035\ua78c]')
_HYPH = re.compile('[\u2010\u2011\u2012\u2043\u00ad\ufe63]')


def normalize_text(text):
    fold = lambda t: _HYPH.sub('-', _APOS.sub("'", t))   # noqa: E731  (before NFKC too: it decomposes U+00B4)
    return fold(unicodedata.normalize('NFKC', fold(text or '')))


def strip_tags(text):
    return re.sub(r'\s+', ' ', _TAG.sub(' ', normalize_text(text))).strip()


def count_words(text):
    return len(_WORD.findall(strip_tags(text)))


def filter_transcript(text, min_words=2):
    """Final transcript -> (clean_text, None) to send, or (None, reason) to drop."""
    raw = (text or '').strip()
    if not raw:
        return None, 'empty'
    clean = strip_tags(raw)
    if not _WORD.search(clean):
        return None, 'noise_tags_only' if _TAG.search(raw) else 'empty'
    if count_words(clean) < min_words:
        return None, 'too_short'
    return clean, None


# ---------------------------------------------------------------- barge-in
BACKCHANNEL = {'yeah', 'yep', 'okay', 'ok', 'mm', 'mhm', 'right', 'uh-huh', 'uh', 'huh', 'sure', 'cool', 'yes',
               'mm-hm', 'mmhm', 'hmm'}


def is_backchannel(text):
    words = [w.lower() for w in re.findall(r"[^\W_]+(?:[-'][^\W_]+)*", strip_tags(text))]
    return bool(words) and all(w in BACKCHANNEL for w in words)


def should_barge_in(partial_text, agent_speaking, min_words=3, started_during=True):
    """Barge-in only for speech that started while the agent's audio was playing, with >= min_words real
    words, and not just a backchannel ("yeah okay right")."""
    return (bool(agent_speaking) and bool(started_during) and count_words(partial_text) >= min_words
            and not is_backchannel(partial_text))


# ---------------------------------------------------------------- adaptive end of turn (W6.2)
_OPEN_END = {'and', 'or', 'but', 'so', 'the', 'a', 'uh', 'um', 'because', '-'}


def eot_wait(text, question_s=1.2, default_s=1.8, open_s=3.5):
    """Seconds of silence after a committed segment before it becomes a turn: short after a question, longer
    when the rider stopped mid-sentence (no terminal punctuation, or a trailing conjunction/article/filler)."""
    t = strip_tags(text).strip()
    if not t:
        return open_s
    last = re.split(r'\s+', t)[-1].lower().strip('"\')\']')
    if t.endswith(('-', '\u2014', '\u2013', '...', '\u2026')) or last.rstrip('.,!?') in _OPEN_END:
        return open_s
    if t.rstrip('"\')\']').endswith('?'):
        return question_s
    if t.rstrip('"\')\']').endswith(('.', '!')):
        return default_s
    return open_s


# ---------------------------------------------------------------- filler replies (W6.3)
_FILLERS = {'mm-hm', 'mm hm', 'mhm', 'mm', 'go on', 'okay', 'ok', "i'm listening", 'im listening', 'yeah', 'sure',
            'uh-huh', 'uh huh', 'okay go on', 'yeah go on', 'sure go on', "okay i'm listening", 'yes', 'right',
            'got it', 'okay sure', 'sure thing', 'yeah sure', 'mm-hm go on', "yeah i'm listening"}


def is_filler(text):
    """A reply that is only a listening noise ("Mm-hm.", "Go on.", "I'm listening.") is not worth speaking."""
    t = strip_markdown(text).lower()
    t = re.sub(r"[^\w\s'-]", ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    if not t or len(t.split()) > 3:
        return False
    return t in _FILLERS or t.replace(',', '') in _FILLERS


_WAKE_HEAD = r'''^[\s"'\u201c\u2018(\[-]*(?:hey[\s,.!?;:-]+)?('''
_WAKE_TAIL = r''')\b[\s,.!?;:"'\u201d\u2019)\]-]*'''

# 10 Nov (Hermes-Ride): the wake word is a setting (``wake_word``). The public default is "Hey Hermes"; a personal
# name is a config value, never the code default. Each entry: the phrase shown, the name the phone sends in its wake
# frame, the transcript spellings that count as the name (STT variants), and the sherpa keywords file in
# static/vendor/ (tools/make_keywords.py, one file per entry: the model lexicon is not shipped, so no runtime build).
WAKE_WORDS = {
    'hey hermes': {'word': 'Hey Hermes', 'name': 'hermes', 'alts': ('hermes', 'hermès', 'hermez', 'hermies'),
                   'keywords': 'kws-words-hey-hermes.txt'},
    'hey lana': {'word': 'Hey Lana', 'name': 'lana', 'alts': ('lana', 'lanna', 'lena', 'llana', 'alana', 'helana'),
                 'keywords': 'kws-words-hey-lana.txt'},
}
DEFAULT_WAKE_WORD = 'hey hermes'


def wake_word(setting):
    """A ``wake_word`` setting ("Hey Lana", "lana", " HEY, LANA ") -> its WAKE_WORDS entry plus ``key``; anything
    unknown or empty -> the default."""
    s = re.sub(r'[^a-z]+', ' ', str(setting or '').lower()).strip() if isinstance(setting, str) else ''
    key = s if s in WAKE_WORDS else ('hey ' + s if 'hey ' + s in WAKE_WORDS else DEFAULT_WAKE_WORD)
    return {'key': key, **WAKE_WORDS[key], 'alts': list(WAKE_WORDS[key]['alts'])}


_WAKE_RES = {}


def _wake_re(alts):
    k = tuple(alts)
    if k not in _WAKE_RES:
        _WAKE_RES[k] = re.compile(_WAKE_HEAD + '|'.join(re.escape(a) for a in k) + _WAKE_TAIL, re.I)
    return _WAKE_RES[k]


def whistle_text(text):
    """Whistle's own formatting removed: what it wrote for a phrase, without the quotation marks it wraps speech in
    and with runs of whitespace collapsed ("\"Hey, Hermes, how fast am I going?\"" -> "Hey, Hermes, how fast am I
    going?"). Letters, punctuation and the wake spellings are untouched (see WAKE_WORDS)."""
    return re.sub(r'\s+', ' ', str(text or '').replace('"', ' ')).strip()


def strip_wake(text, alts=None):
    """The utterance without a leading wake word ("Hey, Hermes. How fast..." -> "How fast..."); the same rule as the
    page's transcript engine (static/wake.js). The pre-roll carries the wake phrase into STT on both routes.
    ``alts``: the configured name's spellings (default: the default wake word's)."""
    m = _wake_re(alts or WAKE_WORDS[DEFAULT_WAKE_WORD]['alts']).match(text or '')
    return (text[m.end():].strip() if m else (text or '')), bool(m)


STALE_NOTE = "(Your previous reply wasn't played; the rider kept talking. Answer everything they said.)"


# ---------------------------------------------------------------- turn queue
class TurnQueue:
    """Finished utterances waiting for the agent. Everything queued is joined into the next turn.
    Each utterance carries the connection id of the page whose transcript created it."""

    def __init__(self):
        self.items = []

    def add(self, text, t_end_speech=None, t_final=None, epoch=None):
        self.items.append({'text': text, 't_end_speech': t_end_speech, 't_final': t_final, 'epoch': epoch})

    def __len__(self):
        return len(self.items)

    def has_epoch(self, epoch):
        return any(i['epoch'] == epoch for i in self.items)

    def drop_except(self, epoch):
        """Remove (and return) every utterance whose epoch is not `epoch` (its page is gone)."""
        keep, gone = [], []
        for i in self.items:
            (keep if epoch is not None and i['epoch'] == epoch else gone).append(i)
        self.items = keep
        return gone

    def take(self):
        """Pop all queued utterances as one turn: {'text', 'parts', 't_end_speech', 't_final', 'epoch'}."""
        if not self.items:
            return None
        items, self.items = self.items, []
        return {
            'text': ' '.join(i['text'] for i in items),
            'parts': len(items),
            # latency is measured from the LAST utterance (the one the rider is waiting on)
            't_end_speech': items[-1]['t_end_speech'],
            't_final': items[-1]['t_final'],
            'epoch': items[-1]['epoch'],
        }


# ---------------------------------------------------------------- sensors / context line
_DIRS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']


def compass_point(deg):
    return _DIRS[int(((deg % 360) + 22.5) // 45) % 8]


# Components of the phone's 1 Hz summary. Each comes with the age (ms) of its source sample, measured on the
# phone when the message was sent: gps_age = now - fix time, mot_age = now - last motion event.
# 'hdg' is the GPS course; 'chdg' the compass heading (motion component, used when there is no GPS course).
SENSOR_COMPONENTS = {'gps': ('gps_age', ('spd', 'hdg', 'lat', 'lon', 'acc')),
                     'mot': ('mot_age', ('lean', 'peak', 'chdg'))}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def sensor_ages(msg, received_at, now):
    """Source age in s of each component at `now` = age the phone measured at send + time since receipt.
    `received_at`/`now` are server monotonic times. No (valid) age field -> that component is absent (None)."""
    ages = {}
    for comp, (field, _) in SENSOR_COMPONENTS.items():
        a = (msg or {}).get(field)
        ok = _num(a) and a >= 0
        ages[comp] = a / 1000 + max(0.0, now - received_at) if ok and received_at is not None else None
    return ages


def describe_sensors(s, ages, max_age_s=10):
    """Sensor summary dict from the phone + per-component source ages in s ({'gps': .., 'mot': ..})
    -> the text after 'Sensors'. Components older than max_age_s (or without an age) are left out;
    the age shown is the source age of the oldest component used. Non-numeric values are ignored."""
    s = s or {}
    fresh = {c for c, a in (ages or {}).items() if a is not None and a <= max_age_s}
    parts, used = [], set()

    def get(comp, k):
        v = s.get(k) if comp in fresh else None
        return v if _num(v) else None

    if get('gps', 'spd') is not None:
        parts.append(f"{round(s['spd'])} km/h")
        used.add('gps')
    if get('gps', 'hdg') is not None:
        parts.append(f"heading {compass_point(s['hdg'])}")
        used.add('gps')
    elif get('mot', 'chdg') is not None:
        parts.append(f"heading {compass_point(s['chdg'])}")
        used.add('mot')
    if get('gps', 'lat') is not None and get('gps', 'lon') is not None:
        pos = f"{s['lat']:.4f},{s['lon']:.4f}"
        if get('gps', 'acc') is not None:
            pos += f" ±{round(s['acc'])} m"
        parts.append(pos)
        used.add('gps')
    if get('mot', 'lean') is not None:
        lean = round(s['lean'])
        parts.append('lean 0°' if lean == 0 else f"lean {abs(lean)}° {'right' if lean > 0 else 'left'}")
        used.add('mot')
    if get('mot', 'peak') is not None:
        parts.append(f"peak accel {s['peak']:.1f} m/s² in last 5 s")
        used.add('mot')
    if not parts:
        return 'Sensors: none (screen locked or no fix)'
    return f"Sensors ({round(max(ages[c] for c in used))} s old): " + ', '.join(parts)


def build_turn(rider_text, sensors, sensor_ages_s, max_age_s=10, interrupted=False, rider_name='the rider',
               stale=False, since=None, ride_log=None):
    """The text the agent gets for one turn: one context line (+ the since-last-turn block) + the rider's words."""
    ctx = f'Ride voice mode: {rider_name} is riding a motorcycle and hears your reply through the helmet.'
    if interrupted:
        ctx += ' They interrupted your previous reply, so they did not hear all of it.'
    if stale:
        ctx += ' ' + STALE_NOTE
    ctx += ' ' + describe_sensors(sensors, sensor_ages_s, max_age_s)
    if since:
        ctx += ' ' + since
    if ride_log:
        ctx += f' Full 1 Hz ride log (JSONL, read it with your tools only if asked about the route): {ride_log}'
    return f'[{ctx}]\n{rider_text}'


PLATFORM_HINT = (
    "You are talking with a motorcyclist through the Ride voice platform: their words are speech-to-text "
    "from a helmet microphone (expect road noise and transcription slips), and your reply is read aloud "
    "into their helmet. Reply in 1 to 3 short spoken sentences. No markdown, lists, tables, emoji, URLs or "
    "code: everything you write is spoken. Lead with the answer. For directions, give one direction at a "
    "time, from their current position only. If they ask for a route or a place without saying where they "
    "are going, ask for the destination rather than assume it. Never answer with a filler alone (no bare "
    "'mm-hm', 'go on' or 'I'm listening'); if there is nothing to say, say nothing. Each message starts "
    "with a bracketed context line with live phone sensors (speed, heading, position, lean angle, peak "
    "acceleration), their age and what happened since the last turn; use them when relevant, never read "
    "the line back, and treat 'Sensors: none' as no data. Don't claim anything the sensors can't show, "
    "and don't give a false 'I can't': if something isn't possible from here, offer to log it as a to-do "
    "instead. Never encourage looking at the phone, and keep safety in mind without lecturing. The rider "
    "cannot tap buttons: anything needing approval has to be approved in another Hermes app or it times out.")


# ---------------------------------------------------------------- speech text
_EMOJI = re.compile('[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]')


def strip_markdown(text):
    t = text or ''
    t = re.sub(r'<think[\s>].*?</think>', ' ', t, flags=re.S)
    t = re.sub(r'```.*?```', ' ', t, flags=re.S)
    t = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', t)          # [label](url) -> label
    t = re.sub(r'https?://\S+|www\.\S+', 'a link', t)
    t = re.sub(r'^\s{0,3}#{1,6}\s*', '', t, flags=re.M)         # headings
    t = re.sub(r'^\s*(?:[-*+•]|\d+[.)])\s+', '', t, flags=re.M)  # list bullets
    t = re.sub(r'^\s*>\s?', '', t, flags=re.M)                  # quotes
    t = re.sub(r'(\*\*|__|\*|_|~~|`)', '', t)
    t = _EMOJI.sub('', t)
    t = re.sub(r'\s+', ' ', t)
    return t.strip()


class SentenceSplitter:
    """Feed text; get back complete sentences as soon as they end."""
    _END = re.compile(r'[.!?…]+["\')\]]*\s+|\n+')

    def __init__(self, min_chars=12):
        self.buf = ''
        self.min_chars = min_chars

    def feed(self, delta):
        self.buf += delta
        out = []
        start = 0
        for m in self._END.finditer(self.buf):
            cand = self.buf[start:m.end()]
            if len(cand.strip()) >= self.min_chars:
                out.append(cand.strip())
                start = m.end()
        self.buf = self.buf[start:]
        return out

    def flush(self):
        rest, self.buf = self.buf.strip(), ''
        return [rest] if rest else []


def sentences(text, min_chars=12):
    """A whole reply -> its sentences, in order."""
    sp = SentenceSplitter(min_chars)
    return sp.feed(text) + sp.flush()


# ---------------------------------------------------------------- PCM -> WAV (Hermes streaming TTS)
def wav_bytes(pcm, sample_rate=24000, channels=1, sample_width=2):
    """Raw little-endian PCM -> a complete WAV file (RIFF header + data)."""
    if len(pcm) % (channels * sample_width):
        pcm = pcm[:len(pcm) - len(pcm) % (channels * sample_width)]
    byte_rate = sample_rate * channels * sample_width
    header = struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF', 36 + len(pcm), b'WAVE', b'fmt ', 16, 1, channels,
                         sample_rate, byte_rate, channels * sample_width, sample_width * 8, b'data', len(pcm))
    return header + pcm


class PcmSegmenter:
    """Collects streaming PCM chunks and cuts them into playable segments.

    A segment is cut when the producer pauses (the gap between two synthesised clauses) or when it grows past
    `max_s` seconds, so playback starts after the first clause instead of after the whole reply. The caller
    drives time: `add(chunk, now)` returns segments that are ready; `poll(now)` cuts after a pause.

    Fix H (item 4, the pop while Lana speaks): each segment is its own <audio> source, so a segment that ends or
    starts mid-waveform clicks. A size cut goes to the end of the quietest `quiet_s` (10 ms) window in the last
    `search_s` (1 s) of the buffer and carries the rest into the next segment (ties: the latest window, so a silent
    buffer is cut at its end); every segment gets a `fade_s` (2 ms) linear fade-in and fade-out, its edge samples 0.
    16-bit PCM only; other widths are cut as before, unfaded."""

    def __init__(self, sample_rate=24000, channels=1, sample_width=2, gap_s=0.15, max_s=6.0, min_s=0.25,
                 fade_s=0.002, search_s=1.0, quiet_s=0.01):
        self.rate, self.channels, self.width = sample_rate, channels, sample_width
        self.frame = channels * sample_width
        self.gap_s, self.max_bytes = gap_s, int(max_s * sample_rate) * self.frame
        self.min_bytes = int(min_s * sample_rate * channels * sample_width)
        self.fade = max(1, int(fade_s * sample_rate))
        self.search, self.quiet = int(search_s * sample_rate), max(1, int(quiet_s * sample_rate))
        self.buf = bytearray()
        self.last = None

    def add(self, chunk, now):
        out = []
        if self.buf and self.last is not None and now - self.last >= self.gap_s and len(self.buf) >= self.min_bytes:
            out.append(self._cut())
        self.buf += chunk
        self.last = now
        while len(self.buf) >= self.max_bytes:
            out.append(self._cut(self._quiet_cut()))
        return out

    def poll(self, now):
        if self.buf and self.last is not None and now - self.last >= self.gap_s and len(self.buf) >= self.min_bytes:
            return [self._cut()]
        return []

    def flush(self):
        return [self._cut()] if self.buf else []

    def _quiet_cut(self):
        """Byte offset of the cut: the end of the quietest window in the last search_s of the buffer."""
        frames = len(self.buf) // self.frame
        if self.width != 2:
            return frames * self.frame
        pcm = array.array('h', bytes(self.buf[:frames * self.frame]))
        if sys.byteorder == 'big':
            pcm.byteswap()
        best_end, best = frames, None
        n = min(self.search, frames - 1) // self.quiet
        for k in range(n):                       # k = 0 is the window that ends at the end of the buffer
            hi = (frames - k * self.quiet) * self.channels
            e = sum(v * v for v in pcm[hi - self.quiet * self.channels:hi])
            if best is None or e < best:
                best, best_end = e, frames - k * self.quiet
        return best_end * self.frame

    def _cut(self, at=None):
        at = len(self.buf) if at is None else at
        seg, self.buf = bytes(self.buf[:at]), bytearray(self.buf[at:])
        return wav_bytes(self._faded(seg), self.rate, self.channels, self.width)

    def _faded(self, seg):
        if self.width != 2:
            return seg
        frames = len(seg) // self.frame
        pcm = array.array('h', seg[:frames * self.frame])
        if sys.byteorder == 'big':
            pcm.byteswap()
        n, c = min(self.fade, frames // 2), self.channels
        for i in range(n):                       # factor i/n in, (n-1-i)/n out: the edge samples are exactly 0
            for ch in range(c):
                pcm[i * c + ch] = int(pcm[i * c + ch] * i / n)
                j = (frames - 1 - i) * c + ch
                pcm[j] = int(pcm[j] * i / n)
        if sys.byteorder == 'big':
            pcm.byteswap()
        return pcm.tobytes() + seg[frames * self.frame:]


def now():
    return time.time()


# ---------------------------------------------------------------- usage and cost (W9)
PRICES = {   # USD; https://elevenlabs.io/pricing/api (read 2026-11-08). LLM tokens are reported, not priced.
    'scribe_realtime_per_hour': 0.39,
    'tts_flash_per_1k_chars': 0.04,
}


def mp3_seconds(nbytes, kbps=32):
    return nbytes * 8 / (kbps * 1000)


def wav_seconds(data):
    """Duration of a canonical PCM WAV (44-byte header) from its header fields; 0 if unreadable."""
    try:
        rate, bits, chans = struct.unpack_from('<I', data, 24)[0], struct.unpack_from('<H', data, 34)[0], \
            struct.unpack_from('<H', data, 22)[0]
        return max(0, len(data) - 44) / (rate * chans * bits / 8)
    except (struct.error, ZeroDivisionError):
        return 0.0


USAGE_KEYS = ('stt_audio_s', 'tts_chars', 'tts_audio_s', 'llm_input_tokens', 'llm_output_tokens',
              'llm_cache_read_tokens', 'llm_requests')


def usage_totals(turn_recs, prices=PRICES):
    """Ride-end summary from per-turn usage dicts: totals plus the estimated ElevenLabs cost."""
    tot = {k: 0 for k in USAGE_KEYS}
    for r in turn_recs:
        for k in USAGE_KEYS:
            v = (r or {}).get(k) or 0
            tot[k] += v
    tot = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in tot.items()}
    stt = tot['stt_audio_s'] / 3600 * prices['scribe_realtime_per_hour']
    tts = tot['tts_chars'] / 1000 * prices['tts_flash_per_1k_chars']
    tot['turns'] = len(turn_recs)
    tot['cost_usd'] = {'stt': round(stt, 4), 'tts': round(tts, 4), 'total': round(stt + tts, 4),
                       'prices': dict(prices)}
    return tot


# ---------------------------------------------------------------- protocol v2: binary audio frames
# server -> phone: 1 byte type + uint32 turn + uint16 seq (big-endian) + one playable audio file.
# A binary frame belongs to the epoch of the socket it arrives on (one socket = one epoch).
AUDIO_TYPES = {1: 'audio/mpeg', 2: 'audio/wav', 3: 'audio/mp4', 4: 'audio/ogg', 5: 'audio/aac', 6: 'audio/flac'}
_AUDIO_TYPE_OF = {v: k for k, v in AUDIO_TYPES.items()}
_FRAME = struct.Struct('>BIH')


def encode_audio_frame(mime, turn, seq, payload):
    return _FRAME.pack(_AUDIO_TYPE_OF.get(mime, 1), turn & 0xFFFFFFFF, seq & 0xFFFF) + bytes(payload)


def decode_audio_frame(data):
    """-> (mime, turn, seq, payload); ValueError on a short frame or an unknown type."""
    if len(data) < _FRAME.size:
        raise ValueError('short audio frame')
    kind, turn, seq = _FRAME.unpack_from(data)
    if kind not in AUDIO_TYPES:
        raise ValueError(f'unknown audio frame type {kind}')
    return AUDIO_TYPES[kind], turn, seq, bytes(data[_FRAME.size:])


# ---------------------------------------------------------------- STT failures: backoff, one notice per episode
STT_BACKOFF_CODES = {'quota_exceeded', 'rate_limited', 'auth_error', 'resource_exhausted', 'unaccepted_terms',
                     'token_mint_failed'}


class SttBackoff:
    """Quota/auth/rate errors from Scribe (phone or server route). Delays 5 s, 15 s, 60 s, 5 min (cap). An
    episode starts at the first failure after a success; the caller announces only the first one."""
    SCHEDULE = (5, 15, 60, 300)

    def __init__(self):
        self.fails = 0
        self.until = 0.0

    def fail(self, now_s):
        """-> (delay_s, first_of_episode)"""
        delay = self.SCHEDULE[min(self.fails, len(self.SCHEDULE) - 1)]
        first = self.fails == 0
        self.fails += 1
        self.until = now_s + delay
        return delay, first

    def ok(self):
        self.fails, self.until = 0, 0.0

    def wait_left(self, now_s):
        return max(0.0, self.until - now_s)


# ---------------------------------------------------------------- secrets in logs
_TOKEN = re.compile(r'sutkn_[A-Za-z0-9_\-]+')
_LONG = re.compile(r'[A-Za-z0-9_\-]{24,}')
_EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
# Fix round 2 (R6): a closed allowlist of our own long labels, not "any snake_case" (an access key may be one).
# An unknown long label is redacted: over-redaction, never a leak.
KNOWN_LABELS = frozenset({
    'dropped_stale_connection', 'said_more_while_thinking', 'session_time_limit_exceeded',
    'insufficient_audio_activity', 'committed_transcript_with_timestamps', 'include_language_detection',
    'vad_silence_threshold_secs', 'scribe_realtime_per_hour', 'interim_assistant_messages',
    'audio_sent_after_speech_end', 'speech_end_to_first_audio', 'speech_end_to_ready_state',
    'speech_end_to_reply_done', 'first_audio_to_last_audio',
})
_KNOWN_SECRETS = set()
_FRAGMENT = 8                                  # a secret's prefix/suffix this long or longer is redacted too


def remember_secret(value):
    """Register a credential value read at runtime: redact() then removes it, and any prefix or suffix of it of
    at least 8 characters (what is left when provider text cuts a key at a length limit), wherever it appears.
    Returns the value unchanged."""
    if isinstance(value, str) and len(value) >= _FRAGMENT:
        _KNOWN_SECRETS.add(value)
    return value


def _cut_secrets(s):
    for sec in _KNOWN_SECRETS:
        if sec in s:
            s = s.replace(sec, '<redacted>')
        for n in range(len(sec) - 1, _FRAGMENT - 1, -1):
            for frag in (sec[:n], sec[-n:]):
                if frag in s:
                    s = s.replace(frag, '<redacted>')
    return s


def redact(s):
    """Log-safe text: known secrets (and their fragments), ElevenLabs single-use tokens (sutkn_...), e-mail
    addresses and any 24+ char run of token characters (API keys, access keys, ids) except our own labels."""
    s = _cut_secrets(str(s or ''))
    s = _TOKEN.sub('<token>', s)
    s = _EMAIL.sub('<email>', s)
    return _LONG.sub(lambda m: m.group(0) if m.group(0) in KNOWN_LABELS else '<redacted>', s)


def sanitize(v, depth=0):
    """AUD-V2-08: redact() applied recursively to every string (keys included) of a record before it reaches a
    sink (turn log, ride log, live-driver dump). Numbers, bools and None pass unchanged."""
    if isinstance(v, str):
        return redact(v)
    if depth > 8:
        return '<deep>'
    if isinstance(v, dict):
        return {(redact(k) if isinstance(k, str) else k): sanitize(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [sanitize(x, depth + 1) for x in v]
    return v


# Every code the phone may report as stt_error (static/stt.js RIDE_STT_ERRORS) plus the server's own; anything
# else is logged as 'other', so a phone-supplied string never becomes a log or storage field.
STT_ERROR_CODES = STT_BACKOFF_CODES | {'whistle_failed', 'error', 'commit_throttled', 'queue_overflow', 'session_time_limit_exceeded',
                                       'input_error', 'invalid_request', 'chunk_size_exceeded',
                                       'insufficient_audio_activity', 'transcriber_error',
                                       'session_timeout', 'session_closed'}


def known(v, allowed, other='other'):
    """A phone-supplied label -> itself if it is one of `allowed`, else `other`."""
    return v if isinstance(v, str) and v in allowed else other
