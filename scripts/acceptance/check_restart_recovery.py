"""Real CLI service-exit and PostgreSQL recovery acceptance for D21."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from typing import cast
from uuid import UUID

from anban.application import build_query_application
from anban.config import load_configuration
from anban.core.ids import ExecutionRunId
from scripts.acceptance.check_cli_e2e import isolated_environment, prepare_workspace
from scripts.workspace_bootstrap import resolve_workspace


class RestartAcceptanceError(RuntimeError):
    """Safe acceptance failure without provider output or physical paths."""


async def cli(*arguments: str, timeout: float) -> tuple[int, list[dict[str, object]]]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "anban.cli",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RestartAcceptanceError("CLI process timed out") from None
    payloads: list[dict[str, object]] = []
    for line in stdout.decode("utf-8").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payloads.append(cast(dict[str, object], payload))
    return process.returncode or 0, payloads


async def accept_restart() -> dict[str, object]:
    source = load_configuration(workspace=resolve_workspace().path)
    marker = hashlib.sha256(os.urandom(32)).hexdigest()[:12]
    workspace = prepare_workspace(source.workspace / "tmp", f"d21-restart-{marker}")
    count_name = f"restart-count-{marker}.txt"
    with isolated_environment(workspace, source):
        start_code, start_payloads = await cli(
            "run",
            (
                "Use exactly one process.execute Tool Call with command=python, background=true, "
                f"and one declared text/plain Artifact at path {count_name}. Pass a Python -c "
                "program that sleeps for five seconds, reads that relative Workspace file if it "
                "exists, treats a missing file as integer zero, increments the integer once, and "
                "writes it back. Use cwd=. and do not add environment overrides, stdin, another "
                "Artifact, or another process call. Do not claim success before the real result "
                "is available."
            ),
            "--async",
            "--detach",
            "--json",
            timeout=120,
        )
        if start_code != 0 or not start_payloads:
            raise RestartAcceptanceError("detached CLI start failed")
        waiting = start_payloads[-1]
        checkpoint_id = waiting.get("checkpoint_id")
        run_id = waiting.get("run_id")
        invocation_id = waiting.get("invocation_id")
        if not all(isinstance(value, str) for value in (checkpoint_id, run_id, invocation_id)):
            raise RestartAcceptanceError("detached CLI did not return durable identities")
        state = workspace / ".anban" / "process" / str(invocation_id)
        if not (state / "started.json").is_file() or (state / "result.json").exists():
            raise RestartAcceptanceError("service did not exit while real work was non-terminal")

        resume_code, resume_payloads = await cli(
            "run",
            "resume",
            str(checkpoint_id),
            "--json",
            timeout=180,
        )
        if resume_code != 0 or not resume_payloads:
            raise RestartAcceptanceError("fresh CLI resume failed")
        terminal = resume_payloads[-1]
        if terminal.get("status") != "succeeded" or terminal.get("run_id") != run_id:
            raise RestartAcceptanceError("recovered CLI terminal was not successful")
        count_path = workspace / count_name
        if not count_path.is_file() or count_path.read_text(encoding="utf-8").strip() != "1":
            raise RestartAcceptanceError("recovered side effect count was not exactly one")

        application = await build_query_application()
        try:
            detail = await application.interactions.show_run(ExecutionRunId(UUID(str(run_id))))
        finally:
            await application.close()
        event_types = [entry.event_type for entry in detail.observability.trace]
        recovery = tuple(
            entry
            for entry in detail.observability.audit
            if entry.event_type.startswith("run.recovery_")
        )
        recovery_completed = next(
            (entry for entry in recovery if entry.event_type == "run.recovery_completed"),
            None,
        )
        capability_completed = next(
            (
                entry
                for entry in detail.observability.audit
                if entry.event_type == "capability.completed"
                and str(entry.invocation_id) == invocation_id
            ),
            None,
        )
        if (
            detail.run.status.value != "succeeded"
            or not detail.observability.complete
            or detail.observability.inconsistencies
            or event_types.count("run.recovery_started") != 1
            or event_types.count("run.recovery_completed") != 1
            or "run.recovery_failed" in event_types
            or len(detail.checkpoints) != 1
            or detail.checkpoints[0].status.value != "completed"
            or len(detail.artifacts) != 1
            or len(detail.invocations) != 1
            or capability_completed is None
            or capability_completed.metadata.root.get("restart_recoverable") is not True
            or recovery_completed is None
            or recovery_completed.metadata.root.get("side_effect_replayed") is not False
        ):
            raise RestartAcceptanceError("durable recovery evidence did not reconcile")
        return {
            "run_id": run_id,
            "invocation_id": invocation_id,
            "checkpoint_id": checkpoint_id,
            "artifact_id": str(detail.artifacts[0].id),
            "recovery_event_count": len(recovery),
        }


def main() -> int:
    try:
        evidence = asyncio.run(accept_restart())
    except Exception as exc:
        print(f"restart recovery acceptance: FAIL ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(
        "restart recovery acceptance: PASS "
        + json.dumps(evidence, ensure_ascii=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
