# Managed Workspace

The managed Workspace is the local execution and configuration boundary. It is deliberately separate from the Git repository.

```text
AnbanWorkspace/
├── anban.toml
├── secrets.env
├── skills/
├── runs/
├── artifacts/
├── cache/
├── logs/
└── tmp/
```

`anban.toml` stores stable references and non-secret configuration. `secrets.env` stores local credentials and database URLs. Installed Skills live under `skills/`. Each future run may use `runs/<run-id>/workspace/`; durable outputs belong under `artifacts/`.

Absolute host paths are adapter concerns. Core, APIs, model-visible content, audit output, and the frontend use logical `anban://...` identifiers and must not expose the Workspace root.
