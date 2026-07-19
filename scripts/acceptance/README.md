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
  through new query Applications. Async-result, Webhook, schedule, and unknown-resume reverse
  inputs fail explicitly without creating a Run; their owned execution remains D27-D28.
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
- `pnpm run acceptance:security`: deterministic fail-closed and Secret-boundary tests.
- `pnpm run acceptance:v0.1`: local quality, Doctor base/online/web, database, model, Capability,
  Runtime, security, and release-closure checks.

Gate E failure paths remain production-path tests: missing program, nonzero exit, timeout,
cancellation, output/Artifact failure, damaged Skill, model repair/exhaustion, database/Event
failure, Invocation compensation, Artifact cleanup, compensation failure, and unconfirmed commit
state. Fixtures provide invalid inputs only; they do not replace successful production execution.

Deterministic Runtime coverage also rejects premature finals, exercises three semantic completion
variants, enforces an exact alternative strategy/target, proves clarification and explicit failure,
and exhausts the finite replan budget without repeating the failed side effect. Model-dependent
S01-S04/S06/S12 completion evidence remains part of P1 Gate #72 rather than being fabricated by
the deterministic suite.
