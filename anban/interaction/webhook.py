"""Authenticated HTTP Webhook Adapter over the ordinary Interaction gateway."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from anban.config import WebhookConfiguration, policy
from anban.core import AnbanError, ErrorCode, ErrorInfo, SafeMetadata
from anban.core.ids import new_interaction_id
from anban.core.models import UtcDateTime, now_utc
from anban.interaction.contracts import (
    CorrelationKey,
    CorrelationPurpose,
    InteractionCorrelation,
    InteractionEnvelope,
    InteractionInputKind,
    InteractionRoute,
)
from anban.interaction.service import InteractionService
from anban.runtime import ExecutionResult

_EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SIGNATURE_PATTERN = re.compile(r"^v1=([0-9a-f]{64})$")
_JSON_CONTENT_TYPES = frozenset({"application/json"})

CloseCallback = Callable[[], Awaitable[None]]
ServiceBuilder = Callable[[], Awaitable[tuple[InteractionService, CloseCallback]]]


class WebhookValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WebhookResumeKey(WebhookValue):
    namespace: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=256)


class WebhookPayload(WebhookValue):
    content: str = Field(min_length=1, max_length=32_768)
    route: InteractionRoute = InteractionRoute.NEW_TASK
    resume_key: WebhookResumeKey | None = None

    @model_validator(mode="after")
    def validate_resume_meaning(self) -> WebhookPayload:
        if (self.route is InteractionRoute.RESUME_ELIGIBLE_RUN) != (self.resume_key is not None):
            raise ValueError("Webhook resume route requires exactly one resume key")
        return self


def webhook_signature(
    secret: str,
    endpoint: str,
    event_id: str,
    timestamp: str,
    body: bytes,
) -> str:
    """Create the versioned signature used by senders and deterministic integration fixtures."""

    material = b"\n".join(
        (
            b"v1",
            endpoint.encode(),
            event_id.encode(),
            timestamp.encode(),
            body,
        )
    )
    return "v1=" + hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()


class WebhookIngressAdapter:
    """Authenticate one bounded delivery before constructing its InteractionEnvelope."""

    def __init__(
        self,
        configuration: WebhookConfiguration,
        interactions: InteractionService,
        *,
        clock: Callable[[], UtcDateTime] = now_utc,
    ) -> None:
        self._configuration = configuration
        self._interactions = interactions
        self._clock = clock

    async def deliver(
        self,
        endpoint_name: str,
        body: bytes,
        *,
        content_type: str | None,
        event_id: str | None,
        timestamp: str | None,
        signature: str | None,
    ) -> ExecutionResult:
        endpoint = self._configuration.endpoint(endpoint_name)
        if endpoint is None:
            raise webhook_error(ErrorCode.VALIDATION_FAILED, "webhook_endpoint_unknown")
        if len(body) > self._configuration.body_max_bytes:
            raise webhook_error(ErrorCode.VALIDATION_FAILED, "webhook_body_too_large")
        media_type = "" if content_type is None else content_type.split(";", 1)[0].strip().lower()
        if media_type not in _JSON_CONTENT_TYPES and not media_type.endswith("+json"):
            raise webhook_error(ErrorCode.VALIDATION_FAILED, "webhook_content_type_invalid")
        received_at = self._clock()
        event_hash, skew = self._authenticate(
            endpoint_name,
            endpoint.secret.get_secret_value(),
            body,
            event_id,
            timestamp,
            signature,
            received_at,
        )
        try:
            payload = WebhookPayload.model_validate_json(body)
        except ValidationError:
            raise webhook_error(ErrorCode.VALIDATION_FAILED, "webhook_payload_invalid") from None
        resume_key = (
            None
            if payload.resume_key is None
            else CorrelationKey(
                purpose=CorrelationPurpose.RESUME,
                namespace=payload.resume_key.namespace,
                value=payload.resume_key.value,
            )
        )
        envelope = InteractionEnvelope(
            id=new_interaction_id(),
            source=f"webhook.{endpoint_name}",
            input_kind=InteractionInputKind.WEBHOOK_EVENT,
            content=payload.content,
            received_at=received_at,
            correlation=InteractionCorrelation(
                route=payload.route,
                resume_key=resume_key,
                deduplication_key=CorrelationKey(
                    purpose=CorrelationPurpose.DEDUPLICATION,
                    namespace=f"webhook.{endpoint_name}",
                    value=event_id or "",
                ),
            ),
            metadata=SafeMetadata(
                {
                    "webhook_endpoint": endpoint_name,
                    "webhook_authenticated": True,
                    "webhook_auth_version": "v1",
                    "webhook_event_hash": event_hash,
                    "webhook_clock_skew_seconds": skew,
                }
            ),
        )
        return await self._interactions.submit(envelope)

    def _authenticate(
        self,
        endpoint: str,
        secret: str,
        body: bytes,
        event_id: str | None,
        timestamp: str | None,
        signature: str | None,
        received_at: datetime,
    ) -> tuple[str, int]:
        if (
            event_id is None
            or _EVENT_ID_PATTERN.fullmatch(event_id) is None
            or len(event_id) > policy.WEBHOOK_EVENT_ID_MAX_CHARS
        ):
            raise webhook_error(ErrorCode.VALIDATION_FAILED, "webhook_event_id_invalid")
        if (
            timestamp is None
            or len(timestamp) > 12
            or not timestamp.isascii()
            or not timestamp.isdecimal()
        ):
            raise webhook_error(ErrorCode.VALIDATION_FAILED, "webhook_timestamp_invalid")
        try:
            supplied_seconds = int(timestamp)
        except ValueError:
            raise webhook_error(ErrorCode.VALIDATION_FAILED, "webhook_timestamp_invalid") from None
        skew = abs(int(received_at.timestamp()) - supplied_seconds)
        if skew > self._configuration.clock_skew_seconds:
            raise webhook_error(ErrorCode.VALIDATION_FAILED, "webhook_timestamp_stale")
        match = None if signature is None else _SIGNATURE_PATTERN.fullmatch(signature)
        expected = webhook_signature(secret, endpoint, event_id, timestamp, body)
        if match is None or not hmac.compare_digest(expected, signature or ""):
            raise webhook_error(ErrorCode.VALIDATION_FAILED, "webhook_authentication_failed")
        event_hash = hashlib.sha256(f"{endpoint}\x00{event_id}".encode()).hexdigest()
        return event_hash, skew


def webhook_error(code: ErrorCode, reason: str) -> AnbanError:
    return AnbanError(
        ErrorInfo(
            code=code,
            message="Webhook delivery was rejected",
            details=SafeMetadata({"reason": reason}),
        )
    )


def create_webhook_http_application(
    configuration: WebhookConfiguration,
    service_builder: ServiceBuilder,
) -> FastAPI:
    """Build one real ASGI Adapter whose lifespan owns the production Application."""

    active: dict[str, WebhookIngressAdapter] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        interactions, close = await service_builder()
        active["adapter"] = WebhookIngressAdapter(configuration, interactions)
        try:
            yield
        finally:
            active.clear()
            await close()

    application = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    async def _health() -> dict[str, str]:
        return {"status": "ready"}

    async def _receive(endpoint_name: str, request: Request) -> JSONResponse:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > configuration.body_max_bytes:
                return error_response(
                    webhook_error(ErrorCode.VALIDATION_FAILED, "webhook_body_too_large")
                )
        adapter = active.get("adapter")
        if adapter is None:
            return JSONResponse(
                status_code=503,
                content={"status": "failed", "reason": "webhook_service_unavailable"},
            )
        try:
            result = await adapter.deliver(
                endpoint_name,
                bytes(body),
                content_type=request.headers.get("content-type"),
                event_id=request.headers.get("x-anban-event-id"),
                timestamp=request.headers.get("x-anban-timestamp"),
                signature=request.headers.get("x-anban-signature"),
            )
        except AnbanError as exc:
            return error_response(exc)
        return JSONResponse(
            status_code=200,
            content={
                "status": result.outcome.status.value,
                "persisted": result.persisted,
                "task_id": str(result.task_id),
                "run_id": str(result.run_id),
            },
        )

    application.add_api_route("/health", _health, methods=["GET"])
    application.add_api_route("/webhooks/{endpoint_name}", _receive, methods=["POST"])
    return application


def error_response(error: AnbanError) -> JSONResponse:
    reason = error.info.details.root.get("reason")
    safe_reason = reason if isinstance(reason, str) else error.info.code.value
    status = (
        404
        if safe_reason in {"webhook_endpoint_unknown", "unknown"}
        else 413
        if safe_reason == "webhook_body_too_large"
        else 401
        if safe_reason
        in {
            "webhook_authentication_failed",
            "webhook_event_id_invalid",
            "webhook_timestamp_invalid",
            "webhook_timestamp_stale",
        }
        else 409
        if safe_reason in {"conflicting", "deduplication_pending", "ineligible"}
        else 503
        if error.info.category.value == "persistence"
        else 422
    )
    return JSONResponse(status_code=status, content={"status": "failed", "reason": safe_reason})
