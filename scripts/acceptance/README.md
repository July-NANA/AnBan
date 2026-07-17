# Real acceptance helpers

These fail-closed scripts are invoked explicitly by a scoped Codex Phase Gate or Version Gate.
They are not part of `pnpm run doctor` or ordinary CI.

- `pnpm run acceptance:model` uses the production ModelPort Adapter for a real model request,
  native Tool Calling, Tool Result pairing, final response, and structured output.
- `pnpm run acceptance:skill` uses the production Workspace catalog and Registry to discover,
  hash-check, safely project, and activate the approved local Weather Skill, then performs its
  documented bounded live weather request.
- `pnpm run acceptance:capability` invokes the production Registry against the managed Workspace,
  performs real run-scoped file write/read/list operations, creates and verifies a logical Artifact
  snapshot, and executes one real allowlisted no-shell process. Its isolated files are removed.
- `pnpm run acceptance:agent` runs the fixed production LangGraph with the real ModelPort and
  Workspace Skill Capability, requiring a native Skill Tool Call, paired Tool Result, and final.
- `conda run -n anban python -m scripts.acceptance.check_migration_schema` verifies the migrated
  PostgreSQL test profile, six-table schema, status and relationship constraints, and ordered Event
  uniqueness. Its probe records are rolled back.
- `pnpm run acceptance:persistence` verifies real PostgreSQL create/read/locked-update paths, Run
  reconstruction, deterministic Event order, atomic rollback, and deterministic cleanup against
  `anban_test`.
- `pnpm run acceptance:runtime` runs the real Model and governed file Capability through the
  persistent Runtime, then rebuilds its Run and safe Audit/Trace in a new process.
- `pnpm run acceptance:p2` is the P2 Gate: one real persisted Run activates the approved Weather
  Skill and performs a governed file write before final output; a new process verifies PostgreSQL,
  Artifact, Event, Audit, and Trace facts. The same command probes production model transport and
  timeout classification plus unknown/invalid Capability, traversal, symlink, process timeout,
  missing executable, output limit, and environment isolation failures.

Run only the helper required by the current Gate. Credentials remain in the managed Workspace
`secrets.env`; the scripts emit allowlisted status messages and fail with a non-zero exit code.
