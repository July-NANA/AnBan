"""Independent worker retaining real Process ownership across service exit."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from anban.capability.process_background import BackgroundWorkerRequest
from anban.capability.workspace import WorkspaceBoundary
from anban.core.errors import AnbanError

_POLL_SECONDS = 0.05


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


async def _run(directory: Path) -> int:
    from anban.capability.process import ProcessCapability

    try:
        request = BackgroundWorkerRequest.model_validate_json(
            await asyncio.to_thread(sys.stdin.buffer.read), strict=True
        )
        arguments = request.arguments
        context = request.context
        workspace_root = Path(request.workspace_root)
        protected = request.protected_values
        settings = request.settings
    except (ValueError, ValidationError):
        return 2

    capability = ProcessCapability(
        WorkspaceBoundary(workspace_root),
        protected_values=protected,
        default_timeout_seconds=settings.default_timeout_seconds,
        max_timeout_seconds=settings.max_timeout_seconds,
        stdout_max_bytes=settings.stdout_max_bytes,
        stderr_max_bytes=settings.stderr_max_bytes,
        stdin_max_bytes=settings.stdin_max_bytes,
        max_arguments=settings.max_arguments,
        max_artifacts=settings.max_artifacts,
        artifact_max_bytes=settings.artifact_max_bytes,
    )
    readiness = asyncio.get_running_loop().create_future()
    execution = asyncio.create_task(capability.execute_supervised(arguments, context, readiness))
    ready = asyncio.ensure_future(asyncio.shield(readiness))
    done, _ = await asyncio.wait((execution, ready), return_when=asyncio.FIRST_COMPLETED)
    if ready in done and not ready.cancelled() and ready.exception() is None:
        _atomic_write(
            directory / "started.json",
            json.dumps(
                {"version": 1, "worker_pid": os.getpid()},
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )
    else:
        ready.cancel()
        await asyncio.gather(ready, return_exceptions=True)

    cancel = directory / "cancel"
    while not execution.done():
        if cancel.is_file():
            await capability.cancel(context)
        await asyncio.sleep(_POLL_SECONDS)
    try:
        result = await execution
    except AnbanError as exc:
        _atomic_write(
            directory / "error.json",
            exc.info.model_dump_json(exclude_computed_fields=True),
        )
        return 0
    except Exception:
        return 3
    _atomic_write(
        directory / "result.json",
        result.model_dump_json(exclude_computed_fields=True),
    )
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    directory = Path(sys.argv[1])
    try:
        if not directory.is_dir():
            return 2
        return asyncio.run(_run(directory))
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
