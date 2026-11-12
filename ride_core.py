"""hermes-ride core: phone page server, turn queue, endpointing, spoken-reply delivery (protocol v2).

No Hermes imports here: the gateway adapter (adapter.py) wires this to the agent through three callables, so
the whole voice pipeline is unit-testable with fakes (tests/test_core.py).

v2 is "web app first": the phone does mic gating (local VAD), the wake word, cues/colours/status clips and
telemetry processing, and by default talks to ElevenLabs Scribe itself with a single-use token minted here. The
VPS sees text, VAD edges and telemetry, keeps the keys, the turn queue and end-of-turn authority, runs the Hermes
turns and TTS.

    phone --(token)--> Scribe realtime (direct, stt_route=phone)        partial/committed text -> RideCore
    phone --gated PCM--> RideCore -> Scribe (stt_route=server, the v0.1 relay, open only while audio flows)
    RideCore -> submit(turn, text, meta) -> adapter -> gateway handle_message()
    adapter.send()/send_voice()/streaming TTS -> RideCore -> binary audio frames -> phone <audio>
    adapter.on_processing_complete() -> RideCore.turn_done()

WebSocket protocol v2 (/ws, cookie-gated). Every server -> phone JSON frame carries "epoch" = "<boot id>-<conn id>";
the page drops all audio/turn state when the epoch changes and ignores JSON frames of any other epoch. Binary
frames belong to the epoch of the socket they arrive on.
  phone -> server  {"t":"hello","v":2,"ua":..,"caps":{"wake":"sherpa|transcript","stt":"phone|server","wake_ms"?:int}}
                   {"t":"wake","w":<wake name>,"score":0..1}       wake fired locally (the cue already played)
                   {"t":"vad","s":1|0,"at":ms}                 speech start/stop edges
                   {"t":"partial","text":..,"at":ms} / {"t":"committed","text":..,"lang":..,"at":ms,"stt_s":s}
                   {"t":"need_token"}                          -> {"t":"token"} (or {"t":"token_error"})
                   {"t":"stt_error","code":..} / {"t":"stt_ok"}
                   {"t":"sensors",...}                          current values (1 Hz while awake)
                   {"t":"telemetry","ride":id,"batch":[...]} / {"t":"summary",...}
                   {"t":"gate","s":"sleep|probe|awake|window|speaking"}  -> {"t":"wake_hit"|"wake_miss"}
                   {"t":"play","turn":N,"seq":i} / {"t":"idle","turn":N,"heard":bool} / {"t":"play_error",..}
                   {"t":"reset","epoch":E}   WP6: the rider held the reset bar; E must be this connection's epoch
                   {"t":"mic","ev":"revive","why":..,"ok":bool,"ac":..,"track":..,"gap_s":s,"n":i}   one per mic
                       revive attempt on the page (logged as a `mic` event, fixed labels only, 30 per connection)
                   binary: PCM16 LE mono 16 kHz, only with stt_route=server and the gate open
  server -> phone  {"t":"hello","v":2,"epoch":E,"cfg":{...}}   always the first frame of a connection
                   {"t":"state","s":"listening|thinking|extended|ready|working|quota", ...}
                   {"t":"token","kind":"realtime_scribe","token":..,"url":..,"params":{..}}   never logged
                   binary: 1 byte type (1 mp3, 2 wav, 3 mp4, 4 ogg) + uint32 turn + uint16 seq + audio file
                   {"t":"end","turn":N,"segs":n} / {"t":"stop","turn":N} / {"t":"hold"|"discard","turn":N}
                   {"t":"final"|"dropped"|"reply"|"stt_backoff",...}
                   {"t":"reset_done"} / {"t":"reset_failed","step":..}   WP6: the page reloads / says it failed
"""
import asyncio
import base64
import collections
import hmac
import json
import logging
import os
import re
import secrets
import time
from urllib.parse import urlencode

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

try:
    from . import ride_logic as rl
    from . import ride_telemetry as rt
except ImportError:          # loaded as a top-level module (tests, tools)
    import ride_logic as rl
    import ride_telemetry as rt

log = logging.getLogger('hermes_ride')


class _Redact(logging.Filter):
    """AUD-V2-08: every hermes_ride log record (message, arguments, traceback) goes out redacted."""

    def filter(self, record):
        record.msg, record.args = rl.redact(record.getMessage()), ()
        if record.exc_info:
            record.exc_text = rl.redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


if not any(isinstance(f, _Redact) for f in log.filters):
    log.addFilter(_Redact())
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
COOKIE = 'rk'
FALLBACK_REPLY = "Sorry, I couldn't get an answer just now."
PROTOCOL = 2
WHISTLE_CAP_S = 28              # a phrase is cut here (helper calls take at most 30 s of audio)

DEFAULTS = {
    'host': '127.0.0.1', 'port': 8790,
    'end_of_turn_silence_s': 1.2,    # Scribe VAD commit (both routes); W6.2 adds the adaptive wait on top
    'min_words': 2, 'barge_in_min_words': 3, 'sensor_max_age_s': 10,
    'rider_name': 'the rider',
    'scribe_url': 'wss://api.elevenlabs.io/v1/speech-to-text/realtime', 'stt_model_id': 'scribe_v2_realtime',
    'stt_language': 'en',            # English locked; '' = auto-detect
    'languages': 'en',               # secondary_languages hints; English only (AUD-V2-05 deferred)
    'keyterms': '',                  # comma list; off by default (+20 % Scribe cost)
    'filter_background_audio': True,
    'stt_route': 'phone',            # 'phone' (direct, single-use token) | 'server' (v0.1 relay, gated)
    # round 3 (B2): which speech-to-text hears a finished phrase on the server route. 'scribe' = today (realtime,
    # both routes); 'whistle' = the offline Whistle model on this host (stt_route is forced to 'server').
    'stt_engine': 'scribe', 'stt_fallback': 'scribe',       # 'scribe' | 'none': what a Whistle failure switches to
    'whistle_python': 'python3', 'whistle_weights': '', 'whistle_language': '',
    'whistle_end_s': 0.9,            # silence after the VAD-off edge before the phrase is transcribed (0.3..3)
    'min_phrase_s': 0.0,             # Whistle: a clip shorter than this (whole clip, silence included) is dropped; 0 = off
    'whistle_timeout_s': 20.0,       # one phrase: past this the helper is killed and the phrase fails
    'stt_idle_close_s': 4.0,         # server route: close the Scribe session after this long without audio
    'stt_start_timeout_s': 8.0,      # server route: a Scribe socket without session_started by then is closed
    'token_min_interval_s': 5.0,
    'tts_base_url': 'https://api.elevenlabs.io', 'tts_model_id': 'eleven_flash_v2_5',
    'voice_id': 'JBFqnCBsd6RMkjVDRZzb', 'tts_output_format': 'mp3_22050_32',
    'tts_engine': 'direct',          # 'direct' | 'hermes' (resolved by the adapter; 'auto' never reaches here)
    'turn_timeout_s': 900,           # safety net only: approvals can hold a turn for approvals.timeout
    'log_dir': None, 'log_max_bytes': 5_000_000, 'log_backups': 3,
    # phone-side settings, sent in hello.cfg
    'window_s': 10, 'vad_base_db': -38.0, 'vad_k_db': 0.15, 'vad_cap_db': -25.0, 'vad_hangover_ms': 2000,
    'vad_preroll_ms': 800, 'wake_preroll_ms': 1500, 'wake_engine': 'sherpa', 'wake_threshold': 0.25, 'wake_score': 1.0,
    'wake_refractory_ms': 2000, 'wake_load_timeout_ms': 8000,
    'wake_word': 'hey hermes',       # 10 Nov (Hermes-Ride): ride_logic.WAKE_WORDS; a personal name is config only
    'show_word': False,              # WP5: the display() word under the orb, a debug option now (was on)
    'orb_design': 'simple',          # WP5: the orb's look (orb.js design table; unknown names fall back to simple)
    'orb_motion': 1.0,               # P4: the orb moves and deforms with the bike (orb_motion.js gain; 0 = off, max 2)
    'tone_first_s': 1.2,             # WP2: the doo-doo starts this long after 'thinking' (a quick answer never hears it)
    'double_tap_ms': 350,            # WP4: two taps within this many ms toggle the soft mute on the phone
    'play_buffer_ms': 250,           # the phone holds the first segment of a reply this long before play() (0 = off)
    'cue_gain': 0.5,                 # R3-2: cues + status clips relative to their files (the reply voice stays 1)
    'telemetry_batch_s': 30, 'summary_s': 60, 'wake_model_url': 'vendor/',
    # turn-taking (W6) and failure voice (W8)
    'eot_question_s': 1.2, 'eot_default_s': 1.8, 'eot_open_s': 3.5, 'eot_max_s': 8.0, 'hold_resume_s': 1.5,
    'hold_max_s': 12.0, 'vad_trust_s': 1.5,
    'extended_after_s': 6.0, 'working_after_s': 25.0, 'finish_timeout_s': 300.0,
    # WP6 reset: the gateway's /new may spend up to 30 s on agent cleanup; past this the phone hears it failed and the
    # lane is freed (the gateway step finishes in the background). Replies with no running turn this long after a
    # reset are the old conversation's stragglers: not spoken.
    'reset_timeout_s': 45.0, 'reset_quiet_s': 5.0,
    # W9 prices (USD), https://elevenlabs.io/pricing/api read 2026-11-08; override per plan
    'price_stt_per_hour': 0.39, 'price_tts_per_1k_chars': 0.04,
}

CUES = {'wake': ['cues/wake_a.m4a', 'cues/wake_b.m4a'], 'thinking': 'cues/thinking.m4a',
        'done': 'cues/done.m4a', 'mute': 'cues/mute.m4a', 'unmute': 'cues/unmute.m4a',   # WP4: mute toggle cues
        'reset': 'cues/reset.m4a', 'reset_failed': 'cues/reset_failed.m4a'}                 # WP6: the reset bar
CLIPS = {k: f'clips/{k}.m4a' for k in ('reconnecting', 'back', 'quota', 'working', 'offline', 'nomic')}

# Round 3 (B1): {t:'mic'} evidence frames (a mic revive attempt on the page) are logged with these labels only
MIC_EV = ('revive',)
MIC_WHY = ('return', 'pageshow', 'no frames', 'tap', 'unmute', 'statechange', 'muted', 'retry')
MIC_AC = ('running', 'suspended', 'interrupted', 'closed', 'none')
MIC_TRACK = ('live', 'ended', 'muted', 'none')
MIC_EVENTS_MAX = 30          # per connection

redact = rl.redact


