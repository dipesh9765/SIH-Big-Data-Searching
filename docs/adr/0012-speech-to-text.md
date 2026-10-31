# 12. Speech-to-text: faster-whisper, timestamped units, a separate worker pool

Status: accepted (scale-out work, `feature/scale-v2`)

## Decision
- **Engine:** faster-whisper (CTranslate2, int8, CPU) with the model baked into the worker image so workers run offline (`HF_HUB_OFFLINE=1`). Audio and video go through the same code: PyAV (FFmpeg libraries) decodes any container, so no ffmpeg binary is needed at run time.
- **Own pool:** uploads of audio/video are queued as `kind='asr'`, one job per worker, long lease (15 min) renewed while segments are produced (throttled to a quarter of the lease). Transcription is hours of CPU for long recordings; it must not share a queue with millisecond text jobs.
- **Units, not one big document:** recognised segments are merged into units of about 400 characters / at most 60 s. Each unit becomes its own `documents` row with `asset_id`, `start_ms`, `end_ms`, `unit_kind='transcript'`, so a hit says *where* in the recording the words are spoken. `GET /v1/assets/{id}/file` serves the original with HTTP `Range`, so a player can seek there. Units carry a dedupe key that includes the asset and offset: two recordings that both say "thank you" must stay two separate hits.
- **Before spending model time:** the file must have an audio stream and a duration within `DOCSEARCH_ASR_MAX_MINUTES` (240); a wall-clock deadline (8x the audio length, at least 5 min) stops a transcription that is not converging.
- **Silence and noise:** voice-activity filtering is on, and segments Whisper itself scores as "not speech" (no-speech probability > 0.6 and mean log-probability < -1) are dropped. Without this Whisper invents stock phrases over silence and music. A test confirms 20 s of silence yields no text.
- **Large uploads:** streamed to a scratch file in 1 MiB chunks while hashing (never held in memory), separate size limit for media (`DOCSEARCH_MAX_MEDIA_MB`, 500).

## Model choice (measured, synthetic speech only)
Test speech was synthesised by eSpeak NG (robotic voices), so these numbers say how the models rank, not what accuracy real recordings will get.
| | base (142 MB) | small (~460 MB) |
|---|---|---|
| English, 3 sentences | "color bleaching"; "Volcano-curup shouldn't release Tosh" | "coral bleaching"; "Volcanic eruptions release Tash" |
| Hindi / Marathi, language auto-detected | detected as la/th/en/hu/ro; romanised or foreign-script garbage | same failure (en, hu, ro) |
| Hindi / Marathi, language forced (`DOCSEARCH_ASR_LANGUAGE=hi`/`mr`) | romanised garbage, or nothing | Devanagari words, partly right ("गले शियर" for "ग्लेशियर") |
| Speed on this laptop, in Docker Desktop | about 0.5-1x real time | about 2x slower than real time |
`small` is the default. Hindi and Marathi need the language forced and still come out poorly on this voice; do not promise them without testing on real recordings (or a larger model: `medium`/`large-v3`, not tried because the download is several GB).

## Found by running it
PyAV 19.0.2 (released days before) breaks faster-whisper 1.2.1 (`av.open(metadata_errors=...)` was removed), so `av` is pinned to 18.1.0 together with the other speech packages in `requirements-asr.txt`.

## Limits
- No speaker separation, punctuation restoration beyond what the model does, or word-level times (unit granularity only).
- A 1-hour recording takes about 2 hours of one worker with `small`; throughput scales by adding workers.
- Whisper can still mis-hear; there is no per-unit confidence stored.
