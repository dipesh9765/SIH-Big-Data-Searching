"""Runtime configuration, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: Path = Path("data/docsearch.db")
    max_upload_bytes: int = 10 * 1024 * 1024
    max_results: int = 50

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        defaults = cls()
        try:
            max_upload_mb = float(
                env.get("DOCSEARCH_MAX_UPLOAD_MB", defaults.max_upload_bytes / 2**20)
            )
            max_results = int(env.get("DOCSEARCH_MAX_RESULTS", defaults.max_results))
        except ValueError as exc:
            raise ValueError(f"invalid DOCSEARCH_* setting: {exc}") from exc
        if max_upload_mb <= 0 or max_results <= 0:
            raise ValueError("DOCSEARCH_MAX_UPLOAD_MB and DOCSEARCH_MAX_RESULTS must be positive")
        return cls(
            db_path=Path(env.get("DOCSEARCH_DB", str(defaults.db_path))),
            max_upload_bytes=int(max_upload_mb * 2**20),
            max_results=max_results,
        )


@dataclass(frozen=True, slots=True)
class ServiceSettings:
    """Settings for the scale-out service (PostgreSQL + object store). Defaults match the local
    docker compose stack and are for development only: set real values in any shared environment."""

    dsn: str = "postgresql://docsearch:docsearch_dev@127.0.0.1:5433/docsearch"
    schema: str = "public"
    s3_endpoint: str = "127.0.0.1:8333"
    s3_access_key: str = "docsearch"
    s3_secret_key: str = "docsearch_dev_secret"  # noqa: S105 - local-development default
    s3_bucket: str = "docsearch"
    s3_secure: bool = False
    max_upload_bytes: int = 10 * 1024 * 1024
    max_results: int = 50
    ocr_languages: str = "eng+hin+mar"
    ocr_max_pages: int = 200
    max_media_bytes: int = 500 * 1024 * 1024
    asr_model: str = "small"
    asr_model_dir: str = ""
    asr_language: str = ""  # empty: detect from the audio
    asr_max_minutes: int = 240
    api_key: str = ""  # when set, every /v1 route requires it

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServiceSettings:
        env = os.environ if env is None else env
        base = Settings.from_env(env)
        defaults = cls()
        secure = env.get("DOCSEARCH_S3_SECURE", "").strip().lower()
        if secure not in ("", "0", "1", "true", "false", "yes", "no"):
            raise ValueError(f"invalid DOCSEARCH_S3_SECURE: {secure!r}")
        return cls(
            dsn=env.get("DOCSEARCH_DSN", defaults.dsn),
            schema=env.get("DOCSEARCH_SCHEMA", defaults.schema),
            s3_endpoint=env.get("DOCSEARCH_S3_ENDPOINT", defaults.s3_endpoint),
            s3_access_key=env.get("DOCSEARCH_S3_ACCESS_KEY", defaults.s3_access_key),
            s3_secret_key=env.get("DOCSEARCH_S3_SECRET_KEY", defaults.s3_secret_key),
            s3_bucket=env.get("DOCSEARCH_S3_BUCKET", defaults.s3_bucket),
            s3_secure=secure in ("1", "true", "yes"),
            max_upload_bytes=base.max_upload_bytes,
            max_results=base.max_results,
            ocr_languages=env.get("DOCSEARCH_OCR_LANGS", defaults.ocr_languages),
            ocr_max_pages=_positive_int(env, "DOCSEARCH_OCR_MAX_PAGES", defaults.ocr_max_pages),
            max_media_bytes=_positive_int(env, "DOCSEARCH_MAX_MEDIA_MB", 500) * 1024 * 1024,
            api_key=env.get("DOCSEARCH_API_KEY", defaults.api_key),
            asr_model=env.get("DOCSEARCH_ASR_MODEL", defaults.asr_model),
            asr_model_dir=env.get("DOCSEARCH_ASR_MODEL_DIR", defaults.asr_model_dir),
            asr_language=env.get("DOCSEARCH_ASR_LANGUAGE", defaults.asr_language),
            asr_max_minutes=_positive_int(
                env, "DOCSEARCH_ASR_MAX_MINUTES", defaults.asr_max_minutes
            ),
        )


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        value = int(env.get(name, default))
    except ValueError as exc:
        raise ValueError(f"invalid {name}: {exc}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value
