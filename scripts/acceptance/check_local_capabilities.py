"""Real acceptance for production file and process Capability wiring."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from anban.capability import CapabilityResultStatus, InvocationContext
from anban.capability.local import local_capability_registry
from anban.core.errors import AnbanError
from anban.core.ids import (
    new_capability_invocation_id,
    new_execution_run_id,
    new_node_run_id,
)
from scripts.workspace_bootstrap import WorkspaceResolutionError, resolve_workspace


class CapabilityAcceptanceError(RuntimeError):
    """Safe failure without physical paths or process output."""


def next_context(context: InvocationContext) -> InvocationContext:
    return context.model_copy(update={"invocation_id": new_capability_invocation_id()})


async def accept_local_capabilities(workspace: Path) -> None:
    registry = local_capability_registry(
        workspace_root=workspace,
        allowed_executables={"python": Path(sys.executable)},
        environment={"PYTHONUTF8": "1"},
    )
    context = InvocationContext(
        run_id=new_execution_run_id(),
        node_run_id=new_node_run_id(),
        invocation_id=new_capability_invocation_id(),
        deadline_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    run_storage = workspace / "runs" / str(context.run_id)
    artifact_storage = workspace / "artifacts" / str(context.run_id)
    try:
        expected = ("file.list", "file.read", "file.write", "process.execute")
        if tuple(item.name for item in registry.search()) != expected:
            raise CapabilityAcceptanceError("registered Capability set mismatch")
        content = "real governed Capability acceptance"
        written = await registry.invoke(
            "file.write",
            {"path": "acceptance/result.txt", "content": content},
            context,
        )
        if written.status is not CapabilityResultStatus.COMPLETED or len(written.artifacts) != 1:
            raise CapabilityAcceptanceError("real file write failed")
        artifact = written.artifacts[0]
        artifact_file = artifact_storage / str(artifact.id)
        if (
            not artifact_file.is_file()
            or artifact_file.read_text(encoding="utf-8") != content
            or artifact.sha256 != hashlib.sha256(content.encode()).hexdigest()
        ):
            raise CapabilityAcceptanceError("Artifact snapshot mismatch")
        read = await registry.invoke(
            "file.read",
            {"path": "acceptance/result.txt"},
            next_context(context),
        )
        listing = await registry.invoke(
            "file.list",
            {"path": "acceptance"},
            next_context(context),
        )
        process = await registry.invoke(
            "process.execute",
            {"command": "python", "args": ["-c", "print('real governed process')"]},
            next_context(context),
        )
        if read.observation != content or "result.txt" not in (listing.observation or ""):
            raise CapabilityAcceptanceError("real file read or list failed")
        if process.status is not CapabilityResultStatus.COMPLETED:
            raise CapabilityAcceptanceError("real process execution failed")
        if str(workspace) in str(written.model_dump(mode="json")):
            raise CapabilityAcceptanceError("physical Workspace path escaped the adapter")
    finally:
        for target in (run_storage, artifact_storage):
            if target.is_relative_to(workspace) and target.name == str(context.run_id):
                shutil.rmtree(target, ignore_errors=True)


def main() -> int:
    try:
        workspace = resolve_workspace().path
        asyncio.run(accept_local_capabilities(workspace))
    except AnbanError as exc:
        print(f"local Capability acceptance: FAIL [{exc.info.code.value}]", file=sys.stderr)
        return 1
    except WorkspaceResolutionError as exc:
        print(f"local Capability acceptance: FAIL [{exc.code}]", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"local Capability acceptance: FAIL ({type(exc).__name__})", file=sys.stderr)
        return 1
    print("local Capability acceptance: PASS - file, process, bounds, Artifact, safe output")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
