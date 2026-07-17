# Environment and Real Acceptance Policy

Anban separates deterministic repository checks, local machine diagnosis, and scoped real
acceptance.

## Ordinary CI

Ordinary CI runs Ruff formatting and linting, Pyright, pytest, frontend checks and tests, frontend
build, and Secret scanning. It does not read real model credentials, install a ClawHub Skill, call
a model, or access third-party Skill services.

## Doctor

`pnpm run doctor` answers whether the current machine has the basic conditions to develop and run
Anban. It checks the active Python and Node toolchains, external managed Workspace, configuration
presence, both PostgreSQL migration heads, uniform Skill parsing, and a harmless production
Process. `--online` adds npx/ClawHub; `--web` adds Chromium.

Base Doctor is offline with respect to public services. It does not install dependencies or
Skills, call a model, run frontend tests/builds, inspect Git state, or inspect workflow content.

## Real acceptance

Real model, Skill, Capability, and end-to-end checks run only when required by the relevant Codex
development task. They are explicit parts of a Phase Gate or Version Gate, not a permanent
per-push readiness workflow. The fail-closed helpers under `scripts/acceptance/` cover real model
native Tool Calling, real Process/file/HTTP/Artifact work, source-independent Skill discovery,
ClawHub search/install as an external CLI operation, new-Application Skill execution, PostgreSQL
restart Trace, and security failure paths. Use the commands in the acceptance README.

Fake Models, Fake Capabilities, Mock Providers, Placeholder Executors, JSON-simulated Tool Calls,
mock success, and silent fallback are prohibited. Checks emit allowlisted summaries only;
credentials, Authorization data, provider responses, database passwords, and sensitive URLs are
never logged or documented.
