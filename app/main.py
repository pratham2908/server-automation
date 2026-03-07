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

from app.routers import analysis, categories, channels, migration, posting, videos  # noqa: E402

app.include_router(channels.router)
app.include_router(videos.router)
app.include_router(categories.router)
app.include_router(analysis.router)
app.include_router(posting.router)
app.include_router(migration.router)


@app.get("/health", tags=["health"])
async def health_check():
    """Simple liveness check."""
    return {"status": "ok"}
