"""Interaction-to-Runtime mapping without Adapter or provider bypasses."""

from __future__ import annotations

from anban.core.metadata import SafeMetadata
from anban.interaction.contracts import InteractionEnvelope
from anban.runtime import ExecutionResult, PersistentChatSession, PersistentRuntime


def interaction_metadata(envelope: InteractionEnvelope) -> SafeMetadata:
    return SafeMetadata(
        {
            "interaction_id": str(envelope.id),
            "source": envelope.source,
        }
    )


class InteractionChatSession:
    """Map bounded CLI envelopes into one Runtime chat session."""

    def __init__(self, session: PersistentChatSession) -> None:
        self._session = session

    @property
    def can_continue(self) -> bool:
        return self._session.can_continue

    @property
    def remaining_seconds(self) -> float:
        return self._session.remaining_seconds

    async def submit(self, envelope: InteractionEnvelope) -> ExecutionResult:
        return await self._session.submit(
            envelope.content,
            metadata=interaction_metadata(envelope),
        )

    async def close(self) -> ExecutionResult | None:
        return await self._session.close()

    async def expire(self) -> ExecutionResult | None:
        return await self._session.expire()

    async def interrupt(self) -> ExecutionResult | None:
        return await self._session.interrupt()


class InteractionService:
    """The only CLI-facing entry into the v0.1 Runtime."""

    def __init__(self, runtime: PersistentRuntime) -> None:
        self._runtime = runtime

    async def submit(self, envelope: InteractionEnvelope) -> ExecutionResult:
        return await self._runtime.execute(
            envelope.content,
            metadata=interaction_metadata(envelope),
        )

    def chat(self) -> InteractionChatSession:
        return InteractionChatSession(self._runtime.chat())
