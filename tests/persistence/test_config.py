"""Database profile selection tests that never load real credentials."""

from __future__ import annotations

from pathlib import Path

import pytest

from anban.core import AnbanError, ErrorCode
from anban.persistence import DatabaseProfile, database_profile, database_url
from anban.workspace import default_configuration_text


def prepare_workspace(path: Path) -> None:
    (path / "anban.toml").write_text(default_configuration_text(), encoding="utf-8")
    (path / "secrets.env").write_text("", encoding="utf-8")


def test_development_and_test_profiles_use_separate_keys(tmp_path: Path) -> None:
    prepare_workspace(tmp_path)
    environment = {
        "DATABASE_URL": "postgresql+asyncpg://localhost/anban",
        "ANBAN_TEST_DATABASE_URL": "postgresql+asyncpg://localhost/anban_test",
    }
    development = database_url(DatabaseProfile.DEVELOPMENT, environ=environment, workspace=tmp_path)
    test = database_url(DatabaseProfile.TEST, environ=environment, workspace=tmp_path)
    assert development != test
    assert development.endswith("/anban")
    assert test.endswith("/anban_test")


def test_database_profile_defaults_to_development_and_rejects_unknown() -> None:
    assert database_profile(None) is DatabaseProfile.DEVELOPMENT
    assert database_profile("test") is DatabaseProfile.TEST
    with pytest.raises(AnbanError) as raised:
        database_profile("production")
    assert raised.value.info.code is ErrorCode.VALIDATION_FAILED
    assert "production" not in str(raised.value.as_dict())


def test_missing_or_wrong_driver_fails_without_echoing_value(tmp_path: Path) -> None:
    prepare_workspace(tmp_path)
    with pytest.raises(AnbanError) as missing:
        database_url(DatabaseProfile.TEST, environ={}, workspace=tmp_path)
    assert missing.value.info.code is ErrorCode.CONFIGURATION_MISSING

    unsafe_value = "sqlite:///local.db"
    with pytest.raises(AnbanError) as wrong_driver:
        database_url(
            DatabaseProfile.TEST,
            environ={"ANBAN_TEST_DATABASE_URL": unsafe_value},
            workspace=tmp_path,
        )
    assert wrong_driver.value.info.code is ErrorCode.VALIDATION_FAILED
    assert unsafe_value not in str(wrong_driver.value)
