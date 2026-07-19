"""Authenticated, replay-safe Webhook ingestion over the ordinary gateway."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from anban.capability import CapabilityRegistry
from anban.config import WebhookConfiguration, WebhookEndpointConfiguration
from anban.core import AnbanError
from anban.interaction import (
    InteractionService,
    WebhookIngressAdapter,
    create_webhook_http_application,
    webhook_signature,
)
from anban.runtime import ExecutionQueryService, PersistentRuntime
from tests.runtime.memory_uow import MemoryUnitOfWorkFactory
from tests.runtime.test_persistent_runtime import TransactionCheckingModel, final_turn

SECRET = "synthetic-webhook-key-material-123456789"
NOW = datetime(2026, 7, 19, 6, 30, tzinfo=UTC)


def configuration() -> WebhookConfiguration:
    return WebhookConfiguration(
        body_max_bytes=32_768,
        clock_skew_seconds=300,
        endpoints=(WebhookEndpointConfiguration(name="events", secret=SecretStr(SECRET)),),
    )


def request_values(
    body: bytes,
    event_id: str,
    *,
    timestamp: datetime = NOW,
    secret: str = SECRET,
) -> dict[str, str]:
    seconds = str(int(timestamp.timestamp()))
    return {
        "content_type": "application/json",
        "event_id": event_id,
        "timestamp": seconds,
        "signature": webhook_signature(secret, "events", event_id, seconds, body),
    }


async def test_authenticated_event_creates_one_run_and_restart_duplicate_does_not_replay() -> None:
    factory = MemoryUnitOfWorkFactory()
    model = TransactionCheckingModel(factory, [final_turn("Authenticated event completed.")])
    service = InteractionService(
        PersistentRuntime(model, CapabilityRegistry(), factory), unit_of_work=factory
    )
    body = json.dumps(
        {"content": "Handle one authenticated changed event object."}, separators=(",", ":")
    ).encode()
    values = request_values(body, "event-restart-4821")
    first = await WebhookIngressAdapter(configuration(), service, clock=lambda: NOW).deliver(
        "events", body, **values
    )

    restarted = InteractionService(
        PersistentRuntime(model, CapabilityRegistry(), factory), unit_of_work=factory
    )
    duplicate = await WebhookIngressAdapter(
        configuration(), restarted, clock=lambda: NOW + timedelta(seconds=2)
    ).deliver("events", body, **values)

    assert duplicate.run_id == first.run_id
    assert model.calls == 1
    inbox = await restarted.inbox()
    assert len(inbox) == 1
    assert inbox[0].input_kind == "webhook_event"
    assert inbox[0].delivery_count == 2
    assert inbox[0].last_disposition.value == "deduplicated"
    trace = await ExecutionQueryService(factory).trace(first.run_id)
    authenticated = tuple(
        event for event in trace.audit if event.event_type == "webhook.authenticated"
    )
    assert len(authenticated) == 1
    assert authenticated[0].metadata.root["webhook_endpoint"] == "events"
    assert authenticated[0].metadata.root["webhook_authenticated"] is True
    assert authenticated[0].metadata.root["webhook_auth_version"] == "v1"
    assert len(str(authenticated[0].metadata.root["webhook_event_hash"])) == 64
    assert "event-restart-4821" not in str(trace)
    assert SECRET not in str(trace)


async def test_authenticated_changed_replay_conflicts_without_second_model_call() -> None:
    factory = MemoryUnitOfWorkFactory()
    model = TransactionCheckingModel(factory, [final_turn()])
    service = InteractionService(
        PersistentRuntime(model, CapabilityRegistry(), factory), unit_of_work=factory
    )
    original = b'{"content":"Original authenticated event."}'
    changed = b'{"content":"Changed authenticated event."}'
    adapter = WebhookIngressAdapter(configuration(), service, clock=lambda: NOW)
    await adapter.deliver("events", original, **request_values(original, "event-conflict-5932"))

    with pytest.raises(AnbanError) as captured:
        await adapter.deliver("events", changed, **request_values(changed, "event-conflict-5932"))

    assert captured.value.info.details.root["reason"] == "conflicting"
    assert model.calls == 1
    inbox = await service.inbox()
    assert inbox[0].delivery_count == 2
    assert inbox[0].last_disposition.value == "conflicting"


@pytest.mark.parametrize(
    ("endpoint", "timestamp", "secret", "reason"),
    [
        ("unknown", NOW, SECRET, "webhook_endpoint_unknown"),
        ("events", NOW, "wrong-synthetic-key-material-123456", "webhook_authentication_failed"),
        ("events", NOW - timedelta(minutes=10), SECRET, "webhook_timestamp_stale"),
    ],
)
async def test_untrusted_delivery_fails_before_inbox_or_model(
    endpoint: str,
    timestamp: datetime,
    secret: str,
    reason: str,
) -> None:
    factory = MemoryUnitOfWorkFactory()
    model = TransactionCheckingModel(factory, [])
    service = InteractionService(
        PersistentRuntime(model, CapabilityRegistry(), factory), unit_of_work=factory
    )
    body = b'{"content":"Untrusted event must not execute."}'

    with pytest.raises(AnbanError) as captured:
        await WebhookIngressAdapter(configuration(), service, clock=lambda: NOW).deliver(
            endpoint,
            body,
            **request_values(body, "event-rejected-6043", timestamp=timestamp, secret=secret),
        )

    assert captured.value.info.details.root["reason"] == reason
    assert model.calls == 0
    assert await service.inbox() == ()


async def test_real_asgi_boundary_returns_bounded_result_and_auth_failure() -> None:
    factory = MemoryUnitOfWorkFactory()
    model = TransactionCheckingModel(factory, [final_turn("HTTP event completed.")])
    service = InteractionService(
        PersistentRuntime(model, CapabilityRegistry(), factory), unit_of_work=factory
    )

    async def close() -> None:
        return None

    async def builder():
        return service, close

    application = create_webhook_http_application(configuration(), builder)
    body = b'{"content":"Enter through the real ASGI request boundary."}'
    values = request_values(body, "event-http-7154", timestamp=datetime.now(UTC))
    headers = {
        "content-type": values["content_type"],
        "x-anban-event-id": values["event_id"],
        "x-anban-timestamp": values["timestamp"],
        "x-anban-signature": values["signature"],
    }
    async with (
        application.router.lifespan_context(application),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://webhook.invalid"
        ) as client,
    ):
        accepted = await client.post("/webhooks/events", content=body, headers=headers)
        rejected = await client.post(
            "/webhooks/events",
            content=body,
            headers={**headers, "x-anban-signature": "v1=" + "0" * 64},
        )

    assert accepted.status_code == 200
    assert accepted.json()["status"] == "succeeded"
    assert accepted.json()["persisted"] is True
    assert rejected.status_code == 401
    assert rejected.json() == {
        "status": "failed",
        "reason": "webhook_authentication_failed",
    }
