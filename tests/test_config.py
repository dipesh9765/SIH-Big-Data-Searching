from pathlib import Path

import pytest

from docsearch.config import Settings


def test_defaults() -> None:
    settings = Settings.from_env({})
    assert settings.db_path == Path("data/docsearch.db")
    assert settings.max_upload_bytes == 10 * 2**20
    assert settings.max_results == 50


def test_environment_overrides() -> None:
    settings = Settings.from_env(
        {
            "DOCSEARCH_DB": "/tmp/x.db",
            "DOCSEARCH_MAX_UPLOAD_MB": "2.5",
            "DOCSEARCH_MAX_RESULTS": "7",
        }
    )
    assert settings.db_path == Path("/tmp/x.db")
    assert settings.max_upload_bytes == int(2.5 * 2**20)
    assert settings.max_results == 7


@pytest.mark.parametrize(
    "env",
    [
        {"DOCSEARCH_MAX_UPLOAD_MB": "lots"},
        {"DOCSEARCH_MAX_RESULTS": "ten"},
        {"DOCSEARCH_MAX_UPLOAD_MB": "0"},
        {"DOCSEARCH_MAX_RESULTS": "-1"},
    ],
)
def test_invalid_values_fail_fast(env: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="DOCSEARCH"):
        Settings.from_env(env)
