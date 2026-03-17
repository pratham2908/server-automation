# YouTube Automation Server – Documentation

## Table of Contents

- [Authentication](#authentication)
- [API Endpoints](#api-endpoints)
  - [Health](#health)
  - [API Schema](#api-schema)
  - [Channels](#channels)
  - [Videos](#videos)
  - [Categories](#categories)
  - [Analysis](#analysis)
- [Database Schema](#database-schema)
  - [channels](#collection-channels)
  - [content_params](#collection-content_params)
  - [videos](#collection-videos)
  - [posting_queue (Ready Queue)](#collection-posting_queue-ready-queue)
  - [schedule_queue (Scheduled Queue)](#collection-schedule_queue-scheduled-queue)
  - [categories](#collection-categories)
  - [analysis](#collection-analysis)
  - [analysis_history](#collection-analysis_history)
- [Services Architecture](#services-architecture)
- [Data Flow Diagrams](#data-flow-diagrams)
  - [Video Status Lifecycle](#video-status-lifecycle)
  - [System Architecture](#system-architecture)
  - [Video Upload and Schedule Flow](#video-upload-and-schedule-flow)
  - [Sync Flow](#sync-flow)
  - [Analysis Flow](#analysis-flow)
  - [To-do Video Generation Flow](#to-do-video-generation-flow)
  - [Content Params Extraction Flow](#content-params-extraction-flow)
  - [Scheduling Slot Computation](#scheduling-slot-computation)

---

## Authentication

**All endpoints** (except `/health`) require an API key passed in the `X-API-Key` header.

```
X-API-Key: your-secret-key
```

- The key is validated against the `API_KEY` value in `.env`.
- Invalid or missing keys return `401 Unauthorized`.

---

## Timezone Convention

**All timestamps** throughout the system use **IST (GMT+5:30)** — both in the code and in the database.

- A central helper `now_ist()` (in `app/timezone.py`) returns the current timezone-aware IST datetime.
- All model `default_factory` values, all `created_at` / `updated_at` / `published_at` / `scheduled_at` / `added_at` fields use IST.
- YouTube API publish dates are converted to IST before storage.
- The scheduling engine uses `Asia/Kolkata` (equivalent to GMT+5:30) for computing publish slots.

---

## API Endpoints

Base URL: `http://localhost:8000`

All channel-scoped endpoints are prefixed with `/api/v1/channels/{channel_id}/` where `channel_id` is the internal slug for the channel (e.g. `tech-tips`).

---

### Health

#### `GET /health`

Simple liveness check. No authentication required.

**Response:**

```json
{ "status": "ok" }
```

---

### API Schema

#### `GET /api/schema`

Returns the full API schema with method, path, description, request body, query params, and example response for every endpoint. Useful for building clients, documentation, or AI integrations. No authentication required.

**Response:**

```json
{
  "service": "YouTube Automation Server",
  "version": "1.0.0",
  "auth": { "header": "X-API-Key", "required_for": "/api/v1/*" },
  "endpoints": [
    {
      "group": "Videos",
      "method": "GET",
      "path": "/api/v1/channels/{channel_id}/videos/",
      "description": "List videos with sync status",
      "query_params": {
        "status_filter": {
          "type": "string",
          "enum": ["todo", "ready", "scheduled", "published"],
          "optional": true
        }
      },
      "request": null,
      "response": { "videos": ["..."], "sync_status": { "...": "..." } }
    }
  ]
}
```

---

### Channels

Prefix: `/api/v1/channels`

Manages YouTube channel registration. Channel data is **auto-fetched from YouTube** on registration.

---

#### `POST /` — Register a new channel

Creates a channel by fetching its data from the YouTube API. You only need to provide the YouTube channel ID.

**Request body:**

```json
{
  "youtube_channel_id": "UCxxxxxxxx", // required – the UC... ID from YouTube
  "channel_id": "my-channel" // optional – custom slug, auto-generated if omitted
}
```

**What happens:**

1. Server calls YouTube API to fetch channel name, description, subscriber count, video count, view count, thumbnail, and custom URL.
2. If `channel_id` is not provided, one is auto-generated from the YouTube custom URL or channel name.
3. All data is stored in the `channels` collection.

**Response (201):**

```json
{
  "_id": "65f...",
  "channel_id": "tech-tips",
  "youtube_channel_id": "UCxxxxxxxx",
  "name": "Tech Tips Daily",
  "description": "Daily tech tutorials and reviews...",
  "custom_url": "@techtips",
  "thumbnail_url": "https://yt3.ggpht.com/...",
  "subscriber_count": 125000,
  "video_count": 342,
  "view_count": 15000000,
  "created_at": "2024-01-15T10:30:00Z",
  "updated_at": "2024-01-15T10:30:00Z"
}
```

**Errors:**

- `404` — YouTube channel ID not found on YouTube
- `409` — Channel with that `channel_id` already exists
- `503` — YouTube service not initialised

---

#### `GET /` — List all channels

Returns all registered channels.

**Response (200):**

```json
[
  {
    "_id": "65f...",
    "channel_id": "tech-tips",
    "name": "Tech Tips Daily",
    "subscriber_count": 125000,
    ...
  }
]
```

---

#### `GET /{channel_id}` — Get a single channel

**Path params:** `channel_id` — internal slug

**Response (200):** Full channel document.

**Errors:** `404` — Channel not found.

---

#### Content Params — CRUD endpoints

Content param definitions are stored in the `content_params` collection (not on the channel document). Each param defines a custom dimension (e.g. `video_topic`, `ranking_factor`, `music`) that videos are tagged with.

---

#### `GET /{channel_id}/content-params` — List all content params for channel

Returns all content param definitions for the channel.

**Response (200):**

```json
[
  {
    "channel_id": "officialgeoranking",
    "name": "video_topic",
    "description": "The thing that the video is ranking",
    "values": [
      { "value": "GDP", "score": 85, "video_count": 4 },
      { "value": "Population", "score": 72, "video_count": 6 }
    ],
    "belongs_to": ["all"],
    "unique": true,
    "created_at": "2024-01-15T10:30:00Z",
    "updated_at": "2024-01-15T10:30:00Z"
  }
]
```

- `values` — for params with predefined options: array of `{value, score, video_count}`; scores and video_count are updated after channel summary analysis. If an analysed video uses a value not yet tracked, it is auto-added. Free-form params have `values: []`.
- `belongs_to` — array of category names this param applies to; default `["all"]` means it applies to all categories. User can scope to specific categories.
- `unique` — if `true`, the TODO generator tells Gemini not to reuse already-used values for this param when generating new video ideas. Useful for free-form params like `video_topic` where each video should cover a distinct topic. Default `false`.

---

#### `POST /{channel_id}/content-params` — Add a new content param

**Request body:**

```json
{
  "name": "video_topic",
  "description": "The thing that the video is ranking",
  "values": [],
  "belongs_to": ["all"],
  "unique": true
}
```

- `name` — parameter key used in `content_params` on videos (required)
- `description` — what this parameter represents (optional)
- `values` — for predefined options: array of `{value, score?, video_count?}`; empty list means free-form (Gemini infers)
- `belongs_to` — array of category names; default `["all"]`
- `unique` — if `true`, the TODO generator tells Gemini not to reuse already-used values for this param (default `false`)

**Response (201):** `{"ok": true, "channel_id": "...", "param": {...}}`

---

#### `PUT /{channel_id}/content-params/{param_name}` — Update a content param

Updates `description`, `values`, `belongs_to`, and/or `unique` for an existing param.

**Path params:** `param_name` — the param's `name` field

**Request body (all fields optional):**

```json
{
  "description": "Updated description",
  "values": [{ "value": "GDP", "score": 85, "video_count": 4 }],
  "belongs_to": ["Geography", "Economics"],
  "unique": true
}
```

**Response (200):** `{"ok": true, "channel_id": "...", "param": {...}}`

---

#### `DELETE /{channel_id}/content-params/{param_name}` — Delete a content param

Removes the content param definition. Does not remove `content_params` from existing videos.

**Path params:** `param_name` — the param's `name` field

**Response (200):** `{"ok": true, "channel_id": "...", "deleted": true}`

---

#### `POST /{channel_id}/refresh` — Re-fetch data from YouTube

Pulls the latest stats (subscriber count, video count, etc.) from YouTube and updates the DB.

**Response (200):**

```json
{
  "ok": true,
  "channel_id": "tech-tips",
  "updated": {
    "name": "Tech Tips Daily",
    "subscriber_count": 126000,
    "video_count": 345,
    ...
  }
}
```

---

#### `PATCH /{channel_id}` — Update a channel

Partially update channel fields.

**Request body:**

```json
{
  "name": "New Channel Name" // optional
}
```

**Response (200):** `{"ok": true, "channel_id": "tech-tips"}`

---

- **Endpoint**: `/api/v1/channels/{channel_id}`
- **Method**: `DELETE`
- **Description**: Removes channel and ALL associated data (videos, categories, analysis, queues).
- **Cleanup**: It automatically iterates through all videos and deletes their associated `.mp4` files from Cloudflare R2 storage before removing the database records.

**Response (200):** `{"ok": true, "channel_id": "tech-tips", "deleted": true}`

---

### Videos

Prefix: `/api/v1/channels/{channel_id}/videos`

Manages the video list — both manually created to-do items and AI-generated suggestions.

---

#### `GET /` — List videos

Returns all videos for a channel, with optional filtering and suggestion marking.

**Query params:**

| Param                   | Type   | Default | Description                                                                                           |
| ----------------------- | ------ | ------- | ----------------------------------------------------------------------------------------------------- |
| `status_filter`         | string | `all`   | Filter by status: `todo`, `ready`, `scheduled`, `published`, or `all`                                 |
| `verification_status` | string | —       | Filter by verification status: `unverified`, `verified`, or `missing`                                  |
| `suggest_n`             | int    | —       | If provided, marks the top N to-do videos as `suggested=true` (ordered by category score, best first) |

**How `suggest_n` works:**

1. Resets all previously suggested videos (`suggested=false`)
2. Fetches active categories sorted by score (highest first)
3. Sorts to-do videos by their category's score
4. Marks the top N as `suggested=true`
5. Returns the full video list

**Response (200):**

The response is a wrapper with `videos` (the array) and `sync_status` (how many videos need syncing).

```json
{
  "videos": [
    {
      "channel_id": "tech-tips",
      "video_id": "550e8400-e29b-41d4-a716-446655440000",
      "title": "10 VS Code Tricks You Didn't Know",
      "description": "In this video...",
      "tags": ["vscode", "productivity", "coding"],
      "category": "Tutorials",
      "status": "todo",
      "suggested": true,
      "youtube_video_id": null,
      "r2_object_key": null,
      "metadata": {
        "views": null,
        "likes": null,
        "comments": null,
        "duration_seconds": null,
        "engagement_rate": null,
        "like_rate": null,
        "comment_rate": null,
        "avg_percentage_viewed": null,
        "avg_view_duration_seconds": null,
        "estimated_minutes_watched": null
      },
      "scheduled_at": null,
      "published_at": null,
      "created_at": "2024-01-15T10:30:00Z",
      "updated_at": "2024-01-15T10:30:00Z"
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

`sync_status` fields:

- `youtube_total` — total videos on the YouTube channel
- `in_database` — videos in our DB that have a `youtube_video_id`
- `new_videos_to_import` — videos on YouTube not yet in the DB
- `pending_reconciliation` — videos in `scheduled` status whose YouTube video is actually live (public) on YouTube (reconciled to `published` on next sync)
- `metadata_to_refresh` — existing videos whose stats will be refreshed on next sync

---

#### `POST /sync` — Sync videos from YouTube

Fetches all videos from the YouTube channel, finds any not already in the DB, categorizes them via Gemini (auto-creating categories from the channel description), and inserts them as `done`.

**Optional request body:**

```json
{
  "new_category_description": "Extra instructions for Gemini on how to categorize"
}
```

**What happens:**

1. Fetches all videos from the channel's uploads playlist (paginated) — pulls `snippet`, `statistics`, `contentDetails` (duration), and `status` (privacyStatus, publishAt)
2. Enriches with YouTube Analytics API data (`avg_percentage_viewed`, `avg_view_duration_seconds`, `estimated_minutes_watched`) when available
3. **Refreshes metadata** for all existing published videos in the DB — updates views, likes, comments, engagement rates, analytics, etc. with the latest data from YouTube
4. **Reconciles scheduled videos** — finds all videos in the DB with status `scheduled` and checks YouTube to see if they are actually live (privacy status is `public`). If live, marks them as `published`, sets `published_at` from YouTube's publish time, and cleans up their `schedule_queue` entry
5. Skips any already in the `videos` collection (by `youtube_video_id`)
6. **Extracts content params AND derives category** for new videos in batches of 5 via Gemini.
7. Auto-creates categories and **updates video counts** for all newly imported/reconciled videos.
8. **Detects scheduled videos** — if a video has `status.publishAt` set to a future time, it is inserted as `scheduled` (not `published`) with `scheduled_at` set to that time, and an entry is added to the `schedule_queue`. This correctly recognises videos that were uploaded to YouTube with a future publish time.
9. Remaining videos are inserted as `published` with full metadata.

**Response (200):**

```json
{
  "ok": true,
  "synced": 15,
  "synced_published": 14,
  "synced_scheduled": 1,
  "reconciled": 0,
  "metadata_refreshed": 30,
  ...
}
```

---

#### `DELETE /{video_id}` — Delete a video

Permanently remove a video and its assets.

**What happens:**

1. Deletes `.mp4` from R2.
2. Decrements category `video_count` if the video was published.
3. Removes from `posting_queue` and `schedule_queue`.
4. Deletes video document and `analysis_history` records.

**Response (200):** `{"ok": true, "video_id": "...", "deleted": true}`

---

#### `PATCH /{video_id}/status` — Update video status

Changes a video's status.

**Path params:** `video_id` — the UUID of the video

**Request body:**

```json
{
  "status": "published" // "todo", "ready", "scheduled", or "published"
}
```

**Side effects:**

- **Automatic Cleanup**: If moving FROM `ready` TO any other status, the video's `.mp4` file is deleted from R2 and it's removed from the `posting_queue`.
- **Category Counts**: When marking as `published`, the category's `video_count` is incremented. If moving AWAY from `published`, the count is decremented.
- **Timestamps**: When marking as `published`, `published_at` is set. When moving to `todo`, `scheduled_at` is cleared.
- **Scheduled Queue**: If moving away from `scheduled`, the entry is removed from the `schedule_queue`.

**Response (200):**

```json
{ "ok": true, "video_id": "550e8400-...", "status": "done" }
```

---

#### `PATCH /{video_id}/category` — Change video category

Moves a video from one category to another. Updates the video document, the per-video record in `analysis_history`, and recomputes metadata, `video_count`, and `video_ids` for both the old and new category.

**Path params:** `video_id` — the UUID of the video

**Request body:**

```json
{
  "old_category_id": "65f...", // MongoDB _id of current category
  "new_category_id": "65f..." // MongoDB _id of target category
}
```

**Response (200):**

```json
{
  "ok": true,
  "video_id": "550e8400-...",
  "old_category": "Tutorials",
  "new_category": "Reviews"
}
```

---

#### `POST /{video_id}/extract-params` — Extract content params via Gemini

Uses Gemini to extract content parameter values from a video's title, description, and tags, based on the channel's content params (from the `content_params` collection). Results are saved with `verification_status: "unverified"`.

**Preconditions:** Channel must have at least one content param defined in the `content_params` collection.

**Response (200):**

```json
{
  "ok": true,
  "video_id": "550e8400-...",
  "content_params": {
    "simulation_type": "battle",
    "challenge_mechanic": "1v1",
    "music": "Epic Orchestral - Two Steps From Hell"
  },
  "verification_status": "unverified"
}
```

---

#### `POST /extract-params/all` — Bulk extract params for all videos

Extracts content parameters for every video in the channel that doesn't have them yet. Runs Gemini extraction one video at a time.

**Response (200):**

```json
{
  "ok": true,
  "extracted": 42,
  "total": 45
}
```

---

#### `POST /{video_id}/verify-params` — Verify video (category + content params)

Marks a video as `"verified"`. Optionally pass corrected `category` and/or `content_params` in the body to override AI-assigned values.

**Request body (optional):**

```json
{
  "category": "battle",
  "content_params": {
    "simulation_type": "survival",
    "challenge_mechanic": "tournament",
    "music": "Dramatic Piano - Ludovico Einaudi"
  }
}
```

**Response (200):**

```json
{
  "ok": true,
  "video_id": "550e8400-...",
  "category": "battle",
  "content_params": { "..." },
  "verification_status": "verified"
}
```

---

#### `POST /{video_id}/upload` — Upload video file

Uploads a video file for an existing `todo` video, streams it to Cloudflare R2, sets status to `ready`, and adds it to the ready queue.

**Request:** `multipart/form-data`

| Field  | Type          | Description                |
| ------ | ------------- | -------------------------- |
| `file` | File (binary) | The video file (.mp4)      |
| `body` | JSON string   | Video metadata (see below) |

**Body JSON:**

```json
{
  "title": "My Video Title", // required
  "description": "Description...", // optional
  "tags": ["tag1", "tag2"], // optional
  "category": "Tutorials" // optional
}
```

**What happens:**

1. Verifies the video exists and is in `todo` status
2. Streams the file to R2 at `{channel_id}/{video_id}.mp4`
3. Updates the video document: status → `ready`, sets `r2_object_key`
4. Creates an entry in the ready queue with the next available position

**Response (201):**

```json
{
  "ok": true,
  "video": { ...full video document... },
  "queue_position": 3
}
```

---

#### `POST /{video_id}/schedule` — Schedule ready video(s)

Schedules video(s) on YouTube. Computes a publish time, uploads the video file to YouTube as private with `publishAt`, and **only on success**: removes from the ready queue, adds to the scheduled queue, sets status to `scheduled`.

**Path params:** `video_id` — the UUID of a single video **OR** `"all"` to schedule every video in the ready queue.

**Request Body (Optional, for single video_id only):**

```json
{
  "scheduled_at": "2026-03-10T14:30:00+05:30"
}
```

**Preconditions:**

- The video(s) must be in `ready` status (uploaded to R2). Returns `400` otherwise.
- A channel analysis with `best_posting_times` must exist. Returns `400` if missing.
- A valid YouTube token must exist for the channel. Returns `503` if missing.

**What happens:**

1. If `video_id` is `"all"`, fetches all entries from the ready queue; otherwise fetches the single video
2. Loads `best_posting_times` from the latest analysis document
3. Gathers `scheduled_at` values from existing scheduled queue entries (occupied slots)
4. Computes the next available publish slot(s) from the weekly calendar, skipping past and occupied slots (unless `scheduled_at` was manually provided in the request body). Timezone from `TIMEZONE` env var, default `Asia/Kolkata`.
5. For each video: downloads from R2, uploads to YouTube as private with `publishAt`. **Only on YouTube upload success**: removes from the ready queue, inserts into the scheduled queue with `scheduled_at`, sets `youtube_video_id`, updates status to `scheduled`

The video remains in `scheduled` status until YouTube auto-publishes it at the `publishAt` time. The sync endpoint then reconciles it to `published`.

**Response (200):**

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

**Errors:**

- `400` — No analysis with `best_posting_times` found
- `400` — Not enough posting slots for the number of videos
- `404` — Video not found / no videos in ready queue
- `503` — No YouTube token for the channel

---

#### `POST /updateToDoList` — Bulk generate to-do videos

Generates completely distinct new video ideas based on the latest analysis.

**Request body:**

```json
{
  "n": 5 // number of videos to generate
}
```

**What happens:**

1. **Delete** any existing "todo" status videos that belong to newly archived categories.
2. **Distribute** the `n` slots across active categories weighted by their performance score.
3. **Fetch content params** — only those whose `belongs_to` includes the current category name or `"all"`.
4. **Exclude** existing video titles from generation so Gemini doesn't repeat ideas.
5. **Call Gemini** to bulk-generate distinct ideas in one shot per category.
6. **Insert** new videos into the `videos` collection with `status: "todo"`.

**Response (200):**

```json
{
  "ok": true,
  "message": "Successfully generated 5 new videos for the to-do list."
}
```

---

### Categories

Prefix: `/api/v1/channels/{channel_id}/categories`

Manages content categories (e.g. "Tutorials", "Reviews"). Categories drive the analysis engine and to-do video generation.

---

#### `GET /` — List categories

Returns all categories sorted by score (highest first).

**Query params:**

| Param           | Type   | Default | Description                              |
| --------------- | ------ | ------- | ---------------------------------------- |
| `status_filter` | string | —       | Filter by status: `active` or `archived` |

**Response (200):**

```json
[
  {
    "_id": "65f...",
    "channel_id": "tech-tips",
    "name": "Tutorials",
    "description": "How-to videos and walkthroughs",
    "raw_description": "Original user-provided description",
    "score": 85.5,
    "status": "active",
    "video_count": 12,
    "metadata": {
      "total_videos": 12,
      "avg_views": 1500.0,
      "avg_likes": 15.5,
      "avg_comments": 3.2,
      "avg_duration_seconds": 28.0,
      "avg_engagement_rate": 1.25,
      "avg_like_rate": 1.03,
      "avg_comment_rate": 0.22,
      "avg_percentage_viewed": 72.5,
      "avg_view_duration_seconds": 20,
      "total_views": 18000,
      "total_estimated_minutes_watched": 560.0
    },
    "created_at": "2024-01-15T10:30:00Z",
    "updated_at": "2024-01-15T10:30:00Z"
  }
]
```

---

#### `POST /` — Add categories

Accepts a **single category** or a **list of categories**.

**Request body (single):**

```json
{
  "name": "Tutorials",
  "description": "How-to videos",
  "raw_description": "Original description",
  "score": 80.0
}
```

**Request body (batch):**

```json
[
  { "name": "Tutorials", "description": "How-to videos", "score": 80 },
  { "name": "Reviews", "description": "Product reviews", "score": 70 }
]
```

**Response (201):**

```json
{
  "ok": true,
  "inserted_count": 2,
  "ids": ["65f...", "65f..."]
}
```

---

#### `PATCH /{category_id}` — Update a category

**Path params:** `category_id` — the MongoDB `_id` of the category

**Request body (all fields optional):**

```json
{
  "name": "Updated Name",
  "description": "Updated description",
  "raw_description": "Updated raw desc",
  "score": 92.0,
  "status": "archived"
}
```

**Response (200):** `{"ok": true, "category_id": "65f..."}`

**Name Propagation**: If the category name is updated, the server automatically updates the `category` field on all associated `videos` and `analysis_history` records to prevent broken metadata.

---

#### `DELETE /{category_id}` — Delete a category

Removes a category and re-allocates its videos.

**What happens:**

1. Finds all videos belonging to this category.
2. Sets their `category` field to `"Uncategorized"`.
3. Updates `analysis_history` records to `"Uncategorized"`.
4. Deletes the category document from the `categories` collection.

**Response (200):** `{"ok": true, "category_id": "...", "deleted": true}`

---

### Analysis

Prefix: `/api/v1/channels/{channel_id}/analysis`

AI-powered channel analysis using Gemini. Analyzes video performance and generates insights.

---

#### `POST /update` — Run full analysis update

**Two-step pipeline** — calls YouTube API + Gemini AI. May take 30+ seconds.

**Step 1 — Per-video analysis:**

1. **Fetch subscriber count** from YouTube via `get_channel_info()`
2. **Fetch done videos** from DB for this channel
3. **Compute delta** — compare with `analysis_history` collection to find videos not yet individually analysed
4. **Exclude recent videos** — skip any with `created_at` less than 3 days ago (hard limit, no exceptions)
5. **Exclude unverified videos** — skip any with `verification_status: "unverified"`
6. **Fetch YouTube stats** (views, likes, comments, duration, engagement rates) + **YouTube Analytics** (avg % viewed, avg view duration, est. minutes watched, **subscribers gained**) for new videos
7. **For each new video**: build a stats snapshot (including `views_per_subscriber`, `subscribers_gained`, `subscriber_count_at_analysis`), send to **Gemini for individual analysis** → get `performance_rating` (0-100), `what_worked`, `what_didnt`, `key_learnings`

   **Performance rating weightage** (used by Gemini to compute the 0-100 `performance_rating`):

   | Metric                      | Weight | Signal                                                    |
   | --------------------------- | ------ | --------------------------------------------------------- |
   | `subscribers_gained`        | 25%    | Direct channel growth impact                              |
   | `avg_percentage_viewed`     | 25%    | Content quality / retention                               |
   | `views`                     | 20%    | Raw reach                                                 |
   | `engagement_rate`           | 10%    | (likes + comments) / views                                |
   | `comments`                  | 8%     | Active audience participation                             |
   | `likes`                     | 5%     | Passive approval                                          |
   | `views_per_subscriber`      | 5%     | Viral reach beyond existing audience (>1.0 = beyond subs) |
   | `estimated_minutes_watched` | 2%     | Total accumulated watch time                              |

   For each metric, Gemini scores that dimension 0-100 relative to the channel's typical range, then computes `performance_rating` as the weighted sum. Missing metrics are treated as 0.

8. **Store each result** in `analysis_history` collection (one doc per video, never re-analysed)

**Step 2 — Channel summary:**

9. **Fetch ALL per-video analyses** from `analysis_history` for this channel
10. **Send to Gemini in batches of 5** — each batch includes per-video AI insights alongside stats and content_params. Gemini produces collective channel insights: `best_posting_times`, `category_analysis`, `content_param_analysis`, `best_combinations`
11. **Save channel summary** to `analysis` collection with `subscriber_count` (increments version)
12. **Update content param value scores** — after channel summary, content param `values` (score, video_count) in the `content_params` collection are updated based on `performance_rating` from `analysis_history`. If an analysed video uses a value not yet tracked, it is auto-added
13. **Run to-do engine:**
    - Updates **all category scores** from Gemini's analysis output
    - Increments **category video_count** for each newly analysed video
    - **Computes and saves category metadata** — aggregates avg views, likes, comments, engagement rates, avg % viewed, total watch time, etc. from published verified videos in each category (excludes unverified)
    - Archives categories with score < 30 AND ≥ 5 videos

**Response (200):**

```json
{
  "channel_id": "tech-tips",
  "subscriber_count": 5000,
  "best_posting_times": [
    {
      "day_of_week": "monday",
      "video_count": 2,
      "times": ["10:00", "14:00"]
    }
  ],
  "category_analysis": [
    {
      "category": "Tutorials",
      "best_title_patterns": ["How to...", "10 Things..."],
      "score": 85.5
    }
  ],
  "content_param_analysis": [
    {
      "param_name": "simulation_type",
      "best_values": ["battle", "survival"],
      "worst_values": ["puzzle"],
      "insight": "Battle simulations get 3x more engagement"
    }
  ],
  "best_combinations": [
    {
      "params": {
        "simulation_type": "battle",
        "challenge_mechanic": "1v1",
        "music": "Epic Orchestral"
      },
      "reasoning": "Highest avg_percentage_viewed at 72%"
    }
  ],
  "analysis_done_video_ids": ["vid1", "vid2", "vid3"],
  "version": 3,
  "created_at": "2024-01-15T10:30:00Z",
  "updated_at": "2024-01-20T14:00:00Z"
}
```

---

#### `DELETE /` — Delete all analysis data

Wipes all analysis data for the channel and resets derived scores. Use this to force a full re-analysis from scratch on the next `POST /update`.

**What gets deleted / reset:**

1. The `analysis` document (channel summary) is deleted
2. All `analysis_history` records (per-video analyses) are deleted
3. All categories have their `score`, `video_count`, `video_ids`, and `metadata` reset to zero/empty
4. All `content_params` value entries have their `score` and `video_count` set to 0

**Response (200):**

```json
{
  "ok": true,
  "channel_id": "tech-tips",
  "analysis_deleted": true,
  "analysis_history_deleted": 42,
  "categories_reset": 5,
  "content_params_reset": 3
}
```

---

#### `GET /latest` — Get latest channel summary

Returns the most recent channel summary for the channel, including `subscriber_count` and an `analysis_status` summary.

**Response (200):** Same format as the POST response above, with an additional `analysis_status` field:

```json
{
  "...all analysis fields...",
  "subscriber_count": 5000,
  "analysis_status": {
    "ready_for_analysis": 5,
    "not_ready_yet": 2,
    "unverified": 3
  }
}
```

- `ready_for_analysis` — published videos not yet in `analysis_history` and older than 3 days
- `not_ready_yet` — published videos not yet in `analysis_history` but less than 3 days old
- `unverified` — published videos with `verification_status: "unverified"`; these are excluded from analysis

**Errors:** `404` — No analysis exists yet for this channel.

---

#### `GET /history` — Get per-video analyses

Returns per-video analyses from the `analysis_history` collection. Each document represents a single video's stats snapshot + AI insight.

**Query params:**

| Param   | Type   | Default | Description                                                                     |
| ------- | ------ | ------- | ------------------------------------------------------------------------------- |
| `from`  | string | —       | Filter `published_at >= from` (IST). e.g. `2026-02-08` or `2026-02-08T20:00:00` |
| `to`    | string | —       | Filter `published_at <= to` (IST). e.g. `2026-02-08` or `2026-02-08T23:59:59`   |
| `limit` | int    | —       | Max number of results; if omitted, returns entire history                       |

**Response (200):**

```json
[
  {
    "channel_id": "tech-tips",
    "video_id": "uuid-1234",
    "youtube_video_id": "dQw4w...",
    "title": "Epic Battle Simulation",
    "category": "Simulations",
    "content_params": {
      "simulation_type": "battle",
      "music": "Epic Orchestral"
    },
    "published_at": "2026-03-01T10:00:00+05:30",
    "stats_snapshot": {
      "views": 15000,
      "likes": 800,
      "comments": 45,
      "engagement_rate": 5.63,
      "avg_percentage_viewed": 72.5,
      "subscribers_gained": 120,
      "views_per_subscriber": 3.0,
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

---

#### `GET /history/{video_id}` — Get single video analysis

Returns the per-video analysis for a specific video.

**Response (200):** Single per-video analysis document (same format as above).

**Errors:** `404` — No analysis found for this video.

---

#### `GET /compare` — Compare time periods

Aggregates per-video analyses across two time periods for side-by-side comparison.

**Query params (all required):**

| Param   | Type     | Description       |
| ------- | -------- | ----------------- |
| `from1` | datetime | Start of period 1 |
| `to1`   | datetime | End of period 1   |
| `from2` | datetime | Start of period 2 |
| `to2`   | datetime | End of period 2   |

**Response (200):**

```json
{
  "channel_id": "tech-tips",
  "period_1": {
    "from": "2026-02-01T00:00:00",
    "to": "2026-02-15T00:00:00",
    "video_count": 10,
    "avg_views": 12000,
    "avg_likes": 650,
    "avg_comments": 35,
    "avg_engagement_rate": 4.5,
    "avg_percentage_viewed": 68.2,
    "avg_views_per_subscriber": 2.4,
    "total_subscribers_gained": 500,
    "avg_performance_rating": 72.3
  },
  "period_2": {
    "from": "2026-02-16T00:00:00",
    "to": "2026-03-01T00:00:00",
    "video_count": 12,
    "avg_views": 18000,
    "avg_likes": 950,
    "avg_comments": 52,
    "avg_engagement_rate": 5.8,
    "avg_percentage_viewed": 74.1,
    "avg_views_per_subscriber": 3.6,
    "total_subscribers_gained": 850,
    "avg_performance_rating": 81.5
  }
}
```

---

---

## Database Schema

Database: **MongoDB Atlas** (database name from `MONGODB_DB_NAME` env var, default: `youtube_automation`)

All collections are shared across channels, with `channel_id` as a discriminator field. All datetime fields are stored in **IST (GMT+5:30)**.

---

### Collection: `channels`

Stores registered YouTube channels with their metadata (auto-fetched from YouTube).

```json
{
  "_id": "ObjectId",
  "channel_id": "tech-tips", // unique internal slug
  "youtube_channel_id": "UCxxxxxxxx", // YouTube UC... ID
  "name": "Tech Tips Daily", // from YouTube
  "description": "Channel description", // from YouTube
  "custom_url": "@techtips", // from YouTube
  "thumbnail_url": "https://...", // from YouTube
  "subscriber_count": 125000, // from YouTube
  "video_count": 342, // from YouTube
  "view_count": 15000000, // from YouTube
  "created_at": "datetime",
  "updated_at": "datetime"
}
```

**Indexes:**
| Fields | Type | Purpose |
|---|---|---|
| `channel_id` | Unique | Fast lookup, prevent duplicates |

---

### Collection: `content_params`

Stores content param definitions per channel. Each document defines one parameter (e.g. `video_topic`, `ranking_factor`, `music`) that videos are tagged with. Replaces the old channel-level `content_schema` array.

```json
{
  "_id": "ObjectId",
  "channel_id": "officialgeoranking",
  "name": "video_topic",
  "description": "The thing that the video is ranking",
  "values": [
    { "value": "GDP", "score": 85, "video_count": 4 },
    { "value": "Population", "score": 72, "video_count": 6 }
  ],
  "belongs_to": ["all"],
  "created_at": "datetime",
  "updated_at": "datetime"
}
```

- `name` — parameter key used in `content_params` on videos
- `description` — what this parameter represents
- `values` — for params with predefined options: array of `{value, score, video_count}`; scores and video_count are updated after channel summary analysis based on `performance_rating` from `analysis_history`. If an analysed video uses a value not yet tracked, it is auto-added. Free-form params have `values: []`.
- `belongs_to` — array of category names this param applies to; `["all"]` means all categories. User can scope to specific categories (e.g. `["Geography", "Economics"]`).

**Indexes:**
| Fields | Type | Purpose |
|---|---|---|
| `(channel_id, name)` | Compound (unique) | Fast lookup per channel |

---

### Collection: `videos`

Stores all video records — both manually uploaded and AI-generated to-do items.

```json
{
  "_id": "ObjectId",
  "channel_id": "tech-tips",
  "video_id": "550e8400-...", // auto-generated UUID
  "title": "10 VS Code Tricks",
  "description": "In this video...",
  "tags": ["vscode", "productivity"],
  "category": "Tutorials",
  "status": "todo", // "todo", "ready", "scheduled", or "published"
  "suggested": false, // true when marked by suggest_n
  "youtube_video_id": null, // set after YouTube upload
  "r2_object_key": "tech-tips/vid.mp4", // set when file is stored in R2
  "metadata": {
    "views": null, // from YouTube Data API
    "likes": null, // from YouTube Data API
    "comments": null, // from YouTube Data API
    "duration_seconds": null, // from YouTube Data API (contentDetails)
    "engagement_rate": null, // (likes + comments) / views × 100
    "like_rate": null, // likes / views × 100
    "comment_rate": null, // comments / views × 100
    "avg_percentage_viewed": null, // from YouTube Analytics API
    "avg_view_duration_seconds": null, // from YouTube Analytics API
    "estimated_minutes_watched": null // from YouTube Analytics API
  },
  "content_params": {
    // channel-specific content dimensions
    "simulation_type": "battle",
    "challenge_mechanic": "1v1",
    "music": "Epic Orchestral - Two Steps From Hell"
  },
  "verification_status": "unverified", // "unverified", "verified", or null — covers both category and content params verification
  "scheduled_at": "datetime | null", // when the video is scheduled to go live on YouTube; null until scheduled
  "published_at": "datetime | null", // when the video was published on YouTube; null until published
  "created_at": "datetime",
  "updated_at": "datetime"
}
```

**Indexes:**
| Fields | Type | Purpose |
|---|---|---|
| `(channel_id, status)` | Compound | Fast filtered queries |
| `video_id` | Unique | Fast lookup by UUID |

**Status lifecycle:**

- `published` → Video is live on YouTube; `published_at` is set at this transition (reconciled by the sync endpoint)

---

### Video Timestamp Logic

The system maintains several timestamps to track a video's lifecycle. All timestamps are in **IST (GMT+5:30)**.

#### `created_at`

- **Manual/AI Generation**: Set to the time the "todo" idea was generated.
- **YouTube Sync**: Set to the **YouTube publish date**. This preserves the historical timeline for imported videos.
- **Purpose**: Represents the conceptual "birth" of the video entry in our system.

#### `updated_at`

- **Trigger**: Updated on every metadata change, status transition, or category move.
- **Purpose**: Tracks the last time the database record was modified.

#### `scheduled_at`

- **Initial State**: `null`.
- **Trigger**: Set during the **Schedule** operation when a future publish slot is calculated or manually provided.
- **YouTube Sync**: If a video on YouTube has `status.publishAt` set to a future time (i.e. it was uploaded as private with a scheduled go-live time), the sync flow imports it with status `scheduled` and sets `scheduled_at` to that time.
- **Cleanup**: Cleared if the video is moved back to `todo` or `ready`.
- **Purpose**: Represents the target time for the video to go live on YouTube.

#### `published_at`

- **Initial State**: `null`.
- **Manual Update**: Set to current time when status is manually set to `published`.
- **YouTube Sync/Reconciliation**: Set to the actual publish time from the YouTube API.
- **YouTube Sync (scheduled video)**: Left as `null` when a video is imported as `scheduled` (it hasn't gone live yet).
- **Purpose**: Represents the definitive time the video became public.

#### `added_at` (Queue Specific)

- **`posting_queue`**: Set when the file is uploaded to R2, marking it as `ready`.
- **`schedule_queue`**: Set when the video is successfully pushed to YouTube with a schedule.
- **Purpose**: Tracks how long an item has been waiting in a specific processing queue.

---

---

### Collection: `posting_queue` (Ready Queue)

The **ready queue**. Stores videos that are **ready** — uploaded to R2 but not yet scheduled on YouTube. Each entry references a video by `video_id`.

```json
{
  "_id": "ObjectId",
  "channel_id": "tech-tips",
  "video_id": "550e8400-...", // references videos.video_id
  "position": 1, // 1-based ordering
  "added_at": "datetime"
}
```

**Indexes:**
| Fields | Type | Purpose |
|---|---|---|
| `(channel_id, position)` | Compound | Fast ordered queue retrieval |

**Notes:**

- Entries are removed when the video is successfully scheduled on YouTube (moved to the scheduled queue).
- Position determines the display order.

---

### Collection: `schedule_queue` (Scheduled Queue)

The **scheduled queue**. Stores videos that are **scheduled** — already uploaded to YouTube as private with a `publishAt` time, waiting for YouTube to auto-publish. Each entry references a video by `video_id` and includes the target publish time.

```json
{
  "_id": "ObjectId",
  "channel_id": "tech-tips",
  "video_id": "550e8400-...", // references videos.video_id
  "position": 1, // 1-based ordering
  "scheduled_at": "datetime", // timezone-aware publish time (computed from best_posting_times)
  "added_at": "datetime"
}
```

**Indexes:**
| Fields | Type | Purpose |
|---|---|---|
| `(channel_id, position)` | Compound | Fast ordered queue retrieval |

**Notes:**

- Entries are added when a video is successfully uploaded to YouTube as private with `publishAt` (during the schedule operation).
- Entries are removed when the sync endpoint reconciles them as `published` (YouTube has auto-published them).
- Position determines display order in the queue view.

---

### Collection: `categories`

Stores content categories with their performance scores.

```json
{
  "_id": "ObjectId",
  "channel_id": "tech-tips",
  "name": "Tutorials",
  "description": "How-to videos and walkthroughs",
  "raw_description": "Original user input",
  "score": 85.5, // 0-100, updated by analysis engine
  "status": "active", // "active" or "archived"
  "video_count": 12, // published eligible videos (same set as metadata)
  "video_ids": ["uuid1", "uuid2"], // video_id of eligible videos (published, 3+ days old)
  "metadata": {
    "total_videos": 12, // published videos in this category
    "avg_views": 1500.0,
    "avg_likes": 15.5,
    "avg_comments": 3.2,
    "avg_duration_seconds": 28.0,
    "avg_engagement_rate": 1.25, // avg (likes+comments)/views × 100
    "avg_like_rate": 1.03,
    "avg_comment_rate": 0.22,
    "avg_percentage_viewed": 72.5, // from YouTube Analytics API
    "avg_view_duration_seconds": 20, // from YouTube Analytics API
    "total_views": 18000,
    "total_estimated_minutes_watched": 560.0
  },
  "created_at": "datetime",
  "updated_at": "datetime"
}
```

**Indexes:**
| Fields | Type | Purpose |
|---|---|---|
| `(channel_id, status, score)` | Compound | Fast sorted queries by active/score |

**Auto-archiving:** The to-do engine archives categories when:

- Score drops below **30** AND
- Category has **≥ 5 videos** (enough data to be statistically meaningful)

**Backfilling `video_ids`:** For existing categories created before `video_ids` was added, run `python backfill_category_video_ids.py [channel_id ...]` from the project root (with dependencies installed). With no arguments it processes all channels; otherwise pass one or more `channel_id` values.

---

### Collection: `analysis`

Stores the AI-generated channel summary. **One document per channel.**

```json
{
  "_id": "ObjectId",
  "channel_id": "tech-tips",
  "subscriber_count": 5000, // channel's subscriber count at last analysis
  "best_posting_times": [
    {
      "day_of_week": "monday",
      "video_count": 2,
      "times": ["10:00", "14:00"]
    }
  ],
  "category_analysis": [
    {
      "category": "Tutorials",
      "best_title_patterns": ["How to...", "10 Things..."],
      "score": 85.5
    }
  ],
  "content_param_analysis": [
    {
      "param_name": "simulation_type",
      "best_values": ["battle", "survival"],
      "worst_values": ["puzzle"],
      "insight": "Battle simulations get 3x more engagement"
    }
  ],
  "best_combinations": [
    {
      "params": {
        "simulation_type": "battle",
        "challenge_mechanic": "1v1",
        "music": "Epic Orchestral"
      },
      "reasoning": "Highest avg_percentage_viewed at 72%"
    }
  ],
  "analysis_done_video_ids": ["vid1", "vid2"], // tracks which videos have been analysed
  "version": 3, // auto-incremented
  "created_at": "datetime",
  "updated_at": "datetime"
}
```

**Indexes:**
| Fields | Type | Purpose |
|---|---|---|
| `channel_id` | Unique | One analysis doc per channel |

---

### Collection: `analysis_history`

Per-video analysis storage — **one document per video**, created once and never re-analysed. Each document contains the video's stats snapshot at analysis time plus AI-generated insights.

```json
{
  "_id": "ObjectId",
  "channel_id": "tech-tips",
  "video_id": "uuid-1234",
  "youtube_video_id": "dQw4w...",
  "title": "Epic Battle Simulation",
  "category": "Simulations",
  "content_params": {
    "simulation_type": "battle",
    "challenge_mechanic": "1v1",
    "music": "Epic Orchestral - Two Steps From Hell"
  },
  "published_at": "datetime", // when the video was published on YouTube (IST)
  "stats_snapshot": {
    "views": 15000,
    "likes": 800,
    "comments": 45,
    "duration_seconds": 35,
    "engagement_rate": 5.63,
    "like_rate": 5.33,
    "comment_rate": 0.3,
    "avg_percentage_viewed": 72.5,
    "avg_view_duration_seconds": 25,
    "estimated_minutes_watched": 6250.0,
    "subscribers_gained": 120, // from YouTube Analytics API
    "views_per_subscriber": 3.0, // views / channel subscriber count
    "subscriber_count_at_analysis": 5000 // channel subs when analysed
  },
  "ai_insight": {
    "performance_rating": 85, // 0-100 score
    "what_worked": "Strong title hook + battle format drove high CTR",
    "what_didnt": "Could improve description SEO for discoverability",
    "key_learnings": [
      "Battle sims drive 3x engagement vs other types",
      "Epic music correlates with higher avg_percentage_viewed"
    ]
  },
  "analyzed_at": "datetime"
}
```

**Indexes:**
| Fields | Type | Purpose |
|---|---|---|
| `(channel_id, video_id)` | Compound (unique) | One analysis per video per channel |
| `(channel_id, analyzed_at)` | Compound (desc) | Fast reverse-chronological queries and date filtering |

---

## Services Architecture

### R2 Service (`app/services/r2.py`)

- **Upload**: Streams file to R2 using `upload_fileobj` (never loads full file into memory)
- **Download**: Streams file from R2 to a temp file, returns the temp file path
- **Delete**: Removes an object from R2
- **Object key format**: `{channel_id}/{video_id}.mp4`

### YouTube Service (`app/services/youtube.py`)

- **Per-channel tokens (DB-stored)**: Each channel has its own OAuth tokens stored in the `youtube_tokens` field of its document in the `channels` collection. This ensures analytics data is fetched from the correct account and uploads go to the right channel
- **YouTubeServiceManager**: Manages per-channel `YouTubeService` instances. Reads tokens from the DB, lazily creates and caches service instances. If a channel has no token, endpoints return a clear error
- **Token provisioning**: The frontend completes the Google OAuth consent flow in the browser, then POSTs the resulting tokens to `POST /channels/{channel_id}/youtube-token`. The backend stores them on the channel document
- **Access token serving**: The frontend can call `GET /channels/{channel_id}/youtube-token` to get a fresh short-lived access token. If expired, the backend auto-refreshes it using the refresh token and returns the new one. The refresh token is never exposed to the frontend
- **Token status**: `GET /channels/{channel_id}/youtube-token/status` returns connection status without exposing tokens
- **Client credentials**: Google OAuth `client_id` and `client_secret` are stored in the `config` collection (key: `youtube_oauth`), with a fallback to `.env` for backward compatibility. Set via `PUT /channels/config/youtube-oauth`
- **Auth**: OAuth2 with auto-refresh. Scopes: `youtube.upload`, `youtube.readonly`, `yt-analytics.readonly`
- **Get channel info**: Fetches channel metadata (name, subscribers, etc.)
- **Get video stats**: Fetches views, likes, comments, duration (from Data API `statistics` + `contentDetails`), plus computed engagement/like/comment rates. Also merges YouTube Analytics data (avg % viewed, avg view duration, estimated minutes watched) when available
- **Get video analytics**: Queries the YouTube Analytics API for `averageViewPercentage`, `averageViewDuration`, and `estimatedMinutesWatched` per video. Batches by 40 IDs. Returns empty data for videos less than ~48 hours old (YouTube Analytics processing delay)
- **Upload video**: Resumable upload in 10MB chunks, defaults to private. Accepts an optional `publish_at` ISO 8601 UTC datetime — when provided, the video is uploaded as private with YouTube's `publishAt` field so it auto-publishes at the scheduled time

### Timezone Helper (`app/timezone.py`)

- **`IST`** — a `timezone(timedelta(hours=5, minutes=30))` constant
- **`now_ist()`** — returns the current datetime in IST, timezone-aware
- Used by all models, routers, and services as the single source of truth for timestamps

### Scheduler Service (`app/services/scheduler.py`)

- **Compute schedule slots**: Takes `best_posting_times` from analysis, a list of already-occupied datetimes, and the number of videos to schedule
- Builds a weekly slot calendar from `best_posting_times` (day + time pairs)
- Starts from the current moment, walks forward week by week, skipping past and occupied slots
- Returns timezone-aware datetimes (timezone from `TIMEZONE` env var, default `Asia/Kolkata`)
- Safety cap of 52 weeks forward

### YouTube Service (`app/services/youtube.py`) — Additional Methods

- **Get subscribers gained**: `get_subscribers_gained(youtube_video_ids)` — queries YouTube Analytics API for `subscribersGained` per video. Returns `dict[youtube_video_id, int]`. Batches by 40 IDs.

### Gemini Service (`app/services/gemini.py`)

- **Analyze single video**: `analyze_single_video(video_data)` — sends a single video's title, content_params, and stats (including `subscribers_gained`, `views_per_subscriber`) to Gemini → returns `performance_rating` (0-100), `what_worked`, `what_didnt`, `key_learnings`. The `performance_rating` uses fixed weightage: `subscribers_gained` 25%, `avg_percentage_viewed` 25%, `views` 20%, `engagement_rate` 10%, `comments` 8%, `likes` 5%, `views_per_subscriber` 5%, `estimated_minutes_watched` 2%
- **Analyze videos (channel summary)**: Sends aggregated per-video data (now including `ai_insight` per video) + previous analysis + content params from `content_params` collection → returns updated channel summary JSON
- **Generate content**: Given a category + its analysis insights + content params (filtered by `belongs_to`) + content_param_analysis + best_combinations → generates title, description, tags, and `content_params` (with music) for new videos
- **Model fallback chain**: Tries models in order — if one fails (rate limit, error), automatically falls back to the next:
  1. `gemini-3.1-pro-preview`
  2. `gemini-3-pro-preview`
  3. `gemini-3-flash-preview`
- **Output**: Forces JSON response via `response_mime_type="application/json"`

### Analysis Engine (`app/services/analysis_engine.py`)

Two-step pipeline:

1. **Step 1 — Per-video analysis**: For each published video not yet in `analysis_history` (excluding `verification_status: "unverified"`): fetch subscriber count + subscribers gained → build stats snapshot with `views_per_subscriber` → send to Gemini individually → store in `analysis_history`
2. **Step 2 — Channel summary**: Aggregate all per-video analyses → send to Gemini in batches of 5 (with AI insights) → save channel summary to `analysis` with `subscriber_count` → run to-do engine

### To-do Engine (`app/services/todo_engine.py`)

Post-analysis step:

1. Updates all category scores from Gemini analysis
2. Increments category video_count for newly analysed videos
3. Computes and saves **category metadata** — aggregated performance metrics (avg views, likes, comments, engagement rates, avg % viewed, total views, total watch time) from published verified videos in each category (excludes unverified)
4. Archives underperforming categories (score < 30, ≥ 5 videos)

To-do video generation is triggered separately via the `/updateToDoList` endpoint, which distributes N slots across active categories weighted by score. The engine fetches only content params whose `belongs_to` includes the current category name or `"all"` (not all params). Generated videos include `content_params` (with music recommendations) set to `verification_status: "verified"`

---

## Data Flow Diagrams

### Video Status Lifecycle

```mermaid
stateDiagram-v2
    [*] --> todo: Gemini generates idea\n(updateToDoList)
    todo --> ready: Client uploads file\n(POST /upload)
    ready --> scheduled: Server uploads to YouTube\nwith publishAt\n(POST /schedule)
    scheduled --> published: YouTube auto-publishes\nat scheduled time\n(reconciled by /sync)

    note right of todo: Idea only.\nNo file exists yet.
    note right of ready: File in R2.\nSitting in ready queue.
    note right of scheduled: Private on YouTube\nwith publishAt set.\nSitting in scheduled queue.
    note right of published: Live on YouTube.\nQueue entries removed.
```

### System Architecture

```mermaid
flowchart TB
    subgraph client [Client]
        VideoCreator["Video Creator\n(makes the actual videos)"]
    end

    subgraph server [Automation Server]
        API["FastAPI API"]
        AnalysisEngine["Analysis Engine"]
        TodoEngine["To-do Engine"]
        Scheduler["Scheduler Service"]
        GeminiSvc["Gemini Service"]
        YTSvc["YouTube Service"]
        R2Svc["R2 Service"]
    end

    subgraph external [External Services]
        MongoDB[(MongoDB Atlas)]
        R2[(Cloudflare R2)]
        YouTube["YouTube API"]
        Gemini["Google Gemini AI"]
    end

    VideoCreator -->|"GET /videos\n(fetch todo list)"| API
    VideoCreator -->|"POST /upload\n(send video file)"| API

    API --> AnalysisEngine
    API --> TodoEngine
    API --> Scheduler

    AnalysisEngine --> GeminiSvc
    AnalysisEngine --> YTSvc
    TodoEngine --> GeminiSvc

    Scheduler -->|"compute slots from\nbest_posting_times"| API

    GeminiSvc --> Gemini
    YTSvc --> YouTube
    R2Svc --> R2
    API --> MongoDB
```

### Video Upload and Schedule Flow

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server
    participant R2 as Cloudflare R2
    participant YT as YouTube

    Note over C,YT: Phase 1: Upload video file

    C->>S: POST /videos/{id}/upload (file)
    S->>R2: Stream file upload
    R2-->>S: OK
    S->>S: Update video: status=ready, r2_object_key
    S->>S: Insert into ready queue
    S-->>C: 201 {video, queue_position}

    Note over C,YT: Phase 2: Schedule (upload to YouTube)

    C->>S: POST /videos/{id}/schedule (or "all")
    S->>S: Load best_posting_times from analysis
    S->>S: Find occupied slots in scheduled queue
    S->>S: Compute next available publish slot(s)

    loop For each video
        S->>R2: Download video to temp file
        R2-->>S: File stream
        S->>YT: Resumable upload (private + publishAt)
        YT-->>S: youtube_video_id
        S->>S: Set youtube_video_id, scheduled_at, status=scheduled
        S->>S: Remove from ready queue
        S->>S: Insert into scheduled queue
        S->>S: Delete temp file
    end

    S-->>C: {scheduled: N, failed: M, videos: [...]}

    Note over C,YT: Phase 3: Auto-publish and reconcile

    Note over YT: YouTube auto-publishes at publishAt time
    C->>S: POST /videos/sync
    S->>YT: Check video status
    YT-->>S: Video is public
    S->>S: Set status=published, published_at
    S->>S: Remove from scheduled queue
```

### Sync Flow

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server
    participant YT as YouTube Data API
    participant YTA as YouTube Analytics API
    participant G as Gemini AI
    participant DB as MongoDB

    C->>S: POST /videos/sync

    S->>YT: Fetch all video IDs (uploads playlist)
    YT-->>S: Video IDs (paginated)
    S->>YT: Fetch snippet + stats + contentDetails + status (batches of 50)
    YT-->>S: Title, views, likes, duration, privacyStatus, publishAt, etc.
    S->>YTA: Fetch analytics (avg % viewed, watch time)
    YTA-->>S: Analytics data

    Note over S,DB: Step 1: Refresh existing videos
    S->>DB: Update metadata for all existing published videos

    Note over S,DB: Step 2: Reconcile scheduled videos
    S->>DB: Find scheduled videos that are now live on YouTube
    S->>DB: Mark as published, remove from scheduled queue

    Note over S,G: Step 3: Categorize new videos
    loop Batches of 5 new videos
        S->>G: Extract content_params + derive category
        G-->>S: {content_params, category} per video
        S->>DB: Auto-create new categories if needed
    end

    Note over S,DB: Step 4: Insert new videos
    alt Video has future status.publishAt
        S->>DB: Insert as scheduled (scheduled_at = publishAt)
        S->>DB: Add entry to schedule_queue
    else Video is public
        S->>DB: Insert as published
    end

    S-->>C: {synced, synced_published, synced_scheduled, reconciled, metadata_refreshed, videos}
```

### Analysis Flow

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server
    participant YT as YouTube API
    participant YTA as YouTube Analytics API
    participant G as Gemini AI
    participant DB as MongoDB

    C->>S: POST /analysis/update

    S->>YT: Fetch subscriber count
    YT-->>S: subscriber_count

    S->>DB: Fetch published videos
    S->>DB: Check analysis_history for already-analysed video_ids
    S->>S: Compute delta + exclude videos < 3 days old + exclude verification_status=unverified

    S->>YT: Fetch stats for new videos
    YT-->>S: views, likes, comments, duration
    S->>YTA: Fetch subscribers gained per video
    YTA-->>S: subscribers_gained mapping

    Note over S,G: Step 1: Per-video analysis
    loop For each new video
        S->>S: Build stats snapshot (+ views_per_subscriber, subscribers_gained)
        S->>G: Analyze single video
        G-->>S: {performance_rating, what_worked, what_didnt, key_learnings}
        S->>DB: Insert into analysis_history (one doc per video)
    end

    Note over S,G: Step 2: Channel summary
    S->>DB: Fetch ALL per-video analyses for channel
    loop Batches of 5 per-video analyses
        S->>G: Send batch (stats + ai_insight) + running analysis + content_params
        G-->>S: Updated channel summary
    end

    S->>DB: Save channel summary to analysis (version++, subscriber_count)
    S->>DB: Update content_params collection (value scores, video_count from analysis_history)

    Note over S,DB: Post-analysis: To-do engine
    S->>DB: Update category scores from Gemini output
    S->>DB: Increment category video_count
    S->>DB: Compute and save category metadata
    S->>DB: Archive categories (score < 30 and >= 5 videos)

    S-->>C: Updated analysis document
```

### To-do Video Generation Flow

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server
    participant G as Gemini AI
    participant DB as MongoDB

    C->>S: POST /updateToDoList {n: 5}

    S->>DB: Fetch active categories (with analysis insights)
    S->>DB: Fetch content_params where belongs_to includes category or "all"
    S->>DB: Fetch latest analysis (category_analysis, content_param_analysis, best_combinations)

    S->>S: Distribute N slots across categories weighted by score

    loop For each category with slots
        S->>DB: Fetch existing titles + content_params (to avoid duplicates)
        S->>G: Generate ideas (category insights + content_params + existing titles)
        G-->>S: [{title, description, tags, content_params}, ...]
        S->>DB: Insert new videos (status=todo, verification_status=verified)
    end

    S-->>C: {ok: true, message: "Generated N videos"}
```

### Content Params Extraction Flow

```mermaid
sequenceDiagram
    participant C as Client
    participant S as Server
    participant G as Gemini AI
    participant DB as MongoDB

    C->>S: POST /extract-params/{video_id} (or /extract-params/all)

    S->>DB: Fetch content_params for channel (from content_params collection)
    S->>DB: Fetch video(s) needing extraction

    loop For each video
        S->>G: Extract params from title + description + tags using content_params
        G-->>S: {param1: value1, param2: value2, music: "..."}
        S->>DB: Save content_params on video, set verification_status=unverified
    end

    S-->>C: {ok, extracted, content_params}

    Note over C,S: Later: client reviews and verifies
    C->>S: POST /verify-params/{video_id} (optional corrections)
    S->>DB: Set verification_status=verified
```

### Scheduling Slot Computation

```mermaid
flowchart TD
    A["Load best_posting_times\nfrom analysis"] --> B["Build weekly slot calendar\n(day + time pairs)"]
    B --> C["Load occupied slots\nfrom scheduled queue"]
    C --> D["Start from current time\nin IST (GMT+5:30)"]
    D --> E{"Slot in the past?"}
    E -->|Yes| F["Skip"]
    E -->|No| G{"Slot already\noccupied?"}
    G -->|Yes| F
    G -->|No| H["Assign to next video"]
    H --> I{"All videos\nassigned?"}
    I -->|No| J["Move to next slot\n(same week or next week)"]
    J --> E
    I -->|Yes| K["Return list of\ntimezone-aware datetimes"]
    F --> J
```
