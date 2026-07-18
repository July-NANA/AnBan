# Module Boundaries

## Interaction

Owns transport-facing input, output, feedback, and bidirectional event adaptation. The v0.5
`InteractionEnvelope` is the single normalized vocabulary for user messages, supplemental input,
asynchronous Capability/MCP/sub-agent results, Webhooks, and schedule occurrences. Its explicit
route is either new Task or requested eligible-Run resumption. Resume and deduplication use
separate bounded external `CorrelationKey` values; neither is a Task, Run, Session, Invocation, or
other system identity. External normalization assigns the Interaction identity, receipt time, and
trusted Adapter source and rejects attempts to supply system-owned fields. Audit projection hashes
correlation values. Interaction does not yet own durable lookup, inbox, deduplication, expiry
records, background delivery, Trigger behavior, domain lifecycle, or execution scheduling.
The existing CLI service therefore rejects every non-CLI kind and every resume/deduplication key
instead of silently treating unsupported input as new work.

## Core

Owns authoritative Task, ExecutionRun, NodeRun, CapabilityInvocation, Checkpoint, Artifact, Event, bounded
Task/Session Context, and `TaskGraphSpec` identity-free structured graph vocabulary. A graph spec
contains only closed node/edge kinds, explicit dependencies, input/output bindings, entry and
terminal identities, nested subgraphs, and hard budgets. Its validator rejects hidden cycles,
invalid control shapes, unreachable nodes, bindings outside dependency scope, and unbounded loop or
parallel behavior before Runtime can build it. Core does not execute the spec and must not absorb
provider clients, SQLAlchemy models, transport details, or scheduling.

## Runtime

Owns v0.1 execution order, state transitions, the fixed LangGraph, bounded Tool Calling, durable
coordination, and query projections. The v0.5 sufficiency evaluator uses a closed structured Model
decision to select only real, ready inventory targets; Runtime constructs and validates the
authoritative assessment, including general Skill-acquisition justification and explicit
clarification/failure. Runtime also owns structured completion assessment and a separately bounded
replan decision. Proposed final text, successful Skill activation, stored Memory, and intermediate
Capability output are not terminal facts. The next alternative must match one exact ready
strategy/target, while identical completed or uncertain calls remain replay-protected.
Runtime now owns one generic dynamic LangGraph builder. It compiles any validated `TaskGraphSpec`
through a single topology-independent registration path and requires callers to inject real node
actions and control routing; compilation never substitutes no-op or mock-success execution.
The generic Task graph executor resolves declared bindings, evaluates every closed condition
operator, bounds loops and total node executions, limits real action parallelism, waits for joins,
and recursively runs nested subgraphs. It calls one explicitly supplied asynchronous action
executor and validates every returned output shape; it performs no automatic retry or fallback.
The production Composition Root adds one structured Main Agent route decision: adequate simple
work retains the fixed Agent, while materially structured work must supply a fully validated graph.
The selected route is durable; a graph route atomically appends and links its initial
`GraphRevision`, then each real action executes as its own durable Node through the existing Agent,
Model, Capability, Invocation, Artifact, and Event path. Invalid planning or node JSON fails the
Run rather than falling back to the fixed Agent. Runtime now also recognizes the existing
Capability lifecycle's non-terminal `accepted` result, records bounded monotonic progress, and
waits for the real terminal result before exposing a Tool Result. It does not replay a background
side effect. For asynchronous execution, Runtime creates a durable Checkpoint after real background
acceptance, returns a bounded waiting projection, and resumes or cancels only after the requested
Checkpoint transition commits. The fixed Agent and every Task Graph action use the same path.
Runtime can explicitly detach local coroutine ownership while leaving the durable waiting state
intact. A fresh Application rebuilds the Invocation context and Event sequence from PostgreSQL,
restores the already-started Capability through its optional recovery contract, and continues from
the authoritative result. Task Graph recovery reconstructs control state from immutable revision
data and persisted structured NodeRun outputs: completed actions are reused, the active action
consumes the recovered result, and only previously unstarted actions may execute.