def _number(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def ms(a, b):
    return round((b - a) * 1000) if a is not None and b is not None else None


class TurnLog:
    """turns.jsonl (latency + dropped transcripts), size-based rotation. No-op without a log dir."""

    def __init__(self, log_dir, max_bytes=0, backups=3):
        self.path = os.path.join(log_dir, 'turns.jsonl') if log_dir else None
        self.max_bytes, self.backups = max_bytes, backups
        if self.path:
            os.makedirs(log_dir, exist_ok=True)

    def write(self, rec):
        if not self.path:
            return
        rec = rl.sanitize({k: (round(v, 3) if isinstance(v, float) else v) for k, v in rec.items()})   # AUD-V2-08
        line = json.dumps(rec, ensure_ascii=False) + '\n'
        try:
            if self.max_bytes and os.path.exists(self.path) \
                    and os.path.getsize(self.path) + len(line.encode()) > self.max_bytes:
                for i in range(self.backups - 1, 0, -1):
                    if os.path.exists(f'{self.path}.{i}'):
                        os.replace(f'{self.path}.{i}', f'{self.path}.{i + 1}')
                os.replace(self.path, self.path + '.1') if self.backups > 0 else os.remove(self.path)
            with open(self.path, 'a') as f:
                f.write(line)
        except OSError as e:
            log.warning('turn log write failed: %s', e)


class Turn:
    """One agent turn (kind 'turn') or a spoken message outside a turn (kind 'notice': cron delivery,
    approval timeout notice, ...)."""

    def __init__(self, tid, conn=None, kind='turn'):
        self.id = tid
        self.conn = conn           # connection id of the page this turn belongs to
        self.kind = kind
        self.cancelled = False     # barge-in or discarded as stale
        self.stale = False         # its page is gone: nothing more is sent or synthesised
        self.segs = 0
        self.played = asyncio.Event()
        self.finished = asyncio.Event()   # playback over on the phone (idle / stop / discard / page gone)
        self.play_start = None
        self.done = asyncio.Event()
        self.ok = True
        self.texts = []            # reply texts the gateway sent during this turn
        self.items = asyncio.Queue()   # speech pipeline: ('say', text) | ('seg', bytes, mime) | None
        self.speaker = None
        self.streams = 0           # open Hermes streaming-TTS handles
        self.rider_text = ''
        self.held = None           # stale-reply hold: list of (bytes, mime) kept back, or None
        self.hold_checked = False
        self.barged = False
        self.suppressed = False    # a filler reply was not spoken
        self.audio_started = None
        self.states = set()        # 'extended' kinds already sent (tool, search, subagent, long)
        self.working_sent = False
        self.usage = {}
        self.summarized = False    # already counted in a ride summary while in flight
        self.summary_snap = None   # its usage at that summary; the rest is reconciled when it finishes (R8)
        self.hook_turn = None      # Hermes turn_id of this turn (pre_llm_call), for hook ownership (AUD-V2-13)
        self.rec = {}


class RideCore:
    def __init__(self, cfg, submit, secret, http=None, tts=None, reset=None, whistle=None):
        """submit: async (turn, text, meta) -> None, hands one turn to the agent (adapter);
        secret: name -> str|None, credential read through Hermes; tts: async (text, prev) -> (bytes, t_first);
        reset: async () -> {'ok', 'step', 'old', 'new', 'override_left'}, the gateway part of the phone's reset (WP6);
        whistle: an object with start()/transcribe(pcm)->(text, ms)/close() (ride_whistle.WhistleEngine), or None to
        build one from the settings at the first Whistle page (round 3, B2)."""
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.wake = rl.wake_word(self.cfg['wake_word'])   # 10 Nov: the configured wake word (unknown -> default)
        self.submit = submit
        self.secret = lambda name: rl.remember_secret(secret(name))   # R6: every credential read is redactable
        self.http = http
        self.tts = tts or self.tts_elevenlabs
        self.queue = rl.TurnQueue()
        self.turns = TurnLog(self.cfg['log_dir'], self.cfg['log_max_bytes'], self.cfg['log_backups'])
        self.ws = None
        self.boot = secrets.token_hex(4)
        self.conn_no = 0
        self.conn = None
        self.route = self.cfg['stt_route']
        want = str(self.cfg.get('stt_engine') or 'scribe').strip().lower()
        if want not in ('scribe', 'whistle'):
            log.warning('stt_engine %r is not known: using scribe', want)
            want = 'scribe'
        self.stt_engine, self.conn_stt = want, want      # configured, and per page (a failure may switch it)
        self.route = 'server' if want == 'whistle' else self.cfg['stt_route']
        self.whistle = whistle       # injected (tests) or built by _whistle_engine()
        self.whistle_err = ''
        self.wh = None               # the phrase being buffered: {'epoch', 'conn', 'pcm', 'speech', 'vad_on', 'vad_off'}
        self.wh_jobs = []            # phrases being transcribed (the turn waits for their text)
        self.wh_watch = None
        self.wh_ready_epoch = None   # gate epoch whose opening was answered with {t:'stt', s:'ready'}
        self.wh_first = set()        # (conn, epoch) whose first phrase already ended
        self.caps = {}
        self.gate = None           # the phone's gate state ({"t":"gate"}); 'probe' = transcript wake check pending
        self.hook_ids = collections.deque(maxlen=256)   # Hermes turn_ids already bound to a ride turn (R9)
        self.gate_epoch = 0        # +1 whenever the gate enters 'sleep' (or a new phone connects); a server-route
        self.stt_epoch = None      # Scribe session belongs to the epoch it was opened in (fix round 2, R4)
        self.stt_ws = None
        self.stt_ok = False
        self.stt_start_fails, self.stt_start_retry_at = 0, 0.0   # server route: session-start misses (R2)
        self.stt_task = None
        self.stt_pending = []      # server route: PCM that arrived before the Scribe session started
        self.stt_last_audio = 0.0
        self.stt_seen = collections.Counter()
        self.stt_backoff = rl.SttBackoff()
        self.token_at = {}         # conn -> monotonic time of the last minted token
        self.worker = None
        self.turn_no = 0
        self.audio_turn = None     # Turn whose audio is on the phone (agent speaking)
        self.active = None         # Turn the agent is working on
        self.interrupted = False   # previous reply was cut by barge-in
        self.passthrough = False   # a clarify question is open: the next utterance answers it directly
        self.sensors = None
        self.sensors_at = None
        self.partial = ''
        self.partial_changed_at = None
        self.last_state = None
        self.loop = None
        self.bg = set()
        self.vad, self.vad_on_at, self.vad_off_at = 0, None, None
        self.partial_at = None
        self.heard_sent = 0.0          # R3-5: last {t:'heard'} to the page (server route), monotonic
        self.commit_at = None
        self.utt_start = None
        self.pending = []          # committed utterances waiting for the adaptive end of turn
        self.eot_task = None
        self.held_turn = None
        self.stale_prefix = []     # rider text of discarded (stale) turns, joined into the next turn
        self.stale_note = False
        self.stt_s_acc = 0.0       # phone route: seconds of gated audio the phone sent to Scribe since the last turn
        self.stt_usage_seen = False   # the page reports stt_usage (all audio sent); else committed stt_s (old page)
        self.unfinished = set()    # turns whose usage is not in ride_usage yet (AUD-V2-12)
        self.stt_bytes_mark = 0
        self.ride_usage = []
        ride_dir = self.cfg.get('ride_dir') or (os.path.join(os.path.dirname(self.cfg['log_dir']), 'rides')
                                                if self.cfg.get('log_dir') else None)
        self.ridelog = rt.RideLog(ride_dir)
        self.ride_id = None
        # P3 (10 Nov): what the Ride Summary prompt at a reset needs: the ride logs of this conversation (page loads
        # with telemetry since boot or the last reset) and how many turns it had. reset_state() clears both, after the
        # adapter's reset step read them.
        self.session_rides = []
        self.session_turns = 0
        self.since_mark = {}       # ride id -> ride-log records already given to the agent (AUD-V2-11)
        self.last_turn_ms = {}     # ride id -> epoch ms of its previous turn (live run 6: one global value cut
                                   # a new ride's first block to the time since another ride's turn)
        self.reset_gateway = reset # WP6: the adapter's interrupt + /new (None: this core can't reset the conversation)
        self.resetting = None      # connection id of the reset in flight (intake is frozen meanwhile)
        self.reset_conns = set()   # connections that already reset: a replayed message can't reset twice
        self.quiet_until = 0.0     # monotonic: until then a reply with no running turn is a straggler, not spoken
        self.mic_events = 0        # B1: {t:'mic'} events logged for the attached connection (cap MIC_EVENTS_MAX)

    # ------------------------------------------------------------ client link
    def epoch(self, cid):
        return f'{self.boot}-{cid}'

    def client_cfg(self):
        """Settings the page needs (hello.cfg). No secrets."""
        c = self.cfg
        return {
            'stt_route': 'server' if self.conn_stt == 'whistle' else c['stt_route'],
            'stt_engine': self.conn_stt, 'window_s': c['window_s'],
            'vad': {'base_db': c['vad_base_db'], 'k': c['vad_k_db'], 'cap_db': c['vad_cap_db'],
                    'hangover_ms': c['vad_hangover_ms'], 'preroll_ms': c['vad_preroll_ms'],
                    'wake_preroll_ms': c['wake_preroll_ms']},
            'wake': {'engine': c['wake_engine'], 'threshold': c['wake_threshold'], 'score': c['wake_score'],
                     'refractory_ms': c['wake_refractory_ms'], 'load_timeout_ms': c['wake_load_timeout_ms'],
                     'model_url': c['wake_model_url'], 'word': self.wake['word'], 'name': self.wake['name'],
                     'alts': list(self.wake['alts']), 'kws': self.wake['keywords']},   # kws: no 'key' in hello (test_protocol)
            'cues': CUES, 'clips': CLIPS, 'word': bool(c['show_word']), 'cue_gain': c['cue_gain'],
            'tone_first_s': c['tone_first_s'], 'double_tap_ms': c['double_tap_ms'], 'orb_design': c['orb_design'],
            'orb_motion': c['orb_motion'],
            'play_buffer_ms': c['play_buffer_ms'],
            'telemetry_batch_s': c['telemetry_batch_s'], 'summary_s': c['summary_s'],
        }

    async def attach(self, ws):
        """Make `ws` the one page. Returns its connection id (None if a newer page attached meanwhile)."""
        self.loop = asyncio.get_running_loop()
        self.conn_stt = self.stt_engine          # a new page starts on the configured engine (hello.cfg says which)
        self.conn_no += 1
        cid = self.conn_no
        try:   # hello goes out before `ws` is reachable by anything else, so it is always the first frame
            await ws.send_str(json.dumps({'t': 'hello', 'v': PROTOCOL, 'epoch': self.epoch(cid),
                                          'cfg': self.client_cfg()}))
        except (ConnectionError, RuntimeError):
            return None
        if cid != self.conn_no:
            return None
        old, self.ws, self.conn = self.ws, ws, cid
        self.conn_stt = self.stt_engine
        self.route = 'server' if self.conn_stt == 'whistle' else self.cfg['stt_route']
        self.caps, self.gate = {}, None
        self.mic_events = 0
        self._whistle_drop()
        self.gate_epoch += 1
        await self.stt_close()                   # each page gets its own Scribe stream
        self.audio_turn = None                   # the old page's audio is not playing here
        self.drop_stale_queue()
        if old is not None and not old.closed:
            log.info('new phone page replaces the old one')
            await old.close(code=4000, message=b'replaced')
        self.last_state = None
        await self.push_state()
        return cid

    def mp3_kbps(self):
        m = re.fullmatch(r'mp3_\d+_(\d+)', str(self.cfg.get('tts_output_format') or ''))
        return int(m.group(1)) if m else 32

    def prices(self):
        return {'scribe_realtime_per_hour': float(self.cfg['price_stt_per_hour']),
                'tts_flash_per_1k_chars': float(self.cfg['price_tts_per_1k_chars'])}

    def take_stt_s(self):
        """Scribe audio seconds since the last take: server route = bytes really relayed to Scribe (fix round 2,
        R8: not bytes received from the phone, some of which are dropped from the capped pending queue or fail to
        send), phone route = what the page reported as sent (stt_usage), independent of transcripts."""
        if self.route == 'server':
            sent = self.stt_seen['sent_bytes']
            s, self.stt_bytes_mark = (sent - self.stt_bytes_mark) / 32000, sent
        else:
            s = self.stt_s_acc
        self.stt_s_acc = 0.0
        return round(s, 2)

    def ride_summary(self, why, conn=None):
        """W9: totals and estimated cost since the page attached (written when it goes). AUD-V2-12: turns still in
        flight are counted now with their usage so far (and not again later, nor in another page's summary), and
        audio sent after the last turn (probe misses, the tail) is in stt_audio_s."""
        recs, self.ride_usage = self.ride_usage, []
        live = [t for t in self.unfinished if not t.summarized and (conn is None or t.conn == conn)]
        for t in live:
            t.summarized = True
            t.summary_snap = {k: t.usage.get(k, 0) for k in rl.USAGE_KEYS}
            recs.append(dict(t.summary_snap))
        left = self.take_stt_s()
        if recs or left > 0:
            tot = rl.usage_totals(recs + [{'stt_audio_s': left}], self.prices())
            tot.update({'turns': len(recs), 'in_flight': len(live), 'stt_after_last_turn_s': left})
            self.turns.write({'event': 'ride_summary', 'why': why, 'at': rl.now(), **tot})

    def detach(self, ws):
        if self.ws is ws:
            self.ride_summary('page_closed', self.conn)
            self.ws, self.conn = None, None
            self.audio_turn = None
            self.drop_stale_queue()
            task = asyncio.ensure_future(self.stt_close())
            self.bg.add(task)
            task.add_done_callback(self.bg.discard)

    def drop_stale_queue(self):
        for i in self.queue.drop_except(self.conn):
            self.log_dropped('stale_connection', i['text'], rl.now(), i['epoch'])

    def log_dropped(self, reason, text, at, conn=None):
        rec = {'event': 'dropped', 'reason': reason, 'text': text, 'at': at}
        if conn is not None:
            rec['conn'] = conn
        self.turns.write(rec)
        log.info('dropped transcript (%s): %r', reason, redact(text)[:120])

    async def send(self, obj, conn=None):
        """Send to the attached page; with `conn`, only if that page is still the attached one."""
        ws, cur = self.ws, self.conn
        if ws is None or ws.closed or (conn is not None and conn != cur):
            return False
        try:
            await ws.send_str(json.dumps({**obj, 'epoch': self.epoch(cur)}))
            return True
        except (ConnectionError, RuntimeError):
            return False

    async def send_bytes(self, data, conn=None):
        ws, cur = self.ws, self.conn
        if ws is None or ws.closed or (conn is not None and conn != cur):
            return False
        try:
            await ws.send_bytes(data)
            return True
        except (ConnectionError, RuntimeError):
            return False

    def state(self):
        busy = (self.active is not None and self.active.conn == self.conn) or self.queue.has_epoch(self.conn)
        return 'thinking' if busy else 'listening'

    async def push_state(self, s=None, conn=None, **extra):
        """Send a state frame when it changes. Explicit one-shot states (extended, ready, working, quota) are
        always sent; the base state (listening/thinking) is derived."""
        s = s or self.state()
        if s == 'thinking' and 'turn' not in extra:
            if self.active is None:
                return          # queued, not started: the turn's own 'thinking' (with its id) follows at once
            extra['turn'] = self.active.id
        key = (s, extra.get('turn'))
        if s in ('listening', 'thinking') and key == self.last_state:
            return
        if await self.send({'t': 'state', 's': s, **extra}, conn):
            self.last_state = key

    def on_client_msg(self, d, cid=None):
        """Synchronous part of the phone protocol; async work is scheduled on the loop."""
        if cid is not None and cid != self.conn:
            return
        t = d.get('t')
        if t == 'sensors':
            self.sensors, self.sensors_at = d, time.monotonic()
        elif t == 'play':
            turn = self.audio_turn
            if turn and turn.id == d.get('turn') and d.get('seq') == 0:
                turn.play_start = rl.now()
                turn.played.set()
        elif t == 'gate':          # AUD-V2-06: the server route needs the probe state for the wake handshake
            self.set_gate(str(d.get('s') or '')[:12])
        elif t == 'play_error':    # AUD-V2-04: the phone could not play this reply's audio (after one retry)
            turn = self.audio_turn
            if turn and turn.id == d.get('turn'):
                turn.rec.setdefault('errors', []).append('play: ' + re.sub(r'[^A-Za-z_]', '', str(d.get('why')))[:32])
        elif t == 'idle':
            if self.audio_turn and self.audio_turn.id == d.get('turn'):
                if d.get('heard') is False:
                    self.audio_turn.rec['delivery'] = 'play_failed'
                self.audio_turn.finished.set()
                self.audio_turn = None
        elif t == 'reset':
            self.request_reset(d, cid)
        elif t == 'hello':
            caps = d.get('caps') if isinstance(d.get('caps'), dict) else {}
            self.caps = {k: rl.known(v, ('sherpa', 'transcript') if k == 'wake' else ('phone', 'server'))
                         for k, v in caps.items() if k in ('wake', 'stt')}
            if self.conn_stt == 'whistle':
                self.route = 'server'          # whatever the page says: the audio comes here (round 3)
            elif d.get('v') == PROTOCOL and self.caps.get('stt') in ('phone', 'server'):
                self.route = self.caps['stt']
            elif d.get('v') != PROTOCOL:
                self.route = 'server'          # a v1 page streams PCM all the time
            log.info('phone hello v=%s caps=%s route=%s ua=%s', d.get('v'), self.caps, self.route,
                     rl.redact(d.get('ua'))[:80])
            # WP3: which browser and wake engine was this ride on? (test 3 could not say.) ua is cut at 200 chars.
            # Fix D: wake_ms = how long the sherpa spotter took to load (int ms 0..600000, else None)
            wms = caps.get('wake_ms')
            wake_ms = (int(round(wms)) if isinstance(wms, (int, float)) and not isinstance(wms, bool)
                       and 0 <= wms <= 600000 else None)
            self.turns.write({'event': 'hello', 'at': rl.now(), 'v': d.get('v'), 'wake': self.caps.get('wake'),
                              'stt': self.caps.get('stt'), 'route': self.route, 'ua': rl.redact(d.get('ua'))[:200],
                              'wake_ms': wake_ms, 'stt_engine': self.conn_stt})
            if self.conn_stt == 'whistle':
                self._spawn(self._whistle_prestart())
        else:
            self._spawn(self.on_client_async(d, cid))

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self.bg.add(task)
        task.add_done_callback(self.bg.discard)
        return task

    async def on_client_async(self, d, cid):
        t = d.get('t')
        if t == 'need_token':
            await self.mint_token(cid)
        elif t == 'partial':
            await self.on_partial(str(d.get('text') or ''), cid)
        elif t == 'committed':
            await self.on_final(str(d.get('text') or ''), conn=cid, lang=d.get('lang'), stt_s=d.get('stt_s'))
        elif t == 'stt_error':
            await self.on_stt_error(rl.known(d.get('code'), rl.STT_ERROR_CODES), cid)
        elif t == 'stt_usage':
            v = d.get('s')
            if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 3600:
                self.stt_usage_seen = True
                self.stt_s_acc += v
        elif t == 'stt_ok':
            self.stt_backoff.ok()
        elif t == 'vad':
            await self.on_vad(d.get('s') == 1, cid)
        elif t == 'telemetry':
            ride = str(d.get('ride') or '')
            n, top = self.ridelog.append(ride, d.get('batch'))
            if top is not None:
                self.ride_id = ride
                self._note_ride(ride)
                await self.send({'t': 'telemetry_ack', 'ride': ride, 'upto': top, 'n': n}, cid)
        elif t == 'summary':
            ride = str(d.get('ride') or '')
            if self.ridelog.append_summary(ride, d.get('s')):
                self.ride_id = ride
                self._note_ride(ride)
        elif t == 'wake':          # the phone's spotter fired (its cue already played)
            self.turns.write({'event': 'wake', 'at': rl.now(), 'engine': self.caps.get('wake'),
                              'word': rl.known(d.get('w'), (self.wake['name'],)), 'score': _number(d.get('score'))})
        elif t == 'no_wake':       # transcript engine: speech without the wake word, dropped on the phone (no text)
            self.turns.write({'event': 'dropped', 'reason': 'no_wake', 'words': _number(d.get('words')),
                              'at': rl.now()})
        elif t == 'mic':           # round 3 (B1): evidence of one mic revive attempt on the page; fixed labels only
            if self.mic_events >= MIC_EVENTS_MAX:          # a broken page must not flood the log
                return
            self.mic_events += 1
            self.turns.write({'event': 'mic', 'at': rl.now(), 'ev': rl.known(d.get('ev'), MIC_EV, None),
                              'why': rl.known(d.get('why'), MIC_WHY, None), 'ok': d.get('ok') is True,
                              'ac': rl.known(d.get('ac'), MIC_AC, None),
                              'track': rl.known(d.get('track'), MIC_TRACK, None),
                              'gap_s': _number(d.get('gap_s')), 'n': _number(d.get('n'))})

    # ------------------------------------------------------------ STT: tokens for the phone (stt_route=phone)
    def stt_params(self):
        """Scribe realtime query parameters shared by both routes (list values = repeated parameters)."""
        c = self.cfg
        p = {'model_id': c['stt_model_id'], 'audio_format': 'pcm_16000', 'commit_strategy': 'vad',
             'vad_silence_threshold_secs': c['end_of_turn_silence_s'],
             'include_language_detection': 'true'}
        if c.get('filter_background_audio'):
            p['filter_background_audio'] = 'true'
        if c.get('stt_language'):
            p['language_code'] = c['stt_language']
        langs = [x.strip() for x in str(c.get('languages') or '').split(',') if x.strip()]
        if langs:
            p['secondary_languages'] = langs
        terms = [x.strip() for x in str(c.get('keyterms') or '').split(',') if x.strip()]
        if terms:
            p['keyterms'] = terms
        return p

    def scribe_url(self):
        q = []
        for k, v in self.stt_params().items():
            q += [(k, x) for x in v] if isinstance(v, list) else [(k, v)]
        return self.cfg['scribe_url'] + '?' + urlencode(q)

    async def mint_token(self, cid):
        """One single-use realtime_scribe token per request, at most one per token_min_interval_s per
        connection, none while backing off. The token value is never logged."""
        now = time.monotonic()
        wait = self.stt_backoff.wait_left(now)
        last = self.token_at.get(cid)
        if last is not None:
            wait = max(wait, self.cfg['token_min_interval_s'] - (now - last))
        if wait > 0:
            await self.send({'t': 'token_error', 'code': 'rate_limited', 'retry_in': round(wait, 1)}, cid)
            return False
        self.token_at = {cid: now}
        key = self.secret('ELEVENLABS_API_KEY')
        token, err = None, None
        if not key:
            err = 'no ELEVENLABS_API_KEY'
        else:
            try:
                async with self.http.post(self.cfg['tts_base_url'] + '/v1/single-use-token/realtime_scribe',
                                          headers={'xi-api-key': key}, timeout=ClientTimeout(total=10)) as r:
                    body = await r.json(content_type=None) if r.status == 200 else None
                    token = body.get('token') if isinstance(body, dict) else None
                    if not token:
                        err = f'HTTP {r.status}'
            except Exception as e:
                err = redact(repr(e))[:120]
        if not token:
            log.warning('scribe token mint failed: %s', redact(err))
            await self.on_stt_error('token_mint_failed', cid)
            return False
        self.turns.write({'event': 'token', 'at': rl.now(), 'conn': cid})
        return await self.send({'t': 'token', 'kind': 'realtime_scribe', 'token': token,
                                'url': self.cfg['scribe_url'], 'params': self.stt_params()}, cid)

    async def on_stt_error(self, code, cid=None):
        """Scribe refused (quota, auth, rate): back off; announce once per episode. Other codes are ignored
        here (the phone reconnects on its own after e.g. a session time limit)."""
        if code not in rl.STT_BACKOFF_CODES:
            log.info('stt error %s (no backoff)', code)
            return
        delay, first = self.stt_backoff.fail(time.monotonic())
        self.turns.write({'event': 'stt_error', 'code': code, 'backoff_s': delay, 'notice': first, 'at': rl.now()})
        log.warning('stt error %s: backing off %s s', code, delay)
        await self.send({'t': 'stt_backoff', 'code': code, 'retry_in': delay}, cid)
        if first:
            await self.push_state('quota', cid)

    # ------------------------------------------------------------ STT: server relay (stt_route=server)
    async def audio(self, pcm, cid=None):
        """Gated PCM from the phone. Opens a Scribe session on demand; it closes after stt_idle_close_s
        without audio, so a sleeping phone costs nothing (and never hits insufficient_audio_activity)."""
        if cid is not None and cid != self.conn:
            return
        self.stt_last_audio = time.monotonic()
        self.stt_seen['audio_bytes'] += len(pcm)
        if self.conn_stt == 'whistle':
            self._whistle_audio(pcm, cid)
            return
        sws = self.stt_ws
        if sws is not None and self.stt_ok and not sws.closed and self.stt_epoch == self.gate_epoch:
            await self._stt_send(sws, pcm)
            return
        self.stt_pending.append(pcm)
        del self.stt_pending[:-60]           # ~6 s of 100 ms frames at most
        now = time.monotonic()
        if (self.stt_task is None or self.stt_task.done()) and not self.stt_backoff.wait_left(now) \
                and now >= self.stt_start_retry_at:
            self.stt_task = asyncio.ensure_future(self._stt_session(self.ws, self.conn))

    async def _stt_send(self, sws, pcm):
        try:
            await sws.send_str(json.dumps({'message_type': 'input_audio_chunk', 'commit': False,
                                           'sample_rate': 16000,
                                           'audio_base_64': base64.b64encode(pcm).decode()}))
        except (ConnectionError, RuntimeError):
            return
        self.stt_seen['sent_bytes'] += len(pcm)

    def _stt_hold(self):
        """Fix round 2 (R2): a session that did not start holds the next attempt back 2, 4, 8, 15, 30 s (reset by a
        started session)."""
        hold = (2, 4, 8, 15, 30)[min(self.stt_start_fails, 4)]
        self.stt_start_fails += 1
        self.stt_start_retry_at = time.monotonic() + hold
        return hold

    async def _stt_session(self, client_ws, cid):
        # AUD-R3-06: the page hears {t:'stt', s:'connecting'|'ready'|'failed'|'closed', why?} for the server's Scribe
        # session, and shows 'listening' / plays 'back' only after 'ready' (session_started). One deadline,
        # stt_start_timeout_s, covers connect + start: an unreachable provider is a failure too, with the same hold.
        state = None

        async def say(s, why=None):
            nonlocal state
            state = s
            await self.send({'t': 'stt', 's': s, **({'why': why} if why else {})}, cid)

        key = self.secret('ELEVENLABS_API_KEY')
        if not key:
            log.error('ELEVENLABS_API_KEY is not available: speech-to-text is off')
            await say('failed', 'no_key')
            return
        watchdog, limit, t0 = None, self.cfg['stt_start_timeout_s'], time.monotonic()
        await say('connecting')

        async def connect():
            return await self.http.ws_connect(self.scribe_url(), headers={'xi-api-key': key},
                                              heartbeat=None, max_msg_size=4 << 20)
        try:
            try:
                sws = await asyncio.wait_for(connect(), timeout=limit)
            except asyncio.TimeoutError:
                hold = self._stt_hold()
                log.warning('scribe: not connected within %.1f s (retry in %s s)', limit, hold)
                await say('failed', 'connect_timeout')
                return
            async with sws:
                if self.ws is not client_ws:
                    return
                self.stt_ws = sws
                opened, epoch = t0, self.gate_epoch       # the start deadline counts from before the connect
                self.stt_epoch = epoch

                async def idle_close():
                    while not sws.closed:
                        await asyncio.sleep(0.25)
                        # fix round 2 (R2): no session_started in time -> close; the next audio retries after a
                        # growing hold (2, 4, 8, 15, 30 s), reset by a started session
                        if not self.stt_ok and time.monotonic() - opened > limit:
                            hold = self._stt_hold()
                            log.warning('scribe: no session_started within %.1f s of connecting, closing '
                                        '(retry in %s s)', limit, hold)
                            await say('failed', 'start_timeout')
                            await sws.close()
                            return
                        if time.monotonic() - self.stt_last_audio > self.cfg['stt_idle_close_s']:
                            log.info('scribe: no audio for %.1f s, closing', self.cfg['stt_idle_close_s'])
                            await sws.close()
                watchdog = asyncio.ensure_future(idle_close())
                async for msg in sws:
                    if msg.type != WSMsgType.TEXT:
                        continue
                    d = json.loads(msg.data)
                    mt = d.get('message_type')
                    self.stt_seen[mt] += 1
                    if mt == 'session_started':
                        self.stt_ok = True
                        self.stt_start_fails = 0
                        self.stt_backoff.ok()
                        pending, self.stt_pending = self.stt_pending, []
                        for p in pending:
                            await self._stt_send(sws, p)
                        await say('ready')
                    elif mt == 'partial_transcript':
                        await self.on_partial(d.get('text', ''), cid, epoch=epoch)
                    elif mt == 'committed_transcript':
                        await self.on_final(d.get('text', ''), conn=cid, epoch=epoch)
                    elif mt in rl.STT_BACKOFF_CODES:
                        await self.on_stt_error(mt, cid)
                        await say('failed', mt)
                    elif mt and mt not in ('committed_transcript_with_timestamps',):
                        log.warning('scribe %s: %s', mt, redact(str(d.get('error') or d.get('warning')))[:200])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning('scribe connect/stream error: %s', redact(repr(e))[:200])
            if state == 'connecting':
                self._stt_hold()
                await say('failed', 'connect_error')
        finally:
            if watchdog is not None:
                watchdog.cancel()
            self.stt_ws, self.stt_ok = None, False
            if state in ('connecting', 'ready'):      # ended without a failure (idle close, gate asleep, page gone)
                try:
                    await say('closed')
                except Exception:
                    pass

    async def stt_close(self):
        task, self.stt_task = self.stt_task, None
        self.stt_pending = []
        self._whistle_drop()
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        self.stt_ws, self.stt_ok = None, False


    # ------------------------------------------------------------ STT: Whistle on this host (stt_engine=whistle)
    def _whistle_engine(self):
        """The injected engine (tests) or one built from the settings; None when it cannot even be constructed."""
        if self.whistle is None:
            try:
                try:
                    from . import ride_whistle as rw
                except ImportError:
                    import ride_whistle as rw
                base = os.path.dirname(self.cfg['log_dir']) if self.cfg.get('log_dir') else None
                self.whistle = rw.WhistleEngine(
                    self.cfg.get('whistle_python') or 'python3', self.cfg.get('whistle_weights') or '',
                    self.cfg.get('whistle_language') or '', float(self.cfg['whistle_timeout_s']),
                    os.path.join(base, 'whistle') if base else None)
            except Exception as e:                 # never fail a page over the STT engine
                self.whistle_err = f'{type(e).__name__}: {e}'[:200]
                log.warning('whistle: cannot build the engine: %s', redact(self.whistle_err))
                return None
        return self.whistle

    async def _whistle_prestart(self):
        """Load the model at the first hello of a Whistle page, so the first phrase does not pay the load."""
        eng = self._whistle_engine()
        if eng is None:
            await self._whistle_failed(f'cannot build the engine: {self.whistle_err}', start=True)
            return False
        try:
            await eng.start()
            return True
        except Exception as e:
            await self._whistle_failed(str(e), start=True)
            return False

    def _whistle_open(self, cid):
        """The buffer for the attached page in the current gate epoch (a new epoch or page starts a new one)."""
        if self.conn_stt != 'whistle' or cid is None or cid != self.conn or self.gate == 'sleep':
            return None
        wh = self.wh
        if wh is not None and (wh['epoch'] != self.gate_epoch or wh['conn'] != cid):
            self._whistle_cut(wh, 'stale_gate', stale=True)
            wh = self.wh = None
        if wh is None:
            self.wh = wh = {'epoch': self.gate_epoch, 'conn': cid, 'pcm': bytearray(), 'speech': self.vad == 1,
                            'vad_on': self.vad_on_at if self.vad == 1 else None, 'vad_off': None,
                            'first': (cid, self.gate_epoch) not in self.wh_first}
        return wh

    def _whistle_audio(self, pcm, cid):
        """Gated PCM of the current epoch: buffered for one phrase (28 s at most), sent to the helper after the VAD
        has been off for whistle_end_s. Silence before the first word is kept to half a second of onset."""
        wh = self._whistle_open(cid)
        if wh is None:
            return
        wh['pcm'].extend(pcm)
        if not wh['first'] and not wh['speech'] and wh['vad_off'] is None and len(wh['pcm']) > 16000:
            del wh['pcm'][:-16000]                   # silence before the first word: half a second of onset at most
        cap = WHISTLE_CAP_S * 32000
        if len(wh['pcm']) > cap:
            rest = wh['pcm'][cap:]
            del wh['pcm'][cap:]
            self._whistle_cut(wh, 'cap')
            if rest:
                nxt = self._whistle_open(cid)
                if nxt is not None:
                    nxt['pcm'].extend(rest)
                    nxt['speech'] = True
        self._whistle_ready(wh['epoch'])
        if self.wh_watch is None or self.wh_watch.done():
            self.wh_watch = self._spawn(self._whistle_watch())

    def _whistle_ready(self, epoch):
        """{t:'stt', s:'ready'} once per gate opening, after the helper is up (the page shows 'listening' on it)."""
        if self.wh_ready_epoch == epoch:
            return
        self.wh_ready_epoch = epoch
        self._spawn(self._whistle_say_ready(epoch))

    async def _whistle_say_ready(self, epoch):
        eng = self._whistle_engine()
        try:
            if eng is None:
                raise RuntimeError(f'cannot build the engine: {self.whistle_err}')
            await eng.start()
        except Exception as e:
            await self._whistle_failed(str(e), start=True)
            return
        if epoch == self.gate_epoch and self.conn_stt == 'whistle':
            await self.send({'t': 'stt', 's': 'ready'}, self.conn)

    async def _whistle_watch(self):
        """Ends a phrase: whistle_end_s of silence after the VAD-off edge (or after the first buffer, when the rider
        was already talking as the gate opened and there is no VAD-on edge), and reports {t:'stt', s:'ready'} once
        per gate opening when the helper is up."""
        try:
            while True:
                await asyncio.sleep(0.05)
                wh, now = self.wh, time.monotonic()
                if wh is None or wh['epoch'] != self.gate_epoch or wh['conn'] != self.conn or self.gate == 'sleep':
                    return
                end = self.cfg['whistle_end_s']
                if now - (wh['vad_off'] if wh['vad_off'] is not None else now) >= end:
                    self._whistle_cut(wh, 'vad_end')
        finally:
            self.wh_watch = None

    def _whistle_cut(self, wh, why, stale=False):
        """Take the buffer out of play; transcribe it unless it carries no speech at all."""
        if self.wh is wh:
            self.wh = None
        if stale:
            if wh['pcm']:
                self.turns.write({'event': 'dropped', 'reason': 'stale_gate', 'audio_s': round(len(wh['pcm']) / 32000, 2),
                                  'cut': why, 'at': rl.now()})
            return
        if not wh['pcm'] or not (wh['speech'] or wh['first']):
            return                                   # no speech of its own and not the first buffer: silence
        self.wh_first.add((wh['conn'], wh['epoch']))
        min_s = float(self.cfg.get('min_phrase_s') or 0)
        audio_s = round(len(wh['pcm']) / 32000, 2)
        if min_s > 0 and audio_s < min_s:
            self.turns.write({'event': 'dropped', 'reason': 'too_short_audio', 'audio_s': audio_s, 'min_s': min_s,
                              'cut': why, 'at': rl.now()})
            return
        job = {'conn': wh['conn'], 'epoch': wh['epoch'], 'pcm': bytes(wh['pcm']), 'vad_on': wh['vad_on'],
               'vad_off': wh['vad_off'], 'cut': why}
        self.wh_jobs.append(job)
        self._spawn(self._whistle_phrase(job))

    async def _whistle_phrase(self, job):
        """One finished phrase: transcribe, then hand the text to on_partial (barge-in, heard) and on_final."""
        eng = self._whistle_engine()
        audio_s = round(len(job['pcm']) / 32000, 2)
        text, ms = '', None
        # keep the page's window open while the phrase is being transcribed (no partials come from Whistle; a slow
        # phrase must not let the 10 s window close and have its own result dropped as asleep)
        keep = self._spawn(self._whistle_heard(job))
        try:
            if eng is None:
                raise RuntimeError(f'cannot build the engine: {self.whistle_err}')
            text, ms = await eng.transcribe(job['pcm'])
        except Exception as e:
            self._whistle_unjob(job)
            await self._whistle_failed(str(e))
            return
        finally:
            keep.cancel()
        text = rl.whistle_text(text)
        self.turns.write({'event': 'stt', 'engine': 'whistle', 'audio_s': audio_s, 'whistle_ms': ms,
                          'words': rl.count_words(text), 'cut': job['cut'],
                          'conn': job['conn'], 'at': rl.now()})
        if not text:
            self._whistle_unjob(job)
            return
        if job['vad_on'] is not None:
            self.utt_start = job['vad_on']           # barge-in eligibility: when this phrase began
        await self.on_partial(text, job['conn'], epoch=job['epoch'])
        if job['vad_off'] is not None:
            self.vad_off_at = max(self.vad_off_at or 0.0, job['vad_off'])
        await self.on_final(text, conn=job['conn'], epoch=job['epoch'])
        self._whistle_unjob(job)                     # after on_final: the turn can't start before its text

    async def _whistle_heard(self, job):
        """{t:'heard'} once a second while a phrase is transcribed and its epoch is still open."""
        while True:
            await asyncio.sleep(1.0)
            if job['conn'] != self.conn or job['epoch'] != self.gate_epoch or self.gate == 'sleep':
                return
            self.heard_sent = time.monotonic()
            await self.send({'t': 'heard'}, job['conn'])

    def _whistle_unjob(self, job):
        self.wh_jobs = [j for j in self.wh_jobs if j is not job]

    async def _whistle_failed(self, why, start=False):
        """A Whistle failure: logged (code whistle_failed), told to the page, and — with stt_fallback scribe and an
        ElevenLabs credential — this connection's server route moves to the existing Scribe relay for the rest of
        the page load (logged once). With stt_fallback none the page keeps hearing `failed`."""
        why = redact(str(why))[:160]
        code = 'whistle_start' if start else 'whistle'
        self.turns.write({'event': 'stt_error', 'code': 'whistle_failed', 'why': code, 'start': bool(start),
                          'at': rl.now()})
        log.warning('whistle failed (%s): %s', code, why)
        if start:
            await self.send({'t': 'stt', 's': 'failed', 'why': 'whistle_start'}, self.conn)
        else:
            await self.send({'t': 'stt', 's': 'failed', 'why': 'whistle'}, self.conn)
        if self.conn_stt != 'whistle' or self.cfg.get('stt_fallback') != 'scribe':
            return
        key = self.secret('ELEVENLABS_API_KEY')
        if not key:
            log.warning('whistle: no Scribe fallback without ELEVENLABS_API_KEY; the page is told recognition is down')
            return
        self.conn_stt, self.route = 'scribe', 'server'
        self._whistle_drop()
        self.turns.write({'event': 'stt_fallback', 'from': 'whistle', 'to': 'scribe', 'why': code, 'at': rl.now()})
        log.info('whistle: this page falls back to the Scribe relay')

    def _whistle_drop(self):
        """Forget the buffered phrase (a new page, a new epoch, a fallback): nothing of it is transcribed."""
        wh, self.wh = self.wh, None
        if wh is not None and len(wh['pcm']) > 20:
            self.turns.write({'event': 'dropped', 'reason': 'stale_gate', 'audio_s': round(len(wh['pcm']) / 32000, 2),
                              'cut': 'replaced', 'at': rl.now()})

    async def whistle_close(self):
        if self.whistle is not None:
            try:
                await self.whistle.close()
            except Exception as e:
                log.warning('whistle close failed: %s', redact(repr(e))[:120])

    # ------------------------------------------------------------ utterances -> turns
    def rider_talking(self, since=None):
        """Is the rider talking right now? A Scribe partial in the last 700 ms (only partials after the last
        commit / `since`: the committed utterance's own partials are not new speech), or the phone's VAD is on and
        either just switched on (< vad_trust_s: Scribe's first partial lags) or Scribe confirmed it with a partial in
        the last 2 s. An energy VAD stuck on road noise (live run 2) is therefore not "talking" for long: Scribe
        filters background audio and sends no partials for it."""
        if self.conn_stt == 'whistle':
            # Round 3 (B2): no partials. A phrase being transcribed is talking (the turn must not start before its
            # text); the phone's VAD is trusted while on for at most 15 s per run (then road noise, not speech).
            now = time.monotonic()
            if any(j.get('conn') == self.conn and j.get('epoch') == self.gate_epoch for j in self.wh_jobs):
                return True
            if self.vad != 1 or self.vad_on_at is None:
                return False
            return now - max(self.vad_on_at, since or 0.0) < 15.0
        now, p = time.monotonic(), self.partial_at
        since = max(since or 0.0, self.commit_at or 0.0)
        if p is not None and p > since and now - p < 0.7:
            return True
        if self.vad != 1:
            return False
        if self.vad_on_at is not None and now - self.vad_on_at < self.cfg['vad_trust_s']:
            return True
        return p is not None and p > max(since, self.vad_on_at or 0.0) and now - p < 2.0

    async def on_vad(self, s, cid=None):
        if cid is not None and cid != self.conn:
            return
        now = time.monotonic()
        if s and not self.vad:
            self.vad_on_at = now
            if self.utt_start is None:
                self.utt_start = rl.now()
        elif not s and self.vad:
            self.vad_off_at = now
        self.vad = 1 if s else 0
        if self.conn_stt == 'whistle':
            wh = self.wh
            if wh is not None and wh['epoch'] == self.gate_epoch and wh['conn'] == self.conn:
                if s:
                    wh['speech'] = True
                    wh['vad_on'] = wh['vad_on'] or rl.now()
                else:
                    # a buffer with no VAD-on edge of its own holds speech that began before the gate opened (the
                    # wake fired mid-utterance): its off edge ends the phrase all the same
                    wh['speech'] = wh['speech'] or bool(wh['pcm'])
                    wh['vad_off'] = now

    def set_gate(self, s):
        """Fix round 2 (R4 / AUD-V2-06): entering 'sleep' starts a new gate epoch. On the server route the Scribe
        session of the old epoch is closed and anything it still delivers is dropped (on_final/on_partial compare
        the session's epoch), so a late final after a wake miss, a probe timeout or the window's end can never
        become a turn without the wake check."""
        old, self.gate = self.gate, s
        if s == 'sleep' and old != 'sleep':
            self.gate_epoch += 1
            self.stt_pending = []
            self.vad, self.vad_on_at, self.vad_off_at = 0, None, None      # the phone's next speech starts a new run
            if self.wh is not None:
                self._whistle_cut(self.wh, 'gate_sleep', stale=True)
            sws = self.stt_ws
            if self.route == 'server' and sws is not None and not sws.closed:
                asyncio.ensure_future(sws.close())   # not awaited: this may run inside that session's reader

    def stale_stt(self, epoch):
        """A server-route Scribe event from a session of an earlier gate epoch, or any while asleep."""
        return epoch is not None and (epoch != self.gate_epoch or self.gate == 'sleep')

    async def on_partial(self, text, cid=None, epoch=None):
        if cid is not None and cid != self.conn:
            return
        if self.gate in ('probe', 'sleep') or self.stale_stt(epoch):
            return                 # not (yet) woken: bystander speech is neither "talking" nor a barge-in
        if text and text != self.partial:
            if not self.partial and self.utt_start is None:
                self.utt_start = rl.now()
            self.partial, self.partial_changed_at = text, rl.now()
            self.partial_at = time.monotonic()
            if self.partial_at - self.heard_sent >= 0.5:      # R3-5: the page's window must not close mid-speech
                self.heard_sent = self.partial_at
                await self.send({'t': 'heard'}, self.conn)
        turn = self.audio_turn
        # AUD-V2-07: eligibility starts when the phone reports the answer really playing ({t:'play'} seq 0, server
        # receipt time, the same clock as utt_start), not when the audio was sent (the ready/done cue plays first)
        started = (turn is not None and turn.play_start is not None and self.utt_start is not None
                   and self.utt_start >= turn.play_start - 0.2)
        if turn and rl.should_barge_in(text, True, self.cfg['barge_in_min_words'], started):
            self.audio_turn = None
            turn.cancelled = True
            turn.barged = True
            self.interrupted = True
            await self.send({'t': 'stop', 'turn': turn.id}, turn.conn)
            turn.finished.set()
            self.turns.write({'event': 'barge_in', 'turn': turn.id, 'at': rl.now(), 'partial': text})
            log.info('barge-in on turn %s', turn.id)

    async def on_final(self, text, t_final=None, conn=None, lang=None, stt_s=None, epoch=None):
        """A committed (final) transcript. Same entry point for Scribe (server route, with the session's gate
        epoch), the phone (phone route) and injected tests. Clean utterances wait for the adaptive end of turn
        before they become a turn."""
        t_final = t_final or rl.now()
        conn = self.conn if conn is None else conn
        if self.stale_stt(epoch):            # R4: opened before the gate last slept, or the gate sleeps now
            self.partial, self.partial_changed_at, self.utt_start = '', None, None
            self.turns.write({'event': 'dropped', 'reason': 'asleep' if epoch == self.gate_epoch else 'stale_gate',
                              'words': rl.count_words(text), 'at': t_final})
            return
        t_end = self.partial_changed_at or (t_final - self.cfg['end_of_turn_silence_s'])
        self.partial, self.partial_changed_at = '', None
        self.utt_start = None
        self.commit_at = time.monotonic()
        if not self.stt_usage_seen and isinstance(stt_s, (int, float)) and not isinstance(stt_s, bool) \
                and 0 <= stt_s < 3600:
            self.stt_s_acc += stt_s
        if conn is None or conn != self.conn:
            self.log_dropped('stale_connection', text, t_final, conn)
            return
        if self.resetting is not None:       # WP6: the conversation is being reset; nothing joins the old one
            self.log_dropped('resetting', text, t_final, conn)
            return
        if self.gate == 'probe' and self.route == 'server':   # AUD-V2-06: sleeping phone, transcript engine: the wake word must open it
            text, woke = rl.strip_wake(text, self.wake['alts'])
            self.set_gate('awake' if woke else 'sleep')
            if not woke:
                self.turns.write({'event': 'dropped', 'reason': 'no_wake', 'words': rl.count_words(text),
                                  'at': t_final})
                await self.send({'t': 'wake_miss'}, conn)
                return
            self.turns.write({'event': 'wake', 'at': t_final, 'engine': 'transcript-server', 'word': self.wake['name']})
            await self.send({'t': 'wake_hit'}, conn)
        answering = self.passthrough and self.active is not None
        text, woke = rl.strip_wake(text, self.wake['alts'])
        if woke and not text:
            return                                   # just the wake word: the phone already answered with a cue
        clean, reason = rl.filter_transcript(text, 1 if answering else self.cfg['min_words'])
        if clean is None:
            self.log_dropped(reason, text, t_final)
            await self.send({'t': 'dropped', 'reason': reason, 'text': text}, conn)
            return
        await self.send({'t': 'final', 'text': clean, 'lang': lang}, conn)
        if answering:   # the open clarify question takes this utterance as its answer, inside the running turn
            self.passthrough = False
            self.turns.write({'event': 'clarify_answer', 'turn': self.active.id, 'text': clean, 'at': t_final})
            await self.submit(self.active, clean, {'clarify_answer': True})
            return
        held = self.held_turn
        if held is not None and held.held is not None:      # the rider said more while his reply was held
            await self.discard_stale(held, clean, 'kept_talking')
        now = time.monotonic()
        silent_for = now - self.vad_off_at if (not self.vad and self.vad_off_at and
                                               self.vad_off_at >= (self.vad_on_at or 0)) else 0.0
        self.pending.append({'text': clean, 't_end': t_end, 't_final': t_final, 'conn': conn, 'lang': lang,
                             'mono': now, 'speech_end': now - min(silent_for, 10.0)})
        if self.eot_task is None or self.eot_task.done():
            self.eot_task = asyncio.ensure_future(self._eot_loop())

    async def _eot_loop(self):
        """Adaptive end of turn (W6.2): after the last committed segment wait eot_question_s / eot_default_s /
        eot_open_s of silence (counted from the end of speech), restarted by new speech; capped at eot_max_s."""
        c = self.cfg
        try:
            while self.pending:
                await asyncio.sleep(0.05)
                if not self.pending:
                    return
                last, now = self.pending[-1], time.monotonic()
                if last['conn'] != self.conn:
                    for p in self.pending:
                        self.log_dropped('stale_connection', p['text'], p['t_final'], p['conn'])
                    self.pending = []
                    return
                if now - last['mono'] < c['eot_max_s']:
                    if self.rider_talking(since=last['mono']):
                        continue
                    quiet_since = last['speech_end']
                    if self.vad_off_at and self.vad_off_at > last['mono']:
                        quiet_since = max(quiet_since, self.vad_off_at)
                    if self.partial_at and self.partial_at > last['mono']:
                        quiet_since = max(quiet_since, self.partial_at)
                    wait = rl.eot_wait(last['text'], c['eot_question_s'], c['eot_default_s'], c['eot_open_s'])
                    if now - quiet_since < wait:
                        continue
                items, self.pending = self.pending, []
                for p in items:
                    self.queue.add(p['text'], p['t_end'], p['t_final'], p['conn'])
                if len(items) > 1:
                    self.turns.write({'event': 'eot_joined', 'parts': len(items), 'at': rl.now()})
                log.info('end of turn: %d segment(s) queued (queued=%d)', len(items), len(self.queue))
                if self.worker is None or self.worker.done():
                    self.worker = asyncio.create_task(self._work())
                await self.push_state()
        finally:
            self.eot_task = None

    async def _work(self):
        """The one turn worker: strictly one agent turn at a time, across reconnects too."""
        try:
            while True:
                self.drop_stale_queue()
                q = self.queue.take()
                if q is None:
                    break
                await self.run_turn(q)
        except Exception:
            log.exception('turn worker crashed')
        finally:
            self.worker = None
            self.active = None
            await self.push_state()

    def deliverable(self, turn):
        if not turn.stale and turn.conn is not None and turn.conn == self.conn \
                and self.ws is not None and not self.ws.closed:
            return True
        if not turn.stale:
            turn.stale = True
            turn.rec['delivery'] = 'dropped_stale_connection'
            turn.finished.set()
            log.info('delivery dropped: turn %s belongs to connection %s (attached: %s)',
                     turn.id, turn.conn, self.conn)
        return False

    async def deliver(self, turn, obj):
        if self.deliverable(turn) and await self.send(obj, turn.conn):
            return True
        self.deliverable(turn)
        return False

    def new_turn(self, conn, kind='turn'):
        self.turn_no += 1
        turn = Turn(self.turn_no, conn, kind)
        turn.speaker = asyncio.create_task(self._speak(turn))
        return turn

    def since_block(self):
        """(since-last-turn text, ride log path) for the context line (W7). The block covers the time since the
        previous turn (the first turn: since the ride's first record)."""
        ride = self.ride_id
        if not ride:
            return None, None
        now_ms = int(rl.now() * 1000)
        # AUD-V2-11: the watermark is what was delivered (position in the append-only ride log), not a clock: a
        # batch that arrives late (records from before the previous turn) is in the next block, once.
        rows = [r for r in self.ridelog.read(ride) if 'summary' not in r]
        new = rows[self.since_mark.get(ride, 0):]
        self.since_mark[ride] = len(rows)
        t_prev = self.last_turn_ms.get(ride)
        self.last_turn_ms[ride] = now_ms
        if not new:
            return None, self.ridelog.path(ride)
        t_from = min([r['t'] for r in new] + ([t_prev] if t_prev is not None else [])) - 1
        try:
            text = rt.since_text(new, t_from, max([now_ms] + [r['t'] for r in new]))
        except Exception as e:                     # never let telemetry break a turn
            log.warning('since-last-turn block failed: %s', redact(repr(e))[:120])
            text = None
        return text, self.ridelog.path(ride)

    async def run_turn(self, q):
        turn = self.new_turn(q.get('epoch'))
        stale_prefix, self.stale_prefix = self.stale_prefix, []
        stale, self.stale_note = self.stale_note, False
        rider_text = ' '.join(stale_prefix + [q['text']])
        turn.rider_text = rider_text
        self.active = turn
        self.passthrough = False
        await self.push_state()
        ages = rl.sensor_ages(self.sensors, self.sensors_at, time.monotonic())
        since, ride_log = self.since_block()
        text = rl.build_turn(rider_text, self.sensors, ages, self.cfg['sensor_max_age_s'], self.interrupted,
                             self.cfg['rider_name'], stale=stale, since=since, ride_log=ride_log)
        self.interrupted = False
        turn.usage['stt_audio_s'] = self.take_stt_s()
        self.unfinished.add(turn)
        rec = turn.rec
        rec.update({'event': 'turn', 'turn': turn.id, 'conn': turn.conn, 'delivery': 'ok', 'engine':
                    self.cfg['tts_engine'], 'route': self.route, 'stt_engine': self.conn_stt,
                    'parts': q['parts'] + len(stale_prefix),
                    'text': rider_text, 'stale_combined': bool(stale_prefix),
                    'sensors': text.split('\n')[0][-160:], 'end_of_speech': q['t_end_speech'],
                    'final': q['t_final']})
        rec['submit'] = rl.now()
        self.session_turns += 1                    # P3: this conversation had a turn (Ride Summary prompt at reset)
        watch = asyncio.ensure_future(self._watch_turn(turn))
        try:
            await self.submit(turn, text, {'rider_text': rider_text, 'sensors': self.sensors, 'turn': turn.id})
        except Exception as e:
            log.warning('submit failed: %s', redact(repr(e))[:200])
            rec.setdefault('errors', []).append('submit: ' + redact(repr(e))[:120])
            turn.ok = False
            turn.done.set()
        try:
            await asyncio.wait_for(turn.done.wait(), self.cfg['turn_timeout_s'])
        except asyncio.TimeoutError:
            rec.setdefault('errors', []).append('turn timeout')
            turn.ok = False
        watch.cancel()
        rec['agent_done'] = rl.now()
        await self.close_turn(turn)
        self.active = None
        return rec

    async def _watch_turn(self, turn):
        """W8/W11: 'extended' once thinking passes extended_after_s with no tool state; 'working' (clip) once
        when a turn runs past working_after_s with no audio."""
        t0 = time.monotonic()
        while not turn.done.is_set() and not turn.cancelled and not turn.segs:
            await asyncio.sleep(0.1)
            el = time.monotonic() - t0
            if el >= self.cfg['extended_after_s'] and not turn.states:
                await self.tool_state(turn, 'long')
            if el >= self.cfg['working_after_s'] and not turn.working_sent and turn.held is None:
                turn.working_sent = True
                turn.rec['working'] = round(el, 1)
                await self.push_state('working', turn.conn, turn=turn.id)

    async def tool_state(self, turn, kind):
        """'extended' state for a turn (tool call, web search, subagent, long thinking): once per kind."""
        if turn is None or turn.kind != 'turn' or kind in turn.states or turn.segs or turn.cancelled:
            return
        turn.states.add(kind)
        turn.rec.setdefault('states', []).append([kind, round(rl.now() - (turn.rec.get('submit') or rl.now()), 2)])
        await self.push_state('extended', turn.conn, turn=turn.id, why=kind)

    _ACTIVE = object()

    def bind_hook_turn(self, turn_id, turn=_ACTIVE):
        """pre_llm_call: later hook events must carry this turn_id (AUD-V2-13). Fix round 2 (R9): the adapter
        passes the Turn that was running when the hook fired; the binding happens only if that very turn is still
        the running one and unbound, and a Hermes turn_id binds at most one ride turn (a delayed pre_llm_call of an
        earlier turn can't claim the next one). In-loop callers may omit `turn` (= the running turn now)."""
        if turn is RideCore._ACTIVE:
            turn = self.active
        if not turn_id or turn is None or turn is not self.active or turn.hook_turn is not None \
                or turn_id in self.hook_ids:
            log.info('ride hook: turn binding refused')
            return
        turn.hook_turn = turn_id
        self.hook_ids.append(turn_id)

    def _hook_turn(self, turn_id):
        """The running turn a hook event belongs to. Fail closed (R9): only a bound turn and an event with its
        turn_id; an event without an id, or any event before the turn is bound, belongs to no turn."""
        turn = self.active
        if turn is None or not turn_id or turn.hook_turn is None or turn_id != turn.hook_turn:
            return None
        return turn

    def on_tool(self, tool_name, turn_id=None):
        """From the adapter's pre_tool_call hook (any thread is fine via call_soon_threadsafe)."""
        name = str(tool_name or '')
        kind = 'subagent' if name == 'delegate_task' else 'search' if (
            name in ('web_search', 'web_extract') or name.startswith('browser')) else 'tool'
        turn = self._hook_turn(turn_id)
        if turn is not None:
            turn.rec.setdefault('tools', []).append(name[:40])
            self._spawn(self.tool_state(turn, kind))

    def on_tool_done(self, tool_name, turn_id=None):
        turn = self._hook_turn(turn_id)
        if turn is not None:
            turn.rec['tools_done'] = turn.rec.get('tools_done', 0) + 1

    def on_llm_usage(self, usage, turn_id=None):
        turn = self._hook_turn(turn_id)
        if turn is None or not isinstance(usage, dict):
            return
        u = turn.usage
        for src, dst in (('input_tokens', 'llm_input_tokens'), ('output_tokens', 'llm_output_tokens'),
                         ('cache_read_tokens', 'llm_cache_read_tokens')):
            v = usage.get(src)
            if isinstance(v, int) and not isinstance(v, bool):
                u[dst] = u.get(dst, 0) + v
        u['llm_requests'] = u.get('llm_requests', 0) + 1

    async def discard_stale(self, turn, new_text, why):
        """W6.1: the reply is stale (the rider kept talking): never play it, answer everything in one turn."""
        if turn.cancelled:
            return
        turn.cancelled = True
        turn.held = None
        if self.held_turn is turn:
            self.held_turn = None
        if self.audio_turn is turn:
            self.audio_turn = None
        turn.rec['delivery'] = 'discarded_stale'
        self.stale_prefix.append(turn.rider_text)
        self.stale_note = True
        self.turns.write({'event': 'discarded_stale', 'turn': turn.id, 'why': why, 'rider_text': turn.rider_text,
                          'new_text': new_text, 'reply': ' '.join(turn.texts), 'at': rl.now()})
        log.info('turn %s reply discarded as stale (%s)', turn.id, why)
        await self.deliver(turn, {'t': 'discard', 'turn': turn.id})
        turn.finished.set()

    async def _hold_watch(self, turn):
        """Play a held reply once the rider has been quiet for hold_resume_s with no new committed utterance."""
        quiet_from, t0 = None, time.monotonic()
        while turn.held is not None and not turn.cancelled and self.deliverable(turn):
            await asyncio.sleep(0.05)
            if time.monotonic() - t0 >= self.cfg['hold_max_s']:
                turn.rec['hold_timeout'] = True       # safety net: a held reply is played, never lost
                break
            if self.rider_talking() or self.pending:
                quiet_from = None
                continue
            quiet_from = quiet_from or time.monotonic()
            if time.monotonic() - quiet_from >= self.cfg['hold_resume_s']:
                break
        if turn.held is None or turn.cancelled or turn.stale:
            return
        turn.rec['hold_released'] = rl.now()
        if self.held_turn is turn:
            self.held_turn = None
        while turn.held:
            data, mime = turn.held.pop(0)
            await self._send_audio(turn, data, mime)
        turn.held = None

    async def close_turn(self, turn):
        """End of a turn: wait for open audio streams, add the safety-net speech, end the audio, log."""
        for _ in range(100):           # Hermes streaming TTS may still be flushing its last clause
            if not turn.streams:
                break
            await asyncio.sleep(0.1)
        if not turn.segs and not turn.cancelled and not turn.stale and turn.held is None:
            if turn.texts and self.cfg['tts_engine'] == 'hermes':
                turn.rec['fallback'] = 'direct_tts'     # Hermes produced no audio: speak the reply ourselves
                for t in turn.texts:
                    turn.items.put_nowait(('say', t))
            elif not turn.texts and not turn.suppressed and turn.kind == 'turn':
                turn.rec['fallback'] = 'no_reply'
                turn.items.put_nowait(('say', FALLBACK_REPLY))
        turn.items.put_nowait(None)
        await turn.speaker
        while turn.held is not None and not turn.cancelled and not turn.stale:   # a held reply: wait for its fate
            await asyncio.sleep(0.05)
        turn.rec['reply'] = ' '.join(turn.texts)
        await self.deliver(turn, {'t': 'reply', 'turn': turn.id, 'text': turn.rec['reply'], 'ok': turn.ok})
        await self.deliver(turn, {'t': 'end', 'turn': turn.id, 'segs': turn.segs})
        if not turn.segs:
            turn.finished.set()
        task = asyncio.create_task(self._finish(turn))
        self.bg.add(task)
        task.add_done_callback(self.bg.discard)

    async def _finish(self, turn):
        """Log the turn once playback is over on the phone (idle / stop / discard / page gone), so barged_in is
        the real outcome (W6.5)."""
        rec = turn.rec
        try:
            await asyncio.wait_for(turn.finished.wait(), self.cfg['finish_timeout_s'])
        except asyncio.TimeoutError:
            rec['finish'] = 'timeout'
        rec['play_start'] = turn.play_start
        rec['barged_in'] = turn.barged
        rec['finished'] = rl.now()
        rec.update({k: (round(v, 2) if isinstance(v, float) else v) for k, v in turn.usage.items()})
        e = rec.get('end_of_speech')
        rec['ms'] = {
            'endpoint': ms(e, rec.get('final')),
            'queue_wait': ms(rec.get('final'), rec.get('submit')),
            'agent_first_output': ms(rec.get('submit'), rec.get('first_output')),
            'agent_total': ms(rec.get('submit'), rec.get('agent_done')),
            'tts_first_byte': ms(rec.get('tts_req'), rec.get('tts_first_byte')),
            'audio_sent_after_speech_end': ms(e, rec.get('audio_sent')),
            'speech_end_to_play': ms(e, turn.play_start),
        }
        self.unfinished.discard(turn)
        if turn.kind == 'turn' and not turn.summarized:
            self.ride_usage.append({k: turn.usage.get(k, 0) for k in rl.USAGE_KEYS})
        self.turns.write(rec)
        if turn.kind == 'turn' and turn.summary_snap is not None:
            self.reconcile_summary(turn)

    def reconcile_summary(self, turn):
        """Fix round 2 (R8): a turn that was still running at a ride summary was counted with its usage so far;
        what it used after that (late LLM tokens, TTS) is written now as an explicit reconciliation record, so
        summary + reconciliations = the ride's final totals."""
        late = {k: turn.usage.get(k, 0) - turn.summary_snap.get(k, 0) for k in rl.USAGE_KEYS}
        if not any(late.values()):
            return
        tot = rl.usage_totals([late], self.prices())
        tot.pop('turns', None)
        self.turns.write({'event': 'ride_summary_reconciled', 'turn': turn.id, 'at': rl.now(), **tot})

    # ------------------------------------------------------------ replies from the gateway
    def _target(self):
        """The turn a reply belongs to: the running turn, else a new notice turn for the attached page."""
        if self.active is not None:
            return self.active, False
        if self.conn is None:
            return None, False
        if time.monotonic() < self.quiet_until:    # WP6: a late line of the conversation that was just reset
            self.turns.write({'event': 'dropped', 'reason': 'after_reset', 'at': rl.now()})
            return None, False
        return self.new_turn(self.conn, 'notice'), True

    async def reply_text(self, text, speak=True):
        """Text the gateway sends to the ride chat (final reply, interim line, notice). Spoken in order unless
        the Hermes engine already speaks the agent's output (speak=False: kept for the safety net). A reply that
        is only a filler ("Mm-hm.", "Go on.") is not spoken (W6.3)."""
        text = (text or '').strip()
        if not text:
            return False
        turn, new_notice = self._target()
        if turn is None:
            log.info('reply for the ride chat while no phone is attached: not spoken')
            return False
        turn.rec.setdefault('first_output', rl.now())
        if turn.kind == 'turn' and rl.is_filler(text):
            turn.suppressed = True
            turn.rec.setdefault('suppressed', []).append(text)
            self.turns.write({'event': 'filler_suppressed', 'turn': turn.id, 'text': text, 'at': rl.now()})
            return True
        turn.texts.append(text)
        if speak or new_notice:
            turn.items.put_nowait(('say', text))
        if new_notice:
            self._close_notice(turn)
        return True

    def _close_notice(self, turn):
        turn.rec.update({'event': 'notice', 'turn': turn.id, 'conn': turn.conn, 'delivery': 'ok'})
        task = asyncio.create_task(self.close_turn(turn))   # don't hold the gateway's send() while speaking
        self.bg.add(task)
        task.add_done_callback(self.bg.discard)

    async def audio_segment(self, data, mime):
        """A finished audio file from Hermes (whole-file TTS fallback, voice message)."""
        turn, notice = self._target()
        if turn is None:
            return False
        turn.rec.setdefault('first_output', rl.now())
        turn.items.put_nowait(('seg', data, mime))
        if notice:
            self._close_notice(turn)
        return True

    def internal_notice(self, kind, text):
        """R3-1: a Hermes status line or background-review summary for the ride chat: never spoken, never part of a
        turn's reply; logged by kind and word count only (no text: it may quote memory or skill content)."""
        self.turns.write({'event': 'internal_notice', 'kind': kind if kind in ('status', 'review') else 'other',
                          'turn': self.active.id if self.active is not None else None,
                          'words': rl.count_words(text or ''), 'at': rl.now()})

    def speak_notice(self, text):
        """A short spoken notice inside the running turn (approval / clarify), or as its own notice turn."""
        return self.reply_text(text, speak=True)

    # Hermes streaming TTS (PCM) ------------------------------------------------
    def pcm_begin(self, sample_rate, channels, sample_width):
        turn = self.active
        if turn is None or turn.cancelled or not self.deliverable(turn):
            return None
        turn.streams += 1
        turn.rec.setdefault('first_output', rl.now())
        turn.rec.setdefault('tts_req', rl.now())
        stream = {'turn': turn, 'seg': rl.PcmSegmenter(sample_rate, channels, sample_width), 'closed': False}
        stream['poll'] = asyncio.create_task(self._pcm_poll(stream))
        return stream

    def _pcm_out(self, stream, segments):
        turn = stream['turn']
        for s in segments:
            if not turn.cancelled:
                turn.items.put_nowait(('seg', s, 'audio/wav'))

    def pcm_write(self, stream, chunk):
        if stream is None or stream['closed']:
            return
        stream['turn'].rec.setdefault('tts_first_byte', rl.now())
        self._pcm_out(stream, stream['seg'].add(chunk, time.monotonic()))

    async def _pcm_poll(self, stream):
        while not stream['closed']:
            await asyncio.sleep(0.05)
            self._pcm_out(stream, stream['seg'].poll(time.monotonic()))

    def pcm_end(self, stream, flush=True):
        if stream is None or stream['closed']:
            return
        stream['closed'] = True
        stream['poll'].cancel()
        if flush:
            self._pcm_out(stream, stream['seg'].flush())
        stream['turn'].streams = max(0, stream['turn'].streams - 1)

    # ------------------------------------------------------------ speech pipeline (one per turn, in order)
    async def _speak(self, turn):
        prev = ''
        while True:
            item = await turn.items.get()
            if item is None:
                return
            if turn.cancelled or not self.deliverable(turn):
                continue               # barge-in or page gone: no money spent on audio nobody hears
            if item[0] == 'seg':
                await self._deliver_audio(turn, item[1], item[2])
                continue
            for s in rl.sentences(item[1]):
                say = rl.strip_markdown(s)
                if turn.cancelled or not say or not self.deliverable(turn):
                    continue
                t_req = rl.now()
                try:
                    audio, t_first = await self.tts(say, prev)
                except Exception as e:
                    log.warning('tts failed: %s', redact(str(e))[:200])
                    turn.rec.setdefault('errors', []).append('tts: ' + redact(str(e))[:120])
                    continue
                turn.usage['tts_chars'] = turn.usage.get('tts_chars', 0) + len(say)
                if 'tts_first_byte' not in turn.rec:
                    turn.rec['tts_req'], turn.rec['tts_first_byte'] = t_req, t_first
                prev = (prev + ' ' + say)[-300:]
                if not turn.cancelled and audio:
                    await self._deliver_audio(turn, audio, 'audio/mpeg')

    async def _deliver_audio(self, turn, data, mime):
        """W6.1 at the moment a reply becomes ready: discard it if the rider already said more, hold it while he
        is talking, else send it."""
        if turn.held is not None:
            turn.held.append((data, mime))
            return
        if turn.segs == 0 and turn.kind == 'turn' and not turn.hold_checked:
            turn.hold_checked = True
            if self.pending or self.queue.has_epoch(turn.conn):
                more = ' '.join([p['text'] for p in self.pending] + [i['text'] for i in self.queue.items])
                await self.discard_stale(turn, more, 'said_more_while_thinking')
                return
            if self.rider_talking():
                turn.held = [(data, mime)]
                self.held_turn = turn
                turn.rec['held'] = rl.now()
                await self.deliver(turn, {'t': 'hold', 'turn': turn.id})
                self._spawn(self._hold_watch(turn))
                return
        await self._send_audio(turn, data, mime)

    async def _send_audio(self, turn, data, mime):
        if turn.segs == 0 and self.deliverable(turn):
            await self.send({'t': 'state', 's': 'ready', 'turn': turn.id}, turn.conn)   # phone: done cue first
        if self.deliverable(turn) and await self.send_bytes(rl.encode_audio_frame(mime, turn.id, turn.segs, data),
                                                            turn.conn):
            if turn.segs == 0:
                turn.rec['audio_sent'] = rl.now()
                turn.audio_started = rl.now()
                self.audio_turn = turn
            turn.segs += 1
            secs = rl.wav_seconds(data) if mime == 'audio/wav' else rl.mp3_seconds(len(data), self.mp3_kbps()) \
                if mime == 'audio/mpeg' else 0.0
            turn.usage['tts_audio_s'] = turn.usage.get('tts_audio_s', 0.0) + secs

    async def tts_elevenlabs(self, text, prev_text=''):
        key = self.secret('ELEVENLABS_API_KEY')
        if not key:
            raise RuntimeError('ELEVENLABS_API_KEY is not available')
        c = self.cfg
        url = f"{c['tts_base_url']}/v1/text-to-speech/{c['voice_id']}/stream?output_format={c['tts_output_format']}"
        body = {'text': text, 'model_id': c['tts_model_id']}
        if prev_text:
            body['previous_text'] = prev_text
        chunks, first = [], None
        async with self.http.post(url, json=body, headers={'xi-api-key': key},
                                  timeout=ClientTimeout(total=30)) as r:
            if r.status != 200:
                raise RuntimeError(f'TTS HTTP {r.status}: {redact(await r.text())[:150]}')   # R6: redact, then cut
            async for chunk in r.content.iter_any():
                if first is None:
                    first = rl.now()
                chunks.append(chunk)
        return b''.join(chunks), first

    # ------------------------------------------------------------ WP6 (D5, P6): the phone's reset
    def request_reset(self, d, cid):
        """{t:'reset', epoch}. Runs only for the attached page, with this connection's epoch, when no reset is in
        flight and this connection has not reset before (a replay can't reset twice). Refusals are logged, not answered.
        Accepted: intake freezes at once (queued and pending utterances dropped, finals dropped while it runs)."""
        why = None
        if cid is None or cid != self.conn:
            why = 'not_attached'
        elif d.get('epoch') != self.epoch(cid):
            why = 'stale_epoch'
        elif self.resetting is not None:
            why = 'in_flight'
        elif cid in self.reset_conns:
            why = 'already_reset'
        if why:
            self.turns.write({'event': 'reset_refused', 'why': why, 'at': rl.now()})
            log.info('reset refused (%s)', why)
            return None
        self.resetting = cid
        self.reset_conns.add(cid)
        self._drop_for_reset()
        return self._spawn(self.run_reset(cid))

    def _drop_for_reset(self):
        for p in self.pending:
            self.log_dropped('reset', p['text'], p['t_final'], p['conn'])
        self.pending = []
        if self.eot_task is not None and not self.eot_task.done():
            self.eot_task.cancel()
        for i in self.queue.items:
            self.log_dropped('reset', i['text'], i['t_final'], i['epoch'])
        self.queue.items = []

    async def run_reset(self, cid):
        """Order: the gateway part (adapter: interrupt the lane's turn, then the gateway's own /new), then this core's
        state, then {t:'reset_done'}. Any failure -> {t:'reset_failed', step}; the core state is reset either way so the
        lane is usable, and a session the gateway did not reset stays as it was (nothing is deleted here)."""
        res = {'ok': False, 'step': 'unsupported'}
        # D3: the gateway step cancels the running turn, which ends it (turn_done ok=False) before reset_state runs.
        # Mark the turns cancelled first, so close_turn does not speak the no-reply fallback into the reset.
        for turn in {t for t in (self.active, self.audio_turn, self.held_turn) if t is not None}:
            turn.cancelled = turn.stale = True
            turn.rec['delivery'] = 'reset'
        try:
            if self.reset_gateway is not None:
                task = asyncio.ensure_future(self.reset_gateway())
                self.bg.add(task)
                task.add_done_callback(self.bg.discard)
                try:
                    res = dict(await asyncio.wait_for(asyncio.shield(task), self.cfg['reset_timeout_s']) or {})
                except asyncio.TimeoutError:
                    res = {'ok': False, 'step': 'timeout'}
                except Exception as e:
                    log.warning('reset: gateway step failed: %s', redact(repr(e))[:160])
                    res = {'ok': False, 'step': 'gateway'}
            try:
                self.reset_state()
            except Exception as e:
                log.warning('reset: core state reset failed: %s', redact(repr(e))[:160])
                if res.get('ok'):
                    res = {**res, 'ok': False, 'step': 'state'}
        finally:
            self.resetting = None
        ok = bool(res.get('ok'))
        step = None if ok else re.sub(r'[^a-z_]', '', str(res.get('step') or 'gateway'))[:24]
        self.turns.write({'event': 'reset', 'at': rl.now(), 'ok': ok, 'step': step, 'old': res.get('old'),
                          'new': res.get('new'), 'override_left': res.get('override_left')})
        log.info('reset %s%s', 'done' if ok else 'failed at ', '' if ok else step)
        await self.send({'t': 'reset_done'} if ok else {'t': 'reset_failed', 'step': step}, cid)
        await self.push_state()

    def reset_state(self):
        """Forget the conversation on this side: the running, speaking and held turns end silently (never spoken,
        delivery 'reset'), queue and pending utterances, stale-reply carry-over, clarify passthrough, partials, the gate
        (a new gate epoch: a late server-route transcript is dropped) and the last state frame."""
        for turn in {t for t in (self.active, self.audio_turn, self.held_turn) if t is not None}:
            turn.cancelled = turn.stale = True
            turn.held = None
            turn.rec['delivery'] = 'reset'
            turn.finished.set()
            turn.done.set()
        self._drop_for_reset()
        self.audio_turn = self.held_turn = None
        self.stale_prefix, self.stale_note = [], False
        self.interrupted = self.passthrough = False
        self.partial, self.partial_changed_at, self.partial_at, self.utt_start, self.commit_at = '', None, None, None, None
        self.gate = None
        self.gate_epoch += 1
        self.stt_pending = []
        self._spawn(self.stt_close())
        self.last_state = None
        self.quiet_until = time.monotonic() + self.cfg['reset_quiet_s']
        self.session_rides, self.session_turns = [], 0     # P3: the new conversation starts with none

    def _note_ride(self, ride):
        if ride and ride not in self.session_rides:
            self.session_rides.append(ride)
            del self.session_rides[:-20]               # bounded: a long day of page reloads

    # ------------------------------------------------------------ the gateway finished a turn
    def turn_done(self, turn=None, ok=True):
        turn = turn or self.active
        if turn is not None and not turn.done.is_set():
            turn.ok = turn.ok and ok
            turn.done.set()

    async def shutdown(self):
        self.ride_summary('server_stopping')     # before cancelling: running turns are counted (AUD-V2-12)
        for t in [self.worker, *self.bg]:
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except BaseException:
                    pass
        await self.stt_close()
        await self.whistle_close()
        if self.ws is not None and not self.ws.closed:
            await self.ws.close(code=1001, message=b'server stopping')


# ---------------------------------------------------------------- HTTP / auth
KEY_FORM = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">
<meta name="robots" content="noindex,nofollow"><title>Hermes · ride</title>
<style>body{margin:0;background:#000;color:#999;font:18px -apple-system,system-ui,sans-serif;display:flex;
flex-direction:column;gap:28px;min-height:100vh;align-items:center;justify-content:center}
.orb{width:min(46vw,46vh);height:min(46vw,46vh);border-radius:50%;background:radial-gradient(circle,#1d6cff 0%,#0b2f7a 38%,#04123a 58%,rgba(0,0,0,0) 71%)}
form{display:flex;flex-direction:column;gap:16px;width:80vw}
input,button{font-size:20px;padding:14px;border-radius:12px;border:1px solid #333;background:#111;color:#ccc}</style>
</head><body><div class="orb" aria-hidden="true"></div><form method="post" action="auth" autocomplete="off">
<label for="k">Ride access key for this browser</label>
<input id="k" name="k" type="password" autocomplete="current-password" required autofocus>
<button type="submit">Open</button><div>{note}</div></form></body></html>'''


class AuthLimiter:
    """AUD-R3-05: bounded key attempts on the form (the only way in). Per client: after `per_client` wrong keys the
    client is locked out for `lock_s`, doubling with every further wrong key (max `lock_max_s`); a right key clears
    it. In aggregate: `aggregate` wrong keys from all clients within `window_s` lock everyone out until the oldest
    leaves the window. A locked attempt is refused before the key is compared. check() and record() run without an
    await in between, so parallel guesses can't pass the bound."""

    def __init__(self, per_client=5, lock_s=60.0, lock_max_s=3600.0, aggregate=30, window_s=900.0, delay_s=1.0,
                 clock=time.monotonic, max_clients=4096):
        self.per_client, self.lock_s, self.lock_max_s = per_client, lock_s, lock_max_s
        self.aggregate, self.window_s, self.delay_s, self.clock = aggregate, window_s, delay_s, clock
        self.max_clients = max_clients
        self.clients = collections.OrderedDict()     # client -> [wrong keys in a row, time of the last one]
        self.recent = collections.deque()            # times of all wrong keys within window_s

    def wait_s(self, client):
        """Seconds this client must wait before a key is compared (0: go ahead)."""
        now = self.clock()
        while self.recent and now - self.recent[0] >= self.window_s:
            self.recent.popleft()
        wait = self.recent[0] + self.window_s - now if len(self.recent) >= self.aggregate else 0.0
        st = self.clients.get(client)
        if st and st[0] >= self.per_client:
            lock = min(self.lock_max_s, self.lock_s * 2 ** (st[0] - self.per_client))
            wait = max(wait, st[1] + lock - now)
        return max(0.0, wait)

    def record(self, client, ok):
        if ok:
            self.clients.pop(client, None)
            return
        now = self.clock()
        self.recent.append(now)
        st = self.clients.pop(client, None) or [0, now]
        self.clients[client] = [st[0] + 1, now]
        while len(self.clients) > self.max_clients:  # bounded memory: the oldest entries go first
            self.clients.popitem(last=False)


def auth_client(request):
    """The client an attempt is counted against: the peer, or behind a local reverse proxy (tailscale serve) the
    last X-Forwarded-For hop, the one the proxy itself appended (earlier hops are the client's own words)."""
    remote = request.remote or ''
    xff = request.headers.get('X-Forwarded-For', '')
    if xff and remote in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
        return xff.split(',')[-1].strip() or remote
    return remote


AUTH_LIMITER = web.AppKey('ride_auth', AuthLimiter)


def make_app(core, key):
    """The phone page, its scripts and the WebSocket, behind the access-key gate."""
    app = web.Application()
    limiter = app[AUTH_LIMITER] = AuthLimiter(delay_s=float(core.cfg.get('auth_fail_delay_s', 1.0)))

    def authed(request):
        c = request.cookies.get(COOKIE, '')
        # D5: UTF-8 bytes: compare_digest raises on a non-ASCII str (a 500, not counted by the limiter)
        return bool(c) and bool(key) and hmac.compare_digest(c.encode('utf-8', 'surrogateescape'), key.encode('utf-8'))

    # R3-7: a browser without the cookie (Safari next to Brave, a new phone) gets a key form at / (POST: the key
    # never sits in a URL, history entry or proxy log). AUD-R3-05: the form is the only way in. Key links (?k=) are
    # retired on every path: refused without the key being compared (a URL key reaches history and proxy logs).
    private = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'}

    def grant(resp):
        resp.set_cookie(COOKIE, key, path='/', secure=True, httponly=True, samesite='Strict', max_age=30 * 86400)
        return resp

    def form(status=200, note=''):
        return web.Response(status=status, text=KEY_FORM.replace('{note}', note), content_type='text/html',
                            headers=private)

    def key_ok(k):
        return bool(k) and bool(key) and hmac.compare_digest(str(k).encode('utf-8', 'surrogateescape'), key.encode('utf-8'))

    @web.middleware
    async def gate(request, handler):
        if request.path == '/healthz':
            return await handler(request)
        if 'k' in request.query:
            log.warning('key link refused (retired: use the form) from %s', request.remote)
            if request.path == '/' and request.method == 'GET':
                return form(403, 'Key links no longer work: type the key here.')
            return web.Response(status=403, text='no', headers=private)
        if request.path == '/auth' and request.method == 'POST':
            try:
                k = (await request.post()).get('k', '')
            except Exception:
                k = ''
            client = auth_client(request)
            wait = limiter.wait_s(client)
            if wait > 0:                        # locked out: the key is not even compared
                log.warning('key form locked for %s (%.0f s)', client, wait)
                resp = form(429, 'Too many wrong keys. Try again later.')
                resp.headers['Retry-After'] = str(int(wait) + 1)
                return resp
            ok = key_ok(k)
            limiter.record(client, ok)
            if ok:
                log.info('access granted via key form')
                return grant(web.Response(status=303, headers={'Location': './', **private}))
            log.warning('bad access key (form) from %s', client)
            if limiter.delay_s > 0:
                await asyncio.sleep(limiter.delay_s)
            return form(403, 'Wrong key.')
        if not authed(request):
            if request.path == '/' and request.method == 'GET':
                return form()
            log.warning('denied %s %s from %s', request.method, request.path, request.remote)
            return web.Response(status=403, text='no')
        return await handler(request)

    app.middlewares.append(gate)

    def static(name, ctype):
        async def h(request):
            return web.FileResponse(os.path.join(STATIC, name),
                                    headers={'Content-Type': ctype, 'Cache-Control': 'no-store'})
        return h

    roots = {'cues': core.cfg.get('cue_dir') or os.path.join(STATIC, 'cues'), 'clips': os.path.join(STATIC, 'clips'),
             'vendor': os.path.join(STATIC, 'vendor')}
    types = {'.m4a': 'audio/mp4', '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.js': 'text/javascript',
             '.wasm': 'application/wasm', '.onnx': 'application/octet-stream', '.txt': 'text/plain; charset=utf-8',
             '.json': 'application/json'}

    async def asset(request):
        """cues/, clips/, vendor/ (fetched browser assets). Flat names only; cacheable (the page keeps cues and
        the wake model in Cache Storage)."""
        root, name = roots[request.match_info['dir']], request.match_info['name']
        ext = os.path.splitext(name)[1].lower()
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.\-]{0,120}', name) or ext not in types:
            return web.Response(status=404)
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            return web.Response(status=404)
        return web.FileResponse(path, headers={'Content-Type': types[ext], 'Cache-Control': 'private, max-age=86400'})

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
        await ws.prepare(request)
        cid = await core.attach(ws)
        if cid is None:
            await ws.close(code=4000, message=b'replaced')
            return ws
        log.info('phone connected (connection %s)', cid)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    if core.conn == cid and core.route == 'server':
                        await core.audio(msg.data, cid)
                elif msg.type == WSMsgType.TEXT:
                    try:
                        d = json.loads(msg.data)
                        if isinstance(d, dict):
                            core.on_client_msg(d, cid)
                    except ValueError:
                        pass
        finally:
            core.detach(ws)
            log.info('phone disconnected (connection %s)', cid)
        return ws

    async def healthz(request):
        return web.Response(text='ok')

    async def graph(request):
        """Speed, lean and G over time for one ride (inline SVG, refreshes every 30 s). Cookie-gated."""
        rides = core.ridelog.rides()
        ride = request.query.get('ride') or (rides[0] if rides else '')
        if ride and not rt.RIDE_ID.fullmatch(ride):
            return web.Response(status=400, text='bad ride id')
        return web.Response(text=rt.graph_html(ride or '-', core.ridelog.read(ride) if ride else []),
                            content_type='text/html', headers={'Cache-Control': 'no-store'})

    app.router.add_get('/', static('index.html', 'text/html; charset=utf-8'))
    app.router.add_get('/worklet.js', static('worklet.js', 'text/javascript'))
    # WP6: gesture.js (WP4) and orb.js (WP5) were missing here, so a real page got 404 and its script stopped
    for js in ('client_state.js', 'gate.js', 'wake.js', 'stt.js', 'telemetry.js', 'kws_worker.js', 'gesture.js', 'orb.js',
               'reset.js', 'orb_motion.js'):
        app.router.add_get('/' + js, static(js, 'text/javascript'))
    app.router.add_get(r'/{dir:cues|clips|vendor}/{name}', asset)
    app.router.add_get('/silence.mp3', static('silence.mp3', 'audio/mpeg'))
    app.router.add_get('/ws', ws_handler)
    app.router.add_get('/healthz', healthz)
    app.router.add_get('/graph', graph)
    return app


class RideServer:
    """Owns the aiohttp runner, the HTTP client session and the core; started by the adapter's connect()."""

    def __init__(self, cfg, submit, secret, key, tts=None, reset=None):
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.submit, self.secret, self.key, self.tts, self.reset = submit, secret, key, tts, reset
        self.core = None
        self.http = None
        self.runner = None
        self.port = None

    async def start(self):
        self.http = ClientSession()
        self.core = RideCore(self.cfg, self.submit, self.secret, self.http, self.tts, self.reset)
        app = make_app(self.core, self.key)
        self.runner = web.AppRunner(app, access_log=None)   # access lines would carry a (refused) ?k=
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.cfg['host'], int(self.cfg['port']))
        try:
            await site.start()
        except Exception:
            await self.stop()
            raise
        sockets = getattr(site._server, 'sockets', None) or []
        self.port = sockets[0].getsockname()[1] if sockets else int(self.cfg['port'])
        return self.port

    async def stop(self):
        if self.core is not None:
            await self.core.shutdown()
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
        if self.http is not None:
            await self.http.close()
            self.http = None
