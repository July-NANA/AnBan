# ADR-0008: Authenticated Webhook Interaction Boundary

- Status: Accepted for D31
- Date: 2026-07-19
- Scope: v0.5 authenticated and replay-safe Webhook ingestion
- Authorization: Delivery Issue #69

## Context

D31 requires a real HTTP Webhook entry point that can create or resume Runs without allowing a
transport to bypass Interaction, durable inbox admission, Runtime, or ordinary persistence. A
sender-controlled event identity is useful for replay protection but is not a system identity, and
an unauthenticated request cannot be trusted enough to create a durable business fact.

## Decision

Anban adds one FastAPI-based `WebhookIngressAdapter` in `interaction`. `anban webhook serve` owns
the ordinary production Application for its ASGI lifespan and exposes configured logical endpoints
at `POST /webhooks/{endpoint}`. Production disables HTTP access logs, server banners, API docs, and
OpenAPI. Deployments terminate TLS in a trusted reverse proxy or equivalent protected listener;
the Adapter does not claim to provide TLS itself.

Each endpoint declares only a bounded logical name and `secret_env` reference in `anban.toml`. The
HMAC secret is at least 32 bytes and resolves from the process environment or mode-0600 Workspace
`secrets.env`. It never enters TOML, logs, errors, HTTP responses, Event Metadata, Audit, or Trace.

The sender supplies `X-Anban-Event-Id`, `X-Anban-Timestamp`, and `X-Anban-Signature`. The timestamp
is Unix seconds within the configured clock-skew window. The signature is lower-case
`v1=<sha256-hex>` HMAC-SHA256 over the exact bytes
`v1\n{endpoint}\n{event-id}\n{timestamp}\n{raw-body}`. Anban checks the bounded body, media type,
endpoint, event identity, timestamp, and signature with constant-time comparison before parsing the
payload or constructing an `InteractionEnvelope`.

An authenticated payload contains bounded `content`, a `new_task` or `resume_eligible_run` route,
and exactly one opaque resume key only for the resume route. The Adapter assigns the Interaction
identity and trusted authentication attestation. External normalization rejects attempts to forge
those fields. The endpoint and event identity form the existing deduplication key; only their
SHA-256 correlation is projected into Event/Audit/Trace. Raw event identity and signature are not
stored. The bounded payload content is ordinary Task or Context business input in PostgreSQL, not
Event/Audit metadata. Raw resume values remain excluded from storage as before.

Authentication failures occur before inbox admission because the caller identity is not trusted;
they therefore cannot become durable business records. Once authenticated, malformed payload,
unknown resume, conflict, expiry, or another semantic routing failure follows the existing inbox
protocol and becomes a durable rejected delivery when that protocol owns the failure. A valid new
event creates work through Interaction and the ordinary Runtime. A valid resume event uses the
same eligible-Run correlation, Task Context, Checkpoint, and recovery path as other governed
updates. `webhook.authenticated` precedes `interaction.routed` in the shared Event stream.

## Consequences

This delivery adds the authorized Webhook Interaction Adapter, bounded configuration, the direct
Uvicorn runtime dependency, and the `webhook.authenticated` Audit event. It adds no Core entity,
table, migration, Port, Protocol, Capability Handler, Tool name, provider, persistence backend,
product module, scheduler, or Webhook-specific Runtime path.

PostgreSQL inbox uniqueness makes identical authenticated delivery replay-safe across HTTP service
restart: a terminal duplicate reconstructs the persisted Run and does not call Model or Capability
again; changed semantics under the same identity conflict. Authentication does not claim
exactly-once behavior for downstream external systems. Anban never automatically retries a
Webhook-triggered side effect, and an ambiguous or failed external effect remains represented by
its ordinary Invocation lifecycle.
