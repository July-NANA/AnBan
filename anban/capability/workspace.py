"""Governed run-scoped Workspace file Capabilities."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
from typing import Literal
from uuid import uuid4

from pydantic import JsonValue

from anban.capability.contracts import (
    ArtifactReference,
    CapabilityDescriptor,
    CapabilityResult,
    CapabilityResultStatus,
    InvocationContext,
)
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.ids import new_artifact_id
from anban.core.metadata import SafeMetadata, validate_safe_text

MAX_FILE_BYTES = 16_384
MAX_LIST_ENTRIES = 500
FileOperation = Literal["list", "read", "write"]


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
    """Resolve model-visible paths only inside one Run's working directory."""

    def __init__(self, root: Path) -> None:
        resolved = root.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("managed Workspace root must be a directory")
        self.root = resolved

    def run_directory(self, context: InvocationContext) -> Path:
        directory = self.root / "runs" / str(context.run_id) / "workspace"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = directory.resolve(strict=True)
        self.require_contained(resolved, self.root)
        return resolved

    def resolve(
        self,
        context: InvocationContext,
        value: str,
        *,
        must_exist: bool,
        allow_root: bool = False,
    ) -> Path:
        self._validate_relative(value, allow_root=allow_root)
        run_root = self.run_directory(context)
        candidate = run_root.joinpath(*Path(value).parts)
        try:
            resolved = candidate.resolve(strict=must_exist)
        except (FileNotFoundError, RuntimeError, OSError) as exc:
            raise capability_error(
                ErrorCode.CAPABILITY_EXECUTION_FAILED,
                "Workspace resource is unavailable",
                reason="resource_unavailable",
                capability_name="workspace",
            ) from exc
        self.require_contained(resolved, run_root)
        return resolved

    @staticmethod
    def _validate_relative(value: str, *, allow_root: bool) -> None:
        if not value or (value == "." and not allow_root):
            raise capability_error(
                ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
                "Workspace path is invalid",
                reason="empty_path",
                capability_name="workspace",
            )
        path = Path(value)
        if path.is_absolute() or PureWindowsPath(value).is_absolute() or ".." in path.parts:
            raise capability_error(
                ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
                "Workspace path is invalid",
                reason="path_escape",
                capability_name="workspace",
            )

    @staticmethod
    def require_contained(candidate: Path, root: Path) -> None:
        if not candidate.is_relative_to(root):
            raise capability_error(
                ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
                "Workspace path is invalid",
                reason="path_escape",
                capability_name="workspace",
            )

    def create_artifact(
        self, context: InvocationContext, content: bytes, media_type: str
    ) -> ArtifactReference:
        """Store physical bytes while exposing only logical identity and integrity metadata."""

        artifact_id = new_artifact_id()
        directory = self.root / "artifacts" / str(context.run_id)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.require_contained(directory.resolve(strict=True), self.root)
        target = directory / str(artifact_id)
        target.write_bytes(content)
        target.chmod(0o600)
        return ArtifactReference(
            id=artifact_id,
            uri=f"anban://artifact/{context.run_id}/{artifact_id}",
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            media_type=media_type,
        )


