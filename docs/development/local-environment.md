# Local Environment

The development environment is portable. Runtime authority comes from the active Python 3.12
environment and Workspace bootstrap, not from an environment manager or workstation path.

## Toolchain

- Recommended local environment: Miniforge environment `anban`
- Python: `3.12`
- Python dependency/tool runner: `uv`
- JavaScript package manager: the pnpm version declared by the root `packageManager`
- Business database: PostgreSQL, with separate development and test profiles

Miniforge is optional. After activating any Python 3.12 environment, install locked dependencies
and the console script into that same environment with:

```bash
uv export --frozen --all-groups --no-emit-project --format requirements-txt \
  --output-file /tmp/anban-requirements.txt
python -m pip install -r /tmp/anban-requirements.txt
python -m pip install --no-deps -e .
```

## Workspace and configuration

Resolve an external Workspace and run `anban workspace init`. `anban.toml` contains only
allowlisted references; `secrets.env` contains the corresponding local values and must remain mode
0600. Required keys are:

```text
DATABASE_URL
ANBAN_TEST_DATABASE_URL
OPENAI_COMPATIBLE_BASE_URL
OPENAI_COMPATIBLE_API_KEY
OPENAI_COMPATIBLE_MODEL
```

Both database values must use the SQLAlchemy `postgresql+asyncpg` driver. Values and passwords are
not documentation or fixtures. Apply migrations with `alembic upgrade head`; select the test
database with `ANBAN_DATABASE_PROFILE=test`.

MCP is optional and uses the official Python SDK locked to stable major v1 (`mcp>=1.27,<2`). Add
server declarations under `[[capability.mcp.servers]]`; keep any referenced environment values in
`secrets.env`. Doctor performs real initialization and Tool discovery for configured servers but
does not invoke a Tool.

The packaged `@anban/clawhub` Skill is always available. Any valid Workspace `SKILL.md` is loaded
through the same parser without consulting installer metadata. The current Application refreshes
the catalog on inventory observation and activation; a restart is not required after installation.

## Checks and responsibility

- `pnpm check`: Ruff, Pyright strict, pytest, frontend typecheck, and frontend tests.
- `pnpm build`: deterministic frontend baseline build.
- `pnpm run doctor`: base toolchain, Workspace/configuration, PostgreSQL migration heads, uniform
  Skill parsing, a harmless production Process, and configured MCP discovery.
- `python -m scripts.doctor --online`: npx and current ClawHub CLI.
- `python -m scripts.doctor --web`: frontend Chromium.
- `pnpm run acceptance:model`, `acceptance:capability`, `acceptance:runtime`, and
  `acceptance:security`: explicit real/scoped Gate commands.

Ordinary CI runs deterministic checks, build, and Secret scanning on the exact commit. It does not
receive real model credentials or call a public model or third-party Skill service.
