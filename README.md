# Anban / 安伴

Anban is a security-governed AI application foundation. This repository currently provides a development-ready baseline for a Python/FastAPI backend, LangGraph runtime integration, React frontend, PostgreSQL persistence, managed local Workspace, and repeatable real-environment validation.

It intentionally contains no product Task, Run, Agent, Graph, Skill Runtime, or Capability Registry implementation.

## Development

1. Install Miniforge and create the `anban` environment from `environment.yml`.
2. Activate the environment before running Python tools.
3. Set `ANBAN_WORKSPACE_DIR` to the managed Workspace when the operating-system default is not suitable.
4. Install JavaScript dependencies with `pnpm install --frozen-lockfile`.
5. Run `pnpm check`, `pnpm build`, and `pnpm run doctor`.

Use the explicit `run` form because pnpm also ships an unrelated built-in command named `doctor`.

See [local environment](docs/development/local-environment.md), [architecture overview](docs/architecture/overview.md), and [contributing](CONTRIBUTING.md).

## Security

Credentials belong in the managed Workspace `secrets.env`, never in Git. Read [SECURITY.md](SECURITY.md) before reporting a vulnerability or handling real-provider configuration.
