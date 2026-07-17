"""Compatibility projection for the unified Workspace configuration loader."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from anban.config import ModelConfiguration, load_configuration


def load_model_configuration(
    *,
    workspace: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> ModelConfiguration:
    """Load one model projection for standalone adapter and acceptance entry points."""

    return load_configuration(workspace=workspace, environ=environ).require_model()


__all__ = ["ModelConfiguration", "load_model_configuration"]
