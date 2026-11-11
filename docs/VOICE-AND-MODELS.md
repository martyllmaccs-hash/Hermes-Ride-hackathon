# Voice, speech-to-text and model wiring (technical)

For people installing Hermes-Ride on their own Hermes, and for a Hermes agent asked to set it up or answer "which
voice / model / engine is Ride using?". Every statement names the file it comes from. Defaults are the shipped
values. Where something has not been tested with Ride, this file says so.

## TL;DR
| Layer | Default | Where it comes from | Swap it |
|---|---|---|---|
| Language model | Your Hermes profile default | The gateway, like any chat (`adapter.py` sets no model) | Change the Hermes default model. `/model` in Desktop or Conduit does **not** reach the Ride lane. |
| Reply voice (TTS) | ElevenLabs `eleven_flash_v2_5`, your `tts.elevenlabs.voice_id` | `adapter.core_config()` | `RIDE_VOICE_ID`, `RIDE_TTS_MODEL`, or `RIDE_TTS_ENGINE=hermes` for Hermes's own TTS |
| Speech-to-text | ElevenLabs Scribe `scribe_v2_realtime`, phone direct | `ride_core.DEFAULTS` | `RIDE_STT_ENGINE=whistle` (offline, on your host) |
| Wake word | "Hey Hermes", sherpa-onnx on the phone | `ride_logic.WAKE_WORDS`, `static/wake.js` | `RIDE_WAKE_WORD` |
| Required secrets | `RIDE_ACCESS_KEY`, `ELEVENLABS_API_KEY` | `adapter.connect()`, `required_env` | The ElevenLabs key is required today, even with Whistle and Hermes TTS |

Every setting can be an env var (`RIDE_*`, profile-scoped, which wins) or `platforms.ride.extra.<name>` in
`config.yaml` (lower-case, without `RIDE_`). The full table is in the README, under *Install*.

## 1. Language model
- Ride hands every utterance to the gateway as a normal message (`adapter._submit` → `handle_message`). The agent
  that answers is the profile's default model and reasoning, with its memory, tools, skills and delegation, the same
  as Desktop or Telegram.
- No model, provider or reasoning setting exists for the Ride lane in this repository. A `/model` switch made in
  another client is per-client and is not written to the gateway session that Ride uses.
- To check what really ran, look for the Ride turns in the agent log. They carry `platform=ride` with `model=` and
  `provider=`. A turn from before the last gateway restart proves nothing about the current setup.

## 2. Reply voice (TTS)
`RIDE_TTS_ENGINE` (`tts_engine`): `auto` (default), `direct` or `hermes`.

| Engine | What happens | Uses |
|---|---|---|
| `direct` | Ride calls the ElevenLabs streaming TTS itself, per clause (`RideCore.tts_elevenlabs`, output `mp3_22050_32`) | `ELEVENLABS_API_KEY`, `voice_id`, `tts_model_id` |
| `hermes` | The turn goes in as a voice message. The phone plays what Hermes's own TTS produces: PCM from the streaming-TTS contract (`supports_streaming_tts`, any sample rate, 16-bit) or a finished file through `send_voice` | Hermes's `tts:` config |
| `auto` | `hermes` only when `tts.provider` is `elevenlabs` **and** the `elevenlabs` SDK imports, else `direct` (`adapter.resolve_engine`) | |

- **Voice and model defaults** (`adapter.core_config`): `voice_id` = `RIDE_VOICE_ID`, else `tts.elevenlabs.voice_id`,
  else ElevenLabs' stock premade voice `JBFqnCBsd6RMkjVDRZzb`. `tts_model_id` = `RIDE_TTS_MODEL`, else
  `tts.elevenlabs.streaming_model_id`, else `eleven_flash_v2_5`. Ride does **not** read `tts.elevenlabs.model_id`;
  set `streaming_model_id`.
- **Recommended**: `eleven_flash_v2_5`, the model this was ridden and tuned on. It gives the fastest first audio and
  the lowest per-character price, which matter more on a bike than the extra polish of a slower model. Pick any
  voice from your ElevenLabs library and put its id in `tts.elevenlabs.voice_id`.
- **Another provider (OpenAI, Gemini, xAI, a plugin TTS)**: set it up in Hermes (Desktop → Settings → Voice, or
  `hermes setup tts`), then set `RIDE_TTS_ENGINE=hermes` explicitly, because `auto` will not choose it. Hermes's
  streaming layer (`tools/tts_streaming.py` in Hermes) has streamers for ElevenLabs, OpenAI, Gemini, xAI and plugin
  PCM providers. **Untested with Ride** for anything but ElevenLabs: latency to the phone has not been measured.
- **Safety net**: on the `hermes` engine, a turn for which Hermes produced no audio is spoken by Ride itself through
  ElevenLabs. This is one reason the key is still required.
- **Not TTS**: the wake answer, thinking and done cues and the six status clips are audio files in `static/cues/`
  and `static/clips/`, played on the phone. They do not change with your voice. Use your own with `RIDE_CUE_DIR`, or
  re-render the clips with `tools/render_status_clips.py --voice <id>`.
- **Plug-in point** for a new direct engine: `RideCore.tts`, an
  `async (text, previous_text) -> (audio bytes, first-byte time)` callable passed to `RideServer(..., tts=...)`.

## 3. Speech-to-text
`RIDE_STT_ENGINE` (`stt_engine`): `scribe` (default) or `whistle`. Any other value is treated as `scribe`, with a
logged warning.

