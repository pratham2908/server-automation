from __future__ import annotations

import asyncio
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.logger import get_logger
from app.models.topic_discovery import CompetitorVideoRef, TopicGroup, TopicDiscoveryResult
from app.services.gemini import GeminiService
from app.services.youtube import YouTubeServiceManager
from app.services.instagram import InstagramServiceManager

logger = get_logger(__name__)


class DiscoveryService:
    """Orchestrates content discovery by scanning competitor performance."""

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        gemini_service: GeminiService,
        youtube_manager: YouTubeServiceManager,
        instagram_manager: InstagramServiceManager,
    ) -> None:
        self._db = db
        self._gemini = gemini_service
        self._youtube_manager = youtube_manager
        self._instagram_manager = instagram_manager

    async def discover_proven_ideas(self, channel_id: str) -> TopicDiscoveryResult:
        """Fetch competitor videos, cluster them into topics, and aggregate stats."""
        
        # 1. Fetch competitors
        competitors = await self._db.competitors.find({"channel_id": channel_id}).to_list(length=None)
        if not competitors:
            logger.info("No competitors found for channel '%s'", channel_id)
            return TopicDiscoveryResult(channel_id=channel_id, topics=[])

        # 2. Fetch videos from competitors (concurrently)
        tasks = []
        for comp in competitors:
            tasks.append(self._fetch_competitor_videos(channel_id, comp))
        
        all_videos_nested = await asyncio.gather(*tasks)
        all_videos: list[CompetitorVideoRef] = [v for sublist in all_videos_nested for v in sublist]
        
        if not all_videos:
            logger.info("No competitor videos found for channel '%s'", channel_id)
            return TopicDiscoveryResult(channel_id=channel_id, topics=[])

        # 3. Cluster via Gemini
        # We only pass title and views to Gemini for clustering
        cluster_input = [
            {"video_id": v.video_id, "title": v.title, "views": v.views}
            for v in all_videos
        ]
        
        # Determine platform based on the parent channel (or just use 'youtube' as default for clustering)
        parent_channel = await self._db.channels.find_one({"channel_id": channel_id})
        platform = parent_channel.get("platform", "youtube") if parent_channel else "youtube"
        
        raw_clusters = await self._gemini.cluster_video_topics(cluster_input, platform=platform)
        
        # 3.5 Fetch done topics to filter
        done_topics_cursor = self._db.done_topics.find({"channel_id": channel_id})
        done_topic_names = {t["topic_name"] for t in await done_topics_cursor.to_list(length=None)}
        
        # 4. Aggregate results into TopicGroup objects
        topics: list[TopicGroup] = []
        for c in raw_clusters:
            topic_name = c.get("topic_name", "Unknown Topic")
            
            # Skip if already done
            if topic_name in done_topic_names:
                logger.info("Skipping done topic: %s", topic_name)
                continue
                
            topic_videos = [all_videos[idx] for idx in c.get("video_indices", []) if idx < len(all_videos)]
            if not topic_videos:
                continue
                
            total_views = sum(v.views for v in topic_videos)
            total_likes = sum(v.likes for v in topic_videos)
            competitor_count = len({v.competitor_name for v in topic_videos})
            
            # Recommendation score: simple formula using views and competitor count
            # Topics found across multiple competitors are "proven"
            score = min(100.0, (total_views / 10000.0) * (competitor_count / 2.0))
            
            topics.append(TopicGroup(
                topic_name=topic_name,
                category=c.get("category", "Uncategorized"),
                description=c.get("description", ""),
                videos=topic_videos,
                total_views=total_views,
                total_likes=total_likes,
                competitor_count=competitor_count,
                channel_id=channel_id,
                recommendation_score=round(score, 1)
            ))
            
            # Persist to DB (optional, but good for caching)
            # We'll handle persistence in the router or here
        
        # Sort by total views descending
        topics.sort(key=lambda x: x.total_views, reverse=True)
        
        return TopicDiscoveryResult(channel_id=channel_id, topics=topics)

    async def _fetch_competitor_videos(self, parent_channel_id: str, competitor: dict) -> list[CompetitorVideoRef]:
        """Fetch and wrap videos from a single competitor."""
        platform = competitor.get("platform", "youtube")
        comp_name = competitor.get("name", "Unknown")
        
        videos: list[CompetitorVideoRef] = []
        
        try:
            if platform == "youtube":
                yt_id = competitor.get("youtube_channel_id")
                if not yt_id: return []
                
                # We use the parent channel's service to fetch public data about the competitor
                service = await self._youtube_manager.get_service(parent_channel_id)
                if not service: return []
                
                raw_vids = service.get_channel_latest_videos(yt_id, max_results=50)
                
                # Fetch more stats (views/likes) for these videos to find the "proven" ones
                video_ids = [v["video_id"] for v in raw_vids]
                stats_map = service.get_video_stats(video_ids)
                
                for rv in raw_vids:
                    vid_stats = stats_map.get(rv["video_id"], {})
                    videos.append(CompetitorVideoRef(
                        video_id=rv["video_id"],
                        title=rv["title"],
                        permalink=f"https://youtube.com/watch?v={rv['video_id']}",
                        published_at=rv["published_at"],
                        views=vid_stats.get("views", 0),
                        likes=vid_stats.get("likes", 0),
                        comments=vid_stats.get("comments", 0),
                        competitor_name=comp_name,
                        platform="youtube"
                    ))
            
            elif platform == "instagram":
                ig_username = competitor.get("instagram_username")
                if not ig_username: return []
                
                service = await self._instagram_manager.get_service(parent_channel_id)
                if not service: return []
                
                parent_channel = await self._db.channels.find_one({"channel_id": parent_channel_id})
                own_ig_id = parent_channel.get("instagram_user_id") if parent_channel else None
                if not own_ig_id: return []
                
                raw_reels = service.discover_competitor_media(own_ig_id, ig_username, max_results=50)
                for rr in raw_reels:
                    videos.append(CompetitorVideoRef(
                        video_id=rr["id"],
                        title=rr["caption"][:100], # Caps as title
                        permalink=rr["permalink"],
                        published_at=rr["published_at"],
                        views=rr.get("views", 0),
                        likes=rr.get("like_count", 0),
                        comments=rr.get("comment_count", 0),
                        competitor_name=comp_name,
                        platform="instagram"
                    ))
                    
        except Exception as exc:
            logger.error("Failed to fetch videos for competitor '%s' (%s): %s", comp_name, platform, exc)
            
        return videos
