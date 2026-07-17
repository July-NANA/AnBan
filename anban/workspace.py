"""Idempotent managed Workspace initialization."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from scripts.workspace_bootstrap import REPOSITORY, WorkspaceResolutionError, resolve_workspace

_DIRECTORIES = ("skills", "runs", "artifacts", "cache", "logs", "tmp")
_CONFIG = """schema_version = 1
workspace_id = "local-main"

[model.default]
provider = "openai-compatible"
base_url_env = "OPENAI_COMPATIBLE_BASE_URL"
api_key_env = "OPENAI_COMPATIBLE_API_KEY"
model_env = "OPENAI_COMPATIBLE_MODEL"

[database]
url_env = "DATABASE_URL"
test_url_env = "ANBAN_TEST_DATABASE_URL"
"""


@dataclass(frozen=True)
class WorkspaceInitialization:
    created_root: bool
    created_config: bool
    created_secrets: bool


def initialize_workspace() -> WorkspaceInitialization:
    root = resolve_workspace().path
    _ensure_safe_root(root)
    created_root = not root.exists()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir():
        raise WorkspaceResolutionError(
            "workspace_layout_invalid", "Managed Workspace root is not a directory."
        )
    root.chmod(0o700)
    for name in _DIRECTORIES:
        directory = root / name
        if directory.is_symlink() or directory.exists() and not directory.is_dir():
            raise WorkspaceResolutionError(
                "workspace_layout_invalid", "Managed Workspace layout is invalid."
            )
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)

    config = root / "anban.toml"
    created_config = _create_file(config, _CONFIG.encode(), 0o600)
    _validate_existing_config(config)
    secrets = root / "secrets.env"
    created_secrets = _create_file(secrets, b"", 0o600)
    if secrets.is_symlink() or not secrets.is_file():
        raise WorkspaceResolutionError(
            "workspace_secret_invalid", "Workspace secret file is invalid."
        )
    secrets.chmod(0o600)
    return WorkspaceInitialization(created_root, created_config, created_secrets)


def _ensure_safe_root(root: Path) -> None:
    repository = REPOSITORY.resolve(strict=True)
    home = Path.home().resolve(strict=True)
    if (
        root == Path(root.anchor)
        or root in {home, repository}
        or root.is_relative_to(repository)
        or repository.is_relative_to(root)
    ):
        raise WorkspaceResolutionError("workspace_path_unsafe", "Managed Workspace path is unsafe.")


def _create_file(path: Path, content: bytes, mode: int) -> bool:
    if path.is_symlink():
        raise WorkspaceResolutionError(
            "workspace_layout_invalid", "Managed Workspace file is invalid."
        )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        return False
    try:
        os.write(descriptor, content)
    finally:
        os.close(descriptor)
    return True


def _validate_existing_config(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise WorkspaceResolutionError(
            "workspace_configuration_invalid", "Workspace configuration is invalid."
        )
    try:
        with path.open("rb") as handle:
            configuration = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WorkspaceResolutionError(
            "workspace_configuration_invalid", "Workspace configuration is invalid."
        ) from exc
    if configuration.get("schema_version") != 1 or not isinstance(
        configuration.get("workspace_id"), str
    ):
        raise WorkspaceResolutionError(
            "workspace_configuration_invalid", "Workspace configuration is invalid."
        )
