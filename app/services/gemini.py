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
    ) -> dict[str, Any]:
        """Send video metadata + stats to Gemini and get an updated analysis.

        Parameters
        ----------
        video_data:
            List of dicts, each containing title, category, tags, and
            YouTube performance metrics for a single video.
        previous_analysis:
            The existing analysis document (if any) so Gemini can refine
            its recommendations incrementally.

        Returns
        -------
        dict
            Updated analysis JSON matching the ``Analysis`` schema
            (best_posting_times, category_analysis).
        """
        logger.info("Starting Gemini analysis for %d videos", len(video_data))
        prompt = self._build_analysis_prompt(video_data, previous_analysis)
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
    ) -> list[dict[str, Any]]:
        """Generate multiple titles, descriptions, and tags for new to-do videos.

        Parameters
        ----------
        channel_id:
            The channel slug (e.g., 'officialgeoranking').
        category:
            The content category name.
        category_analysis:
            The Gemini-generated insights for this category (patterns,
            templates, best tags, score).
        count:
            The number of distinct videos to generate.
        existing_titles:
            List of titles that have already been generated for this category.

        Returns
        -------
        list[dict]
            ``[{"title": ..., "description": ..., "tags": [...]}, ...]``
        """
        logger.info(
            "Generating %d Gemini video ideas for category '%s' (Channel: %s)",
            count,
            category,
            channel_id,
        )
        prompt = self._build_content_prompt(
            channel_id, category, category_analysis, count, existing_titles
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
    ) -> str:
        previous_section = ""
        if previous_analysis:
            previous_section = (
                "\n\n## Previous Analysis\n"
                "Build upon and refine this existing analysis:\n"
                f"```json\n{json.dumps(previous_analysis, indent=2)}\n```"
            )

        return f"""You are a YouTube channel analytics expert. Analyze the following video
performance data and produce a comprehensive channel analysis.

## Video Data (Batch)
This is one batch of videos from a larger dataset. Incorporate these new
data points into the analysis, refining your conclusions incrementally.

```json
{json.dumps(video_data, indent=2)}
```
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
      "best_description_template": "template text",
      "best_tags": ["tag1", "tag2"],
      "score": 85.5
    }}
  ]
}}

Guidelines:
- **best_posting_times**: Recommend the optimal posting schedule.
  - `video_count` = the number of videos to post on that day.
  - `times` = an array of exactly `video_count` optimal posting times (HH:MM, 24-hour format).
  - Example: if `video_count` is 1, `times` has 1 entry. If `video_count` is 3, `times` has 3 entries.
  - Include an entry for each day of the week (monday through sunday).
- **category_analysis**: For each content category found in the videos:
  - Identify the most effective title patterns and description templates.
  - List the best-performing tags.
  - Score each category from 0-100 based on overall engagement and performance.
- **Engagement metrics** (available per video in the `stats` object):
  - `views`, `likes`, `comments` — raw counts.
  - `engagement_rate` — (likes + comments) / views × 100.
  - `like_rate` — likes / views × 100.
  - `comment_rate` — comments / views × 100.
  - `duration_seconds` — video length in seconds.
  - `avg_percentage_viewed` — average % of the video watched by viewers (from YouTube Analytics).
  - `avg_view_duration_seconds` — average watch time per view in seconds.
  - `estimated_minutes_watched` — total accumulated watch time in minutes.
  Use ALL of these to judge which categories, title patterns, tags, and video
  lengths drive the best audience retention and engagement — not just raw views.
  High `avg_percentage_viewed` is the strongest signal of content quality.
  A video with fewer views but high engagement_rate and avg_percentage_viewed
  is more valuable than one with many views but low retention.
- If previous analysis exists, **refine it incrementally** — do not discard
  prior insights without good reason. Merge new observations with existing ones."""

    @staticmethod
    def _build_content_prompt(
        channel_id: str,
        category: str,
        category_analysis: dict[str, Any],
        count: int,
        existing_titles: list[str] | None,
    ) -> str:
        existing_section = ""
        if existing_titles:
            existing_section = (
                "\n\n## Existing Videos to Avoid\n"
                "Do NOT generate videos about these explicit topics/titles, as they "
                "already exist. Find completely distinct angles or new topics within the category:\n"
                + "\n".join(f"- {title}" for title in existing_titles)
            )

        return f"""You are a YouTube content strategist. Generate metadata for {count} completely distinct new videos
in the "{category}" category.

## Category Insights
```json
{json.dumps(category_analysis, indent=2)}
```
{existing_section}

## Required Output Format
Return a JSON array containing exactly {count} objects, with exactly these keys:

[
  {{
    "title": "Engaging video title following the best patterns",
    "description": "Full description using the best template",
    "tags": ["tag1", "tag2", "tag3"],
    "basis_factor": "Reasoning or comparison basis"
  }}
]

Guidelines:
- Generate exactly {count} completely distinct video ideas. DO NOT repeat titles or topics.
- The titles should follow the best-performing patterns identified.
- The descriptions should use the best templates but feel natural and unique.
- Include 5-15 relevant tags per video.
- **basis_factor**: If the channel is `officialgeoranking`, this MUST describe the exact data source, logic, or criteria used for the ranking (e.g., "Ranked by GDP using World Bank data", "Ranked by data from UNESCO World Heritage sites"). It should not just restate the title. For other channels, provide a short, generic reason for the suggestion.
- Strictly return a JSON array of objects (`[]`), even if count is 1."""
