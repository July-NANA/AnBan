# Environment and Real Acceptance Policy

Anban separates deterministic repository checks, local machine diagnosis, and scoped real
acceptance.

## Ordinary CI

Ordinary CI runs Ruff formatting and linting, Pyright, pytest, frontend checks and tests, frontend
build, and Secret scanning. It does not read real model credentials, install a ClawHub Skill, call
a model, or access a weather service.

## Doctor

`pnpm run doctor` answers whether the current machine has the basic conditions to develop and run
Anban. It checks the active Python and Node toolchains, external managed Workspace, configuration
presence, development and test PostgreSQL connectivity with rollback-safe temporary writes, the
approved local Skill files and lock record, and a minimal Playwright Chromium launch.

Doctor is offline with respect to public services. It does not install dependencies or Skills,
call a model, query weather, run frontend tests or builds, inspect Git branch/worktree state, or
inspect GitHub workflow content. Missing local prerequisites fail explicitly.

## Real acceptance

Real model, Skill, Capability, and end-to-end checks run only when required by the relevant Codex
development task. They are explicit parts of a Phase Gate or Version Gate, not a permanent
per-push readiness workflow. The fail-closed helpers under `scripts/acceptance/` cover real model
native Tool Calling, the pinned Weather Skill and its documented public request, real file/process
Capability boundaries, the PostgreSQL Runtime slice, installed-CLI restart E2E, and security
failure probes. Use the commands documented in the acceptance README; ordinary CI never runs them
implicitly.

Fake Models, Fake Capabilities, Mock Providers, Placeholder Executors, JSON-simulated Tool Calls,
mock success, and silent fallback are prohibited. Checks emit allowlisted summaries only;
credentials, Authorization data, provider responses, database passwords, and sensitive URLs are
never logged or documented.
