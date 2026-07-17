"""Fail-closed lifecycle transition guards for v0.1 domain records."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from anban.core.errors import InvalidTransitionError
from anban.core.models import (
    CapabilityInvocationStatus,
    ExecutionRunStatus,
    NodeRunStatus,
    TaskStatus,
)


def _active_lifecycle[StatusT: StrEnum](
    initial: StatusT,
    running: StatusT,
    succeeded: StatusT,
    failed: StatusT,
    cancelled: StatusT,
    timed_out: StatusT,
) -> Mapping[StatusT, frozenset[StatusT]]:
    """Build the common v0.1 initial → running → terminal topology."""

    return {
        initial: frozenset({running}),
        running: frozenset({succeeded, failed, cancelled, timed_out}),
        succeeded: frozenset(),
        failed: frozenset(),
        cancelled: frozenset(),
        timed_out: frozenset(),
    }


TASK_TRANSITIONS = _active_lifecycle(
    TaskStatus.CREATED,
    TaskStatus.RUNNING,
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.TIMED_OUT,
)
EXECUTION_RUN_TRANSITIONS = _active_lifecycle(
    ExecutionRunStatus.CREATED,
    ExecutionRunStatus.RUNNING,
    ExecutionRunStatus.SUCCEEDED,
    ExecutionRunStatus.FAILED,
    ExecutionRunStatus.CANCELLED,
    ExecutionRunStatus.TIMED_OUT,
)
NODE_RUN_TRANSITIONS = _active_lifecycle(
    NodeRunStatus.CREATED,
    NodeRunStatus.RUNNING,
    NodeRunStatus.SUCCEEDED,
    NodeRunStatus.FAILED,
    NodeRunStatus.CANCELLED,
    NodeRunStatus.TIMED_OUT,
)
CAPABILITY_INVOCATION_TRANSITIONS = _active_lifecycle(
    CapabilityInvocationStatus.REQUESTED,
    CapabilityInvocationStatus.RUNNING,
    CapabilityInvocationStatus.SUCCEEDED,
    CapabilityInvocationStatus.FAILED,
    CapabilityInvocationStatus.CANCELLED,
    CapabilityInvocationStatus.TIMED_OUT,
)


def _ensure_transition[StatusT: StrEnum](
    lifecycle: str,
    transitions: Mapping[StatusT, frozenset[StatusT]],
    current: StatusT,
    target: StatusT,
) -> None:
    if target not in transitions[current]:
        raise InvalidTransitionError(lifecycle, current.value, target.value)


def ensure_task_transition(current: TaskStatus, target: TaskStatus) -> None:
    _ensure_transition("task", TASK_TRANSITIONS, current, target)


def ensure_execution_run_transition(
    current: ExecutionRunStatus, target: ExecutionRunStatus
) -> None:
    _ensure_transition("execution_run", EXECUTION_RUN_TRANSITIONS, current, target)


def ensure_node_run_transition(current: NodeRunStatus, target: NodeRunStatus) -> None:
    _ensure_transition("node_run", NODE_RUN_TRANSITIONS, current, target)


def ensure_capability_invocation_transition(
    current: CapabilityInvocationStatus, target: CapabilityInvocationStatus
) -> None:
    _ensure_transition("capability_invocation", CAPABILITY_INVOCATION_TRANSITIONS, current, target)
