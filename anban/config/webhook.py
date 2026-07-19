"""Strict Webhook endpoint configuration and Secret resolution."""

from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from anban.config import policy

_ENDPOINT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_ENVIRONMENT_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class WebhookConfigurationValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WebhookEndpointSettings(WebhookConfigurationValue):
    name: str = Field(min_length=1, max_length=32)
    secret_env: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if _ENDPOINT_PATTERN.fullmatch(value) is None:
            raise ValueError("Webhook endpoint name must be a bounded logical identifier")
        return value

    @field_validator("secret_env")
    @classmethod
    def validate_secret_environment_key(cls, value: str) -> str:
        if _ENVIRONMENT_KEY_PATTERN.fullmatch(value) is None:
            raise ValueError("Webhook Secret environment reference is invalid")
        return value


class WebhookSettings(WebhookConfigurationValue):
    body_max_bytes: int = Field(
        default=policy.WEBHOOK_BODY_MAX_BYTES,
        ge=1,
        le=policy.WEBHOOK_BODY_HARD_MAX_BYTES,
    )
    clock_skew_seconds: int = Field(
        default=policy.WEBHOOK_CLOCK_SKEW_DEFAULT_SECONDS,
        ge=policy.WEBHOOK_CLOCK_SKEW_MIN_SECONDS,
        le=policy.WEBHOOK_CLOCK_SKEW_MAX_SECONDS,
    )
    endpoints: tuple[WebhookEndpointSettings, ...] = Field(
        default=(), max_length=policy.WEBHOOK_ENDPOINTS_MAX
    )

    @model_validator(mode="after")
    def validate_unique_endpoints(self) -> WebhookSettings:
        names = tuple(endpoint.name for endpoint in self.endpoints)
        if len(names) != len(set(names)):
            raise ValueError("Webhook endpoint names must be unique")
        return self


class WebhookEndpointConfiguration(WebhookConfigurationValue):
    name: str
    secret: SecretStr = Field(repr=False)


class WebhookConfiguration(WebhookConfigurationValue):
    body_max_bytes: int
    clock_skew_seconds: int
    endpoints: tuple[WebhookEndpointConfiguration, ...]

    def endpoint(self, name: str) -> WebhookEndpointConfiguration | None:
        return next((endpoint for endpoint in self.endpoints if endpoint.name == name), None)

    def protected_values(self) -> tuple[str, ...]:
        return tuple(endpoint.secret.get_secret_value() for endpoint in self.endpoints)


class WebhookConfigurationResolutionError(ValueError):
    def __init__(self, endpoint_name: str) -> None:
        super().__init__("Webhook endpoint Secret is unavailable")
        self.endpoint_name = endpoint_name


def resolve_webhook_configuration(
    settings: WebhookSettings,
    *,
    environment: Mapping[str, str],
    secrets: Mapping[str, str | None],
) -> WebhookConfiguration:
    endpoints: list[WebhookEndpointConfiguration] = []
    for endpoint in settings.endpoints:
        value = environment.get(endpoint.secret_env) or secrets.get(endpoint.secret_env)
        if not isinstance(value, str) or len(value.encode()) < policy.WEBHOOK_SECRET_MIN_BYTES:
            raise WebhookConfigurationResolutionError(endpoint.name)
        endpoints.append(WebhookEndpointConfiguration(name=endpoint.name, secret=SecretStr(value)))
    return WebhookConfiguration(
        body_max_bytes=settings.body_max_bytes,
        clock_skew_seconds=settings.clock_skew_seconds,
        endpoints=tuple(endpoints),
    )
