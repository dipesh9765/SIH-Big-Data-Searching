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


def test_service_settings_defaults_and_overrides() -> None:
    from docsearch.config import ServiceSettings

    defaults = ServiceSettings.from_env({})
    assert defaults.schema == "public"
    assert defaults.s3_secure is False
    custom = ServiceSettings.from_env(
        {
            "DOCSEARCH_DSN": "postgresql://u:p@db/x",
            "DOCSEARCH_SCHEMA": "s",
            "DOCSEARCH_S3_ENDPOINT": "s3.example:9000",
            "DOCSEARCH_S3_SECURE": "true",
            "DOCSEARCH_MAX_UPLOAD_MB": "2",
            "DOCSEARCH_MAX_RESULTS": "7",
        }
    )
    assert custom.dsn == "postgresql://u:p@db/x"
    assert custom.s3_endpoint == "s3.example:9000"
    assert custom.s3_secure is True
    assert custom.max_upload_bytes == 2 * 2**20
    assert custom.max_results == 7


def test_service_settings_reject_bad_values() -> None:
    from docsearch.config import ServiceSettings

    with pytest.raises(ValueError, match="DOCSEARCH_S3_SECURE"):
        ServiceSettings.from_env({"DOCSEARCH_S3_SECURE": "maybe"})
    with pytest.raises(ValueError, match="invalid DOCSEARCH"):
        ServiceSettings.from_env({"DOCSEARCH_MAX_RESULTS": "many"})
