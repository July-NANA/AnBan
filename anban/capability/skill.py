"""Uniform package and Workspace SKILL.md discovery and activation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from anban.capability.contracts import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityResult,
    CapabilityResultStatus,
    InvocationContext,
)
from anban.capability.workspace import capability_error
from anban.core.errors import ErrorCode
from anban.core.metadata import SafeMetadata
from scripts.workspace_bootstrap import resolve_workspace

MAX_SKILL_SOURCE_BYTES = 65_536
MAX_SKILL_CONTEXT_CHARS = 15_000
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SLUG_PATTERN = re.compile(r"^@[a-z0-9][a-z0-9-]{0,63}/[a-z0-9][a-z0-9-]{0,63}$")


class SkillPackage(BaseModel):
    """Identity and complete bounded instructions derived only from one SKILL.md."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str = Field(pattern=_SLUG_PATTERN.pattern)
    name: str = Field(pattern=_NAME_PATTERN.pattern)
    description: str = Field(min_length=1, max_length=1024)
    skill_root: str = Field(min_length=1, max_length=512)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    instructions: str = Field(min_length=1, max_length=MAX_SKILL_CONTEXT_CHARS)


@dataclass(frozen=True)
class SkillDiagnostic:
    """Non-sensitive reason for skipping one invalid SKILL.md."""

    path: str
    reason: str


