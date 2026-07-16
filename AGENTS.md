# AGENTS.md

## Project Overview

Anban / 安伴 is a security-governed AI application built around explicit architecture boundaries, real execution, auditable environment checks, and fail-closed integration behavior.

## Technology Stack

- Frontend: React + TypeScript
- Backend: Python + FastAPI
- Agent/Graph Runtime: LangGraph
- Database: PostgreSQL
- Python environment: Miniforge conda environment `anban`, Python 3.12
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

## Python Environment

- Distribution: Miniforge
- Conda environment name: `anban`
- Required Python version: 3.12
- Activation:

  ```bash
  source "$(conda info --base)/etc/profile.d/conda.sh"
  conda activate anban
  ```

- Verified primary workstation interpreter: `/Users/fanyuhang/miniforge3/envs/anban/bin/python`
- This path is informational, not a portable runtime requirement. The authoritative requirements are Miniforge, environment `anban`, and Python 3.12.
- Doctor must use the active environment facts and must not parse AGENTS.md for runtime configuration.
- Do not use the macOS system Python.
- Do not create a second virtual environment outside this Miniforge environment.
- Run uv, Ruff, Pyright, pytest, and project Python commands only after activating this environment or through `conda run -n anban`.
- Other developers and CI may use a different Miniforge installation path, but the environment name and Python version must remain the same.

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
