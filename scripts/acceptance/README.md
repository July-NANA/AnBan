# Real acceptance helpers

These commands fail closed and emit only bounded evidence:

- `python -m scripts.acceptance.check_migration_schema`: current migration head and schema
  constraints against the configured test database.
- `pnpm run acceptance:persistence`: real PostgreSQL repository, transaction, aggregate, Artifact,
  Event-order, Task/Session Context restart, summary coverage, and rollback checks.
- `pnpm run acceptance:model`: real ModelPort content, native Tool Call/Result, final response, and
  structured output.
- `pnpm run acceptance:capability`: the production Registry surface plus real Process stdin, env,
  multi-Artifact, nonzero exit, partial Artifact failure behavior, and durable Memory
  retention/recall through PostgreSQL.
- `pnpm run acceptance:runtime`: Gate A in an isolated Workspace, then Gate B-D in a fresh isolated
  Workspace. It uses the normal production Application and natural-language prompts, performs real
  Process/file/HTTP/Artifact work, invokes the ordinary packaged ClawHub Skill, installs exactly one
  compatible public Skill, activates and uses it to finish the original installation Run, then
  starts three fresh Applications and requires complete Skill/Process Traces. Skill identity
  evidence is limited to slug, relative `SKILL.md` path, and content hash;
  production and acceptance do not derive identity from installation records. Gate A also runs two
  differently worded multi-Artifact tasks without prescribing cwd, filenames, Tool Schema, command,
  or Tool order. Gate A strictly proves that one Process Invocation can collect two Artifacts; the
  semantic recovery variants separately prove Run-level persistence and restart queries for at
  least two valid Artifacts without prescribing how many legitimate Invocations the model uses.
- `pnpm run acceptance:interaction-update`: D22 real Provider/PostgreSQL/Process acceptance. Three
  semantic context-only variants and one dynamic structural replacement enter through fresh CLI
  processes and the ordinary Interaction envelope. It proves opaque correlation, immutable
  revision history, restart recovery, protected action reuse, one real side effect, and explicit
  unknown/terminal correlation failure without persisting raw keys or model responses.
- `pnpm run acceptance:graph-result-reuse`: D23 real Provider/PostgreSQL/Process acceptance. A
  randomized sequential graph replans through fresh CLI processes, reuses one unchanged concrete
  Capability-backed NodeRun, and recovers one active Process without replay. Five deterministic
  semantic variants separately cover transitive invalidation, pure reexecution, removal,
  side-effect rejection, and prior invalidation history; two ordinary-Composition reverse tests
  durably reject side-effect reexecution and changed active ancestry without replacing the
  production execution path.
- `pnpm run acceptance:interaction-gateway`: D25 real Provider/PostgreSQL acceptance. Three
  changed direct-answer Task objects enter the ordinary Application from distinct logical Adapter
  sources, create independent durable Runs, and reconstruct one `interaction.routed` Audit fact
  through new query Applications. Async-result, unattested schedule, and unknown-resume reverse
  inputs fail explicitly without creating a Run; their owned execution remains Adapter-governed.
- `pnpm run acceptance:interaction-inbox`: D26 real Provider/PostgreSQL acceptance. Three changed
  task objects and logical sources each create one Run; a fresh Application redelivers the same
  randomized identity and reconstructs that Run without replay. Fresh queries reconcile two
  deliveries, `interaction.inbox_routed`, complete Trace, conflict, post-receipt expiry, and durable
  unsupported-input rejection.
- `pnpm run acceptance:human-input`: D27 real Provider/PostgreSQL/Process acceptance. A user reply,
  supplemental input, and explicit Human Input event route into three active Runs through fresh
  Applications and the ordinary eligible-Run correlation. The CLI covers reply and Human Input;
  the supplemental variant proves duplicate delivery reconstruction. Context, Checkpoint, inbox,
  ordered Audit/Trace, one real side effect, restart recovery, and unknown/terminal reverse cases
  reconcile without raw correlations or replay.
- `pnpm run acceptance:async-result`: D28 real Provider/PostgreSQL/Process acceptance. Three
  changed background Process tasks cross fresh Applications and resume only after a correlated
  result-ready signal. One variant first rejects the MCP/Process kind mismatch, one deduplicates a
  repeated signal, and all reconcile the real terminal result, Invocation, Checkpoint, Artifact,
  inbox, ordered Audit/Trace, original delivery, and exactly-once side effect. Deterministic MCP
  and Sub-agent lifecycle variants cover the same generic path until D29/D30 provide their real
  integrations.
- `pnpm run acceptance:mcp`: D29 real Provider/PostgreSQL/MCP acceptance. An isolated Workspace
  launches a real official-SDK stdio server with a randomized Tool name and structured schema.
  Three changed tasks enter through the ordinary Interaction/Runtime Composition Root, invoke the
  Tool once, and reconcile Invocation, external state, safe MCP Audit metadata, and complete Trace
  through fresh Applications. Reconnect preserves logical identity; malformed protocol and an
  unavailable command fail closed. Deterministic MCP tests additionally cover schema rejection,
  timeout, cancellation, Tool error, output bounds, and protected-value boundaries.
