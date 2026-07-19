"""Bounded Workspace configuration for real MCP stdio servers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from anban.config import policy

_SERVER_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class McpConfigurationValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class McpStdioServerSettings(McpConfigurationValue):
    name: str = Field(min_length=1, max_length=32, pattern=_SERVER_NAME.pattern)
    transport: Literal["stdio"] = "stdio"
    command: str = Field(min_length=1, max_length=512)
    args: tuple[str, ...] = Field(default=(), max_length=policy.MCP_ARGUMENTS_MAX)
    cwd: str = Field(default=".", min_length=1, max_length=512)
    environment: dict[str, str] = Field(
        default_factory=dict,
        max_length=policy.MCP_ENVIRONMENT_MAX,
    )

    @field_validator("command", "cwd")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if "\x00" in value or not value.strip():
            raise ValueError("MCP stdio configuration contains invalid text")
        return value

    @field_validator("args")
    @classmethod
    def validate_arguments(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any("\x00" in value or len(value) > 4096 for value in values):
            raise ValueError("MCP stdio arguments are invalid")
        return values

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, values: dict[str, str]) -> dict[str, str]:
        if any(
            _ENVIRONMENT_NAME.fullmatch(child) is None
            or _ENVIRONMENT_NAME.fullmatch(reference) is None
            for child, reference in values.items()
        ):
            raise ValueError("MCP environment references are invalid")
        return values


class McpSettings(McpConfigurationValue):
    request_timeout_seconds: int = Field(
        default=policy.MCP_REQUEST_TIMEOUT_DEFAULT_SECONDS,
        ge=policy.MCP_REQUEST_TIMEOUT_MIN_SECONDS,
        le=policy.MCP_REQUEST_TIMEOUT_MAX_SECONDS,
    )
    output_max_bytes: int = Field(
        default=policy.MCP_OUTPUT_MAX_BYTES,
        ge=1,
        le=policy.MCP_OUTPUT_HARD_MAX_BYTES,
    )
    max_tools_per_server: int = Field(
        default=policy.MCP_TOOLS_MAX,
        ge=1,
        le=policy.MCP_TOOLS_HARD_MAX,
    )
    servers: tuple[McpStdioServerSettings, ...] = Field(
        default=(),
        max_length=policy.MCP_SERVERS_MAX,
    )

    @model_validator(mode="after")
    def validate_unique_servers(self) -> Self:
        names = tuple(server.name for server in self.servers)
        if len(names) != len(set(names)):
            raise ValueError("MCP server names must be unique")
        return self


class McpServerConfiguration(McpConfigurationValue):
    name: str
    command: str
    args: tuple[str, ...]
    cwd: str
    environment: dict[str, SecretStr] = Field(repr=False)


class McpConfiguration(McpConfigurationValue):
    request_timeout_seconds: int
    output_max_bytes: int
    max_tools_per_server: int
    servers: tuple[McpServerConfiguration, ...]

    def protected_values(self) -> tuple[str, ...]:
        return tuple(
            secret.get_secret_value()
            for server in self.servers
            for secret in server.environment.values()
            if secret.get_secret_value()
        )


class McpConfigurationResolutionError(ValueError):
    """One configured MCP secret reference is unavailable."""

    def __init__(self, server_name: str) -> None:
        self.server_name = server_name
        super().__init__("configured MCP environment reference is unavailable")


def resolve_mcp_configuration(
    settings: McpSettings,
    *,
    environment: Mapping[str, str],
    secrets: Mapping[str, str | None],
) -> McpConfiguration:
    servers: list[McpServerConfiguration] = []
    for server in settings.servers:
        resolved_environment: dict[str, SecretStr] = {}
        for child, reference in server.environment.items():
            value = environment.get(reference) or secrets.get(reference)
            if not isinstance(value, str) or not value.strip():
                raise McpConfigurationResolutionError(server.name)
            resolved_environment[child] = SecretStr(value.strip())
        servers.append(
            McpServerConfiguration(
                name=server.name,
                command=server.command,
                args=server.args,
                cwd=server.cwd,
                environment=resolved_environment,
            )
        )
    return McpConfiguration(
        request_timeout_seconds=settings.request_timeout_seconds,
        output_max_bytes=settings.output_max_bytes,
        max_tools_per_server=settings.max_tools_per_server,
        servers=tuple(servers),
    )
