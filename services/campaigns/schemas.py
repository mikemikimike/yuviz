"""Pydantic request models for Campaign Service's REST API."""

from __future__ import annotations

from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

CampaignStatus = Literal["draft", "running", "paused", "completed"]


def _valid_timezone(name: str | None) -> str | None:
    if name is not None:
        try:
            ZoneInfo(name)
        except (ValueError, OSError, KeyError):
            raise ValueError(f"unknown timezone {name!r}")
    return name


class CampaignCreate(BaseModel):
    agent_id:               str
    name:                   str
    caller_id:              str | None = None
    max_concurrent_calls:   int = Field(1, ge=1)
    pacing_seconds:         int = 5
    max_attempts:           int = Field(1, ge=1)
    retry_gap_minutes:      int = Field(60, ge=1)
    # "HH:MM" 24h, both or neither; None/None means unrestricted.
    calling_hours_start:    str | None = None
    calling_hours_end:      str | None = None
    calling_hours_timezone: str = "UTC"

    _check_timezone = field_validator("calling_hours_timezone")(_valid_timezone)


class CampaignUpdate(BaseModel):
    name:                   str | None = None
    caller_id:              str | None = None
    max_concurrent_calls:   int | None = Field(None, ge=1)
    pacing_seconds:         int | None = None
    max_attempts:           int | None = Field(None, ge=1)
    retry_gap_minutes:      int | None = Field(None, ge=1)
    calling_hours_start:    str | None = None
    calling_hours_end:      str | None = None
    calling_hours_timezone: str | None = None

    _check_timezone = field_validator("calling_hours_timezone")(_valid_timezone)


class ContactCreate(BaseModel):
    phone_number: str
    name:         str | None = None


class DncNumberCreate(BaseModel):
    phone_number: str
    reason:       str | None = None


class PlatformLimitsUpdate(BaseModel):
    model_config = {"strict": True}
    total_call_capacity:           int | None
    inbound_reserve:               int
    max_outbound_concurrent_calls: int | None


class CpsLimitUpdate(BaseModel):
    model_config = {"strict": True}
    cps: int


class TenantCapUpdate(BaseModel):
    model_config = {"strict": True}
    max_outbound_concurrent_calls: int | None
