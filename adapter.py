"""hermes-ride gateway adapter: the phone on the motorcycle is a chat on platform ``ride``.

What Hermes does (not re-implemented here): the agent session, history, memory, tools, user authorization,
approvals and clarify bookkeeping, the system-prompt ``platform_hint``, and — engine ``hermes`` — streaming
TTS (LLM deltas -> sentence chunks -> the configured TTS provider -> PCM to ``write_streaming_tts``).

What this adapter adds: the phone page + WebSocket behind an access key, always-on Scribe v2 realtime STT
with road-noise filtering and barge-in, one turn at a time with queued utterances joined, the live sensor
context line, audio delivery to the phone, spoken approval/clarify notices, and direct ElevenLabs TTS when
Hermes's own TTS can't stream here (engine ``direct``).
"""
from __future__ import annotations

import asyncio
import collections
import dataclasses
import functools
import html
import importlib
import importlib.util
import inspect
import logging
import mimetypes
import os
import re
import sys
import threading
import time
import uuid
import weakref
from typing import Any, Optional

from gateway.config import Platform
from gateway.platform_registry import PlatformEntry
from gateway.platforms._shared import coerce_port, extra_or_secret, get_scoped_secret
from gateway.platforms.base import (AudioFormat, BasePlatformAdapter, ExecApprovalPrompt, SendResult,
                                    StreamingTTSHandle)
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome

from . import ride_logic as rl
from .ride_core import RideServer, redact

log = logging.getLogger('hermes_ride')

PLUGIN_NAME = 'hermes-ride'
PLATFORM = 'ride'
_TRUTHY = {'1', 'true', 'yes', 'on'}
_AUDIO_MIME = {'.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.m4a': 'audio/mp4', '.aac': 'audio/aac',
               '.ogg': 'audio/ogg', '.opus': 'audio/ogg', '.flac': 'audio/flac'}
_MAX_VOICE_FILE = 8 << 20
# R3-1 / AUD-R3-04: Hermes's background-review / memory summary ("Self-improvement review: ...", locale key
# display.review.summary_callback; "Memory updated") reaches send() as an interim send whose metadata is the same as
# the model's own interim lines (spoken): {"_interim_send": True}; non_conversational is set for Discord only. The
# review always comes from one producer, gateway/run_turn_runner.py TurnRunner._make_bg_review_callbacks ->
# deliver() -> _send_status_text() -> adapter.send(). send() identifies that producer while it is calling (its frame
# is on the stack; the coroutine itself runs later on the loop), whatever the text looks like. The leading-glyph rule
# (interim + "\U0001f4be") is only the backup for a Hermes without that producer.
_REVIEW_LINE = re.compile('^\\s*\U0001f4be')
_REVIEW_PRODUCER = ('gateway.run_turn_runner', '_make_bg_review_callbacks.<locals>.deliver')


@functools.lru_cache(maxsize=1)
def _review_producer_known() -> bool:
    """This Hermes has the background-review producer send() recognises (checked once)."""
    try:
        mod = sys.modules.get(_REVIEW_PRODUCER[0]) or importlib.import_module(_REVIEW_PRODUCER[0])
        fn = mod.TurnRunner._make_bg_review_callbacks
        return any(getattr(c, 'co_qualname', '').endswith(_REVIEW_PRODUCER[1]) for c in fn.__code__.co_consts)
    except Exception:
        return False


def _from_review_producer(depth: int = 40) -> bool:
    """True when the review producer's deliver() is on the calling stack."""
    f = sys._getframe(1)
    while f is not None and depth > 0:
        if (f.f_globals.get('__name__') == _REVIEW_PRODUCER[0]
                and getattr(f.f_code, 'co_qualname', '').endswith(_REVIEW_PRODUCER[1])):
            return True
        f, depth = f.f_back, depth - 1
    return False


def _notice_kind(metadata: Optional[dict], content: str, from_producer: bool = False) -> Optional[str]:
    """'review' for the background review, else None (spoken)."""
    if from_producer:
        return 'review'
    if not _review_producer_known() and (metadata or {}).get('_interim_send') and _REVIEW_LINE.match(content or ''):
        return 'review'                         # backup only: this Hermes has no recognisable producer
    return None


def _secret(name: str) -> Optional[str]:
    """Credential read through Hermes's profile-scoped secrets (``external_fallback`` covers reads from the
    adapter's own background tasks, outside any turn)."""
    v = get_scoped_secret(name, None, external_fallback=True)
    v = str(v).strip() if v is not None else ''
    return rl.remember_secret(v) or None           # R6: known to redact() from now on


def _hermes_config() -> dict:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def resolve_engine(setting: Optional[str], tts_cfg: dict) -> str:
    """``auto`` -> ``hermes`` when Hermes's TTS is ElevenLabs and its SDK is importable (so the gateway's
    streaming-TTS consumer can actually produce audio), else ``direct``."""
    s = str(setting or 'auto').strip().lower()
    if s in ('hermes', 'direct'):
        return s
    provider = str((tts_cfg or {}).get('provider') or '').strip().lower()
    if provider == 'elevenlabs' and importlib.util.find_spec('elevenlabs') is not None:
        return 'hermes'
    return 'direct'


