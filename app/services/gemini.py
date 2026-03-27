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

    async def _generate(self, prompt: str, specific_model: str | None = None) -> str:
        """Try each model in the fallback chain until one succeeds.
        
        If `specific_model` is provided, it attempts only that model.

        Returns the raw response text. Raises the last exception if
        every model fails.
        """
        import asyncio
        last_error: Exception | None = None

        models_to_try = [specific_model] if specific_model else self._MODEL_CHAIN

        for model in models_to_try:
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
        """Upload a video file to Gemini, wait for processing, then generate.

        Tries each model in the fallback chain. Cleans up the uploaded
        file from Gemini storage after generation (or on failure).
        """
        import asyncio

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
                    logger.info("Gemini video analysis response from model '%s'", model, extra={"color": "CYAN"})
                    return response.text
                except Exception as exc:
                    last_error = exc
                    is_last = model == self._MODEL_CHAIN[-1]
                    if is_last:
                        logger.error("All Gemini models failed for video analysis: %s", exc)
                    else:
                        logger.warning("Model '%s' failed for video analysis: %s — trying next", model, exc)

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

        Returns
        -------
        dict matching the ``RetentionPrediction`` schema.
        """
        prompt = self._build_retention_analysis_prompt(video_title, platform)
        text = await self._generate_with_video(video_path, prompt)

        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.error("Failed to parse Gemini video retention response: %s", text)
            raise ValueError("Failed to parse Gemini video retention analysis response")

    @staticmethod
    def _build_retention_analysis_prompt(video_title: str, platform: str = "youtube") -> str:
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

        return f"""You are an elite Video Retention Analyst and AI Content Auditor. Your objective is to analyze this video file to reverse-engineer its engagement structure and predict audience retention.

## Video Context
- **Title**: "{video_title}"
- **Platform**: {platform.capitalize()}
- {platform_context}

## Analysis Task

Perform a deep-dive analysis on the pacing, visual hooks, and overall narrative or structural flow of this video. Extract exact timestamps of significant visual changes, score the hook, and predict audience retention.

## Rules

1. **The 5-Second Rule**: Be hyper-critical of the first 5 seconds. If there is no significant visual change, motion, or compelling audio hook in this window, flag it as HIGH RISK. Score the hook ruthlessly.

2. **Visual Pacing**: Measure the frequency of scene cuts, major on-screen motion, or prominent visual transitions. Note every significant visual change with its exact timestamp.

3. **Objective Extraction**: Do NOT provide subjective praise. Focus on structural data: what happens, when it happens, and how long it takes. Be specific with timestamps.

4. **Drop-Off Prediction**: Identify moments where viewers are most likely to leave. Common causes: slow pacing, repetitive content, confusing narrative, long static shots, weak payoff after buildup.

5. **Retention Prediction**: Based on the video's structure, pacing, hook quality, and content flow, predict the average percentage of the video that viewers will watch.

## Required Output Format

Return a JSON object with exactly these keys:

{{
  "predicted_avg_retention_percent": 65.0,
  "predicted_drop_off_points": [
    {{
      "timestamp_seconds": 8.5,
      "reason": "Static talking head with no visual change for 6 seconds after hook",
      "severity": 7
    }}
  ],
  "hook_analysis": {{
    "score": 72,
    "risk_level": "medium",
    "first_frame_description": "Close-up of product on white background with bold text overlay",
    "visual_change_within_5s": true,
    "audio_hook_present": true,
    "text_overlay_present": true,
    "notes": [
      "Strong opening visual but audio hook could be sharper",
      "Text overlay appears at 1.2s — good for grabbing scanning viewers"
    ]
  }},
  "pacing_analysis": {{
    "total_scene_cuts": 24,
    "avg_cut_interval_seconds": 3.2,
    "longest_static_segment_seconds": 8.5,
    "pacing_score": 68,
    "visual_change_timestamps": [
      {{
        "timestamp_seconds": 0.0,
        "description": "Opening frame — product reveal with zoom-in",
        "transition_type": "zoom"
      }},
      {{
        "timestamp_seconds": 2.8,
        "description": "Cut to presenter speaking to camera",
        "transition_type": "hard_cut"
      }}
    ]
  }},
  "narrative_structure": "problem-solution",
  "strengths": [
    "Strong opening hook with immediate visual interest",
    "Good pacing in first 30 seconds with frequent cuts"
  ],
  "weaknesses": [
    "Middle section (45s-70s) has extended talking head with no B-roll",
    "No clear payoff or callback to the hook's promise"
  ],
  "recommendations": [
    "Add B-roll or visual overlays during the explanation section (45s-70s)",
    "Insert a pattern interrupt around the 60-second mark to recapture attention",
    "Tighten the ending — current outro drags and will cause late drop-offs"
  ]
}}

## Field Guidelines

- **predicted_avg_retention_percent**: Your best estimate (0-100) of what percentage of the video the average viewer will watch. Be realistic — most YouTube videos average 40-60%. Only exceptional videos hit 70%+.
- **predicted_drop_off_points**: List specific timestamps where significant viewer loss is predicted. Include the reason and severity (1-10). Focus on the 3-5 most impactful points.
- **hook_analysis**: Deep analysis of the first 5 seconds.
  - `score`: 0-100. Below 50 = high risk of immediate bounce. Above 80 = excellent hook.
  - `risk_level`: "low" (score >= 70), "medium" (40-69), "high" (< 40).
  - Include every observable element: what's on screen, audio, text overlays.
- **pacing_analysis**: Every significant visual change gets a timestamp entry.
  - `transition_type`: one of hard_cut, fade, dissolve, zoom, pan, whip, slide, motion_change, other.
  - `pacing_score`: 0-100 based on variety, rhythm, and appropriateness for the content type.
- **narrative_structure**: One of: linear, problem-solution, listicle, tutorial, montage, story-arc, vlog, comparison, reveal, other.
- **strengths / weaknesses / recommendations**: 2-5 items each. Be specific with timestamps. No generic advice.

Be thorough, objective, and data-driven. Every claim must reference a specific moment in the video."""

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

        text = await self._generate(prompt, specific_model="gemini-3-flash-preview")
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
