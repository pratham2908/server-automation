"""FastAPI application entry-point.

Configures the app, lifespan events (DB + service init), and router mounting.
"""

import logging
from app.logger import setup_root_logging
setup_root_logging()

logging.basicConfig(level=logging.INFO)
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI

from app.config import get_settings
from app.database import close_db, connect_db

# Services – initialised during lifespan, accessible to routers.
r2_service = None
youtube_service_manager = None
instagram_service_manager = None
gemini_service = None
_auto_publisher_task = None
_comment_analysis_task = None

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    """Startup / shutdown lifecycle manager.

    - Connects to MongoDB and creates indexes.
    - Initialises R2, YouTube, and Gemini service singletons.
    - On shutdown, closes the database connection.
    """
    global r2_service, youtube_service_manager, instagram_service_manager, gemini_service

    settings = get_settings()

    # ---- Database ----
    db = await connect_db(settings.MONGODB_URI, settings.MONGODB_DB_NAME)
    logger.info("Connected to MongoDB (%s)", settings.MONGODB_DB_NAME)

    # ---- R2 ----
    from app.services.r2 import R2Service

    r2_service = R2Service(
        endpoint_url=settings.R2_ENDPOINT_URL,
        access_key_id=settings.R2_ACCESS_KEY_ID,
        secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        bucket_name=settings.R2_BUCKET_NAME,
    )
    logger.info("R2 service initialised")

    # ---- YouTube (per-channel token manager, tokens stored in DB) ----
    from app.services.youtube import YouTubeServiceManager

    youtube_service_manager = YouTubeServiceManager(
        db=db,
        client_id=settings.YOUTUBE_CLIENT_ID,
        client_secret=settings.YOUTUBE_CLIENT_SECRET,
    )
    logger.info("YouTube service manager initialised (DB-backed tokens)")

    # ---- Instagram (per-channel token manager, tokens stored in DB) ----
    from app.services.instagram import InstagramServiceManager

    instagram_service_manager = InstagramServiceManager(
        db=db,
        app_id=settings.INSTAGRAM_APP_ID,
        app_secret=settings.INSTAGRAM_APP_SECRET,
    )
    logger.info("Instagram service manager initialised (DB-backed tokens)")

    # ---- Gemini ----
    from app.services.gemini import GeminiService

    gemini_service = GeminiService(api_key=settings.GEMINI_API_KEY)
    logger.info("Gemini service initialised")

    # ---- Background auto-publisher (Instagram scheduled reels) ----
    import asyncio
    from app.services.auto_publisher import run_auto_publisher

    global _auto_publisher_task, _comment_analysis_task
    _auto_publisher_task = asyncio.create_task(run_auto_publisher(db, r2_service))
    logger.info("Background auto-publisher started")

    # ---- Background comment analysis cron (24-hour cycle) ----
    from app.services.comment_analysis_cron import run_comment_analysis_cron

    _comment_analysis_task = asyncio.create_task(
        run_comment_analysis_cron(db, youtube_service_manager, instagram_service_manager, gemini_service)
    )
    logger.info("Background comment analysis cron started")

    yield

    # ---- Shutdown ----
    for task in (_auto_publisher_task, _comment_analysis_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    await close_db()
    logger.info("Database connection closed")


# ------------------------------------------------------------------
# App & router registration
# ------------------------------------------------------------------

from fastapi.middleware.cors import CORSMiddleware
from app.middleware import StructuredLoggingMiddleware

app = FastAPI(
    title="Video Automation Server",
    description="Automated multi-channel YouTube & Instagram video management",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(StructuredLoggingMiddleware)

from app.routers import analysis, categories, channels, comment_analysis, retention_analysis, system, ui, videos  # noqa: E402

app.include_router(channels.router)
app.include_router(videos.router)
app.include_router(categories.router)
app.include_router(analysis.router)
app.include_router(comment_analysis.router)
app.include_router(comment_analysis.config_router)
app.include_router(retention_analysis.router)
app.include_router(ui.router)
app.include_router(system.router)


@app.get("/health", tags=["health"])
async def health_check():
    """Simple liveness check."""
    return {"status": "ok"}


@app.get("/api/schema", tags=["schema"])
async def api_schema():
    """Full API schema with request/response examples for every endpoint."""
    return {
        "service": "Video Automation Server",
        "version": "2.0.0",
        "auth": {
            "header": "X-API-Key",
            "required_for": "/api/v1/*",
        },
        "endpoints": [
            # -- Health --
            {
                "group": "Health",
                "method": "GET",
                "path": "/health",
                "description": "Server liveness check",
                "request": None,
                "response": {"status": "ok"},
            },
            # -- Channels --
            {
                "group": "Channels",
                "method": "GET",
                "path": "/api/v1/channels/",
                "description": "List all channels (YouTube + Instagram)",
                "request": None,
                "response": [
                    {
                        "channel_id": "ch1",
                        "name": "My Tech Channel",
                        "platform": "youtube",
                        "youtube_channel_id": "UCxxxxxxxx",
                        "subscriber_count": 5000,
                        "video_count": 60,
                    }
                ],
            },
            {
                "group": "Channels",
                "method": "GET",
                "path": "/api/v1/channels/{channel_id}",
                "description": "Get a single channel",
                "request": None,
                "response": {
                    "channel_id": "ch1",
                    "name": "My Tech Channel",
                    "platform": "youtube",
                },
            },
            {
                "group": "Channels",
                "method": "POST",
                "path": "/api/v1/channels/",
                "description": "Register a new channel (YouTube or Instagram)",
                "request": {
                    "platform": "youtube",
                    "youtube_channel_id": "UCxxxxxxxx",
                    "channel_id": "optional-custom-slug",
                },
                "response": {"channel_id": "ch1", "name": "Fetched from platform"},
            },
            {
                "group": "Channels",
                "method": "PATCH",
                "path": "/api/v1/channels/{channel_id}",
                "description": "Update channel fields",
                "request": {"name": "New Channel Name"},
                "response": {"channel_id": "ch1", "name": "New Channel Name"},
            },
            {
                "group": "Channels",
                "method": "POST",
                "path": "/api/v1/channels/{channel_id}/refresh",
                "description": "Re-fetch channel name and stats from the channel's platform (YouTube or Instagram)",
                "request": None,
                "response": {"channel_id": "ch1", "name": "Refreshed Name"},
            },
            {
                "group": "Channels",
                "method": "GET",
                "path": "/api/v1/channels/{channel_id}/content-params",
                "description": "List all content param definitions for the channel",
                "request": None,
                "response": [{"name": "simulation_type", "values": [{"value": "battle", "score": 85, "video_count": 4}], "belongs_to": ["all"]}],
            },
            {
                "group": "Channels",
                "method": "POST",
                "path": "/api/v1/channels/{channel_id}/content-params",
                "description": "Add a new content param definition",
                "request": {"name": "simulation_type", "description": "Type of simulation", "values": ["battle", "survival"], "belongs_to": ["all"]},
                "response": {"name": "simulation_type", "values": [{"value": "battle", "score": 0, "video_count": 0}], "belongs_to": ["all"]},
            },
            {
                "group": "Channels",
                "method": "DELETE",
                "path": "/api/v1/channels/{channel_id}",
                "description": "Delete channel and all associated data, including R2 files",
                "request": None,
                "response": {"ok": True, "channel_id": "ch1", "deleted": True},
            },
            # -- YouTube OAuth Config --
            {
                "group": "Config",
                "method": "PUT",
                "path": "/api/v1/channels/config/youtube-oauth",
                "description": "Store YouTube OAuth client credentials in DB",
                "request": {"client_id": "818394441499-...", "client_secret": "GOCSPX-..."},
                "response": {"ok": True, "message": "YouTube OAuth config saved"},
            },
            {
                "group": "Config",
                "method": "GET",
                "path": "/api/v1/channels/config/youtube-oauth",
                "description": "Check if YouTube OAuth client credentials are configured",
                "request": None,
                "response": {"configured": True, "client_id": "818394441499-..."},
            },
            # -- YouTube Tokens --
            {
                "group": "YouTube Tokens",
                "method": "POST",
                "path": "/api/v1/channels/{channel_id}/youtube-token",
                "description": "Store YouTube OAuth tokens for a channel (called by frontend after OAuth flow)",
                "request": {
                    "token": "ya29.a0ARrdaM...",
                    "refresh_token": "1//0eXyz...",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "scopes": ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly", "https://www.googleapis.com/auth/youtube.force-ssl", "https://www.googleapis.com/auth/yt-analytics.readonly"],
                    "expiry": "2026-03-07T12:00:00Z",
                },
                "response": {"ok": True, "channel_id": "ch1", "message": "YouTube tokens stored"},
            },
            {
                "group": "YouTube Tokens",
                "method": "GET",
                "path": "/api/v1/channels/{channel_id}/youtube-token",
                "description": "Get a fresh access token (auto-refreshes if expired). Never returns the refresh token.",
                "request": None,
                "response": {"ok": True, "access_token": "ya29.a0ARrdaM...", "expiry": "2026-03-07T13:00:00Z"},
            },
            {
                "group": "YouTube Tokens",
                "method": "GET",
                "path": "/api/v1/channels/{channel_id}/youtube-token/status",
                "description": "Check YouTube token status (connected/disconnected/expired) without exposing token values",
                "request": None,
                "response": {
                    "channel_id": "ch1",
                    "connected": True,
                    "status": "active",
                    "has_refresh_token": True,
                    "expiry": "2026-03-07T13:00:00Z",
                },
            },
            # -- Categories --
            {
                "group": "Categories",
                "method": "GET",
                "path": "/api/v1/channels/{channel_id}/categories/",
                "description": "List categories for a channel",
                "query_params": {
                    "status_filter": {"type": "string", "enum": ["active", "archived"], "optional": True},
                },
                "request": None,
                "response": [
                    {
                        "channel_id": "ch1",
                        "name": "Tutorials",
                        "description": "How-to guides",
                        "score": 85.5,
                        "status": "active",
                        "video_count": 10,
                        "metadata": {
                            "total_videos": 10,
                            "avg_views": 1500.0,
                            "avg_likes": 15.5,
                            "avg_comments": 3.2,
                            "avg_duration_seconds": 28.0,
                            "avg_engagement_rate": 1.25,
                            "avg_like_rate": 1.03,
                            "avg_comment_rate": 0.22,
                            "avg_percentage_viewed": 72.5,
                            "avg_view_duration_seconds": 20,
                            "total_views": 15000,
                            "total_estimated_minutes_watched": 560.0,
                        },
                    }
                ],
            },
            {
                "group": "Categories",
                "method": "POST",
                "path": "/api/v1/channels/{channel_id}/categories/",
                "description": "Add one or more categories",
                "request": {"name": "Tutorials", "description": "How-to guides", "score": 80},
                "response": ["inserted_id_1"],
            },
            {
                "group": "Categories",
                "method": "PATCH",
                "path": "/api/v1/channels/{channel_id}/categories/{category_object_id}",
                "description": "Update a category. Name changes propagate to videos.",
                "request": {"name": "New Name", "description": "Updated", "score": 90, "status": "archived"},
                "response": {"ok": True, "category_id": "inserted_id_1"},
            },
            {
                "group": "Categories",
                "method": "DELETE",
                "path": "/api/v1/channels/{channel_id}/categories/{category_object_id}",
                "description": "Delete a category and move its videos to 'Uncategorized'",
                "request": None,
                "response": {"ok": True, "category_id": "inserted_id_1", "deleted": True},
            },
            # -- Videos --
            {
                "group": "Videos",
                "method": "GET",
                "path": "/api/v1/channels/{channel_id}/videos/",
                "description": "List videos with sync status",
                "query_params": {
                    "status_filter": {"type": "string", "enum": ["todo", "ready", "scheduled", "published"], "optional": True},
                    "verification_status": {"type": "string", "enum": ["unverified", "verified", "missing"], "optional": True},
                    "suggest_n": {"type": "integer", "optional": True, "description": "Mark top N todo videos as suggested"},
                },
                "request": None,
                "response": {
                    "videos": [
                        {
                            "channel_id": "ch1",
                            "video_id": "uuid-1234",
                            "title": "How to code",
                            "description": "...",
                            "tags": ["coding", "tutorial"],
                            "category": "Tutorials",
                            "status": "todo",
                            "suggested": False,
                            "youtube_video_id": None,
                            "instagram_media_id": None,
                            "r2_object_key": None,
                            "metadata": {
                                "views": None,
                                "likes": None,
                                "comments": None,
                                "duration_seconds": None,
                                "engagement_rate": None,
                                "like_rate": None,
                                "comment_rate": None,
                                "avg_percentage_viewed": None,
                                "avg_view_duration_seconds": None,
                                "estimated_minutes_watched": None,
                            },
                            "scheduled_at": None,
                            "published_at": None,
                            "created_at": "2026-03-01T12:00:00Z",
                            "updated_at": "2026-03-01T12:00:00Z",
                        }
                    ],
                    "sync_status": {
                        "available": True,
                        "platform_total": 60,
                        "in_database": 55,
                        "new_videos_to_import": 5,
                        "pending_reconciliation": 2,
                        "metadata_to_refresh": 55,
                    },
                },
            },
            {
                "group": "Videos – Content Params",
                "method": "POST",
                "path": "/api/v1/channels/{channel_id}/videos/{video_id}/extract-params",
                "description": "Extract content params via Gemini from video metadata. Saves as unverified.",
                "request": None,
                "response": {
                    "ok": True,
                    "video_id": "uuid-1234",
                    "content_params": {"simulation_type": "battle", "music": "Epic Orchestral"},
                    "verification_status": "unverified",
                },
            },
            {
                "group": "Videos – Content Params",
                "method": "POST",
                "path": "/api/v1/channels/{channel_id}/videos/extract-params/all",
                "description": "Bulk extract content params for all videos missing them",
                "request": None,
                "response": {"ok": True, "extracted": 42, "total": 45},
            },
            {
                "group": "Videos – Content Params",
                "method": "POST",
                "path": "/api/v1/channels/{channel_id}/videos/{video_id}/verify-params",
                "description": "Mark content params as verified. Optionally pass corrected values.",
                "request": {"content_params": {"simulation_type": "survival", "music": "Dramatic Piano"}},
                "response": {
                    "ok": True,
                    "video_id": "uuid-1234",
                    "content_params": {"simulation_type": "survival", "music": "Dramatic Piano"},
                    "verification_status": "verified",
                },
            },
            {
                "group": "Videos",
                "method": "PATCH",
                "path": "/api/v1/channels/{channel_id}/videos/{video_id}/status",
                "description": "Update video status. Setting to 'published' auto-sets published_at.",
                "request": {"status": "published"},
                "response": {"ok": True, "video_id": "uuid-1234", "status": "published"},
            },
            {
                "group": "Videos",
                "method": "POST",
                "path": "/api/v1/channels/{channel_id}/videos/{video_id}/upload",
                "description": "Upload video file to R2 (multipart/form-data with 'file' field). Moves todo \u2192 ready.",
                "content_type": "multipart/form-data",
                "request": {"file": "(binary video file)"},
                "response": {
                    "ok": True,
                    "video": {"video_id": "uuid-1234", "status": "ready", "r2_object_key": "ch1/uuid-1234.mp4"},
                    "queue_position": 3,
                },
            },
            {
                "group": "Videos",
                "method": "POST",
                "path": "/api/v1/channels/{channel_id}/videos/create",
                "description": "Create ad-hoc video, upload to R2, and add to posting or schedule queue (Instagram only). Bypass todo stage.",
                "content_type": "multipart/form-data",
                "request": {
                    "file": "(binary)",
                    "title": "My Reel",
                    "description": "...",
                    "tags": "tag1, tag2",
                    "category": "Comedy",
                    "content_params": "{\"topic\": \"humor\"}",
                    "scheduled_at": "2026-03-20T10:00:00+05:30 (Instagram only)"
                },
                "response": {
                    "ok": True,
                    "video": {"video_id": "uuid-1234", "status": "scheduled", "scheduled_at": "..."},
                    "queue_position": 1
                },
            },
            {
                "group": "Videos",
                "method": "POST",
                "path": "/api/v1/channels/{channel_id}/videos/{video_id}/schedule",
                "description": "Schedule video(s) on the channel's platform (YouTube upload or Instagram queue). Pass video_id='all' to schedule everything in the ready queue.",
                "request": None,
                "response": {
                    "ok": True,
                    "scheduled": 2,
                    "failed": 0,
                    "videos": [
                        {
                            "video_id": "550e8400-...",
                            "status": "scheduled",
                            "youtube_video_id": "dQw4w...",
                            "scheduled_at": "2026-03-10T10:00:00+05:30",
                        },
                    ],
                },
            },
            {
                "group": "Videos",
                "method": "POST",
                "path": "/api/v1/channels/{channel_id}/videos/sync",
                "description": "Sync from platform (YouTube or Instagram): refresh metadata, reconcile statuses, import new videos",
                "request": {"new_category_description": "Optional Gemini instructions"},
                "response": {
                    "ok": True,
                    "synced": 5,
                    "metadata_refreshed": 45,
                    "categories_created": ["Tutorials"],
                    "videos": [{"title": "New Video", "category": "Tutorials"}],
                },
            },
            {
                "group": "Videos",
                "method": "POST",
                "path": "/api/v1/channels/{channel_id}/videos/updateToDoList",
                "description": "Generate n new video ideas via Gemini based on latest analysis",
                "request": {"n": 5},
                "response": {"ok": True, "message": "Successfully generated 5 new videos for the to-do list."},
            },
            {
                "group": "Videos",
                "method": "DELETE",
                "path": "/api/v1/channels/{channel_id}/videos/{video_id}",
                "description": "Delete a video document and clean up all associated assets and queue entries",
                "request": None,
                "response": {"ok": True, "video_id": "uuid-1234", "deleted": True},
            },
            # -- Analysis --
            {
                "group": "Analysis",
                "method": "GET",
                "path": "/api/v1/channels/{channel_id}/analysis/latest",
                "description": "Get latest channel summary with subscriber_count and analysis_status counts",
                "request": None,
                "response": {
                    "channel_id": "ch1",
                    "subscriber_count": 5000,
                    "version": 2,
                    "category_analysis": [
                        {"category": "Tutorials", "score": 85.5, "best_title_patterns": ["How to..."]},
                    ],
                    "best_posting_times": [
                        {"day_of_week": "monday", "times": ["14:00", "18:00"], "video_count": 2},
                    ],
                    "content_param_analysis": [
                        {"param_name": "simulation_type", "best_values": ["battle"], "worst_values": ["puzzle"], "insight": "..."},
                    ],
                    "best_combinations": [
                        {"params": {"simulation_type": "battle", "music": "Epic"}, "reasoning": "..."},
                    ],
                    "analysis_done_video_ids": ["vid1", "vid2"],
                    "analysis_status": {
                        "ready_for_analysis": 5,
                        "not_ready_yet": 2,
                    },
                },
            },
            {
                "group": "Analysis",
                "method": "POST",
                "path": "/api/v1/channels/{channel_id}/analysis/update",
                "description": "Two-step pipeline: (1) per-video analysis stored in analysis_history, (2) channel summary",
                "request": None,
                "response": {"channel_id": "ch1", "subscriber_count": 5000, "version": 3, "category_analysis": ["..."]},
            },
            {
                "group": "Analysis",
                "method": "GET",
                "path": "/api/v1/channels/{channel_id}/analysis/history",
                "description": "List per-video analyses with optional date range filter",
                "query_params": {
                    "from": {"type": "datetime", "optional": True, "description": "Filter analyzed_at >= from"},
                    "to": {"type": "datetime", "optional": True, "description": "Filter analyzed_at <= to"},
                    "limit": {"type": "integer", "optional": True, "description": "Max results; if omitted, returns entire history"},
                },
                "request": None,
                "response": [
                    {
                        "channel_id": "ch1",
                        "video_id": "uuid-1234",
                        "youtube_video_id": "dQw4w...",
                        "title": "Epic Battle Simulation",
                        "category": "Simulations",
                        "content_params": {"simulation_type": "battle", "music": "Epic Orchestral"},
                        "published_at": "2026-03-01T10:00:00+05:30",
                        "stats_snapshot": {
                            "views": 15000, "likes": 800, "comments": 45,
                            "engagement_rate": 5.63, "avg_percentage_viewed": 72.5,
                            "subscribers_gained": 120, "views_per_subscriber": 3.0,
                            "subscriber_count_at_analysis": 5000,
                        },
                        "ai_insight": {
                            "performance_rating": 85,
                            "what_worked": "Strong title hook + battle format",
                            "what_didnt": "Could improve description SEO",
                            "key_learnings": ["Battle sims drive 3x engagement"],
                        },
                        "analyzed_at": "2026-03-07T12:00:00+05:30",
                    }
                ],
            },
            {
                "group": "Analysis",
                "method": "GET",
                "path": "/api/v1/channels/{channel_id}/analysis/history/{video_id}",
                "description": "Get per-video analysis for a specific video",
                "request": None,
                "response": {
                    "channel_id": "ch1",
                    "video_id": "uuid-1234",
                    "published_at": "2026-03-01T10:00:00+05:30",
                    "stats_snapshot": {"views": 15000, "subscribers_gained": 120},
                    "ai_insight": {"performance_rating": 85, "what_worked": "..."},
                    "analyzed_at": "2026-03-07T12:00:00+05:30",
                },
            },
            {
                "group": "Analysis",
                "method": "GET",
                "path": "/api/v1/channels/{channel_id}/analysis/compare",
                "description": "Compare channel performance across two time periods",
                "query_params": {
                    "from1": {"type": "datetime", "required": True},
                    "to1": {"type": "datetime", "required": True},
                    "from2": {"type": "datetime", "required": True},
                    "to2": {"type": "datetime", "required": True},
                },
                "request": None,
                "response": {
                    "channel_id": "ch1",
                    "period_1": {
                        "from": "2026-02-01", "to": "2026-02-15",
                        "video_count": 10, "avg_views": 12000,
                        "avg_engagement_rate": 4.5, "total_subscribers_gained": 500,
                        "avg_performance_rating": 72.3,
                    },
                    "period_2": {
                        "from": "2026-02-16", "to": "2026-03-01",
                        "video_count": 12, "avg_views": 18000,
                        "avg_engagement_rate": 5.8, "total_subscribers_gained": 850,
                        "avg_performance_rating": 81.5,
                    },
                },
            },
            # -- Comment Analysis --
            {
                "group": "Comment Analysis",
                "method": "POST",
                "path": "/api/v1/channels/{channel_id}/comment-analysis/trigger",
                "description": "Manually trigger a comment-analysis cycle for this channel (same as what the 24h cron does)",
                "request": None,
                "response": {
                    "ok": True,
                    "channel_id": "ch1",
                    "analyzed": 3,
                    "re_analyzed": 1,
                    "skipped": 10,
                    "errors": 0,
                },
            },
            {
                "group": "Comment Analysis",
                "method": "GET",
                "path": "/api/v1/channels/{channel_id}/comment-analysis/history",
                "description": "List comment analyses for a channel with optional filters",
                "query_params": {
                    "source": {"type": "string", "enum": ["own", "competitor"], "optional": True},
                    "platform": {"type": "string", "enum": ["youtube", "instagram"], "optional": True},
                    "limit": {"type": "integer", "optional": True},
                },
                "request": None,
                "response": [
                    {
                        "_id": "60f7b2a1...",
                        "channel_id": "ch1",
                        "platform_video_id": "dQw4w9WgXcQ",
                        "platform": "youtube",
                        "source": "competitor",
                        "competitor_channel_id": "UCxxxxxxxx",
                        "video_title": "Competitor's Best Video",
                        "video_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                        "total_comments_fetched": 450,
                        "total_comments_analyzed": 380,
                        "last_known_comment_count": 450,
                        "comments_analyzed_upto": "2026-03-20T15:30:00Z",
                        "analysis": {
                            "sentiment_summary": {"positive_percentage": 72, "negative_percentage": 12, "neutral_percentage": 16, "overall_sentiment": "positive"},
                            "what_audience_loves": [{"theme": "Clear explanations", "signal_strength": 8, "representative_quotes": ["..."], "count": 45}],
                            "complaints": [{"theme": "Audio quality", "signal_strength": 4, "representative_quotes": ["..."], "count": 8}],
                            "demands": [{"topic": "Cover advanced topics", "signal_strength": 9, "demand_type": "content_request", "representative_quotes": ["..."], "count": 67}],
                            "content_gaps": ["No coverage of advanced workflows"],
                            "trending_topics": ["AI integration"],
                            "key_insights": ["Strong demand for advanced content"],
                        },
                        "version": 2,
                        "analyzed_at": "2026-03-20T12:00:00+05:30",
                    }
                ],
            },
            {
                "group": "Comment Analysis",
                "method": "GET",
                "path": "/api/v1/channels/{channel_id}/comment-analysis/{analysis_id}",
                "description": "Get a specific comment analysis by its MongoDB _id",
                "request": None,
                "response": {"_id": "60f7b2a1...", "channel_id": "ch1", "platform_video_id": "...", "analysis": {"...": "..."}},
            },
            {
                "group": "Comment Analysis",
                "method": "DELETE",
                "path": "/api/v1/channels/{channel_id}/comment-analysis/{analysis_id}",
                "description": "Delete a specific comment analysis",
                "request": None,
                "response": {"ok": True, "deleted": True, "analysis_id": "60f7b2a1..."},
            },
            {
                "group": "Comment Analysis",
                "method": "DELETE",
                "path": "/api/v1/channels/{channel_id}/comment-analysis/",
                "description": "Delete all comment analyses for a channel",
                "request": None,
                "response": {"ok": True, "channel_id": "ch1", "deleted_count": 15},
            },
            {
                "group": "Comment Analysis",
                "method": "GET",
                "path": "/api/v1/channels/{channel_id}/comment-analysis/aggregate",
                "description": "Aggregate all comment analyses into a combined demand/sentiment report",
                "query_params": {
                    "source": {"type": "string", "enum": ["own", "competitor"], "optional": True},
                },
                "request": None,
                "response": {
                    "channel_id": "ch1",
                    "total_videos_analyzed": 15,
                    "total_comments_analyzed": 5200,
                    "aggregate_sentiment": {"positive_percentage": 68, "negative_percentage": 14, "neutral_percentage": 18, "overall_sentiment": "positive"},
                    "top_loves": [{"theme": "Production quality", "signal_strength": 9, "count": 320, "representative_quotes": ["..."]}],
                    "top_complaints": [{"theme": "Upload frequency", "signal_strength": 6, "count": 85, "representative_quotes": ["..."]}],
                    "top_demands": [{"topic": "Tutorial series", "signal_strength": 10, "demand_type": "content_request", "count": 410, "representative_quotes": ["..."]}],
                    "all_content_gaps": ["Advanced workflows", "Mobile-first content"],
                    "all_trending_topics": ["AI tools", "Short-form content"],
                    "all_key_insights": ["Audience craves depth over breadth"],
                },
            },
            # -- Retention Analysis --
            {
                "group": "Retention Analysis",
                "method": "GET",
                "path": "/api/v1/channels/{channel_id}/retention-analysis/history",
                "description": "List retention analyses for a channel with optional status filter",
                "query_params": {
                    "status": {"type": "string", "enum": ["pending", "analyzing", "completed", "failed"], "optional": True},
                    "limit": {"type": "integer", "optional": True, "default": 50},
                },
                "request": None,
                "response": [
                    {
                        "channel_id": "ch1",
                        "video_id": "uuid-1234",
                        "video_title": "Epic Battle Simulation",
                        "platform": "youtube",
                        "status": "completed",
                        "analysis": {
                            "predicted_avg_retention_percent": 62.5,
                            "hook_analysis": {"score": 78, "risk_level": "low"},
                            "pacing_analysis": {"pacing_score": 71, "total_scene_cuts": 18},
                        },
                        "analyzed_at": "2026-03-20T12:00:00+05:30",
                    }
                ],
            },
            {
                "group": "Retention Analysis",
                "method": "GET",
                "path": "/api/v1/channels/{channel_id}/retention-analysis/{video_id}",
                "description": "Get retention analysis for a video. Includes 'comparison' sub-object when actual metrics are available.",
                "request": None,
                "response": {
                    "channel_id": "ch1",
                    "video_id": "uuid-1234",
                    "video_title": "Epic Battle Simulation",
                    "status": "completed",
                    "analysis": {
                        "predicted_avg_retention_percent": 62.5,
                        "hook_analysis": {"score": 78, "risk_level": "low"},
                        "pacing_analysis": {"pacing_score": 71, "total_scene_cuts": 18},
                        "strengths": ["Strong opening hook"],
                        "weaknesses": ["Mid-section pacing drops"],
                        "recommendations": ["Add B-roll at 45s mark"],
                    },
                    "actual_avg_percentage_viewed": 58.3,
                    "actual_performance_rating": 72,
                    "comparison": {
                        "predicted_avg_retention_percent": 62.5,
                        "actual_avg_percentage_viewed": 58.3,
                        "retention_deviation": 4.2,
                        "retention_accuracy_pct": 95.8,
                        "prediction_quality": "accurate",
                    },
                },
            },
            {
                "group": "Retention Analysis",
                "method": "POST",
                "path": "/api/v1/channels/{channel_id}/retention-analysis/{video_id}/trigger",
                "description": "Manually trigger retention analysis for a video (must have R2 file)",
                "request": None,
                "response": {
                    "ok": True,
                    "video_id": "uuid-1234",
                    "message": "Retention analysis triggered — poll GET /{video_id} for status",
                },
            },
            {
                "group": "Retention Analysis",
                "method": "DELETE",
                "path": "/api/v1/channels/{channel_id}/retention-analysis/{video_id}",
                "description": "Delete the retention analysis for a video",
                "request": None,
                "response": {"ok": True, "video_id": "uuid-1234", "deleted": True},
            },
            # -- Comment Analysis Config --
            {
                "group": "Comment Analysis Config",
                "method": "GET",
                "path": "/api/v1/comment-analysis/config/",
                "description": "Get the current comment analysis cron schedule (analysis_hour in IST)",
                "request": None,
                "response": {
                    "key": "comment_analysis_config",
                    "analysis_hour": 3,
                    "updated_at": "2026-03-20T12:00:00+05:30",
                },
            },
            {
                "group": "Comment Analysis Config",
                "method": "PUT",
                "path": "/api/v1/comment-analysis/config/",
                "description": "Update the comment analysis cron schedule. Changes take effect on the next cycle (no restart needed).",
                "request": {"analysis_hour": 4},
                "response": {
                    "ok": True,
                    "analysis_hour": 4,
                    "message": "Comment analysis cron will run daily at 04:00 IST",
                },
            },
        ],
    }