def _whistle_defaults(hermes_cfg: dict):
    """(python, weights) for stt_engine whistle from Hermes's own `stt.providers.whistle.command` when it exists:
    the first token if it is a file, and whistle.cact next to the command's script. Else ('python3', '')."""
    try:
        from .ride_whistle import hermes_whistle_paths
    except ImportError:
        from ride_whistle import hermes_whistle_paths
    py, weights = hermes_whistle_paths(hermes_cfg)
    return py or 'python3', weights


def core_config(extra: dict, hermes_cfg: dict) -> dict:
    """Plugin settings: env (profile-scoped) -> ``platforms.ride.extra`` in config.yaml -> defaults; voice
    defaults follow Hermes's own ``tts.elevenlabs`` settings."""
    el = ((hermes_cfg.get('tts') or {}).get('elevenlabs') or {})

    def get(key, env, default=''):
        return extra_or_secret(extra, key, env, default)

    def num(key, env, default):
        try:
            return float(get(key, env, default))
        except (TypeError, ValueError):
            return default

    whisper_py, whisper_weights = _whistle_defaults(hermes_cfg)     # round 3 (B2): Whistle on this host
    mic_db = num('mic_base_db', 'RIDE_MIC_BASE_DB', -38.0)   # Fix E: the gate's loud threshold at standstill (dB)
    play_buf = num('play_buffer_ms', 'RIDE_PLAY_BUFFER_MS', 250)
    play_buf = int(max(0.0, min(1000.0, play_buf))) if play_buf == play_buf else 250   # NaN -> the default
    orb_motion = num('orb_motion', 'RIDE_ORB_MOTION', 1.0)   # P4: how much the orb moves with the bike
    orb_motion = max(0.0, min(2.0, orb_motion)) if orb_motion == orb_motion else 1.0   # NaN -> the default
    cfg = {
        'host': str(get('host', 'RIDE_HOST', '127.0.0.1')),
        'port': coerce_port(get('port', 'RIDE_PORT', 8790), 8790),
        'rider_name': str(get('rider_name', 'RIDE_RIDER_NAME', 'the rider')),
        'end_of_turn_silence_s': max(0.5, min(5.0, num('end_of_turn_s', 'RIDE_END_OF_TURN_S', 1.2))),
        'stt_language': str(get('language', 'RIDE_LANGUAGE', 'en')),   # English locked by default
        'languages': str(get('languages', 'RIDE_LANGUAGES', 'en')),
        'keyterms': str(get('keyterms', 'RIDE_KEYTERMS', '')),
        'stt_route': 'server' if str(get('stt_route', 'RIDE_STT_ROUTE', 'phone')).strip().lower() == 'server'
                     else 'phone',
        # round 3 (B2): which engine transcribes a finished phrase on the server route. Unknown names are read by
        # RideCore as scribe with a logged warning (adapter here: scribe unless it is exactly whistle).
        'stt_engine': str(get('stt_engine', 'RIDE_STT_ENGINE', 'scribe')).strip().lower() or 'scribe',
        'stt_fallback': 'none' if str(get('stt_fallback', 'RIDE_STT_FALLBACK', 'scribe')).strip().lower() == 'none'
                        else 'scribe',
        'whistle_python': str(get('whistle_python', 'RIDE_WHISTLE_PYTHON', whisper_py)) or 'python3',
        'whistle_weights': str(get('whistle_weights', 'RIDE_WHISTLE_WEIGHTS', whisper_weights)),
        'whistle_language': '',          # filled below from stt_language (or the setting when it is not empty)
        'whistle_end_s': max(0.3, min(3.0, num('whistle_end_s', 'RIDE_WHISTLE_END_S', 0.9))),
        'min_phrase_s': max(0.0, min(10.0, num('min_phrase_s', 'RIDE_MIN_PHRASE_S', 0.0))),
        'whistle_timeout_s': max(1.0, min(120.0, num('whistle_timeout_s', 'RIDE_WHISTLE_TIMEOUT_S', 20.0))),
        'window_s': max(0.0, min(300.0, num('window_s', 'RIDE_WINDOW_S', 10))),
        'vad_hangover_ms': max(0, min(5000, int(num('vad_hangover_ms', 'RIDE_VAD_HANGOVER_MS', 2000)))),
        'vad_base_db': max(-70.0, min(-20.0, mic_db if mic_db == mic_db else -38.0)),   # NaN -> the default
        'wake_engine': 'transcript' if str(get('wake_engine', 'RIDE_WAKE_ENGINE', 'sherpa')).strip().lower()
                       == 'transcript' else 'sherpa',
        'cue_dir': str(get('cue_dir', 'RIDE_CUE_DIR', '')) or None,
        'cue_gain': max(0.0, min(1.0, num('cue_gain', 'RIDE_CUE_GAIN', 0.5))),   # R3-2: cues/clips, not the voice
        'tone_first_s': max(0.0, min(30.0, num('tone_first_s', 'RIDE_TONE_FIRST_S', 1.2))),   # WP2: doo-doo lead-in
        'double_tap_ms': int(max(150.0, min(1000.0, num('double_tap_ms', 'RIDE_DOUBLE_TAP_MS', 350)))),   # WP4: mute
        'play_buffer_ms': play_buf,   # the phone holds the first reply segment this long (ms, 0 = off)
        'orb_design': str(get('orb_design', 'RIDE_ORB_DESIGN', 'simple')).strip().lower() or 'simple',   # WP5
        'orb_motion': orb_motion,     # P4: gain of the orb's motion physics on the phone (0 = off, 1, up to 2)
        'wake_word': rl.wake_word(get('wake_word', 'RIDE_WAKE_WORD', rl.DEFAULT_WAKE_WORD))['key'],   # Hermes-Ride
        'show_word': str(get('show_word', 'RIDE_SHOW_WORD', '')).strip().lower() in _TRUTHY,   # WP5: debug word
        # P3 (10 Nov): at a reset, a "Ride Summary?" yes/no row is left as the old session's last message (default on)
        'summary_prompt': str(get('summary_prompt', 'RIDE_SUMMARY_PROMPT', 'true')).strip().lower() in _TRUTHY,
        'price_stt_per_hour': max(0.0, num('price_stt_per_hour', 'RIDE_PRICE_STT_PER_HOUR', 0.39)),
        'price_tts_per_1k_chars': max(0.0, num('price_tts_per_1k_chars', 'RIDE_PRICE_TTS_PER_1K', 0.04)),
        'voice_id': str(get('voice_id', 'RIDE_VOICE_ID', el.get('voice_id') or 'JBFqnCBsd6RMkjVDRZzb')),
        'tts_model_id': str(get('tts_model', 'RIDE_TTS_MODEL',
                                el.get('streaming_model_id') or 'eleven_flash_v2_5')),
    }
    if str(get('language', 'RIDE_LANGUAGE', 'en')).strip().lower() in ('', 'auto'):
        cfg['stt_language'] = ''
    cfg['whistle_language'] = str(get('whistle_language', 'RIDE_WHISTLE_LANGUAGE', cfg['stt_language'] or 'en'))
    return cfg



