# Real acceptance helpers

These commands fail closed and emit only bounded evidence:

- `python -m scripts.acceptance.check_migration_schema`: current migration head and schema
  constraints against the configured test database.
- `pnpm run acceptance:persistence`: real PostgreSQL repository, transaction, aggregate, Artifact,
  and Event-order checks.
- `pnpm run acceptance:model`: real ModelPort content, native Tool Call/Result, final response, and
  structured output.
- `pnpm run acceptance:capability`: the production Registry surface plus real Process stdin, env,
  multi-Artifact, nonzero exit, and partial Artifact failure behavior.
- `pnpm run acceptance:runtime`: Gate A in an isolated Workspace, then Gate B-D in a fresh isolated
  Workspace. It uses the normal production Application and natural-language prompts, performs real
  Process/file/HTTP/Artifact work, invokes the ordinary packaged ClawHub Skill, installs exactly one
  compatible public Skill, starts new Applications, and requires three complete Skill/Process
  Traces. Skill identity evidence is limited to slug, relative `SKILL.md` path, and content hash;
  production and acceptance do not derive identity from installation records.
- `pnpm run acceptance:security`: deterministic fail-closed and Secret-boundary tests.
- `pnpm run acceptance:v0.1`: local quality, Doctor base/online/web, database, model, Capability,
  Runtime, security, and release-closure checks.

Gate E failure paths remain production-path tests: missing program, nonzero exit, timeout,
cancellation, output/Artifact failure, damaged Skill, model repair/exhaustion, database failure,
and Event failure. Fixtures provide invalid inputs only; they do not replace successful production
execution.
