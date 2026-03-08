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
    score: float = 0.0


class ContentParamAnalysis(BaseModel):
    """Performance analysis for a single content parameter dimension."""

    param_name: str
    best_values: list[str] = Field(default_factory=list)
    worst_values: list[str] = Field(default_factory=list)
    insight: str = ""


class BestCombination(BaseModel):
    """A top-performing combination of content parameter values."""

    params: dict[str, str] = Field(default_factory=dict)
    reasoning: str = ""


class Analysis(BaseModel):
    """Top-level analysis document (one per channel)."""

    channel_id: str
    best_posting_times: list[BestTimeSlot] = Field(default_factory=list)
    category_analysis: list[CategoryAnalysis] = Field(default_factory=list)
    content_param_analysis: list[ContentParamAnalysis] = Field(default_factory=list)
    best_combinations: list[BestCombination] = Field(default_factory=list)
    analysis_done_video_ids: list[str] = Field(default_factory=list)
    version: int = Field(1, ge=1, description="Auto-incremented on each update")
    created_at: datetime = Field(default_factory=now_ist)
    updated_at: datetime = Field(default_factory=now_ist)
