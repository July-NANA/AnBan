# Changelog

All notable changes are documented here.

## [Unreleased]

### Added

- v0.1 CLI commands for Workspace initialization, durable task execution, bounded temporary chat,
  Run listing/detail, Trace, and Artifact inspection.
- Typed Task, ExecutionRun, NodeRun, CapabilityInvocation, Artifact, and Event contracts with
  guarded terminal lifecycle and safe structured errors.
- PostgreSQL migrations, focused repositories, short Unit of Work transactions, aggregate rebuild,
  and deterministic ordered Event queries.
- OpenAI-compatible ModelPort with native Tool Calling, Tool Result pairing, structured output,
  timeout/error classification, bounded responses, and known-Secret response rejection.
- Governed file/process Capabilities, approved Workspace Skill activation, and the fixed LangGraph
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

- v0.1 is CLI-only with one fixed General Agent and one active Skill.
- Chat context is process-local, limited to eight user inputs or 15 minutes, and cannot resume after
  restart.
- The default CLI has no allowlisted process executable; the process boundary is exercised only by
  controlled acceptance wiring.
- Dynamic graphs, multiple Agents, browser, MCP, memory, schedules/webhooks, checkpoint resume,
  policy/approval systems, strong sandboxing, replay, routing/fallback, and Skill installation are
  not implemented.
