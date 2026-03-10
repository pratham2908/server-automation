# Frontend API Reference

**Base URL**: `http://localhost:8000`  
_(Or `http://68.233.115.135:8000` in production)_

**Authentication**: All requests under `/api/v1/` require the following header:

```http
X-API-Key: <your-api-key>
```

**Timezone**: All timestamps in requests and responses are in **IST (GMT+5:30)**.

---

## Health Check

### Get Server Health

- **Endpoint**: `/health`
- **Method**: `GET`
- **Response**:

```json
{
  "status": "ok"
}
```

---

## API Schema

### Get Full API Schema

- **Endpoint**: `/api/schema`
- **Method**: `GET`
- **Description**: Returns the complete API schema — every endpoint with its method, path, description, request body, query params, and example response. No API key required.
- **Response**:

```json
{
  "service": "YouTube Automation Server",
  "version": "1.0.0",
  "auth": {
    "header": "X-API-Key",
    "required_for": "/api/v1/*"
  },
  "endpoints": [
    {
      "group": "Videos",
      "method": "GET",
      "path": "/api/v1/channels/{channel_id}/videos/",
      "description": "List videos with sync status",
      "query_params": { "status_filter": { "type": "string", "enum": ["todo","ready","scheduled","published"], "optional": true } },
      "request": null,
      "response": { "videos": ["..."], "sync_status": {"...": "..."} }
    }
  ]
}
```

---

## Channels

### List Channels

- **Endpoint**: `/api/v1/channels/`
- **Method**: `GET`
- **Response**: Array of Channel objects.

```json
[
  {
    "_id": "651f8a8...",
    "channel_id": "ch1",
    "name": "My Tech Channel",
    "youtube_channel_id": "UCxxxxxxxx",
    "created_at": "2026-03-01T12:00:00Z",
    "updated_at": "2026-03-01T12:00:00Z"
  }
]
```

### Get Single Channel

- **Endpoint**: `/api/v1/channels/{channel_id}`
- **Method**: `GET`
- **Response**: Channel object.

### Register Channel

- **Endpoint**: `/api/v1/channels/`
- **Method**: `POST`
- **Request Body**:

```json
{
  "youtube_channel_id": "UCxxxxxxxx",
  "channel_id": "optional-custom-slug"
}
```

- **Response**: Created Channel object.

### Update Channel

- **Endpoint**: `/api/v1/channels/{channel_id}`
- **Method**: `PATCH`
- **Request Body**:

```json
{
  "name": "New Channel Name"
}
```

- **Response**: Updated Channel object.

### Refresh Channel Data

- **Endpoint**: `/api/v1/channels/{channel_id}/refresh`
- **Method**: `POST`
- **Description**: Re-fetches name and stats from YouTube.
- **Response**: Updated Channel object.

### Set Content Schema

- **Endpoint**: `/api/v1/channels/{channel_id}/content-schema`
- **Method**: `PUT`
- **Description**: Defines or replaces the channel's content parameter schema (custom dimensions for classifying videos).
- **Request Body**:

```json
{
  "content_schema": [
    {"name": "simulation_type", "description": "Type of simulation", "values": ["battle", "survival", "puzzle"]},
    {"name": "challenge_mechanic", "description": "Core challenge format", "values": ["1v1", "tournament"]},
    {"name": "music", "description": "Background music style", "values": []}
  ]
}
```

- **Response**: `{"ok": true, "channel_id": "...", "params_defined": 3}`

### Delete Channel

- **Endpoint**: `/api/v1/channels/{channel_id}`
- **Method**: `DELETE`
- **Description**: Removes channel and ALL associated videos, categories, analysis, and queues.
- **Response**:

```json
{
  "status": "deleted"
}
```

---

## Categories

### List Categories

- **Endpoint**: `/api/v1/channels/{channel_id}/categories/`
- **Method**: `GET`
- **Query Params**: `?status_filter=active` (optional, can be `active` or `archived`)
- **Response**: Array of Category objects.

