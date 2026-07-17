"""Freeze the explicitly authorized v0.1 architecture surface.

Do not update these allowlists merely to make this test pass. Every allowlist change requires
explicit architecture authorization and an Architecture Delta in the delivery evidence.
"""

from __future__ import annotations

import ast
from pathlib import Path

from anban.capability import local_capability_registry

REPOSITORY = Path(__file__).parents[2]
ANBAN = REPOSITORY / "anban"

APPROVED_PRODUCT_MODULES = {
    "capability",
    "core",
    "interaction",
    "model",
    "persistence",
    "runtime",
}
APPROVED_INFRASTRUCTURE_MODULES = {"config"}
APPROVED_PROTOCOLS = {
    "CapabilityHandler",
    "CapabilityPort",
    "ExecutionRepository",
    "ModelPort",
    "UnitOfWork",
    "UnitOfWorkFactory",
}
APPROVED_ADAPTER_TYPES = {
    "OpenAICompatibleAdapter",
    "SQLAlchemyExecutionRepository",
    "SQLAlchemyUnitOfWork",
    "SQLAlchemyUnitOfWorkFactory",
}
APPROVED_CAPABILITY_HANDLERS = {"ProcessCapability", "SkillActivationCapability"}
APPROVED_INTERACTION_TYPES = {"InteractionChatSession", "InteractionService"}


def classes() -> tuple[tuple[Path, ast.ClassDef], ...]:
    found: list[tuple[Path, ast.ClassDef]] = []
    for source in sorted(ANBAN.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        found.extend((source, node) for node in ast.walk(tree) if isinstance(node, ast.ClassDef))
    return tuple(found)


def test_top_level_product_and_infrastructure_packages_are_fixed() -> None:
    packages = {
        path.name for path in ANBAN.iterdir() if path.is_dir() and (path / "__init__.py").is_file()
    }
    assert packages == APPROVED_PRODUCT_MODULES | APPROVED_INFRASTRUCTURE_MODULES


def test_protocol_surface_is_fixed() -> None:
    protocols = {
        node.name
        for _, node in classes()
        if any(isinstance(base, ast.Name) and base.id == "Protocol" for base in node.bases)
    }
    assert protocols == APPROVED_PROTOCOLS


def test_adapter_and_handler_types_are_fixed() -> None:
    definitions = classes()
    adapters = {node.name for _, node in definitions if node.name.endswith("Adapter")}
    handlers = {
        node.name
        for source, node in definitions
        if source.parent.name == "capability" and node.name.endswith("Capability")
    }
    interaction_types = {
        node.name
        for source, node in definitions
        if source.parent.name == "interaction" and node.name.endswith(("Service", "ChatSession"))
    }
    persistence_types = {
        node.name
        for source, node in definitions
        if source.parent.name == "persistence"
        and node.name.startswith("SQLAlchemy")
        and node.name.endswith(("Repository", "UnitOfWork", "UnitOfWorkFactory"))
    }
    assert adapters == APPROVED_ADAPTER_TYPES & adapters
    assert adapters == {"OpenAICompatibleAdapter"}
    assert persistence_types == APPROVED_ADAPTER_TYPES - adapters
    assert handlers == APPROVED_CAPABILITY_HANDLERS
    assert interaction_types == APPROVED_INTERACTION_TYPES


def test_production_capability_names_are_fixed(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    registry = local_capability_registry(workspace_root=tmp_path)
    assert {descriptor.name for descriptor in registry.search()} == {
        "process.execute",
        "skill.activate",
    }
