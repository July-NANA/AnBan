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
npx --yes clawhub@latest --workdir . --no-input install <slug>
npx --yes clawhub@latest --workdir . --no-input list
npx --yes clawhub@latest --workdir . --no-input update <slug>
```

Searching is not installing. Install only when the user explicitly asks to install, or asks to
search and install. Do not log in, publish, invent credentials, or retry without a finite bound.
An installation succeeds only when the real command succeeds and the installed Skill files exist.

After installation, tell the user that a new Anban Application or session must be started before
the new Skill can be discovered and used. Never claim that a newly installed Skill was loaded or
used in the same Agent loop.
