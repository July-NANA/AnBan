# AGENTS.md

## Project Overview

Anban / 安伴 is a security-governed AI agent runtime application. It focuses on controlled agent execution, auditability, skill and tool governance, workflow orchestration, and future desktop operation.

## Repository Layout

- `apps/web`: React + TypeScript + Vite frontend.
- `apps/api`: Rust Axum API service.
- `apps/desktop`: reserved for future Tauri desktop work.
- `crates/*`: Rust workspace crates for runtime, audit, skill, tool, governance, ClawHub, workflow, and LLM domains.
- `packages/*`: shared frontend packages.
- `docs/*`: specs, ADRs, and product notes.

## Required Commands

```bash
pnpm install
pnpm check
pnpm build:web
cargo fmt --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

## Development Rules

- Do not commit `.env` or any secret.
- Do not write real tokens into README, workflow, test, fixture, example, or demo code.
- Do not add production dependencies casually; explain the reason before adding one.
- Do not introduce new infrastructure technology unless an ADR already exists or is updated.
- Do not bypass Rust clippy, rustfmt, or TypeScript checks.
- Do not use Supabase as the main business database.
- Do not use a third-party Agent framework as the core Agent Runtime.
- Prefer focused commits with one clear scope.
- Before modifying behavior, check related docs, ADRs, and specs.

## Repository Workflow

- `anban` is the single active development, integration, remediation, governance, and acceptance branch.
- Work directly and serially on `anban`. Feature, fix, documentation, and Batch branches are disabled unless the user explicitly requests a policy change.
- Pull requests are not part of the current local Codex workflow, and `main` must not be operated directly.
- Only one Batch and one repository writer may be active at a time. Do not allow another tool, Codex task, or user to modify or push the repository concurrently.
- Before any write, confirm the current branch is `anban`, local `anban` equals `origin/anban`, ahead/behind is `0/0`, the working tree is clean, the approved base SHA is recorded, and applicable remote CI for that SHA succeeded.
- Commit valid changes directly to `anban` only after the relevant local Gate passes. Keep each commit focused, clearly titled, and linked to the relevant Issue or Issues.
- Do not use `Closes`, `Fixes`, or `Resolves` unless performing formal acceptance.
- Never force-push, reset, rebase, squash, or amend pushed history. Correct failures after push with a new remediation commit on `anban`.
- If exact-SHA CI fails after push, keep the active Issues in `in progress` or `review`, stop the next Batch, and remediate with a new commit.
- Acceptance reviews the approved-base-to-current-`anban` diff and direct commits, confirms local Gates and exact-SHA remote CI, then updates and closes the accepted Issues.

## Source File Size Limit

For all future development, source code files should stay under 800 lines.

Scope:

- Applies to authored source code under `apps/`, `crates/`, and `packages/`.
- Includes application code, library code, tests, and frontend source files.
- Excludes generated files, lockfiles, build outputs, vendored dependencies, and migration files unless they are hand-authored and practically maintainable.

Rules:

- Do not create a new source file over 800 lines.
- Do not expand an existing source file beyond 800 lines.
- When a file approaches the limit, split it into focused modules before adding more logic.
- When touching an existing file that already exceeds 800 lines, prefer splitting it as part of the change or create/attach a refactor issue before adding more logic.
- Do not artificially compress code to stay under the limit; keep readability and clear module boundaries.
- Public APIs should remain stable unless an issue explicitly approves breaking changes.

## Architecture Rules

- Agent Runtime is self-developed.
- Controlled Agent Loop is self-developed.
- SkillManifest, ToolManifest, and AuditEvent are self-developed.
- Supabase is only responsible for Auth.
- PostgreSQL stores business data, Run / Step, Audit Event, Skill Registry, and Tool Registry.
- V0.1 focuses on executable agents and recordable operation traces.
- V0.5 focuses on ClawHub Skill search, installation, and governance.
- V1.0 focuses on the security-governed runtime core.
- V2.0 focuses on a complete platform.

## Definition of Done

- Code is formatted.
- Frontend checks pass.
- Rust clippy passes.
- Rust tests pass.
- Related documentation is updated.
- Related ADR is updated or confirmed unnecessary.
- Commits reference the relevant Issue or Issues.
- The exact pushed `anban` SHA passes applicable remote CI.
- No secret is committed.