def ride_character_tool_path() -> str:
    """The installed plugin's own tools/ride_character.py: the summary prompt tells the agent to run this path."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools', 'ride_character.py')


def summary_yes_prompt(logs, poster_path=None) -> str:
    """P3/B3 (10 Nov): the prompt the Yes button sends into the old Ride session (the agent there has the ride's
    conversation). No HTML poster any more: the agent answers in chat with the ride's character against the rider's
    own baseline, the ride as a story, and Ideas / Fix / Maintain from this conversation (listed only)."""
    tool = ride_character_tool_path()
    if logs:
        data = ("This session's ride log(s), 1 Hz JSON lines (t epoch ms, spd km/h, lon_g G with negative = "
                "braking, lean deg, d_m m, acc m, cal, ev): " + ', '.join(logs) + '.')
        char = ("Run this command first and use its JSON (label, why, vs, baseline_rides, caveats, features):\n"
                f"    python3 {tool} " + ' '.join(logs) + " --rides-dir " + os.path.dirname(logs[0]) + " --json\n"
                "Several logs given together are one ride; the baseline is the rider's other rides there.")
    else:
        data = 'There is no telemetry for this session.'
        char = 'Skip the ride-character part: there is no ride log for this session.'
    return ('Yes, make the Ride Summary for this ride. ' + data + '\n' + char + '\n'
            'Then answer IN CHAT, in three parts, as plain text: no map, no stats table, no page to open:\n'
            "1. **Ride character**: the label and what it means, in plain words against the rider's own baseline. "
            'Keep the numbers in the background (no table); say the caveats the tool reports, and say plainly when '
            'there is not enough history yet.\n'
            '2. **The ride**: a short story of this ride from this conversation and the log (where it went, what was '
            'talked about, memorable moments), a few sentences.\n'
            "3. **Ideas / Fix / Maintain**: three short lists from this ride's conversation: new things to try, "
            "bugs and problems raised, and things that work and must keep working. Every item quotes the rider's "
            'own words or says when it was said, so it can be checked. List them only: write nothing to any file.\n'
            'If there is no telemetry, do parts 2 and 3 only. Private to this session: the rider only.')


def summary_widget_html(yes_prompt: str, no_prompt: str) -> str:
    """P3: two big buttons for Hermes Desktop's inline preview; the frame supplies theme vars and font."""
    y, n = html.escape(yes_prompt, quote=True), html.escape(no_prompt, quote=True)
    return f"""<!doctype html><meta charset="utf-8">
<style>
.rs{{display:flex;gap:12px;padding:4px 0}}
.rs button{{font:inherit;font-size:16px;font-weight:600;padding:12px 28px;border-radius:12px;cursor:pointer;
  border:1px solid var(--border);background:var(--card);color:var(--foreground)}}
.rs button.y{{background:var(--accent);color:var(--card);border-color:var(--accent)}}
.rs button:disabled{{opacity:.4;cursor:default}}
</style>
<div class="rs">
  <button class="y" data-hermes-send="{y}">Yes, make the summary</button>
  <button data-hermes-send="{n}">No</button>
</div>
<script>
document.querySelectorAll('.rs button').forEach(b => b.addEventListener('click', () =>
  document.querySelectorAll('.rs button').forEach(x => x.disabled = true)));
</script>
"""

