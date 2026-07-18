"""Minimal governed Registry implementing CapabilityPort."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import cast

from pydantic import JsonValue

from anban.capability.contracts import (
    CapabilityDescriptor,
    CapabilityHandler,
    CapabilityPort,
    CapabilityProgress,
    CapabilityResult,
    CapabilityResultStatus,
    InvocationContext,
)
from anban.capability.schema import (
    ArgumentsValidationError,
    SchemaDefinitionError,
    validate_arguments,
    validate_input_schema,
)
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.metadata import SafeMetadata


class CapabilityRegistry(CapabilityPort):
    """Single v0.1 entry point for discovery, validation, invocation, and cancellation."""

    def __init__(self, handlers: Iterable[CapabilityHandler] = ()) -> None:
        self._handlers: dict[str, CapabilityHandler] = {}
        self._active: dict[str, tuple[CapabilityHandler, InvocationContext]] = {}
        self._progress_sequences: dict[str, int] = {}
        for handler in handlers:
            self.register(handler)

    def register(self, handler: CapabilityHandler) -> None:
        descriptor = handler.descriptor
        try:
            validate_input_schema(descriptor.input_schema)
        except SchemaDefinitionError as exc:
            raise ValueError(f"invalid input schema for {descriptor.name}") from exc
        if descriptor.name in self._handlers:
            raise ValueError(f"Capability is already registered: {descriptor.name}")
        self._handlers[descriptor.name] = handler

    def search(self, query: str | None = None) -> tuple[CapabilityDescriptor, ...]:
        if query is not None and len(query) > 128:
            raise self._error(
                ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
                "Capability search query is too long",
            )
        normalized = query.strip().lower() if query else ""
        descriptors = (
            handler.descriptor
            for handler in self._handlers.values()
            if not normalized
            or normalized in handler.descriptor.name.lower()
            or normalized in handler.descriptor.description.lower()
        )
        return tuple(sorted(descriptors, key=lambda item: item.name))

    def describe(self, name: str) -> CapabilityDescriptor:
        return self._get_handler(name).descriptor

    async def invoke(
        self,
        name: str,
        arguments: dict[str, JsonValue],
        context: InvocationContext,
    ) -> CapabilityResult:
        handler = self._get_handler(name)
        if not handler.descriptor.available:
            raise self._error(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Capability is unavailable",
                name,
                reason="capability_unavailable",
            )
        try:
            validate_arguments(handler.descriptor.input_schema, arguments)
        except ArgumentsValidationError as exc:
            raise self._error(
                ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
                "Capability arguments are invalid",
                name,
                reason=exc.reason,
            ) from exc
        invocation_key = str(context.invocation_id)
        if invocation_key in self._active:
            raise self._error(
                ErrorCode.CAPABILITY_EXECUTION_FAILED,
                "Capability invocation identity is already active",
                name,
            )
        self._active[invocation_key] = (handler, context)
        retain = False
        try:
            result = await handler.invoke(arguments, context)
            validated = CapabilityResult.model_validate(result)
            if validated.status is CapabilityResultStatus.ACCEPTED:
                if not callable(getattr(handler, "progress", None)) or not callable(
                    getattr(handler, "wait", None)
                ):
                    raise self._error(
                        ErrorCode.CAPABILITY_EXECUTION_FAILED,
                        "Capability accepted background work without lifecycle support",
                        name,
                    )
                validated = self._correlate(validated, context)
                self._progress_sequences[invocation_key] = 0
                retain = True
            return validated
        except AnbanError:
            raise
        except Exception as exc:
            raise self._error(
                ErrorCode.CAPABILITY_EXECUTION_FAILED,
                "Capability execution failed",
                name,
            ) from exc
        finally:
            if not retain:
                self._active.pop(invocation_key, None)

    async def progress(self, context: InvocationContext) -> CapabilityProgress:
        handler, authoritative_context = self._active_invocation(context)
        progress = getattr(handler, "progress", None)
        if not callable(progress):
            raise self._error(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Capability does not support background progress",
                handler.descriptor.name,
            )
        inspect_progress = cast(Callable[[InvocationContext], Awaitable[object]], progress)
        try:
            validated = CapabilityProgress.model_validate(
                await inspect_progress(authoritative_context)
            )
            key = str(context.invocation_id)
            previous = self._progress_sequences.get(key)
            if previous is None or validated.sequence <= previous:
                raise self._error(
                    ErrorCode.CAPABILITY_EXECUTION_FAILED,
                    "Capability progress sequence is not monotonic",
                    handler.descriptor.name,
                )
            self._progress_sequences[key] = validated.sequence
            return validated.model_copy(
                update={
                    "metadata": SafeMetadata(
                        {
                            **validated.metadata.root,
                            "result_correlation_id": key,
                        }
                    )
                }
            )
        except AnbanError:
            raise
        except Exception as exc:
            raise self._error(
                ErrorCode.CAPABILITY_EXECUTION_FAILED,
                "Capability progress inspection failed",
                handler.descriptor.name,
            ) from exc

    async def wait(self, context: InvocationContext) -> CapabilityResult:
        handler, authoritative_context = self._active_invocation(context)
        waiter = getattr(handler, "wait", None)
        if not callable(waiter):
            raise self._error(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Capability does not support background result waiting",
                handler.descriptor.name,
            )
        wait_for_result = cast(Callable[[InvocationContext], Awaitable[object]], waiter)
        try:
            result = CapabilityResult.model_validate(await wait_for_result(authoritative_context))
            if result.status is CapabilityResultStatus.ACCEPTED:
                raise self._error(
                    ErrorCode.CAPABILITY_EXECUTION_FAILED,
                    "Capability wait returned a non-terminal result",
                    handler.descriptor.name,
                )
            result = self._correlate(result, context)
        except AnbanError:
            raise
        except Exception as exc:
            raise self._error(
                ErrorCode.CAPABILITY_EXECUTION_FAILED,
                "Capability result wait failed",
                handler.descriptor.name,
            ) from exc
        self._active.pop(str(context.invocation_id), None)
        self._progress_sequences.pop(str(context.invocation_id), None)
        return result

    async def cancel(self, context: InvocationContext) -> None:
        handler, authoritative_context = self._active_invocation(context)
        try:
            await handler.cancel(authoritative_context)
        except AnbanError:
            raise
        except Exception as exc:
            raise self._error(
                ErrorCode.CAPABILITY_EXECUTION_FAILED,
                "Capability cancellation failed",
                handler.descriptor.name,
            ) from exc

    def _active_invocation(
        self, context: InvocationContext
    ) -> tuple[CapabilityHandler, InvocationContext]:
        active = self._active.get(str(context.invocation_id))
        if active is None:
            raise self._error(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Capability invocation is not active",
            )
        handler, authoritative_context = active
        if authoritative_context != context:
            raise self._error(
                ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
                "Capability invocation context does not match",
                handler.descriptor.name,
            )
        return handler, authoritative_context

    @staticmethod
    def _correlate(result: CapabilityResult, context: InvocationContext) -> CapabilityResult:
        return result.model_copy(
            update={
                "metadata": SafeMetadata(
                    {
                        **result.metadata.root,
                        "result_correlation_id": str(context.invocation_id),
                    }
                )
            }
        )

    def _get_handler(self, name: str) -> CapabilityHandler:
        handler = self._handlers.get(name)
        if handler is None:
            raise self._error(ErrorCode.CAPABILITY_UNKNOWN, "Capability is not registered", name)
        return handler

    @staticmethod
    def _error(
        code: ErrorCode,
        message: str,
        name: str | None = None,
        *,
        reason: str | None = None,
    ) -> AnbanError:
        details = SafeMetadata(
            {
                **({} if name is None else {"capability_name": name}),
                **({} if reason is None else {"reason": reason}),
            }
        )
        return AnbanError(ErrorInfo(code=code, message=message, details=details))
