from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.config import get_settings
from app.database import get_db
from app.dependencies import verify_api_key_flexible as verify_api_key
from app.services.storage_usage import get_cached_usage, refresh_storage_usage

router = APIRouter(prefix="/api/v1/storage", tags=["storage"], dependencies=[Depends(verify_api_key)])


@router.get("/usage")
async def storage_usage(db: AsyncIOMotorDatabase = Depends(get_db)):
    """Total R2 storage as of the last daily check (cached; never lists the bucket)."""
    return await get_cached_usage(db)


@router.post("/usage/refresh")
async def refresh_usage(db: AsyncIOMotorDatabase = Depends(get_db)):
    """Re-measure now. Lists the whole bucket, so it takes a few seconds."""
    from app.main import r2_service

    if not r2_service:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="R2 not initialised")
    return await refresh_storage_usage(db, r2_service, get_settings())
