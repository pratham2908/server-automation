from __future__ import annotations

"""Gemini AI service – channel analysis and video content generation.

Uses the ``google-genai`` SDK to interact with the Gemini API.
"""

import json
import logging
from typing import Any

from google import genai
from google.genai import types

from app.logger import get_logger

logger = get_logger(__name__)


class GeminiService:
    """Provides AI-powered analysis and content generation via Gemini."""

    # Model fallback chain — tried in order. If a model fails, the next
    # one is attempted.  Edit this list to change priority.
    _MODEL_CHAIN = [
        "gemini-3-flash-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
    ]

    def __init__(self, api_key: str) -> None:
        self._client = genai.Client(api_key=api_key)

    # ------------------------------------------------------------------
    # Internal — model fallback
    # ------------------------------------------------------------------

    async def _generate(self, prompt: str, specific_model: str | None = None) -> str:
        """Try each model in the fallback chain until one succeeds."""
        import asyncio
        from app.services.metrics import metrics_service
        import time
        
        last_error: Exception | None = None
        models_to_try = [specific_model] if specific_model else self._MODEL_CHAIN

        for model in models_to_try:
            start_time = time.time()
            try:
                # Use the async client and enforce a 90s timeout
                response = await asyncio.wait_for(
                    self._client.aio.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                        ),
                    ),
                    timeout=90.0,
                )
                duration = (time.time() - start_time) * 1000
                metrics_service.record_ai_call(model, duration, "success")
                logger.info("Gemini response from model '%s' (%.2fms)", model, duration, extra={"color": "CYAN"})
                return response.text
            except Exception as exc:
                duration = (time.time() - start_time) * 1000
                metrics_service.record_ai_call(model, duration, "error")
                last_error = exc
                is_last = model == models_to_try[-1]
                if is_last:
                    logger.error(f"🚨 All Gemini models tried failed! Last error: {exc}")
                else:
                    logger.warning(
                        "⚠️ Model '%s' failed (%.2fms): %s — trying next fallback",
                        model, duration, exc,
                    )

        raise last_error  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def analyze_videos(
        self,
        video_data: list[dict[str, Any]],
        previous_analysis: dict[str, Any] | None = None,
        content_schema: list[dict[str, Any]] | None = None,
        platform: str = "youtube",
    ) -> dict[str, Any]:
        """Send video metadata + stats to Gemini and get an updated analysis.

        Parameters
        ----------
        video_data:
            List of dicts, each containing title, content_params, category,
            and YouTube performance metrics for a single video.
        previous_analysis:
            The existing analysis document (if any) so Gemini can refine
            its recommendations incrementally.
        content_schema:
            The channel's content parameter definitions for dimension-level analysis.

        Returns
        -------
        dict
            Updated analysis JSON matching the ``Analysis`` schema
            (best_posting_times, category_analysis, content_param_analysis, best_combinations).
        """
        logger.info("Starting Gemini analysis for %d videos", len(video_data))
        prompt = self._build_analysis_prompt(video_data, previous_analysis, content_schema, platform)
        text = await self._generate(prompt)

        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.error("🚨 Failed to parse JSON from Gemini analysis response: %s", text)
            raise ValueError("Failed to parse Gemini analysis response")

    async def analyze_single_video(
        self,
        video_data: dict[str, Any],
        platform: str = "youtube",
    ) -> dict[str, Any]:
        """Analyze a single video's performance and produce AI insights.

        Parameters
        ----------
        video_data:
            Dict with title, category, content_params, and stats
            (including subscribers_gained, views_per_subscriber).

        Returns
        -------
        dict
            ``{"performance_rating": 0-100, "what_worked": "...", "what_didnt": "...", "key_learnings": [...]}``
        """
        prompt = self._build_single_video_prompt(video_data, platform)
        text = await self._generate(prompt)

        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.error("Failed to parse Gemini per-video analysis response: %s", text)
            raise ValueError("Failed to parse Gemini per-video analysis response")

    async def cluster_video_topics(
        self,
        videos: list[dict[str, Any]],
        platform: str = "youtube",
    ) -> list[dict[str, Any]]:
        """Cluster a list of video titles into distinct, proven concepts.
        
        Parameters
        ----------
        videos:
            List of dicts with ``video_id``, ``title``, and ``views``.
            
        Returns
        -------
        list[dict]
            A list of topic groups, each with a name, description, and the
            indices of the videos that belong to it.
        """
        logger.info("Clustering %d videos into topics via Gemini", len(videos))
        prompt = self._build_clustering_prompt(videos, platform)
        text = await self._generate(prompt)

        try:
            result = json.loads(text)
            return result.get("topics", [])
        except (json.JSONDecodeError, TypeError):
            logger.error("Failed to parse Gemini topic clustering response: %s", text)
            return []

    async def generate_video_content(
        self,
        channel_id: str,
        category: str,
        category_analysis: dict[str, Any],
        count: int = 1,
        existing_titles: list[str] | None = None,
        content_schema: list[dict[str, Any]] | None = None,
        content_param_analysis: list[dict[str, Any]] | None = None,
        best_combinations: list[dict[str, Any]] | None = None,
        existing_content_params: list[dict[str, str]] | None = None,
        platform: str = "youtube",
    ) -> list[dict[str, Any]]:
        """Generate titles, descriptions, tags, and content_params for new to-do videos.

        Parameters
        ----------
        channel_id:
            The channel slug.
        category:
            The content category name.
        category_analysis:
            The Gemini-generated insights for this category.
        count:
            The number of distinct videos to generate.
        existing_titles:
            List of titles that have already been generated for this category.
        content_schema:
            The channel's content parameter definitions.
        content_param_analysis:
            Performance insights for each content parameter dimension.
        best_combinations:
            Top-performing parameter combinations.
        existing_content_params:
            Content params from all existing videos — used to avoid repeating
            the same topic/ranking_factor/etc. combinations.

        Returns
        -------
        list[dict]
            ``[{"title": ..., "description": ..., "tags": [...], "content_params": {...}}, ...]``
        """
        logger.info(
            "Generating %d Gemini video ideas for category '%s' (Channel: %s)",
            count,
            category,
            channel_id,
        )
        prompt = self._build_content_prompt(
            channel_id, category, category_analysis, count, existing_titles,
            content_schema, content_param_analysis, best_combinations,
            existing_content_params, platform,
        )
        text = await self._generate(prompt)

        try:
            result = json.loads(text)
            if isinstance(result, list):
                return result
            # Fallback if Gemini returned a single object instead of a list
            return [result]
        except (json.JSONDecodeError, TypeError):
            logger.error(
                "🚨 Failed to parse JSON from Gemini content response: %s", text
            )
            raise ValueError("Failed to parse Gemini content response")

    # ------------------------------------------------------------------
    # Prompt builders
    # ------------------------------------------------------------------

    @staticmethod
    def _build_analysis_prompt(
        video_data: list[dict[str, Any]],
        previous_analysis: dict[str, Any] | None,
        content_schema: list[dict[str, Any]] | None = None,
        platform: str = "youtube",
    ) -> str:
        previous_section = ""
        if previous_analysis:
            previous_section = (
                "\n\n## Previous Analysis\n"
                "Build upon and refine this existing analysis:\n"
                f"```json\n{json.dumps(previous_analysis, indent=2)}\n```"
            )

        schema_section = ""
        if content_schema:
            schema_section = (
                "\n\n## Content Parameter Schema\n"
                "These are the custom dimensions defined for this channel. "
                "Use them to perform content_param_analysis.\n"
                f"```json\n{json.dumps(content_schema, indent=2)}\n```"
            )

        if platform == "instagram":
            persona = "Instagram Reels analytics expert"
            stats_desc = (
                "Instagram Reels performance metrics including `views`, `likes`, `comments`, "
                "`shares`, `saves`, `reach`, and `views_per_subscriber` (reach beyond existing followers)."
            )
        else:
            persona = "YouTube channel analytics expert"
            stats_desc = (
                "YouTube performance metrics including `subscribers_gained` (how many new subs this video brought) "
                "and `views_per_subscriber` (reach beyond existing audience)."
            )

        return f"""You are a {persona}. Analyze the following video
performance data and produce a comprehensive channel summary.

Each video includes:
- **title**: Use this to identify titling patterns that drive performance.
- **content_params**: Custom content dimensions that define what the video is about.
- **stats**: {stats_desc}
- **ai_insight**: A per-video AI analysis with `performance_rating`, `what_worked`, `what_didnt`, and `key_learnings`. Use these to identify channel-wide patterns.

Do NOT rely on description or tags — use only title, content_params, stats, and ai_insight.

## Video Data (Batch)
```json
{json.dumps(video_data, indent=2)}
```
{schema_section}
{previous_section}

## Required Output Format
Return a JSON object with exactly these keys:

{{
  "best_posting_times": [
    {{
      "day_of_week": "monday",
      "video_count": 2,
      "times": ["10:00", "14:00"]
    }}
  ],
  "category_analysis": [
    {{
      "category": "category_name",
      "best_title_patterns": ["pattern1", "pattern2"],
      "score": 85.5
    }}
  ],
  "content_param_analysis": [
    {{
      "param_name": "simulation_type",
      "best_values": ["battle", "survival"],
      "worst_values": ["puzzle"],
      "insight": "Battle simulations get 3x more engagement than puzzle types"
    }}
  ],
  "best_combinations": [
    {{
      "params": {{"simulation_type": "battle", "challenge_mechanic": "1v1"}},
      "reasoning": "This combination yields the highest avg_percentage_viewed at 72%"
    }}
  ]
}}

Guidelines:
- **best_posting_times**: Optimal posting schedule for each day (monday–sunday).
  - `video_count` = how many videos to post on that day.
  - `times` = exactly `video_count` optimal posting times (HH:MM, 24-hour format).
- **category_analysis**: For each content category:
  - Identify the most effective **title patterns** only (no description/tags analysis).
  - Score each category from 0-100 based on engagement, retention, AND subscriber growth impact.
- **content_param_analysis**: For each content parameter dimension:
  - `best_values`: which parameter values correlate with highest performance.
  - `worst_values`: which values underperform.
  - `insight`: a concise explanation of the trend.
- **best_combinations**: The top 3-5 combinations of content_params values that yield the best results.
- **Follower/subscriber-aware analysis**:
  - {"For YouTube: `subscribers_gained` shows how many new subscribers each video brought." if platform == "youtube" else "For Instagram: `reach` and `shares` indicate how far each reel spreads beyond followers."}
  - `views_per_subscriber` above 1.0 means the video reached beyond the existing audience — a strong viral signal.
  - Factor these into category scores and combination rankings.
- **Engagement metrics** (in `stats`):
  - `views`, `likes`, `comments` — raw counts.
  - `engagement_rate` — {"(likes + comments) / views x 100" if platform == "youtube" else "(likes + comments + shares + saves) / reach x 100"}.
  - {"`avg_percentage_viewed` — strongest signal of content quality." if platform == "youtube" else "`reach`, `saves`, `shares` — key Instagram engagement signals."}
  - {"`avg_view_duration_seconds`, `estimated_minutes_watched`." if platform == "youtube" else ""}
- **Per-video AI insights**: Use the `ai_insight` field to identify recurring patterns in what works and what doesn't across videos. Aggregate `key_learnings` into your recommendations.
- If previous analysis exists, **refine incrementally**."""

    @staticmethod
    def _build_clustering_prompt(
        videos: list[dict[str, Any]],
        platform: str = "youtube",
    ) -> str:
        """Build the prompt for strict semantic clustering of video titles."""
        
        video_list_str = "\n".join([
            f"{i}: {v['title']} ({v.get('views', 0)} views)"
            for i, v in enumerate(videos)
        ])
        
        return f"""You are an expert content strategist specializing in niche trend analysis. Your goal is to identify **Proven Content Concepts** by clustering similar videos into distinct topic groups.

## ULTRA-GRANULAR CLUSTERING RULES
You must follow a TWO-TIER hierarchy for every discovered idea:

1. **Category (The Theme)**: Group topics by their broad content type or genre (e.g., "Walking Journeys", "Mass Battles", "Building Challenges").
2. **Topic Name (The Identity)**: This must be surgically precise. It represents the **Exact Proven Idea**.
   - **Entity Identity Rule**: If two videos share a theme but have different locations, specific numbers, or subjects, they are **DIFFERENT** topics.
   - Example (Category: "Walking Journeys"): "India to USA" and "USA to Russia" are **SEPARATE** topics. They must NOT be grouped.
   - Example (Category: "Survival"): "100 Days in Jungle" and "100 Days in Desert" are **SEPARATE** topics.
   - Match only if the **core hook and specific entities** are identical (e.g., "Man walks 27 years" and "This guy walked for 27 years").

## Video Titles to Cluster ({platform.capitalize()})
{video_list_str}

## Required Output Format
Return a JSON object with a single "topics" key containing an array of clusters:

{{
  "topics": [
    {{
      "category": "Broad Category Name",
      "topic_name": "Specific Concept Name (Exact Topic)",
      "description": "Short explanation of why this specific concept is trending or successful",
      "video_indices": [0, 2, 5]
    }}
  ]
}}

Guidelines:
- **category**: A broader grouping that helps organize related concepts.
- **topic_name**: The specific concept match (Exact Topic).
- **description**: Focus on the appeal of the specific concept.
- **video_indices**: Use the index numbers from the list above.
- Every video must be assigned to exactly one topic. If a video is unique, put it in its own topic group.
- Do NOT generate generic categories like "Uncategorized". Be descriptive.
- Ensure the **topic_name** is specific enough that it represents a single "proven idea"."""

    @staticmethod
    def _build_single_video_prompt(video_data: dict[str, Any], platform: str = "youtube") -> str:
        if platform == "instagram":
            persona = "Instagram Reels performance analyst"
            metrics_section = """## What Each Metric Means
- **views**: Total views of the reel.
- **likes**, **comments**: Raw engagement counts.
- **shares**: Number of times the reel was shared — strong viral signal.
- **saves**: Number of times the reel was saved — indicates high-value content.
- **reach**: Number of unique accounts that saw the reel.
- **engagement_rate**: (likes + comments + shares + saves) / reach x 100 — overall interaction rate.
- **views_per_subscriber**: views / follower count — reach beyond existing audience. Above 1.0 means viral reach.
- **subscriber_count_at_analysis**: The account's total follower count when this analysis was run.

## Scoring Weightage (use exactly these weights for performance_rating)
When computing performance_rating 0-100, weight each factor as follows (total 100%):
- **reach**: 25%
- **shares**: 20%
- **saves**: 15%
- **views**: 15%
- **engagement_rate**: 10%
- **comments**: 5%
- **likes**: 5%
- **views_per_subscriber**: 5%"""
        else:
            persona = "YouTube performance analyst"
            metrics_section = """## What Each Metric Means
- **views**: Total view count.
- **likes**, **comments**: Raw engagement counts.
- **engagement_rate**: (likes + comments) / views x 100 — overall interaction rate.
- **avg_percentage_viewed**: Average % of the video watched — strong signal of content quality.
- **avg_view_duration_seconds**: Average watch time per view.
- **estimated_minutes_watched**: Total accumulated watch time.
- **subscribers_gained**: How many new subscribers this specific video brought in.
- **views_per_subscriber**: views / channel subscriber count — reach beyond existing audience. Above 1.0 means the video reached far beyond subscribers.
- **subscriber_count_at_analysis**: The channel's total subscriber count when this analysis was run (for context).

## Audience Retention
If `audience_retention_curve` is passed within the video data, it maps the elapsed time ratio (0.0 to 1.0) to audience watch percent. Use this to pinpoint precisely where audience drops off.

## Scoring Weightage (use exactly these weights for performance_rating)
When computing performance_rating 0-100, weight each factor as follows (total 100%):
- **subscribers_gained**: 25%
- **avg_percentage_viewed**: 25%
- **views**: 20%
- **engagement_rate**: 10%
- **comments**: 8%
- **likes**: 5%
- **views_per_subscriber**: 5%
- **estimated_minutes_watched**: 2%"""

        return f"""You are a {persona}. Analyze this single video's performance data and provide actionable insights.

## Video Data
```json
{json.dumps(video_data, indent=2)}
```

{metrics_section}

For each metric, score that dimension 0-100 based on how strong the value is (relative to typical expectations for this channel/content). Then compute: performance_rating = weighted sum of those dimension scores. Use 0 for any missing metric. This ensures consistent, comparable ratings across videos.

## Required Output Format
Return a JSON object with exactly these keys:

{{
  "performance_rating": 75,
  "what_worked": "Clear explanation of why this video performed well or poorly",
  "what_didnt": "What held this video back or could be improved",
  "key_learnings": [
    "Specific takeaway 1",
    "Specific takeaway 2",
    "Specific takeaway 3"
  ],
  "retention_drop_offs": [
    {{
      "elapsed_percentage": 15.0,
      "drop_percentage": 5.5,
      "reasoning": "Sharp drop observed at 15% elapsed time, likely right after the hook ended."
    }}
  ]
}}

Guidelines:
- **performance_rating**: Score 0-100 using the exact weightage above. Compute a 0-100 score per dimension, then take the weighted sum. Be consistent so ratings are comparable across videos.
- **what_worked**: Be specific — mention the title style, content_params choices, engagement patterns. Reference actual numbers.
- **what_didnt**: Be honest and constructive. If the video underperformed on a metric, explain why that matters and what could change.
- **key_learnings**: 2-4 concise, actionable takeaways. These will be aggregated across all videos to identify channel-wide patterns.
- **retention_drop_offs**: If the `audience_retention_curve` is provided in the video data, analyze it to identify any significant points where viewers click away. `elapsed_percentage` is the relative time in the video (0-100), `drop_percentage` is how much retention was lost at that point, and `reasoning` is your hypothesis for why it happened."""

    @staticmethod
    def _build_content_prompt(
        channel_id: str,
        category: str,
        category_analysis: dict[str, Any],
        count: int,
        existing_titles: list[str] | None,
        content_schema: list[dict[str, Any]] | None = None,
        content_param_analysis: list[dict[str, Any]] | None = None,
        best_combinations: list[dict[str, Any]] | None = None,
        existing_content_params: list[dict[str, str]] | None = None,
        platform: str = "youtube",
    ) -> str:
        existing_section = ""
        if existing_titles:
            existing_section = (
                "\n\n## Existing Videos to Avoid\n"
                "Do NOT generate videos about these explicit topics/titles, as they "
                "already exist. Find completely distinct angles or new topics within the category:\n"
                + "\n".join(f"- {title}" for title in existing_titles)
            )

        unique_param_names: list[str] = []
        if existing_content_params and content_schema:
            for schema_entry in content_schema:
                if not schema_entry.get("unique"):
                    continue
                param_name = schema_entry["name"]
                unique_param_names.append(param_name)
                used_values = sorted({
                    p[param_name] for p in existing_content_params if p.get(param_name)
                })
                if used_values:
                    existing_section += (
                        f"\n\n## Already-Used `{param_name}` Values — DO NOT REPEAT\n"
                        f"These `{param_name}` values have already been covered. You MUST pick completely "
                        f"NEW, UNUSED `{param_name}` values. Do NOT reuse any from this list, "
                        "even with a different angle or combination of other params.\n"
                        + "\n".join(f"- {v}" for v in used_values)
                    )

        params_section = ""
        if content_schema:
            params_section += (
                "\n\n## Content Parameter Schema\n"
                "Each video must include `content_params` with values for these dimensions:\n"
                f"```json\n{json.dumps(content_schema, indent=2)}\n```"
            )
        if content_param_analysis:
            params_section += (
                "\n\n## Content Parameter Performance Insights\n"
                f"```json\n{json.dumps(content_param_analysis, indent=2)}\n```"
            )
        if best_combinations:
            params_section += (
                "\n\n## Best-Performing Combinations\n"
                "Favor these parameter combinations when generating new videos:\n"
                f"```json\n{json.dumps(best_combinations, indent=2)}\n```"
            )

        if platform == "instagram":
            strategist = "top-tier Instagram Reels content strategist obsessed with virality, reach, saves, and shares"
        else:
            strategist = "top-tier YouTube content strategist obsessed with virality, click-through rate, and watch time"

        return f"""You are a {strategist}. Generate metadata for {count} completely distinct new videos in the "{category}" category.

## Category Insights
```json
{json.dumps(category_analysis, indent=2)}
```
{params_section}
{existing_section}

## Required Output Format
Return a JSON array containing exactly {count} objects, with exactly these keys:

[
  {{
    "title": "Catchy, scroll-stopping title",
    "description": "Compelling description optimized for search and engagement",
    "tags": ["tag1", "tag2", "tag3"],
    "content_params": {{"simulation_type": "battle", "challenge_mechanic": "1v1"}},
    "basis_factor": "Reasoning or comparison basis"
  }}
]

## Title Guidelines — Make Them CATCHY
- Titles MUST be scroll-stopping and irresistible. Think about what makes someone click while scrolling.
- Use proven psychological hooks: curiosity gaps ("You Won't Believe..."), strong numbers ("100 vs 1"), superlatives ("The MOST Insane..."), challenges, versus formats, countdowns.
- Reference trending memes, pop culture, or viral formats when it fits naturally.
- {"Keep titles punchy — ideally under 60 characters. Front-load the hook." if platform == "youtube" else "Keep titles concise for Instagram captions — the first line of the caption is the hook."}
- Study the `best_title_patterns` from category insights and push them further. Don't just copy — evolve the pattern to be even more clickable.
- NEVER use generic or descriptive titles. Every title should create an urge to click.

## Description Guidelines — {"Optimize for Search & Watch Time" if platform == "youtube" else "Optimize for Engagement & Reach"}
- Open with a bold, attention-grabbing first line ({"this shows in search results and suggested videos" if platform == "youtube" else "this is the hook that shows before 'more' on Instagram"}).
- {"Include relevant keywords naturally for YouTube SEO — think about what viewers would search for." if platform == "youtube" else "Include relevant hashtags for Instagram discoverability. Use a mix of popular and niche hashtags."}
- Add a brief teaser of what happens in the video without spoiling the payoff (keep them watching).
- Keep it concise but compelling — 2-4 short paragraphs max.
- Include a call-to-action ("{"Subscribe for more" if platform == "youtube" else "Save this for later"}", "Comment your prediction") to drive engagement.

## Tag Guidelines — Maximize Discoverability
- {"Include 10-15 tags per video." if platform == "youtube" else "Include 15-25 hashtags."}
- {"Mix broad high-volume tags (e.g. 'simulation', 'challenge') with specific long-tail tags." if platform == "youtube" else "Mix high-volume hashtags with niche ones for optimal reach."}
- Include the category name and key content_params values as tags.
- Add trending/seasonal tags if relevant.
- {"Order tags from most specific to most broad." if platform == "youtube" else "Place hashtags at the end of the caption or in the first comment."}

## Other Rules
- Generate exactly {count} completely distinct video ideas. DO NOT repeat titles or topics.
- **content_params**: MUST include values for every parameter in the content schema.
- **basis_factor**: Provide a short reasoning for why this video idea should perform well.
- For any content param marked as unique above, you MUST NOT reuse ANY value from its "Already-Used" list. Every value for that param must be completely new and never covered before.
- Strictly return a JSON array of objects (`[]`), even if count is 1."""

    # ------------------------------------------------------------------
    # Multimodal video retention analysis
    # ------------------------------------------------------------------

    async def _generate_with_video(self, video_path: str, prompt: str) -> str:
        """Upload a video file to Gemini, wait for processing, then generate."""
        import asyncio
        from app.services.metrics import metrics_service
        import time

        uploaded_file = await self._client.aio.files.upload(file=video_path)
        logger.info("Uploaded video to Gemini (name=%s, state=%s)", uploaded_file.name, uploaded_file.state)

        try:
            # Poll until the file is ACTIVE (processing complete)
            poll_count = 0
            while uploaded_file.state.name == "PROCESSING":
                poll_count += 1
                if poll_count > 60:
                    raise TimeoutError("Gemini file processing exceeded 5-minute timeout")
                await asyncio.sleep(5)
                uploaded_file = await self._client.aio.files.get(name=uploaded_file.name)

            if uploaded_file.state.name == "FAILED":
                raise RuntimeError(f"Gemini file processing failed: {uploaded_file.state}")

            logger.info("Gemini file ready (state=%s)", uploaded_file.state.name)

            last_error: Exception | None = None
            for model in self._MODEL_CHAIN:
                start_time = time.time()
                try:
                    response = await asyncio.wait_for(
                        self._client.aio.models.generate_content(
                            model=model,
                            contents=[uploaded_file, prompt],
                            config=types.GenerateContentConfig(
                                response_mime_type="application/json",
                            ),
                        ),
                        timeout=180.0,
                    )
                    duration = (time.time() - start_time) * 1000
                    metrics_service.record_ai_call(model, duration, "success")
                    logger.info("Gemini video analysis response from model '%s' (%.2fms)", model, duration, extra={"color": "CYAN"})
                    return response.text
                except Exception as exc:
                    duration = (time.time() - start_time) * 1000
                    metrics_service.record_ai_call(model, duration, "error")
                    last_error = exc
                    is_last = model == self._MODEL_CHAIN[-1]
                    if is_last:
                        logger.error("All Gemini models failed for video analysis: %s", exc)
                    else:
                        logger.warning("Model '%s' failed for video analysis (%.2fms): %s — trying next", model, duration, exc)

            raise last_error  # type: ignore[misc]
        finally:
            try:
                await self._client.aio.files.delete(name=uploaded_file.name)
                logger.info("Deleted Gemini uploaded file %s", uploaded_file.name)
            except Exception as exc:
                logger.warning("Failed to delete Gemini file %s: %s", uploaded_file.name, exc)

    async def analyze_video_retention(
        self,
        video_path: str,
        video_title: str,
        platform: str = "youtube",
        pacing_templates: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Analyze a video file for retention prediction.

        Parameters
        ----------
        video_path:
            Local filesystem path to the video file.
        video_title:
            Title of the video (provides context to the model).
        platform:
            ``"youtube"`` or ``"instagram"``.
        pacing_templates:
            Optional list of proven pacing templates for the channel.

        Returns
        -------
        dict matching the ``RetentionPrediction`` schema.
        """
        prompt = self._build_retention_analysis_prompt(video_title, platform, pacing_templates)
        text = await self._generate_with_video(video_path, prompt)

        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.error("Failed to parse Gemini video retention response: %s", text)
            raise ValueError("Failed to parse Gemini video retention analysis response")

    @staticmethod
    def _build_retention_analysis_prompt(
        video_title: str, 
        platform: str = "youtube",
        pacing_templates: list[dict[str, Any]] | None = None,
    ) -> str:
        if platform == "instagram":
            platform_context = (
                "This is an Instagram Reel. Reels are short-form vertical videos (typically 15-90 seconds). "
                "The first 1-3 seconds are critical — users scroll past instantly if not hooked. "
                "Pacing should be fast with frequent visual changes."
            )
        else:
            platform_context = (
                "This is a YouTube video. Audience retention is the single most important metric for the algorithm. "
                "The first 5 seconds determine whether viewers stay or bounce. "
                "Average retention above 50% is good; above 70% is excellent."
            )

        template_section = ""
        if pacing_templates:
            template_section = (
                "\n\n## Proven Pacing Templates for this Channel\n"
                "The following pacing patterns have historically performed well for this creator. "
                "Use these as a reference to evaluate if the current video's pacing aligns with "
                "proven success patterns:\n"
                f"```json\n{json.dumps(pacing_templates, indent=2)}\n```\n"
                "In your recommendations, note if the video deviates significantly from these templates "
                "in a way that might hurt retention."
            )

        return f"""You are an elite Video Strategist and AI Content Auditor. Your objective is twofold:
1. **Retention Analysis**: Analyze this video file to reverse-engineer its engagement structure and predict audience retention.
2. **"God Mode" Packaging**: Generate high-conversion packaging (Titles, Description, Tags) and identify the single best frame for a thumbnail.

## Video Context
- **Title**: "{video_title}"
- **Platform**: {platform.capitalize()}
- {platform_context}
{template_section}

## Analysis Tasks

1. **Retention Analysis**: Perform a deep-dive analysis on pacing, visual hooks, and narrative flow. Extract exact timestamps of significant visual changes, score the hook, and predict audience retention.
2. **Packaging Optimization**: 
   - **Titles**: Generate 3-5 high-CTR title options. For YouTube, prioritize search intent vs. curiosity gap. For Instagram, prioritize punchy, shareable hooks.
   - **Description**: A search-optimized description. YouTube: SEO-rich, includes keywords. Instagram: Minimalist, hashtag strategy, viral caption.
   - **Tags**: A comprehensive list of tags/hashtags. (Strict: max 500 characters total for YT).
   - **Thumbnail Selection**: Identify the exact timestamp of the most visually "wow" or representative moment for a thumbnail. Provide reasoning.
3. **Retention Curve Prediction**: Based on the pacing and narrative structure, predict the full retention curve. Provide a data point for every 5% interval (0.0, 0.05, 0.1, ..., 1.0) where 1.0 = 100% of viewers.

## Rules

1. **Character Limits (CRITICAL)**:
   - YouTube Title: Max 100 characters.
   - YouTube Tags: Total combined length under 500 characters.
   - Instagram Caption: Optimized for the "more" click.
2. **The 5-Second Rule**: Be hyper-critical of the first 5 seconds. Flag it as HIGH RISK if no visual change occurs.
3. **Objective Data**: Focus on structural data: what happens, when, and how long.
4. **Thumbnail Reasoning**: Explain why that specific timestamp is the best "click-bait" frame (e.g., "High-action collision at 12.4s").

## Required Output Format

Return a JSON object with exactly these keys:

{{
  "predicted_avg_retention_percent": 65.0,
  "predicted_drop_off_points": [
    {{
      "timestamp_seconds": 8.5,
      "reason": "Static talking head with no visual change",
      "severity": 7
    }}
  ],
  "hook_analysis": {{
    "score": 72,
    "risk_level": "medium",
    "notes": ["Strong visual opening at 0.5s"]
  }},
  "pacing_analysis": {{
    "total_scene_cuts": 24,
    "avg_cut_interval_seconds": 3.2,
    "pacing_score": 68,
    "visual_change_timestamps": [
      {{ "timestamp_seconds": 0.0, "description": "Intro", "transition_type": "zoom" }}
    ]
  }},
  "packaging": {{
    "suggested_titles": [
      "I built a $1,000,000 PC in 24 hours",
      "Why your PC is slow (and how to fix it)"
    ],
    "suggested_description": "In this video, we break down...",
    "suggested_tags": ["pc building", "tech", "gaming"],
    "best_thumbnail_timestamp": 12.4,
    "thumbnail_reasoning": "Highest visual drama with primary subject clearly visible"
  }},
  "narrative_structure": "tutorial",
  "recommendations": ["Add B-roll at 45s"],
  "predicted_retention_curve": {{
    "0.0": 1.0,
    "0.05": 0.85,
    "0.1": 0.78,
    "0.15": 0.75,
    "0.2": 0.72,
    "1.0": 0.35
  }}
}}

Be thorough, objective, and data-driven. Predict the curve at 5% intervals exactly."""

    # ------------------------------------------------------------------
    # Comment sentiment classification (for auto-reply)
    # ------------------------------------------------------------------

    async def classify_comment_sentiment(
        self,
        comments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Classify each comment's sentiment for the auto-reply system.

        Returns a list of ``{"comment_id": "...", "sentiment": "positive|negative|neutral|spam"}``.
        """
        batch = [
            {"comment_id": c["comment_id"], "text": c["text"], "author": c.get("author", "")}
            for c in comments
        ]

        prompt = f"""Classify the sentiment of each comment below.

Categories:
- **positive**: Genuine appreciation, excitement, praise, compliments, or love for the content.
- **negative**: Complaints, criticism, dislike, or dissatisfaction.
- **neutral**: Questions, factual statements, or remarks with no clear sentiment.
- **spam**: Self-promotion, gibberish, irrelevant links, or bot-like content.

Comments:
```json
{json.dumps(batch, indent=2)}
```

Return a JSON array with one entry per comment:
[{{"comment_id": "...", "sentiment": "positive|negative|neutral|spam"}}]

Classify every comment. Do not skip any."""

        text = await self._generate(prompt)
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return []

    # ------------------------------------------------------------------
    # Comment sentiment & demand analysis
    # ------------------------------------------------------------------

    async def analyze_comments(
        self,
        comments: list[dict[str, Any]],
        video_title: str,
        platform: str = "youtube",
        previous_analysis: dict[str, Any] | None = None,
        total_previous_comments: int = 0,
    ) -> dict[str, Any]:
        """Analyze a batch of comments and return structured intelligence.

        Parameters
        ----------
        comments:
            List of dicts with at least ``text`` and ``like_count``.
        video_title:
            Title of the video whose comments are being analyzed.
        platform:
            ``"youtube"`` or ``"instagram"``.
        previous_analysis:
            If provided, Gemini refines this existing analysis using
            only the *new* comments (incremental mode).
        total_previous_comments:
            How many comments the previous analysis was based on.
            Helps Gemini weight sentiment proportionally.

        Returns
        -------
        dict matching the ``CommentAnalysisResult`` schema.
        """
        if previous_analysis:
            prompt = self._build_incremental_comment_analysis_prompt(
                comments, previous_analysis, video_title, platform,
                total_previous_comments,
            )
        else:
            prompt = self._build_comment_analysis_prompt(
                comments, video_title, platform,
            )

        text = await self._generate(prompt)

        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.error("Failed to parse Gemini comment analysis response: %s", text)
            raise ValueError("Failed to parse Gemini comment analysis response")

    @staticmethod
    def _build_comment_analysis_prompt(
        comments: list[dict[str, Any]],
        video_title: str,
        platform: str = "youtube",
    ) -> str:
        """Build a fresh (first-time) comment analysis prompt."""

        comments_json = json.dumps(comments, indent=2, default=str)

        return f"""You are an expert audience research analyst and data extraction engine. Your job is to analyze a batch of {platform.capitalize()} comments from the video titled "{video_title}" and extract actionable intelligence.

## Comments ({len(comments)} total)
```json
{comments_json}
```

## Rules

1. **Be Objective**: Ignore spam, self-promotion, bot comments, and irrelevant noise. Focus only on signals related to content quality, missing information, and future requests.
2. **Consolidate**: If multiple people express the same sentiment or ask for the same thing, group it into a single theme and represent its popularity with a higher `signal_strength` score (1-10). Factor in `like_count` — a comment with 50 likes carries more weight than one with 0.
3. **Strict Output**: Respond ONLY with a valid JSON object matching the exact schema below. No markdown, no explanations.

## Required Output Format

Return a JSON object with exactly these keys:

{{
  "sentiment_summary": {{
    "positive_percentage": 65.0,
    "negative_percentage": 15.0,
    "neutral_percentage": 20.0,
    "overall_sentiment": "positive"
  }},
  "what_audience_loves": [
    {{
      "theme": "Clear and detailed explanations",
      "signal_strength": 8,
      "representative_quotes": ["Best tutorial I've ever seen!", "Finally someone explained this properly"],
      "count": 45
    }}
  ],
  "complaints": [
    {{
      "theme": "Audio quality issues",
      "signal_strength": 5,
      "representative_quotes": ["Audio is too quiet", "Hard to hear over the background music"],
      "count": 12
    }}
  ],
  "demands": [
    {{
      "topic": "Cover advanced techniques",
      "signal_strength": 9,
      "demand_type": "content_request",
      "representative_quotes": ["Please do a video on advanced settings!", "When will you cover the pro features?"],
      "count": 67
    }}
  ],
  "content_gaps": [
    "No coverage of advanced workflows",
    "Missing comparison with competitor tools"
  ],
  "trending_topics": [
    "AI integration requests",
    "Mobile-first content demand"
  ],
  "key_insights": [
    "Audience strongly values step-by-step depth over breadth",
    "There is latent demand for a dedicated series on advanced features"
  ]
}}

## Field Guidelines

- **sentiment_summary**: Percentages must sum to 100. `overall_sentiment` is one of: `positive`, `negative`, `neutral`, `mixed`.
- **what_audience_loves**: Themes the audience explicitly praises. 2-4 representative quotes per theme. `signal_strength` 1-10.
- **complaints**: What the audience is unhappy about or criticizes. Same structure.
- **demands**: Specific requests for new content, features, topics, or formats.
  - `demand_type` is one of: `content_request` (new video/topic), `feature_request` (product feature), `topic_request` (specific subject), `format_request` (style/length/format change).
- **content_gaps**: Topics or information the audience expected but didn't find.
- **trending_topics**: Emerging themes or topics that appear to be gaining traction.
- **key_insights**: 3-5 high-level strategic takeaways from the comment analysis.

Be thorough but concise. Every theme must have real evidence from the comments."""

    @staticmethod
    def _build_incremental_comment_analysis_prompt(
        new_comments: list[dict[str, Any]],
        previous_analysis: dict[str, Any],
        video_title: str,
        platform: str = "youtube",
        total_previous_comments: int = 0,
    ) -> str:
        """Build an incremental (refinement) comment analysis prompt."""

        comments_json = json.dumps(new_comments, indent=2, default=str)
        prev_json = json.dumps(previous_analysis, indent=2, default=str)

        return f"""You are an expert audience research analyst. You previously analyzed {total_previous_comments} comments from the {platform.capitalize()} video titled "{video_title}" and produced the analysis below.

## Previous Analysis (based on {total_previous_comments} comments)
```json
{prev_json}
```

## New Comments ({len(new_comments)} additional comments since last analysis)
```json
{comments_json}
```

## Your Task

Refine the previous analysis by incorporating these {len(new_comments)} new comments. The updated analysis should reflect the FULL picture (all {total_previous_comments} previous + {len(new_comments)} new = {total_previous_comments + len(new_comments)} total comments).

### How to Refine

1. **Sentiment**: Adjust percentages proportionally. If previous was based on {total_previous_comments} comments and {len(new_comments)} new ones arrived, weight accordingly.
2. **Themes (loves, complaints)**: If a new comment echoes an existing theme, bump its `signal_strength` and `count`. If it introduces a new theme, add it. Update `representative_quotes` if the new comments have more articulate examples.
3. **Demands**: Same merging logic. If a demand already exists, increase `count` and `signal_strength`. New demands get added.
4. **Content gaps, trending topics, key insights**: Update to reflect the broader picture.

### Rules

- Ignore spam, self-promotion, bot comments.
- Factor in `like_count` for weighting.
- Respond ONLY with a valid JSON object matching the exact schema (same as the previous analysis structure). No markdown, no explanations.
- Return the COMPLETE updated analysis, not a diff.

## Required Output Format

Return a JSON object with exactly these keys:
{{
  "sentiment_summary": {{ "positive_percentage": ..., "negative_percentage": ..., "neutral_percentage": ..., "overall_sentiment": "..." }},
  "what_audience_loves": [ {{ "theme": "...", "signal_strength": 1-10, "representative_quotes": ["..."], "count": N }} ],
  "complaints": [ {{ "theme": "...", "signal_strength": 1-10, "representative_quotes": ["..."], "count": N }} ],
  "demands": [ {{ "topic": "...", "signal_strength": 1-10, "demand_type": "content_request|feature_request|topic_request|format_request", "representative_quotes": ["..."], "count": N }} ],
  "content_gaps": ["..."],
  "trending_topics": ["..."],
  "key_insights": ["..."]
}}"""

    # ------------------------------------------------------------------
    # Thumbnail analysis (multimodal image)
    # ------------------------------------------------------------------

    async def analyze_thumbnail(
        self,
        image_path: str,
        title: str,
        platform: str = "youtube",
    ) -> dict[str, Any]:
        """Analyze a thumbnail image for click-worthiness and visual quality.

        Uses inline image bytes (no file upload API needed for images).
        """
        import asyncio
        import mimetypes
        import time
        from app.services.metrics import metrics_service

        with open(image_path, "rb") as f:
            image_bytes = f.read()

        mime_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"
        image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        prompt = self._build_thumbnail_analysis_prompt(title, platform)

        last_error: Exception | None = None
        for model in self._MODEL_CHAIN:
            start_time = time.time()
            try:
                response = await asyncio.wait_for(
                    self._client.aio.models.generate_content(
                        model=model,
                        contents=[image_part, prompt],
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                        ),
                    ),
                    timeout=90.0,
                )
                duration = (time.time() - start_time) * 1000
                metrics_service.record_ai_call(model, duration, "success")
                logger.info("Gemini thumbnail analysis from '%s' (%.2fms)", model, duration, extra={"color": "CYAN"})

                return json.loads(response.text)
            except Exception as exc:
                duration = (time.time() - start_time) * 1000
                metrics_service.record_ai_call(model, duration, "error")
                last_error = exc
                is_last = model == self._MODEL_CHAIN[-1]
                if is_last:
                    logger.error("All Gemini models failed for thumbnail analysis: %s", exc)
                else:
                    logger.warning("Model '%s' failed for thumbnail analysis (%.2fms): %s — trying next", model, duration, exc)

        raise last_error  # type: ignore[misc]

    @staticmethod
    def _build_thumbnail_analysis_prompt(title: str, platform: str = "youtube") -> str:
        if platform == "instagram":
            platform_context = (
                "This thumbnail is for an Instagram Reel. Instagram thumbnails appear as vertical "
                "squares or 4:5 crops in the grid. Mobile-first: most viewers see them at small sizes "
                "on phone screens. Visual consistency with the creator's feed aesthetic matters."
            )
            aspect_note = "Aspect ratio context: Instagram grid shows 1:1 square crops; Reels cover uses 9:16."
        else:
            platform_context = (
                "This thumbnail is for a YouTube video. YouTube thumbnails are 16:9 (1280x720). "
                "They appear alongside dozens of competing thumbnails in search, suggested, and home feed. "
                "Click-through rate (CTR) is the primary metric — the thumbnail must grab attention in under "
                "1 second at small sizes (120px tall in mobile suggested)."
            )
            aspect_note = "Aspect ratio context: 16:9 landscape. Must be readable at both full size and small mobile preview."

        return f"""You are an elite Thumbnail Analyst and Visual CTR Specialist. Analyze this thumbnail image and provide a comprehensive quality and click-worthiness assessment.

## Context
- **Video Title**: "{title}"
- **Platform**: {platform.capitalize()}
- {platform_context}
- {aspect_note}

## Analysis Dimensions

Score each dimension 0-100 and provide specific observations:

1. **Composition**: Rule of thirds, visual hierarchy, focal point clarity, negative space usage, overall balance.
2. **Text Readability**: If text is present — font size vs thumbnail size, contrast against background, readability at small sizes (120px tall), text placement, character count. If no text, score based on whether text would improve it.
3. **Emotional Impact**: Does the image trigger curiosity, excitement, surprise, or urgency? Facial expressions, dramatic visuals, emotional contrast.
4. **Face Visibility**: If faces are present — size, expression clarity, eye contact with camera, lighting on face. If no faces, evaluate whether adding a face/reaction would help.
5. **Contrast & Color**: Color saturation, brightness, contrast ratio between subject and background, color temperature, visual "pop" factor. Would this stand out in a feed of other thumbnails?

## Rules

1. Be hyper-critical. Most thumbnails are mediocre. Only score above 85 if truly exceptional.
2. Always evaluate mobile readability — if text or details disappear at small sizes, penalize heavily.
3. Compare mentally against top-performing thumbnails in this content's likely niche.
4. Every observation must be specific and actionable.

## Required Output Format

Return a JSON object with exactly these keys:

{{
  "overall_score": 72,
  "composition_score": 80,
  "text_readability_score": 55,
  "emotional_impact_score": 68,
  "face_visibility_score": 85,
  "contrast_color_score": 74,
  "ctr_prediction": 6.5,
  "click_worthiness": "good",
  "strengths": [
    "Strong facial expression creates curiosity",
    "High contrast between subject and background"
  ],
  "weaknesses": [
    "Text is too small to read on mobile — needs to be 2x larger",
    "Background is cluttered and competes with the subject"
  ],
  "recommendations": [
    "Increase text size by 50% and add a dark stroke/shadow for contrast",
    "Simplify the background — use a gradient or blur to isolate the subject",
    "Add a subtle border or glow around the subject to create visual separation"
  ],
  "detailed_analysis": {{
    "composition": "Subject is centered but slightly too small in frame. The rule of thirds is not leveraged — placing the subject at a power point would increase visual interest.",
    "text_elements": "Title text is present in the upper right but at 14pt equivalent size — illegible below 200px thumbnail width. White text on light background without stroke.",
    "color_palette": "Predominantly blue/teal with warm accents. Saturation is moderate. The palette is cohesive but lacks a strong contrast accent color.",
    "subject_focus": "Main subject is clearly identifiable but doesn't dominate the frame. Background elements draw attention away.",
    "mobile_readability": "At typical mobile thumbnail size (120px tall), text is completely illegible and facial expression is barely discernible. Needs bolder, simpler elements."
  }}
}}

## Field Guidelines

- **overall_score**: Weighted average — composition 20%, text 15%, emotion 25%, face 15%, contrast 25%. Adjust if a critical weakness drags everything down.
- **ctr_prediction**: Estimated CTR percentage (0.0-15.0). Average YouTube CTR is 2-5%. Outstanding thumbnails hit 8-12%.
- **click_worthiness**: One of "excellent" (>80), "good" (60-80), "needs_work" (40-59), "poor" (<40).
- **strengths**: 2-4 specific things the thumbnail does well.
- **weaknesses**: 2-4 specific issues to fix, with concrete details.
- **recommendations**: 3-5 actionable improvement suggestions, ordered by expected impact.
- **detailed_analysis**: One paragraph per dimension with specific observations.

Be thorough, specific, and ruthlessly honest. Generic feedback is useless."""

    # ------------------------------------------------------------------
    # Pre-publish scorecard synthesis
    # ------------------------------------------------------------------

    async def generate_scorecard(
        self,
        signals: dict[str, Any],
        platform: str = "youtube",
    ) -> dict[str, Any]:
        """Synthesize multiple pre-publish signals into a unified scorecard.

        *signals* is a dict with optional keys: ``retention``, ``thumbnail``,
        ``title_description``, ``content_params``, ``posting_time``, ``category``,
        ``channel_patterns``.  The Gemini prompt evaluates each present signal and
        produces a combined readiness verdict.
        """
        prompt = self._build_scorecard_prompt(signals, platform)
        text = await self._generate(prompt)

        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.error("Failed to parse Gemini scorecard response: %s", text)
            raise ValueError("Failed to parse Gemini scorecard response")

    @staticmethod
    def _build_scorecard_prompt(signals: dict[str, Any], platform: str = "youtube") -> str:
        signals_json = json.dumps(signals, indent=2, default=str)

        platform_note = (
            "This is a YouTube video. CTR, retention, and SEO matter most."
            if platform == "youtube"
            else "This is an Instagram Reel. Hook speed, visual impact, and hashtag relevance matter most."
        )

        return f"""You are a Pre-publish Content Strategist. You have been given all available pre-publish signals for a video that is about to be published. Your job is to synthesize them into a single readiness scorecard.

## Platform
{platform_note}

## Available Signals
```json
{signals_json}
```

Each signal key may or may not be present. Only score dimensions where data is available. If a signal is missing, note it as "not available" in your assessment rather than penalizing the score.

## Scoring Dimensions

Evaluate each dimension 0-100 based on available signals:

1. **hook_score**: How strong is the opening? (from retention analysis hook data, if available)
2. **retention_score**: Predicted audience retention quality (from retention analysis, if available)
3. **thumbnail_score**: Thumbnail quality and CTR potential (from thumbnail analysis, if available)
4. **title_score**: Title quality — click-worthiness, curiosity gap, length, SEO (evaluate the title directly)
5. **description_score**: Description quality — hook line, keywords, CTA, length (evaluate directly)
6. **content_alignment_score**: How well the video's content params match the channel's proven winning formulas (from channel patterns and content param data)
7. **timing_score**: Whether the planned or likely posting time aligns with the channel's best posting times (if data available)

## Required Output Format

Return a JSON object:

{{
  "overall_score": 72,
  "verdict": "needs_work",
  "dimensions": {{
    "hook": {{"score": 75, "available": true, "note": "Strong visual hook but no audio cue in first 2s"}},
    "retention": {{"score": 68, "available": true, "note": "Predicted 58% retention — pacing drops mid-video"}},
    "thumbnail": {{"score": 82, "available": true, "note": "Good composition, text could be larger"}},
    "title": {{"score": 70, "available": true, "note": "Decent curiosity gap but too long at 72 chars"}},
    "description": {{"score": 60, "available": true, "note": "Missing keywords, weak opening line"}},
    "content_alignment": {{"score": 85, "available": true, "note": "Strong match with top-performing formula"}},
    "timing": {{"score": 90, "available": true, "note": "Publishing on Friday evening — matches best posting window"}}
  }},
  "top_issues": [
    "Predicted retention drop at 45s — add a pattern interrupt or B-roll",
    "Description opening line is generic — lead with the hook or a bold claim",
    "Thumbnail text is 14pt equivalent — increase to 24pt+ for mobile legibility"
  ],
  "publish_recommendation": "This video scores 72/100. The content alignment and timing are strong, but the mid-video pacing and weak description are holding it back. Fix the description opening line and consider adding visual variety around the 45-second mark. The thumbnail is solid but could be improved with larger text. Verdict: publishable with minor fixes.",
  "missing_signals": ["retention analysis not available"]
}}

## Field Guidelines

- **overall_score**: Weighted average of available dimensions. Hook 15%, retention 20%, thumbnail 20%, title 15%, description 10%, content alignment 10%, timing 10%. If a dimension is unavailable, redistribute its weight proportionally.
- **verdict**: One of "ready" (score >= 80), "needs_work" (60-79), "major_issues" (< 60).
- **dimensions**: Each dimension has `score` (0-100), `available` (bool — was data present to evaluate this?), and `note` (1-2 sentence specific assessment).
- **top_issues**: The 3 most impactful things to fix, ordered by expected improvement. Be specific with timestamps, character counts, or exact suggestions. Max 5 items.
- **publish_recommendation**: 2-4 sentence natural language summary. State the score, the strongest and weakest dimensions, the most impactful fix, and a clear verdict.
- **missing_signals**: List of signal types that were not available for evaluation.

Be specific, actionable, and honest. Reference concrete data from the signals."""

    # ------------------------------------------------------------------
    # Content intelligence — video extraction & comparison
    # ------------------------------------------------------------------

    async def extract_video_intelligence(
        self,
        videos: list[dict[str, Any]],
        platform: str = "youtube",
    ) -> list[dict[str, Any]]:
        """Batch-extract hook, CTA, content structure from video metadata.

        Each video dict should have at least ``video_id``, ``title``,
        ``description`` (can be empty), and optionally ``tags``, ``views``,
        ``likes``, ``comments``, ``duration_seconds``.
        """
        videos_json = json.dumps(videos, indent=2, default=str)

        platform_note = (
            "These are YouTube videos. Evaluate hooks, CTAs, and structure through a YouTube lens "
            "(subscribe CTAs, end screens, cards, pinned comments, SEO-driven titles)."
            if platform == "youtube"
            else "These are Instagram Reels. Evaluate hooks, CTAs, and structure through an Instagram lens "
            "(follow CTAs, caption hooks, hashtag strategy, save/share prompts, visual-first pacing)."
        )

        prompt = f"""You are a Content Strategy Analyst. Analyze each video below and extract structured intelligence about its hook technique, CTA strategy, content structure, and title style.

## Platform
{platform_note}

## Videos ({len(videos)} total)
```json
{videos_json}
```

## What to Extract Per Video

For each video, analyze the title and description (and tags if provided) to infer:

1. **hook_type**: The hook technique used in the title/opening. One of: question, bold_claim, visual_shock, story_tease, stat_fact, controversy, direct_address, how_to, listicle_tease, challenge, comparison, other.
2. **hook_description**: 1 sentence describing the specific hook strategy used.
3. **cta_type**: Primary CTA detected in description/title. One of: subscribe, comment, like, link_click, follow, share, save, engage, none.
4. **cta_placement**: Where CTA appears. One of: title, description_top, description_bottom, both, none.
5. **cta_text**: The actual CTA text (or "none" if absent). Keep it brief.
6. **content_structure**: The narrative format. One of: tutorial, listicle, story, comparison, challenge, reaction, vlog, montage, reveal, before_after, educational, entertainment, review, other.
7. **content_pacing**: Inferred pacing from title/description style. One of: fast, medium, slow.
8. **key_topics**: Array of 2-4 topic keywords the video covers.
9. **title_style**: The titling technique. One of: question, how_to, number_list, shock, emotional, curiosity_gap, direct, challenge, comparison, other.
10. **estimated_production**: Inferred production quality. One of: low, medium, high.

## Required Output Format

Return a JSON array with one object per video:

[
  {{
    "video_id": "...",
    "hook_type": "question",
    "hook_description": "Opens with a provocative question that challenges conventional wisdom",
    "cta_type": "subscribe",
    "cta_placement": "description_bottom",
    "cta_text": "Subscribe for more tips!",
    "content_structure": "tutorial",
    "content_pacing": "medium",
    "key_topics": ["productivity", "time management"],
    "title_style": "question",
    "estimated_production": "medium"
  }}
]

## Rules

- Analyze EVERY video in the list. Do not skip any.
- Base your analysis on the title and description text. If description is empty, infer from title alone.
- Be precise with classifications — don't default to "other" unless nothing else fits.
- key_topics should be specific themes, not generic categories."""

        text = await self._generate(prompt)
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.error("Failed to parse video intelligence extraction: %s", text[:500])
            return []

    async def compare_content_patterns(
        self,
        own_videos: list[dict[str, Any]],
        competitor_videos: list[dict[str, Any]],
        platform: str = "youtube",
    ) -> dict[str, Any]:
        """Compare own-channel patterns against competitor patterns.

        Both lists should contain video intelligence entries with extracted
        fields (hook_type, cta_type, etc.) plus performance metrics.
        """
        own_json = json.dumps(own_videos, indent=2, default=str)
        comp_json = json.dumps(competitor_videos, indent=2, default=str)

        platform_note = (
            "Platform: YouTube. Focus on CTR, retention, subscribe CTAs, SEO titles, and watch time."
            if platform == "youtube"
            else "Platform: Instagram Reels. Focus on hook speed, saves, shares, follow CTAs, and visual impact."
        )

        prompt = f"""You are an elite Content Strategy Consultant. You have two datasets: the creator's own videos and their competitors' videos. Both have been analyzed for hooks, CTAs, content structure, and performance.

Your job: find what competitors do that works, what the creator does well, and most importantly — what specific changes would improve the creator's content.

## {platform_note}

## Creator's Own Videos ({len(own_videos)} videos)
```json
{own_json}
```

## Competitor Videos ({len(competitor_videos)} videos)
```json
{comp_json}
```

## Analysis Tasks

1. **Hook Patterns**: Which hook types correlate with highest views/engagement for competitors? Which hooks does the creator use most? What hooks are competitors using successfully that the creator hasn't tried?

2. **CTA Patterns**: What CTAs do top-performing competitor videos use? Where are they placed? How does the creator's CTA strategy compare? What's missing?

3. **Content Structure**: Which content formats (tutorial, listicle, comparison, etc.) perform best for competitors? Which does the creator rely on? What formats are proven winners that the creator hasn't explored?

4. **Title Patterns**: Which title styles correlate with highest views for competitors? How do the creator's title patterns compare?

5. **Action Items**: Based on all the above, what are the top concrete changes the creator should make?

## Required Output Format

Return a JSON object:

{{
  "hook_patterns": {{
    "competitor_winning_hooks": ["specific observation with data, e.g. 'question hooks avg 2.3x more views than direct titles'"],
    "your_best_hooks": ["what works for the creator with evidence"],
    "hooks_to_try": ["specific hook types competitors use successfully that the creator hasn't tried"],
    "hooks_to_avoid": ["hooks that underperform for both creator and competitors"]
  }},
  "cta_patterns": {{
    "competitor_ctas": ["what top competitors do for CTAs"],
    "your_cta_gaps": ["what the creator is missing in their CTA strategy"],
    "recommendations": ["specific CTA improvements to make"]
  }},
  "content_structure": {{
    "competitor_winning_formats": ["which formats drive highest views/engagement for competitors"],
    "your_strengths": ["which formats work best for the creator"],
    "gaps": ["proven formats the creator hasn't explored"]
  }},
  "title_patterns": {{
    "competitor_patterns": ["what title styles correlate with high performance"],
    "your_patterns": ["the creator's title tendencies and their performance"],
    "suggestions": ["specific title style changes to test"]
  }},
  "top_action_items": [
    "Ranked list of 5-7 specific, actionable changes the creator should make, ordered by expected impact. Each should reference concrete data from the analysis."
  ],
  "overall_gap_score": 35
}}

## Field Guidelines

- **All observations must cite data** — don't say "question hooks work well," say "question hooks avg 150k views vs 80k for direct titles across 12 competitor videos."
- **top_action_items**: 5-7 items, ordered by expected impact. Each must be a specific action (not vague advice).
- **overall_gap_score**: 0-100, where 0 = creator is doing everything competitors do (no gap), 100 = massive gaps across all dimensions. This measures how much room for improvement exists based on competitor patterns.
- Be brutally honest. The creator wants to know what to change, not be reassured."""

        text = await self._generate(prompt)
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.error("Failed to parse content pattern comparison: %s", text[:500])
            raise ValueError("Failed to parse content pattern comparison response")
