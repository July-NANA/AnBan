# Anban / 安伴

Anban is a security-governed AI application foundation. This repository currently provides a development-ready baseline for a Python/FastAPI backend, LangGraph runtime integration, React frontend, PostgreSQL persistence, and a managed local Workspace.

It intentionally contains no product Task, Run, Agent, Graph, Skill Runtime, or Capability Registry implementation.

## Development

1. Install Miniforge and create the `anban` environment from `environment.yml`.
2. Activate the environment before running Python tools.
3. Set `ANBAN_WORKSPACE_DIR` to the managed Workspace when the operating-system default is not suitable.
4. Install JavaScript dependencies with `pnpm install --frozen-lockfile`.
5. Run `pnpm check` and `pnpm build` for deterministic quality checks.
6. Run `pnpm run doctor` to diagnose this machine's local development prerequisites.

Use the explicit `run` form because pnpm also ships an unrelated built-in command named `doctor`.

Ordinary CI runs Ruff, Pyright, pytest, frontend checks and tests, frontend build, and Secret
scanning. Doctor checks the local toolchain, Workspace, configuration presence, PostgreSQL,
approved local Skill files, and Chromium; it does not call a model or public service. Scoped Codex
Phase and Version Gates explicitly run real model, Skill, Capability, and end-to-end acceptance
when the work requires it. See `scripts/acceptance/README.md` for the available helpers.

See [local environment](docs/development/local-environment.md), [architecture overview](docs/architecture/overview.md), and [contributing](CONTRIBUTING.md).

## Security

Credentials belong in the managed Workspace `secrets.env`, never in Git. Read [SECURITY.md](SECURITY.md) before reporting a vulnerability or handling real-provider configuration.
