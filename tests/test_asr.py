"""Speech-to-text. Grouping and pipeline behaviour run anywhere (with a stand-in transcriber);
recognition tests need faster-whisper, its model and espeak-ng/ffmpeg to make test speech, all of
which the worker image provides (deploy/Dockerfile.worker, target `test`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from docsearch.asr import AsrConfig, Transcript, group_units, probe, transcribe
from docsearch.engine.pg import PgIndex
from docsearch.extract import ExtractionError, Unit
from docsearch.ingest.objects import MemoryStore
from docsearch.ingest.pipeline import submit
from docsearch.ingest.queue import JobQueue
from docsearch.ingest.worker import Worker

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get(
    "DOCSEARCH_TEST_DSN", "postgresql://docsearch:docsearch_dev@127.0.0.1:5433/docsearch"
)


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


needs_db = pytest.mark.skipif(not _reachable(), reason="no PostgreSQL at DOCSEARCH_TEST_DSN")


def _asr_ready() -> bool:
    try:
        import av  # noqa: F401
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return shutil.which("espeak-ng") is not None and shutil.which("ffmpeg") is not None


needs_asr = pytest.mark.skipif(
    not _asr_ready(), reason="faster-whisper / espeak-ng / ffmpeg missing"
)


# ------------------------------------------------------------------------------- grouping


def test_segments_are_grouped_into_units_by_size() -> None:
    segments = [(i * 5.0, i * 5.0 + 4.0, "x" * 150) for i in range(6)]  # 6 segments of 150 chars
    units = group_units(segments, chars=400, max_ms=600_000)
    assert [(u.start_ms, u.end_ms) for u in units] == [(0, 14_000), (15_000, 29_000)]
    assert all(len(u.text) >= 300 for u in units)


def test_units_never_span_more_than_the_time_limit() -> None:
    segments = [(i * 10.0, i * 10.0 + 9.0, "short") for i in range(20)]
    units = group_units(segments, chars=10_000, max_ms=45_000)
    assert len(units) > 1
    assert all(u.end_ms - u.start_ms <= 45_000 for u in units)


def test_a_single_very_long_segment_still_becomes_one_unit() -> None:
    units = group_units([(0.0, 300.0, "one long monologue")], chars=400, max_ms=60_000)
    assert units == [Unit("one long monologue", 0, 300_000)]


def test_no_segments_no_units() -> None:
    assert group_units([], chars=400, max_ms=60_000) == []


def test_units_keep_order_and_text() -> None:
    units = group_units([(0, 1, "alpha"), (1, 2, "beta")], chars=5, max_ms=60_000)
    assert [u.text for u in units] == ["alpha", "beta"]


# ------------------------------------------------------- pipeline (stand-in transcriber)


@pytest.fixture
def index() -> Iterator[PgIndex]:
    if not _reachable():
        pytest.skip("no PostgreSQL")
    pg = PgIndex(DSN, schema=f"t_{uuid.uuid4().hex[:12]}")
    try:
        yield pg
    finally:
        pg.close()
        pg.drop_schema()


def fake_transcriber(monkeypatch: pytest.MonkeyPatch, transcript: Transcript | Exception) -> None:
    def fake(path: Path, config: object = None, on_progress: object = None) -> Transcript:
        assert path.exists()  # the pipeline really downloaded the file
        assert path.name.startswith("media.")
        if isinstance(transcript, Exception):
            raise transcript
        if on_progress is not None:
            on_progress(1.0, 2.0)  # type: ignore[operator]
        return transcript

    monkeypatch.setattr("docsearch.ingest.pipeline.transcribe", fake)


def asr_worker(index: PgIndex, store: MemoryStore) -> Worker:
    return Worker(index, store, worker_id="asr-1", kinds=("asr",), batch_size=1)


def asset_row(index: PgIndex, asset_id: int) -> tuple[object, ...]:
    with index.connection() as conn:
        row = conn.execute(
            "SELECT status, document_id, units, duration_ms, language, error"
            " FROM assets WHERE id = %s",
            (asset_id,),
        ).fetchone()
    assert row is not None
    return row


@needs_db
def test_a_recording_becomes_timestamped_searchable_units(
    index: PgIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    sub = submit(index, store, name="lecture.mp3", data=b"pretend audio bytes " * 20)
    fake_transcriber(
        monkeypatch,
        Transcript(
            [
                Unit("the glacier retreat accelerated this decade", 0, 30_000),
                Unit("volcanic eruptions released ash across the region", 30_000, 61_000),
            ],
            "en",
            0.98,
            61_000,
        ),
    )
    result = asr_worker(index, store).run_once()
    assert result.indexed == 1
    status, first_doc, units, duration, language, error = asset_row(index, sub.asset_id)
    assert (status, units, duration, language, error) == ("indexed", 2, 61_000, "en", None)
    assert first_doc is not None
    glacier = index.search("glacier").hits
    volcano = index.search("volcanic").hits
    assert [h.media.start_ms for h in glacier if h.media] == [0]
    assert [(h.media.start_ms, h.media.end_ms) for h in volcano if h.media] == [(30_000, 61_000)]
    assert glacier[0].media is not None
    assert glacier[0].media.asset_id == sub.asset_id
    assert glacier[0].media.unit_kind == "transcript"
    assert glacier[0].document.title == "lecture.mp3"


@needs_db
def test_the_same_words_in_two_recordings_stay_separate_units(
    index: PgIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    one = submit(index, store, name="a.mp3", data=b"first recording " * 20)
    two = submit(index, store, name="b.mp3", data=b"second recording " * 20)
    fake_transcriber(
        monkeypatch, Transcript([Unit("thank you very much", 0, 5_000)], "en", 0.9, 5_000)
    )
    worker = asr_worker(index, store)
    assert worker.run_once().indexed == 1
    assert worker.run_once().indexed == 1
    hits = index.search("thank").hits
    assert sorted(h.media.asset_id for h in hits if h.media) == sorted([one.asset_id, two.asset_id])


@needs_db
def test_a_recording_with_no_speech_fails_with_a_clear_reason(
    index: PgIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    sub = submit(index, store, name="silence.wav", data=b"RIFF silence " * 20)
    fake_transcriber(monkeypatch, Transcript([], "en", 0.5, 12_000))
    result = asr_worker(index, store).run_once()
    assert result.failed == 1
    status, *_, error = asset_row(index, sub.asset_id)
    assert (status, error) == ("failed", "no speech was recognised")


@needs_db
def test_an_unreadable_recording_fails_permanently_and_a_crash_in_the_model_is_retried(
    index: PgIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore()
    bad = submit(index, store, name="bad.mp4", data=b"not a video " * 20)
    fake_transcriber(monkeypatch, ExtractionError("not a readable audio or video file"))
    assert asr_worker(index, store).run_once().failed == 1
    assert asset_row(index, bad.asset_id)[-1] == "not a readable audio or video file"

    flaky = submit(index, store, name="ok.mp3", data=b"another recording " * 20)
    fake_transcriber(monkeypatch, RuntimeError("out of memory"))
    assert asr_worker(index, store).run_once().retried == 1
    assert asset_row(index, flaky.asset_id)[0] == "queued"
    assert JobQueue(index).counts()["queued"] == 1


@needs_db
def test_asr_jobs_are_not_taken_by_text_or_ocr_workers(index: PgIndex) -> None:
    store = MemoryStore()
    submit(index, store, name="talk.mp3", data=b"audio " * 50)
    assert Worker(index, store, worker_id="t", kinds=("text",)).run_once().indexed == 0
    assert Worker(index, store, worker_id="o", kinds=("ocr",)).run_once().indexed == 0
    assert JobQueue(index).counts()["queued"] == 1


# --------------------------------------------------------------- real speech recognition


def synth(tmp_path: Path, text: str, voice: str = "en", name: str = "speech.wav") -> Path:
    target = tmp_path / name
    subprocess.run(
        ["espeak-ng", "-v", voice, "-s", "140", "-w", str(target), text],
        check=True,
        capture_output=True,
    )
    return target


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True, capture_output=True)


@pytest.fixture(scope="module")
def real_config() -> AsrConfig:
    return AsrConfig(
        model=os.environ.get("DOCSEARCH_ASR_MODEL", "small"),
        model_dir=os.environ.get("DOCSEARCH_ASR_MODEL_DIR", ""),
    )


@needs_asr
def test_english_speech_is_transcribed_with_time_stamps(
    tmp_path: Path, real_config: AsrConfig
) -> None:
    wav = synth(tmp_path, "The glacier retreat is accelerating. Volcanic eruptions released ash.")
    result = transcribe(wav, real_config)
    text = " ".join(u.text for u in result.units).lower()
    assert "glacier" in text
    assert "volcanic" in text or "volcano" in text
    assert result.language == "en"
    assert result.units[0].start_ms >= 0
    assert 0 < result.units[-1].end_ms <= result.duration_ms + 1000


@needs_asr
def test_long_speech_is_split_into_ordered_units(tmp_path: Path, real_config: AsrConfig) -> None:
    sentence = "The monsoon rainfall record for the district was higher than last year. "
    wav = synth(tmp_path, sentence * 18)
    config = AsrConfig(
        model=real_config.model, model_dir=real_config.model_dir, unit_chars=300, unit_max_ms=20_000
    )
    result = transcribe(wav, config)
    assert len(result.units) >= 3
    starts = [u.start_ms for u in result.units]
    assert starts == sorted(starts)
    assert all(u.end_ms > u.start_ms for u in result.units)
    assert all(
        a.end_ms <= b.start_ms + 1 for a, b in zip(result.units, result.units[1:], strict=False)
    )


@needs_asr
def test_the_audio_track_of_a_video_is_transcribed(tmp_path: Path, real_config: AsrConfig) -> None:
    wav = synth(tmp_path, "Earthquake early warning systems saved many lives.")
    video = tmp_path / "clip.mp4"
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc=duration=8:size=160x120:rate=10",
        "-i", str(wav), "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(video),
    )  # fmt: skip
    text = " ".join(u.text for u in transcribe(video, real_config).units).lower()
    assert "earthquake" in text


@needs_asr
def test_a_video_without_sound_is_rejected_before_any_model_time(tmp_path: Path) -> None:
    video = tmp_path / "mute.mp4"
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc=duration=3:size=160x120:rate=10", "-c:v", "mpeg4", str(video)
    )
    with pytest.raises(ExtractionError, match="no audio track"):
        probe(video, max_minutes=10)


@needs_asr
def test_silence_produces_no_invented_text(tmp_path: Path, real_config: AsrConfig) -> None:
    silent = tmp_path / "silence.wav"
    ffmpeg("-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "20", str(silent))
    assert transcribe(silent, real_config).units == []


@needs_asr
def test_garbage_and_overlong_recordings_are_rejected(tmp_path: Path) -> None:
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"this is not audio at all " * 200)
    with pytest.raises(ExtractionError, match="not a readable"):
        probe(junk, max_minutes=10)
    long_silence = tmp_path / "long.wav"
    ffmpeg("-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "130", str(long_silence))
    with pytest.raises(ExtractionError, match="longer than 2 minutes"):
        probe(long_silence, max_minutes=2)
    assert probe(long_silence, max_minutes=3) == pytest.approx(130_000, abs=500)


@needs_asr
@needs_db
def test_end_to_end_spoken_words_are_searchable_with_a_position(
    index: PgIndex, tmp_path: Path, real_config: AsrConfig
) -> None:
    store = MemoryStore()
    wav = synth(tmp_path, "Earthquake early warning systems saved many lives.")
    sub = submit(index, store, name="warning.wav", data=wav.read_bytes())
    worker = Worker(index, store, worker_id="asr-1", kinds=("asr",), batch_size=1, asr=real_config)
    assert worker.run_once().indexed == 1
    hits = index.search("earthquake").hits
    assert len(hits) == 1
    media = hits[0].media
    assert media is not None
    assert media.asset_id == sub.asset_id
    assert media.start_ms is not None
    assert 0 <= media.start_ms < 10_000
    row = asset_row(index, sub.asset_id)
    assert (row[0], row[4]) == ("indexed", "en")
