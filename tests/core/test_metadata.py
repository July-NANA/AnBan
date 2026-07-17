"""Safe metadata boundary tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from anban.core import SafeMetadata


def test_safe_scalar_metadata_passes() -> None:
    metadata = SafeMetadata({"attempt": 1, "cached": False, "provider": "openai-compatible"})
    assert metadata.root["attempt"] == 1


@pytest.mark.parametrize(
    "metadata",
    [
        {"api_key": "canary"},
        {"database_url": "configured"},
        {"output": "/Users/example/private.txt"},
        {"Bad-Key": "value"},
        {"value": "x" * 513},
    ],
)
def test_sensitive_or_unbounded_metadata_fails(metadata: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SafeMetadata.model_validate(metadata)
