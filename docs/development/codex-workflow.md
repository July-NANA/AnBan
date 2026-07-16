# Codex Workflow

Codex works serially on `anban` with one repository writer.

1. Verify remote, approved base SHA, branch synchronization, working tree, and Secret boundaries.
2. Read applicable architecture and ADRs.
3. Divide work into focused scopes and validate before each commit.
4. Never rewrite pushed history; use a new remediation commit.
5. Run local acceptance repeatedly until it passes.
6. Push to `origin/anban` and wait for applicable CI on the exact SHA.

Do not create Issues, Epics, Milestones, release tags, version Gates, or business scope unless the user explicitly requests them. Do not work directly on `main`.
