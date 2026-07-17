# Changelog

All notable changes are documented here.

## [Unreleased]

### Added

- Uniform package/Workspace `SKILL.md` discovery and ordinary packaged `@anban/clawhub` Skill;
  installation metadata is intentionally ignored by production.
- General PATH/absolute executable Process with inherited environment, env overlays, cwd, stdin,
  configurable budgets, safe execution summaries, and atomic multi-file Artifact snapshots.
- Architecture-surface tests and scoped Doctor online/web modes.
- Provider-compatible native Tool Call normalization that ignores non-authoritative companion text
  without weakening Tool identity, arguments, finish-reason, or Secret validation.
- Deterministic Skill conflicts: Workspace `@anban/*` is reserved and ordinary duplicate slugs are
  all excluded; version and installer metadata are not production identity.
- Uniform parsing of scoped plain-Markdown Skills and deterministic normalization of unscoped
  frontmatter display names, without installer-specific branches.
- Recoverable failed Tool observations return to the model without rewriting the failed Invocation;
  user-visible results may report legitimate paths while Metadata keeps its stricter path boundary.

- v0.1 CLI commands for Workspace initialization, durable task execution, bounded temporary chat,
  Run listing/detail, Trace, and Artifact inspection.
- Typed Task, ExecutionRun, NodeRun, CapabilityInvocation, Artifact, and Event contracts with
  guarded terminal lifecycle and safe structured errors.
- PostgreSQL migrations, focused repositories, short Unit of Work transactions, aggregate rebuild,
  and deterministic ordered Event queries.
- OpenAI-compatible ModelPort with native Tool Calling, Tool Result pairing, structured output,
  timeout/error classification, bounded responses, and known-Secret response rejection.
- General Process Capability, uniform Workspace Skill activation, and the fixed LangGraph
  General Agent with turn, call, time, repetition, and no-progress bounds.
- Durable Runtime coordination and allowlisted Audit/Trace projections from one Event fact source.
- Explicit real Model, Skill, Capability, CLI E2E, PostgreSQL restart, and security acceptance
  commands that remain outside ordinary CI.

### Security

- Fail-closed validation for configuration, provider data, Tool Calls, Capability arguments,
  Workspace containment, external symlinks, process termination/output, persistence/Event failure,
  and interruption.
- Logical Artifact URIs and allowlisted metadata prevent physical paths, credentials, database
  URLs, raw Provider responses, and unbounded process output from entering CLI/Audit/Trace output.
- Ambiguous post-side-effect Event failure never triggers automatic Capability replay.

### Known limitations

- v0.1 is CLI-only with one fixed General Agent; Skills may be activated repeatedly without shared
  mutable activation state.
- Chat context is process-local, limited to eight user inputs or 15 minutes, and cannot resume after
  restart.
- Process uses the launching OS user's permissions; program allowlists, sandboxing, approvals,
  network isolation, and fine-grained file permissions are deferred.
- Dynamic graphs, multiple Agents, browser, MCP, memory, schedules/webhooks, checkpoint resume,
  policy/approval systems, strong sandboxing, replay, and routing/fallback are not implemented.
