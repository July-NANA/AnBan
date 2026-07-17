"""Real filesystem tests for run-scoped governed file Capabilities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from anban.capability import CapabilityRegistry, CapabilityResultStatus, InvocationContext
from anban.capability.local import local_capability_registry
from anban.core.errors import AnbanError, ErrorCode
from anban.core.ids import (
    new_capability_invocation_id,
    new_execution_run_id,
    new_node_run_id,
)
from scripts.workspace_bootstrap import REPOSITORY


def context() -> InvocationContext:
    return InvocationContext(
        run_id=new_execution_run_id(),
        node_run_id=new_node_run_id(),
        invocation_id=new_capability_invocation_id(),
        deadline_at=datetime.now(UTC) + timedelta(seconds=10),
    )


def registry(root: Path) -> CapabilityRegistry:
    return local_capability_registry(workspace_root=root)


async def test_all_local_handlers_are_registered_through_registry(tmp_path: Path) -> None:
    names = tuple(item.name for item in registry(tmp_path).search())
    assert names == ("file.list", "file.read", "file.write", "process.execute")


async def test_write_read_list_and_logical_artifact_are_real(tmp_path: Path) -> None:
    gateway = registry(tmp_path)
    invocation_context = context()
    write = await gateway.invoke(
        "file.write",
        {"path": "results/answer.txt", "content": "bounded result"},
        invocation_context,
    )
    artifact = write.artifacts[0]
    run_root = tmp_path / "runs" / str(invocation_context.run_id) / "workspace"
    artifact_file = tmp_path / "artifacts" / str(invocation_context.run_id) / str(artifact.id)

    assert write.status is CapabilityResultStatus.COMPLETED
    assert artifact.uri.startswith("anban://artifact/")
    assert str(tmp_path) not in (write.observation or "")
    assert (run_root / "results" / "answer.txt").read_text() == "bounded result"
    assert artifact_file.read_text() == "bounded result"
    assert artifact_file.stat().st_mode & 0o777 == 0o600

    read = await gateway.invoke(
        "file.read",
        {"path": "results/answer.txt"},
        invocation_context.model_copy(update={"invocation_id": new_capability_invocation_id()}),
    )
    listing = await gateway.invoke(
        "file.list",
        {"path": "results"},
        invocation_context.model_copy(update={"invocation_id": new_capability_invocation_id()}),
    )
    assert read.observation == "bounded result"
    assert listing.observation == '[{"name":"answer.txt","kind":"file"}]'


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.txt", "folder/../../outside"])
async def test_absolute_and_traversal_paths_fail_closed(tmp_path: Path, path: str) -> None:
    with pytest.raises(AnbanError) as failure:
        await registry(tmp_path).invoke("file.read", {"path": path}, context())
    assert failure.value.info.code is ErrorCode.CAPABILITY_ARGUMENTS_INVALID
    assert str(tmp_path) not in str(failure.value.as_dict())


async def test_workspace_external_symlink_fails_closed(tmp_path: Path) -> None:
    invocation_context = context()
    gateway = registry(tmp_path)
    await gateway.invoke("file.list", {}, invocation_context)
    run_root = tmp_path / "runs" / str(invocation_context.run_id) / "workspace"
    external = tmp_path.parent / "external-anban-capability-target"
    external.write_text("outside")
    (run_root / "outside-link").symlink_to(external)
    try:
        with pytest.raises(AnbanError) as failure:
            await gateway.invoke(
                "file.read",
                {"path": "outside-link"},
                invocation_context.model_copy(
                    update={"invocation_id": new_capability_invocation_id()}
                ),
            )
        assert failure.value.info.code is ErrorCode.CAPABILITY_ARGUMENTS_INVALID
    finally:
        external.unlink(missing_ok=True)


async def test_oversized_or_sensitive_file_content_fails_closed(tmp_path: Path) -> None:
    gateway = registry(tmp_path)
    with pytest.raises(AnbanError) as oversized:
        await gateway.invoke(
            "file.write",
            {"path": "large.txt", "content": "x" * 16_385},
            context(),
        )
    assert oversized.value.info.code is ErrorCode.CAPABILITY_ARGUMENTS_INVALID

    with pytest.raises(AnbanError) as sensitive:
        await gateway.invoke(
            "file.write",
            {"path": "unsafe.txt", "content": "Bearer canary-value"},
            context(),
        )
    assert sensitive.value.info.code is ErrorCode.CAPABILITY_ARGUMENTS_INVALID


def test_workspace_root_cannot_overlap_repository() -> None:
    with pytest.raises(ValueError, match="overlaps"):
        local_capability_registry(workspace_root=REPOSITORY)
