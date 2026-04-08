from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import verify_api_key
import app.main as main_app
from app.services.discovery_service import DiscoveryService
from app.models.topic_discovery import TopicDiscoveryResult, DoneTopic

router = APIRouter(
    prefix="/api/v1/discovery",
    tags=["Discovery"],
    dependencies=[Depends(verify_api_key)],
)


def get_discovery_service(db: AsyncIOMotorDatabase = Depends(get_db)) -> DiscoveryService:
    return DiscoveryService(
        db=db,
        gemini_service=main_app.gemini_service,
        youtube_manager=main_app.youtube_service_manager,
        instagram_manager=main_app.instagram_service_manager,
    )


@router.post("/{channel_id}/scan", response_model=TopicDiscoveryResult)
async def trigger_discovery_scan(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    service: DiscoveryService = Depends(get_discovery_service),
):
    """Trigger a fresh discovery scan of all competitors for a channel."""
    # Check if channel exists
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_id}' not found",
        )

    result = await service.discover_proven_ideas(channel_id)
    
    # Persist the latest result
    await db.discovery_results.replace_one(
        {"channel_id": channel_id},
        result.model_dump(),
        upsert=True
    )
    
    return result


@router.get("/{channel_id}/topics", response_model=TopicDiscoveryResult)
async def get_discovered_topics(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return the latest discovered topics for a channel."""
    result = await db.discovery_results.find_one({"channel_id": channel_id})
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No discovery results found for channel '{channel_id}'. Run a /scan first.",
        )
    return result


@router.post("/{channel_id}/topics/done")
async def mark_topic_as_done(
    channel_id: str,
    topic_name: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Mark a topic as done for a channel."""
    done_topic = DoneTopic(channel_id=channel_id, topic_name=topic_name)
    await db.done_topics.update_one(
        {"channel_id": channel_id, "topic_name": topic_name},
        {"$set": done_topic.model_dump()},
        upsert=True
    )
    return {"status": "success", "topic_name": topic_name}


@router.delete("/{channel_id}/topics/done")
async def unmark_topic_as_done(
    channel_id: str,
    topic_name: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Remove a topic from the 'done' list."""
    await db.done_topics.delete_one({"channel_id": channel_id, "topic_name": topic_name})
    return {"status": "success"}


@router.get("/{channel_id}/topics/done", response_model=list[DoneTopic])
async def list_done_topics(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Return all topics marked as done for a channel."""
    return await db.done_topics.find({"channel_id": channel_id}).to_list(length=None)