### Scribe (default)
- ElevenLabs Scribe v2 realtime (`stt_model_id` `scribe_v2_realtime`, `commit_strategy: vad`).
- With `RIDE_STT_ROUTE=phone` (default), the phone streams to ElevenLabs directly with 15-minute, single-use tokens
  that the server mints. Your server never receives audio.
- With `RIDE_STT_ROUTE=server`, the gated audio is relayed through your host.
- Billed by streamed time. Audio is sent only while the gate is open: from the wake word (with 1.5 s of pre-roll)
  until the 10 s conversation window ends.
- `RIDE_LANGUAGE` defaults to `en`. Empty or `auto` means auto-detect.

### Whistle (offline, on your Hermes host)
- Cactus Compute's 17 MB offline model. This is **not** OpenAI Whisper. It transcribes one finished phrase at a time
  in `tools/whistle_worker.py`, a long-lived helper driven by `ride_whistle.WhistleEngine`.
- The page is forced onto the server route. Audio goes from the phone to your host and nowhere else, and no Scribe
  credits are used.
- **You install it; the plugin does not.** You need a Python with the `needle` package and the `.cact` weights:
  - `RIDE_WHISTLE_PYTHON`: that interpreter. Default: the first word of Hermes's `stt.providers.whistle.command`
    when that is a file, else `python3`.
  - `RIDE_WHISTLE_WEIGHTS`: the weights file. Default: `whistle.cact` next to that command's script, else the
    package default.
- **Limits**: no partial text, so an interruption waits until your phrase is transcribed. A phrase is at most about
  30 s. The transcription time is added to every turn. Measured on a busy 4-core VPS: a 4.4 s phrase took
  0.5–4.4 s, a 9.4 s phrase 1.8–14.2 s.
- **Failure**: covers a helper that cannot start, a timeout, or a second failure. It is logged as `whistle_failed`.
  With `RIDE_STT_FALLBACK=scribe` (default) that page load switches to Scribe through your host. With `none`, the
  page says recognition is down. A reload starts on Whistle again.
- **Check it before riding**: start the helper with your interpreter and weights, and pipe one JSON line
  `{"id":1,"wav":"<16 kHz mono wav>","language":"en"}` to `tools/whistle_worker.py --weights <file>`. A first line of
  `{"ready": true}` and a `text` reply mean it works. Without `needle` you get `ModuleNotFoundError: No module named
  'needle'`, and Ride falls back as above.

## 4. Wake word
- `RIDE_WAKE_WORD`: `hey hermes` (default) or `hey lana`. Unknown names fall back to the default.
- The default engine, `sherpa`, is open-vocabulary keyword spotting in WebAssembly on the phone. While asleep
  nothing leaves the phone. It needs the assets from `sh tools/fetch_assets.sh`: a local official-source build using
  Emscripten 4.0.23 and the official checksum-pinned model (see README build prerequisites).
- Without those assets, or if sherpa fails to load within 8 s, the page uses the `transcript` engine. That engine
  sends speech-like sound to Scribe while asleep, so it can look for the wake word. Run the fetch script.
- A new wake word needs an entry in `ride_logic.WAKE_WORDS` plus a keywords file from `tools/make_keywords.py`.

## 5. Costs (estimates, set your own prices)
- The per-turn and per-ride usage log is in `turns.jsonl` under the plugin data dir. It records Scribe seconds, TTS
  characters, LLM tokens and an estimated cost.
- The prices are settings: `RIDE_PRICE_STT_PER_HOUR` (default `0.39` USD) and `RIDE_PRICE_TTS_PER_1K` (`0.04`). These
  are ElevenLabs pay-as-you-go API list prices, not what a subscription plan costs. Check current pricing for your
  plan.
- Whistle audio costs nothing in STT. TTS and the LLM are billed as usual.

## 6. For an agent setting this up: a checklist
1. `hermes plugins install <owner>/<repo> --enable`, then `sh tools/fetch_assets.sh` in the plugin folder.
2. Put the secrets in the profile's env or secret manager: `RIDE_ACCESS_KEY` (24+ random characters),
   `ELEVENLABS_API_KEY`, `RIDE_ALLOWED_USERS=rider`. Never write the values into chat or files in the repo.
3. Voice: set `tts.elevenlabs.voice_id` and `streaming_model_id: eleven_flash_v2_5` in `config.yaml`, or the
   `RIDE_VOICE_ID` / `RIDE_TTS_MODEL` overrides.
4. Optional offline STT: `RIDE_STT_ENGINE=whistle`, plus `RIDE_WHISTLE_PYTHON` and `RIDE_WHISTLE_WEIGHTS` if Hermes's
   `stt.providers.whistle` does not already point at them.
5. Restart that profile's gateway. Then `curl -s 127.0.0.1:8790/healthz` should return 200, and the gateway log
   shows `ride: phone page on http://... (tts engine <engine>, voice <id>)`.
6. Serve the page over HTTPS (for example `tailscale serve --bg --https=8447 http://127.0.0.1:8790`) and hand the
   rider `docs/MANUAL.md`.
7. After the first ride, confirm from the logs, not from the settings. In `turns.jsonl`, `hello` events carry
   `stt_engine`, `wake` events carry `engine`, and turn records carry `engine` and `route`. Agent-log turns with
   `platform=ride` show the model that served them.