```json
[
  {
    "_id": "651f8a8...",
    "channel_id": "ch1",
    "name": "Tutorials",
    "description": "How-to guides",
    "score": 85.5,
    "status": "active",
    "video_count": 10,
    "metadata": {
      "total_videos": 10,
      "avg_views": 1500.0,
      "avg_likes": 15.5,
      "avg_comments": 3.2,
      "avg_duration_seconds": 28.0,
      "avg_engagement_rate": 1.25,
      "avg_like_rate": 1.03,
      "avg_comment_rate": 0.22,
      "avg_percentage_viewed": 72.5,
      "avg_view_duration_seconds": 20,
      "total_views": 15000,
      "total_estimated_minutes_watched": 560.0
    }
  }
]
```

### Add Categories

- **Endpoint**: `/api/v1/channels/{channel_id}/categories/`
- **Method**: `POST`
- **Request Body**: Can be a single object or an array of objects.

```json
{
  "name": "Tutorials",
  "description": "How-to guides",
  "score": 80
}
```

- **Response**: Array of inserted `_id` strings.

### Update Category

- **Endpoint**: `/api/v1/channels/{channel_id}/categories/{category_object_id}`
- **Method**: `PATCH`
- **Request Body**: (All fields optional)

```json
{
  "name": "New Name",
  "description": "Updated desc",
  "score": 90,
  "status": "archived"
}
```

- **Response**: Updated Category object.

---

## Videos

### List Videos

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/`
- **Method**: `GET`
- **Query Params**:
  - `status_filter=todo|ready|scheduled|published` (optional)
  - `content_params_status=unverified|verified|missing` (optional, filter by param verification status)
  - `suggest_n=3` (optional, brings top N suggestions first)
- **Response**: Object with `videos` array and `sync_status` summary.

```json
{
  "videos": [
    {
      "channel_id": "ch1",
      "video_id": "uuid-1234",
      "title": "How to code",
      "description": "...",
      "tags": ["coding", "tutorial"],
      "category": "Tutorials",
      "status": "todo",
      "suggested": false,
      "metadata": {
        "views": 1000,
        "likes": 25,
        "comments": 5,
        "duration_seconds": 30,
        "engagement_rate": 3.0,
        "like_rate": 2.5,
        "comment_rate": 0.5,
        "avg_percentage_viewed": 72.5,
        "avg_view_duration_seconds": 22,
        "estimated_minutes_watched": 366.7
      },
      "scheduled_at": "2026-03-01T09:00:00Z",
      "published_at": "2026-03-01T10:00:00Z"
    }
  ],
  "sync_status": {
    "available": true,
    "youtube_total": 60,
    "in_database": 55,
    "new_videos_to_import": 5,
    "pending_reconciliation": 2,
    "metadata_to_refresh": 55
  }
}
```

### Update Video Status

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/{video_id}/status`
- **Method**: `PATCH`
- **Request Body**:

```json
{
  "status": "published" // Can be "todo", "ready", "scheduled", "published"
}
```

- **Notes**: When status is set to `published`, `published_at` is automatically set to the current time.

- **Response**:

```json
{
  "status": "updated",
  "new_status": "published"
}
```

### Extract Content Params

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/{video_id}/extract-params`
- **Method**: `POST`
- **Description**: Uses Gemini to extract content parameter values from a video's title, description, and tags based on the channel's `content_schema`. Saves with `content_params_status: "unverified"`.
- **Response**:

```json
{
  "ok": true,
  "video_id": "uuid-1234",
  "content_params": {"simulation_type": "battle", "music": "Epic Orchestral"},
  "content_params_status": "unverified"
}
```

### Bulk Extract Content Params

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/extract-params/all`
- **Method**: `POST`
- **Description**: Extracts content parameters for every video that doesn't have them yet.
- **Response**: `{"ok": true, "extracted": 42, "total": 45}`

