"""Pure validation tests; no fake provider or simulated execution success."""

import os
import tomllib
from pathlib import Path, PureWindowsPath

import pytest

from scripts.check_real_model import ReadinessError, validate_tool_arguments
from scripts.readiness import (
    check_ci_files,
    check_workspace,
    environment_contract_valid,
    load_workspace_config,
    python_environment_result,
)
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


def environment_file(path: Path, *, name: str = "anban", python: str = "3.12") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"name: {name}\nchannels:\n  - conda-forge\ndependencies:\n  - python={python}\n  - uv\n",
        encoding="utf-8",
    )
    return path


def python_result(
    tmp_path: Path,
    *,
    environment_name: str = "anban",
    version: tuple[int, int] = (3, 12),
    prefix_name: str = "miniconda-installation",
    executable_inside_prefix: bool = True,
    include_prefix: bool = True,
    uv_version: str | None = "uv 0.11.29",
):
    prefix = tmp_path / prefix_name / "envs" / "anban"
    executable = prefix / "bin" / "python"
    if not executable_inside_prefix:
        executable = tmp_path / "other" / "bin" / "python"
    environ = {"CONDA_DEFAULT_ENV": environment_name}
    if include_prefix:
        environ["CONDA_PREFIX"] = str(prefix)
    return python_environment_result(
        environ,
        version,
        executable,
        uv_version,
        environment_file(tmp_path / "environment.yml"),
    )


def test_tool_arguments_require_exact_closed_schema() -> None:
    assert validate_tool_arguments('{"filename":"validation.txt","content":"nonce"}', "nonce") == {
        "filename": "validation.txt",
        "content": "nonce",
    }


def test_tool_arguments_reject_additional_properties() -> None:
    with pytest.raises(ReadinessError, match="closed argument schema"):
        validate_tool_arguments(
            '{"filename":"validation.txt","content":"nonce","extra":true}', "nonce"
        )


def test_workspace_configuration_is_read_as_toml(tmp_path: Path) -> None:
    (tmp_path / "anban.toml").write_text(
        'schema_version = 1\nworkspace_id = "local-main"\n', encoding="utf-8"
    )

    assert load_workspace_config(tmp_path) == {
        "schema_version": 1,
        "workspace_id": "local-main",
    }


def test_invalid_workspace_configuration_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "anban.toml").write_text("not = [valid", encoding="utf-8")

    with pytest.raises(tomllib.TOMLDecodeError):
        load_workspace_config(tmp_path)


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


def test_windows_default_uses_local_app_data(tmp_path: Path) -> None:
    value = default_workspace_value(
        "win32", {"LOCALAPPDATA": r"C:\Users\Ada\AppData\Local"}, tmp_path
    )

    assert PureWindowsPath(value) == PureWindowsPath(r"C:\Users\Ada\AppData\Local\Anban\workspace")


def test_windows_default_requires_local_app_data(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceResolutionError, match="LOCALAPPDATA"):
        default_workspace_value("win32", {}, tmp_path)


def test_unknown_platform_fails_without_repository_fallback(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    with pytest.raises(WorkspaceResolutionError, match="No reliable Workspace default"):
        resolve_workspace(repository=repository, environ={}, platform="unknown", home=tmp_path)


def test_dotenv_shell_content_is_not_executed(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    marker = tmp_path / "must-not-exist"
    (repository / ".env").write_text(f"ANBAN_WORKSPACE_DIR=$(touch {marker})\n", encoding="utf-8")

    with pytest.raises(WorkspaceResolutionError):
        resolve_workspace(repository=repository, environ={}, platform="darwin", home=tmp_path)

    assert not marker.exists()


def test_workspace_bootstrap_value_is_not_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    workspace = tmp_path / "private-bootstrap-value"

    resolve_workspace(
        repository=repository,
        environ={"ANBAN_WORKSPACE_DIR": str(workspace)},
        platform="darwin",
        home=tmp_path,
    )

    assert str(workspace) not in capsys.readouterr().out


@pytest.mark.parametrize("value", ["", "relative/workspace"])
def test_workspace_rejects_empty_or_relative_bootstrap(tmp_path: Path, value: str) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    with pytest.raises(WorkspaceResolutionError):
        resolve_workspace(
            repository=repository,
            environ={"ANBAN_WORKSPACE_DIR": value},
            platform="darwin",
            home=tmp_path,
        )


def test_workspace_rejects_root_home_repository_and_repository_children() -> None:
    repository = Path(__file__).resolve().parents[1]

    for unsafe in (Path("/"), Path.home(), repository, repository / "local-workspace"):
        result = check_workspace(unsafe)
        assert result.status == "FAIL"
        assert result.code == "workspace_path_unsafe"


def test_runner_temp_workspace_passes_portable_rules(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path / "runner-temp-workspace")

    result = check_workspace(workspace)

    assert result.status == "PASS"
    assert str(workspace) not in result.detail


def test_workspace_permissions_remain_required(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path / "workspace")
    os.chmod(workspace, 0o755)

    result = check_workspace(workspace)

    assert result.code == "workspace_permissions_invalid"


def test_secret_permissions_remain_required(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path / "workspace")
    os.chmod(workspace / "secrets.env", 0o644)

    result = check_workspace(workspace)

    assert result.code == "workspace_secret_permissions_invalid"


def test_primary_workstation_path_resolves_through_configuration() -> None:
    result = resolve_workspace(
        environ={"ANBAN_WORKSPACE_DIR": "/Users/fanyuhang/AnbanWorkspace"},
        platform="darwin",
    )

    assert result.path == Path("/Users/fanyuhang/AnbanWorkspace")


def test_python_environment_accepts_non_miniforge_directory_name(tmp_path: Path) -> None:
    result = python_result(tmp_path, prefix_name="arbitrary-conda-root")

    assert result.status == "PASS"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"environment_name": "base"}, "miniforge_environment_invalid"),
        ({"version": (3, 11)}, "miniforge_python_version_invalid"),
        ({"include_prefix": False}, "miniforge_prefix_missing"),
        ({"executable_inside_prefix": False}, "miniforge_interpreter_invalid"),
        ({"uv_version": None}, "miniforge_uv_unavailable"),
    ],
)
def test_python_environment_rejects_invalid_facts(
    tmp_path: Path, overrides: dict[str, object], code: str
) -> None:
    result = python_result(tmp_path, **overrides)  # type: ignore[arg-type]

    assert result.status == "FAIL"
    assert result.code == code


def test_environment_yml_requires_anban_and_python_312(tmp_path: Path) -> None:
    assert environment_contract_valid(environment_file(tmp_path / "valid.yml"))
    assert not environment_contract_valid(
        environment_file(tmp_path / "wrong-name.yml", name="other")
    )
    assert not environment_contract_valid(
        environment_file(tmp_path / "wrong-python.yml", python="3.11")
    )


def test_local_and_github_runner_paths_use_the_same_python_rules(tmp_path: Path) -> None:
    local = python_result(tmp_path / "local", prefix_name="miniforge3")
    runner = python_result(tmp_path / "runner", prefix_name="miniconda3")

    assert local.status == "PASS"
    assert runner.status == "PASS"


def test_ci_workflows_keep_setup_miniconda_miniforge_configuration() -> None:
    assert check_ci_files().status == "PASS"
