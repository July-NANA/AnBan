"""Safe metadata projections shared by Runtime persistence transitions."""

from __future__ import annotations

from anban.core.errors import ErrorInfo
from anban.core.metadata import SafeMetadata, SafeScalar
from anban.core.models import ExecutionRunStatus, NodeRunStatus, TaskStatus
from anban.runtime.contracts import AgentOutcome, AgentOutcomeStatus

CAPABILITY_EVENT_METADATA = frozenset(
    {
        "argument_count",
        "arguments_hash",
        "artifact_count",
        "active_chars",
        "background",
        "cancelled",
        "catalog_diagnostic_count",
        "catalog_digest",
        "catalog_skill_count",
        "command",
        "content_hash",
        "context_entry_id",
        "context_scope",
        "context_summary_id",
        "covered_entry_count",
        "cwd_scope",
        "duration_ms",
        "entry_count",
        "exit_code",
        "method",
        "memory_operation",
        "mcp_content_count",
        "mcp_protocol_version",
        "mcp_server",
        "mcp_structured",
        "mcp_tool_digest",
        "omitted_line_count",
        "observation_hash",
        "original_entries_retained",
        "progress_sequence",
        "progress_status",
        "result_correlation_id",
        "restart_recoverable",
        "size_bytes",
        "status_code",
        "summary_count",
        "stderr_hash",
        "stderr_size",
        "skill_slug",
        "skill_root",
        "stdout_hash",
        "stdout_size",
        "timed_out",
    }
)
SKILL_CATALOG_EVENT_METADATA = frozenset(
    {"catalog_diagnostic_count", "catalog_digest", "catalog_skill_count"}
)
PERSISTENCE_DIAGNOSTIC_METADATA = frozenset(
    {
        "artifact_cleanup_attempted",
        "artifact_cleanup_failed",
        "artifact_cleanup_succeeded",
        "compensation_error_code",
        "invocation_compensation_failed",
        "last_validation_reason",
        "persistence_state_unconfirmed",
        "reason",
    }
)
_HEX_DIGITS = frozenset("0123456789abcdef")


def is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX_DIGITS


def error_metadata(
    error: ErrorInfo,
    *,
    turn_number: int | None = None,
    allowed_details: frozenset[str] = frozenset(),
) -> SafeMetadata:
    values: dict[str, SafeScalar] = {
        "error_code": error.code.value,
        "error_category": error.category.value,
        **{key: value for key, value in error.details.root.items() if key in allowed_details},
    }
    if turn_number is not None:
        values["turn_number"] = turn_number
    return SafeMetadata(values)


def metadata_projection(metadata: SafeMetadata, allowed: frozenset[str]) -> SafeMetadata:
    """Project adapter metadata through an explicit Event/record allowlist."""

    return SafeMetadata({key: value for key, value in metadata.root.items() if key in allowed})


def terminal_statuses(
    status: AgentOutcomeStatus,
) -> tuple[TaskStatus, ExecutionRunStatus, NodeRunStatus]:
    return (
        TaskStatus(status.value),
        ExecutionRunStatus(status.value),
        NodeRunStatus(status.value),
    )


def outcome_metadata(
    outcome: AgentOutcome,
    *,
    model_turn_count: int | None = None,
    capability_call_count: int | None = None,
    artifact_count: int | None = None,
) -> SafeMetadata:
    return SafeMetadata(
        {
            "model_turn_count": (
                outcome.model_turn_count if model_turn_count is None else model_turn_count
            ),
            "capability_call_count": (
                outcome.capability_call_count
                if capability_call_count is None
                else capability_call_count
            ),
            "artifact_count": len(outcome.artifacts) if artifact_count is None else artifact_count,
            **(
                {}
                if outcome.error is None
                else error_metadata(
                    outcome.error, allowed_details=PERSISTENCE_DIAGNOSTIC_METADATA
                ).root
            ),
        }
    )
