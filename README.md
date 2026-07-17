# Anban / 安伴

Anban v0.1 is a security-governed, CLI-only AI runtime. It executes one real task through a fixed
LangGraph General Agent, an OpenAI-compatible model, an approved Workspace Skill, governed local
Capabilities, and PostgreSQL. A completed or failed Run remains inspectable from a new process.

```text
CLI -> Task / ExecutionRun -> fixed General Agent -> ModelPort
    -> Workspace Skill / Capability -> PostgreSQL + Artifact + Event
    -> Audit / Trace projection -> final CLI result
```

## v0.1 commands

```bash
anban workspace init
anban run "<task>"
anban chat
anban runs
anban run show <run-id>
anban trace <run-id>
anban artifacts <run-id>
```

Every command supports `--json`. Query commands rebuild their result from PostgreSQL and Artifact
metadata; they do not depend on process memory or require model configuration.

The default CLI exposes real `file.list`, `file.read`, `file.write`, and `skill.activate`
Capabilities. `process.execute` uses the same governed boundary, but the default CLI enables no
executable; a caller must provide an explicit allowlist in composition code. v0.1 acceptance uses
one controlled Python mapping to verify the no-shell process boundary.

See the [CLI reference](docs/cli.md) for output, limits, and exit behavior.

## Clean-checkout setup

1. Activate any Python 3.12 environment. Miniforge with the `anban` environment from
   `environment.yml` is the recommended local option, but is not required.
2. Install locked Python/frontend dependencies into that same environment and install the editable
   console script:

   ```bash
   uv export --frozen --all-groups --no-emit-project --format requirements-txt \
     --output-file /tmp/anban-requirements.txt
   python -m pip install -r /tmp/anban-requirements.txt
   python -m pip install --no-deps -e .
   pnpm install --frozen-lockfile
   ```

3. Resolve an external managed Workspace. Set `ANBAN_WORKSPACE_DIR` when the OS default is not the
   intended location, then run `anban workspace init`.
4. In the external Workspace, configure the existing environment-variable references in
   `anban.toml` and place their values only in mode-0600 `secrets.env`. Required keys are
   `DATABASE_URL`, `ANBAN_TEST_DATABASE_URL`, `OPENAI_COMPATIBLE_BASE_URL`,
   `OPENAI_COMPATIBLE_API_KEY`, and `OPENAI_COMPATIBLE_MODEL`.
5. Provision the pinned `@steipete/weather@1.0.0` Skill through a reviewed external process at the
   Workspace location described in [Managed Workspace](docs/architecture/workspace.md). Anban
   validates its lock record and
   approved content hash; it does not install or update Skills.
6. Apply migrations:

   ```bash
   alembic upgrade head
   ANBAN_DATABASE_PROFILE=test alembic upgrade head
   ```

7. Run deterministic checks:

   ```bash
   pnpm check
   pnpm build
   pnpm run doctor
   ```

`doctor` checks local prerequisites and configuration presence, but never calls a model or public
weather service. Ordinary CI is also deterministic. Real integrations run only through the
explicit commands in [Real acceptance](scripts/acceptance/README.md).

## Architecture and persistence

The six backend modules are `interaction`, `core`, `runtime`, `model`, `capability`, and
`persistence`. Interaction enters Runtime; Runtime depends on Core contracts and the Model,
Capability, and persistence Ports; concrete provider, Workspace, PostgreSQL, and CLI code are
adapters. Core has no SQLAlchemy, LangGraph, provider, CLI, or concrete Capability dependency.

PostgreSQL is authoritative for Task, ExecutionRun, NodeRun, CapabilityInvocation, Artifact
metadata, and the ordered Event stream. Artifact bytes live in the external Workspace and are
addressed by logical `anban://artifact/...` URIs. Audit and Trace are safe projections of the one
Event fact source, not duplicate event stores.

Read [ADR-0003](docs/adr/0003-v0.1-core-runtime-cli.md), the
[architecture overview](docs/architecture/overview.md), and the
[lifecycle/error contract](docs/architecture/lifecycle-and-errors.md).

## Security and explicit limits

The Runtime fails closed for missing configuration, invalid model or Tool Call data, unknown or
invalid Capabilities, path escape, external symlinks, process timeout/output overflow, database or
Event write failure, and interruption. It does not replay a Capability after an ambiguous
post-side-effect persistence failure. Provider responses and process output are bounded; raw
provider data, credentials, database URLs, Authorization data, and physical Workspace paths are
not Event, Audit, Trace, or CLI fields.

v0.1 deliberately excludes a React product UI, dynamic graphs, multiple Agents, branching or
parallel execution, browser, MCP, long-term memory, Cron, Webhook, checkpoint resume, a complete
Policy Engine, approvals, a strong container sandbox, Replay, model routing/fallback, and Skill
search or installation.

Read [SECURITY.md](SECURITY.md) before handling real credentials or reporting a vulnerability.

## Troubleshooting

- `configuration_missing`: initialize the external Workspace and add the required value to
  `secrets.env`; never put runtime credentials in the repository `.env`.
- `capability_execution_failed` during startup: verify the approved Skill directory and lock
  record with `pnpm run doctor`.
- `persistence_unavailable`: verify the selected PostgreSQL profile, apply migrations, then rerun
  Doctor.
- `model_transport_failed` or `model_timeout`: verify the configured provider independently; Anban
  does not fall back to a fake model.
- A failed Run is still queried with `anban run show <run-id>` and `anban trace <run-id>` when its
  durable identity was created successfully.

Development policy is in [CONTRIBUTING.md](CONTRIBUTING.md). The v0.1 release candidate notes are
in [docs/releases/v0.1.0.md](docs/releases/v0.1.0.md).
