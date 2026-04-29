from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field
from app.timezone import now_ist

class ErrorEntry(BaseModel):
    """Represents a single error entry in the error queue."""
    id: str = Field(..., alias="_id")
    feature: str
    message: str
    stack_trace: Optional[str] = None
    context: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=now_ist)
    resolved: bool = False

class ErrorCreate(BaseModel):
    """Payload to create a new error entry."""
    feature: str
    message: str
    stack_trace: Optional[str] = None
    context: Dict[str, Any] = Field(default_factory=dict)

class ErrorUpdate(BaseModel):
    """Payload to update an error entry (e.g., mark as resolved)."""
    resolved: bool
