"""CLI-only Interaction Adapter for Anban v0.1."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from anban.application import (
    Application,
    build_application,
    build_inventory_application,
    build_query_application,
)
from anban.core.errors import AnbanError, ErrorCategory, ErrorCode, ErrorInfo
from anban.core.ids import ExecutionRunId, SessionId, TaskId, new_interaction_id
from anban.interaction import InteractionEnvelope
from anban.runtime import (
    AgentOutcomeStatus,
    ArtifactDetail,
    ContextDetail,
    ExecutionResult,
    RunDetail,
    RunObservability,
    RunSummary,
)
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
    run.add_argument("values", nargs="+")
    add_json_option(run)
    chat = commands.add_parser("chat", help="Start one bounded temporary chat.")
    add_json_option(chat)
    runs = commands.add_parser("runs", help="List durable Runs.")
    runs.add_argument("--limit", type=int, default=20)
    add_json_option(runs)
    trace = commands.add_parser("trace", help="Show one ordered Run Trace.")
    trace.add_argument("run_id")
    add_json_option(trace)
    artifacts = commands.add_parser("artifacts", help="List logical Run Artifacts.")
    artifacts.add_argument("run_id")
    add_json_option(artifacts)
    context = commands.add_parser("context", help="Inspect bounded durable Context.")
    context_commands = context.add_subparsers(dest="context_scope", required=True)
    for scope in ("task", "session"):
        context_scope = context_commands.add_parser(scope, help=f"Inspect {scope} Context.")
        context_scope.add_argument("identity")
        add_json_option(context_scope)
    capabilities = commands.add_parser("capabilities", help="Inspect available Agent paths.")
    capability_commands = capabilities.add_subparsers(dest="capability_command", required=True)
    capability_list = capability_commands.add_parser("list", help="Show the inventory snapshot.")
    add_json_option(capability_list)
    capability_search = capability_commands.add_parser(
        "search", help="Search the bounded inventory."
    )
    capability_search.add_argument("text", nargs="?")
    capability_search.add_argument("--kind", action="append", default=[])
    capability_search.add_argument("--available-only", action="store_true")
    capability_search.add_argument("--limit", type=int, default=32)
    add_json_option(capability_search)
    capability_describe = capability_commands.add_parser(
        "describe", help="Describe one exact inventory key."
    )
    capability_describe.add_argument("key")
    add_json_option(capability_describe)
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
    emit_session(session.session_id, json_output=json_output)
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


async def list_runs(limit: int, *, json_output: bool) -> int:
    application = await build_query_application()
    try:
        runs = await application.interactions.runs(limit)
    finally:
        await application.close()
    emit_runs(runs, json_output=json_output)
    return EXIT_SUCCESS


async def show_run(run_id: ExecutionRunId, *, json_output: bool) -> int:
    application = await build_query_application()
    try:
        detail = await application.interactions.show_run(run_id)
    finally:
        await application.close()
    emit_run_detail(detail, json_output=json_output)
    return EXIT_SUCCESS


async def show_trace(run_id: ExecutionRunId, *, json_output: bool) -> int:
    application = await build_query_application()
    try:
        trace = await application.interactions.trace(run_id)
    finally:
        await application.close()
    emit_trace(trace, json_output=json_output)
    return EXIT_SUCCESS


async def list_artifacts(run_id: ExecutionRunId, *, json_output: bool) -> int:
    application = await build_query_application()
    try:
        artifacts = await application.interactions.artifacts(run_id)
    finally:
        await application.close()
    emit_artifacts(artifacts, json_output=json_output)
    return EXIT_SUCCESS


async def show_context(scope: str, identity: TaskId | SessionId, *, json_output: bool) -> int:
    application = await build_query_application()
    try:
        detail = (
            await application.interactions.task_context(TaskId(identity))
            if scope == "task"
            else await application.interactions.session_context(SessionId(identity))
        )
    finally:
        await application.close()
    emit_context(detail, json_output=json_output)
    return EXIT_SUCCESS


async def inspect_capabilities(arguments: argparse.Namespace, *, json_output: bool) -> int:
    application = build_inventory_application()
    try:
        if arguments.capability_command == "list":
            snapshot = application.snapshot()
            emit_inventory_snapshot(snapshot, json_output=json_output)
        elif arguments.capability_command == "search":
            items = application.search(
                text=arguments.text,
                kinds=tuple(arguments.kind),
                include_unavailable=not arguments.available_only,
                limit=arguments.limit,
            )
            emit_inventory_items(items, json_output=json_output)
        else:
            emit_inventory_item(application.describe(arguments.key), json_output=json_output)
    finally:
        await application.close()
    return EXIT_SUCCESS


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


def emit_session(session_id: SessionId, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"session_id": str(session_id)}, separators=(",", ":")))
    else:
        print(f"Session: {session_id}")


def emit_runs(runs: tuple[RunSummary, ...], *, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps(
                [run.model_dump(mode="json") for run in runs],
                separators=(",", ":"),
            )
        )
        return
    print("RUN ID                                STATUS      CREATED")
    for run in runs:
        print(f"{run.id}  {run.status.value:<10}  {run.created_at.isoformat()}")


def emit_run_detail(detail: RunDetail, *, json_output: bool) -> None:
    if json_output:
        print(detail.model_dump_json())
        return
    print(f"Run: {detail.run.id}")
    print(f"Task: {detail.task.id} [{detail.task.status.value}]")
    print(f"Status: {detail.run.status.value}")
    print(f"Created: {detail.run.created_at.isoformat()}")
    if detail.graph_revision is not None:
        print(
            f"Graph revision: {detail.graph_revision.id} "
            f"[{detail.graph_revision.status.value}] {detail.graph_revision.spec_hash}"
        )
    if detail.final_text is not None:
        print(f"Final: {detail.final_text}")
    print("Nodes:")
    for node in detail.nodes:
        print(f"  {node.id}  {node.node_name}  {node.status.value}")
    print("Invocations:")
    for invocation in detail.invocations:
        print(f"  {invocation.id}  {invocation.capability_name}  {invocation.status.value}")
    print(f"Artifacts: {len(detail.artifacts)}")
    print(f"Trace complete: {str(detail.observability.complete).lower()}")


def emit_trace(trace: RunObservability, *, json_output: bool) -> None:
    payload = {
        "run_id": str(trace.run_id),
        "complete": trace.complete,
        "inconsistencies": trace.inconsistencies,
        "trace": [entry.model_dump(mode="json") for entry in trace.trace],
    }
    if json_output:
        print(json.dumps(payload, separators=(",", ":")))
        return
    print(f"Run: {trace.run_id}")
    print(f"Complete: {str(trace.complete).lower()}")
    for entry in trace.trace:
        correlations = [
            value
            for value in (
                None if entry.node_run_id is None else f"node={entry.node_run_id}",
                None if entry.invocation_id is None else f"invocation={entry.invocation_id}",
                None if entry.artifact_id is None else f"artifact={entry.artifact_id}",
            )
            if value is not None
        ]
        suffix = "" if not correlations else " " + " ".join(correlations)
        print(f"{entry.sequence:04d} {entry.occurred_at.isoformat()} {entry.event_type}{suffix}")
    for inconsistency in trace.inconsistencies:
        print(f"Incomplete: {inconsistency}")


def emit_artifacts(artifacts: tuple[ArtifactDetail, ...], *, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps(
                [artifact.model_dump(mode="json") for artifact in artifacts],
                separators=(",", ":"),
            )
        )
        return
    print("ARTIFACT ID                           SIZE  MEDIA TYPE  LOGICAL URI")
    for artifact in artifacts:
        print(f"{artifact.id}  {artifact.size_bytes}  {artifact.media_type}  {artifact.uri}")


def emit_context(detail: ContextDetail, *, json_output: bool) -> None:
    if json_output:
        print(detail.model_dump_json())
        return
    print(f"Context: {detail.scope.value} {detail.identity}")
    print(f"Active entries: {detail.active_entry_count} ({detail.active_chars} chars)")
    print(f"Stored entries: {len(detail.entries)}")
    print(f"Summaries: {len(detail.summaries)}")
    for summary in detail.summaries:
        print(
            f"  {summary.id}  covers={len(summary.covered_entry_ids)}  "
            f"chars={summary.content_chars}  sha256={summary.content_hash}"
        )


def emit_inventory_snapshot(snapshot: Any, *, json_output: bool) -> None:
    if json_output:
        print(snapshot.model_dump_json())
        return
    print(f"Generated: {snapshot.generated_at.isoformat()}")
    emit_inventory_items(snapshot.items, json_output=False)


def emit_inventory_items(items: Sequence[Any], *, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps(
                [item.model_dump(mode="json") for item in items],
                separators=(",", ":"),
            )
        )
        return
    print("KEY  KIND  AVAILABILITY  NAME")
    for item in items:
        print(f"{item.key}  {item.kind.value}  {item.availability.value}  {item.name}")


def emit_inventory_item(item: Any, *, json_output: bool) -> None:
    if json_output:
        print(item.model_dump_json())
        return
    emit_inventory_items((item,), json_output=False)
    print(f"Description: {item.description}")
    if item.unavailable_reason is not None:
        print(f"Unavailable: {item.unavailable_reason}")


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
            values = list(arguments.values)
            if values[0] == "show":
                if len(values) != 2:
                    raise ValueError("run show requires one Run ID")
                return asyncio.run(show_run(parse_run_id(values[1]), json_output=json_output))
            return asyncio.run(execute_run(" ".join(values), json_output=json_output))
        if arguments.command == "chat":
            return asyncio.run(execute_chat(json_output=json_output))
        if arguments.command == "runs":
            return asyncio.run(list_runs(arguments.limit, json_output=json_output))
        if arguments.command == "trace":
            return asyncio.run(show_trace(parse_run_id(arguments.run_id), json_output=json_output))
        if arguments.command == "capabilities":
            return asyncio.run(inspect_capabilities(arguments, json_output=json_output))
        if arguments.command == "context":
            identity = parse_context_id(arguments.context_scope, arguments.identity)
            return asyncio.run(
                show_context(arguments.context_scope, identity, json_output=json_output)
            )
        return asyncio.run(list_artifacts(parse_run_id(arguments.run_id), json_output=json_output))
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


def parse_run_id(value: str) -> ExecutionRunId:
    return ExecutionRunId(UUID(value))


def parse_context_id(scope: str, value: str) -> TaskId | SessionId:
    identifier = UUID(value)
    return TaskId(identifier) if scope == "task" else SessionId(identifier)


if __name__ == "__main__":
    raise SystemExit(main())