class RideAdapter(BasePlatformAdapter):
    """One phone page = one chat (``RIDE_CHAT_ID``), one user (``RIDE_USER_ID``)."""

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform(PLATFORM))
        self._extra = dict(getattr(config, 'extra', None) or {})
        self.chat_id = str(extra_or_secret(self._extra, 'chat_id', 'RIDE_CHAT_ID', 'ride'))
        self.user_id = str(extra_or_secret(self._extra, 'user_id', 'RIDE_USER_ID', 'rider'))
        self._engine_setting = extra_or_secret(self._extra, 'tts_engine', 'RIDE_TTS_ENGINE', 'auto')
        self.engine = 'direct'
        self.server: Optional[RideServer] = None
        self._event_turns: dict[str, Any] = {}     # gateway event id -> core Turn
        self._streams: dict[int, Any] = {}         # id(StreamingTTSHandle) -> core PCM stream
        self._seq = 0
        self._boot = uuid.uuid4().hex[:6]
        _ADAPTERS.add(self)

    @property
    def core(self):
        return self.server.core if self.server is not None else None

    # ------------------------------------------------------------ lifecycle
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        key = _secret('RIDE_ACCESS_KEY')
        if not key or len(key) < 16:
            self._set_fatal_error('ride_access_key', 'RIDE_ACCESS_KEY is missing or shorter than 16 characters',
                                  retryable=False)
            return False
        if not _secret('ELEVENLABS_API_KEY'):
            self._set_fatal_error('ride_elevenlabs_key', 'ELEVENLABS_API_KEY is not available (speech-to-text)',
                                  retryable=False)
            return False
        hermes_cfg = _hermes_config()
        cfg = core_config(self._extra, hermes_cfg)
        self.engine = resolve_engine(self._engine_setting, hermes_cfg.get('tts') or {})
        cfg['tts_engine'] = self.engine
        try:
            from plugins.plugin_storage import plugin_data_dir
            cfg['log_dir'] = str(plugin_data_dir(PLUGIN_NAME) / 'logs')
            cfg['ride_dir'] = str(plugin_data_dir(PLUGIN_NAME) / 'rides')
            self._summary_dir = str(plugin_data_dir(PLUGIN_NAME) / 'summaries')   # P3: the prompt widgets
        except Exception as e:
            log.warning('ride: no plugin data dir, turn log off: %s', e)
        self._summary_prompt = cfg['summary_prompt']
        self.server = RideServer(cfg, self._submit, _secret, key, reset=self._reset_lane)
        try:
            port = await self.server.start()
        except OSError as e:
            self.server = None
            self._set_fatal_error('ride_bind', f"can't listen on {cfg['host']}:{cfg['port']}: {e}", retryable=True)
            return False
        log.info('ride: phone page on http://%s:%s (tts engine %s, voice %s)', cfg['host'], port, self.engine,
                 cfg['voice_id'])
        self._mark_connected(listener_base=f"http://{cfg['host']}:{port}")
        return True

    async def disconnect(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            await server.stop()
        self._mark_disconnected()

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {'name': 'Ride', 'type': 'dm', 'chat_id': chat_id}

    # ------------------------------------------------------------ inbound: one turn -> gateway
    async def _submit(self, turn, text: str, meta: dict) -> None:
        self._seq += 1
        clarify = bool(meta.get('clarify_answer'))
        event_id = f'ride-{self._boot}-{turn.id}' + (f'-a{self._seq}' if clarify else '')
        source = self.build_source(chat_id=self.chat_id, chat_name='Ride', chat_type='dm', user_id=self.user_id,
                                   user_name=str(self.server.cfg.get('rider_name') or 'rider'),
                                   message_id=event_id)
        mtype = MessageType.VOICE if self.engine == 'hermes' and not clarify else MessageType.TEXT
        # No ``metadata``: Hermes's native admission has a closed metadata contract and refuses any key it
        # doesn't know (``invalid_params (metadata)``, found in the live test). The rider's raw words and the
        # sensors stay in the plugin's own turn log.
        event = MessageEvent(text=text, message_type=mtype, source=source, message_id=event_id,
                             user_id=self.user_id, user_name=source.user_name, reply_expected=True)
        if not clarify:
            self._event_turns[event_id] = turn
            await self._wait_session_idle(event)
        await self.handle_message(event)

    async def _wait_session_idle(self, event, timeout=5.0):
        """The previous turn's background task may still be releasing its session guard right after
        ``on_processing_complete``; a message arriving then would take the gateway's busy path (interrupt)."""
        import asyncio
        active = getattr(self, '_active_sessions', None)
        if not isinstance(active, dict):
            return
        key = self._event_session_key(event)
        for _ in range(int(timeout / 0.05)):
            if key not in active:
                return
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------ WP6 (D5, P6): the phone's reset, gateway part
    async def _reset_lane(self) -> dict:
        """Called by the core after the phone held the reset bar (epoch and socket already checked there). In order:
        interrupt the lane's running turn (``cancel_session_processing``), then the gateway's own ``/new``
        (``_handle_reset_command``: stops the owner turn, archives the old session and opens a new one, clears the
        lane's model/reasoning override and other conversation state through its conversation-scope funnel). Its banner
        is dropped: nothing is spoken. The core resets its own state after this returns. Private gateway APIs: feature-
        detected; any failure returns the step that failed and stops there (the old session is never deleted here)."""
        runner = getattr(self, 'gateway_runner', None)
        new = getattr(runner, '_handle_reset_command', None) if runner is not None else None
        cancel = getattr(self, 'cancel_session_processing', None)
        if not callable(new) or not callable(cancel):
            log.warning('ride: reset: this gateway has no /new path or turn cancel for plugins')
            return {'ok': False, 'step': 'unsupported'}
        self._seq += 1
        mid = f'ride-{self._boot}-reset{self._seq}'
        source = self.build_source(chat_id=self.chat_id, chat_name='Ride', chat_type='dm', user_id=self.user_id,
                                   user_name=str(self.server.cfg.get('rider_name') or 'rider') if self.server else 'rider',
                                   message_id=mid)
        event = MessageEvent(text='/new', message_type=MessageType.TEXT, source=source, message_id=mid,
                             user_id=self.user_id, user_name=source.user_name)
        res = {'ok': False, 'old': self._lane_session_id(runner, source)}
        try:
            await cancel(self._event_session_key(event), release_guard=True, discard_pending=True)
        except Exception as e:
            log.warning('ride: reset: interrupting the turn failed: %s', redact(repr(e))[:160])
            return {**res, 'step': 'interrupt'}
        self._event_turns.clear()
        try:
            await new(event)
        except Exception as e:
            log.warning('ride: reset: /new failed: %s', redact(repr(e))[:160])
            return {**res, 'step': 'new'}
        # after /new, not before: /new stops the owner turn, and an interrupted reply is stored as it unwinds. Posted
        # first, that late reply landed after the prompt (11 Nov ride), so the card was not the old session's last row
        await self._post_summary_prompt(runner, res['old'])
        override = None
        try:
            override = runner._session_model_override(runner._session_key_for_source(source))
        except Exception:
            pass
        return {**res, 'ok': True, 'new': self._lane_session_id(runner, source), 'override_left': bool(override)}

    # ------------------------------------------------------------ P3 (10 Nov): the Ride Summary question at a reset
    async def _post_summary_prompt(self, runner, old_sid) -> None:
        """Right after the session switch, leave a 'Ride Summary?' yes/no question as the old session's last message;
        the rider chooses whether to confirm. Appends one assistant row to the old session (never spoken,
        never sent through ``send()``): a Yes/No widget (``::preview`` of an HTML file with ``data-hermes-send``
        buttons, for Hermes Desktop) plus a plain line so typing "yes" works too. Nothing is generated here: the poster
        is made by the agent only if the rider says yes. Best effort: any failure is logged and the reset goes on."""
        core = self.core
        if not getattr(self, '_summary_prompt', True) or core is None or not old_sid or not core.session_turns:
            return
        store = getattr(runner, 'async_session_store', None)
        folder = getattr(self, '_summary_dir', None)
        rides = list(core.session_rides)
        ok = False
        try:
            if store is None or not folder:
                raise RuntimeError('no session store or data dir')
            logs = [p for p in (core.ridelog.path(r) for r in rides) if p and os.path.isfile(p)] or \
                   [p for p in (core.ridelog.path(r) for r in rides) if p]
            safe = re.sub(r'[^A-Za-z0-9_.-]', '_', str(old_sid))[:80]
            os.makedirs(folder, exist_ok=True)
            widget = os.path.join(folder, f'prompt-{safe}.html')
            with open(widget, 'w', encoding='utf-8') as f:
                f.write(summary_widget_html(summary_yes_prompt(logs), 'No ride summary, thanks.'))
            lines = ['**Ride Summary?**', '', 'This ride session is closed. Want a summary of it?', '',
                     f'::preview{{file="{widget}"}}', '',
                     'Or just reply **yes**. Ride log: ' + (', '.join(f'`{p}`' for p in logs) if logs
                                                           else 'none (no telemetry this session).')]
            await asyncio.wait_for(store.append_to_transcript(old_sid, {
                'role': 'assistant', 'content': '\n'.join(lines), 'timestamp': time.time()}), 5.0)
            ok = True
        except Exception as e:
            log.warning('ride: reset: Ride Summary prompt not written: %s', redact(repr(e))[:160])
        core.turns.write({'event': 'summary_prompt', 'at': rl.now(), 'ok': ok, 'session': str(old_sid)[:64],
                          'rides': len(rides)})

    @staticmethod
    def _lane_session_id(runner, source) -> Optional[str]:
        """The Ride lane's gateway session id, for the reset log only (None when the gateway does not say)."""
        try:
            sid = runner.session_store.peek_session_id(runner._session_key_for_source(source))
            return str(sid)[:64] if sid else None
        except Exception:
            return None

    async def on_processing_start(self, event: MessageEvent) -> None:
        turn = self._event_turns.get(getattr(event, 'message_id', None) or '')
        if turn is not None:
            turn.rec.setdefault('agent_start', rl.now())

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        turn = self._event_turns.pop(getattr(event, 'message_id', None) or '', None)
        if turn is not None and self.core is not None:
            self.core.turn_done(turn, ok=outcome == ProcessingOutcome.SUCCESS)

    # ------------------------------------------------------------ outbound: replies -> phone audio
    @inspect.markcoroutinefunction
    def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
             metadata: Optional[dict[str, Any]] = None):
        """Awaitable like the base class's ``async def send``, but a plain method: AUD-R3-04 decides here, while
        the caller is still on the stack, whether this is the gateway's background review."""
        return self._send(chat_id, content, reply_to, metadata, _notice_kind(metadata, content, _from_review_producer()))

    async def _send(self, chat_id: str, content: str, reply_to: Optional[str], metadata: Optional[dict[str, Any]],
                    notice: Optional[str]) -> SendResult:
        core = self.core
        if core is None:
            return SendResult(success=False, error='ride platform not connected', retryable=True)
        if notice:
            core.internal_notice(notice, content)
            return SendResult(success=True, message_id=f'ride-review-{uuid.uuid4().hex[:10]}')
        # Engine hermes: the agent's own words are already being spoken by Hermes's streaming TTS; the text is
        # kept and spoken only if that produced no audio this turn (core safety net).
        speak = self.engine != 'hermes' or core.active is None
        ok = await core.reply_text(content, speak=speak)
        if not ok:
            return SendResult(success=False, error='no phone attached')
        return SendResult(success=True, message_id=f'ride-out-{uuid.uuid4().hex[:10]}')

    async def send_or_update_status(self, chat_id: str, status_key: str, content: str,
                                    metadata: Optional[dict[str, Any]] = None) -> SendResult:
        """R3-1: the gateway's agent-status egress (``gateway/run.py`` ``_send_or_update_status_coro``, fed by
        ``status_callback``): the memory recall indicator, retry / fallback diagnostics, compression progress.
        A voice platform has no status bar, so none of it is spoken; it is logged by kind and word count only.
        A failed turn still speaks: its final error text arrives through ``send()``, and a turn with no text at
        all gets the core's short fallback error."""
        if self.core is not None:
            self.core.internal_notice('status', content)
        return SendResult(success=True)

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
                         **kwargs) -> SendResult:
        """Whole-file audio from Hermes (auto-TTS fallback, a voice reply, the TTS tool) -> one segment."""
        core = self.core
        if core is None:
            return SendResult(success=False, error='ride platform not connected', retryable=True)
        try:
            if os.path.getsize(audio_path) > _MAX_VOICE_FILE:
                return SendResult(success=False, error='audio file too large for the phone link')
            with open(audio_path, 'rb') as f:
                data = f.read()
        except OSError as e:
            return SendResult(success=False, error=f'cannot read audio: {e}')
        ext = os.path.splitext(audio_path)[1].lower()
        mime = _AUDIO_MIME.get(ext) or mimetypes.guess_type(audio_path)[0] or 'audio/mpeg'
        if not await core.audio_segment(data, mime):
            return SendResult(success=False, error='no phone attached')
        return SendResult(success=True, message_id=f'ride-voice-{uuid.uuid4().hex[:10]}')

    def _should_auto_tts_for_chat(self, chat_id: str) -> bool:
        """Ride is voice-only: Hermes speaks every reply when it owns TTS (engine hermes), never otherwise
        (engine direct speaks the text itself; a second synthesis would double the audio)."""
        return self.engine == 'hermes' and str(chat_id) == self.chat_id

    # Streaming TTS contract (#60671): PCM from Hermes's TTS provider while the model is still generating ----
    def supports_streaming_tts(self, chat_id: str, audio_format: AudioFormat) -> bool:
        return (self.engine == 'hermes' and str(chat_id) == self.chat_id and self.core is not None
                and int(getattr(audio_format, 'sample_width', 2)) == 2)

    async def begin_streaming_tts(self, chat_id: str, audio_format: AudioFormat,
                                  metadata: Optional[dict[str, Any]] = None) -> Optional[StreamingTTSHandle]:
        core = self.core
        if core is None:
            return None
        stream = core.pcm_begin(audio_format.sample_rate, audio_format.channels, audio_format.sample_width)
        if stream is None:
            return None
        handle = StreamingTTSHandle(chat_id=str(chat_id), audio_format=audio_format)
        self._streams[id(handle)] = stream
        return handle

    async def write_streaming_tts(self, handle: StreamingTTSHandle, chunk: bytes) -> None:
        stream = self._streams.get(id(handle))
        if stream is not None and not handle.aborted and self.core is not None:
            self.core.pcm_write(stream, chunk)

    async def finish_streaming_tts(self, handle: StreamingTTSHandle, *, interrupted: bool = False) -> None:
        stream = self._streams.pop(id(handle), None)
        if stream is not None and self.core is not None:
            self.core.pcm_end(stream, flush=not interrupted)

    async def abort_streaming_tts(self, handle: StreamingTTSHandle, error: Optional[str] = None) -> None:
        stream = self._streams.pop(id(handle), None)
        if stream is not None and self.core is not None:
            self.core.pcm_end(stream, flush=False)
        if error:
            log.info('ride: streaming TTS aborted: %s', redact(str(error))[:160])

    # ------------------------------------------------------------ approvals / clarify (catalog rule 12)
    async def _send_exec_approval_prompt(self, prompt: ExecApprovalPrompt) -> SendResult:
        """A rider can't tap a card and road noise must never approve anything: speak a notice, never
        resolve it. The approval stays open for another Hermes surface; otherwise Hermes's own timeout
        denies it (its timeout notice is spoken too)."""
        core = self.core
        if core is None:
            return SendResult(success=False, error='ride platform not connected')
        try:
            minutes = max(1, round(int((_hermes_config().get('approvals') or {}).get('timeout', 300)) / 60))
        except (TypeError, ValueError):
            minutes = 5
        await core.speak_notice(
            "That needs approval, and I can't take approvals by voice. Approve it in another Hermes app, "
            f"or it will be declined in about {minutes} minute{'s' if minutes != 1 else ''}.")
        log.info('ride: approval request %s announced by voice (not resolvable here)', prompt.request_id)
        return SendResult(success=True, message_id=f'ride-approval-{uuid.uuid4().hex[:10]}')

    async def send_clarify(self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
                           session_key: str, metadata: Optional[dict[str, Any]] = None) -> SendResult:
        """Spoken question; the rider's next utterance is the answer (the gateway's clarify text-intercept
        resolves it). An unanswered question runs into Hermes's clarify timeout as usual."""
        core = self.core
        if core is None:
            return SendResult(success=False, error='ride platform not connected')
        text = rl.strip_markdown(question)
        if choices:
            opts = '; '.join(f'{i}: {rl.strip_markdown(str(c))}' for i, c in enumerate(choices, start=1))
            text += f' Options: {opts}. Say the one you want.'
            try:
                from tools.clarify_gateway import mark_awaiting_text
                mark_awaiting_text(clarify_id)
            except Exception as e:
                log.debug('ride: mark_awaiting_text failed: %s', e)
        core.passthrough = True
        await core.speak_notice(text)
        return SendResult(success=True, message_id=f'ride-clarify-{uuid.uuid4().hex[:10]}')


