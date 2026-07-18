"""Durable Capability execution without side-effect replay."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from contextlib import suppress

from pydantic import JsonValue

from anban.capability import (
    ArtifactReference,
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityPort,
    CapabilityResult,
    CapabilityResultStatus,
    InvocationContext,
)
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
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
    ) -> None:
        self._inner = inner
        self._persistence = persistence
        self._artifact_cleanup = artifact_cleanup

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
        await self._persist_terminal(name, context, result)
        return result

    async def _persist_terminal(
        self,
        name: str,
        context: InvocationContext,
        result: CapabilityResult,
    ) -> None:
        capability_kind: CapabilityKind | None = None
        with suppress(AnbanError):
            capability_kind = self._inner.describe(name).kind
        try:
            await self._persistence.finish_invocation(name, capability_kind, context, result)
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
