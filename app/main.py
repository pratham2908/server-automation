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
youtube_service = None
gemini_service = None

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    """Startup / shutdown lifecycle manager.

    - Connects to MongoDB and creates indexes.
    - Initialises R2, YouTube, and Gemini service singletons.
    - On shutdown, closes the database connection.
    """
    global r2_service, youtube_service, gemini_service

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

    # ---- YouTube ----
    from app.services.youtube import YouTubeService

    try:
        youtube_service = YouTubeService(
            client_id=settings.YOUTUBE_CLIENT_ID,
            client_secret=settings.YOUTUBE_CLIENT_SECRET,
            token_path=settings.YOUTUBE_TOKEN_JSON,
        )
        logger.info("YouTube service initialised")
    except Exception:
        logger.warning(
            "YouTube service failed to initialise – upload endpoints will be unavailable",
            exc_info=True,
        )

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

app = FastAPI(
    title="YouTube Automation Server",
    description="Automated multi-channel YouTube video management",
    version="1.0.0",
    lifespan=lifespan,
)

from app.routers import analysis, categories, channels, posting, videos  # noqa: E402

app.include_router(channels.router)
app.include_router(videos.router)
app.include_router(categories.router)
app.include_router(analysis.router)
app.include_router(posting.router)


@app.get("/health", tags=["health"])
async def health_check():
    """Simple liveness check."""
    return {"status": "ok"}
