# Client Integration Guide — YouTube Automation Server

## Quick Setup

**Base URL**: `http://localhost:8000` (or your production URL)

**Auth Header** (required for all `/api/v1/` endpoints):
```
X-API-Key: <your-api-key>
```

**Timezone**: All timestamps are in IST (GMT+5:30).

---

## Core Workflow

### 1. Define Content Schema (one-time setup per channel)

Before syncing or extracting content params, define what dimensions matter for this channel:

```
PUT /api/v1/channels/{channel_id}/content-schema
```

```json
{
  "content_schema": [
    {"name": "simulation_type", "description": "Type of simulation", "values": ["battle", "survival", "puzzle", "race"]},
    {"name": "challenge_mechanic", "description": "Core challenge format", "values": ["1v1", "tournament", "survival"]},
    {"name": "music", "description": "Background music style", "values": []}
  ]
}
```

### 2. Sync Existing Videos from YouTube

```
POST /api/v1/channels/{channel_id}/videos/sync
```

This fetches all YouTube videos, extracts `content_params` (including music identification) and derives `category` for new ones, refreshes metadata for existing ones, and reconciles scheduled videos. Content params are saved as `"unverified"`.

### 3. Backfill Content Params for Existing Videos

If you set up the content schema after initial sync:

```
POST /api/v1/channels/{channel_id}/videos/extract-params/all
```

This extracts params for every video missing them. Review unverified videos:

```
GET /api/v1/channels/{channel_id}/videos/?content_params_status=unverified
```

### 4. Verify Content Params

Review and verify each video's extracted params:

```
POST /api/v1/channels/{channel_id}/videos/{video_id}/verify-params
```

To correct values while verifying:

```json
{
  "content_params": {
    "simulation_type": "survival",
    "music": "Dramatic Piano - Ludovico Einaudi"
  }
}
```

### 5. Run Analysis

```
POST /api/v1/channels/{channel_id}/analysis/update
```

Analysis now uses `title` + `content_params` + performance `stats` (not description/tags). Returns:
- `category_analysis` — per-category title patterns and scores
- `content_param_analysis` — which parameter values perform best/worst
- `best_combinations` — top-performing parameter combos with reasoning

### 6. Generate To-Do Videos

```
POST /api/v1/channels/{channel_id}/videos/updateToDoList
```

```json
{"n": 5}
```

Generated videos include `content_params` with music recommendations, set to `content_params_status: "verified"`.

### 7. Upload Video File

For a `todo` video, upload the produced video file:

```
POST /api/v1/channels/{channel_id}/videos/{video_id}/upload
Content-Type: multipart/form-data
file: <video.mp4>
```

Status changes from `todo` → `ready`, video is added to the ready queue.

### 8. Schedule on YouTube

Schedule a single video:
```
POST /api/v1/channels/{channel_id}/videos/{video_id}/schedule
```

Schedule all ready videos:
```
POST /api/v1/channels/{channel_id}/videos/all/schedule
```

Videos are uploaded to YouTube as private with a computed `publishAt` time. Status → `scheduled`.

### 9. Reconcile Published Videos

Run sync periodically to check if scheduled videos have gone live:

```
POST /api/v1/channels/{channel_id}/videos/sync
```

Videos confirmed public on YouTube are marked `published`.

---

## Useful Query Filters

| Filter | Usage |
|--------|-------|
| `?status_filter=todo` | List only to-do videos |
| `?status_filter=scheduled` | List only scheduled videos |
| `?content_params_status=unverified` | Videos needing param review |
| `?content_params_status=missing` | Videos with no params at all |
| `?suggest_n=3` | Mark top 3 suggested videos |

---

## Response: sync_status (from GET /videos/)

```json
{
  "available": true,
  "youtube_total": 60,
  "in_database": 55,
  "new_videos_to_import": 5,
  "pending_reconciliation": 2,
  "metadata_to_refresh": 55
}
```

---

## Full API Schema

For the complete endpoint reference with request/response examples:

```
GET /api/schema
```

This endpoint requires no authentication and returns every endpoint's method, path, description, request body, query params, and example response.
