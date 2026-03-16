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
        "gemini-3.1-pro-preview",
        "gemini-3-pro-preview",
        "gemini-3-flash-preview",
    ]

    def __init__(self, api_key: str) -> None:
        self._client = genai.Client(api_key=api_key)

    # ------------------------------------------------------------------
    # Internal — model fallback
    # ------------------------------------------------------------------

    async def _generate(self, prompt: str) -> str:
        """Try each model in the fallback chain until one succeeds.

        Returns the raw response text. Raises the last exception if
        every model fails.
        """
        import asyncio
        last_error: Exception | None = None

        for model in self._MODEL_CHAIN:
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
                logger.info("Gemini response from model '%s'", model, extra={"color": "CYAN"})
                return response.text
            except Exception as exc:
                last_error = exc
                is_last = model == self._MODEL_CHAIN[-1]
                if is_last:
                    logger.error(f"🚨 All Gemini models in the fallback chain failed! Last error: {exc}")
                else:
                    logger.warning(
                        "⚠️ Model '%s' failed: %s — trying next fallback",
                        model,
                        exc,
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
        prompt = self._build_analysis_prompt(video_data, previous_analysis, content_schema)
        text = await self._generate(prompt)

        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.error("🚨 Failed to parse JSON from Gemini analysis response: %s", text)
            raise ValueError("Failed to parse Gemini analysis response")

    async def analyze_single_video(
        self,
        video_data: dict[str, Any],
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
        prompt = self._build_single_video_prompt(video_data)
        text = await self._generate(prompt)

        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.error("Failed to parse Gemini per-video analysis response: %s", text)
            raise ValueError("Failed to parse Gemini per-video analysis response")

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
            existing_content_params,
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

        return f"""You are a YouTube channel analytics expert. Analyze the following video
performance data and produce a comprehensive channel summary.

Each video includes:
- **title**: Use this to identify titling patterns that drive performance.
- **content_params**: Custom content dimensions that define what the video is about.
- **stats**: YouTube performance metrics including `subscribers_gained` (how many new subs this video brought) and `views_per_subscriber` (reach beyond existing audience).
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
      "params": {{"simulation_type": "battle", "challenge_mechanic": "1v1", "music": "Epic Orchestral"}},
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
- **best_combinations**: The top 3-5 combinations of content_params values that yield the best results. Include music recommendations.
- **Subscriber-aware analysis**:
  - `subscribers_gained` shows how many new subscribers each video brought. Videos with high subscribers_gained are especially valuable even if view counts are moderate.
  - `views_per_subscriber` above 1.0 means the video reached beyond the existing audience — a strong viral signal.
  - Factor these into category scores and combination rankings.
- **Engagement metrics** (in `stats`):
  - `views`, `likes`, `comments` — raw counts.
  - `engagement_rate` — (likes + comments) / views x 100.
  - `avg_percentage_viewed` — strongest signal of content quality.
  - `avg_view_duration_seconds`, `estimated_minutes_watched`.
- **Per-video AI insights**: Use the `ai_insight` field to identify recurring patterns in what works and what doesn't across videos. Aggregate `key_learnings` into your recommendations.
- If previous analysis exists, **refine incrementally**."""

    @staticmethod
    def _build_single_video_prompt(video_data: dict[str, Any]) -> str:
        return f"""You are a YouTube performance analyst. Analyze this single video's performance data and provide actionable insights.

## Video Data
```json
{json.dumps(video_data, indent=2)}
```

## What Each Metric Means
- **views**: Total view count.
- **likes**, **comments**: Raw engagement counts.
- **engagement_rate**: (likes + comments) / views x 100 — overall interaction rate.
- **avg_percentage_viewed**: Average % of the video watched — strong signal of content quality.
- **avg_view_duration_seconds**: Average watch time per view.
- **estimated_minutes_watched**: Total accumulated watch time.
- **subscribers_gained**: How many new subscribers this specific video brought in.
- **views_per_subscriber**: views / channel subscriber count — reach beyond existing audience. Above 1.0 means the video reached far beyond subscribers.
- **subscriber_count_at_analysis**: The channel's total subscriber count when this analysis was run (for context).

## Scoring Weightage (use exactly these weights for performance_rating)
When computing performance_rating 0-100, weight each factor as follows (total 100%):
- **subscribers_gained**: 25%
- **avg_percentage_viewed**: 25%
- **views**: 20%
- **engagement_rate**: 10%
- **comments**: 8%
- **likes**: 5%
- **views_per_subscriber**: 5%
- **estimated_minutes_watched**: 2%

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
  ]
}}

Guidelines:
- **performance_rating**: Score 0-100 using the exact weightage above. Compute a 0-100 score per dimension, then take the weighted sum. Be consistent so ratings are comparable across videos.
- **what_worked**: Be specific — mention the title style, content_params choices, engagement patterns. Reference actual numbers.
- **what_didnt**: Be honest and constructive. If the video underperformed on a metric, explain why that matters and what could change.
- **key_learnings**: 2-4 concise, actionable takeaways. These will be aggregated across all videos to identify channel-wide patterns."""

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

        return f"""You are a top-tier YouTube content strategist obsessed with virality, click-through rate, and watch time. Generate metadata for {count} completely distinct new videos in the "{category}" category.

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
    "content_params": {{"simulation_type": "battle", "music": "Epic Orchestral - Two Steps From Hell"}},
    "basis_factor": "Reasoning or comparison basis"
  }}
]

## Title Guidelines — Make Them CATCHY
- Titles MUST be scroll-stopping and irresistible. Think about what makes someone click while scrolling.
- Use proven psychological hooks: curiosity gaps ("You Won't Believe..."), strong numbers ("100 vs 1"), superlatives ("The MOST Insane..."), challenges, versus formats, countdowns.
- Reference trending memes, pop culture, or viral formats when it fits naturally.
- Keep titles punchy — ideally under 60 characters. Front-load the hook.
- Study the `best_title_patterns` from category insights and push them further. Don't just copy — evolve the pattern to be even more clickable.
- NEVER use generic or descriptive titles. Every title should create an urge to click.

## Description Guidelines — Optimize for Search & Watch Time
- Open with a bold, attention-grabbing first line (this shows in search results and suggested videos).
- Include relevant keywords naturally for YouTube SEO — think about what viewers would search for.
- Add a brief teaser of what happens in the video without spoiling the payoff (keep them watching).
- Keep it concise but compelling — 2-4 short paragraphs max.
- Include a call-to-action ("Subscribe for more", "Comment your prediction") to drive engagement.

## Tag Guidelines — Maximize Discoverability
- Include 10-15 tags per video.
- Mix broad high-volume tags (e.g. "simulation", "challenge") with specific long-tail tags (e.g. "1v1 battle simulation", "epic tournament challenge").
- Include the category name and key content_params values as tags.
- Add trending/seasonal tags if relevant.
- Order tags from most specific to most broad.

## Other Rules
- Generate exactly {count} completely distinct video ideas. DO NOT repeat titles or topics.
- **content_params**: MUST include values for every parameter in the content schema. ALWAYS include a "music" key with a specific music/audio track recommendation that fits the video's theme and mood.
- **basis_factor**: Provide a short reasoning for why this video idea should perform well.
- For any content param marked as unique above, you MUST NOT reuse ANY value from its "Already-Used" list. Every value for that param must be completely new and never covered before.
- Strictly return a JSON array of objects (`[]`), even if count is 1."""
