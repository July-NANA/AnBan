"""Bounded metadata safe for authoritative domain records."""

from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath
from typing import Self

from pydantic import Field, RootModel, model_validator

SafeScalar = str | int | float | bool | None

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FORBIDDEN_KEY_PARTS = (
    "authorization",
    "database_url",
    "host_path",
    "api_key",
    "password",
    "provider_response",
    "secret",
    "stderr",
    "stdout",
    "token",
)


class SafeMetadata(RootModel[dict[str, SafeScalar]]):
    """Small scalar metadata with sensitive key and host-path rejection."""

    root: dict[str, SafeScalar] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_boundary(self) -> Self:
        if len(self.root) > 32:
            raise ValueError("metadata cannot contain more than 32 entries")
        for key, value in self.root.items():
            if not _KEY_PATTERN.fullmatch(key):
                raise ValueError(f"metadata key is invalid: {key}")
            if any(part in key for part in _FORBIDDEN_KEY_PARTS):
                raise ValueError(f"metadata key is not allowed: {key}")
            if isinstance(value, str):
                if len(value) > 512:
                    raise ValueError(f"metadata value is too long: {key}")
                if Path(value).is_absolute() or PureWindowsPath(value).is_absolute():
                    raise ValueError(f"metadata value cannot be an absolute host path: {key}")
        return self
