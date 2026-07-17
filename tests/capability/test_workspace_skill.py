"""Workspace Skill discovery, parsing, activation, and containment tests."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from anban.capability import (
    ApprovedSkill,
    CapabilityKind,
    CapabilityRegistry,
    CapabilityResultStatus,
    InvocationContext,
    WorkspaceSkillCatalog,
    register_workspace_skill,
)
from anban.core.errors import AnbanError, ErrorCode
from anban.core.ids import (
    new_capability_invocation_id,
    new_execution_run_id,
    new_node_run_id,
)

SOURCE = """---
name: weather
description: Get current weather safely.
homepage: https://example.invalid
metadata: {"unknown":{"permissions":["everything"]}}
---

# Weather

Use the approved public weather endpoint.
Do not load references unless the task requires one.
curl -o /tmp/weather-output example.invalid
"""


def build_workspace(root: Path, *, source: str = SOURCE) -> ApprovedSkill:
    skill_directory = root / "skills" / "@owner" / "weather"
    skill_directory.mkdir(parents=True)
    raw = source.encode()
    (skill_directory / "SKILL.md").write_bytes(raw)
    lock_directory = root / ".clawhub"
    lock_directory.mkdir()
    (lock_directory / "lock.json").write_text(
        json.dumps(
            {
                "skills": {
                    "@owner/weather": {
                        "version": "1.0.0",
                        "ownerHandle": "owner",
                        "pinned": True,
                        "unknownPermission": "ignored",
                    }
                }
            }
        )
    )
    return ApprovedSkill(
        slug="@owner/weather",
        version="1.0.0",
        owner_handle="owner",
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def context() -> InvocationContext:
    return InvocationContext(
        run_id=new_execution_run_id(),
        node_run_id=new_node_run_id(),
        invocation_id=new_capability_invocation_id(),
        deadline_at=datetime.now(UTC) + timedelta(seconds=10),
    )


def test_discovery_preserves_safe_source_hash_without_loading_resources(tmp_path: Path) -> None:
    approved = build_workspace(tmp_path)
    reference = tmp_path / "skills" / "@owner" / "weather" / "references"
    reference.mkdir()
    (reference / "private.txt").write_text("reference-canary-must-not-load")

    package = WorkspaceSkillCatalog(tmp_path, approved=(approved,)).discover()[0]

    assert package.source_uri == "anban://skill/@owner/weather@1.0.0"
    assert package.content_hash == approved.sha256
    assert package.omitted_line_count == 1
    assert "/tmp" not in package.instructions
    assert "reference-canary" not in package.instructions
    assert "unknownPermission" not in package.model_dump_json()
    assert str(tmp_path) not in package.model_dump_json()


async def test_skill_uses_registry_search_describe_and_activation_boundary(tmp_path: Path) -> None:
    approved = build_workspace(tmp_path)
    registry = CapabilityRegistry()
    packages = register_workspace_skill(registry, workspace_root=tmp_path, approved=(approved,))

    descriptor = registry.describe("skill.activate")
    assert descriptor.kind is CapabilityKind.SKILL
    assert registry.search("Workspace Skill") == (descriptor,)
    result = await registry.invoke(
        "skill.activate",
        {"name": "@owner/weather"},
        context(),
    )

    assert packages[0].slug == "@owner/weather"
    assert result.status is CapabilityResultStatus.COMPLETED
    assert "Use the approved public weather endpoint." in (result.observation or "")
    assert result.metadata.root["content_hash"] == approved.sha256
    assert str(tmp_path) not in str(result.model_dump(mode="json"))


async def test_unknown_skill_fails_before_activation(tmp_path: Path) -> None:
    approved = build_workspace(tmp_path)
    registry = CapabilityRegistry()
    register_workspace_skill(registry, workspace_root=tmp_path, approved=(approved,))
    with pytest.raises(AnbanError) as failure:
        await registry.invoke("skill.activate", {"name": "@owner/unknown"}, context())
    assert failure.value.info.code is ErrorCode.CAPABILITY_ARGUMENTS_INVALID


def test_missing_or_changed_skill_fails_explicitly(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    (tmp_path / ".clawhub").mkdir()
    (tmp_path / ".clawhub" / "lock.json").write_text('{"skills":{}}')
    approved = ApprovedSkill(
        slug="@owner/weather",
        version="1.0.0",
        owner_handle="owner",
        sha256="0" * 64,
    )
    with pytest.raises(AnbanError) as missing:
        WorkspaceSkillCatalog(tmp_path, approved=(approved,)).discover()
    assert missing.value.info.details.root["reason"] == "skill_missing"

    changed = build_workspace(tmp_path / "changed")
    changed_file = tmp_path / "changed" / "skills" / "@owner" / "weather" / "SKILL.md"
    changed_file.write_text(SOURCE + "changed")
    with pytest.raises(AnbanError) as mismatch:
        WorkspaceSkillCatalog(tmp_path / "changed", approved=(changed,)).discover()
    assert mismatch.value.info.details.root["reason"] == "approval_mismatch"


def test_invalid_frontmatter_and_unbounded_source_fail(tmp_path: Path) -> None:
    invalid_root = tmp_path / "invalid"
    invalid = build_workspace(invalid_root, source="no frontmatter")
    with pytest.raises(AnbanError) as frontmatter:
        WorkspaceSkillCatalog(invalid_root, approved=(invalid,)).discover()
    assert frontmatter.value.info.details.root["reason"] == "frontmatter_invalid"

    large_root = tmp_path / "large"
    large = build_workspace(large_root, source=SOURCE + "x" * 65_536)
    with pytest.raises(AnbanError) as source_limit:
        WorkspaceSkillCatalog(large_root, approved=(large,)).discover()
    assert source_limit.value.info.details.root["reason"] == "source_limit"


def test_external_skill_symlink_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    external = tmp_path / "external"
    external.mkdir()
    raw = SOURCE.encode()
    (external / "SKILL.md").write_bytes(raw)
    namespace = root / "skills" / "@owner"
    namespace.mkdir(parents=True)
    (namespace / "weather").symlink_to(external, target_is_directory=True)
    lock = root / ".clawhub"
    lock.mkdir()
    (lock / "lock.json").write_text(
        json.dumps(
            {
                "skills": {
                    "@owner/weather": {
                        "version": "1.0.0",
                        "ownerHandle": "owner",
                        "pinned": True,
                    }
                }
            }
        )
    )
    approved = ApprovedSkill("@owner/weather", "1.0.0", "owner", hashlib.sha256(raw).hexdigest())
    with pytest.raises(AnbanError) as escaped:
        WorkspaceSkillCatalog(root, approved=(approved,)).discover()
    assert escaped.value.info.details.root["reason"] == "skill_missing"