# ---------------------------------------------------------------- tool-state hooks (W11)
# Hermes calls plugin hooks for every session. Only ride sessions matter: pre_llm_call / post_api_request carry
# ``platform`` and teach us the ride session ids (the Raft pattern); pre/post_tool_call carry only session_id.
# Each learned session is bound to the one adapter (core) that owns it, and events carry the Hermes turn_id, so a
# ride event never reaches another profile's core or a later turn (AUD-V2-13).
# The hooks read the tool *name* and token counts, never tool args or results, and only send state frames.
_ADAPTERS: 'weakref.WeakSet[RideAdapter]' = weakref.WeakSet()
_RIDE_SESSIONS: 'collections.OrderedDict[str, weakref.ref]' = collections.OrderedDict()   # session -> owner
_HOOK_LOCK = threading.Lock()


def _platform(kwargs: dict) -> str:
    p = kwargs.get('platform')
    return str(getattr(p, 'value', p) or '')


def _owner(kwargs: dict, learn: bool = False):
    """AUD-V2-13: the ride adapter that owns this hook's session, or None. A payload that names another platform
    is never a ride event, even for a learned session id. A ride session is learned (pre_llm_call / a ride
    post_api_request) by the one adapter with a running turn for that sender; ambiguity binds nothing."""
    plat, sid = _platform(kwargs), kwargs.get('session_id')
    sid = sid if isinstance(sid, str) and sid else None
    if plat and plat != PLATFORM:
        return None
    with _HOOK_LOCK:
        ref = _RIDE_SESSIONS.get(sid) if sid else None
        owner = ref() if ref is not None else None
        if owner is not None or not (learn and plat == PLATFORM):
            return owner
        sender = str(kwargs.get('sender_id') or '')
        cands = [a for a in list(_ADAPTERS) if getattr(a, 'core', None) is not None
                 and getattr(a.core, 'active', None) is not None and (not sender or str(a.user_id) == sender)]
        if len(cands) != 1:
            if cands:
                log.info('ride hook: %d busy ride adapters for one session, event not routed', len(cands))
            return None
        if sid:
            _RIDE_SESSIONS[sid] = weakref.ref(cands[0])
            while len(_RIDE_SESSIONS) > 256:
                _RIDE_SESSIONS.popitem(last=False)
        return cands[0]


