# ADR-0002: Workspace and Configuration

- Status: Accepted
- Date: 2026-07-17

## Context

Runtime configuration, credentials, installed Skills, run working data, and artifacts must not be mixed with source control or exposed through domain interfaces.

## Decision

The canonical local Workspace is `/Users/fanyuhang/AnbanWorkspace`.

- Stable configuration: `anban.toml`
- Secrets: `secrets.env`
- Installed Skills: `skills/`
- Per-run working directory: `runs/<run-id>/workspace/`
- Durable outputs: `artifacts/`

The Workspace root has mode 0700 and `secrets.env` has mode 0600. Database settings are referenced by environment-variable name from `anban.toml`; credentials are never embedded in TOML.

The repository `.env` is limited to bootstrap configuration for `ANBAN_WORKSPACE_DIR`. Workspace content remains outside Git.

Absolute host paths cannot enter Core, public APIs, model-visible content, audit data, or the frontend. Logical resources use `anban://...` URIs; adapters resolve them to local paths only at the execution boundary.

## Consequences

Local and CI environments may use different physical Workspace roots while preserving the same layout and logical identifiers. Readiness validates permissions, references, and isolation before execution.
