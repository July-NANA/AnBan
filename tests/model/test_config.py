"""Fail-closed model configuration tests with synthetic values only."""

from __future__ import annotations

from pathlib import Path

import pytest

from anban.config import load_configuration, policy
from anban.core import AnbanError, ErrorCode

CONFIG = """
schema_version = 1
workspace_id = "test-workspace"

[model.default]
provider = "openai-compatible"
base_url_env = "OPENAI_COMPATIBLE_BASE_URL"
api_key_env = "OPENAI_COMPATIBLE_API_KEY"
model_env = "OPENAI_COMPATIBLE_MODEL"

[database]
url_env = "DATABASE_URL"
test_url_env = "ANBAN_TEST_DATABASE_URL"
"""


def test_configuration_uses_allowlisted_environment_references(tmp_path: Path) -> None:
    (tmp_path / "anban.toml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "secrets.env").write_text("", encoding="utf-8")
    configuration = load_configuration(
        workspace=tmp_path,
        environ={
            "OPENAI_COMPATIBLE_BASE_URL": "https://provider.invalid/v1",
            "OPENAI_COMPATIBLE_API_KEY": "synthetic-test-value",
            "OPENAI_COMPATIBLE_MODEL": "test-model",
        },
    )
    model = configuration.require_model()
    assert model.model == "test-model"
    assert model.request_timeout_seconds == policy.MODEL_REQUEST_TIMEOUT_DEFAULT_SECONDS
    assert configuration.agent.max_model_turns == policy.AGENT_MAX_MODEL_TURNS_DEFAULT
    assert configuration.agent.max_replans == policy.AGENT_MAX_REPLANS_DEFAULT
    assert (
        configuration.process.default_timeout_seconds
        == policy.PROCESS_DEFAULT_TIMEOUT_DEFAULT_SECONDS
    )
    assert configuration.mcp.servers == ()
    assert configuration.webhook.endpoints == ()


def test_webhook_configuration_resolves_secret_reference_without_exposure(
    tmp_path: Path,
) -> None:
    (tmp_path / "anban.toml").write_text(
        CONFIG
        + """
[interaction.webhook]
body_max_bytes = 8192
clock_skew_seconds = 120

[[interaction.webhook.endpoints]]
name = "events"
secret_env = "TEST_WEBHOOK_SECRET"
""",
        encoding="utf-8",
    )
    secret = "synthetic-webhook-secret-material-123456"
    (tmp_path / "secrets.env").write_text(f"TEST_WEBHOOK_SECRET={secret}\n", encoding="utf-8")

    configuration = load_configuration(workspace=tmp_path, environ={})

    assert configuration.webhook.body_max_bytes == 8192
    assert configuration.webhook.clock_skew_seconds == 120
    assert configuration.webhook.endpoints[0].name == "events"
    assert secret not in repr(configuration)
    assert secret in configuration.protected_values()


def test_missing_webhook_secret_reference_fails_explicitly(tmp_path: Path) -> None:
    (tmp_path / "anban.toml").write_text(
        CONFIG
        + """
[[interaction.webhook.endpoints]]
name = "missing"
secret_env = "MISSING_WEBHOOK_SECRET"
""",
        encoding="utf-8",
    )
    (tmp_path / "secrets.env").write_text("", encoding="utf-8")

    with pytest.raises(AnbanError) as raised:
        load_configuration(workspace=tmp_path, environ={})

    assert raised.value.info.code is ErrorCode.CONFIGURATION_MISSING
    assert raised.value.info.details.root["webhook_endpoint"] == "missing"


def test_missing_webhook_endpoint_fails_only_when_ingress_is_required(tmp_path: Path) -> None:
    (tmp_path / "anban.toml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "secrets.env").write_text("", encoding="utf-8")
    configuration = load_configuration(workspace=tmp_path, environ={})

    with pytest.raises(AnbanError) as raised:
        configuration.require_webhook()

    assert raised.value.info.code is ErrorCode.CONFIGURATION_MISSING
    assert raised.value.info.details.root["reason"] == "webhook_not_configured"


def test_mcp_configuration_resolves_secret_references_without_exposing_values(
    tmp_path: Path,
) -> None:
    (tmp_path / "anban.toml").write_text(
        CONFIG
        + """
[capability.mcp]
request_timeout_seconds = 12
output_max_bytes = 4096
max_tools_per_server = 7

[[capability.mcp.servers]]
name = "dynamic"
transport = "stdio"
command = "python"
args = ["server.py"]
cwd = "."
environment = { MCP_FIXTURE_KEY = "TEST_MCP_FIXTURE_KEY" }
""",
        encoding="utf-8",
    )
    (tmp_path / "secrets.env").write_text(
        "TEST_MCP_FIXTURE_KEY=synthetic-mcp-secret\n",
        encoding="utf-8",
    )

    configuration = load_configuration(workspace=tmp_path, environ={})

    assert configuration.mcp.request_timeout_seconds == 12
    assert configuration.mcp.max_tools_per_server == 7
    assert configuration.mcp.servers[0].name == "dynamic"
    assert "synthetic-mcp-secret" not in repr(configuration)
    assert "synthetic-mcp-secret" in configuration.protected_values()


def test_missing_mcp_environment_reference_fails_explicitly(tmp_path: Path) -> None:
    (tmp_path / "anban.toml").write_text(
        CONFIG
        + """
[[capability.mcp.servers]]
name = "missing"
command = "python"
environment = { MCP_FIXTURE_KEY = "MISSING_MCP_FIXTURE_KEY" }
""",
        encoding="utf-8",
    )
    (tmp_path / "secrets.env").write_text("", encoding="utf-8")

    with pytest.raises(AnbanError) as raised:
        load_configuration(workspace=tmp_path, environ={})

    assert raised.value.info.code is ErrorCode.CONFIGURATION_MISSING
    assert raised.value.info.details.root["mcp_server"] == "missing"


def test_missing_configuration_fails_explicitly(tmp_path: Path) -> None:
    (tmp_path / "anban.toml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "secrets.env").write_text("", encoding="utf-8")
    with pytest.raises(AnbanError) as raised:
        load_configuration(workspace=tmp_path, environ={}).require_model()
    assert raised.value.info.code is ErrorCode.CONFIGURATION_MISSING
    assert "synthetic-test-value" not in str(raised.value.as_dict())


def test_configuration_above_hard_limit_fails_without_clamping(tmp_path: Path) -> None:
    (tmp_path / "anban.toml").write_text(
        CONFIG.replace(
            'model_env = "OPENAI_COMPATIBLE_MODEL"',
            'model_env = "OPENAI_COMPATIBLE_MODEL"\nrequest_timeout_seconds = 121',
        ),
        encoding="utf-8",
    )
    (tmp_path / "secrets.env").write_text("", encoding="utf-8")
    with pytest.raises(AnbanError) as raised:
        load_configuration(workspace=tmp_path, environ={})
    assert raised.value.info.code is ErrorCode.VALIDATION_FAILED


def test_policy_defaults_are_inside_their_hard_ranges() -> None:
    assert (
        policy.MODEL_REQUEST_TIMEOUT_MIN_SECONDS
        <= policy.MODEL_REQUEST_TIMEOUT_DEFAULT_SECONDS
        <= policy.MODEL_REQUEST_TIMEOUT_MAX_SECONDS
    )
    assert (
        policy.MODEL_TRANSPORT_RETRIES_MIN
        <= policy.MODEL_TRANSPORT_RETRIES_DEFAULT
        <= policy.MODEL_TRANSPORT_RETRIES_MAX
    )
    assert (
        policy.MODEL_RESPONSE_REPAIR_RETRIES_MIN
        <= policy.MODEL_RESPONSE_REPAIR_RETRIES_DEFAULT
        <= policy.MODEL_RESPONSE_REPAIR_RETRIES_MAX
    )
    assert (
        policy.AGENT_MAX_REPLANS_MIN
        <= policy.AGENT_MAX_REPLANS_DEFAULT
        <= policy.AGENT_MAX_REPLANS_MAX
    )
