"""Pure doctor logic tests; no real integration acceptance is claimed here."""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath

import pytest
from sqlalchemy.sql.elements import TextClause

import scripts.doctor as doctor
from anban.config import load_configuration
from anban.core import AnbanError
from anban.workspace import default_configuration_text
from scripts.workspace_bootstrap import (
    WorkspaceResolutionError,
    default_workspace_value,
    resolve_workspace,
)


def create_workspace(path: Path) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    for name in ("skills", "runs", "artifacts", "cache", "logs", "tmp"):
        (path / name).mkdir(mode=0o700)
    (path / "anban.toml").write_text(
        'schema_version = 1\nworkspace_id = "local-main"\n', encoding="utf-8"
    )
    secrets = path / "secrets.env"
    secrets.write_text("", encoding="utf-8")
    os.chmod(secrets, 0o600)
    return path


def python_result(
    *,
    version: tuple[int, int] = (3, 12),
    missing_tool: str | None = None,
    dependencies_available: bool = True,
    package_source_valid: bool = True,
) -> doctor.CheckResult:
    tools: dict[str, str | None] = {
        "ruff": "ruff 1",
        "pytest": "pytest 1",
        "pyright": "pyright 1",
    }
    if missing_tool is not None:
        tools[missing_tool] = None
    return doctor.python_environment_result(
        version,
        tools,
        dependencies_available=dependencies_available,
        package_source_valid=package_source_valid,
    )


def test_python_312_current_environment_passes_without_conda() -> None:
    assert python_result().status == "PASS"


def test_python_version_error_fails() -> None:
    assert python_result(version=(3, 11)).code == "python_version_invalid"


def test_missing_project_dependency_fails() -> None:
    assert python_result(dependencies_available=False).code == "python_dependency_unavailable"


def test_missing_current_environment_tool_fails() -> None:
    assert python_result(missing_tool="pyright").code == "python_tool_unavailable"


def test_installed_package_must_match_checkout() -> None:
    assert python_result(package_source_valid=False).code == "anban_package_mismatch"


def test_package_scripts_do_not_force_conda() -> None:
    package = (doctor.REPOSITORY / "package.json").read_text(encoding="utf-8")
    assert "conda run" not in package


