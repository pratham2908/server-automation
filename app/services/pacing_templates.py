"""Pacing Template Service — extracts and matches visual pacing patterns.

Identifies 'pacing templates' from top-performing videos and compares
new analyses against them to provide actionable alignment feedback.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import ValidationError

from app.logger import get_logger
from app.models.retention_analysis import (
    PacingAnalysis,
    PacingMatch,
    PacingTemplate,
)
from app.timezone import now_ist

logger = get_logger(__name__)


class PacingTemplateService:
    """Service for managing and matching video pacing templates."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self.db = db

    async def discover_templates(self, channel_id: str) -> list[PacingTemplate]:
        """Analyze top-performing videos for the channel and identify templates.
        
        A video is considered a candidate if it has an actual performance rating
        above 80 and a completed retention analysis.
        """
        logger.info("Discovering pacing templates for channel: %s", channel_id)
        
        # 1. Fetch top-performing videos with retention analysis
        cursor = self.db.retention_analysis.find({
            "channel_id": channel_id,
            "status": "completed",
            "actual_performance_rating": {"$gte": 75},
        })
        
        candidate_analyses = await cursor.to_list(length=100)
        if not candidate_analyses:
            logger.info("No high-performing videos found for template discovery in channel %s", channel_id)
            return []

        # 2. Group by narrative structure (or cluster)
        groups = defaultdict(list)
        for doc in candidate_analyses:
            structure = doc.get("analysis", {}).get("narrative_structure", "other")
            groups[structure].append(doc)

        templates = []
        for structure, docs in groups.items():
            if len(docs) < 1: # Even 1 can be a template for small channels
                continue
            
            # 3. Compute aggregate metrics for the template
            total_perf = 0.0
            total_avg_interval = 0.0
            total_pacing_score = 0
            
            # Aggregate cut density in deciles
            aggregate_density = [0.0] * 10
            
            for doc in docs:
                total_perf += doc.get("actual_performance_rating", 0)
                pacing = doc.get("analysis", {}).get("pacing_analysis") or {}
                total_avg_interval += pacing.get("avg_cut_interval_seconds", 0)
                total_pacing_score += pacing.get("pacing_score", 0)
                
                # Compute cut density distribution
                duration = doc.get("duration_seconds")
                cuts = pacing.get("visual_change_timestamps", [])
                if duration and duration > 0 and cuts:
                    for cut in cuts:
                        timestamp = cut.get("timestamp_seconds", 0)
                        decile = min(int((timestamp / duration) * 10), 9)
                        aggregate_density[decile] += 1
            
            num_docs = len(docs)
            avg_perf = total_perf / num_docs
            avg_interval = total_avg_interval / num_docs
            avg_pacing_score = total_pacing_score / num_docs
            
            # Normalize density
            total_cuts = sum(aggregate_density)
            if total_cuts > 0:
                aggregate_density = [d / total_cuts for d in aggregate_density]

            template_id = f"{channel_id}-{structure.lower().replace(' ', '-')}"
            name = f"{structure.capitalize()} Pace"
            
            template = PacingTemplate(
                template_id=template_id,
                name=name,
                description=f"Automated template for {structure} structure derived from {num_docs} top videos.",
                target_avg_cut_interval=round(avg_interval, 2),
                target_pacing_score=int(avg_pacing_score),
                cut_density_distribution=aggregate_density,
                avg_performance_rating=round(avg_perf, 2),
                video_count=num_docs,
                updated_at=now_ist()
            )
            
            # Store/Update in db
            await self.db.pacing_templates.update_one(
                {"template_id": template_id, "channel_id": channel_id},
                {"$set": {**template.dict(), "channel_id": channel_id}},
                upsert=True
            )
            templates.append(template)

        logger.success("Discovered %d pacing templates for channel %s", len(templates), channel_id)
        return templates

    async def get_templates(self, channel_id: str) -> list[PacingTemplate]:
        """Retrieve all defined templates for a channel."""
        cursor = self.db.pacing_templates.find({"channel_id": channel_id})
        docs = await cursor.to_list(length=20)
        return [PacingTemplate(**doc) for doc in docs]

    def match_pacing(
        self, 
        analysis: PacingAnalysis, 
        templates: list[PacingTemplate],
        video_duration: float | None = None
    ) -> list[PacingMatch]:
        """Compare a video's pacing against available templates."""
        matches = []
        
        for t in templates:
            # 1. Compare avg cut interval (exponential decay for score)
            import math
            
            # If interval is within 20% of target, high score
            interval_diff = abs(analysis.avg_cut_interval_seconds - t.target_avg_cut_interval)
            # score = 100 * exp(-diff / scale)
            interval_score = 100 * math.exp(-interval_diff / (t.target_avg_cut_interval or 1.0))
            
            # 2. Compare pacing score
            pacing_score_diff = abs(analysis.pacing_score - t.target_pacing_score)
            pacing_score_match = max(0, 100 - (pacing_score_diff * 2))
            
            # 3. Compare density if duration is provided
            density_score = 100.0
            if video_duration and analysis.visual_change_timestamps:
                current_density = [0.0] * 10
                for cut in analysis.visual_change_timestamps:
                    decile = min(int((cut.timestamp_seconds / video_duration) * 10), 9)
                    current_density[decile] += 1
                
                total_cuts = sum(current_density)
                if total_cuts > 0:
                    current_density = [d / total_cuts for d in current_density]
                    
                    # Compute cosine similarity or MSE for density
                    mse = sum((a - b) ** 2 for a, b in zip(current_density, t.cut_density_distribution)) / 10
                    density_score = max(0, 100 * (1 - (mse * 5))) # Heuristic scaling
            
            # Final match score
            total_match_score = int((interval_score * 0.4) + (pacing_score_match * 0.3) + (density_score * 0.3))
            
            if total_match_score > 50: # Only return decent matches
                deviations = []
                recommendations = []
                
                if analysis.avg_cut_interval_seconds > t.target_avg_cut_interval * 1.5:
                    deviations.append(f"Cuts are significantly slower than {t.name} (avg {analysis.avg_cut_interval_seconds:.1f}s vs target {t.target_avg_cut_interval:.1f}s)")
                    recommendations.append(f"Increase cut frequency to match the energetic {t.name} style.")
                elif analysis.avg_cut_interval_seconds < t.target_avg_cut_interval * 0.5:
                    deviations.append(f"Cuts are much faster than {t.name}")
                    recommendations.append("Consider slowing down the pacing to allow viewers to digest the content.")

                matches.append(PacingMatch(
                    template_id=t.template_id,
                    template_name=t.name,
                    match_score=total_match_score,
                    deviations=deviations,
                    recommendations=recommendations
                ))
        
        # Sort by match score descending
        matches.sort(key=lambda x: x.match_score, reverse=True)
        return matches
