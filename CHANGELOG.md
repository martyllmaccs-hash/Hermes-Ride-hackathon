# Changelog

## Unreleased

### Changed
- Increased the microphone VAD's default silence hangover from **600 ms to 2000 ms** in both the browser gate and server-provided configuration. Riders can pause longer between words before the phone declares the utterance ended. This is a shared/public default, not a hidden per-install adjustment.
- Exposed `platforms.ride.extra.vad_hangover_ms` (`RIDE_VAD_HANGOVER_MS`), range 0–5000 ms, so installers can tune or revert the hangover. The default and invalid-value fallback are 2000 ms; setting 600 restores the previous timing. Gateway restart and phone reload are required for a changed configuration.
- With Whistle's unchanged 0.9-second server phrase-end wait, the nominal silence-to-transcription delay is now approximately **2.9 seconds**, rather than 1.5 seconds. This favors allowing thinking pauses over fastest response time. It does not claim to fix microphone cutouts or false transcriptions.

### Added
- `platforms.ride.extra.min_phrase_s` (`RIDE_MIN_PHRASE_S`, 0–10 s, default **0 = off**): with the Whistle engine, a phrase whose whole clip is shorter than this is dropped before transcription (`dropped` / `too_short_audio` in the turn log). Meant for suppressing phantom short transcripts; it also drops genuine short commands. Off by default in the public tree; unproven as a fix until ridden.

### Scope
- The public wake-word default remains **Hey Hermes**. An install may select **Hey Lana** with the existing `wake_word` setting; the personal selection is configuration, not a change to the public wake phrase.
- No new high/low-pass filter is included, and the VAD hangover change itself adds no clip-duration rejection (see `min_phrase_s`, off by default). The separately identified Whistle resumed-speech end-timer issue is not fixed by this setting change.
