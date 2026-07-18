# Anban / 安伴

Anban v0.1 is an executable Agent Runtime for local development and real functional validation.
One fixed General Agent uses a real OpenAI-compatible model, activates discovered Skills, executes
real programs through `process.execute`, uses bounded Task/Session context through
`memory.context`, and persists Invocation, Artifact, Context, Event, Audit, and Trace facts in
PostgreSQL.

```text
User task -> FixedGeneralAgent -> skill.activate -> process.execute
                              -> memory.context
          -> PersistedCapabilityPort -> PostgreSQL / managed Artifacts / Event / Audit / Trace
```

The production Capability surface is `memory.context`, `skill.activate`, and `process.execute`.
Skill is execution knowledge; Process is the general execution channel; Memory is structured,
bounded durable context. A Skill never writes Anban business tables. Runtime and Persistence own
Artifact snapshots and all durable facts. Supporting another concrete tool normally means adding
a Skill, not adding a Capability Handler.

All Skills use the same architecture regardless of whether they ship in the Anban package, were
installed by ClawHub, were copied, or were created by a user. Production discovers `SKILL.md`,
derives identity from its path and content, and does not read installation Lock, Origin, registry,
publisher, or fingerprint metadata. Scoped paths supply their `@owner/name` identity even when the
document is plain Markdown; unscoped frontmatter display names are normalized into `@local/*`.
The packaged `@anban/clawhub` is an ordinary Skill whose instructions use `process.execute` to call
the real CLI.

## Commands

```bash
anban workspace init
anban run "<task>"
anban chat
anban runs
anban trace <run-id>
anban artifacts <run-id>
anban context task <task-id>
anban context session <session-id>
anban capabilities list
python -m scripts.doctor
python -m scripts.doctor --online
python -m scripts.doctor --web
```

Activate Python 3.12, install the frozen Python and pnpm dependencies, initialize an external
Workspace, put runtime credentials only in its mode-0600 `secrets.env`, and apply both migration
profiles with `alembic upgrade head` and `ANBAN_DATABASE_PROFILE=test alembic upgrade head`.

`memory.context` can read, remember, supersede/conflict, compress, and expire bounded Task or
Session context. Compression records ordered source Entry IDs and retains the authoritative raw
rows. Secret-classified or configured protected values fail closed. CLI context inspection emits
only identities, classifications, counts, and hashes—not raw content or source references.

## v0.1 execution boundary

Anban v0.1 is an executable Agent Runtime for local development and real functional validation.
`process.execute` can run any program available to the operating-system user that started Anban and
inherits that process environment. This version provides no program allowlist, process sandbox,
command approval, network isolation, or fine-grained file permissions. Those governance controls
are deferred to later versions.

Programs are launched without an implicit shell. A Skill must explicitly call `bash`, `sh`,
PowerShell, or another installed shell when it needs pipelines or expansion. Process output,
stdin, time, arguments, and explicitly declared single-file Artifacts are bounded by `anban.toml`.
One Process invocation may collect multiple declared files only after all validate successfully.
Full arguments, environment, stdout, stderr, and physical paths never enter Event Metadata; safe
hashes, sizes, counts, status, duration, and a logical cwd scope do.

An ordinary failed Process Invocation remains durably failed. When it has a bounded safe
observation, the Agent may use that Tool Result to choose another valid approach; timeout,
cancellation, protected output, missing observations, and persistence failures remain terminal.
Stable pre-execution argument and availability failures are also persisted as failed before a
minimal safe Tool Result is returned for model replanning. Runtime never edits arguments or
replays the Capability. If terminal persistence fails, Runtime confirms the transaction state,
attempts one failed-Invocation compensation, and removes only uncommitted managed Artifact
snapshots whose ownership is certain.
User-visible final answers may report result paths, while Metadata and errors retain the stricter
physical-path prohibition.

See [architecture](docs/architecture/overview.md), [Workspace](docs/architecture/workspace.md),
[CLI](docs/cli.md), [security](SECURITY.md), and [real acceptance](scripts/acceptance/README.md).
