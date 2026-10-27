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
