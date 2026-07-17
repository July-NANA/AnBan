"""Fail-closed model configuration tests with synthetic values only."""

from __future__ import annotations

from pathlib import Path

import pytest

from anban.core import AnbanError, ErrorCode
from anban.model import load_model_configuration

CONFIG = """
[model.default]
provider = "openai-compatible"
base_url_env = "OPENAI_COMPATIBLE_BASE_URL"
api_key_env = "OPENAI_COMPATIBLE_API_KEY"
model_env = "OPENAI_COMPATIBLE_MODEL"
"""


def test_configuration_uses_allowlisted_environment_references(tmp_path: Path) -> None:
    (tmp_path / "anban.toml").write_text(CONFIG, encoding="utf-8")
    configuration = load_model_configuration(
        workspace=tmp_path,
        environ={
            "OPENAI_COMPATIBLE_BASE_URL": "https://provider.invalid/v1",
            "OPENAI_COMPATIBLE_API_KEY": "synthetic-test-value",
            "OPENAI_COMPATIBLE_MODEL": "test-model",
        },
    )
    assert configuration.model == "test-model"


def test_missing_configuration_fails_explicitly(tmp_path: Path) -> None:
    (tmp_path / "anban.toml").write_text(CONFIG, encoding="utf-8")
    with pytest.raises(AnbanError) as raised:
        load_model_configuration(workspace=tmp_path, environ={})
    assert raised.value.info.code is ErrorCode.CONFIGURATION_MISSING
    assert "synthetic-test-value" not in str(raised.value.as_dict())
