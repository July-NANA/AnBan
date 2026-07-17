# Managed Workspace

The managed Workspace is an external local boundary for configuration, approved Skill source, Run
files, and Artifact bytes. It must not be the repository, a repository child, the user's home root,
or a filesystem root.

```text
<workspace>/
├── anban.toml
├── secrets.env
├── .clawhub/lock.json
├── skills/@steipete/weather/SKILL.md
├── runs/<run-id>/workspace/
└── artifacts/<run-id>/<artifact-id>
```

`anban workspace init` is idempotent. It creates the root, `anban.toml`, and mode-0600
`secrets.env` when absent; it never replaces existing configuration or Secret content and never
prints the physical path or a Secret value.

Workspace resolution uses, in order, `ANBAN_WORKSPACE_DIR`, the repository `.env` bootstrap value,
or the supported operating-system default. The repository `.env` may contain only the Workspace
bootstrap location. Runtime model and database values belong in Workspace `secrets.env`.

The approved v0.1 Skill is `@steipete/weather@1.0.0`. Discovery requires its exact source file,
lock record, publisher, pin, version, and approved SHA-256
`1ca0c8d768ad603ea8d5d47f56a9b435fe575f7f34e719eda85c82003d740e93`. Anban activates at most one
Skill and loads only the bounded `SKILL.md` projection; references and assets are not loaded by
default. Anban does not install, update, search, or fetch a Skill.

Each Run receives a contained filesystem root. `file.list`, `file.read`, and `file.write` reject
absolute paths, `..` traversal, and symlinks that resolve outside that Run root. Artifact snapshots
use logical `anban://artifact/...` URIs. Physical paths stay inside adapters and do not enter Core,
model messages, PostgreSQL metadata, CLI output, Event, Audit, or Trace.
