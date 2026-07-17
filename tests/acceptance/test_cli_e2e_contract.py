"""Deterministic contracts for the real-model Runtime acceptance harness."""

from __future__ import annotations

from dataclasses import replace

import pytest

from scripts.acceptance.check_cli_e2e import (
    RecoverableArtifactEvidence,
    recoverable_artifact_failure,
    recoverable_artifact_issues,
)


def evidence(
    artifact_invocations: tuple[str | None, ...] = ("invocation-1", "invocation-1"),
) -> RecoverableArtifactEvidence:
    artifact_ids = tuple(f"artifact-{index}" for index in range(1, len(artifact_invocations) + 1))
    node_run_ids = tuple("node-1" for _ in artifact_ids)
    invocation_ids = frozenset(
        invocation_id for invocation_id in artifact_invocations if invocation_id is not None
    )
    return RecoverableArtifactEvidence(
        run_id="run-1",
        detail_run_id="run-1",
        persisted=True,
        outcome_status="succeeded",
        final_text_present=True,
        trace_complete=True,
        inconsistencies=(),
        artifact_ids=artifact_ids,
        queried_artifact_ids=artifact_ids,
        artifact_invocation_ids=artifact_invocations,
        artifact_node_run_ids=node_run_ids,
        artifact_uris=tuple(
            f"anban://artifact/run-1/{artifact_id}" for artifact_id in artifact_ids
        ),
        artifact_sizes=tuple(10 + index for index in range(len(artifact_ids))),
        artifact_sha256s=tuple(f"{index + 1:064x}" for index in range(len(artifact_ids))),
        invocation_ids=invocation_ids,
        node_run_ids=frozenset({"node-1"}),
        capability_invocation_count=len(invocation_ids),
        artifact_event_count=len(artifact_ids),
    )


@pytest.mark.parametrize(
    "artifact_invocations",
    [
        ("invocation-1", "invocation-1"),
        ("invocation-1", "invocation-2"),
        ("invocation-1", "invocation-1", "invocation-2", "invocation-2"),
    ],
)
def test_recoverable_artifacts_accept_valid_run_level_topologies(
    artifact_invocations: tuple[str, ...],
) -> None:
    assert recoverable_artifact_issues(evidence(artifact_invocations)) == ()


def test_recoverable_artifacts_require_at_least_two_results() -> None:
    assert recoverable_artifact_issues(evidence(("invocation-1",))) == (
        "artifact_count_below_minimum",
    )


def test_recoverable_artifacts_require_complete_trace() -> None:
    actual = replace(evidence(), trace_complete=False)
    assert recoverable_artifact_issues(actual) == ("trace_incomplete",)


def test_recoverable_artifacts_reject_reported_inconsistency() -> None:
    actual = replace(evidence(), inconsistencies=("artifact_correlation_invalid",))
    assert recoverable_artifact_issues(actual) == ("trace_inconsistent",)


def test_recoverable_artifacts_reject_invalid_run_relationships() -> None:
    actual = replace(
        evidence(),
        detail_run_id="run-2",
        artifact_invocation_ids=("missing-invocation", "invocation-1"),
        artifact_node_run_ids=("missing-node", "node-1"),
        artifact_uris=(
            "anban://artifact/run-2/artifact-1",
            "anban://artifact/run-1/artifact-2",
        ),
    )
    assert recoverable_artifact_issues(actual) == (
        "artifact_invocation_invalid",
        "artifact_node_invalid",
        "artifact_uri_invalid",
        "run_query_mismatch",
    )


def test_recoverable_artifacts_require_nonempty_structurally_valid_results() -> None:
    actual = replace(
        evidence(),
        artifact_sizes=(0, 11),
        artifact_sha256s=("not-a-sha", f"{2:064x}"),
    )
    assert recoverable_artifact_issues(actual) == (
        "artifact_empty",
        "artifact_sha256_invalid",
    )


def test_recoverable_artifact_failure_is_a_safe_bounded_matrix() -> None:
    actual = replace(evidence(("invocation-1",)), trace_complete=False)
    message = recoverable_artifact_failure(
        "recoverable Artifact run 2",
        actual,
        recoverable_artifact_issues(actual),
    )
    assert "run_id=run-1" in message
    assert "persisted=true" in message
    assert "outcome_status=succeeded" in message
    assert "trace_complete=false" in message
    assert "inconsistency_count=0" in message
    assert "artifacts=1" in message
    assert "artifact_invocations=1" in message
    assert "capability_invocations=1" in message
    assert "artifact_events=1" in message
