"""Production wiring for the v0.1 general Process Capability."""

from __future__ import annotations

from pathlib import Path

from anban.capability.process import ProcessCapability
from anban.capability.registry import CapabilityRegistry
from anban.capability.workspace import WorkspaceBoundary
from anban.config import policy
from scripts.workspace_bootstrap import REPOSITORY, resolve_workspace


def local_capability_registry(
    *,
    workspace_root: Path | None = None,
    process_default_timeout_seconds: int = policy.PROCESS_DEFAULT_TIMEOUT_DEFAULT_SECONDS,
    process_max_timeout_seconds: int = policy.PROCESS_TIMEOUT_CONFIG_DEFAULT_SECONDS,
    stdout_max_bytes: int = policy.PROCESS_STDOUT_MAX_BYTES,
    stderr_max_bytes: int = policy.PROCESS_STDERR_MAX_BYTES,
    stdin_max_bytes: int = policy.PROCESS_STDIN_MAX_BYTES,
    max_arguments: int = policy.PROCESS_ARGUMENTS_MAX,
    max_artifacts: int = policy.PROCESS_ARTIFACTS_MAX,
    artifact_max_bytes: int = policy.PROCESS_ARTIFACT_MAX_BYTES,
    protected_values: tuple[str, ...] = (),
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
        protected_values=protected_values,
        default_timeout_seconds=process_default_timeout_seconds,
        max_timeout_seconds=process_max_timeout_seconds,
        stdout_max_bytes=stdout_max_bytes,
        stderr_max_bytes=stderr_max_bytes,
        stdin_max_bytes=stdin_max_bytes,
        max_arguments=max_arguments,
        max_artifacts=max_artifacts,
        artifact_max_bytes=artifact_max_bytes,
    )
    return CapabilityRegistry((process,))
