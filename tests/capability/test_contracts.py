"""Capability contract invariants."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from anban.capability import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityResult,
    CapabilityResultStatus,
    InvocationContext,
)
from anban.core.errors import ErrorCode, ErrorInfo
from anban.core.ids import (
    new_capability_invocation_id,
    new_execution_run_id,
    new_node_run_id,
)


def test_skill_is_a_capability_descriptor() -> None:
    descriptor = CapabilityDescriptor(
        name="skill.activate",
        description="Activate one governed Workspace Skill.",
        kind=CapabilityKind.SKILL,
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string", "maxLength": 128}},
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    assert descriptor.kind is CapabilityKind.SKILL


def test_descriptor_rejects_unsafe_description_or_schema() -> None:
    with pytest.raises(ValidationError):
        CapabilityDescriptor(
            name="unsafe.tool",
            description="Bearer canary-value",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        )
    with pytest.raises(ValidationError):
        CapabilityDescriptor(
            name="unsafe.tool",
            description="Unsafe schema.",
            input_schema={
                "type": "object",
                "properties": {"api_key": {"type": "string"}},
                "additionalProperties": False,
            },
        )


def test_invocation_context_requires_runtime_typed_identity() -> None:
    context = InvocationContext(
        run_id=new_execution_run_id(),
        node_run_id=new_node_run_id(),
        invocation_id=new_capability_invocation_id(),
        deadline_at=datetime.now(UTC),
    )
    assert context.invocation_id


def test_completed_result_requires_only_safe_bounded_observation() -> None:
    result = CapabilityResult(
        status=CapabilityResultStatus.COMPLETED,
        observation="bounded observation",
    )
    assert result.error is None

    with pytest.raises(ValidationError):
        CapabilityResult(
            status=CapabilityResultStatus.COMPLETED,
            observation="Bearer canary-value",
        )


def test_failed_result_requires_structured_error() -> None:
    result = CapabilityResult(
        status=CapabilityResultStatus.FAILED,
        error=ErrorInfo(
            code=ErrorCode.CAPABILITY_EXECUTION_FAILED,
            message="Capability execution failed",
        ),
    )
    assert result.observation is None

    with pytest.raises(ValidationError):
        CapabilityResult(status=CapabilityResultStatus.FAILED)
