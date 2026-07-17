"""Read-only v0.1 release-candidate closure checks for the exact pushed SHA."""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from scripts.workspace_bootstrap import REPOSITORY

EXPECTED_VERSION = "0.1.0"
MAX_COMMAND_OUTPUT = 65_536
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
HOST_PATH_PATTERN = re.compile(r"/(?:Users|home)/[^\s`]+|[A-Za-z]:\\\\")
DOCUMENTS = (
    "README.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "docs/architecture/overview.md",
    "docs/architecture/workspace.md",
    "docs/cli.md",
    "docs/releases/v0.1.0.md",
    "scripts/acceptance/README.md",
)
CLI_HELP = (
    ("--help",),
    ("workspace", "init", "--help"),
    ("run", "--help"),
    ("run", "show", "--help"),
    ("chat", "--help"),
    ("runs", "--help"),
    ("trace", "--help"),
    ("artifacts", "--help"),
)


class ReleaseClosureError(RuntimeError):
    """Safe failure without subprocess output, configuration, or physical paths."""


def command(*arguments: str, environment: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        arguments,
        cwd=REPOSITORY,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = result.stdout + result.stderr
    if result.returncode != 0 or len(output.encode()) > MAX_COMMAND_OUTPUT:
        raise ReleaseClosureError("release closure command failed")
    return output


def git_facts() -> str:
    branch = command("git", "branch", "--show-current").strip()
    status = command("git", "status", "--porcelain")
    head = command("git", "rev-parse", "HEAD").strip()
    remote = command("git", "rev-parse", "origin/anban").strip()
    if branch != "anban" or status or head != remote or not SHA_PATTERN.fullmatch(head):
        raise ReleaseClosureError("anban is not clean and synchronized")
    return head


def check_repository_surface() -> None:
    for relative in DOCUMENTS:
        if not (REPOSITORY / relative).is_file():
            raise ReleaseClosureError("required release documentation is missing")
    tracked = command("git", "ls-files").splitlines()
    if any(
        Path(path).name in {".env", "secrets.env"} or path.startswith(("artifacts/", "runs/"))
        for path in tracked
    ):
        raise ReleaseClosureError("a protected configuration file is tracked")
    for relative in DOCUMENTS:
        text = (REPOSITORY / relative).read_text(encoding="utf-8")
        if HOST_PATH_PATTERN.search(text):
            raise ReleaseClosureError("release documentation contains a physical host path")


def check_cli_and_migrations() -> None:
    for arguments in CLI_HELP:
        command(sys.executable, "-m", "anban.cli", *arguments)
    for profile in ("development", "test"):
        environment = dict(os.environ)
        environment["ANBAN_DATABASE_PROFILE"] = profile
        if "(head)" not in command(
            sys.executable, "-m", "alembic", "current", environment=environment
        ):
            raise ReleaseClosureError("a PostgreSQL migration profile is not at head")


def installed_version() -> str:
    try:
        installed = version("anban")
    except PackageNotFoundError:
        raise ReleaseClosureError("anban package is not installed") from None
    if installed != EXPECTED_VERSION:
        raise ReleaseClosureError("anban package version is not the release candidate")
    try:
        import anban

        source = Path(anban.__file__ or "").resolve()
    except (ImportError, OSError):
        raise ReleaseClosureError("anban package cannot be imported") from None
    if not source.is_relative_to(REPOSITORY.resolve()):
        raise ReleaseClosureError("anban package does not correspond to this checkout")
    return installed


def main() -> int:
    try:
        if sys.version_info[:2] != (3, 12):
            raise ReleaseClosureError("Python 3.12 is required")
        sha = git_facts()
        check_repository_surface()
        check_cli_and_migrations()
        package_version = installed_version()
    except ReleaseClosureError:
        print("v0.1 release closure: FAIL [acceptance_invalid]", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"v0.1 release closure: FAIL ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(
        "v0.1 release closure: PASS - clean synced branch, package, CLI, migrations, docs, "
        "protected files"
    )
    print(
        f"v0.1 release evidence: sha={sha} package={package_version} "
        f"python={platform.python_version()} platform={sys.platform} interpreter=current"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