def _to_core(owner, method: str, *args) -> None:
    core = getattr(owner, 'core', None) if owner is not None else None
    loop = getattr(core, 'loop', None) if core is not None else None
    if loop is None or loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(getattr(core, method), *args)
    except RuntimeError:
        pass


def _turn_id(kwargs: dict):
    t = kwargs.get('turn_id')
    return str(t)[:64] if t else None


def _on_pre_llm_call(**kwargs) -> None:
    owner = _owner(kwargs, learn=True)
    if owner is not None and _turn_id(kwargs):
        # R9: capture the turn running now (an atomic attribute read); the core binds only if it still runs
        _to_core(owner, 'bind_hook_turn', _turn_id(kwargs), getattr(owner.core, 'active', None))


def _on_pre_tool_call(**kwargs) -> None:
    _to_core(_owner(kwargs), 'on_tool', str(kwargs.get('tool_name') or '')[:64], _turn_id(kwargs))


def _on_post_tool_call(**kwargs) -> None:
    _to_core(_owner(kwargs), 'on_tool_done', str(kwargs.get('tool_name') or '')[:64], _turn_id(kwargs))


def _on_post_api_request(**kwargs) -> None:
    usage = kwargs.get('usage')
    if isinstance(usage, dict):
        _to_core(_owner(kwargs, learn=True), 'on_llm_usage',
                 {k: usage.get(k) for k in ('input_tokens', 'output_tokens', 'cache_read_tokens')}, _turn_id(kwargs))


