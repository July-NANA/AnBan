# Contributing

## Branches

- `main` is the stable release branch.
- `anban` is the single development, integration, remediation, and acceptance branch.
- Work serially on `anban`; do not create feature branches or pull requests unless repository policy changes.

## Environment

Use Miniforge, the `anban` Conda environment, Python 3.12, and pnpm. Do not create a repository-local Python virtual environment. Keep the managed Workspace outside the repository and never stage `.env` or Workspace files.

## Changes

- Read the architecture and ADRs before changing behavior.
- Keep commits focused and validate their exact scope.
- Keep authored source files below 800 lines.
- Do not add infrastructure or production dependencies without documenting why.
- Never replace a real integration check with a fake success path.

## Required Checks

```bash
pnpm check
pnpm build
pnpm run doctor
```

`pnpm check` and `pnpm build` are deterministic repository checks. `pnpm run doctor` diagnoses
local prerequisites and does not perform public-network acceptance. A scoped Codex Phase or
Version Gate explicitly runs applicable real model, Skill, Capability, and end-to-end checks.
Ordinary GitHub Actions must pass on the exact pushed `anban` SHA.
