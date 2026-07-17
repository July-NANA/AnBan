"""CLI-only Interaction Adapter for Anban v0.1."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from anban.application import Application, build_application
from anban.core.errors import AnbanError, ErrorCategory, ErrorCode, ErrorInfo
from anban.core.ids import new_interaction_id
from anban.interaction import InteractionEnvelope
from anban.runtime import AgentOutcomeStatus, ExecutionResult
from anban.workspace import WorkspaceInitialization, initialize_workspace
from scripts.workspace_bootstrap import WorkspaceResolutionError

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_TIMEOUT = 124
EXIT_INTERRUPTED = 130


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="anban")
    root.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    commands = root.add_subparsers(dest="command", required=True)
    workspace = commands.add_parser("workspace", help="Manage the local Workspace.")
    workspace_commands = workspace.add_subparsers(dest="workspace_command", required=True)
    workspace_init = workspace_commands.add_parser("init", help="Initialize the Workspace.")
    add_json_option(workspace_init)
    run = commands.add_parser("run", help="Execute one durable task.")
    run.add_argument("task")
    add_json_option(run)
    chat = commands.add_parser("chat", help="Start one bounded temporary chat.")
    add_json_option(chat)
    return root


def add_json_option(command: argparse.ArgumentParser) -> None:
    command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)


def envelope(content: str) -> InteractionEnvelope:
    return InteractionEnvelope(id=new_interaction_id(), content=content)


async def execute_run(task: str, *, json_output: bool) -> int:
    application = await build_application()
    try:
        result = await application.interactions.submit(envelope(task))
    finally:
        await application.close()
    emit_result(result, json_output=json_output)
    return result_exit_code(result)


async def execute_chat(*, json_output: bool) -> int:
    application: Application = await build_application()
    session = application.interactions.chat()
    exit_code = EXIT_SUCCESS
    try:
        while session.can_continue:
            try:
                line = await asyncio.wait_for(
                    read_stdin_line("" if json_output else "anban> "),
                    timeout=session.remaining_seconds,
                )
            except EOFError:
                break
            except TimeoutError:
                expired = await session.expire()
                if expired is not None:
                    emit_result(expired, json_output=json_output)
                exit_code = EXIT_TIMEOUT
                break
            content = line.strip()
            if content in {"/exit", "/quit"}:
                break
            if not content:
                continue
            result = await session.submit(envelope(content))
            emit_result(result, json_output=json_output)
            exit_code = result_exit_code(result)
            if exit_code != EXIT_SUCCESS:
                break
    except asyncio.CancelledError:
        await asyncio.shield(session.interrupt())
        exit_code = EXIT_INTERRUPTED
        raise
    finally:
        closed = await asyncio.shield(session.close())
        if closed is not None and result_exit_code(closed) != EXIT_SUCCESS:
            should_emit = exit_code == EXIT_SUCCESS
            exit_code = result_exit_code(closed)
            if should_emit and closed.outcome.status is not AgentOutcomeStatus.SUCCEEDED:
                emit_result(closed, json_output=json_output)
        await asyncio.shield(application.close())
    return exit_code


async def read_stdin_line(prompt: str) -> str:
    """Read one line without leaving an uncancellable executor thread on interruption."""

    loop = asyncio.get_running_loop()
    result = loop.create_future()

    def ready() -> None:
        try:
            line = sys.stdin.readline()
        except BaseException as exc:
            if not result.done():
                result.set_exception(exc)
            return
        if not result.done():
            if line == "":
                result.set_exception(EOFError())
            else:
                result.set_result(line.rstrip("\r\n"))

    try:
        loop.add_reader(sys.stdin.fileno(), ready)
    except (AttributeError, NotImplementedError):
        return input(prompt)
    if prompt:
        print(prompt, end="", flush=True)
    try:
        return await result
    finally:
        loop.remove_reader(sys.stdin.fileno())


def emit_result(result: ExecutionResult, *, json_output: bool) -> None:
    outcome = result.outcome
    payload = {
        "status": outcome.status.value,
        "run_id": str(result.run_id),
        "persisted": result.persisted,
        "final": outcome.final_text,
        "error": None if outcome.error is None else outcome.error.model_dump(mode="json"),
    }
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
        return
    if outcome.status is AgentOutcomeStatus.SUCCEEDED:
        print(outcome.final_text)
        print(f"Run: {result.run_id}")
    else:
        error = outcome.error or ErrorInfo(
            code=ErrorCode.VALIDATION_FAILED,
            message="Execution failed",
        )
        print(f"error[{error.code.value}]: {error.message}", file=sys.stderr)
        print(f"Run: {result.run_id}", file=sys.stderr)


def emit_workspace(result: WorkspaceInitialization, *, json_output: bool) -> None:
    payload = {
        "status": "initialized",
        "created_root": result.created_root,
        "created_config": result.created_config,
        "created_secrets": result.created_secrets,
    }
    if json_output:
        print(json.dumps(payload, separators=(",", ":")))
    else:
        print("Workspace initialized.")


def result_exit_code(result: ExecutionResult) -> int:
    if not result.persisted:
        return EXIT_FAILURE
    if result.outcome.status is AgentOutcomeStatus.SUCCEEDED:
        return EXIT_SUCCESS
    if result.outcome.status is AgentOutcomeStatus.TIMED_OUT:
        return EXIT_TIMEOUT
    if result.outcome.status is AgentOutcomeStatus.CANCELLED:
        return EXIT_INTERRUPTED
    return EXIT_FAILURE


def error_exit_code(error: ErrorInfo) -> int:
    if error.category in {ErrorCategory.CONFIGURATION, ErrorCategory.VALIDATION}:
        return EXIT_USAGE
    if error.code is ErrorCode.MODEL_TIMEOUT or error.category is ErrorCategory.TIMEOUT:
        return EXIT_TIMEOUT
    if error.category is ErrorCategory.INTERRUPTION:
        return EXIT_INTERRUPTED
    return EXIT_FAILURE


def emit_error(code: str, message: str, *, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps(
                {"status": "failed", "error": {"code": code, "message": message}},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
    else:
        print(f"error[{code}]: {message}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    json_output = bool(arguments.json)
    try:
        if arguments.command == "workspace":
            result = initialize_workspace()
            emit_workspace(result, json_output=json_output)
            return EXIT_SUCCESS
        if arguments.command == "run":
            return asyncio.run(execute_run(arguments.task, json_output=json_output))
        return asyncio.run(execute_chat(json_output=json_output))
    except KeyboardInterrupt:
        emit_error("execution_interrupted", "Execution was interrupted", json_output=json_output)
        return EXIT_INTERRUPTED
    except AnbanError as exc:
        emit_error(exc.info.code.value, exc.info.message, json_output=json_output)
        return error_exit_code(exc.info)
    except WorkspaceResolutionError as exc:
        emit_error(exc.code, str(exc), json_output=json_output)
        return EXIT_USAGE
    except (ValidationError, ValueError):
        emit_error("validation_failed", "Input validation failed", json_output=json_output)
        return EXIT_USAGE
    except Exception:
        emit_error("execution_failed", "Execution failed", json_output=json_output)
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
