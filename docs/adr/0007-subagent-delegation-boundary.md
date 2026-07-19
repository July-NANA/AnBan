# ADR-0007: Durable Sub-agent Delegation Boundary

- Status: Accepted for D30
- Date: 2026-07-19
- Scope: v0.5 parent/child Agent execution and aggregation
- Authorization: Delivery Issue #66

## Context

D30 requires a real child-Agent lifecycle without a second scheduler, a fake child result, or a
Core bypass. A delegated objective must execute through the ordinary Model, Capability Registry,
Runtime, PostgreSQL, Artifact, Audit, and Trace paths. The parent must retain its own lifecycle,
wait for an authoritative child result, and preserve child failure and provenance.

## Decision

Anban registers one shared `agent.delegate` Handler in the existing Capability Registry. It accepts
one bounded objective and starts the same `PersistentRuntime` as an independent child Task and Run.
The Handler returns non-terminal `accepted` only after the child Task, Run, root Node, parent link,
and `subagent.child_created` fact are durable. It never executes the objective inline and never
constructs a child result itself.

`ExecutionRun` carries optional `parent_run_id`, `parent_invocation_id`, and `delegation_depth`.
A child must carry both parent identities, its parent Invocation must belong to its parent Run, one
Invocation may create at most one child, and depth is bounded to three. PostgreSQL foreign keys,
checks, a uniqueness constraint, and an index enforce the same relationship as Core validation.
The root Run has no parent and depth zero.

The child uses the ordinary Runtime composition, including the real Model and all ready
Capabilities. Its Artifacts remain owned by the child Run and Invocation. The parent Invocation
waits through the existing background Capability and Checkpoint lifecycle. A correlated
`SUBAGENT_RESULT` Interaction is only a readiness signal; after a fresh Application starts, the
Handler reconstructs the terminal child through its durable relationship event and Run aggregate,
then returns the authoritative status, bounded final result, and Artifact count to the parent
Agent. The signal content cannot become a Tool Result or execution channel.

Parent cancellation cancels the owned active child task and both Runs persist cancellation. Child
failure, cancellation, and timeout map to the same terminal parent Invocation result and cannot
become success. A service shutdown cancels an active in-process child instead of pretending that
another worker owns it. A terminal child remains restart-queryable and can be aggregated after a
parent Application restart. Recovering an active child across complete process exit would require
a separately authorized worker-ownership design.

## Consequences

This delivery adds the authorized `agent.delegate` Handler/Tool name, parent linkage on
`ExecutionRun`, migration `0010`, and the `subagent.child_created` Audit event. It adds no Port,
Protocol, Adapter, provider, persistence backend, product module, queue, or sub-agent-specific
Runtime. Delegation uses the same inventory, Registry, Agent, and persistence path at every depth.

Starting a child is not automatically retried. The database uniqueness constraint prevents a
second child for one parent Invocation, and restart recovery consumes only a terminal persisted
child. Child side effects retain their original Invocation provenance; neither parent recovery nor
duplicate result delivery replays them. If child status is unavailable or still active during
terminal recovery, aggregation fails explicitly.