### Verify Content Params

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/{video_id}/verify-params`
- **Method**: `POST`
- **Description**: Marks a video's content_params as verified. Optionally pass corrected values.
- **Request Body** (optional):

```json
{
  "content_params": {"simulation_type": "survival", "music": "Dramatic Piano"}
}
```

- **Response**: `{"ok": true, "video_id": "...", "content_params": {...}, "content_params_status": "verified"}`

### Upload Video File

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/{video_id}/upload`
- **Method**: `POST`
- **Content-Type**: `multipart/form-data`
- **Form Fields**:
  - `file`: The actual video file.
- **Description**: Uploads the video file to R2 for an existing `todo` video, changing its status to `ready` and placing it in the ready queue.
- **Response**: Returns the updated Video object (status becomes `ready`) and `queue_position`.

### Schedule Ready Video(s)

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/{video_id}/schedule`
- **Method**: `POST`
- **Path Params**: `video_id` — a specific video UUID **OR** `"all"` to schedule every video in the ready queue.
- **Description**: Schedules video(s) on YouTube. Computes `scheduled_at` publish times from the channel's `best_posting_times` analysis, downloads from R2, uploads to YouTube as private with `publishAt`. **Only on success**: removes from the ready queue, adds to the scheduled queue, status → `scheduled`. Requires an analysis with `best_posting_times` and a YouTube token.
- **Response**:

```json
{
  "ok": true,
  "scheduled": 2,
  "failed": 0,
  "videos": [
    {
      "video_id": "550e8400-...",
      "status": "scheduled",
      "youtube_video_id": "dQw4w...",
      "scheduled_at": "2026-03-10T10:00:00+05:30"
    },
    {
      "video_id": "660f9500-...",
      "status": "scheduled",
      "youtube_video_id": "xYz1a...",
      "scheduled_at": "2026-03-10T14:00:00+05:30"
    }
  ]
}
```

### Sync Videos from YouTube

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/sync`
- **Method**: `POST`
- **Request Body**: (Optional, to provide classification instructions to Gemini)

```json
{
  "new_category_description": "Keep categories broad like Rankings, Comparisons, etc."
}
```

