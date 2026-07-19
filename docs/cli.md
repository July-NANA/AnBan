# CLI Reference

The production CLI commands include `workspace init`, `run`, `chat`, `runs`, `inbox`, `trace`,
`artifacts`, `context task`, `context session`, the v0.5 `capabilities` inspection group, and
`webhook serve`. Query and execution commands support `--json`. Run failures use stable error
codes; Trace, Artifact, and Context queries work from a new database-only Application.

## Webhook service

`anban webhook serve [--host HOST] [--port PORT]` starts the real ASGI Interaction Adapter and
owns an ordinary production Application for the server lifespan. It serves `GET /health` and
`POST /webhooks/{endpoint}`. Access logs, API documentation, OpenAPI, server banners, and date
headers are disabled. The default listener is `127.0.0.1:8080`; production deployments must
terminate TLS through a trusted reverse proxy or equivalent protected listener.

Configure only logical endpoint names and Secret references in `anban.toml`:

```toml
[interaction.webhook]
body_max_bytes = 65536
clock_skew_seconds = 300

[[interaction.webhook.endpoints]]
name = "tasks"
secret_env = "ANBAN_WEBHOOK_TASKS_SECRET"
```

`ANBAN_WEBHOOK_TASKS_SECRET` must be at least 32 bytes and exists only in the process environment
or Workspace `secrets.env`. The sender signs the exact request bytes with HMAC-SHA256. Required
headers are:

- `Content-Type: application/json`
- `X-Anban-Event-Id`: bounded sender event identity
- `X-Anban-Timestamp`: Unix seconds inside the configured clock-skew window
- `X-Anban-Signature`: lower-case `v1=<sha256-hex>` over
  `v1\n{endpoint}\n{event-id}\n{timestamp}\n{raw-body}`

The JSON payload creates new work by default:

```json
{"content":"classify the received event","route":"new_task"}
```

An eligible waiting Run can instead receive governed contextual input:

```json
{
  "content": "continue with the approved value",
  "route": "resume_eligible_run",
  "resume_key": {"namespace": "opaque-namespace", "value": "opaque-value"}
}
```

Authentication completes before inbox admission. Authenticated deliveries then use the same
durable deduplication, routing, Context, Checkpoint, Runtime, Model, Capability, Audit, and Trace
paths as other Interaction input. Identical event delivery reconstructs its terminal Run across
server restart without replay; changed semantics under the same identity conflict.

`anban inbox [--limit N]` lists bounded durable delivery facts from a new database-only
Application: Interaction identity, logical source/kind/route, content hash, lifecycle status,
expiry, correlated Task/Run/Node identities, terminal category, delivery count, and last protocol
disposition. It never emits raw content or correlation values.

`anban run --async <request>` uses the ordinary Interaction and Runtime Composition Root. It emits
each durable waiting Checkpoint, resumes it, and emits the eventual terminal result; it does not
reinvoke the accepted Capability. `anban run show <run-id>` lists bounded Checkpoint projections,
and `anban trace <run-id>` includes their Run, Node, Invocation, and Checkpoint correlations.
`anban run <request> --async --detach` emits the first durable waiting projection, releases local
coroutine ownership, and exits without cancelling the external work. A new process continues it
with `anban run resume <checkpoint-id>` or requests real cancellation with
`anban run cancel <checkpoint-id>`. Resume reconstructs through the ordinary production
Composition Root and never reinvokes the accepted Capability.
Every waiting projection also includes an opaque resume namespace and correlation value. A fresh
process applies supplemental input with
`anban run update <namespace> <correlation-value> <content...>`. This constructs the ordinary
supplemental `InteractionEnvelope`; it does not call Runtime by Checkpoint identity. The raw key is
shown to the caller but never stored in PostgreSQL or Audit/Trace. Unknown or terminal correlation
fails explicitly.

The same eligible-Run route accepts an ordinary user reply with
`anban run reply <namespace> <correlation-value> <content...>` and an explicit human-input event
with `anban run human-input <namespace> <correlation-value> <content...>`. All three commands use
the same durable inbox, bounded update classification, Task Context, immutable revision, and
Checkpoint recovery path while preserving their distinct semantic input kind in Audit and Context
metadata.

An asynchronous worker signals result readiness with
`anban run process-result <namespace> <correlation-value> <content...>`,
`anban run mcp-result ...`, or `anban run subagent-result ...`. The content is a bounded delivery
notice, not an authoritative Capability result. Runtime resolves the Checkpoint-owned Invocation,
requires the matching Process/MCP/Sub-agent inventory kind, and retrieves the real terminal result
and Artifacts through the existing Capability lifecycle. Wrong-kind, unknown, duplicate-conflict,
or terminal signals fail or deduplicate without executing a result payload.

`capabilities list` returns a point-in-time snapshot. `capabilities search [TEXT]` accepts repeated
`--kind`, `--available-only`, and a bounded `--limit`. `capabilities describe KEY` requires an
exact inventory key and fails explicitly for unknown keys. These commands compose the current
Workspace Registry, Skill catalog, model configuration, registered Memory Handler, and configured
MCP servers without executing a Capability or opening a model client. MCP inventory inspection
performs real protocol discovery and therefore fails when a configured server is unavailable or
malformed. Each supported Tool appears as a ready dynamic `mcp.<server>.<tool-fragment>.<digest>`
Capability. With no configured server, MCP remains visible as unavailable; the sub-agent path also
remains unavailable until its owning delivery.

The Agent sees `memory.context`, `skill.activate`, and `process.execute`. Memory accepts `read`,
`remember`, `compress`, and `expire` operations over Runtime-owned Task or Session identity.
`remember` can record superseding or conflicting relationships; `compress` requires explicit
ordered Entry coverage and retains every raw row. `anban context task <task-id>` and
`anban context session <session-id>` return only bounded safe metadata: identities,
classifications, state, counts, timestamps, relationships, and SHA-256 hashes. Raw Context content
and source references are not emitted.

The Process input accepts `command`,
string `args`, optional `cwd`, `env` name/value entries, text `stdin`, `timeout`, and declared
`artifacts` with path and optional media type. Ordinary names resolve through inherited `PATH`;
absolute executable paths must be executable regular files; relative executable paths are rejected.
No implicit shell is used. Multiple Artifact declarations are validated together and duplicate
resolved paths are rejected before managed snapshots are created.

Default budgets are 12 model turns, 16 Capability calls, 3 replans, 600 seconds total, and
repeated-call limit 3 (`0` disables it; `1` is invalid). Replans can be disabled with `0` and have
a hard maximum of 8. Process defaults are 60 seconds with a configurable maximum of 300, 64 KiB
each for stdout/stderr/stdin, 128 arguments, 8 Artifacts, and 16 MiB per Artifact. Hard maxima are
24 turns, 32 calls, 1800 seconds total, 600 seconds Process, 256 KiB streams, 256 arguments, 32
Artifacts, and 64 MiB per Artifact.

`python -m scripts.doctor` checks the active Python 3.12 toolchain, Node/pnpm, Workspace,
configuration, both configured PostgreSQL databases and migration heads, uniform Skill discovery,
a harmless real Process through the production Registry, and real discovery for every configured
MCP server. `--online` additionally checks npx
and the current ClawHub CLI. `--web` additionally launches the locked Chromium check.
