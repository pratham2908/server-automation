from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.models.error import ErrorCreate, ErrorEntry, ErrorUpdate
from app.timezone import now_ist

router = APIRouter(prefix="/api/v1/errors", tags=["errors"])


@router.post("/", response_model=ErrorEntry, status_code=status.HTTP_201_CREATED)
async def create_error(error: ErrorCreate, db: AsyncIOMotorDatabase = Depends(get_db)):
    """Log a new error to the queue with clubbing support."""
    from app.services.errors import get_error_service

    error_service = get_error_service(db)
    # Using log_error to handle clubbing/grouping
    await error_service.log_error(
        feature=error.feature,
        message=error.message,
        exception=None,  # No exception object here as it's from API
        context=error.context,
    )

    # Find the newly created/updated doc to return it
    # Note: Since log_error might have incremented a count, we return the current state
    doc = await db.errors.find_one(
        {"feature": error.feature, "message": error.message, "resolved": False}
    )
    return doc


@router.get("/", response_model=list[ErrorEntry])
async def list_errors(
    feature: str | None = None,
    resolved: bool | None = False,
    limit: int = Query(50, ge=1, le=100),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """List errors with optional filtering."""
    query: dict[str, Any] = {}

    if feature:
        query["feature"] = feature
    if resolved is not None:
        query["resolved"] = resolved

    cursor = db.errors.find(query).sort("timestamp", -1).limit(limit)
    return await cursor.to_list(length=limit)


@router.patch("/{error_id}", response_model=ErrorEntry)
async def update_error(error_id: str, update: ErrorUpdate, db: AsyncIOMotorDatabase = Depends(get_db)):
    """Update error status (e.g. resolve)."""
    result = await db.errors.find_one_and_update(
        {"_id": error_id}, {"$set": {"resolved": update.resolved}}, return_document=True
    )
    if not result:
        raise HTTPException(status_code=404, detail="Error not found")
    return result


@router.post("/bulk-resolve", status_code=status.HTTP_200_OK)
async def bulk_resolve(error_ids: list[str], db: AsyncIOMotorDatabase = Depends(get_db)):
    """Resolve multiple errors at once."""
    await db.errors.update_many(
        {"_id": {"$in": error_ids}},
        {"$set": {"resolved": True, "updated_at": now_ist()}}
    )
    return {"status": "ok", "resolved_count": len(error_ids)}


@router.delete("/{error_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_error(error_id: str, db: AsyncIOMotorDatabase = Depends(get_db)):
    """Delete an error from the database."""
    result = await db.errors.delete_one({"_id": error_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Error not found")


@router.delete("/", status_code=status.HTTP_204_NO_CONTENT)
async def clear_all_resolved(db: AsyncIOMotorDatabase = Depends(get_db)):
    """Delete all resolved errors."""
    await db.errors.delete_many({"resolved": True})
