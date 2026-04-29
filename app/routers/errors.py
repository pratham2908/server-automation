from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.models.error import ErrorCreate, ErrorEntry, ErrorUpdate
from app.timezone import now_ist

router = APIRouter(prefix="/api/errors", tags=["errors"])


@router.post("/", response_model=ErrorEntry, status_code=status.HTTP_201_CREATED)
async def create_error(error: ErrorCreate, db: AsyncIOMotorDatabase = Depends(get_db)):
    """Log a new error to the queue."""
    error_id = str(uuid.uuid4())
    error_doc = error.dict()
    error_doc["_id"] = error_id
    error_doc["timestamp"] = now_ist()
    error_doc["resolved"] = False

    await db.errors.insert_one(error_doc)
    return error_doc


@router.get("/", response_model=List[ErrorEntry])
async def list_errors(
    feature: Optional[str] = None,
    resolved: Optional[bool] = False,
    limit: int = Query(50, ge=1, le=100),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """List errors with optional filtering."""
    query = {}
    if feature:
        query["feature"] = feature
    if resolved is not None:
        query["resolved"] = resolved

    cursor = db.errors.find(query).sort("timestamp", -1).limit(limit)
    return await cursor.to_list(length=limit)


@router.patch("/{error_id}", response_model=ErrorEntry)
async def update_error(
    error_id: str, update: ErrorUpdate, db: AsyncIOMotorDatabase = Depends(get_db)
):
    """Update error status (e.g. resolve)."""
    result = await db.errors.find_one_and_update(
        {"_id": error_id}, {"$set": {"resolved": update.resolved}}, return_document=True
    )
    if not result:
        raise HTTPException(status_code=404, detail="Error not found")
    return result


@router.delete("/{error_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_error(error_id: str, db: AsyncIOMotorDatabase = Depends(get_db)):
    """Delete an error from the database."""
    result = await db.errors.delete_one({"_id": error_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Error not found")
    return None


@router.delete("/", status_code=status.HTTP_204_NO_CONTENT)
async def clear_all_resolved(db: AsyncIOMotorDatabase = Depends(get_db)):
    """Delete all resolved errors."""
    await db.errors.delete_many({"resolved": True})
    return None
