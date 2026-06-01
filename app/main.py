"""FastAPI application entry-point.

Configures the app, lifespan events (DB + service init), and router mounting.
"""

import logging

from app.logger import setup_root_logging

setup_root_logging()

logging.basicConfig(level=logging.INFO)
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.database import close_db, connect_db

# Services – initialised during lifespan, accessible to routers.
r2_service = None
youtube_service_manager = None
instagram_service_manager = None
gemini_service = None
_auto_publisher_task = None
_youtube_uploader_task = None
_velocity_booster_task = None
_comment_analysis_task = None
_comment_reply_task = None
_sync_analysis_task = None
_growth_tracking_task = None
_metrics_persistence_task = None
_batch_analysis_task = None

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

    import asyncio

    from app.services.error_reporting import (
        bind_error_queue_db,
        create_monitored_task,
        install_loop_exception_handler,
        report_error,
    )

    bind_error_queue_db(db)
    install_loop_exception_handler()

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

    gemini_service = GeminiService(project=settings.GOOGLE_CLOUD_PROJECT, location=settings.GOOGLE_CLOUD_LOCATION)
    logger.info("Gemini service initialised")

    # ---- Background auto-publisher (Instagram scheduled reels) ----
    from app.services.auto_publisher import run_auto_publisher

    global \
        _auto_publisher_task, \
        _youtube_uploader_task, \
        _comment_analysis_task, \
        _comment_reply_task, \
        _sync_analysis_task
    _auto_publisher_task = create_monitored_task(
        run_auto_publisher(db, r2_service),
        feature="Background: Instagram auto-publisher",
    )
    logger.info("Background auto-publisher (Instagram) started")

    # ---- Background YouTube uploader (queued YouTube videos) ----
    from app.services.youtube_uploader import run_youtube_uploader

    _youtube_uploader_task = create_monitored_task(
        run_youtube_uploader(db, r2_service),
        feature="Background: YouTube uploader",
    )
    logger.info("Background YouTube uploader started")

    # ---- Background Velocity Booster (auto-boost pace if engagement low) ----
    from app.services.velocity_booster import run_velocity_booster

    _velocity_booster_task = create_monitored_task(
        run_velocity_booster(db, youtube_service_manager, instagram_service_manager, gemini_service),
        feature="Background: Velocity booster",
    )

    logger.info("Background Velocity Booster started")

    # ---- Background comment analysis cron (24-hour cycle) ----
    from app.services.comment_analysis_cron import run_comment_analysis_cron

    _comment_analysis_task = create_monitored_task(
        run_comment_analysis_cron(db, youtube_service_manager, instagram_service_manager, gemini_service),
        feature="Background: Comment analysis cron",
    )
    logger.info("Background comment analysis cron started")

    # ---- Background metrics persistence (every hour) ----
    from app.services.metrics import metrics_service

    async def run_metrics_persistence():
        while True:
            await asyncio.sleep(3600)  # 1 hour
            try:
                await metrics_service.persist_snapshot(db)
            except Exception as e:
                logger.error(f"Failed to persist metrics: {e}")
                await report_error(
                    feature="Background: Metrics persistence",
                    message=f"Failed to persist metrics snapshot: {e!s}",
                    exception=e,
                )

    global _metrics_persistence_task
    _metrics_persistence_task = create_monitored_task(
        run_metrics_persistence(),
        feature="Background: Metrics persistence loop",
    )
    logger.info("Background metrics persistence started")

    # ---- Background comment reply cron (every 6 hours) ----
    from app.services.comment_reply_cron import run_comment_reply_cron

    _comment_reply_task = create_monitored_task(
        run_comment_reply_cron(db, youtube_service_manager, instagram_service_manager, gemini_service),
        feature="Background: Comment reply cron",
    )
    logger.info("Background comment reply cron started")

    # ---- Background sync + analysis cron (every 12 hours by default) ----
    from app.services.sync_analysis_cron import run_sync_analysis_cron

    _sync_analysis_task = create_monitored_task(
        run_sync_analysis_cron(db, youtube_service_manager, instagram_service_manager, gemini_service),
        feature="Background: Sync-analysis cron",
    )
    logger.info("Background sync-analysis cron started")

    # ---- Background growth tracking cron (every 24 hours by default) ----
    from app.services.growth_cron import run_growth_tracking_cron

    global _growth_tracking_task
    _growth_tracking_task = create_monitored_task(
        run_growth_tracking_cron(db, youtube_service_manager, instagram_service_manager),
        feature="Background: Growth tracking cron",
    )
    logger.info("Background growth tracking cron started")

    # ---- Sequential batch analysis worker ----
    from app.services.batch_upload_service import run_batch_analysis_worker

    global _batch_analysis_task
    _batch_analysis_task = create_monitored_task(
        run_batch_analysis_worker(db, r2_service, gemini_service),
        feature="Background: Batch analysis worker",
    )
    logger.info("Background batch analysis worker started")

    yield

    # ---- Shutdown ----
    if _metrics_persistence_task:
        _metrics_persistence_task.cancel()
        # Take final snapshot
        try:
            await metrics_service.persist_snapshot(db)
        except Exception:
            pass

    for task in (
        _auto_publisher_task,
        _youtube_uploader_task,
        _velocity_booster_task,
        _comment_analysis_task,
        _comment_reply_task,
        _metrics_persistence_task,
        _sync_analysis_task,
        _growth_tracking_task,
        _batch_analysis_task,
    ):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    from app.services.error_reporting import bind_error_queue_db

    bind_error_queue_db(None)
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

from app.routers import (
    analysis,
    auth,
    batch_upload,
    categories,
    channels,
    comment_analysis,
    comment_replies,
    content_intelligence,
    discovery,
    errors,
    growth,
    observability,
    preview_analysis,
    retention_analysis,
    scorecard,
    sync_analysis,
    system,
    thumbnail_analysis,
    ui,
    videos,
)  # noqa: E402

app.include_router(auth.router)
app.include_router(channels.router)
app.include_router(errors.router)
app.include_router(videos.router)
app.include_router(batch_upload.router)
app.include_router(scorecard.router)
app.include_router(categories.router)
app.include_router(analysis.router)
app.include_router(comment_analysis.router)
app.include_router(comment_analysis.config_router)
app.include_router(comment_replies.router)
app.include_router(comment_replies.config_router)
app.include_router(preview_analysis.router)
app.include_router(retention_analysis.router)
app.include_router(sync_analysis.config_router)
app.include_router(sync_analysis.trigger_router)
app.include_router(thumbnail_analysis.router)
app.include_router(discovery.router)
app.include_router(content_intelligence.router)
app.include_router(ui.router)
app.include_router(system.router)
app.include_router(observability.router)
app.include_router(growth.router)


@app.get("/health", tags=["health"])
async def health_check():
    """Simple liveness check."""
    return {"status": "ok"}


from app.docs.schema import get_api_schema


@app.get("/api/schema", tags=["schema"])
async def api_schema():
    """Full API schema with request/response examples for every endpoint."""
    return get_api_schema()
