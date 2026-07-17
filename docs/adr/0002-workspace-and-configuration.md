# ADR-0002: Workspace and Configuration

- Status: Accepted
- Date: 2026-07-17

## Context

Runtime configuration, credentials, installed Skills, run working data, and artifacts must not be mixed with source control or exposed through domain interfaces.

## Decision

The primary workstation uses an external managed Workspace. Its physical path is intentionally not
a portable runtime or domain contract and is not recorded here.

- Stable configuration: `anban.toml`
- Secrets: `secrets.env`
- Installed Skills: `skills/`
- Per-run working directory: `runs/<run-id>/workspace/`
- Durable outputs: `artifacts/`

The Workspace root has mode 0700 and `secrets.env` has mode 0600. Database settings are referenced by environment-variable name from `anban.toml`; credentials are never embedded in TOML.

The Workspace root is resolved by Bootstrap configuration: the current process `ANBAN_WORKSPACE_DIR`, then the repository's untracked `.env`, then the operating-system default. The repository `.env` is limited to bootstrap configuration for `ANBAN_WORKSPACE_DIR`. Workspace content remains outside Git, and `anban.toml` cannot select the root because it is read only after the Workspace has been found.

Physical Workspace paths are adapter configuration rather than domain identity. Absolute host paths cannot enter Core, public APIs, model-visible content, audit data, or the frontend. Logical resources use stable `anban://...` URIs across environments; adapters resolve them to local paths only at the execution boundary.

## Consequences

Local and CI environments may use different physical Workspace roots while preserving the same layout and logical identifiers. Doctor validates local permissions, references, configuration presence, and isolation without public-network calls. Real model, Skill, and Capability behavior is validated explicitly by the relevant Codex Phase or Version Gate.