def test_workspace_environment_variable_has_highest_priority(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    dotenv_workspace = tmp_path / "dotenv-workspace"
    environment_workspace = tmp_path / "environment-workspace"
    (repository / ".env").write_text(f"ANBAN_WORKSPACE_DIR={dotenv_workspace}\n", encoding="utf-8")

    result = resolve_workspace(
        repository=repository,
        environ={"ANBAN_WORKSPACE_DIR": str(environment_workspace)},
        platform="darwin",
        home=tmp_path,
    )

    assert result.path == environment_workspace
    assert result.source == "environment"


def test_workspace_falls_back_to_repository_dotenv(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    workspace = tmp_path / "dotenv-workspace"
    (repository / ".env").write_text(f"ANBAN_WORKSPACE_DIR={workspace}\n", encoding="utf-8")

    result = resolve_workspace(repository=repository, environ={}, platform="darwin", home=tmp_path)

    assert result.path == workspace
    assert result.source == "repository .env"


@pytest.mark.parametrize(
    ("platform", "environ", "expected"),
    [
        ("darwin", {}, "Library/Application Support/Anban/workspace"),
        ("linux", {}, ".local/share/anban/workspace"),
        ("linux", {"XDG_DATA_HOME": "/var/lib/user-data"}, "/var/lib/user-data/anban/workspace"),
    ],
)
def test_workspace_uses_operating_system_default(
    tmp_path: Path, platform: str, environ: dict[str, str], expected: str
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    result = resolve_workspace(
        repository=repository, environ=environ, platform=platform, home=tmp_path
    )
    expected_path = Path(expected)
    if not expected_path.is_absolute():
        expected_path = tmp_path / expected_path
    assert result.path == expected_path.resolve()
    assert result.source == "operating-system default"


def test_workspace_inside_repository_fails() -> None:
    result = doctor.check_workspace(doctor.REPOSITORY / "local-workspace")
    assert result.code == "workspace_path_unsafe"


def test_workspace_permissions_error_fails(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path / "workspace")
    os.chmod(workspace, 0o755)
    assert doctor.check_workspace(workspace).code == "workspace_permissions_invalid"


def test_secrets_permissions_error_fails(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path / "workspace")
    os.chmod(workspace / "secrets.env", 0o644)
    assert doctor.check_workspace(workspace).code == "workspace_secret_permissions_invalid"


def test_configuration_presence_only_outputs_configured(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path / "workspace")
    secret_value = "must-never-appear"
    (workspace / "anban.toml").write_text(default_configuration_text(), encoding="utf-8")
    configuration = load_configuration(
        workspace=workspace,
        environ={
            "OPENAI_COMPATIBLE_BASE_URL": "https://provider.invalid/v1",
            "OPENAI_COMPATIBLE_API_KEY": secret_value,
            "OPENAI_COMPATIBLE_MODEL": "test-model",
        },
    )
    presence = doctor.configuration_presence(configuration)
    result = next(
        item
        for item in doctor.configuration_results(presence)
        if item.name.endswith("OPENAI_COMPATIBLE_API_KEY")
    )
    output = "\n".join(doctor.result_lines(result))
    assert result.detail == "configured"
    assert secret_value not in output


def test_missing_configuration_only_names_field() -> None:
    result = doctor.configuration_results({})[0]
    output = "\n".join(doctor.result_lines(result))
    assert result.name == "configuration DATABASE_URL"
    assert result.detail == "missing"
    assert "value" not in output.lower()


class ProbeResult:
    def __init__(self, *, row: tuple[str, str] | None = None, scalar: object = None) -> None:
        self.row = row
        self.scalar = scalar

    def one(self) -> tuple[str, str]:
        assert self.row is not None
        return self.row

    def scalar_one(self) -> object:
        return self.scalar


class ProbeTransaction:
    def __init__(self) -> None:
        self.rolled_back = False

    async def rollback(self) -> None:
        self.rolled_back = True


class ProbeConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.transaction = ProbeTransaction()

    async def execute(self, statement: TextClause) -> ProbeResult:
        query = str(statement)
        self.queries.append(query)
        if "current_database()" in query:
            return ProbeResult(row=("custom_database", "custom_user"))
        if "alembic_version" in query:
            return ProbeResult(scalar="head-1")
        if "SELECT value" in query:
            return ProbeResult(scalar="ok")
        if "to_regclass" in query:
            return ProbeResult(scalar=None)
        if "information_schema.tables" in query:
            return ProbeResult(scalar=37)
        return ProbeResult()

    async def rollback(self) -> None:
        return None

    async def begin(self) -> ProbeTransaction:
        return self.transaction


class ProbeConnectContext:
    def __init__(self, connection: ProbeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> ProbeConnection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class ProbeEngine:
    def __init__(self, connection: ProbeConnection) -> None:
        self.connection = connection

    def connect(self) -> ProbeConnectContext:
        return ProbeConnectContext(self.connection)

    async def dispose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_database_with_business_tables_passes_and_transaction_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = ProbeConnection()
    engine = ProbeEngine(connection)

    def probe_create_async_engine(_url: str, *, echo: bool, pool_pre_ping: bool) -> ProbeEngine:
        assert echo is False
        assert pool_pre_ping is True
        return engine

    monkeypatch.setattr(doctor, "create_async_engine", probe_create_async_engine)

    await doctor.database_probe("postgresql+asyncpg://not-logged", "head-1")

    assert any("information_schema.tables" in query for query in connection.queries)
    assert connection.transaction.rolled_back
    assert any("to_regclass" in query for query in connection.queries)


def test_skill_discovery_uses_uniform_parser_without_install_metadata(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path / "workspace")
    (workspace / "anban.toml").write_text(default_configuration_text(), encoding="utf-8")
    skill = workspace / "skills" / "@owner" / "example" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: example\ndescription: Example Skill.\n---\n\nRun a real command.\n",
        encoding="utf-8",
    )
    metadata = workspace / ".clawhub" / "lock.json"
    metadata.parent.mkdir()
    metadata.write_text("not json", encoding="utf-8")
    configuration = load_configuration(workspace=workspace, environ={})

    result = doctor.check_skills(workspace, configuration)

    assert result.status == "PASS"
    assert "valid=2" in result.detail


def test_doctor_call_graph_excludes_real_acceptance_network_and_duplicate_checks() -> None:
    source = Path(doctor.__file__).read_text(encoding="utf-8")
    forbidden = (
        "OpenAI(",
        "check_real_model",
        "wttr.in",
        "open-meteo",
        '"pnpm", "install"',
        '"pnpm", "build"',
        '"pnpm", "check"',
        "real-readiness.yml",
        "check_ci_files",
    )
    assert all(token not in source for token in forbidden)
    assert "if arguments.online:" in source
    assert "if arguments.web:" in source
    base_section = source[source.index("if configuration is not None:") :]
    assert base_section.index("if arguments.online:") > base_section.index("check_process")


def test_doctor_options_keep_online_and_web_checks_out_of_base_mode() -> None:
    parsed = doctor.parser().parse_args([])
    assert parsed.online is False
    assert parsed.web is False
    assert doctor.parser().parse_args(["--online"]).online is True
    assert doctor.parser().parse_args(["--web"]).web is True


def test_doctor_repository_check_ignores_branch_and_worktree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_git(*_args: str, **_kwargs: object) -> str:
        raise AssertionError("doctor must not inspect Git state")

    monkeypatch.setattr(doctor, "command", forbidden_git)
    assert doctor.check_repository().status == "PASS"


def test_invalid_workspace_configuration_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "anban.toml").write_text("not = [valid", encoding="utf-8")
    (tmp_path / "secrets.env").write_text("", encoding="utf-8")
    with pytest.raises(AnbanError):
        load_configuration(workspace=tmp_path, environ={})


def test_dotenv_shell_content_is_not_executed(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    marker = tmp_path / "must-not-exist"
    (repository / ".env").write_text(f"ANBAN_WORKSPACE_DIR=$(touch {marker})\n", encoding="utf-8")
    with pytest.raises(WorkspaceResolutionError):
        resolve_workspace(repository=repository, environ={}, platform="darwin", home=tmp_path)
    assert not marker.exists()


def test_windows_default_uses_local_app_data(tmp_path: Path) -> None:
    value = default_workspace_value(
        "win32", {"LOCALAPPDATA": r"C:\Users\Ada\AppData\Local"}, tmp_path
    )
    assert PureWindowsPath(value) == PureWindowsPath(r"C:\Users\Ada\AppData\Local\Anban\workspace")


def test_unknown_platform_fails_without_repository_fallback(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    with pytest.raises(WorkspaceResolutionError, match="No reliable Workspace default"):
        resolve_workspace(repository=repository, environ={}, platform="unknown", home=tmp_path)


def test_node_package_manager_requires_matching_pnpm() -> None:
    assert doctor.node_environment_result("v24.0.0", "10.19.0", "pnpm@10.19.0").status == "PASS"
    assert (
        doctor.node_environment_result("v24.0.0", "9.0.0", "pnpm@10.19.0").code
        == "pnpm_version_incompatible"
    )


def test_configuration_key_set_is_complete() -> None:
    assert set(doctor.CONFIGURATION_KEYS) == {
        "DATABASE_URL",
        "ANBAN_TEST_DATABASE_URL",
        "OPENAI_COMPATIBLE_BASE_URL",
        "OPENAI_COMPATIBLE_API_KEY",
        "OPENAI_COMPATIBLE_MODEL",
    }
