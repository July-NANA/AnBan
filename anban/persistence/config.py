"""Fail-closed database profile resolution without exposing connection values."""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

from dotenv import dotenv_values

from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.metadata import SafeMetadata
from scripts.workspace_bootstrap import resolve_workspace


class DatabaseProfile(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"


_PROFILE_KEYS = {
    DatabaseProfile.DEVELOPMENT: "DATABASE_URL",
    DatabaseProfile.TEST: "ANBAN_TEST_DATABASE_URL",
}


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
) -> str:
    """Resolve one profile from process environment, then Workspace secrets."""

    active_environment = os.environ if environ is None else environ
    key = _PROFILE_KEYS[profile]
    configured = active_environment.get(key)
    if not configured:
        workspace_path = resolve_workspace().path if workspace is None else workspace
        value = dotenv_values(workspace_path / "secrets.env", interpolate=False).get(key)
        configured = value if isinstance(value, str) else None
    configured = configured.strip() if configured else configured
    if not configured:
        raise AnbanError(
            ErrorInfo(
                code=ErrorCode.CONFIGURATION_MISSING,
                message="database configuration is missing",
                details=SafeMetadata({"configuration_key": key, "profile": profile.value}),
            )
        )
    if not configured.startswith("postgresql+asyncpg://"):
        raise AnbanError(
            ErrorInfo(
                code=ErrorCode.VALIDATION_FAILED,
                message="database configuration must use PostgreSQL asyncpg",
                details=SafeMetadata({"configuration_key": key, "profile": profile.value}),
            )
        )
    return configured
