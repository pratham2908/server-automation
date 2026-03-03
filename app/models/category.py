"""Category Pydantic models."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class CategoryStatus(str, Enum):
    """Lifecycle states for a category."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class Category(BaseModel):
    """Full category document in the ``categories`` collection."""

    channel_id: str
    name: str
    description: str = ""
    raw_description: str = ""
    score: float = 0.0
    status: CategoryStatus = CategoryStatus.ACTIVE
    video_count: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CategoryCreate(BaseModel):
    """Payload for adding new categories."""

    name: str
    description: str = ""
    raw_description: str = ""
    score: float = 0.0


class CategoryUpdate(BaseModel):
    """Partial update payload for an existing category."""

    name: Optional[str] = None
    description: Optional[str] = None
    raw_description: Optional[str] = None
    score: Optional[float] = None
    status: Optional[CategoryStatus] = None