class WorkspaceSkillCatalog:
    """Scan package and Workspace roots through one parser and validation path."""

    def __init__(
        self,
        workspace_root: Path | None = None,
        *,
        package_skills_root: Path | None = None,
        protected_values: tuple[str, ...] = (),
    ) -> None:
        workspace = resolve_workspace().path if workspace_root is None else workspace_root
        package_root = (
            Path(__file__).resolve().parent.parent / "skills"
            if package_skills_root is None
            else package_skills_root
        )
        self._roots = (
            self._root(package_root, "package", "package/skills"),
            self._root(workspace / "skills", "workspace", "skills"),
        )
        self._protected_values = tuple(value for value in protected_values if value)
        self._diagnostics: tuple[SkillDiagnostic, ...] = ()

    @property
    def diagnostics(self) -> tuple[SkillDiagnostic, ...]:
        return self._diagnostics

    def discover(self) -> tuple[SkillPackage, ...]:
        candidates: dict[str, list[tuple[SkillPackage, str]]] = {}
        diagnostics: list[SkillDiagnostic] = []
        for physical_root, label, logical_root in self._roots:
            if not physical_root.is_dir():
                continue
            for source_path in sorted(physical_root.rglob("SKILL.md")):
                relative = source_path.relative_to(physical_root)
                diagnostic_path = f"{label}:{relative.as_posix()}"
                try:
                    package = self._load(physical_root, logical_root, source_path, relative)
                    if label == "workspace" and package.slug.startswith("@anban/"):
                        raise SkillLoadError("reserved_skill_namespace")
                    candidates.setdefault(package.slug, []).append((package, diagnostic_path))
                except SkillLoadError as exc:
                    diagnostics.append(SkillDiagnostic(diagnostic_path, exc.reason))
        packages: dict[str, SkillPackage] = {}
        for slug in sorted(candidates):
            entries = candidates[slug]
            if len(entries) == 1:
                packages[slug] = entries[0][0]
                continue
            diagnostics.extend(
                SkillDiagnostic(path, "slug_conflict")
                for _, path in sorted(entries, key=lambda entry: entry[1])
            )
        self._diagnostics = tuple(sorted(diagnostics, key=lambda item: (item.path, item.reason)))
        return tuple(packages[key] for key in sorted(packages))

    @staticmethod
    def _root(path: Path, label: str, logical: str) -> tuple[Path, str, str]:
        try:
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"{label} Skill root is invalid") from exc
        return resolved, label, logical

    def _load(
        self,
        root: Path,
        logical_root: str,
        source_path: Path,
        relative: Path,
    ) -> SkillPackage:
        try:
            resolved = source_path.resolve(strict=True)
            if not resolved.is_file() or not resolved.is_relative_to(root):
                raise SkillLoadError("path_invalid")
            raw = resolved.read_bytes()
        except SkillLoadError:
            raise
        except (OSError, RuntimeError) as exc:
            raise SkillLoadError("source_unavailable") from exc
        if len(raw) > MAX_SKILL_SOURCE_BYTES:
            raise SkillLoadError("source_limit")
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillLoadError("source_not_utf8") from exc
        if len(source) > MAX_SKILL_CONTEXT_CHARS:
            raise SkillLoadError("context_limit")
        if any(value in source for value in self._protected_values):
            raise SkillLoadError("protected_value")
        fields = self._frontmatter(source)
        scoped_name = self._scoped_name(relative)
        if fields is None:
            if scoped_name is None:
                raise SkillLoadError("frontmatter_invalid")
            name = scoped_name
            description = self._plain_description(source)
        else:
            name = scoped_name or self._logical_name(fields["name"])
            description = fields["description"]
        if not _NAME_PATTERN.fullmatch(name):
            raise SkillLoadError("name_invalid")
        if not description or len(description) > 1024:
            raise SkillLoadError("description_invalid")
        slug = self._slug(relative, name)
        package_root = relative.parent
        return SkillPackage(
            slug=slug,
            name=name,
            description=description,
            skill_root=f"{logical_root}/{package_root.as_posix()}",
            content_hash=hashlib.sha256(raw).hexdigest(),
            instructions=source,
        )

    @staticmethod
    def _frontmatter(source: str) -> dict[str, str] | None:
        lines = source.splitlines()
        if not lines or lines[0].strip() != "---":
            return None
        try:
            end = next(index for index in range(1, min(len(lines), 65)) if lines[index] == "---")
        except StopIteration as exc:
            raise SkillLoadError("frontmatter_invalid") from exc
        fields: dict[str, str] = {}
        for line in lines[1:end]:
            key, separator, raw_value = line.partition(":")
            if not separator or key not in {"name", "description"}:
                continue
            value = raw_value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            fields[key] = value
        if not fields.get("name") or not fields.get("description"):
            raise SkillLoadError("frontmatter_invalid")
        return fields

    @staticmethod
    def _scoped_name(relative: Path) -> str | None:
        parts = relative.parts
        if len(parts) != 3 or not parts[0].startswith("@") or parts[2] != "SKILL.md":
            return None
        return parts[1]

    @staticmethod
    def _plain_description(source: str) -> str:
        for line in source.splitlines():
            candidate = line.strip()
            if candidate and not candidate.startswith(("#", "```")):
                return candidate
        raise SkillLoadError("description_invalid")

    @staticmethod
    def _logical_name(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        if not _NAME_PATTERN.fullmatch(normalized):
            raise SkillLoadError("name_invalid")
        return normalized

    @staticmethod
    def _slug(relative: Path, name: str) -> str:
        parts = relative.parts
        if len(parts) == 3 and parts[0].startswith("@") and parts[2] == "SKILL.md":
            slug = f"{parts[0]}/{parts[1]}"
            if not _SLUG_PATTERN.fullmatch(slug) or parts[1] != name:
                raise SkillLoadError("identity_mismatch")
            return slug
        return f"@local/{name}"


class SkillLoadError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SkillActivationCapability:
    """Return complete instructions for any uniformly discovered Skill."""

    def __init__(self, packages: tuple[SkillPackage, ...]) -> None:
        if not packages:
            raise ValueError("Skill activation requires at least one valid package")
        self._packages = {package.slug: package for package in packages}
        enum: list[JsonValue] = list(self._packages)
        self._descriptor = CapabilityDescriptor(
            name="skill.activate",
            description="Activate one discovered Skill and return its complete instructions.",
            kind=CapabilityKind.SKILL,
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string", "enum": enum, "maxLength": 128}},
                "required": ["name"],
                "additionalProperties": False,
            },
        )

    @property
    def descriptor(self) -> CapabilityDescriptor:
        return self._descriptor

    async def invoke(
        self, arguments: dict[str, JsonValue], context: InvocationContext
    ) -> CapabilityResult:
        slug = arguments.get("name")
        if not isinstance(slug, str) or slug not in self._packages:
            raise capability_error(
                ErrorCode.CAPABILITY_ARGUMENTS_INVALID,
                "Skill identity is invalid",
                reason="unknown_skill",
                capability_name=self.descriptor.name,
            )
        package = self._packages[slug]
        observation = (
            f"Activated Skill: {package.slug}\n"
            f"Skill root: {package.skill_root}\n"
            f"Content SHA-256: {package.content_hash}\n"
            "SKILL.md:\n"
            f"{package.instructions}"
        )
        return CapabilityResult(
            status=CapabilityResultStatus.COMPLETED,
            observation=observation,
            metadata=SafeMetadata(
                {
                    "skill_slug": package.slug,
                    "skill_root": package.skill_root,
                    "content_hash": package.content_hash,
                }
            ),
        )

    async def cancel(self, context: InvocationContext) -> None:
        return None
