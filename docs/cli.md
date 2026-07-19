# CLI Reference

The production CLI commands include `workspace init`, `run`, `chat`, `runs`, `inbox`, `trace`, `artifacts`,
`context task`, `context session`, and the v0.5 `capabilities` inspection group. Every command
supports `--json`. Run failures use stable error codes; Trace, Artifact, and Context queries work
from a new database-only Application.

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
metadata. Unsupported machine-result kinds remain unavailable until their owning delivery.

`capabilities list` returns a point-in-time snapshot. `capabilities search [TEXT]` accepts repeated
`--kind`, `--available-only`, and a bounded `--limit`. `capabilities describe KEY` requires an
exact inventory key and fails explicitly for unknown keys. These commands compose the current
Workspace Registry, Skill catalog, model configuration, and registered Memory Handler without
executing a Capability or opening a model client. Memory is ready; MCP and sub-agent paths remain
visible as unavailable until their owning v0.5 deliveries implement them.

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
and a harmless real Process through the production Registry. `--online` additionally checks npx
and the current ClawHub CLI. `--web` additionally launches the locked Chromium check.