- **What it does**:
  - Fetches all videos from the YouTube channel
  - **Refreshes metadata** (views, likes, comments, engagement rates, analytics) for every existing video in the DB
  - Reconciles scheduled videos that are actually live (public) on YouTube (marks them as `published`, sets `published_at` from YouTube's publish time)
  - Imports new videos: **extracts content_params (including music) AND derives category** from those params via a single Gemini call. Content params saved as `"unverified"`

- **Response**:

```json
{
  "ok": true,
  "synced": 5,
  "reconciled": 2,
  "metadata_refreshed": 45,
  "categories_created": ["Tutorials"],
  "videos": [
    { "title": "New Video Title", "category": "Tutorials" }
  ]
}
```

---

### Update To-Do List (Generate Videos)

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/updateToDoList`
- **Method**: `POST`
- **Request Body**:

```json
{
  "n": 5
}
```

- **Description**: Tells Gemini to generate `n` new video ideas based on the latest analysis.
- **Response**:

```json
{
  "status": "generating in background"
}
```

---

## Analysis

### Get Latest Channel Summary

- **Endpoint**: `/api/v1/channels/{channel_id}/analysis/latest`
- **Method**: `GET`
- **Response**: Channel summary with `subscriber_count` and `analysis_status`.

```json
{
  "channel_id": "ch1",
  "subscriber_count": 5000,
  "version": 2,
  "category_analysis": [
    {
      "category": "Tutorials",
      "score": 85.5,
      "best_title_patterns": ["How to...", "10 Things..."]
    }
  ],
  "best_posting_times": [
    {"day_of_week": "monday", "video_count": 2, "times": ["14:00", "18:00"]}
  ],
  "content_param_analysis": [
    {"param_name": "simulation_type", "best_values": ["battle"], "worst_values": ["puzzle"], "insight": "..."}
  ],
  "best_combinations": [
    {"params": {"simulation_type": "battle", "music": "Epic"}, "reasoning": "..."}
  ],
  "analysis_status": {
    "ready_for_analysis": 5,
    "not_ready_yet": 2
  }
}
```

- `ready_for_analysis`: published videos not yet in `analysis_history`, older than 3 days
- `not_ready_yet`: published videos not yet in `analysis_history`, less than 3 days old

### Trigger Analysis Update

- **Endpoint**: `/api/v1/channels/{channel_id}/analysis/update`
- **Method**: `POST`
- **Description**: Two-step pipeline: (1) per-video analysis with stats snapshot + AI insight stored in `analysis_history`, (2) channel summary aggregation. Includes subscriber count and subscribers gained per video.
- **Response**: Returns the updated channel summary.

### Get Per-Video Analyses (History)

- **Endpoint**: `/api/v1/channels/{channel_id}/analysis/history`
- **Method**: `GET`
- **Query Params**:
  - `from` (optional, string): Filter `published_at >= from` (IST). e.g. `2026-02-08` or `2026-02-08T20:00:00`
  - `to` (optional, string): Filter `published_at <= to` (IST). e.g. `2026-02-08` or `2026-02-08T23:59:59`
  - `limit` (optional, int, default 50): Max results
- **Response**: Array of per-video analysis objects.

```json
[
  {
    "channel_id": "ch1",
    "video_id": "uuid-1234",
    "youtube_video_id": "dQw4w...",
    "title": "Epic Battle Simulation",
    "category": "Simulations",
    "content_params": {"simulation_type": "battle", "music": "Epic Orchestral"},
    "published_at": "2026-03-01T10:00:00+05:30",
    "stats_snapshot": {
      "views": 15000, "likes": 800, "comments": 45,
      "engagement_rate": 5.63, "avg_percentage_viewed": 72.5,
      "subscribers_gained": 120, "views_per_subscriber": 3.0,
      "subscriber_count_at_analysis": 5000
    },
    "ai_insight": {
      "performance_rating": 85,
      "what_worked": "Strong title hook + battle format",
      "what_didnt": "Could improve description SEO",
      "key_learnings": ["Battle sims drive 3x engagement"]
    },
    "analyzed_at": "2026-03-07T12:00:00+05:30"
  }
]
```

### Get Single Video Analysis

- **Endpoint**: `/api/v1/channels/{channel_id}/analysis/history/{video_id}`
- **Method**: `GET`
- **Response**: Single per-video analysis object (same format as above).
- **Errors**: `404` if no analysis exists for the video.

### Compare Time Periods

- **Endpoint**: `/api/v1/channels/{channel_id}/analysis/compare`
- **Method**: `GET`
- **Query Params** (all required):
  - `from1`, `to1`: Start and end of period 1
  - `from2`, `to2`: Start and end of period 2
- **Description**: Aggregates per-video analyses for each period and returns side-by-side averages.
- **Response**:

```json
{
  "channel_id": "ch1",
  "period_1": {
    "from": "2026-02-01T00:00:00", "to": "2026-02-15T00:00:00",
    "video_count": 10, "avg_views": 12000,
    "avg_engagement_rate": 4.5, "total_subscribers_gained": 500,
    "avg_performance_rating": 72.3
  },
  "period_2": {
    "from": "2026-02-16T00:00:00", "to": "2026-03-01T00:00:00",
    "video_count": 12, "avg_views": 18000,
    "avg_engagement_rate": 5.8, "total_subscribers_gained": 850,
    "avg_performance_rating": 81.5
  }
}
```


> **Note:** To view scheduled videos, use `GET /api/v1/channels/{channel_id}/videos?status_filter=scheduled`. The `scheduled_at` field on each video shows the YouTube publish time. To schedule all ready videos at once, use `POST /api/v1/channels/{channel_id}/videos/all/schedule`.

