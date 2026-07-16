"""Create an ephemeral trusted-CI Workspace without emitting Secret values."""

from __future__ import annotations

import os
import sys
from pathlib import Path

REQUIRED = (
    "ANBAN_WORKSPACE_DIR",
    "DATABASE_URL",
    "ANBAN_TEST_DATABASE_URL",
    "OPENAI_COMPATIBLE_BASE_URL",
    "OPENAI_COMPATIBLE_API_KEY",
    "OPENAI_COMPATIBLE_MODEL",
)

CONFIGURATION = """schema_version = 1
workspace_id = "local-main"

[model.default]
provider = "openai-compatible"
base_url_env = "OPENAI_COMPATIBLE_BASE_URL"
api_key_env = "OPENAI_COMPATIBLE_API_KEY"
model_env = "OPENAI_COMPATIBLE_MODEL"

[database]
url_env = "DATABASE_URL"
test_url_env = "ANBAN_TEST_DATABASE_URL"
"""


def main() -> int:
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        print("trusted readiness BLOCKED: missing Environment settings: " + ", ".join(missing))
        return 2
    values = {name: os.environ[name] for name in REQUIRED}
    if any("\n" in value or "\r" in value for value in values.values()):
        print("trusted readiness FAIL: an Environment setting contains a newline")
        return 1

    workspace = Path(values["ANBAN_WORKSPACE_DIR"]).resolve()
    runner_temp = Path(os.environ.get("RUNNER_TEMP", "/nonexistent-runner-temp")).resolve()
    if workspace == Path("/") or runner_temp not in workspace.parents:
        print("trusted readiness FAIL: Workspace is outside RUNNER_TEMP")
        return 1

    workspace.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(workspace, 0o700)
    for name in ("skills", "runs", "artifacts", "cache", "logs", "tmp"):
        (workspace / name).mkdir(mode=0o700)
    (workspace / "anban.toml").write_text(CONFIGURATION, encoding="utf-8")

    secret_names = REQUIRED[1:]
    secret_content = "\n".join(f"{name}={values[name]}" for name in secret_names) + "\n"
    secret_path = workspace / "secrets.env"
    descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(secret_content)
    os.chmod(secret_path, 0o600)
    print("trusted readiness Workspace created with allowlisted settings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
