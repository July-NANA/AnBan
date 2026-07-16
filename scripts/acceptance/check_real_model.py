"""Explicitly validate a real model and one bounded local file capability."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast

from dotenv import dotenv_values
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageFunctionToolCall, ChatCompletionMessageParam

from scripts.workspace_bootstrap import resolve_workspace as resolve_workspace_bootstrap

MODEL_KEYS = (
    "OPENAI_COMPATIBLE_BASE_URL",
    "OPENAI_COMPATIBLE_API_KEY",
    "OPENAI_COMPATIBLE_MODEL",
)


class ReadinessError(RuntimeError):
    """A sanitized readiness failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str, remediation: str, *, blocked: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remediation = remediation
        self.blocked = blocked


@dataclass(frozen=True)
class ModelConfiguration:
    provider_type: str
    base_url: str
    api_key: str
    model: str


@dataclass(frozen=True)
class ModelCheckResult:
    provider_type: str
    model: str
    normal_request: bool
    native_tool_calling: bool
    real_file_operation: bool
    tool_result_returned: bool
    final_response: bool


def resolve_workspace() -> Path:
    """Resolve the managed Workspace without exposing it to model-visible content."""

    return resolve_workspace_bootstrap().path


def load_model_configuration(workspace: Path) -> ModelConfiguration:
    """Read only the environment-variable references declared by anban.toml."""

    config_path = workspace / "anban.toml"
    secrets_path = workspace / "secrets.env"
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
        model_config = cast(dict[str, Any], config["model"]["default"])
        provider_type = str(model_config["provider"])
        names = {
            "base_url": str(model_config["base_url_env"]),
            "api_key": str(model_config["api_key_env"]),
            "model": str(model_config["model_env"]),
        }
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ReadinessError(
            "model_configuration_invalid",
            "Workspace model references are missing or invalid.",
            "Repair anban.toml using the documented schema.",
        ) from exc

    if set(names.values()) != set(MODEL_KEYS):
        raise ReadinessError(
            "model_configuration_not_allowlisted",
            "Workspace model references are outside the approved allowlist.",
            "Use the three documented OPENAI_COMPATIBLE_* references.",
        )

    try:
        parsed = dotenv_values(secrets_path, interpolate=False)
    except OSError as exc:
        raise ReadinessError(
            "real_model_credentials_missing",
            "Workspace secrets.env is unavailable.",
            "Create secrets.env with mode 0600 and configure the real provider.",
            blocked=True,
        ) from exc

    values = {name: os.environ.get(name) or parsed.get(name) for name in MODEL_KEYS}
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise ReadinessError(
            "real_model_credentials_missing",
            "One or more real model settings are missing.",
            "Configure all three OPENAI_COMPATIBLE_* values in Workspace secrets.env.",
            blocked=True,
        )

    return ModelConfiguration(
        provider_type=provider_type,
        base_url=cast(str, values[names["base_url"]]),
        api_key=cast(str, values[names["api_key"]]),
        model=cast(str, values[names["model"]]),
    )


def validate_tool_arguments(raw_arguments: str, nonce: str) -> dict[str, str]:
    """Validate the closed Tool schema and exact random validation value."""

    try:
        parsed: object = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ReadinessError(
            "real_model_tool_arguments_invalid",
            "The native Tool Call did not contain valid JSON arguments.",
            "Use a provider/model with schema-conformant native Tool Calling.",
        ) from exc
    if not isinstance(parsed, dict):
        raise ReadinessError(
            "real_model_tool_arguments_invalid",
            "The native Tool Call did not match the closed argument schema.",
            "Use a provider/model that honors the supplied Tool schema.",
        )
    arguments = cast(dict[str, object], parsed)
    if set(arguments) != {"filename", "content"}:
        raise ReadinessError(
            "real_model_tool_arguments_invalid",
            "The native Tool Call did not match the closed argument schema.",
            "Use a provider/model that honors the supplied Tool schema.",
        )
    if arguments.get("filename") != "validation.txt" or arguments.get("content") != nonce:
        raise ReadinessError(
            "real_model_tool_arguments_mismatch",
            "The native Tool Call changed the required filename or validation value.",
            "Use a model that follows the bounded file validation request exactly.",
        )
    return {"filename": "validation.txt", "content": nonce}


def _provider_failure(exc: Exception) -> NoReturn:
    raise ReadinessError(
        "real_model_request_failed",
        f"The real provider request failed ({type(exc).__name__}).",
        "Verify provider availability, model access, and OpenAI-compatible request support.",
        blocked=True,
    ) from exc


