# Managed Workspace

The external Workspace contains `anban.toml`, mode-0600 `secrets.env`, `skills/`, `runs/`,
`artifacts/`, `cache/`, `logs/`, and `tmp/`. It is separate from the repository.

Workspace Skills are discovered recursively from `skills/**/SKILL.md`. A scoped path such as
`skills/@owner/name/SKILL.md` has slug `@owner/name`; its path supplies identity whether the file
uses YAML frontmatter or ordinary Markdown. An unscoped Skill uses a deterministic lowercase,
hyphenated `@local/<frontmatter-name>` derived from its frontmatter display name. Version and
installation-source metadata are not production identity.
Invalid UTF-8, source over 64 KiB, context over 15,000 characters, invalid identity, symlink escape,
protected content, reserved namespace use, or slug conflict produces a safe per-file diagnostic
while other valid Skills load. Workspace Skills cannot claim `@anban/*`; all candidates for any
other duplicate slug are excluded rather than resolved by scan order.

Instructions are never silently filtered or truncated: URLs, shell examples, absolute paths, and
resource references remain intact. Activation returns slug, content SHA-256, complete
`SKILL.md`, and the logical Skill root. `scripts/`, `assets/`, `references/`, and templates remain
on disk until a Skill uses them through `process.execute`.

`.clawhub/lock.json`, Origin files, `_meta.json`, registry, publisher, versions, and fingerprints are
not production or acceptance identity inputs. A newly installed `SKILL.md` is discovered only by a
newly built Application.

Relative Process cwd values resolve from the Workspace root; absolute cwd is allowed. Declared
Artifact paths resolve from the effective cwd. Multiple declared files are validated before any
snapshot, stored under `artifacts/<run-id>/`, and persisted only through Runtime/Persistence.
