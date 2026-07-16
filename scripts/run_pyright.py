"""Run Pyright against the currently executing Miniforge interpreter."""

from __future__ import annotations

import subprocess
import sys


def main() -> int:
    completed = subprocess.run(["pyright", "--pythonpath", sys.executable], check=False)
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
