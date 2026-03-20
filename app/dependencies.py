"""FastAPI dependencies shared across routers."""

from fastapi import Header, HTTPException, status, Request
from app.config import get_settings
from app.logger import get_logger

logger = get_logger(__name__)


async def verify_api_key(request: Request, x_api_key: str = Header(None)) -> str:
    """Validate the ``X-API-Key`` header against the configured secret.

    Returns the key on success so downstream handlers can identify the caller
    if needed in the future.
    """
    settings = get_settings()
    if x_api_key != settings.API_KEY:
        client_host = request.client.host if request.client else "unknown"
        logger.warning(
            "Unauthorized request from %s: %s %s - [X-API-Key: %s...]",
            client_host, request.method, request.url.path, x_api_key[:4] if x_api_key else "None"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    return x_api_key
