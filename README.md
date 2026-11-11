# Hermes-Ride

<p align="center"><img src="assets/hermes-ride.svg" width="280" alt="Hermes-Ride mascot: Hermes in a headset"></p>

**Docs:** rider's manual `docs/MANUAL.md` (setup, use, mute, reset) · voice, speech-to-text and model wiring
`docs/VOICE-AND-MODELS.md` (for installers and for a Hermes agent setting it up).

Talk with your Hermes agent hands-free while riding a motorcycle. Your phone sits on the handlebar mount with
the screen on and black, your helmet headset is the mic and speaker, you say **"Hey Hermes"** (the default wake word; `RIDE_WAKE_WORD`), and the agent hears
**where you are and how you're riding**: speed, heading, position, lean angle, G, and what happened since you last
asked.

Hermes-Ride (plugin id `hermes-ride`) is a Hermes **gateway platform plugin** (`ride`). The phone is just another chat: your agent with its
memory, tools and session history answers, not a separate bot.

## Change it by asking your agent
No settings screen: tell your Hermes agent what you want (e.g. *"make the conversation window 15 seconds"*) and it
sets the matching key under `platforms.ride.extra` in `config.yaml`, then restarts the gateway. Do that while you are
parked: a restart drops the Ride connection. Full list with ranges: the settings table under [Install](#install).

| Ask for | Key | Default |
|---|---|---|
| another wake word | `wake_word` | `hey hermes` (also `hey lana`) |
| a longer or shorter follow-up window after an answer | `window_s` | `10` s |
| how the agent names you | `rider_name` | "the rider" |
| a different reply voice or voice model | `voice_id`, `tts_model` | from Hermes's `tts.elevenlabs` settings |
| free, offline speech-to-text on your server instead of ElevenLabs Scribe | `stt_engine: whistle` | `scribe` |
| a less (or more) sensitive mic | `mic_base_db` (higher = less sensitive) | `-38` dBFS |
| quieter or louder cues and status clips | `cue_gain` | `0.5` |
| when the "doo, doo" thinking tone starts | `tone_first_s` | `1.2` s |
| a slower or faster double tap to wake | `double_tap_ms` | `350` ms |
| smoother reply audio (bigger start buffer) | `play_buffer_ms` | `250` ms |
| no *Ride Summary?* question after a reset | `summary_prompt: false` | on |
| the state word under the orb, for debugging | `show_word: true` | off |

**Feedback, bugs, ideas:** open an issue at
[github.com/martyllmaccs-hash/Hermes-Ride-hackathon/issues](https://github.com/martyllmaccs-hash/Hermes-Ride-hackathon/issues).

## Architecture (protocol v2, "web app first")
As much as possible runs **on the phone**, so the data stream to the server is small: the server sees text, VAD
edges and telemetry, not your audio.

```
 phone page (screen on, black) ───────────────────────────────────────────────────────────────────────┐
 mic ─► AudioWorklet 20 ms frames ─► energy VAD (threshold rises with speed) ─► gate                   │
          │                              sleep: nothing leaves the phone                              │
          └─► wake word "Hey Hermes" (sherpa-onnx WASM, in a Worker) ─► wake cue plays at once ─► awake│
                awake / 10 s conversation window: ─► ElevenLabs Scribe realtime, DIRECT (single-use token) │
 cues + status clips (Cache Storage), orb states, telemetry (calibration, lean, G, events, IndexedDB)   │
 └──────────────── WS: text, VAD edges, telemetry batches every 30 s ▲ binary reply audio ──────────────┘
                                   ▼                                 │
 ride adapter (in your Hermes gateway): token minting, adaptive end of turn, stale-reply hold/discard,
 filler filter, barge-in rule, tool-state hooks, ride log, usage + cost, spoken reply (TTS)
                                   ▼
                       gateway handle_message()  →  your agent (one turn at a time)
```

- **Wake word "Hey Hermes"** by default; `wake_word` (`RIDE_WAKE_WORD`) picks another from the built-in list
  (`hey hermes`, `hey lana`; `ride_logic.WAKE_WORDS`, one sherpa keywords file each). While the phone sleeps with the
  default sherpa engine,
  nothing leaves it. With the transcript fallback engine, speech-like sound while asleep (a VAD *probe*) is sent to
  Scribe (direct, or via your server on `stt_route=server`) so the wake word can be checked; a miss starts no turn
  and no text is logged. In the conversation window everything the mic hears streams to Scribe, road noise
  included.
- **Recovery**: one STT connection state machine on the phone (gate + Scribe socket + tokens) is reconciled on
  every reconnect, provider close and quota backoff. A Scribe socket that doesn't start a session within 8 s is
  closed and retried after a growing backoff (2 s … 30 s); while awake that is spoken ("reconnecting"). The page
  shows *listening* and plays *back* only after a Scribe session really started (after an outage or quota while
  asleep: at the next wake). A failed playback is retried once (one retry per attempt, the failed attempt is
  stopped), then skipped (the queue never wedges) and logged as not delivered. Tap 1 never hangs: a pending audio
  unlock or audio start times out with "tap 1 again", releasing the mic and audio context it took.
- **Cues** (all local, D2): wake answer (random of two), *thinking*, *done* before the answer and *done* after it.
  There is no separate extended cue or state any more (WP1): long thinking, tool calls, web search and subagents
  all stay *thinking* on the page (the server still sends `extended` / `working` frames for its logs).
- **Status clips in Lana's voice**, played locally, so they work with no connection: "Lost the connection,
  reconnecting." (after 5 s down), "I'm back.", "I'm offline. I'll keep trying." (after 60 s), "Still working on
  it." (a turn past 25 s with no audio), "Speech recognition's out of quota. Switching to backup.", "I can't hear
  the mic."
- **The orb**: one circle in the middle of the black page, at most 3/4 of the screen width (`min(75vw, 75vh)`, so it
  fits in landscape too). Sleep = pure black and static (no animation, no timers, no style writes); listening = blue,
  a slow breath, plus a ring that follows your voice level (the mic frame level the gate already measures); thinking =
  swells to bright white for about 1 s, then breathes slowly, each breath restarted on the "doo, doo" tone; speaking =
  blue with a pulse (the ring follows the reply's level for WAV segments); offline / reconnecting / connecting = a slow
  amber pulse. The look (`orb_design` / `RIDE_ORB_DESIGN`, default `simple`): a halo, light drifting slowly inside the
  core and a glass shell with a sky highlight; `plain` is the flat orb without those; unknown names fall back to
  `simple`. Everything is CSS, and only opacity and transform ever change. `show_word` (`RIDE_SHOW_WORD`, default off)
  shows the state word under it for debugging.
- **The orb moves with the motorcycle** (`static/orb_motion.js`): while it is awake it takes the phone's motion
  sensors, at most 20 style updates a second (CSS custom properties; CSS transitions smooth them), none asleep.
  - *Lean*: the orb counter-leans like a spirit level: its light and its top stay under the real sky while the phone
    leans with the bike. A leaned motorcycle has almost no lateral G (gravity plus cornering force stay along the
    bike's own vertical), so lean is **not** read from the accelerometer: it is the gyro's roll rate, held by the turn
    rate and GPS speed (`sin(lean) = v x yaw rate / g`). Without GPS speed it shows the roll and drifts back upright
    in a few seconds.
  - *Braking* squashes the orb and pushes it up the screen (forward); *accelerating* stretches it and pulls it back.
  - *Bumps* kick a short jelly wobble (settled in under a second); the extra load in a turn or a dip presses it flatter.
  - `orb_motion` (`RIDE_ORB_MOTION`, default `1`, `0` to `2`): how strong; `0` switches it off, and so does the
    phone's "reduce motion" setting. It assumes the phone is mounted with the screen facing you.
- **Wake by double tap** (10 Nov): two taps anywhere within `double_tap_ms` (default 350 ms, `RIDE_DOUBLE_TAP_MS`,
  150 to 1000) wake Lana exactly like saying "Hey Lana": the same wake cue, the same conversation window, the same
  `{t:'wake'}` frame (so the server log cannot tell a tap from a spoken wake). It works only while she is asleep (for
  the transcript engine also while it is probing speech). While she is awake, thinking or speaking a double tap does
  nothing: it never cuts her off. While muted it does nothing. Not a tap: a second finger, a drag or swipe, a long
  press, or a tap on a button/link or inside an element marked `data-no-dbltap` (the setup screen, the reset reveal).
  `touch-action: manipulation` stops iOS from zooming on the double tap.
- **MUTE / UNMUTE** (round 3): the one mic control, a one-tap button in the reset reveal (below), as big as the reset
  bar (88vw x 72px, 19px text). Muted: the gate sleeps, the pre-roll is dropped, speech-to-text is closed and nothing
  reaches the wake spotter, so neither the wake word nor a double tap wakes her; the button turns cyan and reads
  UNMUTE. The mic stays captured, so unmuting is instant (no reload), and a mute pressed while the mic is being
  restarted is kept. The lock-screen / Control Centre pause button mutes and play unmutes (Media Session).
- **Reset** (WP6): a grey grabber sits near the bottom edge, above the iPhone's home indicator. Drag up from it and a
  long red bar labelled **RESET – NEW SESSION** and the MUTE button appear (they hide again after 10 s untouched). **Hold the red bar 3 s** (it
  fills while you hold; letting go or sliding off cancels): a short rising cue plays, the server interrupts Lana's
  current turn, starts a new conversation through the gateway's own `/new` (the old session stays in the history;
  a model/reasoning override on the Ride lane is cleared) and resets its own state, and the page reloads. Without an
  answer within 5 s it reloads anyway. After the reload iOS needs the setup tap again. If the
  server reports a failure you hear two low notes, the screen says which step failed, and you can hold the bar again;
  the conversation keeps working.
- **Ride Summary?** (10 Nov; an AI summary in chat since round 3): just before the reset switches sessions, the old
  session gets one last message, **Ride Summary?** (*"This ride session is closed. Want a summary of it?"*), with
  *Yes* / *No* buttons. Nothing is summarised unless you say yes. Never spoken; skipped for a session with no turns;
  a failure never blocks the reset. Off with `summary_prompt: false` (`RIDE_SUMMARY_PROMPT`). What *Yes* produces:
  see *Ride Summary* below.
- **Setup screens** (WP7): one tap on the pulsing orb starts everything (audio, mic, motion, GPS, link), no
  instruction text; the key form for a new browser shows the same orb above it.
- **Conversation window**: after an answer the mic stays open for 10 s (longer while you are still speaking), so follow-ups need no wake word. While Lana is thinking the page plays a soft repeating "doo, doo" (the Hermes voice-mode "thinking" blubs): first one `tone_first_s` (default 1.2 s, `RIDE_TONE_FIRST_S`, 0 to 30) after the thinking cue, so a quick answer never hears it, then one every 4 to 6 s until the reply starts (no spoken "still working", no separate extended state).
- **Turn-taking**: adaptive end of turn (1.2 s after a question, 1.8 s after a full stop, 3.5 s when you stop
  mid-sentence; counted from the end of your speech); a reply that becomes ready while you're still talking is
  held, and if you said more it is **discarded** and everything you said is answered in one turn; filler-only
  replies ("Mm-hm.") are not spoken; barge-in needs ≥ 3 words that started while Lana was speaking (not "yeah
  okay").
- **Telemetry** processed on the phone: orientation calibration (down = mean gravity over 60 s; forward = dominant
  horizontal axis of linear acceleration over 120 s, sign from GPS; pocket-shift flag), lean (plus a turn-rate
  cross-check), per-minute G stats and events (hard braking < -0.6 G, acceleration > 0.4 G, lean > 40°, stopped
  > 2 min, GPS lost). Every turn gets a "since last turn" line (distance, speeds, max lean, events with times) and
  the path of the full 1 Hz ride log. **`/graph`** shows speed, lean and G over time.
- **Usage**: per turn Scribe seconds (all audio sent, with or without a transcript), TTS characters and seconds,
  LLM tokens (from Hermes's `post_api_request` hook, routed to the turn whose Hermes `turn_id` it carries; events
  without one are ignored); a ride-end summary with an estimated cost, including turns still in flight and audio
  after the last turn; what an in-flight turn uses after the summary is written as a `ride_summary_reconciled`
  record when it finishes. Server route: seconds of audio actually relayed to Scribe. Prices are settings.
- Approvals are **never** given by voice (spoken notice; approve elsewhere or Hermes's timeout declines). Clarify
  questions are spoken and your next sentence answers them.

Measured on a real gateway with a fake phone streaming recorded speech (details in `HANDOFF.md`): sensor question
"How fast am I going and how far am I leaning?" → "You're going 87 kilometres per hour and leaning 24 degrees to
the right.", 7.2 s from the end of speech to the first audio frame on a noisy-road recording (the agent took
4.0 s); speech end → transcript 1.5–1.8 s. On the phone the *done* cue (1.8 s) plays before the answer by design.

### Protocol v2 (`/ws`, cookie-gated)
phone → server: `hello {v:2, caps:{wake, stt}}`, `gate {s}` (on every change and reconnect), `wake`,
`vad {s:1|0}`, `partial`, `committed {text, lang}`, `stt_usage {s}` (Scribe seconds sent, 1 Hz), `need_token`,
`stt_error` / `stt_ok`, `sensors` (while awake), `telemetry {ride, batch}`, `summary`, `play` /
`idle {heard}` / `play_error`, `no_wake`, `reset {epoch}` (must carry this connection's epoch; refused when stale,
from an old page, while a reset runs or replayed); binary PCM16 only with `stt_route=server` and the gate open.
server → phone: `hello {v:2, epoch, cfg}` (first frame), `state {s: listening|thinking|extended|ready|working|
quota, turn, why}`, `token` (never logged), `token_error`, `stt_backoff`, **binary audio frames** (1 byte type
1=mp3 2=wav 3=mp4 4=ogg + uint32 turn + uint16 seq + one playable file; ~25 % smaller than base64 JSON), `end`,
`stop`, `hold`, `discard`, `telemetry_ack`, `wake_hit` / `wake_miss` (server route, transcript probe),
`final` / `dropped` / `reply`, `reset_done` / `reset_failed {step}`. Every JSON frame carries the epoch;
the page drops everything of an old epoch.

## Ride Summary
At a reset (with `summary_prompt` on and at least one turn in the session) the plugin appends one assistant message to
the **old** Ride session: the question, a small Yes / No widget (an inline preview in Hermes Desktop) and the paths of
that session's ride logs. The plugin itself summarises nothing and calls no model.

- **Yes** sends a prepared prompt into that old session, so the agent that held the ride's conversation answers
  **in chat** (no HTML poster, no map, no stats table), in three parts:
  1. **Ride character**: a label and what it means, in plain words against the rider's own baseline; the numbers
     stay in the background and the caveats are said.
  2. **The ride**: a short story from the conversation and the log.
  3. **Ideas / Fix / Maintain**: three short lists pulled from the ride's conversation, each item with the rider's
     own words quoted or the time it was said, so it can be checked. They are listed only: the prompt tells the agent
     to write nothing to any file.

  A session without telemetry gets parts 2 and 3 only. **No** sends "No ride summary, thanks." Typing "yes" instead
  of clicking also works as an answer, but then the agent sees only the question and the log paths, not the prepared
  three-part prompt.
- **The `ride_character` tool**: the prompt tells the agent to run
  `python3 <plugin folder>/tools/ride_character.py <this session's ride logs> --rides-dir <the rides folder> --json`
  first and to use its JSON. So the agent needs a shell tool and a `python3` on its path; the tool is pure standard
  library (`ride_character.py`), reads the 1 Hz ride logs, writes nothing, and can be run by hand:
  `tools/ride_character.py <ride log>... [--rides-dir DIR] [--json]` (several logs of one session are joined as one
  ride; JSON is the only output format). From each ride it takes duration, moving time, distance, the share of time
  stopped, the share below 15 km/h while moving, mean and 95th-percentile braking and acceleration, stops per km,
  top speed, speed spread and the share of time leaning over 20°.
- **Labels** (fixed): `calm`, `spirited`, `stop-start`, `congested filtering`, `mixed`, `too short`. The baseline is
  the median of each number over the rider's other rides in the rides folder that have at least 3 minutes of moving
  time (the latest 20). A ride is `spirited`, `stop-start` or `congested filtering` when its braking/acceleration,
  its stops, or its slow share is at least a quarter above the rider's own median; otherwise `calm` or `mixed`.
- **Cold start**: with fewer than 5 such rides there is no baseline. The ride is then described on its own terms
  from fixed thresholds (stopped 35 % of the time or more: stop-start; half or more of the moving time under
  15 km/h: congested filtering; 95th-percentile braking from 0.35 G or acceleration from 0.3 G: spirited; else
  calm), and the output carries the caveat "not enough history for a baseline" with the number of rides it had.
  A ride with less than 3 minutes of moving time is `too short` and gets no character.
- **Never invented**: a missing part is a caveat in the output (no motion data: pace only; lean and acceleration
  still calibrating at the start; no speed data), not a guessed value.
- **Not checked yet**: how Hermes Desktop renders the Yes click and the reply in the old session, and how good the
  summary is on a real ride: the tool is tested on synthetic logs (plus one read-only pass over real ride logs that
  must not crash), the agent's three-part answer is not tested at all.

## Requirements
- Hermes Agent **≥ 0.23.0** with the gateway (aiohttp ships with it).
- An **ElevenLabs API key** (Scribe speech-to-text, and text-to-speech unless Hermes's own TTS is used). It is
  required at start with `stt_engine: whistle` too.
- Only for `stt_engine: whistle`: on the Hermes host, a Python interpreter that has the `needle` package (Whistle)
  and the Whistle weights file (`.cact`). The plugin installs neither.
- **HTTPS** to the phone (iOS allows mic and motion only on secure origins): e.g. Tailscale (below).
- A phone browser with Web Audio + AudioWorklet, WebAssembly, `getUserMedia`, Geolocation, DeviceMotion,
  IndexedDB. Bluetooth helmet headset in call mode.

## Install
```bash
hermes plugins install <owner>/hermes-ride --enable
cd <hermes home>/plugins/hermes-ride && sh tools/fetch_assets.sh     # wake-word engine + model, ~33 MB download
```
`tools/fetch_assets.sh` downloads the browser wake-word assets into `static/vendor/` (gitignored, ~16 MB kept),
every file pinned by URL and sha256 (needs `curl`, `sha256sum`, `python3`). Without them the page falls back to
the transcript wake engine by itself.

Secrets (profile-scoped, e.g. `.env` or your secret manager), then restart that profile's gateway:

| Variable | |
|---|---|
| `RIDE_ACCESS_KEY` | **required**, 24+ random characters, e.g. `python3 -c "import secrets;print(secrets.token_urlsafe(24))"` |
| `ELEVENLABS_API_KEY` | **required**, also with `RIDE_STT_ENGINE=whistle` (stays on the server; the phone gets 15-minute single-use Scribe tokens) |
| `RIDE_ALLOWED_USERS=rider` | gateway allowlist: the phone always speaks as user `rider` (`RIDE_USER_ID`) |
| `RIDE_PORT`, `RIDE_HOST` | default `8790`, `127.0.0.1` |
| `RIDE_STT_ROUTE` | `phone` (default: the phone talks to Scribe directly) or `server` (fallback: gated PCM relayed by the server) |
| `RIDE_STT_ENGINE` | `scribe` (default: realtime Scribe on both routes, as before) or `whistle` (offline Whistle on the Hermes host; forces the server route, no Scribe credits); any other value is `scribe` with a logged warning |
| `RIDE_STT_FALLBACK` | what a Whistle page switches to after a Whistle failure: `scribe` (default, and any value but `none`: the gated audio then goes through the host to ElevenLabs Scribe for the rest of that page load) or `none` (the page keeps hearing that recognition is down) |
| `RIDE_WHISTLE_PYTHON` | interpreter with the `needle` package for `tools/whistle_worker.py`; default: the first word of Hermes's `stt.providers.whistle.command` when that is a file, else `python3` |
| `RIDE_WHISTLE_WEIGHTS` | the `.cact` weights; default: `whistle.cact` next to that command's script when it exists, else empty (the `needle` package default) |
| `RIDE_WHISTLE_LANGUAGE` | language code handed to Whistle; default: `RIDE_LANGUAGE` (`en`), and `en` when that is empty or `auto` |
| `RIDE_WHISTLE_END_S` | Whistle: seconds of silence after the phone's voice-activity off edge before the phrase is transcribed, default `0.9` (0.3 to 3) |
| `RIDE_WHISTLE_TIMEOUT_S` | Whistle: seconds one phrase may take; past this the helper is killed and the phrase fails, default `20` (1 to 120) |
| `RIDE_WAKE_ENGINE` | `sherpa` (default; falls back to `transcript` if it can't load within 8 s) or `transcript` |
| `RIDE_WAKE_WORD` | the wake word, default `hey hermes`; also `hey lana` (any case and punctuation; unknown names fall back to the default). A new word needs a `ride_logic.WAKE_WORDS` entry and a keywords file from `tools/make_keywords.py` |
| `RIDE_LANGUAGE` | Scribe language, default `en` (English locked); empty or `auto` = auto-detect |
| `RIDE_LANGUAGES` | auto-detect hints (`secondary_languages`), default `en` (English only; other languages are not supported yet: CJK utterances are dropped by the word count) |
| `RIDE_KEYTERMS` | e.g. `Hermes`; off by default (Scribe keyterms cost +20 %) |
| `RIDE_WINDOW_S` | conversation window after an answer, default `10` (speech in progress keeps it open: VAD speech or a Scribe partial in the last 1.5 s) |
| `RIDE_PRICE_STT_PER_HOUR` / `RIDE_PRICE_TTS_PER_1K` | prices for the cost estimate, default `0.39` / `0.04` USD |
| `RIDE_END_OF_TURN_S` | Scribe VAD silence before a commit, default `1.2` |
| `RIDE_CUE_DIR` | your own cue folder (same file names as `static/cues/`: `wake_a`, `wake_b`, `thinking`, `done`, `reset`, `reset_failed` `.m4a`; a missing reset cue just plays nothing; `mute` and `unmute` are still in the server's cue map but the page no longer plays them) |
| `RIDE_MIC_BASE_DB` | how loud a sound must be (dBFS, at standstill; more is added with speed) before the mic gate counts it; default `-38` (was -45: "mic is too damn sensitive"), -70 to -20, higher = less sensitive |
| `RIDE_PLAY_BUFFER_MS` | the phone holds the first segment of each spoken reply this many ms before playing it, so the next segments are already queued and the joins do not gap; cues and clips never wait; default `250` (0 to 1000, `0` = off) |
| `RIDE_TONE_FIRST_S` | the soft "doo, doo" while the assistant thinks starts this many seconds after the thinking cue, then every 4 to 6 s; default `1.2` (0 to 30) |
| `RIDE_ORB_DESIGN` | the orb's look, default `simple` (the only design so far; unknown names fall back to it) |
| `RIDE_SHOW_WORD` | `true` shows the state word (sleep, listening, …) under the orb, for debugging; default off |
| `RIDE_DOUBLE_TAP_MS` | the wake double tap: the second tap must start within this many ms of the first, default `350` (150 to 1000) |
| `RIDE_CUE_GAIN` | loudness of cues and status clips relative to their files, `0`–`1`, default `0.5` (the reply voice is not affected; applied on the phone, no re-render) |
| `RIDE_SUMMARY_PROMPT` | `true` (default): a reset leaves a *Ride Summary?* yes/no question as the old session's last message; `false` = off |
| `RIDE_TTS_ENGINE` | `auto` (default), `hermes`, `direct` |
| `RIDE_VOICE_ID`, `RIDE_TTS_MODEL` | default: `tts.elevenlabs.*` from config.yaml, else ElevenLabs defaults |
| `RIDE_RIDER_NAME` | how the context line names you, default "the rider" |

Not a setting (yet): the phone gate's `voiced_ms` (300 ms of continuous voice before sleep opens a probe, WP3) is a
default in `static/gate.js`, not sent in the hello config.

The same keys work under `platforms.ride.extra` in `config.yaml` (lower-case names without `RIDE_`); env wins.

### HTTPS with Tailscale
```bash
tailscale serve --bg --https=8447 http://127.0.0.1:8790
```
On the phone open `https://<machine>.<tailnet>.ts.net:8447/` and type `RIDE_ACCESS_KEY` into the form once per
browser (it sets an HttpOnly, Secure, SameSite=Strict cookie for 30 days). Key links (`/?k=…`) are retired and refused:
a key in a URL ends up in chat history and proxy logs. Wrong keys are bounded: 5 per client, then a lock from 60 s
that doubles (max 1 h); 30 wrong keys in 15 min from all clients lock the form for everyone until they age out.
Use a random key of 24+ characters (at least 16 are enforced; for example `openssl rand -base64 24`); a key that
was ever sent as a link should be replaced.
Graph: `…:8447/graph` (newest ride) or `/graph?ride=<id>`. Don't use Tailscale **Funnel** unless you mean to.

### On the bike
Mount the phone, connect the headset, open the page and tap the pulsing orb **once** (audio unlock, motion
permission, cue preload, mic, GPS, link, wake engine). The screen goes black. Say "Hey Hermes" (or your `wake_word`) and you hear the wake answer. Calibration learns as
you ride (down within a minute, forward after the first accelerations and stops).

## Wiring your own voice and model
The principle (9 Nov 2026): Ride uses what your Hermes already has, and there is no provider choice in the
Ride page. **Today that is only partly true.** This section says what the code does now (read in `adapter.py`:
`connect()`, `resolve_engine()`, `core_config()`) and where a provider that is not built yet would plug in. Anything
marked *untested* has not been run with Ride.

### 1. Reply voice (TTS)
- **What Ride reads today**: `RIDE_TTS_ENGINE` (or `platforms.ride.extra.tts_engine`), default `auto`.
  - `auto` picks `hermes` only when Hermes's `tts.provider` is `elevenlabs` **and** the `elevenlabs` Python SDK can
    be imported; otherwise `direct`. So `auto` follows Hermes for ElevenLabs only.
  - `direct`: Ride calls ElevenLabs TTS itself with `ELEVENLABS_API_KEY`, voice `RIDE_VOICE_ID` (default
    `tts.elevenlabs.voice_id`), model `RIDE_TTS_MODEL` (default `tts.elevenlabs.streaming_model_id`, else
    `eleven_flash_v2_5`).
  - `hermes`: Ride hands each turn to the gateway as a voice message and plays what Hermes's own TTS produces: 16-bit
    PCM from the streaming TTS (`supports_streaming_tts`) or a finished file through `send_voice`.
- **To use another voice**: pick the provider in Hermes Desktop → Settings → Voice → Speech (or `hermes setup tts`),
  then set **`RIDE_TTS_ENGINE=hermes`**. *Untested* with Ride for any provider other than ElevenLabs: Hermes core
  streams TTS for ElevenLabs, Gemini, OpenAI and xAI (per the plan below), but whether each reaches the phone in time
  has not been checked.
- **Limits today**: if Hermes produced no audio for a turn, Ride's safety net speaks the reply text itself with
  ElevenLabs, so `ELEVENLABS_API_KEY` is still used. Cues and status clips are audio files, not TTS.
- **Plug-in point**: `RideCore.tts`, an `async (text, previous_text) -> (audio bytes, first-byte time)` callable,
  passed as `RideServer(..., tts=...)`; today's direct TTS is `RideCore.tts_elevenlabs`.

### 2. Speech-to-text (STT)
- **Two engines, one setting**: `RIDE_STT_ENGINE` (or `platforms.ride.extra.stt_engine`) is `scribe` (default) or
  `whistle`.
  - **`scribe` (default, unchanged)**: **ElevenLabs Scribe** v2 realtime. Routes: `phone` (default; the phone
    streams to Scribe with single-use tokens the server mints) or `server` (`RIDE_STT_ROUTE=server`; the server
    relays the gated audio). `ELEVENLABS_API_KEY` is required for this engine and still required at start
    (`connect()` stops with `ride_elevenlabs_key`; it is in `requires_env`) because the reply voice uses it too.
    Hermes's own `stt.providers` setting is not read by Ride — except as the *default path* for Whistle (below).
  - **`whistle` (round 3, 10 Nov)**: offline **Whistle** (Cactus, 17 MB, Apache-2.0) on **your Hermes host**, one
    finished phrase at a time, no realtime and no Scribe credits. The page is put on the `server` route
    (`hello.cfg.stt_route` is `server` whatever `RIDE_STT_ROUTE` says, and `hello.cfg.stt_engine` names the engine).
    - **Where the audio goes**: the gated PCM16 at 16 kHz (from the wake word, with its pre-roll, until the
      conversation window ends) goes from the phone to your Hermes host and is transcribed there by the local
      model. It goes **nowhere else**: not to ElevenLabs. The helper runs with the package's telemetry switched off,
      and with a weights file set (`RIDE_WHISTLE_WEIGHTS`) it fetches nothing. The one exception is the fallback below, after a Whistle failure.
    - **How a phrase is made**: `RideCore` buffers the audio of the current gate opening. The phrase ends when the
      phone's voice activity has been off for `RIDE_WHISTLE_END_S` (0.9 s); it is then written to a temporary WAV
      in a private (0700) scratch folder, the long-lived helper `tools/whistle_worker.py` (run by
      `RIDE_WHISTLE_PYTHON`, the model loaded once, at the first page load) transcribes it, the WAV is deleted, and
      the text takes the same path as a Scribe commit (wake-word strip, filters, barge-in, end of turn). A buffer
      with no speech in it (silence, road noise under the gate) is never sent to the model.
    - **Limits**: there is **no partial text**. Interrupting a reply therefore waits until your phrase has ended
      and been transcribed (with Scribe it happens while you speak); and with no partials to confirm speech, the
      phone's voice activity is trusted for at most 15 s per run. One phrase is at most **30 s** of audio for the
      model: a longer one is cut at 28 s and the rest becomes the next phrase. The transcription time is added to
      every turn.
    - **Latency, measured** on the development host (a 4-core VPS that was doing other work, model kept loaded;
      test fixtures; several runs on 10 Nov): helper start 0.4–0.9 s; a 4.4 s phrase 0.5–4.4 s; a 2.9 s phrase
      0.2–6.1 s; a 9.4 s phrase 1.8–14.2 s. The slow end is the host being busy (load average up to 5.3): Whistle can take longer than the phrase is long, and `RIDE_WHISTLE_TIMEOUT_S` (20 s) is the guard.
      Not measured: a phone on a mobile network, and accuracy with a helmet microphone at speed.
    - **Texts it produced** for the fixtures (the same in every run): `"Hey, Hermes, how fast am I going?"`,
      `Hermes searched the web for the weather forecast in bright to morrow.` (the fixture says "in Bright tomorrow"),
      `HELANA. This is a test from the bike. What is a good place to stop for coffee in about half an hour?` and
      `"Hey lana! This is a test from the bike. ...`. So `rl.whistle_text()` drops Whistle's quotation marks before the wake
      strip, and `helana` is one of the `hey lana` spellings in `ride_logic.WAKE_WORDS`; the wake word is stripped
      at the start of an utterance only.
    - **Log**: each phrase is an `stt` event in `turns.jsonl` (`engine`, `audio_s`, `whistle_ms`, `words`, `cut`);
      turn records and the `hello` event carry `stt_engine`. No Scribe seconds are counted for Whistle audio.
  - **Failure**: a Whistle error (helper cannot start, a timeout, two failures in a row) is logged as `stt_error`
    code `whistle_failed` (turns.jsonl) and the page hears `{t:'stt', s:'failed', why:'whistle'}` (or
    `why:'whistle_start'`). With `RIDE_STT_FALLBACK=scribe` (default) that page's server route switches to the
    existing Scribe relay for the rest of the page load (one `stt_fallback` event): from then on its gated audio
    **does** go through your host to ElevenLabs. With `none` it keeps hearing `failed` and the page's own clip
    says recognition is down. A reload starts on Whistle again.
- **Not built**: **Apple native** (recognition on the phone): a phone-side engine next to `static/stt.js` would
  send the same `partial` / `committed` messages, so the server needs no change. It needs a speech-recognition API
  that iOS Brave/Bluefy/Safari expose to a web page and that keeps working with the screen black or locked (not
  checked).
- **The ElevenLabs credential with Whistle**: `ELEVENLABS_API_KEY` is still required at start (`connect()` checks
  it before anything else is built) and is still used: for the reply voice (direct TTS, or the safety net when
  Hermes's own TTS produced no audio) and for the Scribe fallback. It has not become optional.

### 3. The language model
- Ride follows the Hermes profile's default model and reasoning, like any gateway chat. There is no model setting
  for the Ride lane.
- A `/model` (or `/reasoning`) switch made in Conduit or Hermes Desktop lives in that client and does not reach the
  gateway's Ride lane.
- The later provider track: notes kept with the project itself, not shipped in this repository.

## Wake engines and licences
- **`sherpa`** (default): [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) open-vocabulary keyword spotting
  in WebAssembly, in a Web Worker on the phone. No training: the phrase is written as the model's own phone tokens
  (`HH EY1 HH ER1 M IY0 Z @HEY_HERMES`, one file per wake word, `static/vendor/kws-words-<wake word>.txt`,
  generated from the model lexicon by `tools/make_keywords.py`; the plain name has a stricter threshold). Checked
  under node with the real build and model: the Hermes file fires on "Hey Hermes" (stock voice, clean and with
  -30 dBFS brown noise) and on none of the other fixtures; plain "Hermes," alone did not fire (measured down to a
  0.2 threshold), so say the two-word phrase. The Lana file fires on all five "Hey Lana" fixtures and on none of the
  negatives or the Hermes fixtures.
  - Runtime: sherpa-onnx `wasm/kws` build, **Apache-2.0**. Upstream no longer publishes a prebuilt KWS web build;
    the pinned copy is the one hosted in the Hugging Face space `yuiyide/sherpa-oon-kws` (commit `6a6f337…`, a
    third-party upload). To use your own build: `./build-wasm-simd-kws.sh` in sherpa-onnx, copy the three files
    into `static/vendor/` and run the fetch script with `SHERPA_SKIP_RUNTIME=1`. (The `@sherpaw/kws` npm package
    is another Apache-2.0 option, not used here.)
  - Model: `sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20` (k2-fsa release `kws-models`), **Apache-2.0** as
    declared by the original model card (ModelScope `pkufool/icefall-kws-zipformer-zh-en-3M-2025-12-20`). The
    release archive itself carries no licence file.
- **`transcript`** (always available, the fallback): your speech (VAD-gated) opens Scribe and the transcript must
  start with the wake word ("Hey Hermes" / "Hermes", also "Hermès", "Hermez", "Hermies"; each word's spellings are in
  `ride_logic.WAKE_WORDS`); otherwise it is dropped on the phone
  and only a word count is logged. Costs Scribe time on bystander speech.
- **openWakeWord** (not built): a custom wake-word model from openwakeword.com would need credits, and those models
  are personal/non-commercial by default.

## Cues
`static/cues/`: our own render of the first bar of Peter's theme from Prokofiev's *Peter and the Wolf* (a nod to
its use in *Blade Runner 2049*; no audio from the film), VSCO-2 CE samples (CC0), spoken words by ElevenLabs.
Full credits in `static/cues/CREDITS.txt`. Replace them with `RIDE_CUE_DIR`. Status clips (`static/clips/`) were
rendered once with `tools/render_status_clips.py`.

## Disclosures (catalog rule 13)
- **Audio goes from the phone to ElevenLabs directly** (`api.elevenlabs.io`, default `stt_route=phone`), and only
  while the gate is open: from the wake word (with 1.5 s before it) until the 10 s conversation window ends. While
  asleep nothing is sent.
- **Mic state and MUTE**: the page tracks whether audio can leave the phone (gate or Scribe session open), the mic
  is captured for the on-phone wake word only (nothing leaves), or it is muted; the on-screen dot is hidden on the
  mount. MUTE (one tap, in the reset reveal) and the lock-screen / Control Centre pause button (Media Session) mute
  the page: gate asleep, Scribe closed, nothing sent; UNMUTE or play resumes without a reload. On iOS the system's own orange mic dot is the final word; whether the lock-screen button reaches the page
  depends on the browser (see HANDOFF, R3-6). The server mints single-use tokens with your key and never sees the audio. Scribe is
  billed per streamed time, **$0.39/hour** (https://elevenlabs.io/pricing/api, read 2026-11-08). With
  `stt_route=server` the gated audio passes through your server to the same Scribe API.
- **With `stt_engine: whistle`** the audio does not go to ElevenLabs: the gated audio (same span: from the wake
  word, with 1.5 s before it, until the conversation window ends) goes from the phone to **your own Hermes host**
  and is transcribed there, offline; it goes nowhere else. Each phrase exists as a temporary WAV file under
  `<hermes home>/plugin-data/hermes-ride/whistle/` (folder mode 0700) only while it is transcribed, then it is
  deleted. Exception: after a Whistle failure, with `stt_fallback: scribe` (the default), that page's gated audio
  is relayed by your host to ElevenLabs Scribe until the page is reloaded; `stt_fallback: none` rules that out.
  ElevenLabs is still used for the reply voice (next point), and `ELEVENLABS_API_KEY` is still required at start.
- **Reply text** goes to ElevenLabs TTS (Flash/Turbo **$0.04 per 1K characters**, same page) unless Hermes's own
  TTS speaks it.
- **The server sees**: your transcribed words, VAD edges (speech on/off times), telemetry (1 Hz records with
  position at 6 decimals, speed, heading, calibrated lean and G, events), per-minute summaries, and usage numbers.
- **Location and motion data go to your model provider**: each turn's context line contains current speed,
  heading, position (4 decimals, ~10 m), lean, acceleration, the "since last turn" summary and the ride-log path;
  it is stored in the session history like any message. The agent can read the full ride log with its tools.
- **Network listener**: HTTP + WebSocket on `RIDE_HOST:RIDE_PORT` (default `127.0.0.1:8790`) for the page, its
  assets and `/graph`, all behind `RIDE_ACCESS_KEY` (typed into the key form once per browser, then a cookie). Anyone
  with the key can talk to your agent as `rider` and reset its Ride conversation: treat it like a password.
- **Hooks**: `pre_llm_call`, `pre_tool_call`, `post_tool_call`, `post_api_request`. They act only for ride sessions,
  read only the tool **name** and token counts (never tool arguments or results), and only send state frames.
- **Reads outside its own data**: Hermes's `config.yaml` via `hermes_cli.config.load_config` (`tts.*`,
  `approvals.timeout`, and `stt.providers.whistle.command` for the default Whistle interpreter and weights paths
  only); audio files Hermes hands to `send_voice`.
- **Writes** to the session history: at a reset, one *Ride Summary?* assistant row in the old Ride session (the
  gateway's own transcript store). Its *Yes* button sends a prompt that asks that session's agent to run
  `tools/ride_character.py` over the ride logs (read-only) and to answer in chat; the ride's conversation and log
  then go to your model provider like any other turn of that session.
- **Writes** under `<hermes home>/plugin-data/hermes-ride/`: `summaries/prompt-<session>.html` (the yes/no
  widget), `whistle/` (Whistle only: one temporary WAV per phrase, deleted after it), `logs/turns.jsonl` (turns, transcripts, dropped
  utterances, usage, ride summaries; rotated 5 MB × 3) and `rides/<ride>.jsonl` (the 1 Hz ride logs; not rotated —
  delete old rides yourself). The phone keeps its own copy of each ride in IndexedDB.
- **Phone-side downloads**: the page loads only from your server (cues, clips, the wake engine and model in
  `static/vendor/`). `tools/fetch_assets.sh` is the only thing that downloads from the internet, run by you.
- **Credentials**: none stored; keys are read through Hermes's profile-scoped secrets. Every log line, turn-log
  record, ride-log record and live-test dump passes a recursive redactor: the keys it has read (and any 8+
  character piece of them), `sutkn_…` tokens, e-mail addresses and any 24+ character key-like run except a fixed
  list of the plugin's own labels; provider error text is redacted before it is shortened. Phone-supplied labels
  are reduced to known values. This is pattern-based: it is a safety net, not a proof that no secret format can
  ever slip through.
- No shell commands at runtime. One subprocess, only with `stt_engine: whistle`: the long-lived helper
  `tools/whistle_worker.py`, started without a shell with the interpreter you set, fed file paths on stdin. No
  self-update, no telemetry to anyone but your own server.
  Approvals are never auto-approved and never answerable by voice (rule 12).

## Known limits
- iOS WebKit with the screen black: WASM/AudioWorklet throttling, mic and motion re-prompts (Brave) are
  **untested** on a real phone. The transcript engine is the fallback if the spotter misses.
- Energy VAD + speed threshold is simple; on a very noisy road the VAD may stay "on" — with Scribe the server
  then trusts it only when Scribe confirms speech with partials; with Whistle (no partials) it trusts the VAD for
  at most 15 s per speech run.
- `stt_engine: whistle`: no partial text, so interrupting a reply waits until your phrase has ended and been
  transcribed; at most 30 s of audio per phrase (cut at 28 s); the transcription time is added to every turn:
  0.5–4.4 s for a 4.4 s phrase and 1.8–14.2 s for a 9.4 s phrase, measured on a busy 4-core host (see *Wiring*,
  speech-to-text). `RIDE_WHISTLE_TIMEOUT_S` (20 s) is the guard. **Untested**: a real phone, the upload of the gated
  PCM over a mobile network, and accuracy with a helmet microphone at speed.
- Ride Summary: the *Yes* click and the reply in Hermes Desktop, and the summary on a real ride, are **untested**.
- One phone page at a time per gateway profile; the newest page wins. Not tested inside a multiplexed gateway as a
  secondary profile.
- Language detection: Scribe's reported language is forwarded but not relied on (it mislabelled English as `zh`
  in a probe when `en` wasn't in the hint list).

## Development
```bash
python3 -m unittest discover -s tests   # 307 tests: core, protocol, turns, telemetry, adapter (imports Hermes from
                                        # $HERMES_ROOT, default /opt/hermes), whistle (the real-model tests skip
                                        # without a Whistle install), character, docs, and the node page tests
                                        # (tests/*_test.js, 22 suites, each also runnable as node tests/<name>_test.js)
sh tools/fetch_assets.sh             # lets tests/wake_test.js run the real spotter on the fixtures
hermes plugins validate .
RIDE_ACCESS_KEY=... python3 tests/live_phone.py --url http://127.0.0.1:8790 --stt phone \
    --wav tests/fixtures/sensor_road.wav --telemetry          # live fake phone (spends Scribe/TTS/model time)
```

## What this repository does not ship
The material that only makes sense on the author's own machine is left out of this repository, and the tests that
need it skip cleanly when it is absent:

- `HANDOFF.md` and `docs/evidence/` (build logs, ride transcripts, phone screenshots),
- `tests/live_phone.py` (drives a phone over a private network).

Everything else here is the plugin as it runs: `static/`, the Python core, the tests and the docs.

## License
MIT, see `LICENSE`. Vendored-at-install assets keep their own licences (Apache-2.0, above).
