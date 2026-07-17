"""Core persistence Port boundary tests."""

from __future__ import annotations

import ast
from pathlib import Path


def test_core_persistence_port_has_no_adapter_imports() -> None:
    path = Path(__file__).resolve().parents[2] / "anban" / "core" / "persistence.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    assert not any(
        name.startswith(("sqlalchemy", "alembic", "anban.persistence")) for name in imported
    )
