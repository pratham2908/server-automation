from fastapi import APIRouter, Depends, Query, HTTPException, status
from typing import List, Dict, Any, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.database import get_db
from app.services.growth_tracking import GrowthTrackingService
from app.routers.observability import verify_api_key

router = APIRouter(prefix="/api/v1/growth", tags=["growth"])

@router.get("/{channel_id}/history", response_model=List[Dict[str, Any]])
async def get_growth_history(
    channel_id: str,
    limit: int = Query(30, ge=1, le=365),
    db: AsyncIOMotorDatabase = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """Get the historical daily growth snapshots for a channel."""
    growth_service = GrowthTrackingService(db)
    history = await growth_service.get_history(channel_id, limit=limit)
    
    # Format for JSON response
    for snap in history:
        if "_id" in snap:
            snap["id"] = str(snap["_id"])
            del snap["_id"]
        if "timestamp" in snap:
            snap["timestamp"] = snap["timestamp"].isoformat()
            
    return history

@router.get("/{channel_id}/velocity", response_model=Dict[str, Any])
async def get_growth_velocity(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """Calculate 7d and 30d growth velocity metrics."""
    growth_service = GrowthTrackingService(db)
    velocity = await growth_service.calculate_velocity(channel_id)
    return velocity

@router.get("/{channel_id}/milestones", response_model=List[int])
async def get_channel_milestones(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """Get all milestones (subscriber counts) reached by the channel."""
    growth_service = GrowthTrackingService(db)
    milestones = await growth_service.get_milestones(channel_id)
    return sorted(milestones)

@router.post("/{channel_id}/snapshot", response_model=Dict[str, Any])
async def trigger_growth_snapshot(
    channel_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    api_key: str = Depends(verify_api_key),
):
    """Force a growth snapshot for a single channel immediately."""
    from app.main import youtube_service_manager, instagram_service_manager
    
    channel = await db.channels.find_one({"channel_id": channel_id})
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
        
    platform = channel.get("platform", "youtube")
    subs, views = 0, 0
    metadata = {}
    
    try:
        if platform == "youtube":
            yt_service = await youtube_service_manager.get_service(channel_id)
            if yt_service:
                info = yt_service.get_channel_info(channel.get("youtube_channel_id", ""))
                subs = info.get("subscriber_count", 0)
                views = info.get("view_count", 0)
                metadata = {"video_count": info.get("video_count", 0)}
        
        elif platform == "instagram":
            ig_service = await instagram_service_manager.get_service(channel_id)
            if ig_service:
                info = ig_service.get_account_info(channel.get("instagram_user_id", ""))
                subs = info.get("followers_count", 0)
                views = 0 
                metadata = {"media_count": info.get("media_count", 0)}
                
        growth_service = GrowthTrackingService(db)
        snapshot = await growth_service.record_snapshot(channel_id, platform, subs, views, metadata)
        
        # Convert timestamp for response
        snapshot["timestamp"] = snapshot["timestamp"].isoformat()
        return snapshot
        
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to record snapshot: {exc}")
