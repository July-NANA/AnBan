"""Pure baseline checks that do not instantiate providers or business behavior."""

from importlib import import_module


def test_six_backend_module_namespaces_are_importable() -> None:
    modules = ("interaction", "core", "runtime", "model", "capability", "persistence")

    for module in modules:
        imported = import_module(f"anban.{module}")
        assert imported.__doc__
