"""Video retention analysis Pydantic models.

Defines the structured output schema that Gemini produces for video
retention prediction, and the MongoDB document shape for the
``retention_analysis`` collection.
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.timezone import now_ist


class HookAnalysis(BaseModel):
    """Analysis of the first 5 seconds — the most critical retention window."""

    score: int = Field(0, ge=0, le=100, description="Hook effectiveness 0-100")
    risk_level: str = Field(
        "medium",
        description="One of: low, medium, high",
    )
    first_frame_description: str = Field("", description="What the viewer sees in the very first frame")
    visual_change_within_5s: bool = Field(False, description="Whether a significant visual change occurs in the first 5 seconds")
    audio_hook_present: bool = Field(False, description="Whether a compelling audio hook exists in the first 5 seconds")
    text_overlay_present: bool = Field(False, description="Whether text overlay / captions appear in the first 5 seconds")
    notes: list[str] = Field(default_factory=list, description="Specific observations about the hook")


class SceneCut(BaseModel):
    """A single visual transition point in the video."""

    timestamp_seconds: float = Field(..., ge=0)
    description: str = Field(..., description="What changes at this point")
    transition_type: str = Field(
        "hard_cut",
        description="One of: hard_cut, fade, dissolve, zoom, pan, whip, slide, motion_change, other",
    )


class PacingAnalysis(BaseModel):
    """Metrics on the visual pacing / scene-cut frequency of the video."""

    total_scene_cuts: int = Field(0, ge=0)
    avg_cut_interval_seconds: float = Field(0, ge=0, description="Average seconds between cuts")
    longest_static_segment_seconds: float = Field(0, ge=0, description="Longest stretch without a visual change")
    pacing_score: int = Field(0, ge=0, le=100, description="Overall pacing quality 0-100")
    visual_change_timestamps: list[SceneCut] = Field(default_factory=list)


class DropOffPoint(BaseModel):
    """A predicted audience drop-off point."""

    timestamp_seconds: float = Field(..., ge=0)
    reason: str = Field(..., description="Why viewers are predicted to leave here")
    severity: int = Field(1, ge=1, le=10, description="Impact severity 1-10")


class RetentionPrediction(BaseModel):
    """Full structured output from Gemini's video retention analysis."""

    predicted_avg_retention_percent: float = Field(
        0, ge=0, le=100,
        description="Predicted average percentage of video watched",
    )
    predicted_drop_off_points: list[DropOffPoint] = Field(default_factory=list)
    hook_analysis: HookAnalysis = Field(default_factory=HookAnalysis)
    pacing_analysis: PacingAnalysis = Field(default_factory=PacingAnalysis)
    narrative_structure: str = Field(
        "",
        description="e.g. linear, problem-solution, listicle, tutorial, montage, story-arc",
    )
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class RetentionAnalysis(BaseModel):
    """Full document stored in the ``retention_analysis`` collection.

    One document per video, keyed by ``(channel_id, video_id)``.
    Predicted fields are populated at analysis time (when video reaches
    ``ready`` status).  Actual fields are backfilled later when the
    existing analysis pipeline processes the published video.
    """

    channel_id: str
    video_id: str
    video_title: str = ""
    platform: str = Field("youtube", description="'youtube' or 'instagram'")
    duration_seconds: Optional[float] = None
    status: str = Field(
        "pending",
        description="One of: pending, analyzing, completed, failed",
    )
    error_message: Optional[str] = None

    # Predicted (populated by Gemini video analysis)
    analysis: RetentionPrediction = Field(default_factory=RetentionPrediction)
    analyzed_at: Optional[datetime] = None

    # Actual metrics (backfilled from analysis_history after publish)
    actual_avg_percentage_viewed: Optional[float] = None
    actual_engagement_rate: Optional[float] = None
    actual_views: Optional[int] = None
    actual_like_rate: Optional[float] = None
    actual_comment_rate: Optional[float] = None
    actual_views_per_subscriber: Optional[float] = None
    actual_performance_rating: Optional[float] = None
    actual_stats_snapshot: Optional[dict[str, Any]] = None
    actuals_populated_at: Optional[datetime] = None

    created_at: datetime = Field(default_factory=now_ist)
    updated_at: datetime = Field(default_factory=now_ist)