def run_check(workspace: Path | None = None) -> ModelCheckResult:
    """Run normal generation, native Tool Calling, real file I/O, and final generation."""

    workspace = workspace or resolve_workspace()
    configuration = load_model_configuration(workspace)
    try:
        client = OpenAI(
            api_key=configuration.api_key,
            base_url=configuration.base_url,
            timeout=60.0,
            max_retries=0,
        )
    except Exception as exc:
        _provider_failure(exc)

    try:
        normal = client.chat.completions.create(
            model=configuration.model,
            messages=[{"role": "user", "content": "Reply briefly that this real request works."}],
            max_tokens=512,
        )
    except Exception as exc:  # The SDK exposes multiple provider/network exception subclasses.
        _provider_failure(exc)
    normal_content = normal.choices[0].message.content if normal.choices else None
    if not normal_content or not normal_content.strip():
        raise ReadinessError(
            "real_model_normal_response_empty",
            "The real model returned no normal response content.",
            "Verify that the configured model supports chat completion content.",
        )

    nonce = uuid.uuid4().hex
    tools = [
        {
            "type": "function",
            "function": {
                "name": "write_validation_file",
                "description": "Write and read one validation file in an isolated local directory.",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string", "const": "validation.txt"},
                        "content": {"type": "string", "const": nonce},
                    },
                    "required": ["filename", "content"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    messages: list[ChatCompletionMessageParam] = [
        {
            "role": "user",
            "content": (
                "Call write_validation_file exactly once with filename validation.txt and content "
                f"{nonce}. After the Tool result, provide a brief final response."
            ),
        }
    ]
    try:
        tool_response = client.chat.completions.create(
            model=configuration.model,
            messages=messages,
            tools=cast(Any, tools),
            tool_choice="auto",
            max_tokens=1024,
        )
    except Exception as exc:
        _provider_failure(exc)

    if not tool_response.choices:
        raise ReadinessError(
            "real_model_tool_calling_unsupported",
            "The provider returned no choice for the native Tool request.",
            "Use a model/provider with native OpenAI-compatible Tool Calling.",
            blocked=True,
        )
    assistant_message = tool_response.choices[0].message
    tool_calls = assistant_message.tool_calls
    if (
        not tool_calls
        or len(tool_calls) != 1
        or not isinstance(tool_calls[0], ChatCompletionMessageFunctionToolCall)
        or tool_calls[0].function.name != "write_validation_file"
    ):
        raise ReadinessError(
            "real_model_tool_calling_unsupported",
            "The provider did not return the required native Tool Call.",
            "Enable native Tool Calling for the configured model; JSON simulation is not accepted.",
            blocked=True,
        )
    tool_call = tool_calls[0]
    arguments = validate_tool_arguments(tool_call.function.arguments, nonce)

    tmp_root = (workspace / "tmp").resolve()
    validation_dir = (tmp_root / f"model-tool-check-{uuid.uuid4().hex}").resolve()
    if validation_dir.parent != tmp_root:
        raise ReadinessError(
            "real_capability_path_invalid",
            "The validation directory escaped the managed Workspace tmp directory.",
            "Repair Workspace path resolution before running a file capability.",
        )
    validation_dir.mkdir(mode=0o700)
    try:
        validation_path = validation_dir / arguments["filename"]
        with validation_path.open("x", encoding="utf-8") as handle:
            handle.write(arguments["content"])
        observed = validation_path.read_text(encoding="utf-8")
        if observed != nonce:
            raise ReadinessError(
                "real_capability_content_mismatch",
                "The real file capability did not preserve the validation content.",
                "Inspect local filesystem permissions and encoding behavior.",
            )

        messages.append(cast(ChatCompletionMessageParam, assistant_message.model_dump()))
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(
                    {
                        "filename": "validation.txt",
                        "content": observed,
                        "status": "written_and_read",
                    }
                ),
            }
        )
        try:
            final = client.chat.completions.create(
                model=configuration.model,
                messages=messages,
                tools=cast(Any, tools),
                max_tokens=512,
            )
        except Exception as exc:
            _provider_failure(exc)
        if not final.choices:
            final_content = None
            final_tool_calls = None
        else:
            final_content = final.choices[0].message.content
            final_tool_calls = final.choices[0].message.tool_calls
        if final_tool_calls or not final_content or not final_content.strip():
            raise ReadinessError(
                "real_model_final_response_missing",
                "The model did not produce a final response after the real Tool result.",
                "Verify that the provider supports the complete native Tool Calling exchange.",
            )
    finally:
        shutil.rmtree(validation_dir, ignore_errors=True)

    return ModelCheckResult(
        provider_type=configuration.provider_type,
        model=configuration.model,
        normal_request=True,
        native_tool_calling=True,
        real_file_operation=True,
        tool_result_returned=True,
        final_response=True,
    )


def main() -> int:
    try:
        result = run_check()
    except ReadinessError as exc:
        status = "BLOCKED" if exc.blocked else "FAIL"
        print(f"real model: {status} [{exc.code}] {exc.message}")
        print(f"remediation: {exc.remediation}")
        return 2 if exc.blocked else 1
    print(f"real model: PASS provider={result.provider_type} model={result.model}")
    print("native Tool Calling: PASS")
    print("real file capability: PASS")
    print("Tool Result return and final response: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
