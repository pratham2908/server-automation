"""FastAPI dependencies shared across routers."""

import jwt
from fastapi import Depends, Header, HTTPException, Query, Request, status
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError

from app.config import get_settings
from app.database import get_db
from app.logger import get_logger
from app.models.profile import ProfileInDB

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
            client_host,
            request.method,
            request.url.path,
            x_api_key[:4] if x_api_key else "None",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    return x_api_key


async def verify_api_key_flexible(
    request: Request,
    x_api_key: str | None = Header(None),
    api_key: str | None = Query(None),
) -> str:
    """Accept API key via ``X-API-Key`` or ``?api_key=`` (for HTML pages that cannot set headers)."""

    settings = get_settings()
    key = (x_api_key or api_key or "").strip() or None
    if not key or key != settings.API_KEY:
        client_host = request.client.host if request.client else "unknown"
        logger.warning(
            "Unauthorized request from %s: %s %s",
            client_host,
            request.method,
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    return key


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


async def get_current_profile(token: str = Depends(oauth2_scheme), db=Depends(get_db)) -> ProfileInDB:
    settings = get_settings()
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    from app.services.auth import ALGORITHM

    try:
        payload = jwt.decode(token, settings.API_KEY, algorithms=[ALGORITHM])
        from typing import cast

        profile_id = cast(str, payload.get("sub"))

        if profile_id is None:
            raise credentials_exception
    except InvalidTokenError:
        raise credentials_exception

    profile = await db.profiles.find_one({"id": profile_id})
    if profile is None:
        raise credentials_exception
    return ProfileInDB(**profile)
