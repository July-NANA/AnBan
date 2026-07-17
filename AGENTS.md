# AGENTS.md

## Project Overview

Anban / 安伴 is a security-governed AI application built around explicit architecture boundaries, real execution, auditable environment checks, and fail-closed integration behavior.

## Technology Stack

- Frontend: React + TypeScript
- Backend: Python + FastAPI
- Agent/Graph Runtime: LangGraph
- Database: PostgreSQL
- Python environment: any active Python 3.12 environment; Miniforge `anban` is recommended locally
- Python tooling: uv, Ruff, Pyright, pytest
- Frontend tooling: pnpm, Vitest, Playwright

## Backend Modules

- `interaction`: external input, output, feedback, and bidirectional event loops.
- `core`: authoritative but thin domain identities, relationships, and lifecycles for concepts such as Task, Run, and Graph.
- `runtime`: execution order, state machines, LangGraph scheduling, waiting, and recovery.
- `model`: thinking, reasoning, and generation through an independent Model Port.
- `capability`: Tool, Skill, MCP, external Agent, and other execution abilities.
- `persistence`: durable memory, state, checkpoints, artifacts, audit data, and traces.

## Architecture Rules

1. A Skill is a specialized Capability.
2. Model is an independent Port and is not part of the general Capability abstraction.
3. Interaction is a bidirectional loop, not a one-way input/output pipeline.
4. Core stays thin and authoritative; it must not become a god module.
5. Runtime owns execution discipline and must not duplicate capabilities already provided by LangGraph.
6. Harness engineering is a cross-cutting requirement; do not create a Harness module or HarnessProfile.
7. Future integrations enter through Interaction Adapters, Model Adapters, or Capability Adapters.
8. Do not create core bypasses for a specific Skill, source, or Provider.
9. Do not use Fake Models, Fake Capabilities, Mock Success, Placeholder Executors, or fallback success.
10. Missing real model or execution conditions must fail explicitly.
11. Secrets must never enter Git, APIs, logs, model output, audit output, or documentation.
12. A Codex task may execute a complete delivery, but it must use phases, focused commits, and repeated acceptance.

## Architecture Authorization

- The six product modules are fixed as `interaction`, `core`, `runtime`, `model`, `capability`, and
  `persistence`. `config` is an authorized cross-cutting infrastructure package, not a seventh
  product module.
- Adding a Port, Protocol, Adapter type, Capability Handler, Tool name, provider, persistence
  backend, Interaction Adapter, top-level product package, or plugin interface requires explicit
  user authorization. An ADR records an already-authorized decision and does not grant authority.
- Architecture allowlist tests must not be updated merely to make a test pass. Any allowlist change
  requires explicit architecture authorization.
- Every delivery must report an Architecture Delta. When there is no architecture change, report
  `Architecture delta: none`.

## Capability Reuse

Capability Handlers are scarce architecture extension points. When an existing Capability can
perform a task with reasonable reliability, cost, latency, and complexity, a Skill must describe
how to use that Capability instead of adding a Handler. Convenience, a prettier schema, shorter
prompts, or easier tests are not sufficient reasons to add a Capability.

A new Capability is allowed only when an existing Capability cannot express the need; measured
reliability, latency, cost, or context use is unacceptable; a newly approved Anban domain entity is
required; a single process cannot correctly own required persistent interaction, streaming,
credential lifecycle, or cancellation; or the user explicitly authorizes it.

All Skills use the same discovery, parsing, activation, execution, and persistence path. Production
Runtime code must not special-case a Skill by source, installer, registry, publisher, slug, metadata
format, or installation record. Skill installation metadata such as Lock or Origin files is not a
production loading or trust boundary.

## Acceptance Integrity

- A structurally valid, executable Provider response must not be rejected solely for a non-critical
  formatting preference. Native Tool Calls are authoritative when present and valid; companion
  assistant text is non-authoritative and must not become another execution channel.
- Production behavior must never recognize or branch on a fixed acceptance Prompt, Skill slug,
  Weather, Sydney, URL, Run ID, test filename, expected output, CI marker, local machine path, or
  model name.
