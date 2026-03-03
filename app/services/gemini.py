from __future__ import annotations

"""Gemini AI service – channel analysis and video content generation.

Uses the ``google-genai`` SDK to interact with the Gemini API.
"""

import json
import logging
from typing import Any

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)


class GeminiService:
    """Provides AI-powered analysis and content generation via Gemini."""

    def __init__(self, api_key: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = "gemini-3.1-pro-preview"

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
        prompt = self._build_analysis_prompt(video_data, previous_analysis)
        response = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )

        try:
            return json.loads(response.text)
        except (json.JSONDecodeError, TypeError):
            logger.error("Gemini returned unparseable analysis: %s", response.text)
            raise ValueError("Failed to parse Gemini analysis response")

    async def generate_video_content(
        self,
        category: str,
        category_analysis: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate title, description, and tags for a new to-do video.

        Parameters
        ----------
        category:
            The content category name.
        category_analysis:
            The Gemini-generated insights for this category (patterns,
            templates, best tags, score).

        Returns
        -------
        dict
            ``{"title": ..., "description": ..., "tags": [...]}``
        """
        prompt = self._build_content_prompt(category, category_analysis)
        response = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )

        try:
            return json.loads(response.text)
        except (json.JSONDecodeError, TypeError):
            logger.error(
                "Gemini returned unparseable content: %s", response.text
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
- If previous analysis exists, **refine it incrementally** — do not discard
  prior insights without good reason. Merge new observations with existing ones."""

    @staticmethod
    def _build_content_prompt(
        category: str,
        category_analysis: dict[str, Any],
    ) -> str:
        return f"""You are a YouTube content strategist. Generate metadata for a new video
in the "{category}" category.

## Category Insights
```json
{json.dumps(category_analysis, indent=2)}
```

## Required Output Format
Return a JSON object with exactly these keys:

{{
  "title": "Engaging video title following the best patterns",
  "description": "Full description using the best template",
  "tags": ["tag1", "tag2", "tag3"]
}}

Guidelines:
- The title should follow the best-performing patterns identified.
- The description should use the best template but feel natural and unique.
- Include 5-15 relevant tags."""
