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
