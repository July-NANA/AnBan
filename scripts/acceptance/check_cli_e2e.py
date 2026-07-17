"""Real-model Gate A-D acceptance through the production Application composition root."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import http.server
import json
import os
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from anban.application import build_application, build_query_application
from anban.capability.skill import WorkspaceSkillCatalog
from anban.config import AnbanConfiguration, load_configuration
from anban.core.ids import ExecutionRunId, new_interaction_id
from anban.interaction import InteractionEnvelope
from anban.runtime import AgentOutcomeStatus, ExecutionResult, RunObservability
from anban.workspace import default_configuration_text
from scripts.workspace_bootstrap import resolve_workspace


class RuntimeGateError(RuntimeError):
    """Safe Gate failure without model, command, credential, or physical-path output."""


class GateHttpHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"gate-http-ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return None


@contextmanager
def local_http_endpoint() -> Generator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), GateHttpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/validation"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def prepare_workspace(parent: Path, name: str) -> Path:
    root = parent / name
    root.mkdir(mode=0o700, exist_ok=False)
    os.chmod(root, 0o700)
    for directory in ("skills", "runs", "artifacts", "cache", "logs", "tmp", "home"):
        (root / directory).mkdir(mode=0o700)
    (root / "anban.toml").write_text(default_configuration_text(), encoding="utf-8")
    secrets = root / "secrets.env"
    secrets.write_text("", encoding="utf-8")
    os.chmod(secrets, 0o600)
    return root


@contextmanager
def isolated_environment(root: Path, source: AnbanConfiguration) -> Generator[None]:
    model = source.require_model()
    values = {
        "ANBAN_WORKSPACE_DIR": str(root),
        "CLAWHUB_CONFIG_PATH": str(root / "home" / "clawhub-config.json"),
        "OPENAI_COMPATIBLE_BASE_URL": model.base_url.get_secret_value(),
        "OPENAI_COMPATIBLE_API_KEY": model.api_key.get_secret_value(),
        "OPENAI_COMPATIBLE_MODEL": model.model,
        "DATABASE_URL": source.database.require("test"),
        "ANBAN_TEST_DATABASE_URL": source.database.require("test"),
    }
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


async def submit(prompt: str) -> ExecutionResult:
    application = await build_application()
    try:
        return await application.interactions.submit(
            InteractionEnvelope(id=new_interaction_id(), content=prompt)
        )
    finally:
        await application.close()


async def trace(run_id: ExecutionRunId) -> RunObservability:
    application = await build_query_application()
    try:
        return await application.interactions.trace(run_id)
    finally:
        await application.close()


def require_success(result: ExecutionResult, label: str) -> None:
    if (
        not result.persisted
        or result.outcome.status is not AgentOutcomeStatus.SUCCEEDED
        or not result.outcome.final_text
    ):
        raise RuntimeGateError(f"{label} did not complete successfully")


async def gate_a() -> dict[str, object]:
    with local_http_endpoint() as endpoint:
        result = await submit(
            "In this isolated Anban Workspace, perform a real general-runtime validation. Run the "
            "current Python, inspect a Workspace file listing, create then modify and delete a "
            "temporary text file, make one real HTTP GET to the provided deterministic validation "
            f"endpoint {endpoint} using an available command-line or Python program and verify its "
            "gate-http-ok response, demonstrate stdin or an environment override, and generate "
            "gate-a-result.txt and gate-a-summary.json, collecting both together as declared "
            "Artifacts from one successful process execution. Do not claim completion unless "
            "every operation really ran. Once those operations and Artifact collection have "
            "succeeded, stop executing commands and summarize; do not add redundant verification "
            "commands."
        )
    require_success(result, "Gate A")
    observation = await trace(result.run_id)
    capability_events = [
        entry
        for entry in observation.trace
        if entry.event_type == "capability.completed"
        and entry.metadata.root.get("capability_name") == "process.execute"
    ]
    if not observation.complete or observation.inconsistencies or not capability_events:
        raise RuntimeGateError("Gate A Trace is incomplete")
    summary_keys = {
        "command",
        "argument_count",
        "arguments_hash",
        "cwd_scope",
        "duration_ms",
        "exit_code",
        "stdout_size",
        "stderr_size",
        "stdout_hash",
        "stderr_hash",
        "artifact_count",
        "timed_out",
        "cancelled",
    }
    if not any(summary_keys <= set(entry.metadata.root) for entry in capability_events):
        raise RuntimeGateError("Gate A Process summary is incomplete")
    if not any(entry.metadata.root.get("artifact_count") == 2 for entry in capability_events):
        raise RuntimeGateError("Gate A did not collect two Artifacts in one Process invocation")
    application = await build_query_application()
    try:
        artifacts = await application.interactions.artifacts(result.run_id)
        detail = await application.interactions.show_run(result.run_id)
    finally:
        await application.close()
    if (
        len(artifacts) != 2
        or len({artifact.invocation_id for artifact in artifacts}) != 1
        or artifacts[0].invocation_id is None
        or not detail.observability.complete
        or detail.observability.inconsistencies
    ):
        raise RuntimeGateError("Gate A restart query is incomplete")
    return {"run_id": str(result.run_id), "artifact_ids": [str(item.id) for item in artifacts]}


async def gate_recoverable_artifacts() -> dict[str, object]:
    prompts = (
        "在 Workspace 的临时目录生成一份文本说明和一份 JSON 摘要，并把两个结果文件作为 "
        "Artifact 返回。请真实执行并在完成后简要说明结果。",
        "Create two useful, differently formatted result files in a temporary Workspace location "
        "and return both as managed Artifacts. Choose the available program, paths, and filenames "
        "yourself, execute the work for real, and summarize only after both outputs are collected.",
    )
    evidence: list[dict[str, object]] = []
    for index, prompt in enumerate(prompts, start=1):
        result = await submit(prompt)
        require_success(result, f"recoverable Artifact run {index}")
        observation = await trace(result.run_id)
        application = await build_query_application()
        try:
            artifacts = await application.interactions.artifacts(result.run_id)
        finally:
            await application.close()
        if (
            not observation.complete
            or observation.inconsistencies
            or len(artifacts) != 2
            or len({artifact.invocation_id for artifact in artifacts}) != 1
        ):
            raise RuntimeGateError(f"recoverable Artifact run {index} is incomplete")
        evidence.append(
            {
                "run_id": str(result.run_id),
                "artifact_ids": [str(artifact.id) for artifact in artifacts],
            }
        )
    return {"runs": evidence}


def workspace_packages(root: Path) -> tuple[str, ...]:
    empty_package = root / "empty-package-skills"
    empty_package.mkdir(exist_ok=True)
    return tuple(
        package.slug
        for package in WorkspaceSkillCatalog(
            root,
            package_skills_root=empty_package,
        ).discover()
    )


async def gate_bcd(root: Path) -> dict[str, object]:
    if workspace_packages(root):
        raise RuntimeGateError("Gate B Workspace was not initially empty")
    install = await submit(
        "Use the available Skill for ClawHub to search public Skills without logging in. Compare "
        "candidates from no more than three successful, semantically distinct search commands and "
        "explain compatibility "
        "before selection. Then choose and "
        "install one low-risk Skill that needs no credentials, Browser, MCP, database, or special "
        "service and whose real behavior can be verified with ordinary process execution. The "
        "request explicitly authorizes searching and installing exactly one suitable Skill."
    )
    require_success(install, "Gate B")
    install_trace = await trace(install.run_id)
    names = [
        entry.metadata.root.get("capability_name")
        for entry in install_trace.trace
        if entry.event_type in {"capability.completed", "skill.activated"}
    ]
    npx_calls = [
        entry
        for entry in install_trace.trace
        if entry.event_type == "capability.completed"
        and entry.metadata.root.get("capability_name") == "process.execute"
        and entry.metadata.root.get("command") == "npx"
        and entry.metadata.root.get("exit_code") == 0
    ]
    if (
        not install_trace.complete
        or install_trace.inconsistencies
        or "skill.activate" not in names
        or len(npx_calls) < 2
    ):
        raise RuntimeGateError("Gate B did not trace Skill activation, search, and install")
    packages = workspace_packages(root)
    if len(packages) != 1:
        raise RuntimeGateError("Gate B did not install exactly one valid SKILL.md")
    slug = packages[0]
    skill_files = tuple(sorted((root / "skills").rglob("SKILL.md")))
    if len(skill_files) != 1:
        raise RuntimeGateError("Gate B installed Skill files are ambiguous")
    skill_file = skill_files[0]
    skill_path = skill_file.relative_to(root).as_posix()
    content_hash = hashlib.sha256(skill_file.read_bytes()).hexdigest()
    run_ids: list[str] = []
    for index in range(3):
        if hashlib.sha256(skill_file.read_bytes()).hexdigest() != content_hash:
            raise RuntimeGateError("Gate C/D Skill content changed before execution")
        result = await submit(
            f"Use the discovered Skill {slug} for a fresh low-risk validation task number "
            f"{index + 1}. Follow its actual instructions, use real process execution, and report "
            "a verifiable result without inventing unavailable capabilities."
        )
        require_success(result, f"Gate C/D run {index + 1}")
        observation = await trace(result.run_id)
        event_names = [
            entry.metadata.root.get("capability_name")
            for entry in observation.trace
            if entry.event_type in {"capability.completed", "skill.activated"}
        ]
        if (
            not observation.complete
            or observation.inconsistencies
            or "skill.activate" not in event_names
            or "process.execute" not in event_names
        ):
            raise RuntimeGateError("Gate C/D Trace is incomplete")
        if hashlib.sha256(skill_file.read_bytes()).hexdigest() != content_hash:
            raise RuntimeGateError("Gate C/D Skill content changed during execution")
        run_ids.append(str(result.run_id))
    return {
        "install_run_id": str(install.run_id),
        "slug": slug,
        "skill_path": skill_path,
        "content_hash": content_hash,
        "execution_run_ids": run_ids,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--gate-a-only", action="store_true")
    mode.add_argument("--gate-bcd-only", action="store_true")
    return result


async def accept_runtime(gate_a_only: bool, gate_bcd_only: bool) -> dict[str, object]:
    source = load_configuration(workspace=resolve_workspace().path)
    parent = source.workspace / "tmp"
    marker = hashlib.sha256(os.urandom(32)).hexdigest()[:12]
    evidence: dict[str, object] = {}
    if not gate_bcd_only:
        gate_a_root = prepare_workspace(parent, f"gate28-a-{marker}")
        with isolated_environment(gate_a_root, source):
            evidence["gate_a"] = await gate_a()
            evidence["recoverable_artifacts"] = await gate_recoverable_artifacts()
    if not gate_a_only:
        gate_b_root = prepare_workspace(parent, f"gate28-b-{marker}")
        with isolated_environment(gate_b_root, source):
            evidence["gate_bcd"] = await gate_bcd(gate_b_root)
    return evidence


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        evidence = asyncio.run(accept_runtime(arguments.gate_a_only, arguments.gate_bcd_only))
    except RuntimeGateError as exc:
        print(f"runtime Gate acceptance: FAIL [{exc}]", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"runtime Gate acceptance: FAIL ({type(exc).__name__})", file=sys.stderr)
        return 1
    print("runtime Gate acceptance: PASS " + json.dumps(evidence, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
