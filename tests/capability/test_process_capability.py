"""Real subprocess execution, bounded I/O, and Artifact snapshot tests."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from anban.capability import (
    ArtifactReference,
    CapabilityRegistry,
    CapabilityResult,
    CapabilityResultStatus,
    InvocationContext,
)
from anban.capability.local import local_capability_registry
from anban.capability.process import ProcessCapability
from anban.capability.workspace import WorkspaceBoundary
from anban.core.errors import AnbanError, ErrorCode
from anban.core.ids import (
    new_capability_invocation_id,
    new_execution_run_id,
    new_node_run_id,
)

_OBSERVATION = TypeAdapter(dict[str, JsonValue])


def context(*, seconds: int = 10) -> InvocationContext:
    return InvocationContext(
        run_id=new_execution_run_id(),
        node_run_id=new_node_run_id(),
        invocation_id=new_capability_invocation_id(),
        deadline_at=datetime.now(UTC) + timedelta(seconds=seconds),
    )


def registry(
    root: Path,
    *,
    stdout_max_bytes: int = 65_536,
    stderr_max_bytes: int = 65_536,
    artifact_max_bytes: int = 16_777_216,
) -> CapabilityRegistry:
    return local_capability_registry(
        workspace_root=root,
        stdout_max_bytes=stdout_max_bytes,
        stderr_max_bytes=stderr_max_bytes,
        artifact_max_bytes=artifact_max_bytes,
    )


def observation(result: CapabilityResult) -> dict[str, JsonValue]:
    raw = result.observation
    assert isinstance(raw, str)
    return _OBSERVATION.validate_json(raw)


async def test_path_program_executes_in_workspace_with_safe_summary(tmp_path: Path) -> None:
    result = await registry(tmp_path).invoke(
        "process.execute",
        {"command": "python", "args": ["-c", "print('real process')"]},
        context(),
    )

    assert result.status is CapabilityResultStatus.COMPLETED
    assert observation(result)["stdout"] == "real process\n"
    assert result.metadata.root == {
        "command": "python",
        "argument_count": 2,
        "arguments_hash": hashlib.sha256(b'["-c","print(\'real process\')"]').hexdigest(),
        "cwd_scope": "workspace_root",
        "duration_ms": result.metadata.root["duration_ms"],
        "exit_code": 0,
        "stdout_size": 13,
        "stderr_size": 0,
        "stdout_hash": hashlib.sha256(b"real process\n").hexdigest(),
        "stderr_hash": hashlib.sha256(b"").hexdigest(),
        "artifact_count": 0,
        "timed_out": False,
        "cancelled": False,
    }
    assert isinstance(result.metadata.root["duration_ms"], int)
    assert str(tmp_path) not in str(result.metadata.model_dump())


async def test_absolute_executable_and_many_plain_arguments(tmp_path: Path) -> None:
    many = [str(index) for index in range(100)]
    result = await registry(tmp_path).invoke(
        "process.execute",
        {
            "command": sys.executable,
            "args": ["-c", "import sys;print(len(sys.argv)-1)", *many],
        },
        context(),
    )

    assert result.status is CapabilityResultStatus.COMPLETED
    assert observation(result)["stdout"] == "100\n"
    assert result.metadata.root["command"] == Path(sys.executable).name
    assert result.metadata.root["argument_count"] == 102


@pytest.mark.parametrize(
    ("command", "expected_code", "expected_reason"),
    [
        (
            "anban-program-that-does-not-exist",
            ErrorCode.CAPABILITY_UNAVAILABLE,
            "missing_executable",
        ),
        (
            "another-unavailable-program-name",
            ErrorCode.CAPABILITY_UNAVAILABLE,
            "missing_executable",
        ),
        ("./python", ErrorCode.CAPABILITY_ARGUMENTS_INVALID, "relative_executable"),
    ],
)
async def test_missing_or_relative_executable_fails_explicitly(
    tmp_path: Path,
    command: str,
    expected_code: ErrorCode,
    expected_reason: str,
) -> None:
    with pytest.raises(AnbanError) as failure:
        await registry(tmp_path).invoke("process.execute", {"command": command}, context())
    assert failure.value.info.code is expected_code
    assert failure.value.info.details.root["reason"] == expected_reason


async def test_inherited_environment_and_call_override(tmp_path: Path) -> None:
    inherited_name = "ANBAN_PROCESS_INHERITED_TEST"
    previous = os.environ.get(inherited_name)
    os.environ[inherited_name] = "inherited"
    try:
        result = await registry(tmp_path).invoke(
            "process.execute",
            {
                "command": "python",
                "args": [
                    "-c",
                    "import os;print(os.environ['HOME']);"
                    "print(os.environ['ANBAN_PROCESS_INHERITED_TEST'])",
                ],
                "env": [{"name": inherited_name, "value": "overridden"}],
            },
            context(),
        )
    finally:
        if previous is None:
            os.environ.pop(inherited_name, None)
        else:
            os.environ[inherited_name] = previous

    assert result.status is CapabilityResultStatus.COMPLETED
    output = str(observation(result)["stdout"])
    assert str(Path.home()) in output
    assert output.endswith("overridden\n")
    assert "overridden" not in str(result.metadata.model_dump())


async def test_relative_and_absolute_working_directories(tmp_path: Path) -> None:
    relative = tmp_path / "nested"
    relative.mkdir()
    outside = tmp_path.parent
    for value, expected_scope, expected in (
        ("nested", "workspace_relative", relative),
        (str(outside), "absolute", outside),
    ):
        result = await registry(tmp_path).invoke(
            "process.execute",
            {"command": "python", "args": ["-c", "import os;print(os.getcwd())"], "cwd": value},
            context(),
        )
        assert result.status is CapabilityResultStatus.COMPLETED
        assert observation(result)["stdout"] == f"{expected}\n"
        assert result.metadata.root["cwd_scope"] == expected_scope


async def test_unavailable_working_directories_share_one_stable_semantic(
    tmp_path: Path,
) -> None:
    regular_file = tmp_path / "not-a-directory"
    regular_file.write_text("bounded", encoding="utf-8")
    for value in ("absent-one", "nested/absent-two", str(regular_file)):
        with pytest.raises(AnbanError) as failure:
            await registry(tmp_path).invoke(
                "process.execute",
                {"command": sys.executable, "cwd": value},
                context(),
            )
        assert failure.value.info.code is ErrorCode.CAPABILITY_ARGUMENTS_INVALID
        assert failure.value.info.details.root["reason"] == "invalid_cwd"


async def test_stdin_stdout_and_stderr_are_structured(tmp_path: Path) -> None:
    result = await registry(tmp_path).invoke(
        "process.execute",
        {
            "command": "python",
            "args": [
                "-c",
                "import sys;data=sys.stdin.read();print(data.upper());"
                "print('diagnostic',file=sys.stderr)",
            ],
            "stdin": "input text",
            "artifacts": [],
        },
        context(),
    )

    assert result.status is CapabilityResultStatus.COMPLETED
    assert observation(result) == {
        "status": "completed",
        "exit_code": 0,
        "stdout": "INPUT TEXT\n",
        "stderr": "diagnostic\n",
        "artifacts": [],
    }


async def test_nonzero_exit_is_failed_with_real_exit_code(tmp_path: Path) -> None:
    result = await registry(tmp_path).invoke(
        "process.execute",
        {
            "command": "python",
            "args": ["-c", "import sys;print('why',file=sys.stderr);sys.exit(7)"],
        },
        context(),
    )

    assert result.status is CapabilityResultStatus.FAILED
    assert result.error is not None
    assert result.error.details.root["reason"] == "nonzero_exit"
    assert result.metadata.root["exit_code"] == 7
    result_observation = observation(result)
    assert result_observation["error_code"] == ErrorCode.CAPABILITY_EXECUTION_FAILED.value
    assert result_observation["reason"] == "nonzero_exit"
    assert result_observation["stderr"] == "why\n"


async def test_timeout_terminates_the_process_group(tmp_path: Path) -> None:
    child = (
        "import time;time.sleep(2);"
        "open('late-child.txt','w',encoding='utf-8').write('not-terminated')"
    )
    result = await registry(tmp_path).invoke(
        "process.execute",
        {
            "command": "python",
            "args": [
                "-c",
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(5)",
            ],
            "timeout": 1,
        },
        context(),
    )

    assert result.status is CapabilityResultStatus.TIMED_OUT
    assert result.metadata.root["timed_out"] is True
    await asyncio.sleep(2)
    assert not (tmp_path / "late-child.txt").exists()


@pytest.mark.parametrize(
    ("program", "field"),
    [
        ("print('x'*1000)", "stdout"),
        ("import sys;print('x'*1000,file=sys.stderr)", "stderr"),
    ],
)
async def test_output_limit_fails_with_bounded_observation(
    tmp_path: Path, program: str, field: str
) -> None:
    result = await registry(tmp_path, stdout_max_bytes=128, stderr_max_bytes=128).invoke(
        "process.execute",
        {"command": "python", "args": ["-c", program]},
        context(),
    )

    assert result.status is CapabilityResultStatus.FAILED
    assert result.error is not None
    assert result.error.details.root["reason"] == "output_limit"
    assert len(str(observation(result)[field]).encode()) <= 128


async def test_process_can_be_cancelled_through_registry(tmp_path: Path) -> None:
    gateway = registry(tmp_path)
    invocation_context = context()
    invocation = asyncio.create_task(
        gateway.invoke(
            "process.execute",
            {"command": "python", "args": ["-c", "import time;time.sleep(5)"]},
            invocation_context,
        )
    )
    await asyncio.sleep(0.2)
    await gateway.cancel(invocation_context)
    result = await invocation

    assert result.status is CapabilityResultStatus.CANCELLED
    assert result.metadata.root["cancelled"] is True


async def test_background_process_reports_monotonic_progress_and_correlated_result(
    tmp_path: Path,
) -> None:
    gateway = registry(tmp_path)
    invocation_context = context()
    accepted = await gateway.invoke(
        "process.execute",
        {
            "command": "python",
            "args": ["-c", "import time;time.sleep(.2);print('background result')"],
            "background": True,
        },
        invocation_context,
    )

    assert accepted.status is CapabilityResultStatus.ACCEPTED
    correlation = str(invocation_context.invocation_id)
    assert accepted.metadata.root["result_correlation_id"] == correlation
    first = await gateway.progress(invocation_context)
    second = await gateway.progress(invocation_context)
    assert (first.sequence, second.sequence) == (1, 2)
    assert first.metadata.root["result_correlation_id"] == correlation

    result = await gateway.wait(invocation_context)
    assert result.status is CapabilityResultStatus.COMPLETED
    assert observation(result)["stdout"] == "background result\n"
    assert result.metadata.root["background"] is True
    assert result.metadata.root["result_correlation_id"] == correlation
    with pytest.raises(AnbanError) as repeated:
        await gateway.wait(invocation_context)
    assert repeated.value.info.code is ErrorCode.CAPABILITY_UNAVAILABLE


async def test_background_process_cancel_and_timeout_are_real_terminal_results(
    tmp_path: Path,
) -> None:
    gateway = registry(tmp_path)
    cancelled_context = context()
    accepted = await gateway.invoke(
        "process.execute",
        {
            "command": "python",
            "args": ["-c", "import time;time.sleep(5)"],
            "background": True,
        },
        cancelled_context,
    )
    assert accepted.status is CapabilityResultStatus.ACCEPTED
    await gateway.cancel(cancelled_context)
    cancelled = await gateway.wait(cancelled_context)
    assert cancelled.status is CapabilityResultStatus.CANCELLED

    timeout_context = context()
    accepted = await gateway.invoke(
        "process.execute",
        {
            "command": "python",
            "args": ["-c", "import time;time.sleep(5)"],
            "timeout": 1,
            "background": True,
        },
        timeout_context,
    )
    assert accepted.status is CapabilityResultStatus.ACCEPTED
    timed_out = await gateway.wait(timeout_context)
    assert timed_out.status is CapabilityResultStatus.TIMED_OUT


async def test_background_lifecycle_rejects_non_authoritative_context(tmp_path: Path) -> None:
    gateway = registry(tmp_path)
    authoritative = context()
    accepted = await gateway.invoke(
        "process.execute",
        {
            "command": "python",
            "args": ["-c", "import time;time.sleep(5)"],
            "background": True,
        },
        authoritative,
    )
    assert accepted.status is CapabilityResultStatus.ACCEPTED
    mismatched = authoritative.model_copy(update={"run_id": new_execution_run_id()})
    with pytest.raises(AnbanError) as failure:
        await gateway.progress(mismatched)
    assert failure.value.info.code is ErrorCode.CAPABILITY_ARGUMENTS_INVALID
    await gateway.cancel(authoritative)
    assert (await gateway.wait(authoritative)).status is CapabilityResultStatus.CANCELLED


async def test_single_and_multiple_declared_artifacts_are_snapshotted(tmp_path: Path) -> None:
    invocation_context = context()
    result = await registry(tmp_path).invoke(
        "process.execute",
        {
            "command": "python",
            "args": [
                "-c",
                "from pathlib import Path;Path('one.txt').write_text('one');"
                "Path('two.bin').write_bytes(b'two')",
            ],
            "artifacts": [
                {"path": "one.txt", "media_type": "text/plain"},
                {"path": "two.bin"},
            ],
        },
        invocation_context,
    )

    assert result.status is CapabilityResultStatus.COMPLETED
    assert [item.media_type for item in result.artifacts] == [
        "text/plain",
        "application/octet-stream",
    ]
    assert [item.sha256 for item in result.artifacts] == [
        hashlib.sha256(b"one").hexdigest(),
        hashlib.sha256(b"two").hexdigest(),
    ]
    artifact_root = tmp_path / "artifacts" / str(invocation_context.run_id)
    assert [(artifact_root / str(item.id)).read_bytes() for item in result.artifacts] == [
        b"one",
        b"two",
    ]


async def test_declared_artifact_accepts_bounded_media_type_parameters(tmp_path: Path) -> None:
    result = await registry(tmp_path).invoke(
        "process.execute",
        {
            "command": "python",
            "args": ["-c", "from pathlib import Path;Path('result.txt').write_text('ok')"],
            "artifacts": [
                {
                    "path": "result.txt",
                    "media_type": 'text/plain; charset="utf-8"; format=flowed',
                }
            ],
        },
        context(),
    )

    assert result.status is CapabilityResultStatus.COMPLETED
    assert result.artifacts[0].media_type == 'text/plain; charset="utf-8"; format=flowed'


@pytest.mark.parametrize(
    "media_type",
    (
        "text/plain; charset",
        "text/plain; charset=",
        "text/plain\r\ncontent-type: application/json",
        'text/plain; charset="unterminated',
    ),
)
async def test_declared_artifact_rejects_malformed_media_type_parameters(
    tmp_path: Path, media_type: str
) -> None:
    with pytest.raises(AnbanError) as failure:
        await registry(tmp_path).invoke(
            "process.execute",
            {
                "command": "python",
                "args": ["-c", "from pathlib import Path;Path('result.txt').write_text('ok')"],
                "artifacts": [{"path": "result.txt", "media_type": media_type}],
            },
            context(),
        )

    assert failure.value.info.code is ErrorCode.CAPABILITY_ARGUMENTS_INVALID
    assert failure.value.info.details.root["reason"] == "artifact_invalid"


async def test_missing_or_oversized_artifact_fails_without_partial_snapshot(
    tmp_path: Path,
) -> None:
    cases: tuple[tuple[list[JsonValue], int], ...] = (
        ([{"path": "created.txt"}, {"path": "missing.txt"}], 1024),
        ([{"path": "created.txt"}], 2),
    )
    for declarations, limit in cases:
        invocation_context = context()
        result = await registry(tmp_path, artifact_max_bytes=limit).invoke(
            "process.execute",
            {
                "command": "python",
                "args": ["-c", "open('created.txt','w').write('data')"],
                "artifacts": declarations,
            },
            invocation_context,
        )
        assert result.status is CapabilityResultStatus.FAILED
        assert result.error is not None
        assert result.error.details.root["reason"] == "artifact_collection_failed"
        assert not (tmp_path / "artifacts" / str(invocation_context.run_id)).exists()


async def test_duplicate_declared_artifact_path_fails_without_snapshot(tmp_path: Path) -> None:
    invocation_context = context()
    result = await registry(tmp_path).invoke(
        "process.execute",
        {
            "command": "python",
            "args": ["-c", "open('same.txt','w').write('data')"],
            "artifacts": [
                {"path": "same.txt", "media_type": "text/plain"},
                {"path": str(tmp_path / "same.txt")},
            ],
        },
        invocation_context,
    )

    assert result.status is CapabilityResultStatus.FAILED
    assert result.error is not None
    assert result.error.details.root["reason"] == "artifact_collection_failed"
    assert not (tmp_path / "artifacts" / str(invocation_context.run_id)).exists()


async def test_unreadable_declared_artifact_fails_without_snapshot(tmp_path: Path) -> None:
    invocation_context = context()
    result = await registry(tmp_path).invoke(
        "process.execute",
        {
            "command": "python",
            "args": [
                "-c",
                "import os;open('unreadable.txt','w').write('data');os.chmod('unreadable.txt',0)",
            ],
            "artifacts": [{"path": "unreadable.txt"}],
        },
        invocation_context,
    )
    (tmp_path / "unreadable.txt").chmod(0o600)

    assert result.status is CapabilityResultStatus.FAILED
    assert result.error is not None
    assert result.error.details.root["reason"] == "artifact_collection_failed"
    assert not (tmp_path / "artifacts" / str(invocation_context.run_id)).exists()


async def test_snapshot_failure_cleans_current_and_previous_artifact_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary = WorkspaceBoundary(tmp_path)
    original = boundary.create_artifact
    created = 0

    def fail_second_snapshot(
        invocation_context: InvocationContext, content: bytes, media_type: str
    ) -> ArtifactReference:
        nonlocal created
        created += 1
        if created == 2:
            raise OSError("test-only snapshot failure")
        return original(invocation_context, content, media_type)

    monkeypatch.setattr(boundary, "create_artifact", fail_second_snapshot)
    gateway = CapabilityRegistry((ProcessCapability(boundary),))
    invocation_context = context()
    result = await gateway.invoke(
        "process.execute",
        {
            "command": "python",
            "args": [
                "-c",
                "open('one.txt','w').write('one');open('two.txt','w').write('two')",
            ],
            "artifacts": [{"path": "one.txt"}, {"path": "two.txt"}],
        },
        invocation_context,
    )

    assert result.status is CapabilityResultStatus.FAILED
    artifact_root = tmp_path / "artifacts" / str(invocation_context.run_id)
    assert not artifact_root.exists() or not any(artifact_root.iterdir())


def test_workspace_snapshot_write_failure_removes_partial_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boundary = WorkspaceBoundary(tmp_path)
    invocation_context = context()
    original = Path.write_bytes

    def partial_write(target: Path, content: bytes) -> int:
        original(target, content[:1])
        raise OSError("test-only partial write")

    monkeypatch.setattr(Path, "write_bytes", partial_write)
    with pytest.raises(OSError):
        boundary.create_artifact(invocation_context, b"content", "text/plain")

    artifact_root = tmp_path / "artifacts" / str(invocation_context.run_id)
    assert artifact_root.exists()
    assert not any(artifact_root.iterdir())


async def test_failed_process_does_not_collect_declared_artifact(tmp_path: Path) -> None:
    invocation_context = context()
    result = await registry(tmp_path).invoke(
        "process.execute",
        {
            "command": "python",
            "args": ["-c", "open('result.txt','w').write('data');raise SystemExit(2)"],
            "artifacts": [{"path": "result.txt"}],
        },
        invocation_context,
    )
    assert result.status is CapabilityResultStatus.FAILED
    assert not (tmp_path / "artifacts" / str(invocation_context.run_id)).exists()


async def test_sensitive_output_and_artifact_fail_without_persisting_value(
    tmp_path: Path,
) -> None:
    secret = "gate-secret-canary-value"
    gateway = local_capability_registry(workspace_root=tmp_path, protected_values=(secret,))
    output_context = context()
    output_result = await gateway.invoke(
        "process.execute",
        {"command": "python", "args": ["-c", f"print({secret!r})"]},
        output_context,
    )
    artifact_context = context()
    artifact_result = await gateway.invoke(
        "process.execute",
        {
            "command": "python",
            "args": ["-c", f"open('unsafe.txt','w').write({secret!r})"],
            "artifacts": [{"path": "unsafe.txt"}],
        },
        artifact_context,
    )

    assert output_result.status is CapabilityResultStatus.FAILED
    assert output_result.observation is None
    assert artifact_result.status is CapabilityResultStatus.FAILED
    assert secret not in str(output_result.model_dump(mode="json"))
    assert secret not in str(artifact_result.model_dump(mode="json"))
    assert not (tmp_path / "artifacts" / str(artifact_context.run_id)).exists()


def test_workspace_root_cannot_overlap_repository() -> None:
    from scripts.workspace_bootstrap import REPOSITORY

    with pytest.raises(ValueError, match="overlaps"):
        local_capability_registry(workspace_root=REPOSITORY)
