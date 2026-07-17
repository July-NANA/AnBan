"""Safe metadata boundary tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from anban.core import SafeMetadata
from anban.core.metadata import validate_safe_text


def test_safe_scalar_metadata_passes() -> None:
    metadata = SafeMetadata(
        {
            "attempt": 1,
            "cached": False,
            "provider": "openai-compatible",
            "input_tokens": 3,
            "output_tokens": 2,
        }
    )
    assert metadata.root["attempt"] == 1


def test_standalone_slash_is_punctuation_but_absolute_host_path_is_rejected() -> None:
    assert validate_safe_text("18 C / 64 F", label="weather") == "18 C / 64 F"
    with pytest.raises(ValueError, match="absolute_host_path"):
        validate_safe_text("stored at /private/result.txt", label="unsafe")


@pytest.mark.parametrize(
    "metadata",
    [
        {"api_key": "canary"},
        {"auth_token": "canary"},
        {"database_url": "configured"},
        {"output": "/Users/example/private.txt"},
        {"output": "failed at /Users/example/private.txt"},
        {"output": "postgresql+asyncpg://database.invalid/anban"},
        {"output": "Bearer canary-value"},
        {"Bad-Key": "value"},
        {"value": "x" * 513},
    ],
)
def test_sensitive_or_unbounded_metadata_fails(metadata: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SafeMetadata.model_validate(metadata)
