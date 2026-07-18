"""Exhaustive property-style tests for the finite v0.1 lifecycles."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum

import pytest

from anban.core import (
    CAPABILITY_INVOCATION_TRANSITIONS,
    CHECKPOINT_TRANSITIONS,
    EXECUTION_RUN_TRANSITIONS,
    NODE_RUN_TRANSITIONS,
    TASK_TRANSITIONS,
    CapabilityInvocationStatus,
    CheckpointStatus,
    ErrorCategory,
    ErrorCode,
    ExecutionRunStatus,
    InvalidTransitionError,
    NodeRunStatus,
    TaskStatus,
    ensure_capability_invocation_transition,
    ensure_checkpoint_transition,
    ensure_execution_run_transition,
    ensure_node_run_transition,
    ensure_task_transition,
)


@pytest.mark.parametrize(
    ("lifecycle", "statuses", "transitions", "guard"),
    [
        ("task", TaskStatus, TASK_TRANSITIONS, ensure_task_transition),
        (
            "execution_run",
            ExecutionRunStatus,
            EXECUTION_RUN_TRANSITIONS,
            ensure_execution_run_transition,
        ),
        ("node_run", NodeRunStatus, NODE_RUN_TRANSITIONS, ensure_node_run_transition),
        (
            "capability_invocation",
            CapabilityInvocationStatus,
            CAPABILITY_INVOCATION_TRANSITIONS,
            ensure_capability_invocation_transition,
        ),
        ("checkpoint", CheckpointStatus, CHECKPOINT_TRANSITIONS, ensure_checkpoint_transition),
    ],
)
def test_every_transition_pair_is_allowed_or_fails_structurally[StatusT: StrEnum](
    lifecycle: str,
    statuses: type[StatusT],
    transitions: Mapping[StatusT, frozenset[StatusT]],
    guard: Callable[[StatusT, StatusT], None],
) -> None:
    assert set(transitions) == set(statuses)
    for current in statuses:
        for target in statuses:
            if target in transitions[current]:
                guard(current, target)
                continue
            with pytest.raises(InvalidTransitionError) as raised:
                guard(current, target)
            assert raised.value.info.code is ErrorCode.INVALID_TRANSITION
            assert raised.value.info.category is ErrorCategory.VALIDATION
            assert raised.value.info.details.root == {
                "lifecycle": lifecycle,
                "current_status": current.value,
                "target_status": target.value,
            }


@pytest.mark.parametrize(
    "transitions",
    [
        TASK_TRANSITIONS,
        EXECUTION_RUN_TRANSITIONS,
        NODE_RUN_TRANSITIONS,
        CAPABILITY_INVOCATION_TRANSITIONS,
        CHECKPOINT_TRANSITIONS,
    ],
)
def test_terminal_states_have_no_outgoing_transition(
    transitions: Mapping[StrEnum, frozenset[StrEnum]],
) -> None:
    terminal_values = {"succeeded", "failed", "cancelled", "timed_out"}
    assert all(
        not targets for status, targets in transitions.items() if status.value in terminal_values
    )
