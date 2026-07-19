"""Real D31 authenticated HTTP Webhook and restart acceptance."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx

from anban.application import build_query_application
from anban.config import load_configuration
from anban.core.ids import ExecutionRunId
from anban.interaction import webhook_signature
from scripts.acceptance.check_cli_e2e import isolated_environment, prepare_workspace
from scripts.acceptance.check_interaction_updates import (
    WaitingIdentity,
    aggregate,
    context_entries,
    query,
    start_detached,
)
from scripts.workspace_bootstrap import resolve_workspace


class WebhookAcceptanceError(RuntimeError):
    """Safe failure without request bodies, Provider output, secrets, or physical paths."""


@dataclass(frozen=True)
class HttpResult:
    status_code: int
    payload: dict[str, object]


@dataclass
class WebhookServer:
    process: asyncio.subprocess.Process
    base_url: str

    async def stop(self) -> None:
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=15)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()


def configure_endpoint(workspace: Path, secret: str) -> None:
    configuration = workspace / "anban.toml"
    configuration.write_text(
        configuration.read_text(encoding="utf-8")
        + """
[[interaction.webhook.endpoints]]
name = "acceptance"
secret_env = "ANBAN_ACCEPTANCE_WEBHOOK_SECRET"
""",
        encoding="utf-8",
    )
    secrets = workspace / "secrets.env"
    secrets.write_text(
        f"ANBAN_ACCEPTANCE_WEBHOOK_SECRET={secret}\n",
        encoding="utf-8",
    )
    os.chmod(secrets, 0o600)


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return cast(int, listener.getsockname()[1])


async def start_server(port: int) -> WebhookServer:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "anban.cli",
        "webhook",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    server = WebhookServer(process, f"http://127.0.0.1:{port}")
    async with httpx.AsyncClient(timeout=2) as client:
        for _ in range(150):
            if process.returncode is not None:
                raise WebhookAcceptanceError("Webhook server exited during startup")
            try:
                response = await client.get(server.base_url + "/health")
            except httpx.HTTPError:
                await asyncio.sleep(0.1)
                continue
            if response.status_code == 200:
                return server
            await asyncio.sleep(0.1)
    await server.stop()
    raise WebhookAcceptanceError("Webhook server did not become ready")


def encoded_payload(
    content: str,
    *,
    resume: WaitingIdentity | None = None,
) -> bytes:
    payload: dict[str, object] = {"content": content}
    if resume is not None:
        payload.update(
            {
                "route": "resume_eligible_run",
                "resume_key": {
                    "namespace": resume.namespace,
                    "value": resume.correlation,
                },
            }
        )
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()


async def deliver(
    server: WebhookServer,
    secret: str,
    event_id: str,
    body: bytes,
    *,
    endpoint: str = "acceptance",
    timestamp: int | None = None,
    signature_override: str | None = None,
) -> HttpResult:
    seconds = str(int(time.time()) if timestamp is None else timestamp)
    signature = signature_override or webhook_signature(secret, endpoint, event_id, seconds, body)
    async with httpx.AsyncClient(timeout=360) as client:
        response = await client.post(
            f"{server.base_url}/webhooks/{endpoint}",
            content=body,
            headers={
                "content-type": "application/json",
                "x-anban-event-id": event_id,
                "x-anban-timestamp": seconds,
                "x-anban-signature": signature,
            },
        )
    try:
        payload = response.json()
    except json.JSONDecodeError:
        raise WebhookAcceptanceError("Webhook response was not bounded JSON") from None
    if not isinstance(payload, dict):
        raise WebhookAcceptanceError("Webhook response shape was invalid")
    return HttpResult(response.status_code, cast(dict[str, object], payload))


async def inbox_for_run(run_id: ExecutionRunId):
    application = await build_query_application()
    try:
        return tuple(
            entry for entry in await application.interactions.inbox(100) if entry.run_id == run_id
        )
    finally:
        await application.close()


async def validate_new_event(
    server: WebhookServer,
    secret: str,
    marker: str,
    label: str,
    instruction: str,
) -> tuple[dict[str, object], str, bytes, ExecutionRunId]:
    event_id = f"{label}-{marker}"
    body = encoded_payload(
        "Treat this authenticated external event as a new bounded task. "
        f"{instruction} Dynamic event object: {marker}-{label}."
    )
    response = await deliver(server, secret, event_id, body)
    run_value = response.payload.get("run_id")
    if (
        response.status_code != 200
        or response.payload.get("status") != "succeeded"
        or not isinstance(run_value, str)
    ):
        raise WebhookAcceptanceError("authenticated new event did not complete")
    run_id = ExecutionRunId(UUID(run_value))
    detail = await query(run_value)
    state = await aggregate(run_value)
    inbox = await inbox_for_run(run_id)
    authenticated = tuple(
        event for event in detail.observability.audit if event.event_type == "webhook.authenticated"
    )
    routed = tuple(
        event for event in detail.observability.audit if event.event_type == "interaction.routed"
    )
    if (
        detail.run.status.value != "succeeded"
        or not detail.observability.complete
        or detail.observability.inconsistencies
        or len(authenticated) != 1
        or len(routed) != 1
        or authenticated[0].sequence >= routed[0].sequence
        or authenticated[0].metadata.root.get("webhook_endpoint") != "acceptance"
        or authenticated[0].metadata.root.get("webhook_authenticated") is not True
        or authenticated[0].metadata.root.get("webhook_auth_version") != "v1"
        or not isinstance(authenticated[0].metadata.root.get("webhook_event_hash"), str)
        or routed[0].metadata.root.get("input_kind") != "webhook_event"
        or routed[0].metadata.root.get("interaction_route") != "new_task"
        or routed[0].metadata.root.get("source") != "webhook.acceptance"
        or len(inbox) != 1
        or inbox[0].status.value != "processed"
        or inbox[0].input_kind != "webhook_event"
        or inbox[0].delivery_count != 1
        or event_id in str(state.events)
        or secret in str(state.events)
    ):
        raise WebhookAcceptanceError("new Webhook persistence or Audit did not reconcile")
    return (
        {"label": label, "run_id": run_value, "deliveries": 1},
        event_id,
        body,
        run_id,
    )


async def validate_restart_replay(
    server: WebhookServer,
    secret: str,
    event_id: str,
    body: bytes,
    run_id: ExecutionRunId,
) -> int:
    before = await run_ids()
    response = await deliver(server, secret, event_id, body)
    inbox = await inbox_for_run(run_id)
    if (
        response.status_code != 200
        or response.payload.get("run_id") != str(run_id)
        or before != await run_ids()
        or len(inbox) != 1
        or inbox[0].delivery_count != 2
        or inbox[0].last_disposition.value != "deduplicated"
    ):
        raise WebhookAcceptanceError("Webhook replay after restart created or replayed work")
    return inbox[0].delivery_count


async def run_ids() -> tuple[str, ...]:
    application = await build_query_application()
    try:
        return tuple(str(run.id) for run in await application.interactions.runs(100))
    finally:
        await application.close()


async def validate_resume_event(
    server: WebhookServer,
    secret: str,
    marker: str,
) -> dict[str, object]:
    count_name = f"d31-webhook-resume-{marker}.txt"
    waiting = await start_detached(
        "Complete one bounded background operation and report its real result. Use exactly one "
        "process.execute call with command=python, background=true, cwd=., and no stdin or "
        "environment override. Pass a Python -c program that sleeps four seconds, writes the "
        f"integer 1 to the relative Workspace file {count_name}, prints 1, and declares that "
        "file as one text/plain Artifact. Do not report completion before the result is ready."
    )
    event_id = f"resume-{marker}"
    update = "Apply this authenticated event as context and report the completed result concisely."
    response = await deliver(
        server,
        secret,
        event_id,
        encoded_payload(update, resume=waiting),
    )
    if response.status_code != 200 or response.payload.get("run_id") != waiting.run_id:
        raise WebhookAcceptanceError("authenticated Webhook did not resume the waiting Run")
    detail = await query(waiting.run_id)
    state = await aggregate(waiting.run_id)
    entries = await context_entries(waiting.task_id)
    inbox = await inbox_for_run(ExecutionRunId(UUID(waiting.run_id)))
    event_types = tuple(event.event_type for event in detail.observability.audit)
    count_file = load_configuration().workspace / count_name
    webhook_entries = tuple(
        entry for entry in entries if entry.metadata.root.get("input_kind") == "webhook_event"
    )
    if (
        response.payload.get("status") != "succeeded"
        or detail.run.status.value != "succeeded"
        or not detail.observability.complete
        or detail.observability.inconsistencies
        or not count_file.is_file()
        or count_file.read_text(encoding="utf-8").strip() != "1"
        or len(detail.invocations) != 1
        or detail.invocations[0].capability_name != "process.execute"
        or detail.invocations[0].status.value != "succeeded"
        or len(detail.artifacts) != 1
        or len(detail.checkpoints) != 1
        or detail.checkpoints[0].status.value != "completed"
        or len(webhook_entries) != 1
        or webhook_entries[0].content != update
        or event_types.count("webhook.authenticated") != 1
        or event_types.count("interaction.update_received") != 1
        or event_types.count("interaction.context_applied") != 1
        or event_types.count("run.recovery_completed") != 1
        or len(inbox) != 2
        or any(entry.status.value != "processed" for entry in inbox)
        or waiting.correlation in str(state.events)
        or event_id in str(state.events)
        or secret in str(state.events)
    ):
        raise WebhookAcceptanceError("Webhook resume persistence or side effect did not reconcile")
    return {
        "label": "resume",
        "run_id": waiting.run_id,
        "artifact_count": len(detail.artifacts),
    }


async def reverse_cases(
    server: WebhookServer,
    secret: str,
    marker: str,
    replay_event_id: str,
    replay_run_id: ExecutionRunId,
) -> dict[str, str]:
    before = await run_ids()
    application = await build_query_application()
    try:
        before_inbox = {entry.interaction_id for entry in await application.interactions.inbox(100)}
    finally:
        await application.close()
    bad_signature = await deliver(
        server,
        secret,
        f"bad-auth-{marker}",
        encoded_payload("This unauthenticated event must not execute."),
        signature_override="v1=" + "0" * 64,
    )
    stale = await deliver(
        server,
        secret,
        f"stale-{marker}",
        encoded_payload("This stale event must not execute."),
        timestamp=1,
    )
    unknown_endpoint = await deliver(
        server,
        secret,
        f"endpoint-{marker}",
        encoded_payload("This unknown endpoint must not execute."),
        endpoint="missing",
    )
    conflict = await deliver(
        server,
        secret,
        replay_event_id,
        encoded_payload("Changed content must conflict with the original event identity."),
    )
    inbox = await inbox_for_run(replay_run_id)
    if (
        [
            bad_signature.status_code,
            stale.status_code,
            unknown_endpoint.status_code,
            conflict.status_code,
        ]
        != [401, 401, 404, 409]
        or before != await run_ids()
        or len(inbox) != 1
        or inbox[0].delivery_count != 3
        or inbox[0].last_disposition.value != "conflicting"
    ):
        raise WebhookAcceptanceError("Webhook authentication or replay reverse cases failed")

    unknown_body = encoded_payload(
        "An authenticated unknown resume event must fail durably.",
        resume=WaitingIdentity(
            run_id=str(UUID(int=0)),
            task_id=str(UUID(int=0)),
            checkpoint_id=str(UUID(int=0)),
            namespace="anban.continuation",
            correlation=f"unknown-{marker}",
        ),
    )
    unknown = await deliver(server, secret, f"unknown-resume-{marker}", unknown_body)
    if unknown.status_code != 404 or unknown.payload.get("reason") != "unknown":
        raise WebhookAcceptanceError("authenticated unknown resume did not fail explicitly")
    application = await build_query_application()
    try:
        entries = await application.interactions.inbox(100)
    finally:
        await application.close()
    new_entries = tuple(entry for entry in entries if entry.interaction_id not in before_inbox)
    if (
        len(new_entries) != 1
        or new_entries[0].input_kind != "webhook_event"
        or new_entries[0].status.value != "rejected"
        or new_entries[0].failure_reason != "unknown"
        or new_entries[0].run_id is not None
    ):
        raise WebhookAcceptanceError("authenticated failure was not durable")
    return {
        "bad_signature": "rejected",
        "stale": "rejected",
        "unknown_endpoint": "rejected",
        "conflict": "rejected",
        "unknown_resume": "rejected",
    }


async def accept_webhooks() -> dict[str, object]:
    source = load_configuration(workspace=resolve_workspace().path)
    marker = hashlib.sha256(os.urandom(32)).hexdigest()[:12]
    secret = hashlib.sha256(os.urandom(64)).hexdigest()
    workspace = prepare_workspace(source.workspace / "tmp", f"d31-webhook-{marker}")
    configure_endpoint(workspace, secret)
    port = available_port()
    with isolated_environment(workspace, source):
        server = await start_server(port)
        try:
            first, _, _, _ = await validate_new_event(
                server,
                secret,
                marker,
                "acknowledgement",
                "Return a concise acknowledgement of the supplied dynamic object.",
            )
            second, replay_id, replay_body, replay_run = await validate_new_event(
                server,
                secret,
                marker,
                "classification",
                "Classify the supplied dynamic object as received and explain that decision.",
            )
        finally:
            await server.stop()
        restarted = await start_server(port)
        try:
            second["deliveries"] = await validate_restart_replay(
                restarted, secret, replay_id, replay_body, replay_run
            )
            resumed = await validate_resume_event(restarted, secret, marker)
            reverse = await reverse_cases(
                restarted,
                secret,
                marker,
                replay_id,
                replay_run,
            )
        finally:
            await restarted.stop()
    return {
        "variants": [first, second, resumed],
        "reverse": reverse,
        "http_server_restarted": True,
        "side_effect_replayed": False,
        "scenarios": ["S01", "S02", "S03", "S04", "S08", "S09", "S10", "S11"],
    }


def main() -> int:
    try:
        evidence = asyncio.run(accept_webhooks())
    except Exception as exc:
        detail = str(exc) if isinstance(exc, WebhookAcceptanceError) else "unexpected"
        print(f"Webhook acceptance: FAIL ({type(exc).__name__}: {detail})", file=sys.stderr)
        return 1
    print(
        "Webhook acceptance: PASS " + json.dumps(evidence, ensure_ascii=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