- Production Agent prompts must not prescribe an acceptance-specific Tool order.
- Invocation, Artifact, Event, HTTP result, file, execution result, and success state must never be
  fabricated. A real failure must not be converted into ordinary success.
- Fake Models, Fake Capabilities, Mock Success, Placeholder Executors, and fallback success are
  forbidden in production and black-box acceptance.
- Deterministic test fixtures may supply controlled inputs but must not alter the production
  execution path. Black-box acceptance must use the ordinary production Composition Root.
- When a scenario cannot be reproduced reliably, retain diagnostics and report the limitation; do
  not add a production scenario branch.

## Python Environment

- Required Python version: 3.12.
- Product commands, Doctor, acceptance, and CI use the currently active Python environment.
- Miniforge with environment name `anban` is the recommended primary-workstation setup, not a
  product or release requirement. Optional activation:

  ```bash
  source "$(conda info --base)/etc/profile.d/conda.sh"
  conda activate anban
  ```

- Doctor must inspect the active interpreter and installed dependencies; it must not infer runtime
  facts from paths, Conda variables, or AGENTS.md.
- Do not use a Python version other than 3.12 and do not create an additional environment when an
  appropriate active environment already exists.
- Run uv, Ruff, Pyright, pytest, and project Python commands from the same active environment.

## Workspace and Secrets

- The managed local Workspace is separate from the repository.
- The verified primary workstation path is `/Users/fanyuhang/AnbanWorkspace`; it is informational rather than a portable runtime requirement.
- Workspace Bootstrap resolution is authoritative and may select a different external physical path on another workstation or in CI.
- Repository `.env` files may only bootstrap `ANBAN_WORKSPACE_DIR`; runtime credentials belong in the Workspace `secrets.env`.
- Never commit `.env`, Workspace content, credentials, database passwords, or provider responses.

## Repository Workflow

- `main` is the stable release branch.
- `anban` is the only development, integration, remediation, and acceptance branch.
- Work directly and serially on `anban`; do not create feature or Batch branches unless the user changes this policy.
- Pull requests are not part of the local Codex workflow, and `main` must not be modified directly.
- Only one repository writer may be active at a time.
- Before a delivery starts, record the approved base SHA and verify `anban` is clean and synchronized with `origin/anban`.
- Use focused commits and validate each scope before pushing.
- Never force-push, reset, rebase, squash, or amend pushed history. Correct pushed failures with a new remediation commit.
- Final acceptance requires local Gates and applicable remote CI on the exact pushed SHA.
- Do not create Issues, Epics, Milestones, roadmap items, release tags, or version scope unless the user explicitly requests them.

## Source File Size Limit

Authored Python and frontend source under `anban/`, `apps/`, `packages/`, `scripts/`, and `tests/` should stay below 800 lines. Generated files, lockfiles, build output, vendored dependencies, and migrations are excluded. Split approaching or oversized files into focused modules; do not compress code unnaturally to meet the limit.

## Development Rules

- Do not add production dependencies casually; document the reason.
- Do not introduce infrastructure without an ADR.
- Do not bypass Ruff, Pyright, pytest, frontend checks, doctor, or scoped real acceptance Gates.
- PostgreSQL stores business data; do not introduce Redis, Celery, Kafka, RabbitMQ, a vector database, or another database without an approved architecture change.
- Before changing behavior, inspect related architecture documents and ADRs.
- Development-readiness work must not implement Task, Run, Agent, Graph, Capability Registry, Skill Runtime, Audit, Trace, or other product behavior.

## Definition of Done

- Python formatting, linting, types, and tests pass.
- Frontend types, tests, and build pass.
- Doctor passes for the local development environment.
- Relevant real model, Skill, Capability, and end-to-end acceptance passes in the scoped Codex Phase or Version Gate when required.
- Relevant architecture and development documentation is current.
- No secret is tracked or emitted.
- The exact pushed `anban` SHA passes all applicable remote CI.
