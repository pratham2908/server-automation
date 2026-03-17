"""Application settings loaded from environment variables via pydantic-settings."""

from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Central configuration – values are read from a `.env` file or the
    environment.  Every field maps 1-to-1 with the keys in `.env.example`."""

    # Server
    API_KEY: str
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # MongoDB
    MONGODB_URI: str
    MONGODB_DB_NAME: str = "youtube_automation"

    # Cloudflare R2
    R2_ACCOUNT_ID: str
    R2_ACCESS_KEY_ID: str
    R2_SECRET_ACCESS_KEY: str
    R2_BUCKET_NAME: str
    R2_ENDPOINT_URL: str

    # Gemini
    GEMINI_API_KEY: str

    # YouTube (optional — prefer DB config collection; kept for backward compat)
    YOUTUBE_CLIENT_ID: Optional[str] = None
    YOUTUBE_CLIENT_SECRET: Optional[str] = None

    # Scheduling
    TIMEZONE: str = "Asia/Kolkata"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


def get_settings() -> Settings:
    """Factory cached at module level so the `.env` file is read once."""
    return Settings()  # type: ignore[call-arg]
