"""Uniform SKILL.md discovery, diagnostics, activation, and refresh tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from anban.capability import (
    CapabilityKind,
    CapabilityRegistry,
    CapabilityResultStatus,
    InvocationContext,
    SkillActivationCapability,
    WorkspaceSkillCatalog,
    local_capability_registry,
)
from anban.core.errors import AnbanError, ErrorCode
from anban.core.ids import (
    new_capability_invocation_id,
    new_execution_run_id,
    new_node_run_id,
)

SOURCE = """---
name: runner
description: Run a documented command without changing its text.
version: 2.1.0
---

# Runner

Open https://example.invalid/reference and preserve this exact example:
`bash -c 'printf input | tool > /tmp/output.txt'`
Use scripts/run.py, assets/input.json, and references/guide.md when needed.
"""


def write_skill(root: Path, relative: str, source: str = SOURCE) -> Path:
    directory = root / relative
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "SKILL.md"
    target.write_text(source, encoding="utf-8")
    return target


def context() -> InvocationContext:
    return InvocationContext(
        run_id=new_execution_run_id(),
        node_run_id=new_node_run_id(),
        invocation_id=new_capability_invocation_id(),
        deadline_at=datetime.now(UTC) + timedelta(seconds=10),
    )


def catalog(
    workspace: Path, package_root: Path, *, protected_values: tuple[str, ...] = ()
) -> WorkspaceSkillCatalog:
    return WorkspaceSkillCatalog(
        workspace,
        package_skills_root=package_root,
        protected_values=protected_values,
    )


def test_package_and_workspace_skills_use_the_same_parser(tmp_path: Path) -> None:
    package_root = tmp_path / "package-skills"
    workspace = tmp_path / "workspace"
    workspace_skills = workspace / "skills"
    write_skill(package_root, "@owner/runner")
    package = catalog(workspace, package_root).discover()[0]

    package_root.rename(tmp_path / "unused-package-skills")
    empty_package = tmp_path / "empty-package"
    empty_package.mkdir()
    write_skill(workspace_skills, "@owner/runner")
    installed = catalog(workspace, empty_package).discover()[0]

    assert package.model_copy(update={"skill_root": installed.skill_root}) == installed
    assert package.skill_root == "package/skills/@owner/runner"
    assert installed.skill_root == "skills/@owner/runner"


def test_multiple_scoped_and_local_skills_are_discovered_without_external_metadata(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    workspace = tmp_path / "workspace"
    package_root.mkdir()
    local_source = SOURCE.replace("name: runner", "name: local-tool").replace(
        "version: 2.1.0\n", ""
    )
    write_skill(workspace / "skills", "plain-directory", local_source)
    write_skill(
        workspace / "skills",
        "@owner/other",
        SOURCE.replace("name: runner", "name: other"),
    )

    packages = catalog(workspace, package_root).discover()

    assert [item.slug for item in packages] == ["@local/local-tool", "@owner/other"]
    assert "version" not in packages[0].model_dump()
    assert packages[0].skill_root == "skills/plain-directory"


def test_instruction_content_is_complete_and_resources_are_not_eagerly_loaded(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    workspace = tmp_path / "workspace"
    write_skill(workspace / "skills", "@owner/runner")
    reference = workspace / "skills" / "@owner" / "runner" / "references"
    reference.mkdir()
    (reference / "private.txt").write_text("resource-canary", encoding="utf-8")

    package = catalog(workspace, package_root).discover()[0]

    assert package.instructions == SOURCE
    assert "https://example.invalid/reference" in package.instructions
    assert "bash -c 'printf input | tool > /tmp/output.txt'" in package.instructions
    assert "resource-canary" not in package.instructions
    assert package.content_hash == hashlib.sha256(SOURCE.encode()).hexdigest()


def test_invalid_skills_report_safe_diagnostics_without_blocking_valid_skills(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    workspace_skills = tmp_path / "workspace" / "skills"
    write_skill(workspace_skills, "@owner/runner")
    write_skill(workspace_skills, "broken", "no frontmatter")
    invalid_utf8 = workspace_skills / "invalid-utf8"
    invalid_utf8.mkdir()
    (invalid_utf8 / "SKILL.md").write_bytes(b"\xff\xfe")
    write_skill(workspace_skills, "large", SOURCE + "x" * 15_000)

    discovered = catalog(tmp_path / "workspace", package_root)
    packages = discovered.discover()

    assert [item.slug for item in packages] == ["@owner/runner"]
    assert {(item.path, item.reason) for item in discovered.diagnostics} == {
        ("workspace:broken/SKILL.md", "frontmatter_invalid"),
        ("workspace:invalid-utf8/SKILL.md", "source_not_utf8"),
        ("workspace:large/SKILL.md", "context_limit"),
    }
    assert str(tmp_path) not in str(discovered.diagnostics)


def test_slug_conflict_excludes_every_candidate_without_scan_order_winner(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    workspace = tmp_path / "workspace"
    write_skill(package_root, "@owner/runner")
    changed = SOURCE.replace(
        "description: Run", "description: Workspace must not replace package. Run"
    )
    write_skill(workspace / "skills", "@owner/runner", changed)

    discovered = catalog(workspace, package_root)
    packages = discovered.discover()

    assert packages == ()
    assert [(item.path, item.reason) for item in discovered.diagnostics] == [
        ("package:@owner/runner/SKILL.md", "slug_conflict"),
        ("workspace:@owner/runner/SKILL.md", "slug_conflict"),
    ]


def test_workspace_cannot_claim_reserved_anban_namespace(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    workspace = tmp_path / "workspace"
    write_skill(
        package_root,
        "@anban/clawhub",
        SOURCE.replace("name: runner", "name: clawhub"),
    )
    write_skill(
        workspace / "skills",
        "@anban/clawhub",
        SOURCE.replace("name: runner", "name: clawhub"),
    )
    write_skill(
        workspace / "skills",
        "@anban/other",
        SOURCE.replace("name: runner", "name: other"),
    )

    discovered = catalog(workspace, package_root)
    packages = discovered.discover()

    assert [item.slug for item in packages] == ["@anban/clawhub"]
    assert [(item.path, item.reason) for item in discovered.diagnostics] == [
        ("workspace:@anban/clawhub/SKILL.md", "reserved_skill_namespace"),
        ("workspace:@anban/other/SKILL.md", "reserved_skill_namespace"),
    ]


def test_three_conflicts_are_excluded_while_unrelated_skill_loads_deterministically(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    workspace = tmp_path / "workspace"
    for relative in ("third", "first", "second"):
        write_skill(workspace / "skills", relative)
    write_skill(
        workspace / "skills",
        "@owner/other",
        SOURCE.replace("name: runner", "name: other"),
    )

    discovered = catalog(workspace, package_root)
    packages = discovered.discover()

    assert [item.slug for item in packages] == ["@owner/other"]
    assert [(item.path, item.reason) for item in discovered.diagnostics] == [
        ("workspace:first/SKILL.md", "slug_conflict"),
        ("workspace:second/SKILL.md", "slug_conflict"),
        ("workspace:third/SKILL.md", "slug_conflict"),
    ]


def test_conflict_result_does_not_depend_on_file_creation_order(tmp_path: Path) -> None:
    results: list[tuple[tuple[str, ...], tuple[tuple[str, str], ...]]] = []
    for label, order in (("forward", ("alpha", "beta")), ("reverse", ("beta", "alpha"))):
        package_root = tmp_path / label / "package"
        package_root.mkdir(parents=True)
        workspace = tmp_path / label / "workspace"
        for relative in order:
            write_skill(workspace / "skills", relative)
        discovered = catalog(workspace, package_root)
        packages = discovered.discover()
        results.append(
            (
                tuple(package.slug for package in packages),
                tuple((item.path, item.reason) for item in discovered.diagnostics),
            )
        )

    assert results[0] == results[1]


@pytest.mark.parametrize(
    "metadata_content",
    [
        None,
        "{}",
        "not json",
        '{"registry":"different","publisher":"someone","fingerprint":"changed"}',
    ],
)
def test_install_metadata_cannot_change_discovery_or_identity(
    tmp_path: Path, metadata_content: str | None
) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    workspace = tmp_path / "workspace"
    write_skill(workspace / "skills", "@owner/runner")
    if metadata_content is not None:
        for relative in (".clawhub/lock.json", ".clawhub/origin.json", "skills/_meta.json"):
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(metadata_content, encoding="utf-8")

    package = catalog(workspace, package_root).discover()[0]

    assert package.slug == "@owner/runner"
    assert "version" not in package.model_dump()
    assert package.content_hash == hashlib.sha256(SOURCE.encode()).hexdigest()


async def test_activation_is_stateless_idempotent_and_allows_multiple_skills(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    workspace = tmp_path / "workspace"
    package_root.mkdir()
    write_skill(workspace / "skills", "@owner/runner")
    write_skill(
        workspace / "skills",
        "@owner/other",
        SOURCE.replace("name: runner", "name: other"),
    )
    packages = catalog(workspace, package_root).discover()
    registry = CapabilityRegistry((SkillActivationCapability(packages),))

    first = await registry.invoke("skill.activate", {"name": "@owner/runner"}, context())
    repeated = await registry.invoke("skill.activate", {"name": "@owner/runner"}, context())
    other = await registry.invoke("skill.activate", {"name": "@owner/other"}, context())

    assert first.status is CapabilityResultStatus.COMPLETED
    assert first.observation == repeated.observation
    assert other.status is CapabilityResultStatus.COMPLETED
    assert first.metadata.root["skill_root"] == "skills/@owner/runner"
    assert "skill_version" not in first.metadata.root
    assert "Version:" not in (first.observation or "")
    assert "SKILL.md:\n---" in (first.observation or "")


async def test_unknown_skill_fails_explicitly(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    workspace = tmp_path / "workspace"
    write_skill(package_root, "@owner/runner")
    registry = CapabilityRegistry(
        (SkillActivationCapability(catalog(workspace, package_root).discover()),)
    )

    with pytest.raises(AnbanError) as failure:
        await registry.invoke("skill.activate", {"name": "@owner/unknown"}, context())
    assert failure.value.info.code is ErrorCode.CAPABILITY_ARGUMENTS_INVALID


async def test_packaged_clawhub_instructions_are_an_ordinary_discovered_skill(
    tmp_path: Path,
) -> None:
    (tmp_path / "skills").mkdir()
    registry = local_capability_registry(workspace_root=tmp_path)

    result = await registry.invoke(
        "skill.activate",
        {"name": "@anban/clawhub"},
        context(),
    )

    assert result.status is CapabilityResultStatus.COMPLETED
    assert result.metadata.root["skill_root"] == "package/skills/@anban/clawhub"
    assert "npx --yes clawhub@latest --workdir . --no-input search" in (result.observation or "")
    assert "new Anban Application or session" in (result.observation or "")


def test_new_registry_discovers_newly_installed_skill_only_after_rebuild(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    first = local_capability_registry(workspace_root=workspace)
    assert "@local/later" not in first.describe("skill.activate").input_schema.__repr__()

    later = SOURCE.replace("name: runner", "name: later")
    write_skill(workspace / "skills", "later", later)
    second = local_capability_registry(workspace_root=workspace)

    assert "@local/later" not in first.describe("skill.activate").input_schema.__repr__()
    assert "@local/later" in second.describe("skill.activate").input_schema.__repr__()


def test_protected_value_and_external_symlink_are_skipped(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    workspace = tmp_path / "workspace"
    secret = "skill-secret-canary"
    write_skill(workspace / "skills", "secret", SOURCE + secret)
    external = tmp_path / "external"
    external_skill = write_skill(external, ".")
    link = workspace / "skills" / "linked"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.mkdir()
    (link / "SKILL.md").symlink_to(external_skill)

    discovered = catalog(workspace, package_root, protected_values=(secret,))
    assert discovered.discover() == ()
    assert {item.reason for item in discovered.diagnostics} == {
        "path_invalid",
        "protected_value",
    }


def test_production_catalog_has_no_install_source_metadata_branches() -> None:
    source = (Path(__file__).parents[2] / "anban" / "capability" / "skill.py").read_text()
    for forbidden in (
        "lock.json",
        "origin.json",
        "_meta.json",
        "registry",
        "publisher",
        "fingerprint",
        "skill_version",
    ):
        assert forbidden not in source


def test_production_registry_contains_only_approved_capabilities(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    names = tuple(item.name for item in local_capability_registry(workspace_root=tmp_path).search())
    assert names == ("process.execute", "skill.activate")
    assert (
        local_capability_registry(workspace_root=tmp_path).describe("skill.activate").kind
        is CapabilityKind.SKILL
    )
