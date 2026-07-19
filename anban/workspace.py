"""Idempotent managed Workspace initialization."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from anban.config import policy
from scripts.workspace_bootstrap import REPOSITORY, WorkspaceResolutionError, resolve_workspace

_DIRECTORIES = ("skills", "runs", "artifacts", "cache", "logs", "tmp")


def default_configuration_text() -> str:
    return f"""# 配置结构版本；由安伴维护，不应手工更改。
schema_version = 1
# Workspace 逻辑标识；可调整，但不得包含物理路径或敏感信息。
workspace_id = "local-main"

[model.default]
# v0.1 固定使用 OpenAI-compatible Provider Adapter。
provider = "openai-compatible"
# 模型端点只允许从该固定环境变量引用，实际值必须放在 secrets.env。
base_url_env = "OPENAI_COMPATIBLE_BASE_URL"
# API Key 只允许从该固定环境变量引用，实际值必须放在 secrets.env。
api_key_env = "OPENAI_COMPATIBLE_API_KEY"
# 模型名称只允许从该固定环境变量引用，实际值必须放在 secrets.env。
model_env = "OPENAI_COMPATIBLE_MODEL"
# 单次模型请求超时时间，单位为秒；允许范围 1–120。
request_timeout_seconds = {policy.MODEL_REQUEST_TIMEOUT_DEFAULT_SECONDS}
# 临时传输错误的自动重试次数，不包含首次请求；允许范围 0–3。
transport_retries = {policy.MODEL_TRANSPORT_RETRIES_DEFAULT}
# 非法响应结构的修复重试次数；单个 Agent Node 共用，允许范围 0–3。
response_repair_retries = {policy.MODEL_RESPONSE_REPAIR_RETRIES_DEFAULT}

[agent]
# 单个 Agent Node 最大模型逻辑轮次；不可超过 24，修复请求计入轮次。
max_model_turns = {policy.AGENT_MAX_MODEL_TURNS_DEFAULT}
# 单个 Agent Node 最大 Capability 调用次数；不可超过 32。
max_capability_calls = {policy.AGENT_MAX_CAPABILITY_CALLS_DEFAULT}
# 单次 Agent 执行总超时时间，单位为秒；不可超过 1800。
total_timeout_seconds = {policy.AGENT_TOTAL_TIMEOUT_DEFAULT_SECONDS}
# 连续相同 Capability 调用达到该次数时终止；0 禁用，1 非法，最大 8。
repeated_call_limit = {policy.AGENT_REPEATED_CALL_LIMIT_DEFAULT}
# 完成评估允许选择替代路径的最大次数；0 禁用，最大 8。
max_replans = {policy.AGENT_MAX_REPLANS_DEFAULT}

[capability.process]
# process.execute 默认超时时间，单位为秒。
default_timeout_seconds = {policy.PROCESS_DEFAULT_TIMEOUT_DEFAULT_SECONDS}
# 单次调用允许请求的最大超时时间，单位为秒。
max_timeout_seconds = {policy.PROCESS_TIMEOUT_CONFIG_DEFAULT_SECONDS}
# stdout 和 stderr 分别允许保留的最大字节数。
stdout_max_bytes = {policy.PROCESS_STDOUT_MAX_BYTES}
stderr_max_bytes = {policy.PROCESS_STDERR_MAX_BYTES}
# stdin 文本的最大 UTF-8 字节数。
stdin_max_bytes = {policy.PROCESS_STDIN_MAX_BYTES}
# 单次进程参数和声明 Artifact 的最大数量。
max_arguments = {policy.PROCESS_ARGUMENTS_MAX}
max_artifacts = {policy.PROCESS_ARTIFACTS_MAX}
# 单个声明 Artifact 的最大字节数。
artifact_max_bytes = {policy.PROCESS_ARTIFACT_MAX_BYTES}

