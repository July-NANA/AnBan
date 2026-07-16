"""Portable managed Workspace bootstrap resolution."""

from __future__ import annotations

import ntpath
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values

REPOSITORY = Path(__file__).resolve().parents[1]
WORKSPACE_VARIABLE = "ANBAN_WORKSPACE_DIR"
WorkspaceSource = Literal["environment", "repository .env", "operating-system default"]


class WorkspaceResolutionError(ValueError):
    """Raised when the managed Workspace root cannot be resolved safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorkspaceResolution:
    path: Path
    source: WorkspaceSource


def default_workspace_value(
    platform: str,
    environ: Mapping[str, str],
    home: Path,
) -> str:
    """Return the platform default without consulting repository configuration."""

    if platform == "darwin":
        return str(home / "Library" / "Application Support" / "Anban" / "workspace")
    if platform.startswith("linux"):
        data_home = environ.get("XDG_DATA_HOME")
        root = str(home / ".local" / "share") if not data_home else data_home.strip()
        if not root:
            raise WorkspaceResolutionError(
                "workspace_default_unavailable", "XDG_DATA_HOME is empty."
            )
        return str(Path(root).expanduser() / "anban" / "workspace")
    if platform == "win32":
        local_app_data = environ.get("LOCALAPPDATA", "").strip()
        if not local_app_data:
            raise WorkspaceResolutionError(
                "workspace_default_unavailable", "LOCALAPPDATA is not available."
            )
        if not ntpath.isabs(local_app_data):
            raise WorkspaceResolutionError(
                "workspace_path_relative", "LOCALAPPDATA must be an absolute path."
            )
        return ntpath.join(local_app_data, "Anban", "workspace")
    raise WorkspaceResolutionError(
        "workspace_default_unavailable", "No reliable Workspace default exists for this OS."
    )


def _configured_value(repository: Path, environ: Mapping[str, str]) -> tuple[str, WorkspaceSource]:
    if WORKSPACE_VARIABLE in environ:
        return environ[WORKSPACE_VARIABLE], "environment"

    dotenv_path = repository / ".env"
    if dotenv_path.is_file():
        value = dotenv_values(dotenv_path, interpolate=False).get(WORKSPACE_VARIABLE)
        if value is not None:
            return value, "repository .env"

    return "", "operating-system default"


def resolve_workspace(
    *,
    repository: Path = REPOSITORY,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> WorkspaceResolution:
    """Resolve the Workspace from environment, repository .env, or the OS default."""

    active_environment = os.environ if environ is None else environ
    active_platform = sys.platform if platform is None else platform
    active_home = Path.home() if home is None else home
    value, source = _configured_value(repository, active_environment)
    if source == "operating-system default":
        value = default_workspace_value(active_platform, active_environment, active_home)

    value = value.strip()
    if not value:
        raise WorkspaceResolutionError(
            "workspace_bootstrap_invalid", "Workspace Bootstrap value is empty."
        )
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise WorkspaceResolutionError(
            "workspace_path_relative", "Workspace Bootstrap path must be absolute."
        )
    return WorkspaceResolution(candidate.resolve(), source)
