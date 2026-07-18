"""Real process-exit recovery for the ordinary Process Capability Registry path."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from anban.capability import CapabilityResultStatus, InvocationContext
from anban.core.errors import AnbanError
from tests.capability.test_process_capability import context, observation, registry

_STARTER = """
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from anban.capability import CapabilityRegistry, InvocationContext
from anban.capability.process import ProcessCapability
from anban.capability.workspace import WorkspaceBoundary
from anban.core.ids import new_capability_invocation_id, new_execution_run_id, new_node_run_id

async def main():
    context = InvocationContext(
        run_id=new_execution_run_id(),
        node_run_id=new_node_run_id(),
        invocation_id=new_capability_invocation_id(),
        deadline_at=datetime.now(UTC) + timedelta(seconds=10),
    )
    gateway = CapabilityRegistry((ProcessCapability(WorkspaceBoundary(Path(sys.argv[1]))),))
    await gateway.invoke(
        "process.execute",
        {
            "command": sys.executable,
            "args": ["-c", "import time;time.sleep(.4);print('after service exit')"],
            "background": True,
        },
        context,
    )
    print(context.model_dump_json(), flush=True)

asyncio.run(main())
"""


async def test_real_service_process_exit_then_fresh_registry_resume(tmp_path: Path) -> None:
    starter = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _STARTER,
        str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await starter.communicate()
    assert starter.returncode == 0
    context = InvocationContext.model_validate_json(stdout)

    restarted = registry(tmp_path)
    await restarted.restore("process.execute", context, 0)
    result = await restarted.wait(context)

    assert result.status is CapabilityResultStatus.COMPLETED
    assert observation(result)["stdout"] == "after service exit\n"
    assert result.metadata.root["restart_recoverable"] is True


async def test_worker_exit_without_result_fails_without_unbounded_wait(tmp_path: Path) -> None:
    invocation = context(seconds=5)
    gateway = registry(tmp_path)
    accepted = await gateway.invoke(
        "process.execute",
        {
            "command": sys.executable,
            "args": ["-c", "import time;time.sleep(4)"],
            "background": True,
        },
        invocation,
    )
    assert accepted.status is CapabilityResultStatus.ACCEPTED
    state_path = tmp_path / ".anban" / "process" / str(invocation.invocation_id) / "started.json"
    state = json.loads(state_path.read_text())
    worker_pid = state["worker_pid"]
    assert isinstance(worker_pid, int)
    os.kill(worker_pid, signal.SIGKILL)

    with pytest.raises(AnbanError) as captured:
        await asyncio.wait_for(gateway.wait(invocation), timeout=1)

    assert captured.value.info.details.root["reason"] == "worker_exited_without_result"


async def test_wait_rechecks_result_after_observing_worker_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation = context(seconds=5)
    gateway = registry(tmp_path)
    accepted = await gateway.invoke(
        "process.execute",
        {
            "command": sys.executable,
            "args": ["-c", "print('published before exit')"],
            "background": True,
        },
        invocation,
    )
    assert accepted.status is CapabilityResultStatus.ACCEPTED

    result_path = tmp_path / ".anban" / "process" / str(invocation.invocation_id) / "result.json"
    while not result_path.is_file():
        await asyncio.sleep(0.01)
    original_is_file = Path.is_file
    result_checks = 0

    def hide_first_result(path: Path) -> bool:
        nonlocal result_checks
        if path == result_path:
            result_checks += 1
        if path == result_path and result_checks == 1:
            return False
        return original_is_file(path)

    def worker_has_exited(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(Path, "is_file", hide_first_result)
    monkeypatch.setattr(os, "kill", worker_has_exited)

    result = await gateway.wait(invocation)

    assert result.status is CapabilityResultStatus.COMPLETED
    assert observation(result)["stdout"] == "published before exit\n"
    assert result_checks >= 2
