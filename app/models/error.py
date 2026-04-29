from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.timezone import IST, now_ist


class ErrorEntry(BaseModel):
    """Represents a single error entry in the error queue."""

    id: str = Field(..., alias="_id")
    feature: str
    message: str
    stack_trace: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=now_ist)
    last_occurred_at: datetime = Field(default_factory=now_ist)
    count: int = 1
    resolved: bool = False

    @field_validator("timestamp", "last_occurred_at", mode="before")
    @classmethod
    def ensure_ist(cls, v: Any) -> Any:
        """Ensure datetimes are IST-aware for the API response."""
        if isinstance(v, datetime):
            if v.tzinfo is None:
                # MongoDB returns naive UTC, so we localize it to UTC first
                v = v.replace(tzinfo=timezone.utc)
            return v.astimezone(IST)
        return v


class ErrorCreate(BaseModel):
    """Payload to create a new error entry."""

    feature: str
    message: str
    stack_trace: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class ErrorUpdate(BaseModel):
    """Payload to update an error entry (e.g., mark as resolved)."""

    resolved: bool
