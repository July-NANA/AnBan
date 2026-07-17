# Local Environment

The development environment is portable. Runtime authority comes from the active Miniforge
environment and Workspace bootstrap, not from a workstation path recorded in documentation.

## Toolchain

- Miniforge environment: `anban`
- Python: `3.12`
- Python dependency/tool runner: `uv`
- JavaScript package manager: the pnpm version declared by the root `packageManager`
- Business database: PostgreSQL, with separate development and test profiles

Do not use the macOS system Python or create a second repository-local virtual environment. After
activating `anban`, install locked dependencies and the console script into that environment with:

```bash
uv export --frozen --all-groups --no-emit-project --format requirements-txt \
  --output-file "$CONDA_PREFIX/anban-requirements.txt"
uv pip install --python "$CONDA_PREFIX/bin/python" \
  -r "$CONDA_PREFIX/anban-requirements.txt"
uv pip install --python "$CONDA_PREFIX/bin/python" -e .
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

The Workspace must contain the pinned `@steipete/weather@1.0.0` Skill. Doctor verifies the local
source and approved hash without installing, updating, or contacting a public service.

## Checks and responsibility

- `pnpm check`: Ruff, Pyright strict, pytest, frontend typecheck, and frontend tests.
- `pnpm build`: deterministic frontend baseline build.
- `pnpm run doctor`: active toolchain, Workspace, configuration presence, PostgreSQL profiles,
  pinned Skill files, and local Chromium. It does not call public services.
- `pnpm run acceptance:model`, `acceptance:skill`, `acceptance:capability`, `acceptance:e2e`, and
  `acceptance:security`: explicit real/scoped Gate commands; see the acceptance README.

Ordinary CI runs deterministic checks, build, and Secret scanning on the exact commit. It does not
receive real model credentials or call a public model or weather service.
