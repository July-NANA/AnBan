"""Managed Workspace initialization is idempotent and secret-preserving."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from anban.config import load_configuration, policy
from anban.workspace import default_configuration_text, initialize_workspace
from scripts.workspace_bootstrap import REPOSITORY, WorkspaceResolutionError


def test_workspace_init_creates_layout_and_never_overwrites_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "managed-workspace"
    monkeypatch.setenv("ANBAN_WORKSPACE_DIR", str(workspace))

    first = initialize_workspace()
    secret_value = "local-canary-value"
    (workspace / "secrets.env").write_text(secret_value, encoding="utf-8")
    second = initialize_workspace()

    assert first.created_root and first.created_config and first.created_secrets
    assert not second.created_root and not second.created_config and not second.created_secrets
    assert (workspace / "secrets.env").read_text(encoding="utf-8") == secret_value
    assert (workspace / "secrets.env").stat().st_mode & 0o777 == 0o600
    assert workspace.stat().st_mode & 0o777 == 0o700
    assert all(
        (workspace / name).is_dir()
        for name in ("skills", "runs", "artifacts", "cache", "logs", "tmp")
    )


def test_workspace_init_does_not_replace_invalid_existing_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "managed-workspace"
    workspace.mkdir()
    configuration = workspace / "anban.toml"
    configuration.write_text("invalid = [", encoding="utf-8")
    monkeypatch.setenv("ANBAN_WORKSPACE_DIR", str(workspace))

    with pytest.raises(WorkspaceResolutionError) as raised:
        initialize_workspace()
    assert raised.value.code == "workspace_configuration_invalid"
    assert configuration.read_text(encoding="utf-8") == "invalid = ["


def test_workspace_init_rejects_repository_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_mode = os.stat(REPOSITORY).st_mode
    monkeypatch.setenv("ANBAN_WORKSPACE_DIR", str(REPOSITORY))

    with pytest.raises(WorkspaceResolutionError) as raised:
        initialize_workspace()
    assert raised.value.code == "workspace_path_unsafe"
    assert os.stat(REPOSITORY).st_mode == original_mode


def test_workspace_template_uses_policy_defaults_and_chinese_comments(tmp_path: Path) -> None:
    workspace = tmp_path / "managed-workspace"
    text = default_configuration_text()
    assert "单次模型请求超时时间" in text
    assert "process.execute 默认超时时间" in text
    workspace.mkdir()
    (workspace / "anban.toml").write_text(text, encoding="utf-8")
    (workspace / "secrets.env").write_text("", encoding="utf-8")
    configuration = load_configuration(workspace=workspace, environ={})
    assert configuration.agent.max_model_turns == policy.AGENT_MAX_MODEL_TURNS_DEFAULT
    assert configuration.agent.max_replans == policy.AGENT_MAX_REPLANS_DEFAULT
    assert (
        configuration.process.default_timeout_seconds
        == policy.PROCESS_DEFAULT_TIMEOUT_DEFAULT_SECONDS
    )
