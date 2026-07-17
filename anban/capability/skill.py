"""Approved Workspace Skill discovery and activation through CapabilityPort."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from anban.capability.contracts import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityResult,
    CapabilityResultStatus,
    InvocationContext,
)
from anban.capability.registry import CapabilityRegistry
from anban.capability.workspace import capability_error
from anban.core.errors import AnbanError, ErrorCode
from anban.core.metadata import SafeMetadata, validate_safe_text
from scripts.workspace_bootstrap import resolve_workspace

MAX_SKILL_SOURCE_BYTES = 65_536
MAX_SKILL_CONTEXT_CHARS = 15_000
_SLUG_PATTERN = re.compile(r"^@[a-z0-9][a-z0-9-]{0,63}/[a-z0-9][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class ApprovedSkill:
    slug: str
    version: str
    owner_handle: str
    sha256: str


WEATHER_SKILL = ApprovedSkill(
    slug="@steipete/weather",
    version="1.0.0",
    owner_handle="steipete",
    sha256="1ca0c8d768ad603ea8d5d47f56a9b435fe575f7f34e719eda85c82003d740e93",
)
APPROVED_SKILLS = (WEATHER_SKILL,)


class SkillPackage(BaseModel):
    """Safe package facts and the bounded model-visible instruction projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str = Field(pattern=_SLUG_PATTERN.pattern)
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str = Field(min_length=1, max_length=1024)
    version: str = Field(min_length=1, max_length=64, pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    owner_handle: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    source_uri: str = Field(min_length=1, max_length=256, pattern=r"^anban://skill/")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    instructions: str = Field(min_length=1, max_length=MAX_SKILL_CONTEXT_CHARS)
    omitted_line_count: int = Field(ge=0)

    @field_validator("description", "instructions", "source_uri")
    @classmethod
    def validate_model_visible_text(cls, value: str) -> str:
        return validate_safe_text(value, label="Skill context", max_length=MAX_SKILL_CONTEXT_CHARS)


class WorkspaceSkillCatalog:
    """Discover only explicitly approved, pinned packages from the managed Workspace."""

    def __init__(
        self,
        workspace_root: Path | None = None,
        *,
        approved: tuple[ApprovedSkill, ...] = APPROVED_SKILLS,
    ) -> None:
        root = resolve_workspace().path if workspace_root is None else workspace_root
        try:
            self._root = root.resolve(strict=True)
            self._skills_root = (self._root / "skills").resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise self._failure("Skill directory is unavailable", "skills_root_invalid") from exc
        if not self._skills_root.is_dir() or not self._skills_root.is_relative_to(self._root):
            raise self._failure("Skill directory is unavailable", "skills_root_invalid")
        self._approved = approved

    def discover(self) -> tuple[SkillPackage, ...]:
        records = self._lock_records()
        packages = tuple(self._load(approved, records) for approved in self._approved)
        if not packages:
            raise self._failure("No approved Workspace Skill is configured", "skill_missing")
        return packages

    def _load(self, approved: ApprovedSkill, records: Mapping[str, object]) -> SkillPackage:
        if not _SLUG_PATTERN.fullmatch(approved.slug):
            raise self._failure("Approved Skill identity is invalid", "approval_invalid")
        namespace, package_name = approved.slug.split("/", maxsplit=1)
        skill_file = self._skills_root / namespace / package_name / "SKILL.md"
        try:
            resolved = skill_file.resolve(strict=True)
            if not resolved.is_file() or not resolved.is_relative_to(self._skills_root):
                raise ValueError("Skill source escapes the Workspace")
            raw = resolved.read_bytes()
        except (OSError, RuntimeError, ValueError) as exc:
            raise self._failure("Approved Workspace Skill is unavailable", "skill_missing") from exc
        if len(raw) > MAX_SKILL_SOURCE_BYTES:
            raise self._failure("Workspace Skill source exceeds its limit", "source_limit")
        digest = hashlib.sha256(raw).hexdigest()
        record = records.get(approved.slug)
        if not isinstance(record, dict):
            raise self._failure("Workspace Skill source record is missing", "source_record_missing")
        source_record = cast(dict[str, object], record)
        if (
            source_record.get("version") != approved.version
            or source_record.get("ownerHandle") != approved.owner_handle
            or source_record.get("pinned") is not True
            or digest != approved.sha256
        ):
            raise self._failure("Workspace Skill approval does not match", "approval_mismatch")
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise self._failure("Workspace Skill source is not UTF-8", "source_invalid") from exc
        name, description, body = self._parse_frontmatter(source)
        if name != package_name:
            raise self._failure("Workspace Skill identity does not match", "identity_mismatch")
        instructions, omitted = self._safe_projection(body)
        return SkillPackage(
            slug=approved.slug,
            name=name,
            description=description,
            version=approved.version,
            owner_handle=approved.owner_handle,
            source_uri=f"anban://skill/{approved.slug}@{approved.version}",
            content_hash=digest,
            instructions=instructions,
            omitted_line_count=omitted,
        )

    def _lock_records(self) -> Mapping[str, object]:
        lock_file = self._root / ".clawhub" / "lock.json"
        try:
            resolved = lock_file.resolve(strict=True)
            if not resolved.is_file() or not resolved.is_relative_to(self._root):
                raise ValueError("Skill source record escapes the Workspace")
            if resolved.stat().st_size > MAX_SKILL_SOURCE_BYTES:
                raise ValueError("Skill source record is too large")
            payload: object = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise self._failure(
                "Workspace Skill source record is invalid", "source_record_invalid"
            ) from exc
        if not isinstance(payload, dict):
            raise self._failure("Workspace Skill source record is invalid", "source_record_invalid")
        payload_mapping = cast(dict[str, object], payload)
        records = payload_mapping.get("skills")
        if not isinstance(records, dict):
            raise self._failure("Workspace Skill source record is invalid", "source_record_invalid")
        return cast(dict[str, object], records)

    def _parse_frontmatter(self, source: str) -> tuple[str, str, str]:
        lines = source.splitlines()
        if not lines or lines[0] != "---":
            raise self._failure("Workspace Skill frontmatter is invalid", "frontmatter_invalid")
        try:
            end = lines.index("---", 1, min(len(lines), 34))
        except ValueError as exc:
            raise self._failure(
                "Workspace Skill frontmatter is invalid", "frontmatter_invalid"
            ) from exc
        fields: dict[str, str] = {}
        for line in lines[1:end]:
            key, separator, value = line.partition(":")
            if separator and key in {"name", "description"}:
                fields[key] = value.strip()
        name = fields.get("name", "")
        description = fields.get("description", "")
        if not name or not description:
            raise self._failure("Workspace Skill frontmatter is invalid", "frontmatter_invalid")
        try:
            validate_safe_text(description, label="Skill description", max_length=1024)
        except ValueError as exc:
            raise self._failure(
                "Workspace Skill description is unsafe", "unsafe_description"
            ) from exc
        return name, description, "\n".join(lines[end + 1 :]).strip()

    def _safe_projection(self, body: str) -> tuple[str, int]:
        retained: list[str] = []
        omitted = 0
        for line in body.splitlines():
            try:
                validate_safe_text(line, label="Skill instruction line", max_length=2048)
            except ValueError:
                omitted += 1
                continue
            retained.append(line)
        instructions = "\n".join(retained).strip()
        try:
            validate_safe_text(
                instructions,
                label="Skill instructions",
                max_length=MAX_SKILL_CONTEXT_CHARS,
            )
        except ValueError as exc:
            raise self._failure(
                "Workspace Skill instructions are unsafe", "instruction_limit"
            ) from exc
        if not instructions:
            raise self._failure("Workspace Skill instructions are empty", "instructions_empty")
        return instructions, omitted

    @staticmethod
    def _failure(message: str, reason: str) -> AnbanError:
        return capability_error(
            ErrorCode.CAPABILITY_EXECUTION_FAILED,
            message,
            reason=reason,
            capability_name="skill.activate",
        )


class SkillActivationCapability:
    """Activate at most one discovered Skill into the current Agent observation context."""

    def __init__(self, packages: tuple[SkillPackage, ...]) -> None:
        if not packages:
            raise ValueError("Skill activation requires a discovered package")
        self._packages = {package.slug: package for package in packages}
        self._active_slug: str | None = None
        enum: list[JsonValue] = list(self._packages)
        self._descriptor = CapabilityDescriptor(
            name="skill.activate",
            description="Activate one approved Workspace Skill for the current Agent execution.",
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
                "Workspace Skill identity is invalid",
                reason="unknown_skill",
                capability_name=self.descriptor.name,
            )
        if self._active_slug not in (None, slug):
            raise capability_error(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Another Workspace Skill is already active",
                reason="activation_limit",
                capability_name=self.descriptor.name,
            )
        self._active_slug = slug
        package = self._packages[slug]
        observation = (
            f"Activated Workspace Skill {package.slug}@{package.version}\n"
            f"Source: {package.source_uri}\n"
            f"Content SHA-256: {package.content_hash}\n"
            "Instructions:\n"
            f"{package.instructions}"
        )
        return CapabilityResult(
            status=CapabilityResultStatus.COMPLETED,
            observation=observation,
            metadata=SafeMetadata(
                {
                    "skill_slug": package.slug,
                    "skill_version": package.version,
                    "skill_source": package.source_uri,
                    "content_hash": package.content_hash,
                    "omitted_line_count": package.omitted_line_count,
                }
            ),
        )

    async def cancel(self, context: InvocationContext) -> None:
        return None


def register_workspace_skill(
    registry: CapabilityRegistry,
    *,
    workspace_root: Path | None = None,
    approved: tuple[ApprovedSkill, ...] = APPROVED_SKILLS,
) -> tuple[SkillPackage, ...]:
    """Discover approved packages and register their single activation boundary."""

    packages = WorkspaceSkillCatalog(workspace_root, approved=approved).discover()
    registry.register(SkillActivationCapability(packages))
    return packages
