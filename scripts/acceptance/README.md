# Real acceptance helpers

These fail-closed scripts are invoked explicitly by a scoped Codex Phase Gate or Version Gate.
They are not part of `pnpm run doctor` or ordinary CI.

- `pnpm run acceptance:model` performs a real model request, native Tool Calling, an isolated
  real file operation, Tool Result return, and a final model response.
- `pnpm run acceptance:skill` verifies the approved local Weather Skill baseline, reads its real
  instructions, and performs the documented bounded live weather request.

Run only the helper required by the current Gate. Credentials remain in the managed Workspace
`secrets.env`; the scripts emit allowlisted status messages and fail with a non-zero exit code.
