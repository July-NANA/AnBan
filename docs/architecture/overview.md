# Architecture Overview

The six product modules are `interaction`, `core`, `runtime`, `model`, `capability`, and
`persistence`. `config` is authorized cross-module infrastructure, not a seventh product module.

```text
Interaction -> Runtime -> ModelPort
                       -> CapabilityPort -> skill.activate
                                         -> process.execute
                                         -> memory.context
                       -> UnitOfWorkFactory -> PostgreSQL
```

Core owns identities, lifecycles, Artifact/Event facts, `ExecutionRepository`, `UnitOfWork`, and
`UnitOfWorkFactory`, including Task/Session Context vocabulary. Core also owns the strict immutable
`TaskGraphSpec` data contract: action, branch, bounded loop, parallel, join, and nested-subgraph
nodes; typed edges; explicit dependencies and value bindings; entry/terminal declarations; and
graph-wide budgets. Validation proves that every node is reachable and can reach a terminal, that
only explicit loop-back edges form cycles, and that bindings reference declared graph inputs or
dependency outputs. PostgreSQL stores each validated spec as an immutable `GraphRevision` with a
canonical hash and same-Task predecessor. New structural content appends a revision; no Repository
update exists and the database rejects direct row updates. The current revision is derived from the
chain tail. Runtime uses one structured Model decision to retain the fixed Agent for adequate
simple work or require a validated graph for materially structured work. It compiles graph data
through one dynamic LangGraph builder and provides generic bounded branch, loop, parallel, join,
and nested-subgraph execution. Graph actions re-enter the existing real Agent/Capability path as
durable Nodes; invalid planning or action output cannot fall back to success. Model remains an
independent Port. Capability owns the Registry and its three Handlers. Runtime also owns Tool-call
correctness, structured completion evaluation, bounded alternative-path selection, repair without
side-effect replay, persistence coordination, and Trace projection. Persistence implements the one
PostgreSQL backend; Interaction supplies the CLI loop and the transport-neutral v0.5
input/correlation vocabulary. Route persistence adds `agent.route_selected`; graph selection also
adds `graph.revision_created` and `run.graph_revision_linked`.

Every future input is normalized into one strict `InteractionEnvelope`. The envelope uses a closed
semantic input kind and explicitly requests either a new Task or resumption of an eligible Run.
The request never carries a system Task/Run/Session/Invocation identity: it uses bounded external
resume correlation instead, with an independent deduplication correlation when needed. System
code assigns the Interaction ID, receipt time, and trusted Adapter source. Malformed, expired,
conflicting, unknown, and ineligible correlation is fail-closed vocabulary; this contract delivery
does not implement lookup or claim that a Run is eligible. Audit/Trace metadata receives only
correlation namespace and SHA-256 fingerprint, never the external correlation value.
The existing CLI service accepts only its original uncorrelated CLI user-message path; it rejects
all other envelope kinds, sources, resume requests, and deduplication keys until the durable
Gateway owns those semantics.

Every Skill follows `SKILL.md -> uniform parser -> SkillPackage -> skill.activate ->
process.execute`. No production code selects behavior by source, installer, registry, publisher,
slug, Lock/Origin format, or fingerprint. Package and Workspace roots differ only in their logical
read-only root. Skill resources are referenced by that root and loaded only when instructions need
them.

`process.execute` understands only general process concepts: executable resolution, arguments,
environment overlays, cwd, stdin, bounded output, timeout/cancellation, process-group cleanup, and
declared single-file Artifacts. One invocation may collect multiple files atomically at the
snapshot layer. It has no HTTP, ClawHub, Git, Weather, PDF, or tool-specific branch.

PostgreSQL stores lifecycle and ordered Event facts. Managed Artifact bytes use logical
`anban://artifact/...` URIs. Audit and Trace are projections of the same Event stream. Database or
Event write failures cannot become ordinary success.

`memory.context` is one ordinary registered Capability. It resolves Task identity through the
current Run and Session identity from Runtime-owned invocation metadata. PostgreSQL stores raw
Context entries, summaries, and ordered summary coverage. Compression is atomic: a valid summary
marks covered entries superseded but never deletes them; any validation or write failure rolls the
whole operation back. No vector database, second registry, source-specific loader, or Memory
backend was introduced.

The Main Agent does not equate a valid final-text shape with goal completion. The existing Model
Port receives a closed completion schema plus the original bounded transcript, real Tool Results,
safe observation facts, ready sufficiency candidates, and remaining replan budget. Only a complete
assessment can produce success. An incomplete assessment selects one exact ready strategy/target,
requests clarification, or fails; production then enforces that selected alternative on the next
native response. This is Runtime logic inside the same fixed LangGraph, not another scheduler,
Port, or execution channel.
