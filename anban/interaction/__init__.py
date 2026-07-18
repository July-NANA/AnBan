"""External input, output, feedback, and bidirectional event adapters."""

from anban.interaction.contracts import (
    CorrelationFailureReason,
    CorrelationKey,
    CorrelationPurpose,
    InteractionCorrelation,
    InteractionEnvelope,
    InteractionInputKind,
    InteractionRoute,
)
from anban.interaction.service import InteractionChatSession, InteractionService

__all__ = [
    "CorrelationFailureReason",
    "CorrelationKey",
    "CorrelationPurpose",
    "InteractionChatSession",
    "InteractionCorrelation",
    "InteractionEnvelope",
    "InteractionInputKind",
    "InteractionRoute",
    "InteractionService",
]
