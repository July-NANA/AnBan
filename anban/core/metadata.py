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
_FORBIDDEN_VALUE_PARTS = (
    "authorization:",
    "bearer ",
    "file://",
    "postgresql://",
    "postgresql+asyncpg://",
)
_ALLOWED_KEY_PARTS = {
    "input_tokens": frozenset({"token"}),
    "output_tokens": frozenset({"token"}),
    "stdout_size": frozenset({"stdout"}),
    "stdout_hash": frozenset({"stdout"}),
    "stderr_size": frozenset({"stderr"}),
    "stderr_hash": frozenset({"stderr"}),
}
_VALUE_TOKEN_SEPARATOR = re.compile(r"[\s=,;()\[\]{}]+")


def safe_text_violation_reason(value: str, *, max_length: int = 512) -> str | None:
    """Return one non-sensitive reason without retaining or reproducing the inspected text."""
    if len(value) > max_length:
        return "length_limit"
    lowered = value.lower()
    if any(part in lowered for part in _FORBIDDEN_VALUE_PARTS):
        return "forbidden_sensitive_form"
    for candidate in _VALUE_TOKEN_SEPARATOR.split(value):
        token = candidate.strip("'\"")
        # 独立斜杠常用于单位或二选一分隔，不包含主机路径事实；更长的绝对路径继续拒绝。
        if token == "/":
            continue
        if token and (Path(token).is_absolute() or PureWindowsPath(token).is_absolute()):
            return "absolute_host_path"
    return None


def validate_safe_text(value: str, *, label: str, max_length: int = 512) -> str:
    """Reject bounded text containing credential forms, URLs, or physical host paths."""

    reason = safe_text_violation_reason(value, max_length=max_length)
    if reason is not None:
        raise ValueError(f"{label} violates safe text policy: {reason}")
    return value


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
            allowed_parts = _ALLOWED_KEY_PARTS.get(key, frozenset())
            forbidden_parts = tuple(
                part for part in _FORBIDDEN_KEY_PARTS if part not in allowed_parts
            )
            if any(part in key for part in forbidden_parts):
                raise ValueError(f"metadata key is not allowed: {key}")
            if isinstance(value, str):
                validate_safe_text(value, label=f"metadata value: {key}")
        return self
