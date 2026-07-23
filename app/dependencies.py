"""FastAPI dependencies shared across routers."""

import hashlib
import hmac
import secrets

import jwt
from fastapi import Depends, Header, HTTPException, Query, Request, status
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.config import get_settings
from app.database import get_db
from app.logger import get_logger
from app.models.profile import ProfileInDB

logger = get_logger(__name__)

CHANNEL_API_KEY_PREFIX_LENGTH = 12


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


# ------------------------------------------------------------------
# Channel API keys – scoped credentials for external creator apps
# ------------------------------------------------------------------


def generate_channel_api_key(channel_id: str) -> str:
    """Return a fresh raw channel key.

    Format is ``ckey_{channel_id}_{token}`` so the owning channel is obvious in
    logs and support tickets, with ~192 bits of entropy in the token itself.
    """
    return f"ckey_{channel_id}_{secrets.token_urlsafe(32)}"


def hash_channel_api_key(raw_key: str) -> str:
    """Return the SHA-256 hex digest stored in place of the raw key."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def verify_channel_api_key(
    request: Request,
    channel_id: str,
    x_channel_api_key: str | None = Header(None),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> str:
    """Authenticate a request against the ``channel_id`` in its path.

    The key is scoped to exactly one channel: a valid key for channel A cannot
    reach channel B's routes. Every failure — missing header, unknown channel,
    channel without a key, or wrong key — returns an identical 401 so the
    response cannot be used to enumerate channels.

    Returns the verified ``channel_id`` so handlers can rely on it directly.
    """
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid channel API key",
    )

    presented_key = (x_channel_api_key or "").strip()
    if not presented_key:
        raise unauthorized

    channel = await db.channels.find_one({"channel_id": channel_id}, {"api_key_hash": 1})
    stored_hash = channel.get("api_key_hash") if channel else None
    if not stored_hash:
        raise unauthorized

    if not hmac.compare_digest(stored_hash, hash_channel_api_key(presented_key)):
        client_host = request.client.host if request.client else "unknown"
        logger.warning(
            "Rejected channel key for '%s' from %s: %s %s",
            channel_id,
            client_host,
            request.method,
            request.url.path,
        )
        raise unauthorized

    return channel_id


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
