"""Database profile projection over the unified Workspace configuration."""

from __future__ import annotations

from enum import StrEnum

from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.metadata import SafeMetadata


class DatabaseProfile(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"


def database_profile(value: str | None) -> DatabaseProfile:
    try:
        return DatabaseProfile(value or DatabaseProfile.DEVELOPMENT)
    except ValueError as exc:
        raise AnbanError(
            ErrorInfo(
                code=ErrorCode.VALIDATION_FAILED,
                message="database profile is invalid",
                details=SafeMetadata({"profile_valid": False}),
            )
        ) from exc
