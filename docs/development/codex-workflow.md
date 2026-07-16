# Codex Workflow

Codex works serially on `anban` with one repository writer.

1. Verify remote, approved base SHA, branch synchronization, working tree, and Secret boundaries.
2. Read applicable architecture and ADRs.
3. Divide work into focused scopes and validate before each commit.
4. Never rewrite pushed history; use a new remediation commit.
5. Run `pnpm check`, `pnpm build`, and `pnpm run doctor` repeatedly until they pass.
6. Run real model, Skill, Capability, and end-to-end acceptance explicitly when required by the
   current Phase Gate or Version Gate.
7. Push to `origin/anban` and wait for ordinary CI on the exact SHA.

Ordinary CI is deterministic: Ruff, Pyright, pytest, frontend checks and tests, frontend build,
and Secret scanning. Doctor diagnoses the current machine's local toolchain, Workspace,
configuration presence, PostgreSQL, approved local Skill files, and Chromium. It does not perform
real model or public-network acceptance. There is no permanent per-push real-readiness workflow.

Do not create Issues, Epics, Milestones, release tags, version Gates, or business scope unless the user explicitly requests them. Do not work directly on `main`.