[capability.mcp]
# MCP stdio 初始化、发现和调用的单次协议超时；允许范围 1–300 秒。
request_timeout_seconds = {policy.MCP_REQUEST_TIMEOUT_DEFAULT_SECONDS}
# 单次 Tool Result 可交给模型的最大 UTF-8 字节数。
output_max_bytes = {policy.MCP_OUTPUT_MAX_BYTES}
# 单个服务器允许动态发现的 Tool 数量上限。
max_tools_per_server = {policy.MCP_TOOLS_MAX}
# 服务器使用 [[capability.mcp.servers]] 配置。环境值只能写成 environment
# 表中的 secrets.env/进程环境变量引用，不得把凭据直接写入本文件。

[interaction.webhook]
# 单个原始请求体上限，在 JSON 解析和 Interaction admission 之前执行。
body_max_bytes = {policy.WEBHOOK_BODY_MAX_BYTES}
# HMAC 时间戳允许的最大时钟偏差，单位为秒。
clock_skew_seconds = {policy.WEBHOOK_CLOCK_SKEW_DEFAULT_SECONDS}
# Endpoint 使用 [[interaction.webhook.endpoints]] 配置；每个条目只保存逻辑 name 与
# secret_env 引用，真实 HMAC Secret 必须来自 secrets.env 或进程环境。

[database]
# 开发数据库只允许从该固定环境变量引用，实际 URL 必须放在 secrets.env。
url_env = "DATABASE_URL"
# 测试数据库只允许从该固定环境变量引用，实际 URL 必须放在 secrets.env。
test_url_env = "ANBAN_TEST_DATABASE_URL"
"""


@dataclass(frozen=True)
class WorkspaceInitialization:
    created_root: bool
    created_config: bool
    created_secrets: bool


def initialize_workspace() -> WorkspaceInitialization:
    root = resolve_workspace().path
    _ensure_safe_root(root)
    created_root = not root.exists()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir():
        raise WorkspaceResolutionError(
            "workspace_layout_invalid", "Managed Workspace root is not a directory."
        )
    root.chmod(0o700)
    for name in _DIRECTORIES:
        directory = root / name
        if directory.is_symlink() or directory.exists() and not directory.is_dir():
            raise WorkspaceResolutionError(
                "workspace_layout_invalid", "Managed Workspace layout is invalid."
            )
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)

    config = root / "anban.toml"
    created_config = _create_file(config, default_configuration_text().encode(), 0o600)
    _validate_existing_config(config)
    secrets = root / "secrets.env"
    created_secrets = _create_file(secrets, b"", 0o600)
    if secrets.is_symlink() or not secrets.is_file():
        raise WorkspaceResolutionError(
            "workspace_secret_invalid", "Workspace secret file is invalid."
        )
    secrets.chmod(0o600)
    return WorkspaceInitialization(created_root, created_config, created_secrets)


def _ensure_safe_root(root: Path) -> None:
    repository = REPOSITORY.resolve(strict=True)
    home = Path.home().resolve(strict=True)
    if (
        root == Path(root.anchor)
        or root in {home, repository}
        or root.is_relative_to(repository)
        or repository.is_relative_to(root)
    ):
        raise WorkspaceResolutionError("workspace_path_unsafe", "Managed Workspace path is unsafe.")


def _create_file(path: Path, content: bytes, mode: int) -> bool:
    if path.is_symlink():
        raise WorkspaceResolutionError(
            "workspace_layout_invalid", "Managed Workspace file is invalid."
        )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        return False
    try:
        os.write(descriptor, content)
    finally:
        os.close(descriptor)
    return True


def _validate_existing_config(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise WorkspaceResolutionError(
            "workspace_configuration_invalid", "Workspace configuration is invalid."
        )
    try:
        with path.open("rb") as handle:
            configuration = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WorkspaceResolutionError(
            "workspace_configuration_invalid", "Workspace configuration is invalid."
        ) from exc
    if configuration.get("schema_version") != 1 or not isinstance(
        configuration.get("workspace_id"), str
    ):
        raise WorkspaceResolutionError(
            "workspace_configuration_invalid", "Workspace configuration is invalid."
        )
