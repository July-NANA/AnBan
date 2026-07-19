"""Authoritative, transport-neutral schedule definitions."""

from __future__ import annotations

import re
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from anban.core.ids import ScheduleId
from anban.core.models import UtcDateTime

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TIMEZONE_PATTERN = re.compile(r"^[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+.-]+)*$")


class ScheduleKind(StrEnum):
    CRON = "cron"
    INTERVAL = "interval"


class ScheduleDefinition(BaseModel):
    """One immutable schedule definition; dispatch state belongs to D33."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: ScheduleId
    name: str = Field(min_length=1, max_length=64)
    kind: ScheduleKind
    timezone: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=32_768)
    cron_expression: str | None = Field(default=None, min_length=1, max_length=256)
    every_seconds: int | None = Field(default=None, ge=1, le=31_536_000)
    anchor_at: UtcDateTime
    next_occurrence_at: UtcDateTime
    created_at: UtcDateTime

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if _NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("Schedule name must be a bounded logical identifier")
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        if _TIMEZONE_PATTERN.fullmatch(value) is None:
            raise ValueError("Schedule timezone must be an IANA timezone name")
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Schedule timezone is unavailable") from None
        return value

    @field_validator("cron_expression")
    @classmethod
    def normalize_cron_expression(cls, value: str | None) -> str | None:
        return None if value is None else " ".join(value.split())

    @model_validator(mode="after")
    def validate_kind_fields_and_time_order(self) -> ScheduleDefinition:
        if self.kind is ScheduleKind.CRON:
            if self.cron_expression is None or self.every_seconds is not None:
                raise ValueError("Cron schedule requires only cron_expression")
            if len(self.cron_expression.split()) != 5:
                raise ValueError("Cron schedule requires the five-field POSIX form")
        elif self.every_seconds is None or self.cron_expression is not None:
            raise ValueError("Interval schedule requires only every_seconds")
        if self.next_occurrence_at <= self.anchor_at:
            raise ValueError("Schedule next occurrence must follow its anchor")
        if self.created_at > self.anchor_at:
            raise ValueError("Schedule creation cannot follow its anchor")
        return self
