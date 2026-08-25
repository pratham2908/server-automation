"""Channel groups — persistence, suggestions, and who a video expands to.

The matching itself is pure and lives in
:mod:`app.services.channel_group_matching`; this is the thin layer that reads the
database, enforces the one-group-per-channel rule, and answers the question every
publishing flow asks: given this channel, who else gets the video?
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.models.channel_group import (
    ChannelGroup,
    ChannelGroupCreate,
    ChannelGroupPublic,
    ChannelGroupUpdate,
    GroupChannel,
    SuggestedGroup,
)
from app.services.channel_group_matching import (
    ChannelIdentity,
    find_matches,
    group_name,
    pick_primary,
)
from app.timezone import now_ist

logger = get_logger(__name__)


class ChannelGroupService:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    async def _channel_map(self) -> dict[str, dict[str, Any]]:
        docs = await self.db.channels.find(
            {}, {"_id": 0, "channel_id": 1, "name": 1, "platform": 1, "handle": 1, "thumbnail_url": 1}
        ).to_list(None)
        return {d["channel_id"]: d for d in docs}

    @staticmethod
    def _to_public(group: ChannelGroup, channels: dict[str, dict[str, Any]]) -> ChannelGroupPublic:
        members = []
        for cid in group.channel_ids:
            doc = channels.get(cid)
            if not doc:
                # A deleted channel must not blank the whole group, so it is shown
                # as itself rather than dropped silently.
                members.append(GroupChannel(channel_id=cid, name=cid, platform="unknown"))
                continue
            members.append(
                GroupChannel(
                    channel_id=cid,
                    name=doc.get("name") or cid,
                    platform=doc.get("platform") or "youtube",
                    handle=doc.get("handle"),
                    thumbnail_url=doc.get("thumbnail_url"),
                    is_primary=cid == group.primary_channel_id,
                )
            )
        # Primary first — it is the channel a video starts on.
        members.sort(key=lambda m: (not m.is_primary, m.channel_id))
        return ChannelGroupPublic(
            group_id=group.group_id,
            name=group.name,
            primary_channel_id=group.primary_channel_id,
            auto_target=group.auto_target,
            channels=members,
            created_at=group.created_at,
            updated_at=group.updated_at,
        )

    async def list_groups(self) -> list[ChannelGroupPublic]:
        channels = await self._channel_map()
        docs = await self.db.channel_groups.find({}).sort("created_at", 1).to_list(None)
        return [self._to_public(ChannelGroup(**d), channels) for d in docs]

    async def group_for(self, channel_id: str) -> ChannelGroup | None:
        doc = await self.db.channel_groups.find_one({"channel_ids": channel_id})
        return ChannelGroup(**doc) if doc else None

    async def expansion_targets(self, channel_id: str) -> list[str]:
        """The other channels a video on *channel_id* should also go to.

        Empty when the channel is in no group, or when its group has auto-target
        switched off — that setting exists so a group can be a label without
        silently changing what gets published.
        """
        group = await self.group_for(channel_id)
        if not group or not group.auto_target:
            return []
        return group.others(channel_id)

    # ------------------------------------------------------------------
    # Suggestions
    # ------------------------------------------------------------------

    async def _repost_pairs(self) -> dict[tuple[str, str], int]:
        """How many videos have been copied from one channel to another already."""
        pairs: dict[tuple[str, str], int] = defaultdict(int)
        reposts = await self.db.videos.find(
            {"is_repost": True, "original_video_id": {"$ne": None}},
            {"_id": 0, "channel_id": 1, "original_video_id": 1},
        ).to_list(None)
        source_ids = list({r["original_video_id"] for r in reposts})
        sources = await self.db.videos.find(
            {"video_id": {"$in": source_ids}}, {"_id": 0, "video_id": 1, "channel_id": 1}
        ).to_list(None)
        source_channel = {s["video_id"]: s["channel_id"] for s in sources}

        for repost in reposts:
            origin = source_channel.get(repost["original_video_id"])
            if origin and origin != repost["channel_id"]:
                pairs[(origin, repost["channel_id"])] += 1
        return dict(pairs)

    async def suggest(self) -> list[SuggestedGroup]:
        """Groups worth creating, excluding channels already in one."""
        channels = await self._channel_map()
        taken: set[str] = set()
        async for doc in self.db.channel_groups.find({}, {"_id": 0, "channel_ids": 1}):
            taken.update(doc["channel_ids"])

        identities = [
            ChannelIdentity(
                channel_id=c["channel_id"],
                name=c.get("name") or c["channel_id"],
                platform=c.get("platform") or "youtube",
                handle=c.get("handle"),
            )
            for cid, c in channels.items()
            if cid not in taken
        ]
        by_id = {c.channel_id: c for c in identities}

        suggestions: list[SuggestedGroup] = []
        for match in find_matches(identities, await self._repost_pairs()):
            members = [by_id[cid] for cid in match.channel_ids]
            primary = pick_primary(members)
            suggestions.append(
                SuggestedGroup(
                    name=group_name(members),
                    primary_channel_id=primary,
                    channel_ids=match.channel_ids,
                    channels=[
                        GroupChannel(
                            channel_id=c.channel_id,
                            name=c.name,
                            platform=c.platform,
                            handle=c.handle,
                            thumbnail_url=(channels.get(c.channel_id) or {}).get("thumbnail_url"),
                            is_primary=c.channel_id == primary,
                        )
                        for c in members
                    ],
                    reason=match.reason,
                    confidence=match.confidence,
                )
            )
        return suggestions

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    async def _assert_available(self, channel_ids: list[str], ignore_group_id: str | None = None) -> None:
        channels = await self._channel_map()
        missing = [cid for cid in channel_ids if cid not in channels]
        if missing:
            raise LookupError(f"No such channel: {', '.join(missing)}")

        query: dict[str, Any] = {"channel_ids": {"$in": channel_ids}}
        if ignore_group_id:
            query["group_id"] = {"$ne": ignore_group_id}
        clash = await self.db.channel_groups.find_one(query)
        if clash:
            overlap = sorted(set(channel_ids) & set(clash["channel_ids"]))
            raise FileExistsError(
                f"{', '.join(overlap)} already belongs to '{clash['name']}' — "
                "a channel can only be in one group, so who else gets a video has one answer"
            )

    async def create(self, payload: ChannelGroupCreate) -> ChannelGroupPublic:
        await self._assert_available(payload.channel_ids)
        group = ChannelGroup(
            group_id=str(uuid.uuid4()),
            name=payload.name,
            primary_channel_id=payload.primary_channel_id,
            channel_ids=payload.channel_ids,
            auto_target=payload.auto_target,
        )
        await self.db.channel_groups.insert_one(group.model_dump())
        logger.info("Created channel group '%s' (%s): %s", group.name, group.group_id, ", ".join(group.channel_ids))
        return self._to_public(group, await self._channel_map())

    async def update(self, group_id: str, payload: ChannelGroupUpdate) -> ChannelGroupPublic:
        doc = await self.db.channel_groups.find_one({"group_id": group_id})
        if not doc:
            raise LookupError(f"Channel group '{group_id}' not found")

        changes = payload.model_dump(exclude_none=True)
        if "channel_ids" in changes:
            await self._assert_available(changes["channel_ids"], ignore_group_id=group_id)

        # Rebuilt through the model so the primary-is-a-member rule is enforced on
        # an edit exactly as it is on a create.
        group = ChannelGroup(**{**doc, **changes, "updated_at": now_ist()})
        await self.db.channel_groups.update_one({"group_id": group_id}, {"$set": group.model_dump()})
        return self._to_public(group, await self._channel_map())

    async def delete(self, group_id: str) -> dict[str, Any]:
        result = await self.db.channel_groups.delete_one({"group_id": group_id})
        if not result.deleted_count:
            raise LookupError(f"Channel group '{group_id}' not found")
        logger.info("Deleted channel group %s", group_id)
        # Videos are untouched: a group decides where new videos go, and unlinking
        # must not rewrite what has already been published.
        return {"ok": True, "group_id": group_id, "deleted": True}
