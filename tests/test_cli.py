"""Command-level CLI dispatch, stable exit codes, and safe output tests."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import anban.cli as cli
from anban.application import InventoryApplication
from anban.capability import CapabilityRegistry, UnifiedCapabilityInventory
from anban.core.errors import AnbanError, ErrorCode, ErrorInfo


def test_cli_module_does_not_import_provider_or_capability_adapters() -> None:
    path = Path(cli.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any(
        name.startswith(("anban.model", "anban.capability", "anban.persistence", "openai"))
        for name in imports
    )


def test_workspace_init_command_is_machine_readable_without_physical_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("ANBAN_WORKSPACE_DIR", str(workspace))

    assert cli.main(["workspace", "init", "--json"]) == cli.EXIT_SUCCESS
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "initialized"
    assert str(workspace) not in str(payload)


def test_run_command_dispatch_and_global_json_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[tuple[str, bool]] = []

    async def execute(task: str, *, json_output: bool) -> int:
        received.append((task, json_output))
        return cli.EXIT_SUCCESS

    monkeypatch.setattr(cli, "execute_run", execute)
    assert cli.main(["--json", "run", "bounded task"]) == cli.EXIT_SUCCESS
    assert received == [("bounded task", True)]


def test_async_run_command_dispatches_continuation_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[tuple[str, bool]] = []

    async def execute(task: str, *, json_output: bool) -> int:
        received.append((task, json_output))
        return cli.EXIT_SUCCESS

    monkeypatch.setattr(cli, "execute_run_async", execute)
    assert cli.main(["run", "durable continuation", "--async", "--json"]) == cli.EXIT_SUCCESS
    assert received == [("durable continuation", True)]


def test_detached_start_and_fresh_checkpoint_commands_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint_id = "00000000-0000-0000-0000-000000000789"
    received: list[tuple[object, ...]] = []

    async def start(task: str, *, json_output: bool, detach: bool = False) -> int:
        received.append(("start", task, json_output, detach))
        return cli.EXIT_SUCCESS

    async def continue_checkpoint(
        identifier: object,
        *,
        cancel: bool,
        json_output: bool,
    ) -> int:
        received.append(("checkpoint", str(identifier), cancel, json_output))
        return cli.EXIT_SUCCESS

    monkeypatch.setattr(cli, "execute_run_async", start)
    monkeypatch.setattr(cli, "execute_checkpoint", continue_checkpoint)

    assert cli.main(["run", "detached work", "--async", "--detach", "--json"]) == cli.EXIT_SUCCESS
    assert cli.main(["run", "resume", checkpoint_id, "--json"]) == cli.EXIT_SUCCESS
    assert cli.main(["run", "cancel", checkpoint_id]) == cli.EXIT_SUCCESS
    assert received == [
        ("start", "detached work", True, True),
        ("checkpoint", checkpoint_id, False, True),
        ("checkpoint", checkpoint_id, True, False),
    ]


def test_mid_run_update_dispatches_external_correlation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[tuple[str, str, str, bool]] = []

    async def update(
        namespace: str,
        correlation_value: str,
        content: str,
        *,
        json_output: bool,
    ) -> int:
        received.append((namespace, correlation_value, content, json_output))
        return cli.EXIT_SUCCESS

    monkeypatch.setattr(cli, "execute_run_update", update)

    assert (
        cli.main(
            [
                "run",
                "update",
                "anban.continuation",
                "opaque-correlation",
                "Apply",
                "the",
                "new",
                "constraint.",
                "--json",
            ]
        )
        == cli.EXIT_SUCCESS
    )
    assert received == [
        (
            "anban.continuation",
            "opaque-correlation",
            "Apply the new constraint.",
            True,
        )
    ]


def test_detach_requires_async_mode(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["run", "detached work", "--detach"]) == cli.EXIT_USAGE
    assert "validation_failed" in capsys.readouterr().err


def test_run_show_and_query_commands_dispatch_stable_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "00000000-0000-0000-0000-000000000123"
    received: list[tuple[str, str, bool]] = []

    async def show(identifier: object, *, json_output: bool) -> int:
        received.append(("show", str(identifier), json_output))
        return cli.EXIT_SUCCESS

    async def trace(identifier: object, *, json_output: bool) -> int:
        received.append(("trace", str(identifier), json_output))
        return cli.EXIT_SUCCESS

    async def artifacts(identifier: object, *, json_output: bool) -> int:
        received.append(("artifacts", str(identifier), json_output))
        return cli.EXIT_SUCCESS

    monkeypatch.setattr(cli, "show_run", show)
    monkeypatch.setattr(cli, "show_trace", trace)
    monkeypatch.setattr(cli, "list_artifacts", artifacts)
    assert cli.main(["run", "show", run_id, "--json"]) == cli.EXIT_SUCCESS
    assert cli.main(["trace", run_id, "--json"]) == cli.EXIT_SUCCESS
    assert cli.main(["artifacts", run_id, "--json"]) == cli.EXIT_SUCCESS
    assert received == [
        ("show", run_id, True),
        ("trace", run_id, True),
        ("artifacts", run_id, True),
    ]


def test_context_commands_dispatch_stable_task_and_session_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = "00000000-0000-0000-0000-000000000456"
    received: list[tuple[str, str, bool]] = []

    async def show(scope: str, identifier: object, *, json_output: bool) -> int:
        received.append((scope, str(identifier), json_output))
        return cli.EXIT_SUCCESS

    monkeypatch.setattr(cli, "show_context", show)
    assert cli.main(["context", "task", identity, "--json"]) == cli.EXIT_SUCCESS
    assert cli.main(["context", "session", identity]) == cli.EXIT_SUCCESS
    assert received == [("task", identity, True), ("session", identity, False)]


def test_raw_exception_text_is_never_emitted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = "raw-exception-canary"

    async def execute(task: str, *, json_output: bool) -> int:
        raise RuntimeError(canary)

    monkeypatch.setattr(cli, "execute_run", execute)
    assert cli.main(["run", "bounded task"]) == cli.EXIT_FAILURE
    output = capsys.readouterr()
    assert canary not in output.out + output.err
    assert "execution_failed" in output.err


def test_capability_inventory_cli_lists_searches_and_describes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    application = InventoryApplication(
        UnifiedCapabilityInventory(CapabilityRegistry(), model_available=True)
    )
    monkeypatch.setattr(cli, "build_inventory_application", lambda: application)

    assert cli.main(["capabilities", "list", "--json"]) == cli.EXIT_SUCCESS
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["generated_at"]
    assert {item["kind"] for item in snapshot["items"]} >= {
        "model",
        "mcp",
        "memory",
        "sub_agent",
    }

    assert (
        cli.main(
            [
                "capabilities",
                "search",
                "durable context",
                "--kind",
                "memory",
                "--limit",
                "3",
                "--json",
            ]
        )
        == cli.EXIT_SUCCESS
    )
    matches = json.loads(capsys.readouterr().out)
    assert [item["key"] for item in matches] == ["memory:context"]

    assert cli.main(["capabilities", "describe", "model:default", "--json"]) == cli.EXIT_SUCCESS
    assert json.loads(capsys.readouterr().out)["availability"] == "ready"


@pytest.mark.parametrize(
    ("error", "exit_code"),
    [
        (
            ErrorInfo(code=ErrorCode.CONFIGURATION_MISSING, message="Configuration missing"),
            cli.EXIT_USAGE,
        ),
        (ErrorInfo(code=ErrorCode.MODEL_TIMEOUT, message="Model timed out"), cli.EXIT_TIMEOUT),
        (
            ErrorInfo(code=ErrorCode.EXECUTION_TIMED_OUT, message="Execution timed out"),
            cli.EXIT_TIMEOUT,
        ),
        (
            ErrorInfo(code=ErrorCode.EXECUTION_INTERRUPTED, message="Execution interrupted"),
            cli.EXIT_INTERRUPTED,
        ),
    ],
)
def test_structured_errors_have_deterministic_exit_codes(
    error: ErrorInfo,
    exit_code: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def execute(task: str, *, json_output: bool) -> int:
        raise AnbanError(error)

    monkeypatch.setattr(cli, "execute_run", execute)
    assert cli.main(["run", "bounded task", "--json"]) == exit_code
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"]["code"] == error.code.value