## Model

Owns the Model Port and its adapters. Model reasoning is deliberately separate from generic executable Capability behavior.

## Capability

Owns `CapabilityPort`, `CapabilityHandler`, the Registry, uniform `SKILL.md` discovery/activation,
the general Process Handler, and the read-only `CapabilityInventoryPort` projection. The unified
inventory describes the independently configured Model, registered Capabilities, ready Skills,
Process, implemented Memory, and explicitly unavailable future MCP and sub-agent paths without
invoking any of them or creating a second Registry. The shared Workspace catalog refreshes through the same
parser, bounds, protected-value checks, conflict rules, and logical identities for both inventory
and `skill.activate`; an Application rebuild is not required to observe an installed, changed, or
removed Skill. Multiple activated Skill instructions remain authoritative native Tool Results in
the same bounded model exchange, with a shared hard context limit rather than a second prompt or
execution channel. Production Capability names are `memory.context`, `skill.activate`, and
`process.execute`. The Memory Handler uses the existing Unit of Work Port and the same Registry;
it does not define another backend or discovery path. A concrete tool normally adds a Skill, not a
Handler. No Skill source or installer receives a special branch. MCP and external Agents remain
future categories. `process.execute` can launch the same governed process in background mode only
after real spawn succeeds. The Registry retains its authoritative Runtime-supplied Invocation
context, enforces monotonic progress, supports cancellation and waiting, and correlates the result
with the system Invocation identity rather than a model argument. Its independent worker retains
physical process ownership across service exit. Raw arguments and protected values cross a private
stdin pipe and are never written to recovery state; the Workspace `.anban/process` area stores only
bounded start, cancel, and validated result facts. No queue or background-specific Tool/Handler
exists.

## Persistence

Owns repositories and storage adapters for business state, Artifact metadata, and the authoritative
Event stream. PostgreSQL is the business database; Audit and Trace are Event projections rather
than duplicate stores. Context entries, summaries, and summary-to-entry coverage are durable
PostgreSQL facts. Raw facts survive bounded compression and restart; no vector database is used.
Validated Task graph content is stored in an append-only `GraphRevision` chain keyed to one Task.
Each row carries the canonical spec hash, reason, predecessor, validation status, and safe metadata;
composite foreign keys keep Runs and predecessors on the same Task. Repository methods expose no
update, partial unique indexes prevent a second initial or sibling successor, and a database
trigger rejects direct UPDATE statements. The chain tail is the current revision without mutating
older rows.
Checkpoint rows durably correlate one Run, Node, and Invocation, retain only a hash of the bounded
continuation state, and transition through waiting, resumed or cancel-requested, then one terminal
state. Their ordered Events share the Run sequence and system-owned Checkpoint identity. Background
Process acceptance, progress, Checkpoint, and terminal Events are reconstructable by a fresh query
Application; the Invocation remains `running` until the actual result transaction. Structured
NodeRun outputs are nullable JSON objects used to reconstruct prior Task Graph action state without
re-execution. Recovery appends `run.recovery_started`, `run.recovery_completed`, or
`run.recovery_failed`; terminal Capability, Checkpoint, Artifact, Node, Run, and Task writes remain
short PostgreSQL transactions in the original ordered Event stream.

Dependencies point toward Ports and stable Core vocabulary. Adapters depend on external systems; Core never depends on a concrete provider, Skill source, filesystem root, or frontend.

`config` is authorized cross-module infrastructure and is not a seventh product module. New Ports,
Adapters, Handler/Tool names, persistence backends, interaction adapters, or top-level product
packages require explicit architecture authorization; an ADR alone does not grant it.

For the existing CLI execution path, Interaction calls the Runtime application entry. Runtime depends on Core contracts,
ModelPort, CapabilityPort, and Core persistence Protocols. Persistence, provider, Workspace, and
CLI adapters point inward toward those contracts; no reverse dependency or integration-specific
Core path is allowed.
