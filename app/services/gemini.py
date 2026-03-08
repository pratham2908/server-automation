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
performance data and produce a comprehensive channel analysis.

Each video has:
- **title**: Use this to identify titling patterns that drive performance.
- **content_params**: Custom content dimensions that define what the video is about. Analyze which parameter values and combinations correlate with the best performance.
- **stats**: YouTube performance metrics.

Do NOT rely on description or tags for content analysis — use only title and content_params.

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
  - Score each category from 0-100 based on engagement and retention.
- **content_param_analysis**: For each content parameter dimension:
  - `best_values`: which parameter values correlate with highest performance.
  - `worst_values`: which values underperform.
  - `insight`: a concise explanation of the trend.
- **best_combinations**: The top 3-5 combinations of content_params values that yield the best results. Include music recommendations.
- **Engagement metrics** (in `stats`):
  - `views`, `likes`, `comments` — raw counts.
  - `engagement_rate` — (likes + comments) / views × 100.
  - `like_rate`, `comment_rate` — individual rates.
  - `duration_seconds` — video length.
  - `avg_percentage_viewed` — strongest signal of content quality.
  - `avg_view_duration_seconds`, `estimated_minutes_watched`.
  High `avg_percentage_viewed` is the strongest signal. A video with fewer views
  but high engagement_rate and avg_percentage_viewed is more valuable than one
  with many views but low retention.
- If previous analysis exists, **refine incrementally**."""

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

        if existing_content_params:
            has_video_topic = any("video_topic" in p for p in existing_content_params)
            if has_video_topic:
                used_topics = sorted({
                    p["video_topic"] for p in existing_content_params if p.get("video_topic")
                })
                existing_section += (
                    "\n\n## Already-Used video_topic Values — DO NOT REPEAT\n"
                    "These video_topic values have already been covered. You MUST pick completely "
                    "NEW, UNUSED video_topic values. Do NOT reuse any from this list, "
                    "even with a different ranking_factor or angle.\n"
                    + "\n".join(f"- {t}" for t in used_topics)
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
- If an "Already-Used video_topic Values" list is provided above, you MUST NOT reuse ANY video_topic from that list. Every `video_topic` value must be completely new and never covered before.
- Strictly return a JSON array of objects (`[]`), even if count is 1."""