class FileCapability:
    """One registered file handler sharing a run-scoped Workspace boundary."""

    def __init__(self, operation: FileOperation, boundary: WorkspaceBoundary) -> None:
        self._operation = operation
        self._boundary = boundary
        self._descriptor = self._build_descriptor(operation)

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def invoke(
        self, arguments: dict[str, JsonValue], context: InvocationContext
    ) -> CapabilityResult:
        if self._operation == "list":
            return self._list(str(arguments.get("path", ".")), context)
        path = str(arguments["path"])
        if self._operation == "read":
            return self._read(path, context)
        content = arguments["content"]
        if not isinstance(content, str):
            raise capability_error(
                ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
                "File content is invalid",
                reason="content_type",
                capability_name=self.descriptor.name,
            )
        return self._write(path, content, context)

    async def cancel(self, context: InvocationContext) -> None:
        return None

    def _list(self, path: str, context: InvocationContext) -> CapabilityResult:
        target = self._boundary.resolve(context, path, must_exist=True, allow_root=True)
        if not target.is_dir():
            raise capability_error(
                ErrorCode.CAPABILITY_EXECUTION_FAILED,
                "Workspace directory is unavailable",
                reason="not_directory",
                capability_name=self.descriptor.name,
            )
        entries: list[dict[str, JsonValue]] = []
        for child in sorted(target.iterdir(), key=lambda item: item.name):
            resolved = child.resolve(strict=True)
            run_root = self._boundary.run_directory(context)
            self._boundary.require_contained(resolved, run_root)
            kind = "directory" if resolved.is_dir() else "file"
            entries.append({"name": child.name, "kind": kind})
            if len(entries) > MAX_LIST_ENTRIES:
                raise capability_error(
                    ErrorCode.CAPABILITY_EXECUTION_FAILED,
                    "Workspace listing exceeds its limit",
                    reason="output_limit",
                    capability_name=self.descriptor.name,
                )
        observation = json.dumps(entries, ensure_ascii=True, separators=(",", ":"))
        if len(observation) > 16_384:
            raise capability_error(
                ErrorCode.CAPABILITY_EXECUTION_FAILED,
                "Workspace listing exceeds its limit",
                reason="output_limit",
                capability_name=self.descriptor.name,
            )
        return CapabilityResult(
            status=CapabilityResultStatus.COMPLETED,
            observation=observation,
            metadata=SafeMetadata({"entry_count": len(entries)}),
        )

    def _read(self, path: str, context: InvocationContext) -> CapabilityResult:
        target = self._boundary.resolve(context, path, must_exist=True)
        if not target.is_file():
            raise capability_error(
                ErrorCode.CAPABILITY_EXECUTION_FAILED,
                "Workspace file is unavailable",
                reason="not_file",
                capability_name=self.descriptor.name,
            )
        size = target.stat().st_size
        if size > MAX_FILE_BYTES:
            raise capability_error(
                ErrorCode.CAPABILITY_EXECUTION_FAILED,
                "Workspace file exceeds its limit",
                reason="output_limit",
                capability_name=self.descriptor.name,
            )
        try:
            content = target.read_text(encoding="utf-8")
            validate_safe_text(content, label="Workspace file", max_length=MAX_FILE_BYTES)
        except (UnicodeDecodeError, ValueError) as exc:
            raise capability_error(
                ErrorCode.CAPABILITY_EXECUTION_FAILED,
                "Workspace file cannot be safely returned",
                reason="unsafe_content",
                capability_name=self.descriptor.name,
            ) from exc
        return CapabilityResult(
            status=CapabilityResultStatus.COMPLETED,
            observation=content,
            metadata=SafeMetadata({"size_bytes": size}),
        )

    def _write(self, path: str, content: str, context: InvocationContext) -> CapabilityResult:
        try:
            validate_safe_text(content, label="Workspace file", max_length=MAX_FILE_BYTES)
        except ValueError as exc:
            raise capability_error(
                ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
                "File content is not safe to persist",
                reason="unsafe_content",
                capability_name=self.descriptor.name,
            ) from exc
        target = self._boundary.resolve(context, path, must_exist=False)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved_parent = target.parent.resolve(strict=True)
        self._boundary.require_contained(resolved_parent, self._boundary.run_directory(context))
        encoded = content.encode("utf-8")
        temporary = resolved_parent / f".anban-{uuid4()}.tmp"
        try:
            temporary.write_bytes(encoded)
            temporary.chmod(0o600)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        artifact = self._boundary.create_artifact(context, encoded, "text/plain; charset=utf-8")
        return CapabilityResult(
            status=CapabilityResultStatus.COMPLETED,
            observation=json.dumps(
                {"written": path, "artifact_uri": artifact.uri},
                ensure_ascii=True,
                separators=(",", ":"),
            ),
            artifacts=(artifact,),
            metadata=SafeMetadata({"size_bytes": len(encoded)}),
        )

    @staticmethod
    def _build_descriptor(operation: FileOperation) -> CapabilityDescriptor:
        path_property: dict[str, JsonValue] = {
            "type": "string",
            "minLength": 1,
            "maxLength": 512,
        }
        properties: dict[str, JsonValue] = {"path": path_property}
        required: list[JsonValue] = ["path"]
        descriptions = {
            "list": "List one bounded directory in the current Run Workspace.",
            "read": "Read one bounded UTF-8 file in the current Run Workspace.",
            "write": "Write one bounded UTF-8 file and create an Artifact snapshot.",
        }
        if operation == "write":
            properties["content"] = {
                "type": "string",
                "maxLength": MAX_FILE_BYTES,
            }
            required.append("content")
        if operation == "list":
            required = []
        input_schema: dict[str, JsonValue] = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }
        return CapabilityDescriptor(
            name=f"file.{operation}",
            description=descriptions[operation],
            input_schema=input_schema,
        )
