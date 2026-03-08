"""Analysis Pydantic models."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.timezone import now_ist


class BestTimeSlot(BaseModel):
    """A recommended posting window for a specific day."""

    day_of_week: str = Field(..., description="monday–sunday (lowercase)")
    video_count: int = Field(1, ge=1)
    times: list[str] = Field(
        default_factory=list,
        description="HH:MM strings in 24-hour format",
    )


class CategoryAnalysis(BaseModel):
    """Gemini-generated insights for a single content category."""

    category: str
    best_title_patterns: list[str] = Field(default_factory=list)
    best_description_template: str = ""
    best_tags: list[str] = Field(default_factory=list)
    score: float = 0.0


class Analysis(BaseModel):
    """Top-level analysis document (one per channel)."""

    channel_id: str
    best_posting_times: list[BestTimeSlot] = Field(default_factory=list)
    category_analysis: list[CategoryAnalysis] = Field(default_factory=list)
    analysis_done_video_ids: list[str] = Field(default_factory=list)
    version: int = Field(1, ge=1, description="Auto-incremented on each update")
    created_at: datetime = Field(default_factory=now_ist)
    updated_at: datetime = Field(default_factory=now_ist)
