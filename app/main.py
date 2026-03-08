"""FastAPI application entry-point.

Configures the app, lifespan events (DB + service init), and router mounting.
"""

import logging

logging.basicConfig(level=logging.INFO)
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI

from app.config import get_settings
from app.database import close_db, connect_db

# Services – initialised during lifespan, accessible to routers.
r2_service = None
youtube_service_manager = None
gemini_service = None

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    """Startup / shutdown lifecycle manager.

    - Connects to MongoDB and creates indexes.
    - Initialises R2, YouTube, and Gemini service singletons.
    - On shutdown, closes the database connection.
    """
    global r2_service, youtube_service_manager, gemini_service

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

    # ---- YouTube (per-channel token manager) ----
    from app.services.youtube import YouTubeServiceManager

    youtube_service_manager = YouTubeServiceManager(
        client_id=settings.YOUTUBE_CLIENT_ID,
        client_secret=settings.YOUTUBE_CLIENT_SECRET,
        tokens_dir="youtube_tokens",
    )
    logger.info("YouTube service manager initialised (per-channel tokens in youtube_tokens/)")

    # ---- Gemini ----
    from app.services.gemini import GeminiService

    gemini_service = GeminiService(api_key=settings.GEMINI_API_KEY)
    logger.info("Gemini service initialised")

    yield

    # ---- Shutdown ----
    await close_db()
    logger.info("Database connection closed")


# ------------------------------------------------------------------
# App & router registration
# ------------------------------------------------------------------

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="YouTube Automation Server",
    description="Automated multi-channel YouTube video management",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.routers import analysis, categories, channels, migration, videos  # noqa: E402

app.include_router(channels.router)
app.include_router(videos.router)
app.include_router(categories.router)
app.include_router(analysis.router)
app.include_router(migration.router)


@app.get("/health", tags=["health"])
async def health_check():
    """Simple liveness check."""
    return {"status": "ok"}


@app.get("/api/schema", tags=["schema"])
async def api_schema():
    """Full API schema with request/response examples for every endpoint."""
    return {
        "service": "YouTube Automation Server",
        "version": "1.0.0",
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
                "description": "List all channels",
                "request": None,
                "response": [
                    {
                        "channel_id": "ch1",
                        "name": "My Tech Channel",
                        "youtube_channel_id": "UCxxxxxxxx",
                        "description": "...",
                        "subscriber_count": 5000,
                        "video_count": 60,
                        "view_count": 1500000,
                        "created_at": "2026-03-01T12:00:00Z",
                        "updated_at": "2026-03-01T12:00:00Z",
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
                    "youtube_channel_id": "UCxxxxxxxx",
                },
            },
            {
                "group": "Channels",
                "method": "POST",
                "path": "/api/v1/channels/",
                "description": "Register a new channel by YouTube channel ID",
                "request": {
                    "youtube_channel_id": "UCxxxxxxxx",
                    "channel_id": "optional-custom-slug",
                },
                "response": {"channel_id": "ch1", "name": "Fetched from YouTube"},
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
                "description": "Re-fetch channel name and stats from YouTube",
                "request": None,
                "response": {"channel_id": "ch1", "name": "Refreshed Name"},
            },
            {
                "group": "Channels",
                "method": "DELETE",
                "path": "/api/v1/channels/{channel_id}",
                "description": "Delete channel and all associated data",
                "request": None,
                "response": {"status": "deleted"},
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
                "description": "Update a category",
                "request": {"name": "New Name", "description": "Updated", "score": 90, "status": "archived"},
                "response": {"name": "New Name", "score": 90},
            },
            # -- Videos --
            {
                "group": "Videos",
                "method": "GET",
                "path": "/api/v1/channels/{channel_id}/videos/",
                "description": "List videos with sync status",
                "query_params": {
                    "status_filter": {"type": "string", "enum": ["todo", "ready", "scheduled", "published"], "optional": True},
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
                            "topic": "Python basics",
                            "status": "todo",
                            "suggested": False,
                            "basis_factor": "Auto-generated...",
                            "youtube_video_id": None,
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
                        "youtube_total": 60,
                        "in_database": 55,
                        "new_videos_to_import": 5,
                        "pending_reconciliation": 2,
                        "metadata_to_refresh": 55,
                    },
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
                "description": "Upload video file to R2 (multipart/form-data with 'file' field). Moves todo → ready.",
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
                "path": "/api/v1/channels/{channel_id}/videos/{video_id}/schedule",
                "description": "Schedule video(s) on YouTube. Pass video_id='all' to schedule everything in the ready queue.",
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
                "description": "Sync from YouTube: refresh metadata, reconcile scheduled→published, import new videos",
                "request": {"new_category_description": "Optional Gemini instructions"},
                "response": {
                    "ok": True,
                    "synced": 5,
                    "metadata_refreshed": 45,
                    "categories_created": ["Tutorials"],
                    "videos": [{"title": "New Video", "category": "Tutorials", "topic": "..."}],
                },
            },
            {
                "group": "Videos",
                "method": "POST",
                "path": "/api/v1/channels/{channel_id}/videos/updateToDoList",
                "description": "Generate n new video ideas via Gemini based on latest analysis",
                "request": {"n": 5},
                "response": {"status": "generating in background"},
            },
            # -- Analysis --
            {
                "group": "Analysis",
                "method": "GET",
                "path": "/api/v1/channels/{channel_id}/analysis/latest",
                "description": "Get latest analysis with analysis_status counts",
                "request": None,
                "response": {
                    "channel_id": "ch1",
                    "version": 2,
                    "category_analysis": [
                        {"category": "Tutorials", "score": 85.5, "reasoning": "High engagement"},
                    ],
                    "best_posting_times": [
                        {"day_of_week": "Monday", "times": ["14:00", "18:00"], "video_count": 2},
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
                "description": "Trigger a new analysis run using YouTube stats and Gemini",
                "request": None,
                "response": {"channel_id": "ch1", "version": 3, "category_analysis": ["..."]},
            },
            {
                "group": "Analysis",
                "method": "GET",
                "path": "/api/v1/channels/{channel_id}/analysis/history",
                "description": "Get analysis run history",
                "query_params": {
                    "limit": {"type": "integer", "default": 10, "optional": True},
                },
                "request": None,
                "response": [
                    {
                        "channel_id": "ch1",
                        "version": 2,
                        "input_videos": ["..."],
                        "new_video_ids": ["vid1"],
                        "result": {},
                        "total_analysed_count": 50,
                        "batch_count": 2,
                        "created_at": "2024-01-20T14:00:00Z",
                    }
                ],
            },
        ],
    }
