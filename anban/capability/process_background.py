"""Durable supervisor client for restart-safe background Process execution."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from anban.capability.contracts import (
    CapabilityProgress,
    CapabilityProgressStatus,
    CapabilityResult,
    CapabilityResultStatus,
    InvocationContext,
)
from anban.capability.workspace import WorkspaceBoundary, capability_error
from anban.core.errors import AnbanError, ErrorCode
from anban.core.metadata import SafeMetadata

_POLL_SECONDS = 0.05
_START_SECONDS = 5.0


class BackgroundProcessSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    default_timeout_seconds: int
    max_timeout_seconds: int
    stdout_max_bytes: int
    stderr_max_bytes: int
    stdin_max_bytes: int
    max_arguments: int
    max_artifacts: int
    artifact_max_bytes: int


class BackgroundWorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    arguments: dict[str, JsonValue]
    context: InvocationContext
    workspace_root: str
    protected_values: tuple[str, ...]
    settings: BackgroundProcessSettings


class BackgroundWorkerState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    worker_pid: int = Field(gt=0)


class DurableProcessSupervisor:
    """Launch and recover one independent worker without persisting its raw request."""

    def __init__(
        self,
        boundary: WorkspaceBoundary,
        protected_values: tuple[str, ...],
        settings: BackgroundProcessSettings,
    ) -> None:
        self._boundary = boundary
        self._protected_values = protected_values
        self._settings = settings
        self._progress_sequences: dict[str, int] = {}

    async def start(
        self,
        arguments: dict[str, JsonValue],
        context: InvocationContext,
    ) -> CapabilityResult:
        directory = self._directory(context)
        self._prepare_directory(directory)
        request = BackgroundWorkerRequest(
            arguments=arguments,
            context=context,
            workspace_root=str(self._boundary.root),
            protected_values=self._protected_values,
            settings=self._settings,
        )
        worker = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "anban.capability.process_worker",
            str(directory),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        if worker.stdin is None:
            await self._stop_worker(worker)
            raise self._failure("worker_stdin_unavailable")
        encoded = request.model_dump_json().encode()
        try:
            worker.stdin.write(encoded)
            await worker.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            await self._stop_worker(worker)
            raise self._failure("worker_request_failed") from None
        finally:
            worker.stdin.close()

        deadline = asyncio.get_running_loop().time() + _START_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            terminal = self._read_result(directory)
            if terminal is not None:
                return terminal
            if self._started_path(directory).is_file():
                key = str(context.invocation_id)
                self._progress_sequences[key] = 0
                return CapabilityResult(
                    status=CapabilityResultStatus.ACCEPTED,
                    metadata=SafeMetadata(
                        {
                            "background": True,
                            "result_correlation_id": key,
                            "restart_recoverable": True,
                        }
                    ),
                )
            if worker.returncode is not None:
                raise self._failure("worker_exited_before_start")
            await asyncio.sleep(_POLL_SECONDS)
        await self.cancel(context)
        raise self._failure("worker_start_timeout")

    async def recover(self, context: InvocationContext, progress_sequence: int) -> None:
        directory = self._directory(context)
        if not self._started_path(directory).is_file() and self._read_result(directory) is None:
            raise self._failure("recovery_state_missing")
        self._progress_sequences[str(context.invocation_id)] = progress_sequence

    async def progress(self, context: InvocationContext) -> CapabilityProgress:
        directory = self._directory(context)
        state = self._read_started(directory)
        if state is None:
            raise self._failure("recovery_state_missing")
        result_ready = self._result_path(directory).is_file()
        if not result_ready and not self._worker_alive(state.worker_pid):
            # The worker may atomically publish its result and exit between the
            # first filesystem observation and the liveness check. Re-observe
            # the authoritative result after the worker is known to be gone.
            result_ready = self._result_path(directory).is_file()
            if not result_ready:
                raise self._failure("worker_exited_without_result")
        key = str(context.invocation_id)
        sequence = self._progress_sequences.get(key, 0) + 1
        self._progress_sequences[key] = sequence
        return CapabilityProgress(
            sequence=sequence,
            status=(
                CapabilityProgressStatus.RESULT_READY
                if result_ready
                else CapabilityProgressStatus.RUNNING
            ),
            metadata=SafeMetadata(
                {
                    "background": True,
                    "result_correlation_id": key,
                    "restart_recoverable": True,
                }
            ),
        )

    async def wait(self, context: InvocationContext) -> CapabilityResult:
        directory = self._directory(context)
        result_deadline = context.deadline_at + timedelta(seconds=1)
        while True:
            result = self._read_result(directory)
            if result is not None:
                key = str(context.invocation_id)
                return result.model_copy(
                    update={
                        "metadata": SafeMetadata(
                            {
                                **result.metadata.root,
                                "background": True,
                                "result_correlation_id": key,
                                "restart_recoverable": True,
                            }
                        )
                    }
                )
            state = self._read_started(directory)
            if state is None:
                raise self._failure("recovery_state_missing")
            if not self._worker_alive(state.worker_pid):
                # Close the same publish-versus-exit observation race as
                # progress(): process termination orders all worker writes,
                # so this second read is definitive and remains fail-closed.
                if self._read_result(directory) is None:
                    raise self._failure("worker_exited_without_result")
                continue
            if datetime.now(UTC) >= result_deadline:
                await self.cancel(context)
                raise self._failure("worker_result_timeout")
            await asyncio.sleep(_POLL_SECONDS)

    async def cancel(self, context: InvocationContext) -> None:
        directory = self._directory(context)
        if self._read_result(directory) is not None:
            return
        if not self._started_path(directory).is_file():
            raise self._failure("recovery_state_missing")
        marker = self._cancel_path(directory)
        marker.touch(mode=0o600, exist_ok=True)

    def _directory(self, context: InvocationContext) -> Path:
        return self._boundary.root / ".anban" / "process" / str(context.invocation_id)

    @staticmethod
    def _prepare_directory(directory: Path) -> None:
        try:
            for parent in (directory.parent.parent, directory.parent):
                parent.mkdir(mode=0o700, exist_ok=True)
                if parent.is_symlink() or not parent.is_dir():
                    raise OSError("invalid durable process state directory")
                parent.chmod(0o700)
            directory.mkdir(mode=0o700, exist_ok=False)
        except FileExistsError:
            raise DurableProcessSupervisor._failure("invocation_state_exists") from None
        except OSError:
            raise DurableProcessSupervisor._failure("worker_state_unavailable") from None

    @staticmethod
    def _started_path(directory: Path) -> Path:
        return directory / "started.json"

    @staticmethod
    def _result_path(directory: Path) -> Path:
        return directory / "result.json"

    @staticmethod
    def _cancel_path(directory: Path) -> Path:
        return directory / "cancel"

    @staticmethod
    def _read_result(directory: Path) -> CapabilityResult | None:
        path = DurableProcessSupervisor._result_path(directory)
        if not path.is_file():
            return None
        try:
            return CapabilityResult.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValidationError):
            raise DurableProcessSupervisor._failure("worker_result_invalid") from None

    @staticmethod
    def _read_started(directory: Path) -> BackgroundWorkerState | None:
        path = DurableProcessSupervisor._started_path(directory)
        if not path.is_file():
            return None
        try:
            return BackgroundWorkerState.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValidationError):
            raise DurableProcessSupervisor._failure("worker_state_invalid") from None

    @staticmethod
    def _worker_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    async def _stop_worker(worker: asyncio.subprocess.Process) -> None:
        if worker.returncode is not None:
            return
        try:
            if os.name == "nt":
                worker.terminate()
            else:
                os.killpg(worker.pid, 15)
            await asyncio.wait_for(worker.wait(), timeout=1)
        except (ProcessLookupError, TimeoutError):
            if worker.returncode is None:
                worker.kill()
                await worker.wait()

    @staticmethod
    def _failure(reason: str) -> AnbanError:
        return capability_error(
            ErrorCode.CAPABILITY_EXECUTION_FAILED,
            "Durable background Process execution failed",
            reason=reason,
            capability_name="process.execute",
        )
