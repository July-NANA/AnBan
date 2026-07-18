"""Durable Capability execution without side-effect replay."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import cast

from pydantic import JsonValue

from anban.capability import (
    ArtifactReference,
    CapabilityDescriptor,
    CapabilityPort,
    CapabilityProgress,
    CapabilityProgressStatus,
    CapabilityResult,
    CapabilityResultStatus,
    InvocationContext,
)
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.ids import CheckpointId
from anban.core.metadata import SafeMetadata, SafeScalar
from anban.runtime.persistence import RunPersistence

ArtifactCleanup = Callable[[InvocationContext, ArtifactReference], None]
_SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class PersistedCapabilityPort:
    """Record Invocation, Artifact, and Event facts around a real Capability Port."""

    def __init__(
        self,
        inner: CapabilityPort,
        persistence: RunPersistence,
        *,
        artifact_cleanup: ArtifactCleanup | None = None,
        checkpoint_background: bool = False,
    ) -> None:
        self._inner = inner
        self._persistence = persistence
        self._artifact_cleanup = artifact_cleanup
        self._background_names: dict[str, str] = {}
        self._checkpoint_background = checkpoint_background
        self._checkpoint_ids: dict[str, CheckpointId] = {}

    def search(self, query: str | None = None) -> tuple[CapabilityDescriptor, ...]:
        return self._inner.search(query)

    def describe(self, name: str) -> CapabilityDescriptor:
        return self._inner.describe(name)

    async def invoke(
        self,
        name: str,
        arguments: dict[str, JsonValue],
        context: InvocationContext,
    ) -> CapabilityResult:
        await self._persistence.begin_invocation(name, context)
        try:
            result = await self._inner.invoke(name, arguments, context)
        except asyncio.CancelledError:
            error = ErrorInfo(
                code=ErrorCode.EXECUTION_INTERRUPTED,
                message="Capability execution was interrupted",
            )
            await self._persist_terminal(
                name,
                context,
                CapabilityResult(status=CapabilityResultStatus.CANCELLED, error=error),
            )
            raise
        except AnbanError as exc:
            failed = CapabilityResult(status=CapabilityResultStatus.FAILED, error=exc.info)
            await self._persist_terminal(name, context, failed)
            recoverable = recoverable_capability_failure(exc.info)
            if recoverable is not None:
                return recoverable
            raise
        except Exception:
            error = ErrorInfo(
                code=ErrorCode.CAPABILITY_EXECUTION_FAILED,
                message="Capability execution failed",
            )
            await self._persist_terminal(
                name,
                context,
                CapabilityResult(status=CapabilityResultStatus.FAILED, error=error),
            )
            raise AnbanError(error) from None
        if result.status is CapabilityResultStatus.ACCEPTED:
            self._background_names[str(context.invocation_id)] = name
            try:
                await self._persistence.capability_progressed(
                    name,
                    context,
                    CapabilityProgress(
                        sequence=0,
                        status=CapabilityProgressStatus.ACCEPTED,
                        metadata=result.metadata,
                    ),
                )
                if self._checkpoint_background:
                    checkpoint = await self._persistence.checkpoints.begin(name, context)
                    self._checkpoint_ids[str(context.invocation_id)] = checkpoint.id
                    result = result.model_copy(
                        update={
                            "metadata": SafeMetadata(
                                {
                                    **result.metadata.root,
                                    "checkpoint_id": str(checkpoint.id),
                                }
                            )
                        }
                    )
            except Exception:
                await self._abort_background(name, context)
                raise
            return result
        await self._persist_terminal(name, context, result)
        return result

    async def progress(self, context: InvocationContext) -> CapabilityProgress:
        name = self._background_name(context)
        progress = await self._inner.progress(context)
        await self._persistence.capability_progressed(name, context, progress)
        return progress

    async def wait(self, context: InvocationContext) -> CapabilityResult:
        name = self._background_name(context)
        try:
            result = await self._inner.wait(context)
        except asyncio.CancelledError:
            error = ErrorInfo(
                code=ErrorCode.EXECUTION_INTERRUPTED,
                message="Capability execution was interrupted",
            )
            await self._terminalize_background(
                name,
                context,
                CapabilityResult(status=CapabilityResultStatus.CANCELLED, error=error),
            )
            raise
        except AnbanError as exc:
            await self._terminalize_background(
                name,
                context,
                CapabilityResult(status=CapabilityResultStatus.FAILED, error=exc.info),
            )
            raise
        except Exception:
            error = ErrorInfo(
                code=ErrorCode.CAPABILITY_EXECUTION_FAILED,
                message="Capability result wait failed",
            )
            await self._terminalize_background(
                name,
                context,
                CapabilityResult(status=CapabilityResultStatus.FAILED, error=error),
            )
            raise AnbanError(error) from None
        await self._terminalize_background(name, context, result)
        return result

    async def _persist_terminal(
        self,
        name: str,
        context: InvocationContext,
        result: CapabilityResult,
    ) -> None:
        descriptor: CapabilityDescriptor | None = None
        with suppress(AnbanError):
            descriptor = self._inner.describe(name)
        try:
            await self._persistence.finish_invocation(name, descriptor, context, result)
            return
        except AnbanError as exc:
            persistence_error = exc.info
        try:
            state = await self._persistence.invocation_result_state(context, result)
        except AnbanError:
            state = "unconfirmed"
        if state == "committed":
            return
        if state == "unconfirmed":
            raise AnbanError(
                error_with_details(persistence_error, persistence_state_unconfirmed=True)
            ) from None

        cleanup_details = self._cleanup_artifacts(context, result.artifacts)
        primary = error_with_details(persistence_error, **cleanup_details)
        try:
            await self._persistence.compensate_invocation_failure(name, context, primary)
        except AnbanError as compensation_error:
            primary = error_with_details(
                primary,
                invocation_compensation_failed=True,
                compensation_error_code=compensation_error.info.code.value,
            )
        raise AnbanError(primary) from None

    def _cleanup_artifacts(
        self,
        context: InvocationContext,
        artifacts: tuple[ArtifactReference, ...],
    ) -> dict[str, SafeScalar]:
        if not artifacts:
            return {}
        attempted = len(artifacts)
        succeeded = 0
        if self._artifact_cleanup is not None:
            for reference in artifacts:
                try:
                    self._artifact_cleanup(context, reference)
                except Exception:
                    continue
                succeeded += 1
        return {
            "artifact_cleanup_attempted": attempted,
            "artifact_cleanup_succeeded": succeeded,
            "artifact_cleanup_failed": succeeded != attempted,
        }

    async def cancel(self, context: InvocationContext) -> None:
        await self._inner.cancel(context)

    async def restore_background(
        self,
        name: str,
        context: InvocationContext,
        checkpoint_id: CheckpointId,
        progress_sequence: int,
    ) -> None:
        restore = getattr(self._inner, "restore", None)
        if not callable(restore):
            raise AnbanError(
                ErrorInfo(
                    code=ErrorCode.CAPABILITY_UNAVAILABLE,
                    message="Capability recovery is unavailable",
                )
            )
        recovery = cast(
            Callable[[str, InvocationContext, int], Awaitable[None]],
            restore,
        )
        await recovery(name, context, progress_sequence)
        key = str(context.invocation_id)
        self._background_names[key] = name
        self._checkpoint_ids[key] = checkpoint_id

    def _background_name(self, context: InvocationContext) -> str:
        name = self._background_names.get(str(context.invocation_id))
        if name is None:
            raise AnbanError(
                ErrorInfo(
                    code=ErrorCode.CAPABILITY_UNAVAILABLE,
                    message="Background Capability invocation is unavailable",
                )
            )
        return name

    async def _abort_background(self, name: str, context: InvocationContext) -> None:
        await self._inner.cancel(context)
        result = await self._inner.wait(context)
        try:
            await self._persist_terminal(name, context, result)
            await self._finish_checkpoint(context, result)
        finally:
            self._background_names.pop(str(context.invocation_id), None)
            self._checkpoint_ids.pop(str(context.invocation_id), None)

    async def _finish_checkpoint(
        self, context: InvocationContext, result: CapabilityResult
    ) -> None:
        checkpoint_id = self._checkpoint_ids.get(str(context.invocation_id))
        if checkpoint_id is not None:
            await self._persistence.checkpoints.finish(checkpoint_id, result)

    async def _terminalize_background(
        self,
        name: str,
        context: InvocationContext,
        result: CapabilityResult,
    ) -> None:
        try:
            await self._persist_terminal(name, context, result)
            await self._finish_checkpoint(context, result)
        finally:
            self._background_names.pop(str(context.invocation_id), None)
            self._checkpoint_ids.pop(str(context.invocation_id), None)


def error_with_details(error: ErrorInfo, **details: SafeScalar) -> ErrorInfo:
    return error.model_copy(update={"details": SafeMetadata({**error.details.root, **details})})


def recoverable_capability_failure(error: ErrorInfo) -> CapabilityResult | None:
    """Convert only complete pre-execution error categories into a safe Tool Result."""

    if error.code not in {
        ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
        ErrorCode.CAPABILITY_UNAVAILABLE,
    }:
        return None
    reason = error.details.root.get("reason")
    if not isinstance(reason, str) or _SAFE_REASON.fullmatch(reason) is None:
        return None
    observation = json.dumps(
        {
            "status": CapabilityResultStatus.FAILED.value,
            "error_code": error.code.value,
            "reason": reason,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return CapabilityResult(
        status=CapabilityResultStatus.FAILED,
        observation=observation,
        error=error,
    )
