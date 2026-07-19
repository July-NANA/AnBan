# ADR-0009: Durable Timezone-aware Schedule Definitions

- Status: Accepted for D32
- Date: 2026-07-19
- Scope: v0.5 Cron, Interval, and timezone-aware schedule definitions
- Authorization: Delivery Issue #67

## Context

D32 must define durable Cron and Interval schedules before D33 can own trigger dispatch,
idempotency, overlap, replay, concurrency, and missed-run policy. A schedule definition is an
authoritative business fact, but creating it is not itself a Task, Run, Interaction delivery, or
successful trigger. Inventing any of those facts in D32 would create a false execution path.

Cron parsing and daylight-saving transitions are easy to implement incorrectly. The existing
Python standard library supplies authoritative IANA timezone data through `zoneinfo`, but it does
not parse Cron expressions. Anban therefore uses the maintained `croniter` 6.2 series only for
bounded five-field POSIX Cron validation and occurrence calculation.

## Decision

Core adds the thin immutable `ScheduleDefinition`, `ScheduleId`, and closed `cron`/`interval` kind.
A definition has one bounded logical name, bounded future Interaction content, an IANA timezone,
an anchor, its first occurrence after that anchor, and exactly one kind-specific expression.

Cron accepts only the normalized five-field POSIX form. Strict validation rejects impossible
calendar dates, and calculation starts from a timezone-aware datetime with a ten-year search bound.
The resulting occurrence is stored as UTC while retaining the IANA timezone name needed to
reconstruct civil-time meaning and daylight-saving behavior. Interval schedules are bounded from
one second through one year and advance by elapsed UTC duration; their timezone is retained for a
consistent future presentation and dispatch contract.

Migration `0011_schedules` adds one `schedules` table with database checks matching the Core
contract, a unique logical name, and an initial-next-occurrence index. The existing
`ExecutionRepository` and Unit of Work gain focused create/get/list operations; no new persistence
Port or backend is introduced. Definitions are immutable in D32. Later dispatch state must not
overwrite their anchor or first calculated occurrence.

`anban schedule create-cron`, `anban schedule create-interval`, `anban schedule show`, and
`anban schedules` compose a database-only production Application. CLI projections include only
logical definition data plus content hash and byte count; raw future Task content is not emitted by
inspection. Each CLI invocation is a fresh process, so successful show/list proves PostgreSQL
restart reconstruction rather than process-local memory.

D32 deliberately creates no Run-scoped Audit/Trace Event: no occurrence has fired, no Interaction
has been delivered, and no Model, Capability, or side effect has executed. The immutable Schedule
row is the authoritative creation fact. D33 must emit ordered trigger, policy, Interaction, Run,
Audit, and Trace evidence only when a real worker evaluates and dispatches an occurrence.

## Consequences

This delivery adds the Issue-authorized Schedule entity/identity, the `schedules` table, migration
`0011`, deterministic Runtime calculation, the schedule CLI surface, and the direct
`croniter>=6.2,<7` dependency. It adds no Port, Protocol, Adapter, Capability Handler, Tool name,
provider, persistence backend, product module, worker, scheduler-to-Runtime call, or trigger-success
fallback.

Creating or querying a definition has no external side effect and performs no automatic retry.
Invalid Cron, unavailable timezone, out-of-range interval, duplicate name, or persistence failure
fails explicitly without a partial definition. Worker ownership, occurrence claims, replay,
overlap, missed-run handling, Interaction deduplication, and create/resume dispatch remain D33.
