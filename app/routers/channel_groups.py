"""Channel groups router — link the channels that carry the same brand.

Reads need only the API key, as everywhere else here. Creating, editing and
deleting a group changes where future videos get published, so those require a
signed-in profile as well.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import get_db
from app.dependencies import get_current_profile, verify_api_key
from app.models.channel_group import (
    ChannelGroupCreate,
    ChannelGroupPublic,
    ChannelGroupUpdate,
    SuggestedGroup,
)
from app.models.profile import ProfileInDB
from app.services.channel_group_service import ChannelGroupService

router = APIRouter(
    prefix="/api/v1/channel-groups",
    tags=["channel-groups"],
    dependencies=[Depends(verify_api_key)],
)


def get_group_service(db: AsyncIOMotorDatabase = Depends(get_db)) -> ChannelGroupService:
    return ChannelGroupService(db)


@router.get("/", response_model=list[ChannelGroupPublic])
async def list_groups(
    service: ChannelGroupService = Depends(get_group_service),
) -> list[ChannelGroupPublic]:
    """Every channel group, members resolved, primary first."""
    return await service.list_groups()


@router.get("/suggestions", response_model=list[SuggestedGroup])
async def suggest_groups(
    service: ChannelGroupService = Depends(get_group_service),
) -> list[SuggestedGroup]:
    """Groups worth creating, for confirmation.

    Never applied on its own: a wrong link would publish a video to a channel it
    does not belong on, so nothing here takes effect until someone accepts it.
    """
    return await service.suggest()


@router.post("/", response_model=ChannelGroupPublic, status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: ChannelGroupCreate,
    service: ChannelGroupService = Depends(get_group_service),
    current_profile: ProfileInDB = Depends(get_current_profile),
) -> ChannelGroupPublic:
    """Link channels together."""
    try:
        return await service.create(payload)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.patch("/{group_id}", response_model=ChannelGroupPublic)
async def update_group(
    group_id: str,
    payload: ChannelGroupUpdate,
    service: ChannelGroupService = Depends(get_group_service),
    current_profile: ProfileInDB = Depends(get_current_profile),
) -> ChannelGroupPublic:
    """Rename a group, change its members, its primary, or its auto-target."""
    try:
        return await service.update(group_id, payload)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.delete("/{group_id}")
async def delete_group(
    group_id: str,
    service: ChannelGroupService = Depends(get_group_service),
    current_profile: ProfileInDB = Depends(get_current_profile),
) -> dict[str, Any]:
    """Unlink a group. Videos already published are untouched."""
    try:
        return await service.delete(group_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
