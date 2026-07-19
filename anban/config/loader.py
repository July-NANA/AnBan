"""Single strict loader for Workspace TOML and Secret references."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from anban.config import policy
from anban.config.mcp import (
    McpConfiguration,
    McpConfigurationResolutionError,
    McpSettings,
    resolve_mcp_configuration,
)
from anban.core import AnbanError, ErrorCode, ErrorInfo, SafeMetadata
from scripts.workspace_bootstrap import resolve_workspace

MODEL_ENVIRONMENT_KEYS = {
    "base_url": "OPENAI_COMPATIBLE_BASE_URL",
    "api_key": "OPENAI_COMPATIBLE_API_KEY",
    "model": "OPENAI_COMPATIBLE_MODEL",
}
DATABASE_ENVIRONMENT_KEYS = {
    "development": "DATABASE_URL",
    "test": "ANBAN_TEST_DATABASE_URL",
}


class ConfigurationValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelSettings(ConfigurationValue):
    provider: Literal["openai-compatible"]
    base_url_env: Literal["OPENAI_COMPATIBLE_BASE_URL"]
    api_key_env: Literal["OPENAI_COMPATIBLE_API_KEY"]
    model_env: Literal["OPENAI_COMPATIBLE_MODEL"]
    request_timeout_seconds: int = Field(
        default=policy.MODEL_REQUEST_TIMEOUT_DEFAULT_SECONDS,
        ge=policy.MODEL_REQUEST_TIMEOUT_MIN_SECONDS,
        le=policy.MODEL_REQUEST_TIMEOUT_MAX_SECONDS,
    )
    transport_retries: int = Field(
        default=policy.MODEL_TRANSPORT_RETRIES_DEFAULT,
        ge=policy.MODEL_TRANSPORT_RETRIES_MIN,
        le=policy.MODEL_TRANSPORT_RETRIES_MAX,
    )
    response_repair_retries: int = Field(
        default=policy.MODEL_RESPONSE_REPAIR_RETRIES_DEFAULT,
        ge=policy.MODEL_RESPONSE_REPAIR_RETRIES_MIN,
        le=policy.MODEL_RESPONSE_REPAIR_RETRIES_MAX,
    )


class ModelSection(ConfigurationValue):
    default: ModelSettings


class AgentConfiguration(ConfigurationValue):
    max_model_turns: int = Field(
        default=policy.AGENT_MAX_MODEL_TURNS_DEFAULT,
        ge=policy.AGENT_MAX_MODEL_TURNS_MIN,
        le=policy.AGENT_MAX_MODEL_TURNS_MAX,
    )
    max_capability_calls: int = Field(
        default=policy.AGENT_MAX_CAPABILITY_CALLS_DEFAULT,
        ge=policy.AGENT_MAX_CAPABILITY_CALLS_MIN,
        le=policy.AGENT_MAX_CAPABILITY_CALLS_MAX,
    )
    total_timeout_seconds: int = Field(
        default=policy.AGENT_TOTAL_TIMEOUT_DEFAULT_SECONDS,
        ge=policy.AGENT_TOTAL_TIMEOUT_MIN_SECONDS,
        le=policy.AGENT_TOTAL_TIMEOUT_MAX_SECONDS,
    )
    repeated_call_limit: int = Field(
        default=policy.AGENT_REPEATED_CALL_LIMIT_DEFAULT,
        ge=policy.AGENT_REPEATED_CALL_LIMIT_MIN,
        le=policy.AGENT_REPEATED_CALL_LIMIT_MAX,
    )
    max_replans: int = Field(
        default=policy.AGENT_MAX_REPLANS_DEFAULT,
        ge=policy.AGENT_MAX_REPLANS_MIN,
        le=policy.AGENT_MAX_REPLANS_MAX,
    )

    @field_validator("repeated_call_limit")
    @classmethod
    def validate_repeated_call_limit(cls, value: int) -> int:
        if value == 1:
            raise ValueError("repeated call limit must be zero or at least two")
        return value


class ProcessConfiguration(ConfigurationValue):
    default_timeout_seconds: int = Field(
        default=policy.PROCESS_DEFAULT_TIMEOUT_DEFAULT_SECONDS,
        ge=policy.PROCESS_DEFAULT_TIMEOUT_MIN_SECONDS,
        le=policy.PROCESS_TIMEOUT_MAX_SECONDS,
    )
    max_timeout_seconds: int = Field(
        default=policy.PROCESS_TIMEOUT_CONFIG_DEFAULT_SECONDS,
        ge=policy.PROCESS_DEFAULT_TIMEOUT_MIN_SECONDS,
        le=policy.PROCESS_TIMEOUT_MAX_SECONDS,
    )
    stdout_max_bytes: int = Field(
        default=policy.PROCESS_STDOUT_MAX_BYTES,
        ge=1,
        le=policy.PROCESS_OUTPUT_HARD_MAX_BYTES,
    )
    stderr_max_bytes: int = Field(
        default=policy.PROCESS_STDERR_MAX_BYTES,
        ge=1,
        le=policy.PROCESS_OUTPUT_HARD_MAX_BYTES,
    )
    stdin_max_bytes: int = Field(
        default=policy.PROCESS_STDIN_MAX_BYTES,
        ge=1,
        le=policy.PROCESS_STDIN_HARD_MAX_BYTES,
    )
    max_arguments: int = Field(
        default=policy.PROCESS_ARGUMENTS_MAX,
        ge=1,
        le=policy.PROCESS_ARGUMENTS_HARD_MAX,
    )
    max_artifacts: int = Field(
        default=policy.PROCESS_ARTIFACTS_MAX,
        ge=1,
        le=policy.PROCESS_ARTIFACTS_HARD_MAX,
    )
    artifact_max_bytes: int = Field(
        default=policy.PROCESS_ARTIFACT_MAX_BYTES,
        ge=1,
        le=policy.PROCESS_ARTIFACT_HARD_MAX_BYTES,
    )

    @model_validator(mode="after")
    def validate_timeout_order(self) -> Self:
        if self.default_timeout_seconds > self.max_timeout_seconds:
            raise ValueError("process default timeout exceeds configured maximum")
        return self


class CapabilitySection(ConfigurationValue):
    process: ProcessConfiguration = Field(default_factory=ProcessConfiguration)
    mcp: McpSettings = Field(default_factory=McpSettings)


class DatabaseSettings(ConfigurationValue):
    url_env: Literal["DATABASE_URL"]
    test_url_env: Literal["ANBAN_TEST_DATABASE_URL"]


class WorkspaceSettings(ConfigurationValue):
    schema_version: Literal[1]
    workspace_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    model: ModelSection
    agent: AgentConfiguration = Field(default_factory=AgentConfiguration)
    capability: CapabilitySection = Field(default_factory=CapabilitySection)
    database: DatabaseSettings


class ModelConfiguration(ConfigurationValue):
    base_url: SecretStr
    api_key: SecretStr
    model: str = Field(min_length=1, max_length=256)
    request_timeout_seconds: int
    transport_retries: int
    response_repair_retries: int

    @model_validator(mode="after")
    def validate_endpoint(self) -> Self:
        endpoint = urlsplit(self.base_url.get_secret_value())
        if endpoint.scheme not in {"https", "http"} or not endpoint.netloc:
            raise ValueError("model endpoint is invalid")
        return self


class DatabaseConfiguration(ConfigurationValue):
    development_url: SecretStr | None = Field(default=None, repr=False)
    test_url: SecretStr | None = Field(default=None, repr=False)

    def require(self, profile: Literal["development", "test"]) -> str:
        value = self.development_url if profile == "development" else self.test_url
        if value is None or not value.get_secret_value().strip():
            raise configuration_failure(
                ErrorCode.CONFIGURATION_MISSING,
                "database configuration is missing",
                profile=profile,
            )
        configured = value.get_secret_value().strip()
        if not configured.startswith("postgresql+asyncpg://"):
            raise configuration_failure(
                ErrorCode.VALIDATION_FAILED,
                "database configuration must use PostgreSQL asyncpg",
                profile=profile,
            )
        return configured


class AnbanConfiguration(ConfigurationValue):
    workspace: Path = Field(repr=False)
    workspace_id: str
    model: ModelConfiguration | None = Field(default=None, repr=False)
    agent: AgentConfiguration
    process: ProcessConfiguration
    mcp: McpConfiguration
    database: DatabaseConfiguration = Field(repr=False)

    def require_model(self) -> ModelConfiguration:
        if self.model is None:
            raise configuration_failure(
                ErrorCode.CONFIGURATION_MISSING, "model configuration is missing"
            )
        return self.model

    def protected_values(self) -> tuple[str, ...]:
        values: list[str] = []
        if self.model is not None:
            values.append(self.model.api_key.get_secret_value())
        for candidate in (self.database.development_url, self.database.test_url):
            if candidate is not None:
                values.append(candidate.get_secret_value())
        values.extend(self.mcp.protected_values())
        return tuple(value for value in values if value)


def configuration_failure(code: ErrorCode, message: str, **details: str | bool | int) -> AnbanError:
    return AnbanError(
        ErrorInfo(code=code, message=message, details=SafeMetadata.model_validate(details))
    )


def _resolved_value(
    key: str, environment: Mapping[str, str], secrets: Mapping[str, str | None]
) -> str | None:
    value = environment.get(key) or secrets.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def load_configuration(
    *,
    workspace: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> AnbanConfiguration:
    root = resolve_workspace().path if workspace is None else workspace
    environment = os.environ if environ is None else environ
    try:
        with (root / "anban.toml").open("rb") as handle:
            settings = WorkspaceSettings.model_validate(tomllib.load(handle))
        raw_secrets = dotenv_values(root / "secrets.env", interpolate=False)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise configuration_failure(
            ErrorCode.VALIDATION_FAILED, "Workspace configuration is invalid"
        ) from exc
    secrets = {key: value if isinstance(value, str) else None for key, value in raw_secrets.items()}
    model_values = {
        name: _resolved_value(key, environment, secrets)
        for name, key in MODEL_ENVIRONMENT_KEYS.items()
    }
    model_configuration: ModelConfiguration | None = None
    if all(model_values.values()):
        model_settings = settings.model.default
        try:
            model_configuration = ModelConfiguration(
                base_url=SecretStr(model_values["base_url"] or ""),
                api_key=SecretStr(model_values["api_key"] or ""),
                model=model_values["model"] or "",
                request_timeout_seconds=model_settings.request_timeout_seconds,
                transport_retries=model_settings.transport_retries,
                response_repair_retries=model_settings.response_repair_retries,
            )
        except ValidationError as exc:
            raise configuration_failure(
                ErrorCode.VALIDATION_FAILED, "model endpoint configuration is invalid"
            ) from exc
    database = DatabaseConfiguration(
        development_url=(
            SecretStr(value)
            if (value := _resolved_value("DATABASE_URL", environment, secrets))
            else None
        ),
        test_url=(
            SecretStr(value)
            if (value := _resolved_value("ANBAN_TEST_DATABASE_URL", environment, secrets))
            else None
        ),
    )
    try:
        mcp = resolve_mcp_configuration(
            settings.capability.mcp,
            environment=environment,
            secrets=secrets,
        )
    except McpConfigurationResolutionError as exc:
        raise configuration_failure(
            ErrorCode.CONFIGURATION_MISSING,
            "MCP server environment configuration is missing",
            mcp_server=exc.server_name,
        ) from None
    return AnbanConfiguration(
        workspace=root,
        workspace_id=settings.workspace_id,
        model=model_configuration,
        agent=settings.agent,
        process=settings.capability.process,
        mcp=mcp,
        database=database,
    )
