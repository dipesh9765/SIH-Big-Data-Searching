"""Speech-to-text for audio and video (faster-whisper, on CPU).

Runs in dedicated `asr` workers. The result is a list of *units*: consecutive recognised segments
grouped into stretches of roughly 30-60 seconds, each with its start and end time. Each unit is
indexed as its own document, so a search hit says *where* in the recording the words are spoken and
the player can seek there.

Protections, because the input is an untrusted media file:
* the container is probed first: it must have an audio stream, and its duration must be within
  `max_minutes`; the check happens before any model time is spent;
* a wall-clock deadline aborts a transcription that runs far longer than the audio should need;
* segments Whisper itself judges to be silence or noise are dropped (it otherwise invents
  stock phrases such as "thank you for watching" over silence and music).
"""

from __future__ import annotations

import functools
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docsearch.extract import ExtractionError, Unit

# Whisper's own convention for "this segment is not speech": high no-speech probability together
# with a low average log-probability. Both must hold, so quiet but real speech is kept.
NO_SPEECH_PROB = 0.6
LOW_LOGPROB = -1.0


@dataclass(frozen=True, slots=True)
class AsrConfig:
    model: str = "small"  # tiny / base / small / medium / large-v3 ... or a path
    model_dir: str = ""  # where model files live (empty: the library's default cache)
    language: str = ""  # ISO code such as "hi", "mr", "en"; empty: detect from the audio
    max_minutes: int = 240
    unit_chars: int = 400  # start a new unit once this much text has accumulated
    unit_max_ms: int = 60_000  # ... or once a unit spans this long
    deadline_factor: float = 8.0  # abort if processing exceeds this multiple of the audio length
    deadline_floor_seconds: float = 300.0  # ... but always allow at least this long
    compute_type: str = "int8"
    beam_size: int = 1
    cpu_threads: int = 0  # 0: library default


@dataclass(frozen=True, slots=True)
class Transcript:
    units: list[Unit]
    language: str
    language_probability: float
    duration_ms: int


@functools.cache
def _load_model(model: str, model_dir: str, compute_type: str, cpu_threads: int) -> Any:
    from faster_whisper import WhisperModel  # noqa: PLC0415 - heavy import, only in ASR workers

    return WhisperModel(
        model,
        device="cpu",
        compute_type=compute_type,
        cpu_threads=cpu_threads,
        download_root=model_dir or None,
    )


_model_lock = threading.Lock()


def get_model(config: AsrConfig) -> Any:
    with _model_lock:  # one load at a time; the loaded model is shared by this process
        return _load_model(config.model, config.model_dir, config.compute_type, config.cpu_threads)


def check_ready(config: AsrConfig) -> None:
    """Raise `RuntimeError` unless faster-whisper and the model can be loaded.

    Called when an ASR worker starts, so a misconfigured worker refuses to run instead of failing
    every job it claims.
    """
    try:
        import av  # noqa: F401, PLC0415
        import faster_whisper  # noqa: F401, PLC0415
    except ImportError as exc:
        raise RuntimeError(f"speech recognition packages are not installed: {exc}") from exc
    try:
        get_model(config)
    except Exception as exc:
        raise RuntimeError(f"cannot load speech model {config.model!r}: {exc}") from exc


def probe(path: Path, max_minutes: int) -> int:
    """Duration in milliseconds, after checking the file has audio and is not too long."""
    import av  # noqa: PLC0415

    try:
        with av.open(str(path)) as container:
            if not container.streams.audio:
                raise ExtractionError("the file has no audio track")
            seconds = (container.duration or 0) / 1_000_000  # av.time_base is microseconds
            if seconds <= 0:
                stream = container.streams.audio[0]
                seconds = float(stream.duration * stream.time_base) if stream.duration else 0.0
    except av.error.FFmpegError as exc:
        raise ExtractionError("not a readable audio or video file") from exc
    if seconds > max_minutes * 60:
        raise ExtractionError(f"the recording is longer than {max_minutes} minutes")
    return int(seconds * 1000)


def group_units(segments: list[tuple[float, float, str]], chars: int, max_ms: int) -> list[Unit]:
    """Merge consecutive (start s, end s, text) segments into units of about `chars` characters,
    never spanning more than `max_ms`, so each unit is a sensible unit of search and playback."""
    units: list[Unit] = []
    texts: list[str] = []
    start = end = 0.0
    size = 0
    for seg_start, seg_end, text in segments:
        if texts and (size >= chars or (seg_end - start) * 1000 > max_ms):
            units.append(Unit(" ".join(texts), int(start * 1000), int(end * 1000)))
            texts, size = [], 0
        if not texts:
            start = seg_start
        texts.append(text)
        size += len(text)
        end = seg_end
    if texts:
        units.append(Unit(" ".join(texts), int(start * 1000), int(end * 1000)))
    return units


def transcribe(
    path: Path,
    config: AsrConfig | None = None,
    on_progress: Callable[[float, float], None] | None = None,
) -> Transcript:
    """Transcribe the audio of `path` (any audio or video container FFmpeg can read).

    `on_progress(seconds_done, seconds_total)` is called per recognised segment, so a worker can
    renew its job lease during a long recording.
    """
    config = config or AsrConfig()
    duration_ms = probe(path, config.max_minutes)
    model = get_model(config)
    deadline = time.monotonic() + max(
        config.deadline_floor_seconds, config.deadline_factor * duration_ms / 1000
    )
    import av  # noqa: PLC0415

    kept: list[tuple[float, float, str]] = []
    try:
        segments, info = model.transcribe(
            str(path),
            language=config.language or None,
            beam_size=config.beam_size,
            vad_filter=True,
            condition_on_previous_text=False,  # stops one bad segment poisoning the rest
        )
        total = duration_ms / 1000 or float(getattr(info, "duration", 0.0))
        for segment in segments:
            if time.monotonic() > deadline:
                raise ExtractionError("transcription took too long and was stopped")
            text = segment.text.strip()
            silent = segment.no_speech_prob > NO_SPEECH_PROB and segment.avg_logprob < LOW_LOGPROB
            if text and not silent:
                kept.append((segment.start, segment.end, text))
            if on_progress is not None:
                on_progress(segment.end, total)
    except av.error.FFmpegError as exc:
        raise ExtractionError("not a readable audio or video file") from exc
    units = group_units(kept, config.unit_chars, config.unit_max_ms)
    return Transcript(units, info.language, float(info.language_probability), duration_ms)
