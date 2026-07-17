"""Governed no-shell process Capability with bounded output and termination."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Mapping
from pathlib import Path

from pydantic import JsonValue

from anban.capability.contracts import (
    CapabilityDescriptor,
    CapabilityResult,
    CapabilityResultStatus,
    InvocationContext,
)
from anban.capability.workspace import WorkspaceBoundary, capability_error
from anban.core.errors import ErrorCode, ErrorInfo
from anban.core.metadata import SafeMetadata, validate_safe_text
from anban.core.models import now_utc

DEFAULT_TIMEOUT_SECONDS = 10
MAX_TIMEOUT_SECONDS = 30
MAX_PROCESS_OUTPUT_BYTES = 16_384
MAX_PROCESS_ARGUMENTS = 64
_ENVIRONMENT_KEYS = frozenset({"LANG", "LC_ALL", "TZ", "PYTHONUTF8"})


class ProcessCapability:
    """Execute an explicitly mapped program; the allowlist is the v0.1 trust boundary.

    This is bounded local execution, not a container sandbox. A caller must not map a general
    interpreter for untrusted model input. The real acceptance probe injects Python only for its
    fixed, controlled command.
    """

    def __init__(
        self,
        boundary: WorkspaceBoundary,
        allowed_executables: Mapping[str, Path],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._boundary = boundary
        self._executables = dict(allowed_executables)
        self._environment = self._validated_environment(environment or {})
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._cancelled: set[str] = set()
        self._descriptor = self._build_descriptor()

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def invoke(
        self, arguments: dict[str, JsonValue], context: InvocationContext
    ) -> CapabilityResult:
        command = arguments["command"]
        args = arguments.get("args", [])
        cwd = arguments.get("cwd", ".")
        timeout = arguments.get("timeout", DEFAULT_TIMEOUT_SECONDS)
        if (
            not isinstance(command, str)
            or not isinstance(args, list)
            or not all(isinstance(item, str) for item in args)
            or not isinstance(cwd, str)
            or not isinstance(timeout, int)
        ):
            raise capability_error(
                ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
                "Process arguments are invalid",
                reason="argument_type",
                capability_name=self.descriptor.name,
            )
        string_args = [item for item in args if isinstance(item, str)]
        executable = self._executables.get(command)
        if executable is None or not executable.is_absolute() or not executable.is_file():
            return self._failure(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Executable is unavailable",
                reason="missing_executable",
            )
        try:
            for argument in string_args:
                validate_safe_text(argument, label="process argument", max_length=4096)
        except ValueError as exc:
            raise capability_error(
                ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
                "Process arguments are unsafe",
                reason="unsafe_argument",
                capability_name=self.descriptor.name,
            ) from exc
        working_directory = self._boundary.resolve(context, cwd, must_exist=True, allow_root=True)
        if not working_directory.is_dir():
            raise capability_error(
                ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
                "Process working directory is invalid",
                reason="invalid_cwd",
                capability_name=self.descriptor.name,
            )
        remaining = (context.deadline_at - now_utc()).total_seconds()
        effective_timeout = min(float(timeout), remaining)
        if effective_timeout <= 0:
            return self._timeout()
        try:
            process = await asyncio.create_subprocess_exec(
                str(executable),
                *string_args,
                cwd=working_directory,
                env=self._environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError, OSError):
            return self._failure(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Executable is unavailable",
                reason="missing_executable",
            )
        key = str(context.invocation_id)
        self._processes[key] = process
        try:
            try:
                stdout, stderr, exceeded = await asyncio.wait_for(
                    self._collect_output(process),
                    timeout=effective_timeout,
                )
            except TimeoutError:
                await self._stop_process(process)
                return self._timeout()
            if key in self._cancelled:
                return CapabilityResult(
                    status=CapabilityResultStatus.CANCELLED,
                    error=ErrorInfo(
                        code=ErrorCode.EXECUTION_INTERRUPTED,
                        message="Process execution was cancelled",
                        details=SafeMetadata({"capability_name": self.descriptor.name}),
                    ),
                )
            if exceeded:
                return self._failure(
                    ErrorCode.CAPABILITY_EXECUTION_FAILED,
                    "Process output exceeds its limit",
                    reason="output_limit",
                )
            if process.returncode != 0:
                return CapabilityResult(
                    status=CapabilityResultStatus.FAILED,
                    error=ErrorInfo(
                        code=ErrorCode.CAPABILITY_EXECUTION_FAILED,
                        message="Process execution failed",
                        details=SafeMetadata(
                            {
                                "capability_name": self.descriptor.name,
                                "reason": "nonzero_exit",
                                "exit_code": process.returncode,
                            }
                        ),
                    ),
                )
            try:
                output = json.dumps(
                    {
                        "exit_code": process.returncode,
                        "stdout": stdout.decode("utf-8", errors="replace"),
                        "stderr": stderr.decode("utf-8", errors="replace"),
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
                if len(output) > 16_384:
                    return self._failure(
                        ErrorCode.CAPABILITY_EXECUTION_FAILED,
                        "Process output exceeds its limit",
                        reason="output_limit",
                    )
                validate_safe_text(output, label="process observation", max_length=16_384)
            except ValueError:
                return self._failure(
                    ErrorCode.CAPABILITY_EXECUTION_FAILED,
                    "Process output cannot be safely returned",
                    reason="unsafe_output",
                )
            return CapabilityResult(
                status=CapabilityResultStatus.COMPLETED,
                observation=output,
                metadata=SafeMetadata({"exit_code": process.returncode}),
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

    async def _collect_output(
        self, process: asyncio.subprocess.Process
    ) -> tuple[bytes, bytes, bool]:
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("process output streams are unavailable")
        stdout_task = asyncio.create_task(self._read_stream(process.stdout, process))
        stderr_task = asyncio.create_task(self._read_stream(process.stderr, process))
        try:
            await process.wait()
            stdout, stdout_exceeded = await stdout_task
            stderr, stderr_exceeded = await stderr_task
            return stdout, stderr, stdout_exceeded or stderr_exceeded
        finally:
            pending = [task for task in (stdout_task, stderr_task) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def _read_stream(
        self, stream: asyncio.StreamReader, process: asyncio.subprocess.Process
    ) -> tuple[bytes, bool]:
        retained = bytearray()
        exceeded = False
        while chunk := await stream.read(4096):
            remaining = MAX_PROCESS_OUTPUT_BYTES - len(retained)
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

    def _failure(self, code: ErrorCode, message: str, *, reason: str) -> CapabilityResult:
        return CapabilityResult(
            status=CapabilityResultStatus.FAILED,
            error=ErrorInfo(
                code=code,
                message=message,
                details=SafeMetadata({"capability_name": self.descriptor.name, "reason": reason}),
            ),
        )

    def _timeout(self) -> CapabilityResult:
        return CapabilityResult(
            status=CapabilityResultStatus.TIMED_OUT,
            error=ErrorInfo(
                code=ErrorCode.EXECUTION_TIMED_OUT,
                message="Process execution timed out",
                details=SafeMetadata({"capability_name": self.descriptor.name}),
            ),
        )

    @staticmethod
    def _validated_environment(environment: Mapping[str, str]) -> dict[str, str]:
        if set(environment) - _ENVIRONMENT_KEYS:
            raise ValueError("process environment contains a non-allowlisted key")
        for key, value in environment.items():
            validate_safe_text(value, label=f"process environment {key}", max_length=256)
        return dict(environment)

    @staticmethod
    def _build_descriptor() -> CapabilityDescriptor:
        return CapabilityDescriptor(
            name="process.execute",
            description=(
                "Execute one allowlisted program without a shell in the current Run Workspace."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "minLength": 1, "maxLength": 128},
                    "args": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 4096},
                        "maxItems": MAX_PROCESS_ARGUMENTS,
                    },
                    "cwd": {"type": "string", "minLength": 1, "maxLength": 512},
                    "timeout": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_TIMEOUT_SECONDS,
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        )
