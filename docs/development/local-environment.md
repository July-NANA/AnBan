# Local Environment

This file records non-sensitive facts for the primary local development workstation. Absolute paths below are workstation records only and do not participate in portable readiness decisions. Runtime authority comes from the active environment and Bootstrap configuration. Other developers and CI may use different physical paths while preserving the environment name and Python version.

## Python

- Distribution: Miniforge
- Miniforge base: `/Users/fanyuhang/miniforge3`
- Conda environment: `anban`
- Python: `3.12.13`
- Interpreter: `/Users/fanyuhang/miniforge3/envs/anban/bin/python`
- uv: `0.11.29`

Do not use the macOS system Python or create a second virtual environment.
Doctor validates the active Conda environment and does not read this document as runtime configuration.

## Workspace

- Root: `/Users/fanyuhang/AnbanWorkspace`
- Configuration: `anban.toml`
- Secret file: `secrets.env` (values are intentionally not recorded)

The root above is a primary workstation record. `ANBAN_WORKSPACE_DIR`, the repository Bootstrap `.env`, or the operating-system default resolves the active external Workspace.

## PostgreSQL

| Purpose | Host | Port | Database | User |
| --- | --- | ---: | --- | --- |
| Development | `127.0.0.1` | 5432 | `anban` | `anban` |
| Test | `127.0.0.1` | 5433 | `anban_test` | `anban` |

Passwords are intentionally omitted.

## Real Model

- Provider type: `openai-compatible`
- Model: `deepseek-v4-pro`

The Base URL, API key, Authorization data, and raw provider responses are intentionally omitted.

## Real ClawHub Skill

- Name: Weather
- Slug: `@steipete/weather`
- Version: `1.0.0`
- Source: ClawHub registry, publisher `@steipete`
- Local path: `/Users/fanyuhang/AnbanWorkspace/skills/@steipete/weather`
- CLI: `clawhub@0.23.1`
- Pin: enabled; updates require explicit review
- `SKILL.md` SHA-256: `1ca0c8d768ad603ea8d5d47f56a9b435fe575f7f34e719eda85c82003d740e93`
- Installed: `2026-07-17`
- Real validation: public city query for Sydney through the Skill-documented `wttr.in` service

Do not run automatic `update --all` during development readiness.
The recorded Skill path follows this workstation's Workspace and is not a portable requirement.
