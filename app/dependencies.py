"""FastAPI dependencies shared across routers."""

from fastapi import Header, HTTPException, status

from app.config import get_settings


async def verify_api_key(x_api_key: str = Header(...)) -> str:
    """Validate the ``X-API-Key`` header against the configured secret.

    Returns the key on success so downstream handlers can identify the caller
    if needed in the future.
    """
    settings = get_settings()
    if x_api_key != settings.API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    return x_api_key
