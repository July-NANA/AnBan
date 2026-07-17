"""Real local production Registry acceptance without a model or network dependency."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from datetime import timedelta

from anban.capability import CapabilityResultStatus, InvocationContext, local_capability_registry
from anban.config import load_configuration
from anban.core.errors import AnbanError
from anban.core.ids import (
    new_capability_invocation_id,
    new_execution_run_id,
    new_node_run_id,
)
from anban.core.models import now_utc


class CapabilityAcceptanceError(RuntimeError):
    """Safe acceptance failure without process output or physical paths."""


def context() -> InvocationContext:
    return InvocationContext(
        run_id=new_execution_run_id(),
        node_run_id=new_node_run_id(),
        invocation_id=new_capability_invocation_id(),
        deadline_at=now_utc() + timedelta(seconds=30),
    )


async def accept_capabilities() -> None:
    configuration = load_configuration()
    registry = local_capability_registry(
        workspace_root=configuration.workspace,
        protected_values=configuration.protected_values(),
    )
    if tuple(item.name for item in registry.search()) != ("process.execute", "skill.activate"):
        raise CapabilityAcceptanceError("production Capability surface mismatch")
    work = configuration.workspace / "tmp" / f"acceptance-{new_execution_run_id()}"
    work.mkdir(mode=0o700)
    invocation = context()
    try:
        completed = await registry.invoke(
            "process.execute",
            {
                "command": sys.executable,
                "cwd": str(work),
                "env": [{"name": "ANBAN_ACCEPTANCE_VALUE", "value": "overridden"}],
                "stdin": "stdin-value",
                "args": [
                    "-c",
                    "import os,sys;from pathlib import Path;"
                    "Path('result.txt').write_text(sys.stdin.read()+'-'+os.environ['ANBAN_ACCEPTANCE_VALUE']);"
                    "print('completed')",
                ],
                "artifacts": [{"path": "result.txt", "media_type": "text/plain"}],
            },
            invocation,
        )
        if (
            completed.status is not CapabilityResultStatus.COMPLETED
            or len(completed.artifacts) != 1
        ):
            raise CapabilityAcceptanceError("real Process or Artifact did not complete")
        payload = json.loads(completed.observation or "{}")
        if payload.get("stdout") != "completed\n":
            raise CapabilityAcceptanceError("real Process output mismatch")
        artifact = completed.artifacts[0]
        snapshot = configuration.workspace / "artifacts" / str(invocation.run_id) / str(artifact.id)
        if snapshot.read_text(encoding="utf-8") != "stdin-value-overridden":
            raise CapabilityAcceptanceError("Artifact snapshot mismatch")

        nonzero = await registry.invoke(
            "process.execute",
            {"command": sys.executable, "args": ["-c", "raise SystemExit(9)"]},
            context(),
        )
        missing_artifact = await registry.invoke(
            "process.execute",
            {
                "command": sys.executable,
                "args": ["-c", "pass"],
                "artifacts": [{"path": "missing.txt"}],
            },
            context(),
        )
        if (
            nonzero.status is not CapabilityResultStatus.FAILED
            or missing_artifact.status is not CapabilityResultStatus.FAILED
        ):
            raise CapabilityAcceptanceError("Process failure paths returned success")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(
            configuration.workspace / "artifacts" / str(invocation.run_id),
            ignore_errors=True,
        )


def main() -> int:
    try:
        asyncio.run(accept_capabilities())
    except AnbanError as exc:
        print(f"local Capability acceptance: FAIL [{exc.info.code.value}]", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"local Capability acceptance: FAIL ({type(exc).__name__})", file=sys.stderr)
        return 1
    print("local Capability acceptance: PASS - surface, process, stdin, env, Artifact, failures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
