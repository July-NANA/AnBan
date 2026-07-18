"""Model Port decorator that records safe facts for one persisted Run."""

from __future__ import annotations

from anban.core.errors import AnbanError, ErrorCode, ErrorInfo
from anban.model import ModelPort, ModelRequest, ModelTurn
from anban.runtime.persistence import RunPersistence


class PersistedModelPort:
    """Record safe model facts without retaining requests or provider responses."""

    def __init__(self, inner: ModelPort, persistence: RunPersistence) -> None:
        self._inner = inner
        self._persistence = persistence
        self._turn_number = 0

    @property
    def turn_count(self) -> int:
        return self._turn_number

    async def complete(self, request: ModelRequest) -> ModelTurn:
        self._turn_number += 1
        turn_number = self._turn_number
        await self._persistence.model_requested(turn_number, request)
        try:
            turn = await self._inner.complete(request)
        except AnbanError as exc:
            await self._persistence.model_failed(turn_number, request, exc.info)
            raise
        except Exception:
            error = ErrorInfo(
                code=ErrorCode.MODEL_REQUEST_FAILED,
                message="Model request failed",
            )
            await self._persistence.model_failed(turn_number, request, error)
            raise AnbanError(error) from None
        await self._persistence.model_completed(turn_number, request, turn)
        return turn
