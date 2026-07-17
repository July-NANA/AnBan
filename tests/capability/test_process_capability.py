"""Real subprocess boundary, output, timeout, and cancellation tests."""

from __future__ import annotations

import asyncio
import sys
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


def context(*, seconds: int = 10) -> InvocationContext:
    return InvocationContext(
        run_id=new_execution_run_id(),
        node_run_id=new_node_run_id(),
        invocation_id=new_capability_invocation_id(),
        deadline_at=datetime.now(UTC) + timedelta(seconds=seconds),
    )


def registry(root: Path, *, executable: Path | None = None) -> CapabilityRegistry:
    return local_capability_registry(
        workspace_root=root,
        allowed_executables={"python": Path(sys.executable) if executable is None else executable},
        environment={"PYTHONUTF8": "1"},
    )


async def test_process_executes_without_shell_and_returns_bounded_output(tmp_path: Path) -> None:
    result = await registry(tmp_path).invoke(
        "process.execute",
        {"command": "python", "args": ["-c", "print('real process')"]},
        context(),
    )
    assert result.status is CapabilityResultStatus.COMPLETED
    assert "real process" in (result.observation or "")
    assert str(tmp_path) not in (result.observation or "")


async def test_process_does_not_inherit_secret_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANBAN_CANARY_SECRET", "canary-value")
    result = await registry(tmp_path).invoke(
        "process.execute",
        {
            "command": "python",
            "args": [
                "-c",
                "import os;print(os.getenv('ANBAN_CANARY_SECRET','not-inherited'))",
            ],
        },
        context(),
    )
    assert result.status is CapabilityResultStatus.COMPLETED
    assert "not-inherited" in (result.observation or "")
    assert "canary-value" not in (result.observation or "")


@pytest.mark.parametrize("command", ["unknown", "python"])
async def test_unknown_or_missing_executable_is_structured(tmp_path: Path, command: str) -> None:
    executable = Path(sys.executable) if command == "unknown" else tmp_path / "missing"
    result = await registry(tmp_path, executable=executable).invoke(
        "process.execute",
        {"command": command},
        context(),
    )
    assert result.status is CapabilityResultStatus.FAILED
    assert result.error is not None
    assert result.error.code is ErrorCode.CAPABILITY_UNAVAILABLE


async def test_nonzero_exit_does_not_return_raw_output(tmp_path: Path) -> None:
    canary = "subprocess-canary"
    result = await registry(tmp_path).invoke(
        "process.execute",
        {"command": "python", "args": ["-c", f"print('{canary}');raise SystemExit(2)"]},
        context(),
    )
    assert result.status is CapabilityResultStatus.FAILED
    assert canary not in str(result.model_dump(mode="json"))


async def test_process_timeout_terminates_the_process_group(tmp_path: Path) -> None:
    invocation_context = context()
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
        invocation_context,
    )
    assert result.status is CapabilityResultStatus.TIMED_OUT
    assert result.error is not None
    assert result.error.code is ErrorCode.EXECUTION_TIMED_OUT
    await asyncio.sleep(2)
    marker = tmp_path / "runs" / str(invocation_context.run_id) / "workspace" / "late-child.txt"
    assert not marker.exists()


@pytest.mark.parametrize(
    "program",
    ["print('x'*20000)", "import sys;print('x'*20000,file=sys.stderr)"],
)
async def test_process_output_limit_fails_without_returning_output(
    tmp_path: Path,
    program: str,
) -> None:
    result = await registry(tmp_path).invoke(
        "process.execute",
        {"command": "python", "args": ["-c", program]},
        context(),
    )
    assert result.status is CapabilityResultStatus.FAILED
    assert result.observation is None
    assert result.error is not None
    assert result.error.details.root["reason"] == "output_limit"


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


async def test_process_cwd_cannot_escape_run_workspace(tmp_path: Path) -> None:
    with pytest.raises(AnbanError) as failure:
        await registry(tmp_path).invoke(
            "process.execute",
            {"command": "python", "cwd": ".."},
            context(),
        )
    assert failure.value.info.code is ErrorCode.CAPABILITY_ARGUMENTS_INVALID


def test_process_environment_rejects_non_allowlisted_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-allowlisted"):
        local_capability_registry(
            workspace_root=tmp_path,
            allowed_executables={"python": Path(sys.executable)},
            environment={"ANBAN_CANARY_SECRET": "canary-value"},
        )
