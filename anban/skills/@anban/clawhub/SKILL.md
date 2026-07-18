---
name: clawhub
description: Search, install, list, and update public Skills with the real ClawHub CLI.
---

# ClawHub CLI

Use this Skill when the user asks to search for, find, install, list, or update third-party Skills.
ClawHub operations are ordinary program executions through `process.execute`.

Use the real CLI from the Anban Workspace root:

```text
npx --yes clawhub@latest --workdir . --no-input search <query>
npx --yes clawhub@latest --workdir . --no-input inspect <slug> --file SKILL.md
npx --yes clawhub@latest --workdir . --no-input install <slug>
npx --yes clawhub@latest --workdir . --no-input list
npx --yes clawhub@latest --workdir . --no-input update <slug>
```

Searching is not installing. Install only when the user explicitly asks to install, or asks to
search and install. Do not log in, publish, invent credentials, or retry without a finite bound.
Stop after the first search that returns enough candidates to compare. Use at most two
semantically distinct searches when one is insufficient, and never repeat an equivalent search
command or add exploratory commands after a suitable candidate is selected.
Search-result summaries are not enough to establish compatibility. Before installation, inspect
the selected candidate's real `SKILL.md`; when several candidates from the same result could fit,
inspect at most two. Check the instructions against every user constraint. For example, a request
for a local, dependency-free workflow excludes a Skill that calls a remote API, needs credentials,
installs packages, or depends on a separate service. Reject an incompatible candidate before
installation and select another candidate from the existing search result when available.
An installation succeeds only when the real command succeeds and the installed Skill files exist.
Do not call `skill.activate` for a search result or a guessed identity. After install succeeds, run
`list` once. A listed unscoped install such as `example-skill` is discovered by the uniform Anban
catalog as `@local/example-skill`; a scoped `@scope/name` keeps that scoped identity. Pass this
catalog identity, not the installation directory name, to `skill.activate`.

After a successful install, activate the exact newly discovered Skill through `skill.activate` in
the same Agent loop. Continue the original user Task with its real instructions; do not replace the
Task with installation. Never claim that the Skill was loaded or used before its real activation
Tool Result is observed. Use the minimum bounded real commands needed to verify that Task; do not
repeat checks for facts already present in Tool Results or the activated Skill instructions.
