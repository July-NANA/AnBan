"""Run Pyright against the currently executing Python interpreter."""

from __future__ import annotations

import subprocess
import sys


def main() -> int:
    completed = subprocess.run(
        [sys.executable, "-m", "pyright", "--pythonpath", sys.executable], check=False
    )
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
