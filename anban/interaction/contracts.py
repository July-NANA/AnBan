"""Transport-neutral Interaction input contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from anban.core.ids import InteractionId
from anban.core.metadata import SafeMetadata
from anban.core.models import UtcDateTime, now_utc


class InteractionEnvelope(BaseModel):
    """A normalized external request before Task creation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: InteractionId
    source: Literal["cli"] = "cli"
    content: str = Field(min_length=1, max_length=32_768)
    received_at: UtcDateTime = Field(default_factory=now_utc)
    metadata: SafeMetadata = Field(default_factory=SafeMetadata)
