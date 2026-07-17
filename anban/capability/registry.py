"""Minimal governed Registry implementing CapabilityPort."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import JsonValue

from anban.capability.contracts import (
    CapabilityDescriptor,
    CapabilityHandler,
    CapabilityPort,
    CapabilityResult,
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
        try:
            result = await handler.invoke(arguments, context)
            return CapabilityResult.model_validate(result)
        except AnbanError:
            raise
        except Exception as exc:
            raise self._error(
                ErrorCode.CAPABILITY_EXECUTION_FAILED,
                "Capability execution failed",
                name,
            ) from exc
        finally:
            self._active.pop(invocation_key, None)

    async def cancel(self, context: InvocationContext) -> None:
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
