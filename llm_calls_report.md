# LLM Calls Report

This document detail all internal LLM (Large Language Model) calls within the automation-server codebase, specifying the models used, where they are triggered, and their exact purpose.

## Gemini Model Fallback System

Most LLM tasks use a tiered fallback chain to ensure high availability and prevent failures due to quota or safety filters.

**Standard Fallback Chain (`_MODEL_CHAIN`):**
1. `gemini-3-flash-preview`
2. `gemini-2.5-pro`
3. `gemini-2.5-flash`

---

## Detailed LLM Invocations

### 1. Channel Performance Analysis
- **Service Method**: `GeminiService.analyze_videos`
- **Model Used**: Standard Fallback Chain
- **Task**: Aggregates performance data from a batch of videos (title, stats, and previous AI insights) to identify channel-wide patterns. It generates optimal posting schedules, updates category scores, and identifies the best combinations of content parameters.

### 2. Individual Video Insights
- **Service Method**: `GeminiService.analyze_single_video`
- **Model Used**: Standard Fallback Chain
- **Task**: Performs a deep-dive analysis of a single video's stats. It calculates a `performance_rating` (0-100) based on weighted metrics (reach, shares, saves, etc.) and provides qualitative feedback on what worked and what could be improved.

### 3. Content Idea Generation
- **Service Method**: `GeminiService.generate_video_content`
- **Model Used**: Standard Fallback Chain
- **Task**: Generates catchy, "scroll-stopping" metadata (titles, descriptions, tags) for new video ideas. It ensures ideas are distinct from existing content and align with top-performing category patterns.

### 4. Multimodal Video Auditing
- **Service Method**: `GeminiService.analyze_video_retention`
- **Model Used**: Standard Fallback Chain
- **Task**: Downloads a video file and uploads it to Gemini for multimodal analysis. It predicts audience retention percentages, identifies specific drop-off timestamps, and analyzes the structural pacing and "5-second hook" risk level.

### 5. Comment Sentiment Classification
- **Service Method**: `GeminiService.classify_comment_sentiment`
- **Model Used**: `gemini-3-flash-preview` 
- **Task**: Classifies individual comments into `positive`, `negative`, `neutral`, or `spam`. This is used by the auto-reply system to identify comments that should receive an automated thank-you response.

### 6. Deep Audience Intelligence
- **Service Method**: `GeminiService.analyze_comments`
- **Model Used**: Standard Fallback Chain
- **Task**: Analyzes a batch of comments to extract high-level intelligence. It identifies "what the audience loves", specific complaints, content gaps, and emerging demands for new topics or features.

### 7. Param Extraction & Categorization (Batch Sync)
- **Location**: `videos.py:_extract_params_and_categorize_batch`
- **Model Used**: `gemini-3-flash-preview` (if Instagram), otherwise Standard Fallback Chain
- **Task**: Used during YouTube/Instagram synchronization to automatically categorize new videos and extract their content parameters from metadata. It attempts to fit videos into existing categories before deriving new ones.

### 8. Manual Parameter Extraction (Ad-hoc)
- **Location**: `videos.py:extract_content_params` (Endpoint: `/extract-params`)
- **Model Used**: `gemini-3-flash-preview` (if Instagram), otherwise Standard Fallback Chain
- **Task**: Extracts content dimension values (e.g., simulation type, music style) for a specific video based on the channel's custom schema.

---

## Technical Observations
- **Instagram Specialization**: For most Instagram-specific tasks (sync, categorization, sentiment), the system defaults to the `gemini-3-flash-preview` model.
- **Structured Data**: All LLM calls enforce a JSON response format (`response_mime_type="application/json"`) to ensure data can be programmatically processed into MongoDB documents.
- **Multimodal Polling**: Video analysis includes a polling mechanism to wait for file processing on Gemini's servers before initiating the generation task.
