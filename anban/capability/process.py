"""General no-shell process execution with bounded I/O and Artifact snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import time
from contextlib import suppress
from pathlib import Path

from pydantic import JsonValue

from anban.capability.contracts import (
    ArtifactReference,
    CapabilityDescriptor,
    CapabilityResult,
    CapabilityResultStatus,
    InventoryKind,
    InvocationContext,
)
from anban.capability.workspace import WorkspaceBoundary, capability_error
from anban.config import policy
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.metadata import SafeMetadata
from anban.core.models import now_utc

_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SENSITIVE_NAME = re.compile(
    r"(?:authorization|credential|database_url|api[_-]?key|password|secret|token)", re.I
)
_MEDIA_TYPE_TOKEN = r"[A-Za-z0-9!#$%&'*+.^_`|~-]+"
_MEDIA_TYPE_PARAMETER_VALUE = rf'(?:{_MEDIA_TYPE_TOKEN}|"[\x20-\x21\x23-\x5B\x5D-\x7E]*")'
_MEDIA_TYPE_PATTERN = (
    rf"^{_MEDIA_TYPE_TOKEN}/{_MEDIA_TYPE_TOKEN}"
    rf"(?:[ \t]*;[ \t]*{_MEDIA_TYPE_TOKEN}[ \t]*=[ \t]*"
    rf"{_MEDIA_TYPE_PARAMETER_VALUE})*$"
)
_MEDIA_TYPE = re.compile(_MEDIA_TYPE_PATTERN)


class ProcessCapability:
    """Execute any available OS program without implicit shell interpretation."""

    def __init__(
        self,
        boundary: WorkspaceBoundary,
        *,
        protected_values: tuple[str, ...] = (),
        default_timeout_seconds: int = policy.PROCESS_DEFAULT_TIMEOUT_DEFAULT_SECONDS,
        max_timeout_seconds: int = policy.PROCESS_TIMEOUT_CONFIG_DEFAULT_SECONDS,
        stdout_max_bytes: int = policy.PROCESS_STDOUT_MAX_BYTES,
        stderr_max_bytes: int = policy.PROCESS_STDERR_MAX_BYTES,
        stdin_max_bytes: int = policy.PROCESS_STDIN_MAX_BYTES,
        max_arguments: int = policy.PROCESS_ARGUMENTS_MAX,
        max_artifacts: int = policy.PROCESS_ARTIFACTS_MAX,
        artifact_max_bytes: int = policy.PROCESS_ARTIFACT_MAX_BYTES,
    ) -> None:
        self._boundary = boundary
        self._protected_values = tuple(value for value in protected_values if value)
        self._default_timeout_seconds = default_timeout_seconds
        self._max_timeout_seconds = max_timeout_seconds
        self._stdout_max_bytes = stdout_max_bytes
        self._stderr_max_bytes = stderr_max_bytes
        self._stdin_max_bytes = stdin_max_bytes
        self._max_arguments = max_arguments
        self._max_artifacts = max_artifacts
        self._artifact_max_bytes = artifact_max_bytes
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._cancelled: set[str] = set()
        self._descriptor = self._build_descriptor()

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def invoke(
        self, arguments: dict[str, JsonValue], context: InvocationContext
    ) -> CapabilityResult:
        command = arguments.get("command")
        args = arguments.get("args", [])
        cwd = arguments.get("cwd", ".")
        timeout = arguments.get("timeout", self._default_timeout_seconds)
        stdin = arguments.get("stdin")
        if (
            not isinstance(command, str)
            or not isinstance(args, list)
            or not all(isinstance(item, str) for item in args)
            or not isinstance(cwd, str)
            or not isinstance(timeout, int)
            or stdin is not None
            and not isinstance(stdin, str)
        ):
            raise self._arguments_error("argument_type")
        string_args = [item for item in args if isinstance(item, str)]
        if len(string_args) > self._max_arguments or any("\x00" in item for item in string_args):
            raise self._arguments_error("argument_limit")
        if not 1 <= timeout <= self._max_timeout_seconds:
            raise self._arguments_error("timeout_limit")
        stdin_bytes = None if stdin is None else stdin.encode("utf-8")
        if stdin_bytes is not None and len(stdin_bytes) > self._stdin_max_bytes:
            raise self._arguments_error("stdin_limit")

        environment = self._environment(arguments.get("env", []))
        executable = self._resolve_executable(command, environment)
        working_directory, cwd_scope = self._boundary.resolve_cwd(cwd)
        declarations = self._artifact_declarations(arguments.get("artifacts", []))
        argument_hash = hashlib.sha256(
            json.dumps(string_args, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        started = time.monotonic()
        remaining = (context.deadline_at - now_utc()).total_seconds()
        effective_timeout = min(float(timeout), remaining)
        if effective_timeout <= 0:
            return self._result(
                CapabilityResultStatus.TIMED_OUT,
                command,
                argument_hash,
                len(string_args),
                cwd_scope,
                started,
                reason="timeout",
                timed_out=True,
            )
        try:
            process = await asyncio.create_subprocess_exec(
                str(executable),
                *string_args,
                cwd=working_directory,
                env=environment,
                stdin=(
                    asyncio.subprocess.DEVNULL if stdin_bytes is None else asyncio.subprocess.PIPE
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError, OSError, ValueError):
            return self._result(
                CapabilityResultStatus.FAILED,
                command,
                argument_hash,
                len(string_args),
                cwd_scope,
                started,
                reason="spawn_failed",
            )
        key = str(context.invocation_id)
        self._processes[key] = process
        try:
            try:
                stdout, stderr, exceeded = await asyncio.wait_for(
                    self._collect_output(process, stdin_bytes), timeout=effective_timeout
                )
            except TimeoutError:
                await self._stop_process(process)
                return self._result(
                    CapabilityResultStatus.TIMED_OUT,
                    command,
                    argument_hash,
                    len(string_args),
                    cwd_scope,
                    started,
                    reason="timeout",
                    timed_out=True,
                )
            if key in self._cancelled:
                return self._result(
                    CapabilityResultStatus.CANCELLED,
                    command,
                    argument_hash,
                    len(string_args),
                    cwd_scope,
                    started,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=process.returncode,
                    reason="cancelled",
                    cancelled=True,
                )
            if exceeded:
                return self._result(
                    CapabilityResultStatus.FAILED,
                    command,
                    argument_hash,
                    len(string_args),
                    cwd_scope,
                    started,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=process.returncode,
                    reason="output_limit",
                )
            if self._contains_protected(stdout, stderr, environment):
                return self._result(
                    CapabilityResultStatus.FAILED,
                    command,
                    argument_hash,
                    len(string_args),
                    cwd_scope,
                    started,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=process.returncode,
                    reason="sensitive_output",
                    include_observation=False,
                )
            if process.returncode != 0:
                return self._result(
                    CapabilityResultStatus.FAILED,
                    command,
                    argument_hash,
                    len(string_args),
                    cwd_scope,
                    started,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=process.returncode,
                    reason="nonzero_exit",
                )
            try:
                artifacts = self._collect_artifacts(
                    declarations, working_directory, context, environment
                )
            except (AnbanError, OSError, ValueError):
                return self._result(
                    CapabilityResultStatus.FAILED,
                    command,
                    argument_hash,
                    len(string_args),
                    cwd_scope,
                    started,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=process.returncode,
                    reason="artifact_collection_failed",
                )
            return self._result(
                CapabilityResultStatus.COMPLETED,
                command,
                argument_hash,
                len(string_args),
                cwd_scope,
                started,
                stdout=stdout,
                stderr=stderr,
                exit_code=process.returncode,
                artifacts=artifacts,
            )
        finally:
            self._processes.pop(key, None)
            self._cancelled.discard(key)

    async def cancel(self, context: InvocationContext) -> None:
        key = str(context.invocation_id)
        process = self._processes.get(key)
        if process is None:
            return
        self._cancelled.add(key)
        await self._stop_process(process)

    def _resolve_executable(self, command: str, environment: dict[str, str]) -> Path:
        if not command or "\x00" in command:
            raise self._arguments_error("command_invalid")
        supplied = Path(command)
        if supplied.is_absolute():
            executable = supplied
        else:
            if "/" in command or "\\" in command:
                raise self._arguments_error("relative_executable")
            resolved = shutil.which(command, path=environment.get("PATH"))
            if resolved is None:
                raise capability_error(
                    ErrorCode.CAPABILITY_UNAVAILABLE,
                    "Executable is unavailable",
                    reason="missing_executable",
                    capability_name=self.descriptor.name,
                )
            executable = Path(resolved)
        try:
            resolved_executable = executable.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise capability_error(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Executable is unavailable",
                reason="missing_executable",
                capability_name=self.descriptor.name,
            ) from exc
        if not resolved_executable.is_file() or not os.access(resolved_executable, os.X_OK):
            raise capability_error(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Executable is unavailable",
                reason="not_executable",
                capability_name=self.descriptor.name,
            )
        return resolved_executable

    def _environment(self, raw: JsonValue) -> dict[str, str]:
        if not isinstance(raw, list):
            raise self._arguments_error("environment_type")
        environment = dict(os.environ)
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise self._arguments_error("environment_type")
            name, value = item.get("name"), item.get("value")
            if (
                not isinstance(name, str)
                or not isinstance(value, str)
                or not _ENVIRONMENT_NAME.fullmatch(name)
                or "\x00" in value
                or name in seen
            ):
                raise self._arguments_error("environment_invalid")
            seen.add(name)
            environment[name] = value
        return environment

    def _artifact_declarations(self, raw: JsonValue) -> tuple[tuple[str, str], ...]:
        if not isinstance(raw, list) or len(raw) > self._max_artifacts:
            raise self._arguments_error("artifact_limit")
        declarations: list[tuple[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise self._arguments_error("artifact_type")
            path = item.get("path")
            media_type = item.get("media_type", "application/octet-stream")
            if (
                not isinstance(path, str)
                or not isinstance(media_type, str)
                or not _MEDIA_TYPE.fullmatch(media_type)
            ):
                raise self._arguments_error("artifact_invalid")
            declarations.append((path, media_type))
        return tuple(declarations)

    def _collect_artifacts(
        self,
        declarations: tuple[tuple[str, str], ...],
        cwd: Path,
        context: InvocationContext,
        environment: dict[str, str],
    ) -> tuple[ArtifactReference, ...]:
        prepared: list[tuple[bytes, str]] = []
        sources: set[Path] = set()
        for value, media_type in declarations:
            source = self._boundary.resolve_artifact(cwd, value)
            if source in sources:
                raise ValueError("Artifact path is declared more than once")
            sources.add(source)
            with source.open("rb") as stream:
                content = stream.read(self._artifact_max_bytes + 1)
            if len(content) > self._artifact_max_bytes:
                raise ValueError("Artifact exceeds configured limit")
            if self._contains_protected(content, b"", environment):
                raise ValueError("Artifact contains protected data")
            prepared.append((content, media_type))
        created: list[ArtifactReference] = []
        try:
            for content, media_type in prepared:
                created.append(self._boundary.create_artifact(context, content, media_type))
        except (OSError, ValueError):
            for reference in created:
                self._boundary.delete_artifact(context, reference)
            raise
        return tuple(created)

    async def _collect_output(
        self, process: asyncio.subprocess.Process, stdin: bytes | None
    ) -> tuple[bytes, bytes, bool]:
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("process output streams are unavailable")
        stdout_task = asyncio.create_task(
            self._read_stream(process.stdout, process, self._stdout_max_bytes)
        )
        stderr_task = asyncio.create_task(
            self._read_stream(process.stderr, process, self._stderr_max_bytes)
        )
        stdin_task = (
            None if stdin is None else asyncio.create_task(self._write_stdin(process, stdin))
        )
        tasks = [stdout_task, stderr_task, *([] if stdin_task is None else [stdin_task])]
        try:
            await process.wait()
            if stdin_task is not None:
                await stdin_task
            stdout, stdout_exceeded = await stdout_task
            stderr, stderr_exceeded = await stderr_task
            return stdout, stderr, stdout_exceeded or stderr_exceeded
        finally:
            pending = [task for task in tasks if not task.done()]
            for task in pending:
                task.cancel()
            for task in pending:
                with suppress(asyncio.CancelledError):
                    await task

    @staticmethod
    async def _write_stdin(process: asyncio.subprocess.Process, content: bytes) -> None:
        if process.stdin is None:
            raise RuntimeError("process stdin is unavailable")
        try:
            process.stdin.write(content)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    async def _read_stream(
        self, stream: asyncio.StreamReader, process: asyncio.subprocess.Process, limit: int
    ) -> tuple[bytes, bool]:
        retained = bytearray()
        exceeded = False
        while chunk := await stream.read(4096):
            remaining = limit - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])
            if len(chunk) > remaining and not exceeded:
                exceeded = True
                await self._stop_process(process)
        return bytes(retained), exceeded

    @staticmethod
    async def _stop_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=1)
        except (ProcessLookupError, TimeoutError):
            if process.returncode is None:
                try:
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()

    def _contains_protected(
        self, stdout: bytes, stderr: bytes, environment: dict[str, str]
    ) -> bool:
        candidates = list(self._protected_values)
        candidates.extend(
            value for name, value in environment.items() if value and _SENSITIVE_NAME.search(name)
        )
        combined = stdout + b"\x00" + stderr
        return any(value.encode("utf-8") in combined for value in candidates if value)

    def _result(
        self,
        status: CapabilityResultStatus,
        command: str,
        arguments_hash: str,
        argument_count: int,
        cwd_scope: str,
        started: float,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        exit_code: int | None = None,
        artifacts: tuple[ArtifactReference, ...] = (),
        reason: str | None = None,
        timed_out: bool = False,
        cancelled: bool = False,
        include_observation: bool = True,
    ) -> CapabilityResult:
        metadata = SafeMetadata(
            {
                "command": Path(command).name,
                "argument_count": argument_count,
                "arguments_hash": arguments_hash,
                "cwd_scope": cwd_scope,
                "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
                "exit_code": exit_code,
                "stdout_size": len(stdout),
                "stderr_size": len(stderr),
                "stdout_hash": hashlib.sha256(stdout).hexdigest(),
                "stderr_hash": hashlib.sha256(stderr).hexdigest(),
                "artifact_count": len(artifacts),
                "timed_out": timed_out,
                "cancelled": cancelled,
            }
        )
        observation = (
            json.dumps(
                {
                    "status": status.value,
                    **(
                        {}
                        if status is CapabilityResultStatus.COMPLETED
                        else {
                            "error_code": (
                                ErrorCode.EXECUTION_TIMED_OUT.value
                                if status is CapabilityResultStatus.TIMED_OUT
                                else ErrorCode.EXECUTION_INTERRUPTED.value
                                if status is CapabilityResultStatus.CANCELLED
                                else ErrorCode.CAPABILITY_EXECUTION_FAILED.value
                            ),
                            "reason": reason or status.value,
                        }
                    ),
                    "exit_code": exit_code,
                    "stdout": stdout.decode("utf-8", errors="replace"),
                    "stderr": stderr.decode("utf-8", errors="replace"),
                    "artifacts": [
                        {
                            "uri": artifact.uri,
                            "sha256": artifact.sha256,
                            "size_bytes": artifact.size_bytes,
                            "media_type": artifact.media_type,
                        }
                        for artifact in artifacts
                    ],
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
            if include_observation
            else None
        )
        if status is CapabilityResultStatus.COMPLETED:
            return CapabilityResult(
                status=status,
                observation=observation,
                artifacts=artifacts,
                metadata=metadata,
            )
        error_code = (
            ErrorCode.EXECUTION_TIMED_OUT
            if status is CapabilityResultStatus.TIMED_OUT
            else ErrorCode.EXECUTION_INTERRUPTED
            if status is CapabilityResultStatus.CANCELLED
            else ErrorCode.CAPABILITY_EXECUTION_FAILED
        )
        return CapabilityResult(
            status=status,
            observation=observation,
            error=ErrorInfo(
                code=error_code,
                message="Process execution did not complete successfully",
                details=SafeMetadata(
                    {"capability_name": self.descriptor.name, "reason": reason or status.value}
                ),
            ),
            metadata=metadata,
        )

    def _arguments_error(self, reason: str) -> AnbanError:
        return capability_error(
            ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
            "Process arguments are invalid",
            reason=reason,
            capability_name="process.execute",
        )

    def _build_descriptor(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            name="process.execute",
            description=(
                "Execute an available program without an implicit shell; supports bounded I/O, "
                "environment overrides, working directories, and declared output Artifacts."
            ),
            inventory_kind=InventoryKind.PROCESS,
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "args": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 16_384},
                        "maxItems": self._max_arguments,
                    },
                    "cwd": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "env": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "minLength": 1, "maxLength": 128},
                                "value": {"type": "string", "maxLength": 4096},
                            },
                            "required": ["name", "value"],
                            "additionalProperties": False,
                        },
                        "maxItems": 64,
                    },
                    "stdin": {"type": "string", "maxLength": self._stdin_max_bytes},
                    "timeout": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": self._max_timeout_seconds,
                    },
                    "artifacts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string", "minLength": 1, "maxLength": 4096},
                                "media_type": {
                                    "type": "string",
                                    "minLength": 3,
                                    "maxLength": 128,
                                },
                            },
                            "required": ["path"],
                            "additionalProperties": False,
                        },
                        "maxItems": self._max_artifacts,
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        )