- `pnpm run acceptance:subagent`: D30 real Provider/PostgreSQL/Process/Sub-agent acceptance. Three
  randomized parent Runs delegate one independently durable child Run through the ordinary
  production Runtime. Each child performs a real Process side effect and owns its Artifact. A
  fresh Application consumes a correlated `SUBAGENT_RESULT` and aggregates the child outcome
  without replay; fresh queries reconcile parent linkage, depth, Checkpoint, inbox, Audit, Trace,
  and provenance. Wrong-kind, duplicate, unknown, and terminal deliveries fail closed or
  deduplicate. Deterministic tests additionally cover child failure and parent cancellation.
- `pnpm run acceptance:webhook`: D31 real HTTP/Provider/PostgreSQL/Process acceptance. A randomized
  endpoint receives signed new-work events through a real Uvicorn CLI process, then identical
  delivery after full server restart reconstructs the terminal Run without replay. A signed event
  resumes a detached background Process through the ordinary correlation, Context, Checkpoint,
  and Runtime recovery path and reconciles one Artifact. Bad signature, stale timestamp, unknown
  endpoint, conflicting replay, and authenticated unknown resume fail closed; fresh inbox,
  Audit/Trace, and database queries verify the authentication and persistence boundary.
- `pnpm run acceptance:schedule`: D32 real CLI/PostgreSQL/timezone acceptance. Separate processes
  create weekday and daily Cron definitions in two IANA timezones plus one UTC Interval, then fresh
  Application and CLI queries reconcile identity, civil time, and safe content hashes. Invalid
  Cron, timezone, interval, and duplicate name fail without a partial row. The Gate proves D32
  creates no Run or inbox delivery; worker dispatch remains D33.
- `pnpm run acceptance:automation`: D33 real CLI/Provider/PostgreSQL Schedule acceptance. Three
  randomized timezone-aware daily definitions become due together and three fresh worker processes
  contend for them. Exactly one occurrence, inbox delivery, and successful Run per definition is
  reconstructed through a fresh Application with ordered `schedule.occurrence_dispatched` Audit
  evidence. A delayed Interval proves missed-run `skip` creates no Run; deterministic tests cover
  lease recovery, restart redelivery, overlap, catch-up-once, and forged-attestation rejection.
- `pnpm run acceptance:p1-main-agent`: twelve real-model Runs in an isolated Workspace through the
  ordinary production Composition Root: direct answer, structured durable Memory, three semantic
  ready-Skill variants, three multi-Skill variants, clarification without a side effect, and three
  explicit-failure variants without an Artifact. Two ready Skills, their identities, markers, and
  task objects are generated for each Gate run. Every case is reconstructed through a fresh
  query-only Application and reconciles terminal database, Audit, Trace, Invocation, and Artifact
  facts.
- `pnpm run acceptance:p1`: the complete #72 sequence: local quality/build/Doctor, online Doctor,
  PostgreSQL, real Model Gateway, real local Capabilities, P1 Main Agent cases, the existing real
  Process/Skill-acquisition Runtime Gate, and security regression. It stops on the first real
  failure and never converts missing integration conditions into success.
- `pnpm run acceptance:p2`: the #71 candidate sequence: deterministic quality/build/Doctor,
  migration and repository checks, real structural Interaction updates, graph result reuse,
  complete service-exit recovery, Human Input, asynchronous result routing, and security.
- `pnpm run acceptance:p3`: the #73 P3 sequence: deterministic gates plus real Interaction inbox,
  Human/async feedback, MCP, sub-agent, authenticated Webhook, timezone schedules, concurrent
  Automation workers, and security evidence.
- `pnpm run acceptance:release`: read-only v0.5 candidate closure for clean synchronized `anban`,
  package version, complete CLI help, both migration profiles, documentation, and protected files.
- `pnpm run acceptance:v0.5`: the complete exact-candidate S01-S12 chain. It runs P1, P2, P3, then
  release closure and stops at the first truthful failure.
- `pnpm run acceptance:security`: deterministic fail-closed and Secret-boundary tests.
- `pnpm run acceptance:v0.1`: retained v0.1 behavioral regression over the current package; local
  quality, Doctor base/online/web, database, model, Capability, Runtime, security, and current
  release-closure checks.

Gate E failure paths remain production-path tests: missing program, nonzero exit, timeout,
cancellation, output/Artifact failure, damaged Skill, model repair/exhaustion, database/Event
failure, Invocation compensation, Artifact cleanup, compensation failure, and unconfirmed commit
state. Fixtures provide invalid inputs only; they do not replace successful production execution.

Deterministic Runtime coverage also rejects premature finals, exercises three semantic completion
variants, enforces an exact alternative strategy/target, proves clarification and explicit failure,
and exhausts the finite replan budget without repeating the failed side effect. Model-dependent
S01-S04/S06/S12 completion evidence remains part of P1 Gate #72 rather than being fabricated by
the deterministic suite.
