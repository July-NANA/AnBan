# Changelog

All notable changes are documented here.

## [Unreleased]

### Added

- Complete v0.5 Main Agent inventory/sufficiency, multi-Skill and multi-Capability execution,
  bounded completion assessment, alternative enforcement, clarification, failure, and replanning.
- Validated `TaskGraphSpec`, immutable PostgreSQL `GraphRevision`, topology-independent dynamic
  LangGraph execution, result invalidation/reuse, Checkpoints, cancellation, and full restart.
- Durable Task/Session Context and the `memory.context` Capability with atomic compression,
  conflict/supersession, expiry, and safe Audit projection.
- One Interaction Gateway and PostgreSQL inbox for new work, Human Input, mid-run changes,
  asynchronous Process/MCP/sub-agent results, deduplication, expiry, and restart reconstruction.
- Real MCP stdio discovery/invocation and independently durable `agent.delegate` child Runs with
  preserved Artifact provenance and correlated result aggregation.
- Authenticated HMAC-SHA256 Webhook ingress for new and eligible-resume work through the durable
  Interaction inbox, with restart-safe deduplication and a real HTTP acceptance Gate.
- Immutable PostgreSQL Cron/Interval schedule definitions with strict five-field validation, IANA
  timezone/DST calculation, safe CLI inspection, and fresh-process acceptance.
- Concurrent durable Schedule occurrence claims, overlap `skip`, missed `skip`/`catch_up_once`,
  bounded lease recovery, ordinary Interaction dispatch, and restart-safe inbox replay protection.
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
- Stable pre-execution argument and availability failures are persisted as failed and returned as
  minimal safe Tool Results for model replanning, without Runtime argument mutation or replay.
- Capability terminal writes now confirm ambiguous commits, compensate confirmed uncommitted
  Invocations once, and clean only the current result's uncommitted managed Artifact snapshots.

### Security

- Fail-closed validation for configuration, provider data, Tool Calls, Capability arguments,
  Workspace containment, external symlinks, process termination/output, persistence/Event failure,
  and interruption.
- Logical Artifact URIs and allowlisted metadata prevent physical paths, credentials, database
  URLs, raw Provider responses, and unbounded process output from entering CLI/Audit/Trace output.
- Ambiguous post-side-effect persistence never triggers Capability replay; committed state is
  confirmed, while uncommitted state is compensated or reported explicitly if compensation fails.

### Known limitations

- v0.5 remains CLI-first; React/React Flow, visual Replay/Fork, the full Policy Engine, RBAC, and
  multi-user collaboration remain outside this release.
- Chat context is process-local, limited to eight user inputs or 15 minutes, and cannot resume after
  restart; durable asynchronous work uses Checkpoints and Interaction correlations instead.
- Process uses the launching OS user's permissions; program allowlists, sandboxing, approvals,
  network isolation, and fine-grained file permissions are deferred.
- Browser, policy/approval systems, strong sandboxing, visual/general replay, and routing fallback
  are not implemented.

## [0.1.0] - 2026-07-17

### Added

- CLI commands for Workspace initialization, durable task execution, bounded temporary chat, Run
  listing/detail, Trace, and Artifact inspection.
- Typed Task, ExecutionRun, NodeRun, CapabilityInvocation, Artifact, and Event contracts with
  guarded terminal lifecycle and safe structured errors.
- PostgreSQL migrations, focused repositories, short Unit of Work transactions, aggregate rebuild,
  and deterministic ordered Event queries.
- OpenAI-compatible ModelPort with native Tool Calling, bounded structured responses, failure
  classification, and known-Secret response rejection.
- General Process Capability, uniform Workspace Skill activation, fixed LangGraph Agent, and real
  Model/Skill/Capability/PostgreSQL/security acceptance.
