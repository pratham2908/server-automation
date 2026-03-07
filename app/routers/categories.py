"""Categories router – CRUD operations for content categories."""

from datetime import datetime
from typing import List, Optional, Union

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import verify_api_key
from app.models.category import CategoryCreate, CategoryUpdate

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/categories",
    tags=["categories"],
    dependencies=[Depends(verify_api_key)],
)


# ------------------------------------------------------------------
# GET /  –  list categories (sorted by score desc)
# ------------------------------------------------------------------


from app.models.category import Category

@router.get("/", response_model=list[Category])
async def list_categories(
    channel_id: str,
    status_filter: Optional[str] = None,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return all categories for *channel_id* sorted by score descending."""
    query: dict = {"channel_id": channel_id}
    if status_filter:
        query["status"] = status_filter

    categories = (
        await db.categories.find(query).sort("score", -1).to_list(length=None)
    )
    for c in categories:
        c["_id"] = str(c["_id"])

    return categories


# ------------------------------------------------------------------
# POST /  –  add one or more categories
# ------------------------------------------------------------------


@router.post("/", status_code=status.HTTP_201_CREATED)
async def add_categories(
    channel_id: str,
    body: Union[CategoryCreate, List[CategoryCreate]],
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Add one or more new categories.

    Accepts a single ``CategoryCreate`` object **or** a list of them.
    """
    items = body if isinstance(body, list) else [body]

    now = datetime.utcnow()
    docs = [
        {
            "channel_id": channel_id,
            "name": item.name,
            "description": item.description,
            "raw_description": item.raw_description,
            "score": item.score,
            "status": "active",
            "video_count": 0,
            "metadata": {"total_videos": 0},
            "created_at": now,
            "updated_at": now,
        }
        for item in items
    ]

    result = await db.categories.insert_many(docs)
    return {
        "ok": True,
        "inserted_count": len(result.inserted_ids),
        "ids": [str(i) for i in result.inserted_ids],
    }


# ------------------------------------------------------------------
# PATCH /{category_id}  –  update a category
# ------------------------------------------------------------------


@router.patch("/{category_id}", response_model=Category)
async def update_category(
    channel_id: str,
    category_id: str,
    body: CategoryUpdate,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Partially update a category."""
    update_data = body.model_dump(exclude_none=True)
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update",
        )

    update_data["updated_at"] = datetime.utcnow()

    try:
        oid = ObjectId(category_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid category_id format",
        )

    result = await db.categories.update_one(
        {"_id": oid, "channel_id": channel_id},
        {"$set": update_data},
    )

    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Category {category_id} not found",
        )

    return {"ok": True, "category_id": category_id}
