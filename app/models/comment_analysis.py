"""Comment analysis Pydantic models.

Defines the document shape for the ``comment_analysis`` MongoDB collection
and the structured output schema that Gemini produces.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.timezone import now_ist


class SentimentSummary(BaseModel):
    """Breakdown of comment sentiment across positive / negative / neutral."""

    positive_percentage: float = Field(0, ge=0, le=100)
    negative_percentage: float = Field(0, ge=0, le=100)
    neutral_percentage: float = Field(0, ge=0, le=100)
    overall_sentiment: str = Field(
        "neutral",
        description="One of: positive, negative, neutral, mixed",
    )


class AudienceSignal(BaseModel):
    """A consolidated audience theme (love or complaint)."""

    theme: str = Field(..., description="Short label for the theme")
    signal_strength: int = Field(1, ge=1, le=10, description="Popularity score 1-10")
    representative_quotes: list[str] = Field(default_factory=list)
    count: int = Field(1, ge=1, description="Number of comments expressing this theme")


class DemandSignal(BaseModel):
    """A specific content / feature / topic request from the audience."""

    topic: str = Field(..., description="What the audience is asking for")
    signal_strength: int = Field(1, ge=1, le=10)
    demand_type: str = Field(
        "content_request",
        description="One of: content_request, feature_request, topic_request, format_request",
    )
    representative_quotes: list[str] = Field(default_factory=list)
    count: int = Field(1, ge=1)


class CommentAnalysisResult(BaseModel):
    """Structured output from Gemini comment analysis."""

    sentiment_summary: SentimentSummary = Field(default_factory=SentimentSummary)
    what_audience_loves: list[AudienceSignal] = Field(default_factory=list)
    complaints: list[AudienceSignal] = Field(default_factory=list)
    demands: list[DemandSignal] = Field(default_factory=list)
    content_gaps: list[str] = Field(default_factory=list)
    trending_topics: list[str] = Field(default_factory=list)
    key_insights: list[str] = Field(default_factory=list)


class CommentAnalysis(BaseModel):
    """Full document stored in the ``comment_analysis`` collection.

    One document per analyzed video, keyed by ``(channel_id, platform_video_id)``.
    """

    channel_id: str = Field(..., description="Parent managed channel")
    platform_video_id: str = Field(..., description="YouTube video ID or IG media ID")
    platform: str = Field("youtube", description="'youtube' or 'instagram'")
    source: str = Field("competitor", description="'own' or 'competitor'")
    competitor_channel_id: Optional[str] = Field(
        None, description="Competitor's channel ID; null for own channel videos",
    )
    video_title: str = Field("")
    video_url: str = Field("")
    total_comments_fetched: int = Field(0, description="Cumulative across all analysis runs")
    total_comments_analyzed: int = Field(0, description="Cumulative, after spam/short filter")
    last_known_comment_count: int = Field(
        0, description="Comment count from platform stats; used as cheap pre-check for new comments",
    )
    comments_analyzed_upto: Optional[datetime] = Field(
        None, description="Timestamp of newest comment analyzed; cutoff for next incremental fetch",
    )
    analysis: CommentAnalysisResult = Field(default_factory=CommentAnalysisResult)
    analyzed_at: datetime = Field(default_factory=now_ist)
    version: int = Field(1, ge=1, description="Incremented on each re-analysis")
    created_at: datetime = Field(default_factory=now_ist)
    updated_at: datetime = Field(default_factory=now_ist)
