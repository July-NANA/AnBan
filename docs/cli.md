# v0.1 CLI Reference

The production CLI commands are `workspace init`, `run`, `chat`, `runs`, `trace`, and `artifacts`.
Every command supports `--json`. Run failures use stable error codes; Trace and Artifact queries
work from a new database-only Application.

The Agent sees only `skill.activate` and `process.execute`. The Process input accepts `command`,
string `args`, optional `cwd`, `env` name/value entries, text `stdin`, `timeout`, and declared
`artifacts` with path and optional media type. Ordinary names resolve through inherited `PATH`;
absolute executable paths must be executable regular files; relative executable paths are rejected.
No implicit shell is used. Multiple Artifact declarations are validated together and duplicate
resolved paths are rejected before managed snapshots are created.

Default budgets are 12 model turns, 16 Capability calls, 600 seconds total, and repeated-call limit
3 (`0` disables it; `1` is invalid). Process defaults are 60 seconds with a configurable maximum
of 300, 64 KiB each for stdout/stderr/stdin, 128 arguments, 8 Artifacts, and 16 MiB per Artifact.
Hard maxima are 24 turns, 32 calls, 1800 seconds total, 600 seconds Process, 256 KiB streams, 256
arguments, 32 Artifacts, and 64 MiB per Artifact.

`python -m scripts.doctor` checks the active Python 3.12 toolchain, Node/pnpm, Workspace,
configuration, both configured PostgreSQL databases and migration heads, uniform Skill discovery,
and a harmless real Process through the production Registry. `--online` additionally checks npx
and the current ClawHub CLI. `--web` additionally launches the locked Chromium check.
