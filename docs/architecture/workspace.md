# Managed Workspace

The external Workspace contains `anban.toml`, mode-0600 `secrets.env`, `skills/`, `runs/`,
`artifacts/`, `cache/`, `logs/`, and `tmp/`. It is separate from the repository.

Workspace Skills are discovered recursively from `skills/**/SKILL.md`. A scoped path such as
`skills/@owner/name/SKILL.md` has slug `@owner/name`; an unscoped Skill uses
`@local/<frontmatter-name>`. Missing version frontmatter becomes `unverified`. Invalid UTF-8,
source over 64 KiB, context over 15,000 characters, invalid identity, symlink escape, protected
content, or slug conflict produces a safe per-file diagnostic while other valid Skills load.

Instructions are never silently filtered or truncated: URLs, shell examples, absolute paths, and
resource references remain intact. Activation returns slug, version, content SHA-256, complete
`SKILL.md`, and the logical Skill root. `scripts/`, `assets/`, `references/`, and templates remain
on disk until a Skill uses them through `process.execute`.

`.clawhub/lock.json`, Origin files, `_meta.json`, registry, publisher, and fingerprints are not
production inputs. Acceptance may inspect them only as external evidence that a CLI installation
happened. A newly installed `SKILL.md` is discovered only by a newly built Application.

Relative Process cwd values resolve from the Workspace root; absolute cwd is allowed. Declared
Artifact paths resolve from the effective cwd, are snapshotted under `artifacts/<run-id>/`, and are
persisted only through Runtime/Persistence.
