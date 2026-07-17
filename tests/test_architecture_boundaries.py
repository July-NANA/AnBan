"""Enforce the accepted six-module dependency direction without importing adapters."""

from __future__ import annotations

import ast
from pathlib import Path

MODULES = {"interaction", "core", "runtime", "model", "capability", "persistence"}
ALLOWED_DEPENDENCIES: dict[str, set[str]] = {
    "interaction": {"core", "runtime"},
    "core": set(),
    "runtime": {"core", "model", "capability"},
    "model": {"core"},
    "capability": {"core"},
    "persistence": {"core"},
}


def module_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    names.update(
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    dependencies: set[str] = set()
    for name in names:
        parts = name.split(".")
        if len(parts) >= 2 and parts[0] == "anban" and parts[1] in MODULES:
            dependencies.add(parts[1])
    return dependencies


def dependency_graph() -> dict[str, set[str]]:
    package = Path(__file__).resolve().parents[1] / "anban"
    graph: dict[str, set[str]] = {module: set() for module in MODULES}
    for module in MODULES:
        for path in (package / module).rglob("*.py"):
            graph[module].update(module_imports(path) - {module})
    return graph


def test_six_module_dependencies_only_point_in_accepted_direction() -> None:
    graph = dependency_graph()
    assert graph.keys() == ALLOWED_DEPENDENCIES.keys()
    for module, dependencies in graph.items():
        assert dependencies <= ALLOWED_DEPENDENCIES[module]


def test_six_module_dependency_graph_has_no_cycle() -> None:
    graph = dependency_graph()
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module: str) -> None:
        if module in visiting:
            raise AssertionError(f"module dependency cycle includes {module}")
        if module in visited:
            return
        visiting.add(module)
        for dependency in graph[module]:
            visit(dependency)
        visiting.remove(module)
        visited.add(module)

    for module in MODULES:
        visit(module)
