"""Task and Session context contract tests."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from anban.core import (
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
    new_artifact_id,
    new_context_entry_id,
    new_context_summary_id,
    new_session_id,
    new_task_id,
    now_utc,
)


def source() -> ContextSource:
    return ContextSource(kind=ContextSourceKind.USER, reference="interaction:dynamic")


def task_entry(content: str = "An authoritative user fact.") -> ContextEntry:
    return ContextEntry(
        id=new_context_entry_id(),
        scope=ContextScope.TASK,
        task_id=new_task_id(),
        kind=ContextEntryKind.USER_FACT,
        content=content,
        source=source(),
    )


def test_task_and_session_context_preserve_scope_and_source() -> None:
    task_id = new_task_id()
    entry = ContextEntry(
        id=new_context_entry_id(),
        scope=ContextScope.TASK,
        task_id=task_id,
        kind=ContextEntryKind.USER_GOAL,
        content="Complete a newly supplied goal.",
        source=source(),
    )
    context = TaskContext(task_id=task_id, entries=(entry,))
    assert TaskContext.model_validate_json(context.model_dump_json()) == context

    session_id = new_session_id()
    session_entry = ContextEntry(
        id=new_context_entry_id(),
        scope=ContextScope.SESSION,
        session_id=session_id,
        kind=ContextEntryKind.SUPPLEMENT,
        content="Use this fact for later Tasks in the same Session.",
        source=source(),
        sensitivity=ContextSensitivity.SENSITIVE,
    )
    assert SessionContext(session_id=session_id, entries=(session_entry,)).entries


def test_secret_context_is_rejected_instead_of_redacted_or_persisted() -> None:
    entry = task_entry().model_dump()
    entry["sensitivity"] = ContextSensitivity.SECRET
    with pytest.raises(ValidationError):
        ContextEntry.model_validate(entry)


def test_artifact_reference_requires_real_typed_identity() -> None:
    base = task_entry().model_dump()
    base.update(kind=ContextEntryKind.ARTIFACT_REFERENCE, artifact_id=new_artifact_id())
    assert ContextEntry.model_validate(base).artifact_id
    with pytest.raises(ValidationError):
        ContextEntry.model_validate(
            {**task_entry().model_dump(), "kind": ContextEntryKind.ARTIFACT_REFERENCE}
        )


def test_conflict_expiry_and_self_reference_semantics_are_explicit() -> None:
    entry = task_entry()
    with pytest.raises(ValidationError):
        ContextEntry.model_validate(
            {**entry.model_dump(), "state": ContextConflictState.CONFLICTING}
        )
    with pytest.raises(ValidationError):
        ContextEntry.model_validate({**entry.model_dump(), "supersedes": entry.id})
    expires_at = entry.created_at + timedelta(minutes=5)
    expired = ContextEntry.model_validate(
        {
            **entry.model_dump(),
            "state": ContextConflictState.EXPIRED,
            "expires_at": expires_at,
        }
    )
    assert expired.expires_at == expires_at


def test_summary_keeps_covered_original_entry_identities() -> None:
    task_id = new_task_id()
    entry_ids = (new_context_entry_id(), new_context_entry_id())
    summary = ContextSummary(
        id=new_context_summary_id(),
        scope=ContextScope.TASK,
        task_id=task_id,
        covered_entry_ids=entry_ids,
        content="A bounded summary that does not replace authoritative source identities.",
    )
    assert summary.covered_entry_ids == entry_ids
    with pytest.raises(ValidationError):
        ContextSummary.model_validate(
            {**summary.model_dump(), "covered_entry_ids": (entry_ids[0], entry_ids[0])}
        )


def test_active_context_enforces_entry_and_character_boundaries() -> None:
    task_id = new_task_id()
    entries = tuple(
        ContextEntry(
            id=new_context_entry_id(),
            scope=ContextScope.TASK,
            task_id=task_id,
            kind=ContextEntryKind.OBSERVATION,
            content=f"Observation {index}",
            source=ContextSource(
                kind=ContextSourceKind.RUNTIME,
                reference=f"node:{index}",
            ),
        )
        for index in range(3)
    )
    with pytest.raises(ValidationError):
        TaskContext(
            task_id=task_id,
            entries=entries,
            boundary=ContextCompressionBoundary(max_active_entries=2),
        )
    with pytest.raises(ValidationError):
        TaskContext(
            task_id=task_id,
            entries=tuple(
                ContextEntry.model_validate({**entry.model_dump(), "content": "x" * 600})
                for entry in entries
            ),
            boundary=ContextCompressionBoundary(max_active_chars=1024),
        )


def test_context_rejects_entries_from_another_identity() -> None:
    entry = task_entry()
    with pytest.raises(ValidationError):
        TaskContext(task_id=new_task_id(), entries=(entry,))


def test_context_timestamps_are_utc_aware() -> None:
    assert now_utc().utcoffset() is not None
