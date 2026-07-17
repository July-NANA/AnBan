"""Workspace path resolution and managed Artifact snapshots."""

from __future__ import annotations

import hashlib
from pathlib import Path

from anban.capability.contracts import ArtifactReference, InvocationContext
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.ids import new_artifact_id
from anban.core.metadata import SafeMetadata


def capability_error(
    code: ErrorCode, message: str, *, reason: str, capability_name: str
) -> AnbanError:
    return AnbanError(
        ErrorInfo(
            code=code,
            message=message,
            details=SafeMetadata({"reason": reason, "capability_name": capability_name}),
        )
    )


class WorkspaceBoundary:
    """Resolve logical working paths and store managed Artifact bytes."""

    def __init__(self, root: Path) -> None:
        resolved = root.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("managed Workspace root must be a directory")
        self.root = resolved

    def resolve_cwd(self, value: str) -> tuple[Path, str]:
        if not value or "\x00" in value:
            raise capability_error(
                ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
                "Process working directory is invalid",
                reason="invalid_cwd",
                capability_name="process.execute",
            )
        supplied = Path(value)
        candidate = supplied if supplied.is_absolute() else self.root / supplied
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise capability_error(
                ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
                "Process working directory is unavailable",
                reason="invalid_cwd",
                capability_name="process.execute",
            ) from exc
        if not resolved.is_dir():
            raise capability_error(
                ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
                "Process working directory is invalid",
                reason="invalid_cwd",
                capability_name="process.execute",
            )
        scope = (
            "workspace_root"
            if resolved == self.root
            else "workspace_relative"
            if resolved.is_relative_to(self.root)
            else "absolute"
        )
        return resolved, scope

    @staticmethod
    def resolve_artifact(cwd: Path, value: str) -> Path:
        if not value or "\x00" in value:
            raise capability_error(
                ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
                "Artifact path is invalid",
                reason="artifact_path_invalid",
                capability_name="process.execute",
            )
        supplied = Path(value)
        candidate = supplied if supplied.is_absolute() else cwd / supplied
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise capability_error(
                ErrorCode.CAPABILITY_EXECUTION_FAILED,
                "Declared Artifact is unavailable",
                reason="artifact_missing",
                capability_name="process.execute",
            ) from exc
        if not resolved.is_file():
            raise capability_error(
                ErrorCode.CAPABILITY_EXECUTION_FAILED,
                "Declared Artifact is not a regular file",
                reason="artifact_not_file",
                capability_name="process.execute",
            )
        return resolved

    def create_artifact(
        self, context: InvocationContext, content: bytes, media_type: str
    ) -> ArtifactReference:
        """Store physical bytes while exposing only logical identity and integrity metadata."""

        artifact_id = new_artifact_id()
        directory = self.root / "artifacts" / str(context.run_id)
        target = directory / str(artifact_id)
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o600)
        except OSError:
            target.unlink(missing_ok=True)
            raise
        return ArtifactReference(
            id=artifact_id,
            uri=f"anban://artifact/{context.run_id}/{artifact_id}",
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            media_type=media_type,
        )

    def delete_artifact(self, context: InvocationContext, reference: ArtifactReference) -> None:
        target = self.root / "artifacts" / str(context.run_id) / str(reference.id)
        target.unlink(missing_ok=True)
