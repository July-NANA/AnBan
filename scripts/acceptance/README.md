# Real acceptance helpers

These fail-closed scripts are invoked explicitly by a scoped Codex Phase Gate or Version Gate.
They are not part of `pnpm run doctor` or ordinary CI.

- `pnpm run acceptance:model` uses the production ModelPort Adapter for a real model request,
  native Tool Calling, Tool Result pairing, final response, and structured output.
- `pnpm run acceptance:skill` verifies the approved local Weather Skill baseline, reads its real
  instructions, and performs the documented bounded live weather request.
- `conda run -n anban python -m scripts.acceptance.check_migration_schema` verifies the migrated
  PostgreSQL test profile, six-table schema, status and relationship constraints, and ordered Event
  uniqueness. Its probe records are rolled back.
- `pnpm run acceptance:persistence` verifies real PostgreSQL create/read/locked-update paths, Run
  reconstruction, deterministic Event order, atomic rollback, and deterministic cleanup against
  `anban_test`.

Run only the helper required by the current Gate. Credentials remain in the managed Workspace
`secrets.env`; the scripts emit allowlisted status messages and fail with a non-zero exit code.
