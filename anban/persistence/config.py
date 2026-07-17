"""Database profile projection over the unified Workspace configuration."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

from anban.config import AnbanConfiguration, load_configuration
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


def database_url(
    profile: DatabaseProfile,
    *,
    environ: Mapping[str, str] | None = None,
    workspace: Path | None = None,
    configuration: AnbanConfiguration | None = None,
) -> str:
    """Return one validated database projection without exposing its value."""

    active = configuration or load_configuration(workspace=workspace, environ=environ)
    return active.database.require(profile.value)
