"""Authoritative OpenAI-compatible model configuration from the managed Workspace."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from dotenv import dotenv_values

from anban.core import AnbanError, ErrorCode, ErrorInfo, SafeMetadata
from scripts.workspace_bootstrap import resolve_workspace

MODEL_KEYS = {
    "base_url": "OPENAI_COMPATIBLE_BASE_URL",
    "api_key": "OPENAI_COMPATIBLE_API_KEY",
    "model": "OPENAI_COMPATIBLE_MODEL",
}


@dataclass(frozen=True)
class ModelConfiguration:
    base_url: str
    api_key: str
    model: str


def configuration_failure(code: ErrorCode, message: str) -> AnbanError:
    return AnbanError(ErrorInfo(code=code, message=message))


def load_model_configuration(
    *,
    workspace: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> ModelConfiguration:
    workspace = resolve_workspace().path if workspace is None else workspace
    active_environment = os.environ if environ is None else environ
    try:
        with (workspace / "anban.toml").open("rb") as handle:
            config = tomllib.load(handle)
        profile = cast(dict[str, object], config["model"]["default"])
        provider = profile.get("provider")
        references = {
            "base_url": profile.get("base_url_env"),
            "api_key": profile.get("api_key_env"),
            "model": profile.get("model_env"),
        }
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise configuration_failure(
            ErrorCode.VALIDATION_FAILED, "Workspace model configuration is invalid"
        ) from exc
    if provider != "openai-compatible" or references != MODEL_KEYS:
        raise configuration_failure(
            ErrorCode.VALIDATION_FAILED, "Workspace model configuration is not allowlisted"
        )
    try:
        secrets = dotenv_values(workspace / "secrets.env", interpolate=False)
    except OSError as exc:
        raise configuration_failure(
            ErrorCode.CONFIGURATION_MISSING, "model configuration is missing"
        ) from exc
    values = {
        name: active_environment.get(reference) or secrets.get(reference)
        for name, reference in MODEL_KEYS.items()
    }
    if any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise configuration_failure(
            ErrorCode.CONFIGURATION_MISSING, "model configuration is missing"
        )
    base_url = cast(str, values["base_url"]).strip()
    if not base_url.startswith(("https://", "http://")):
        raise AnbanError(
            ErrorInfo(
                code=ErrorCode.VALIDATION_FAILED,
                message="model endpoint configuration is invalid",
                details=SafeMetadata({"provider": "openai-compatible"}),
            )
        )
    return ModelConfiguration(
        base_url=base_url,
        api_key=cast(str, values["api_key"]).strip(),
        model=cast(str, values["model"]).strip(),
    )