HOOKS = (('pre_llm_call', _on_pre_llm_call), ('pre_tool_call', _on_pre_tool_call),
         ('post_tool_call', _on_post_tool_call), ('post_api_request', _on_post_api_request))


# ---------------------------------------------------------------- registration
def check_requirements() -> bool:
    """Passive: the adapter needs only aiohttp (shipped with Hermes's gateway)."""
    return importlib.util.find_spec('aiohttp') is not None


def validate_config(config) -> bool:
    return bool(_secret('RIDE_ACCESS_KEY') and _secret('ELEVENLABS_API_KEY'))


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> Optional[dict]:
    """Seed ``PlatformConfig.extra`` before construction; None until the two required secrets exist."""
    if not validate_config(None):
        return None
    from gateway.platforms._shared import seed_extra_from_env
    return seed_extra_from_env((
        ('RIDE_HOST', 'host', None), ('RIDE_PORT', 'port', int), ('RIDE_TTS_ENGINE', 'tts_engine', None),
        ('RIDE_CHAT_ID', 'chat_id', None), ('RIDE_USER_ID', 'user_id', None),
    ), home_env='RIDE_HOME_CHANNEL', home_default='ride')


def register(ctx):
    kwargs = dict(
        emoji='\U0001F3CD',
        platform_hint=rl.PLATFORM_HINT,
        allowed_users_env='RIDE_ALLOWED_USERS',
        allow_all_env='RIDE_ALLOW_ALL_USERS',
        max_message_length=0,
        is_connected=is_connected,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var='RIDE_HOME_CHANNEL',
        allow_update_command=False,
        # Spoken platform: no tool-progress chatter, no edit-streaming; the model's own interim lines
        # ("let me check") are kept so the rider isn't left in silence during tool calls.
        display_tier='minimal',
        display_overrides={'interim_assistant_messages': True, 'streaming': False},
        dashboard_description='Hands-free motorcycle voice chat: phone page with mic, GPS and lean angle; '
                              'Scribe speech-to-text; spoken replies.',
        dashboard_env_vars=('RIDE_ACCESS_KEY', 'ELEVENLABS_API_KEY', 'RIDE_ALLOWED_USERS', 'RIDE_PORT'),
    )
    known = {f.name for f in dataclasses.fields(PlatformEntry)}
    dropped = sorted(k for k in kwargs if k not in known)
    if dropped:
        log.info('ride: this Hermes has no PlatformEntry fields %s (skipped)', dropped)
    ctx.register_platform(
        name=PLATFORM,
        label='Ride',
        adapter_factory=RideAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=['RIDE_ACCESS_KEY', 'ELEVENLABS_API_KEY'],
        install_hint='No extra packages (uses aiohttp from the Hermes gateway)',
        **{k: v for k, v in kwargs.items() if k in known},
    )
    for name, fn in HOOKS:
        ctx.register_hook(name, fn)
