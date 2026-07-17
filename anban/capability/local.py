"""Production wiring for the four v0.1 local Capabilities."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from anban.capability.process import ProcessCapability
from anban.capability.registry import CapabilityRegistry
from anban.capability.workspace import FileCapability, WorkspaceBoundary
from scripts.workspace_bootstrap import REPOSITORY, resolve_workspace


def local_capability_registry(
    *,
    workspace_root: Path | None = None,
    allowed_executables: Mapping[str, Path] | None = None,
    environment: Mapping[str, str] | None = None,
) -> CapabilityRegistry:
    """Build the only production Registry wiring for local v0.1 handlers."""

    root = resolve_workspace().path if workspace_root is None else workspace_root
    resolved_root = root.resolve(strict=True)
    repository = REPOSITORY.resolve(strict=True)
    home = Path.home().resolve(strict=True)
    if (
        resolved_root.parent == resolved_root
        or resolved_root in (home, repository)
        or resolved_root.is_relative_to(repository)
        or repository.is_relative_to(resolved_root)
    ):
        raise ValueError("managed Workspace root is too broad or overlaps the repository")
    boundary = WorkspaceBoundary(resolved_root)
    process = ProcessCapability(
        boundary,
        allowed_executables or {},
        environment=environment,
    )
    return CapabilityRegistry(
        (
            FileCapability("list", boundary),
            FileCapability("read", boundary),
            FileCapability("write", boundary),
            process,
        )
    )
