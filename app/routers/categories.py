"""Categories router – CRUD operations for content categories."""

import uuid
from typing import List, Optional, Union

from app.timezone import now_ist

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger

logger = get_logger(__name__)

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
        c.pop("_id", None)

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

    now = now_ist()
    docs = [
        {
            "id": str(uuid.uuid4()),
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

    await db.categories.insert_many(docs)
    names = [item.name for item in items]
    logger.success("✅ Created %d category(ies) for channel '%s': %s", len(names), channel_id, ", ".join(names))
    return {
        "ok": True,
        "inserted_count": len(docs),
        "ids": [d["id"] for d in docs],
    }


# ------------------------------------------------------------------
# PATCH /{category_id}  –  update a category
# ------------------------------------------------------------------


@router.patch("/{category_id}")
async def update_category(
    channel_id: str,
    category_id: str,
    body: CategoryUpdate,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Partially update a category and propagate name changes back to videos."""
    update_data = body.model_dump(exclude_none=True)
    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update",
        )

    existing = await db.categories.find_one({"id": category_id, "channel_id": channel_id})
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Category {category_id} not found",
        )

    old_name = existing["name"]
    new_name = update_data.get("name")

    update_data["updated_at"] = now_ist()

    await db.categories.update_one(
        {"id": category_id, "channel_id": channel_id},
        {"$set": update_data},
    )

    if new_name and new_name != old_name:
        await db.videos.update_many(
            {"channel_id": channel_id, "category": old_name},
            {"$set": {"category": new_name, "updated_at": now_ist()}}
        )
        await db.analysis_history.update_many(
            {"channel_id": channel_id, "category": old_name},
            {"$set": {"category": new_name}}
        )
        await db.analysis.update_one(
            {"channel_id": channel_id, "category_analysis.category": old_name},
            {"$set": {"category_analysis.$.category": new_name}},
        )

    if update_data.get("status") == "archived":
        active_count = await db.videos.count_documents(
            {"channel_id": channel_id, "category": new_name or old_name, "status": {"$ne": "todo"}}
        )
        if active_count:
            logger.warning(
                "Category '%s' archived but still has %d non-todo videos (ready/scheduled/published)",
                new_name or old_name,
                active_count,
            )

    if new_name and new_name != old_name:
        logger.success("✅ Renamed category '%s' → '%s' for channel '%s'", old_name, new_name, channel_id)
    elif update_data.get("status") == "archived":
        logger.success("📦 Archived category '%s' for channel '%s'", new_name or old_name, channel_id)
    else:
        logger.success("✅ Updated category '%s' for channel '%s'", new_name or old_name, channel_id)

    return {"ok": True, "category_id": category_id}


# ------------------------------------------------------------------
# DELETE /{category_id}  –  remove a category
# ------------------------------------------------------------------


@router.delete("/{category_id}")
async def delete_category(
    channel_id: str,
    category_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Remove a category and move its videos to 'Uncategorized'."""
    category = await db.categories.find_one({"id": category_id, "channel_id": channel_id})
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Category {category_id} not found",
        )

    cat_name = category["name"]

    await db.videos.update_many(
        {"channel_id": channel_id, "category": cat_name},
        {"$set": {"category": "Uncategorized", "updated_at": now_ist()}}
    )

    await db.analysis_history.update_many(
        {"channel_id": channel_id, "category": cat_name},
        {"$set": {"category": "Uncategorized"}}
    )

    await db.analysis.update_one(
        {"channel_id": channel_id, "category_analysis.category": cat_name},
        {"$set": {"category_analysis.$.category": "Uncategorized"}},
    )

    await db.categories.delete_one({"id": category_id, "channel_id": channel_id})

    from app.services.todo_engine import recompute_category
    uncat = await db.categories.find_one(
        {"channel_id": channel_id, "name": "Uncategorized"}
    )
    if uncat:
        await recompute_category(channel_id, "Uncategorized", db)

    logger.success("🗑️ Deleted category '%s' for channel '%s' — videos moved to Uncategorized", cat_name, channel_id)
    return {"ok": True, "category_id": category_id, "deleted": True}
