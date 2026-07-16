"""Pure validation tests; no fake provider or simulated execution success."""

import tomllib
from pathlib import Path

import pytest

from scripts.check_real_model import ReadinessError, validate_tool_arguments
from scripts.readiness import load_workspace_config


def test_tool_arguments_require_exact_closed_schema() -> None:
    assert validate_tool_arguments('{"filename":"validation.txt","content":"nonce"}', "nonce") == {
        "filename": "validation.txt",
        "content": "nonce",
    }


def test_tool_arguments_reject_additional_properties() -> None:
    with pytest.raises(ReadinessError, match="closed argument schema"):
        validate_tool_arguments(
            '{"filename":"validation.txt","content":"nonce","extra":true}', "nonce"
        )


def test_workspace_configuration_is_read_as_toml(tmp_path: Path) -> None:
    (tmp_path / "anban.toml").write_text(
        'schema_version = 1\nworkspace_id = "local-main"\n', encoding="utf-8"
    )

    assert load_workspace_config(tmp_path) == {
        "schema_version": 1,
        "workspace_id": "local-main",
    }


def test_invalid_workspace_configuration_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "anban.toml").write_text("not = [valid", encoding="utf-8")

    with pytest.raises(tomllib.TOMLDecodeError):
        load_workspace_config(tmp_path)
