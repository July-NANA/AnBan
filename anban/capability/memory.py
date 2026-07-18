"""Durable bounded Task and Session context Capability."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from pydantic import JsonValue, ValidationError

from anban.capability.contracts import (
    CapabilityDescriptor,
    CapabilityResult,
    CapabilityResultStatus,
    InventoryKind,
    InvocationContext,
)
from anban.core.context import (
    ContextCompressionBoundary,
    ContextConflictState,
    ContextEntry,
    ContextEntryKind,
    ContextScope,
    ContextSensitivity,
    ContextSource,
    ContextSourceKind,
    ContextSummary,
    SessionContext,
    TaskContext,
)
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.core.ids import (
    ContextEntryId,
    SessionId,
    TaskId,
    new_context_entry_id,
    new_context_summary_id,
)
from anban.core.metadata import SafeMetadata
from anban.core.models import now_utc
from anban.core.persistence import ExecutionRepository, UnitOfWorkFactory

_OPERATIONS = ["read", "remember", "compress", "expire"]
_KINDS = [
    ContextEntryKind.USER_FACT.value,
    ContextEntryKind.SUPPLEMENT.value,
    ContextEntryKind.OBSERVATION.value,
]
_SENSITIVITIES = [
    ContextSensitivity.PUBLIC.value,
    ContextSensitivity.INTERNAL.value,
    ContextSensitivity.SENSITIVE.value,
]
_RELATIONSHIPS = ["none", "supersedes", "conflicts_with"]


class MemoryContextCapability:
    """Read and retain bounded context through the existing Unit of Work."""

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        *,
        protected_values: tuple[str, ...] = (),
        boundary: ContextCompressionBoundary | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._protected_values = tuple(value for value in protected_values if value)
        self._boundary = boundary or ContextCompressionBoundary()
        self._descriptor = self._build_descriptor()

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def invoke(
        self, arguments: dict[str, JsonValue], context: InvocationContext
    ) -> CapabilityResult:
        operation = arguments.get("operation")
        scope = self._scope(arguments.get("scope"))
        if not isinstance(operation, str):
            raise self._arguments_error("operation_invalid")
        try:
            async with self._unit_of_work() as unit:
                repository = unit.executions
                identity = await self._identity(scope, context, repository)
                if operation == "read":
                    result = await self._read(repository, scope, identity)
                elif operation == "remember":
                    result = await self._remember(repository, scope, identity, arguments, context)
                    await unit.commit()
                elif operation == "compress":
                    result = await self._compress(repository, scope, identity, arguments)
                    await unit.commit()
                elif operation == "expire":
                    result = await self._expire(repository, scope, identity, arguments)
                    await unit.commit()
                else:
                    raise self._arguments_error("operation_invalid")
        except AnbanError:
            raise
        except (ValidationError, ValueError):
            raise self._arguments_error("context_invalid") from None
        return result

    async def cancel(self, context: InvocationContext) -> None:
        return None

    async def _read(
        self,
        repository: ExecutionRepository,
        scope: ContextScope,
        identity: TaskId | SessionId,
    ) -> CapabilityResult:
        entries = await repository.list_context_entries(scope, identity)
        summaries = await repository.list_context_summaries(scope, identity)
        active = self._active(entries)
        self._validate_active(scope, identity, active, summaries)
        observation = json.dumps(
            {
                "status": "completed",
                "scope": scope.value,
                "entries": [
                    {
                        "id": str(entry.id),
                        "kind": entry.kind.value,
                        "content": entry.content,
                        "sensitivity": entry.sensitivity.value,
                        "state": entry.state.value,
                        "source": {
                            "kind": entry.source.kind.value,
                            "reference": entry.source.reference,
                        },
                    }
                    for entry in active
                ],
                "summaries": [
                    {
                        "id": str(summary.id),
                        "covered_entry_ids": [
                            str(entry_id) for entry_id in summary.covered_entry_ids
                        ],
                        "content": summary.content,
                    }
                    for summary in summaries
                ],
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return self._result(
            observation,
            "read",
            scope,
            entry_count=len(active),
            summary_count=len(summaries),
            active_chars=sum(len(entry.content) for entry in active),
        )

    async def _remember(
        self,
        repository: ExecutionRepository,
        scope: ContextScope,
        identity: TaskId | SessionId,
        arguments: dict[str, JsonValue],
        context: InvocationContext,
    ) -> CapabilityResult:
        content = arguments.get("content")
        if not isinstance(content, str) or self._contains_protected(content):
            raise self._arguments_error("content_invalid")
        try:
            kind = ContextEntryKind(arguments.get("kind", "user_fact"))
        except (ValueError, TypeError):
            raise self._arguments_error("kind_invalid") from None
        if kind is ContextEntryKind.ARTIFACT_REFERENCE:
            raise self._arguments_error("kind_invalid")
        try:
            sensitivity = ContextSensitivity(arguments.get("sensitivity", "internal"))
        except (ValueError, TypeError):
            raise self._arguments_error("sensitivity_invalid") from None
        relationship = arguments.get("relationship", "none")
        related = await self._related_entry(
            repository,
            scope,
            identity,
            relationship,
            arguments.get("related_entry_id"),
        )
        created_at = now_utc()
        expires_at = self._expiry(created_at, arguments.get("expires_in_seconds"))
        entry = ContextEntry(
            id=new_context_entry_id(),
            scope=scope,
            task_id=TaskId(identity) if scope is ContextScope.TASK else None,
            session_id=SessionId(identity) if scope is ContextScope.SESSION else None,
            kind=kind,
            content=content,
            source=ContextSource(
                kind=ContextSourceKind.CAPABILITY,
                reference=f"invocation:{context.invocation_id}",
            ),
            sensitivity=sensitivity,
            state=(
                ContextConflictState.CONFLICTING
                if relationship == "conflicts_with"
                else ContextConflictState.ACTIVE
            ),
            supersedes=(None if relationship != "supersedes" or related is None else related.id),
            conflicts_with=(
                None if relationship != "conflicts_with" or related is None else related.id
            ),
            created_at=created_at,
            expires_at=expires_at,
        )
        entries = list(await repository.list_context_entries(scope, identity))
        if relationship == "supersedes":
            if related is None:
                raise self._arguments_error("related_entry_required")
            replacement = related.model_copy(update={"state": ContextConflictState.SUPERSEDED})
            await repository.update_context_entry(replacement)
            entries = [replacement if item.id == related.id else item for item in entries]
        active = (*self._active(tuple(entries)), entry)
        summaries = await repository.list_context_summaries(scope, identity)
        self._validate_active(scope, identity, active, summaries)
        await repository.add_context_entry(entry)
        observation = json.dumps(
            {"status": "completed", "entry_id": str(entry.id), "scope": scope.value},
            separators=(",", ":"),
        )
        return self._result(
            observation,
            "remember",
            scope,
            entry_count=len(active),
            entry_id=str(entry.id),
            active_chars=sum(len(item.content) for item in active),
        )

    async def _compress(
        self,
        repository: ExecutionRepository,
        scope: ContextScope,
        identity: TaskId | SessionId,
        arguments: dict[str, JsonValue],
    ) -> CapabilityResult:
        content = arguments.get("content")
        identifiers = arguments.get("covered_entry_ids")
        if (
            not isinstance(content, str)
            or self._contains_protected(content)
            or not isinstance(identifiers, list)
            or not identifiers
            or not all(isinstance(value, str) for value in identifiers)
        ):
            raise self._arguments_error("compression_invalid")
        try:
            raw_ids = tuple(value for value in identifiers if isinstance(value, str))
            entry_ids = tuple(ContextEntryId(UUID(value)) for value in raw_ids)
        except ValueError:
            raise self._arguments_error("entry_identity_invalid") from None
        if len(entry_ids) != len(set(entry_ids)):
            raise self._arguments_error("entry_identity_duplicate")
        entries = await repository.list_context_entries(scope, identity)
        by_id = {entry.id: entry for entry in entries}
        covered = tuple(by_id.get(entry_id) for entry_id in entry_ids)
        if any(
            entry is None
            or entry.state not in {ContextConflictState.ACTIVE, ContextConflictState.CONFLICTING}
            for entry in covered
        ):
            raise self._arguments_error("entry_not_compressible")
        summary = ContextSummary(
            id=new_context_summary_id(),
            scope=scope,
            task_id=TaskId(identity) if scope is ContextScope.TASK else None,
            session_id=SessionId(identity) if scope is ContextScope.SESSION else None,
            covered_entry_ids=entry_ids,
            content=content,
        )
        if len(summary.content) > self._boundary.max_summary_chars:
            raise self._arguments_error("summary_limit")
        summaries = await repository.list_context_summaries(scope, identity)
        active = tuple(entry for entry in self._active(entries) if entry.id not in set(entry_ids))
        self._validate_active(scope, identity, active, (*summaries, summary))
        await repository.add_context_summary(summary)
        for entry in covered:
            if entry is not None:
                await repository.update_context_entry(
                    entry.model_copy(update={"state": ContextConflictState.SUPERSEDED})
                )
        observation = json.dumps(
            {
                "status": "completed",
                "summary_id": str(summary.id),
                "covered_entry_count": len(entry_ids),
                "original_entries_retained": True,
            },
            separators=(",", ":"),
        )
        return self._result(
            observation,
            "compress",
            scope,
            summary_id=str(summary.id),
            covered_entry_count=len(entry_ids),
            original_entries_retained=True,
        )

    async def _expire(
        self,
        repository: ExecutionRepository,
        scope: ContextScope,
        identity: TaskId | SessionId,
        arguments: dict[str, JsonValue],
    ) -> CapabilityResult:
        entry = await self._entry(repository, arguments.get("related_entry_id"))
        self._require_scope(entry, scope, identity)
        expires_at = max(now_utc(), entry.created_at + timedelta(microseconds=1))
        expired = entry.model_copy(
            update={"state": ContextConflictState.EXPIRED, "expires_at": expires_at}
        )
        await repository.update_context_entry(expired)
        observation = json.dumps(
            {"status": "completed", "entry_id": str(entry.id), "state": "expired"},
            separators=(",", ":"),
        )
        return self._result(observation, "expire", scope, entry_id=str(entry.id), entry_count=1)

    async def _related_entry(
        self,
        repository: ExecutionRepository,
        scope: ContextScope,
        identity: TaskId | SessionId,
        relationship: JsonValue,
        raw_entry_id: JsonValue,
    ) -> ContextEntry | None:
        if relationship not in _RELATIONSHIPS:
            raise self._arguments_error("relationship_invalid")
        if relationship == "none":
            if raw_entry_id is not None:
                raise self._arguments_error("related_entry_unexpected")
            return None
        entry = await self._entry(repository, raw_entry_id)
        self._require_scope(entry, scope, identity)
        if entry.state in {ContextConflictState.SUPERSEDED, ContextConflictState.EXPIRED}:
            raise self._arguments_error("related_entry_inactive")
        return entry

    async def _entry(
        self, repository: ExecutionRepository, raw_entry_id: JsonValue
    ) -> ContextEntry:
        if not isinstance(raw_entry_id, str):
            raise self._arguments_error("entry_identity_required")
        try:
            entry_id = ContextEntryId(UUID(raw_entry_id))
        except ValueError:
            raise self._arguments_error("entry_identity_invalid") from None
        entry = await repository.get_context_entry(entry_id)
        if entry is None:
            raise self._arguments_error("entry_unknown")
        return entry

    @staticmethod
    def _require_scope(
        entry: ContextEntry, scope: ContextScope, identity: TaskId | SessionId
    ) -> None:
        entry_identity = entry.task_id if scope is ContextScope.TASK else entry.session_id
        if entry.scope is not scope or entry_identity != identity:
            raise MemoryContextCapability._arguments_error("entry_scope_mismatch")

    @staticmethod
    def _active(entries: tuple[ContextEntry, ...]) -> tuple[ContextEntry, ...]:
        return tuple(
            entry
            for entry in entries
            if entry.state in {ContextConflictState.ACTIVE, ContextConflictState.CONFLICTING}
            and (entry.expires_at is None or entry.expires_at > now_utc())
        )

    def _validate_active(
        self,
        scope: ContextScope,
        identity: TaskId | SessionId,
        entries: tuple[ContextEntry, ...],
        summaries: tuple[ContextSummary, ...],
    ) -> None:
        context_type = TaskContext if scope is ContextScope.TASK else SessionContext
        context_type.model_validate(
            {
                f"{scope.value}_id": identity,
                "entries": entries,
                "summaries": summaries,
                "boundary": self._boundary,
            }
        )

    async def _identity(
        self,
        scope: ContextScope,
        context: InvocationContext,
        repository: ExecutionRepository,
    ) -> TaskId | SessionId:
        if scope is ContextScope.SESSION:
            raw = context.metadata.root.get("session_id")
            if not isinstance(raw, str):
                raise self._arguments_error("session_context_unavailable")
            try:
                return SessionId(UUID(raw))
            except ValueError:
                raise self._arguments_error("session_context_invalid") from None
        run = await repository.get_run(context.run_id)
        if run is None:
            raise self._arguments_error("run_context_unavailable")
        return run.task_id

    @staticmethod
    def _scope(value: JsonValue) -> ContextScope:
        try:
            return ContextScope(value)
        except (ValueError, TypeError):
            raise MemoryContextCapability._arguments_error("scope_invalid") from None

    @staticmethod
    def _expiry(created_at: datetime, value: JsonValue) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 31_536_000:
            raise MemoryContextCapability._arguments_error("expiry_invalid")
        return created_at + timedelta(seconds=value)

    def _contains_protected(self, value: str) -> bool:
        return any(protected in value for protected in self._protected_values)

    @staticmethod
    def _result(
        observation: str,
        operation: str,
        scope: ContextScope,
        **facts: str | int | bool,
    ) -> CapabilityResult:
        return CapabilityResult(
            status=CapabilityResultStatus.COMPLETED,
            observation=observation,
            metadata=SafeMetadata(
                {
                    "memory_operation": operation,
                    "context_scope": scope.value,
                    "observation_hash": hashlib.sha256(observation.encode()).hexdigest(),
                    **facts,
                }
            ),
        )

    @staticmethod
    def _arguments_error(reason: str) -> AnbanError:
        return AnbanError(
            ErrorInfo(
                code=ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
                message="Memory context arguments are invalid",
                details=SafeMetadata({"capability_name": "memory.context", "reason": reason}),
            )
        )

    @staticmethod
    def _build_descriptor() -> CapabilityDescriptor:
        properties = cast(
            dict[str, JsonValue],
            {
                "operation": {"type": "string", "enum": _OPERATIONS},
                "scope": {"type": "string", "enum": ["task", "session"]},
                "kind": {"type": "string", "enum": _KINDS},
                "content": {"type": "string", "minLength": 1, "maxLength": 8192},
                "sensitivity": {"type": "string", "enum": _SENSITIVITIES},
                "relationship": {"type": "string", "enum": _RELATIONSHIPS},
                "related_entry_id": {
                    "type": "string",
                    "minLength": 36,
                    "maxLength": 36,
                },
                "covered_entry_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 36, "maxLength": 36},
                    "maxItems": 128,
                },
                "expires_in_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 31_536_000,
                },
            },
        )
        schema: dict[str, JsonValue] = {
            "type": "object",
            "properties": properties,
            "required": ["operation", "scope"],
            "additionalProperties": False,
        }
        return CapabilityDescriptor(
            name="memory.context",
            description=(
                "Read, retain, expire, or atomically compress bounded durable Task and Session "
                "context while preserving original facts."
            ),
            inventory_kind=InventoryKind.MEMORY,
            input_schema=schema,
        )
