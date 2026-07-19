# ADR-0010: Durable Schedule Dispatch and Recovery

- Status: Accepted for D33
- Date: 2026-07-19
- Scope: v0.5 trigger dispatch, idempotency, overlap, replay, and missed-run policy
- Authorization: Delivery Issue #68

## Context

D32 persists immutable Cron and Interval definitions but deliberately creates no execution facts.
D33 must turn a due definition into ordinary Anban work without letting a worker call Runtime,
Model, Capability, or business functions directly. Concurrent workers, process termination,
ambiguous persistence, delayed polling, and long-running executions must not duplicate a real side
effect or fabricate success.

## Decision

Core adds the thin immutable `ScheduleOccurrence` lifecycle and closed missed, overlap, and status
vocabularies. Migration `0012_schedule_occurrences` adds definition policy columns and a durable
occurrence table. The unique `(schedule_id, scheduled_for)` key owns occurrence idempotency, a
partial unique index permits only one claimed occurrence per Schedule, and a stored lease permits
recovery after worker loss. An expired claim retains its occurrence and Interaction identities and
increments only its bounded attempt count.

The default and only currently authorized overlap policy is `skip`. A due occurrence observed while
another claim is active is recorded as `skipped` without a Run. Missed policy is either `skip`,
which records the latest delayed occurrence and the bounded number skipped, or `catch_up_once`,
which claims only the latest delayed occurrence and records how many older occurrences were
coalesced. The immutable Schedule anchor and initial occurrence remain unchanged; occurrence rows
are the durable cursor.

The Issue-authorized `ScheduleWorkerAdapter` lives in Interaction. One bounded `run-once` scan asks
Runtime's Schedule service to calculate and claim due facts, then submits a trusted
`SCHEDULE_OCCURRENCE` envelope to the existing `InteractionService`. The envelope uses its stored
occurrence identity as a deduplication correlation and includes bounded policy attestations. It
cannot call Runtime execution or a Capability directly. The ordinary inbox, Task, Run, Model,
Capability, Audit, and Trace paths remain authoritative.

A durably returned Run terminalizes the occurrence as `processed`, including a truthful Run error
code when execution failed. A validation failure before a Run terminalizes it as `failed`.
Persistence or Audit ambiguity leaves it claimed and reports `retry_pending`; the lease can later
redeliver the same Interaction identity, whose inbox record prevents Model or Capability replay.
No automatic retry asserts that an unknown side effect succeeded.

`schedule.occurrence_dispatched` is appended before `interaction.routed` in the new Run's ordered
Event stream. Its safe metadata includes occurrence identity, scheduled time, attempt/missed counts,
and policy values, never Schedule content. Skipped occurrences have no Run-scoped Event because no
execution was dispatched.

## Consequences

This delivery adds the authorized `ScheduleWorkerAdapter`, `ScheduleOccurrence` entity/identity,
policy fields, occurrence table, migration `0012`, and `anban scheduler run-once` plus occurrence
inspection. It extends the existing `ExecutionRepository`; it adds no Port, Protocol, Capability
Handler, Tool name, provider, persistence backend, top-level module, or direct scheduler-to-business
path.

PostgreSQL transactions serialize claims by Schedule and enforce idempotency independently of one
worker process. A process restart reconstructs definitions, occurrence state, inbox, Run, Audit,
and Trace. Safe replay can repeat only the Interaction delivery under the same stored identity;
terminal inbox reconstruction does not re-execute Model or Capability side effects.
