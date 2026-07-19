"""Real D29 MCP discovery and structured Tool invocation acceptance."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

from anban.application import (
    build_application,
    build_inventory_application,
    build_query_application,
)
from anban.capability import CapabilityInventoryQuery, InventoryKind
from anban.config import load_configuration
from anban.core import AnbanError
from anban.core.ids import new_interaction_id
from anban.interaction import InteractionEnvelope
from anban.runtime import AgentOutcomeStatus
from scripts.acceptance.check_cli_e2e import isolated_environment, prepare_workspace
from scripts.acceptance.check_restart_recovery import cli
from scripts.workspace_bootstrap import resolve_workspace


class McpAcceptanceError(RuntimeError):
    """Safe failure without Provider, protocol, credential, or physical-path output."""


def configure_server(
    workspace: Path,
    tool_name: str,
    state_name: str,
    label_field: str,
    value_field: str,
) -> None:
    fixture = Path(__file__).with_name("mcp_fixture_server.py")
    target = workspace / "mcp_fixture_server.py"
    shutil.copyfile(fixture, target)
    configuration = workspace / "anban.toml"
    text = configuration.read_text(encoding="utf-8")
    text += (
        "\n[[capability.mcp.servers]]\n"
        'name = "acceptance"\n'
        'transport = "stdio"\n'
        f"command = {json.dumps(sys.executable)}\n"
        f"args = [{json.dumps(target.name)}, {json.dumps(tool_name)}, {json.dumps(state_name)}, "
        f'"0", {json.dumps(label_field)}, {json.dumps(value_field)}]\n'
        'cwd = "."\n'
    )
    configuration.write_text(text, encoding="utf-8")


async def inventory_descriptor() -> tuple[str, str]:
    application = await build_inventory_application()
    try:
        matches = application.inventory.search(
            CapabilityInventoryQuery(
                kinds=(InventoryKind.MCP,),
                include_unavailable=False,
                limit=8,
            )
        )
    finally:
        await application.close()
    if len(matches) != 1 or matches[0].version_digest is None:
        raise McpAcceptanceError("real MCP Tool inventory did not reconcile")
    return matches[0].key, matches[0].version_digest


async def run_variant(
    capability_name: str,
    label: str,
    value: int,
    expected_count: int,
    state_name: str,
    label_field: str,
    value_field: str,
) -> dict[str, object]:
    application = await build_application()
    try:
        result = await application.interactions.submit(
            InteractionEnvelope(
                id=new_interaction_id(),
                content=(
                    "Use exactly one dynamically discovered MCP Tool call and no Process or Skill. "
                    f"Invoke the available MCP Tool with {label_field}={json.dumps(label)} "
                    f"and {value_field}={value}. Do not set the optional fail field. "
                    "Truthfully report the real structured result "
                    "after the Tool Result is available."
                ),
            )
        )
    finally:
        await application.close()
    query = await build_query_application()
    try:
        detail = await query.interactions.show_run(result.run_id)
    finally:
        await query.close()
    completed = tuple(
        event for event in detail.observability.audit if event.event_type == "capability.completed"
    )
    state_path = load_configuration().workspace / state_name
    if (
        not result.persisted
        or result.outcome.status is not AgentOutcomeStatus.SUCCEEDED
        or detail.run.status.value != "succeeded"
        or not detail.observability.complete
        or detail.observability.inconsistencies
        or len(detail.invocations) != 1
        or detail.invocations[0].capability_name != capability_name
        or detail.invocations[0].status.value != "succeeded"
        or len(completed) != 1
        or completed[0].metadata.root.get("mcp_server") != "acceptance"
        or completed[0].metadata.root.get("mcp_structured") is not True
        or completed[0].metadata.root.get("mcp_content_count") != 1
        or not isinstance(completed[0].metadata.root.get("mcp_tool_digest"), str)
        or not isinstance(completed[0].metadata.root.get("mcp_protocol_version"), str)
        or not state_path.is_file()
        or state_path.read_text(encoding="utf-8").strip() != str(expected_count)
        or len(detail.artifacts) != 0
    ):
        raise McpAcceptanceError("real MCP Run persistence or Audit did not reconcile")
    return {
        "label": label,
        "run_id": str(result.run_id),
        "invocation_id": str(detail.invocations[0].id),
        "call_count": expected_count,
    }


async def accept_mcp() -> dict[str, object]:
    source = load_configuration(workspace=resolve_workspace().path)
    marker = hashlib.sha256(os.urandom(32)).hexdigest()[:12]
    workspace = prepare_workspace(source.workspace / "tmp", f"d29-mcp-{marker}")
    tool_name = f"structured_operation_{marker}"
    state_name = f"d29-mcp-count-{marker}.txt"
    label_field = f"subject_{marker}"
    value_field = f"amount_{marker}"
    configure_server(workspace, tool_name, state_name, label_field, value_field)
    with isolated_environment(workspace, source):
        capability_name, digest = await inventory_descriptor()
        code, payloads = await cli(
            "capabilities",
            "describe",
            capability_name,
            "--json",
            timeout=60,
        )
        if (
            code != 0
            or len(payloads) != 1
            or payloads[0].get("kind") != "mcp"
            or payloads[0].get("availability") != "ready"
        ):
            raise McpAcceptanceError("CLI did not discover the real MCP Tool")
        variants = (
            (f"alpha object {marker}", 7),
            (f"negative object {marker}", -4),
            (f"summary object {marker}", 19),
        )
        evidence = [
            await run_variant(
                capability_name,
                label,
                value,
                index,
                state_name,
                label_field,
                value_field,
            )
            for index, (label, value) in enumerate(variants, start=1)
        ]
        restarted_name, restarted_digest = await inventory_descriptor()
        if restarted_name != capability_name or restarted_digest != digest:
            raise McpAcceptanceError("MCP discovery identity changed after Application restart")

        configuration = workspace / "anban.toml"
        configured = configuration.read_text(encoding="utf-8")
        args_line = (
            f"args = [{json.dumps('mcp_fixture_server.py')}, {json.dumps(tool_name)}, "
            f'{json.dumps(state_name)}, "0", {json.dumps(label_field)}, '
            f"{json.dumps(value_field)}]"
        )
        malformed_line = 'args = ["-c", "print(\'\\u007bnot-json\', flush=True)"]'
        configuration.write_text(
            configured.replace(args_line, malformed_line, 1),
            encoding="utf-8",
        )
        try:
            await inventory_descriptor()
        except AnbanError as exc:
            malformed = exc.info.details.root.get("reason") == "mcp_transport_unavailable"
        else:
            malformed = False
        if not malformed:
            raise McpAcceptanceError("malformed MCP server response did not fail closed")

        configuration.write_text(
            configured.replace(json.dumps(sys.executable), json.dumps("missing-mcp-command"), 1),
            encoding="utf-8",
        )
        try:
            await inventory_descriptor()
        except AnbanError as exc:
            unavailable = exc.info.details.root.get("reason") == "mcp_transport_unavailable"
        else:
            unavailable = False
        if not unavailable:
            raise McpAcceptanceError("unavailable MCP server did not fail closed")
    return {
        "variants": evidence,
        "discovery_reconnected": True,
        "malformed_server": "rejected",
        "unavailable_server": "rejected",
        "scenarios": ["S01", "S02", "S03", "S04", "S08", "S09", "S10", "S11"],
        "side_effect_replayed": False,
    }


def main() -> int:
    try:
        evidence = asyncio.run(accept_mcp())
    except Exception as exc:
        detail = str(exc) if isinstance(exc, McpAcceptanceError) else "unexpected"
        print(f"MCP acceptance: FAIL ({type(exc).__name__}: {detail})", file=sys.stderr)
        return 1
    print("MCP acceptance: PASS " + json.dumps(evidence, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
