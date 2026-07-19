"""Freeze the explicitly authorized v0.1 architecture surface.

Do not update these allowlists merely to make this test pass. Every allowlist change requires
explicit architecture authorization and an Architecture Delta in the delivery evidence.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import cast

from anban.capability import MemoryContextCapability, local_capability_registry
from anban.core.persistence import UnitOfWorkFactory

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
    "CapabilityInventoryPort",
    "CapabilityPort",
    "ExecutionRepository",
    "ModelPort",
    "UnitOfWork",
    "UnitOfWorkFactory",
}
APPROVED_ADAPTER_TYPES = {
    "McpStdioAdapter",
    "OpenAICompatibleAdapter",
    "SQLAlchemyExecutionRepository",
    "SQLAlchemyUnitOfWork",
    "SQLAlchemyUnitOfWorkFactory",
}
APPROVED_CAPABILITY_HANDLERS = {
    "AgentDelegateCapability",
    "McpToolCapability",
    "MemoryContextCapability",
    "ProcessCapability",
    "SkillActivationCapability",
}
APPROVED_INTERACTION_TYPES = {"InteractionChatSession", "InteractionService"}
FORBIDDEN_ACCEPTANCE_LITERALS = {
    "在 Workspace 的临时目录生成一份文本说明和一份 JSON 摘要",
    "report.txt",
    "summary.json",
    "@local/json-utility-tools",
}
UUID_LITERAL = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)


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
    assert adapters == {"McpStdioAdapter", "OpenAICompatibleAdapter"}
    assert persistence_types == APPROVED_ADAPTER_TYPES - adapters
    assert handlers == APPROVED_CAPABILITY_HANDLERS
    assert interaction_types == APPROVED_INTERACTION_TYPES


def test_production_capability_names_are_fixed(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    memory = MemoryContextCapability(cast(UnitOfWorkFactory, object()))
    registry = local_capability_registry(
        workspace_root=tmp_path,
        additional_handlers=(memory,),
    )
    assert {descriptor.name for descriptor in registry.search()} == {
        "memory.context",
        "process.execute",
        "skill.activate",
    }


def test_production_sources_do_not_embed_acceptance_specific_literals() -> None:
    for source in sorted(ANBAN.rglob("*.py")):
        content = source.read_text(encoding="utf-8")
        assert not any(literal in content for literal in FORBIDDEN_ACCEPTANCE_LITERALS)
        assert UUID_LITERAL.search(content) is None
