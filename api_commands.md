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
- **Request Body (YouTube)**:

```json
{
  "platform": "youtube",
  "youtube_channel_id": "UCxxxxxxxx",
  "channel_id": "optional-custom-slug"
}
```

- **Request Body (Instagram)**:

```json
{
  "platform": "instagram",
  "instagram_user_id": "17841400123456789",
  "channel_id": "my-channel-ig"
}
```

- **Description**: `platform` defaults to `"youtube"`. For YouTube, `youtube_channel_id` is required. For Instagram, `instagram_user_id` is required. `channel_id` is auto-generated if omitted.
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
- **Description**: Re-fetches name and stats from the appropriate platform (YouTube or Instagram, based on channel's `platform` field).
- **Response**: Updated Channel object.

### Content Params (CRUD)

Content params are custom dimensions for classifying videos. Manage them with these endpoints:

#### List Content Params

- **Endpoint**: `/api/v1/channels/{channel_id}/content-params`
- **Method**: `GET`
- **Description**: Returns all content params for the channel.
- **Response**: Array of param objects with `name`, `description`, `values`, `belongs_to`, `unique`.

#### Add Content Param

- **Endpoint**: `/api/v1/channels/{channel_id}/content-params`
- **Method**: `POST`
- **Request Body**:

```json
{
  "name": "simulation_type",
  "description": "Type of simulation",
  "values": ["battle", "survival", "puzzle"],
  "belongs_to": ["all"],
  "unique": false
}
```

- **Description**: `values` is a list of strings. `belongs_to` defaults to `["all"]` if omitted. `unique` (default `false`) — if `true`, the TODO generator tells Gemini not to reuse already-used values for this param.
- **Response**: Created param object.

#### Update Content Param

- **Endpoint**: `/api/v1/channels/{channel_id}/content-params/{param_name}`
- **Method**: `PUT`
- **Request Body** (all fields optional):

```json
{
  "description": "Updated description",
  "values": ["battle", "survival", "puzzle", "adventure"],
  "belongs_to": ["all"],
  "unique": true
}
```

- **Response**: Updated param object.

#### Delete Content Param

- **Endpoint**: `/api/v1/channels/{channel_id}/content-params/{param_name}`
- **Method**: `DELETE`
- **Response**: `{"ok": true, "param_name": "...", "deleted": true}`

### Competitors

#### List Competitors

- **Endpoint**: `/api/v1/channels/{channel_id}/competitors`
- **Method**: `GET`
- **Response**:

```json
{
  "channel_id": "physicsasmr_official",
  "competitors": [
    {
      "channel_id": "physicsasmr_official",
      "youtube_channel_id": "UC...",
      "handle": "@MrBeast",
      "name": "MrBeast",
      "thumbnail": "https://...",
      "created_at": "2026-03-07T12:00:00+05:30"
    }
  ]
}
```

#### Add Competitor

- **Endpoint**: `/api/v1/channels/{channel_id}/competitors`
- **Method**: `POST`
- **Request Body**:

```json
{
  "youtube_channel_id": "UC...",
  "handle": "@MrBeast",
  "name": "MrBeast",
  "thumbnail": "https://..."
}
```

- **Response**: The created competitor document (201). Returns 409 if the competitor already exists for this channel.

#### Remove Competitor

- **Endpoint**: `/api/v1/channels/{channel_id}/competitors/{youtube_channel_id}`
- **Method**: `DELETE`
- **Response**: `{"ok": true, "deleted": "UC..."}`

---

### YouTube OAuth Config

#### Set YouTube OAuth Client Credentials

- **Endpoint**: `/api/v1/channels/config/youtube-oauth`
- **Method**: `PUT`
- **Request Body**:

```json
{
  "client_id": "818394441499-...",
  "client_secret": "GOCSPX-..."
}
```

- **Description**: Stores the Google OAuth client ID and secret in the DB. Replaces the old `.env` variables `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET`.
- **Response**: `{"ok": true, "message": "YouTube OAuth config saved"}`

#### Check YouTube OAuth Config

- **Endpoint**: `/api/v1/channels/config/youtube-oauth`
- **Method**: `GET`
- **Description**: Returns whether client credentials are configured (and the `client_id` for verification). Never exposes the secret.
- **Response**: `{"configured": true, "client_id": "818394441499-..."}`

### YouTube Token Management

#### Store YouTube Token

- **Endpoint**: `/api/v1/channels/{channel_id}/youtube-token`
- **Method**: `POST`
- **Request Body**:

```json
{
  "token": "ya29.a0ARrdaM...",
  "refresh_token": "1//0eXyz...",
  "token_uri": "https://oauth2.googleapis.com/token",
  "scopes": ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly", "https://www.googleapis.com/auth/youtube.force-ssl", "https://www.googleapis.com/auth/yt-analytics.readonly"],
  "expiry": "2026-03-07T12:00:00Z"
}
```

- **Description**: Called by the frontend after the user completes the Google OAuth consent flow. Stores the tokens on the channel document in the DB. Invalidates any cached YouTube service for the channel.
- **Response**: `{"ok": true, "channel_id": "...", "message": "YouTube tokens stored"}`

#### Get Fresh Access Token

- **Endpoint**: `/api/v1/channels/{channel_id}/youtube-token`
- **Method**: `GET`
- **Description**: Returns a fresh access token for the channel. If the stored token is expired, it is automatically refreshed using the refresh token and saved back. **Only the short-lived access token is returned — never the refresh token.**
- **Response**:

```json
{
  "ok": true,
  "access_token": "ya29.a0ARrdaM...",
  "expiry": "2026-03-07T13:00:00Z"
}
```

#### Check YouTube Token Status

- **Endpoint**: `/api/v1/channels/{channel_id}/youtube-token/status`
- **Method**: `GET`
- **Description**: Returns whether a YouTube token exists and its status, without exposing any token values. Useful for the frontend to show "connected" / "disconnected" / "expired" state.
- **Response**:

```json
{
  "channel_id": "officialgeoranking",
  "connected": true,
  "status": "active",
  "has_refresh_token": true,
  "expiry": "2026-03-07T13:00:00Z"
}
```

- **Status values**: `"disconnected"` (no tokens), `"active"` (valid), `"expired_refreshable"` (expired but has refresh token — will auto-refresh on GET), `"expired"` (expired, no refresh token — re-auth needed)

### Instagram OAuth Config

#### Set Instagram OAuth Credentials

- **Endpoint**: `/api/v1/channels/config/instagram-oauth`
- **Method**: `PUT`
- **Request Body**:

```json
{
  "app_id": "123456789012345",
  "app_secret": "abc123def456..."
}
```

- **Description**: Stores the Facebook App ID and secret in the DB for Instagram Graph API access.
- **Response**: `{"ok": true, "message": "Instagram OAuth config saved"}`

#### Check Instagram OAuth Config

- **Endpoint**: `/api/v1/channels/config/instagram-oauth`
- **Method**: `GET`
- **Response**: `{"configured": true, "app_id": "123456789012345"}`

### Instagram Token Management

#### Store Instagram Token

- **Endpoint**: `/api/v1/channels/{channel_id}/instagram-token`
- **Method**: `POST`
- **Request Body**:

```json
{
  "access_token": "EAAGm0PX4Zx...",
  "expires_at": "2026-05-07T12:00:00Z"
}
```

- **Description**: Called by the frontend after the user completes the Facebook Login OAuth flow. Stores the long-lived token on the channel document.
- **Response**: `{"ok": true, "channel_id": "...", "message": "Instagram token stored"}`

#### Get Instagram Access Token

- **Endpoint**: `/api/v1/channels/{channel_id}/instagram-token`
- **Method**: `GET`
- **Description**: Returns the current access token. Auto-refreshes if < 7 days remain.
- **Response**: `{"ok": true, "access_token": "EAAGm0PX4Zx...", "expires_at": "2026-05-07T12:00:00Z"}`

#### Check Instagram Token Status

- **Endpoint**: `/api/v1/channels/{channel_id}/instagram-token/status`
- **Method**: `GET`
- **Description**: Returns token connection status without exposing the token value.
- **Response**: `{"channel_id": "...", "connected": true, "status": "active", "expires_at": "2026-05-07T12:00:00Z"}`

### Delete Channel

- **Endpoint**: `/api/v1/channels/{channel_id}`
- **Method**: `DELETE`
- **Description**: Removes channel and ALL associated data (videos, categories, analysis, queues). Also deletes all associated video files from R2 storage.
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
- **Description**: Partially update a category. If the `name` is changed, all videos and analysis history records in that category are automatically updated to the new name.
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

### Delete Category

- **Endpoint**: `/api/v1/channels/{channel_id}/categories/{category_object_id}`
- **Method**: `DELETE`
- **Description**: Deletes a category. All videos belonging to this category are moved to "Uncategorized" to maintain data integrity.
- **Response**: `{"ok": true, "category_id": "...", "deleted": true}`

---

## Videos

### List Videos

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/`
- **Method**: `GET`
- **Query Params**:
  - `status_filter=todo|ready|scheduled|published` (optional)
  - `verification_status=unverified|verified|missing` (optional, filter by param verification status)
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

- **Notes**:
  - When status is set to `published`, `published_at` is automatically set to the current time.
  - If moving FROM `ready` TO `todo` or `published`, the video is automatically removed from the ready queue and its file is deleted from Cloudflare R2.
  - If moving TO `todo`, scheduling info (`scheduled_at`) is cleared and it's removed from the scheduled queue.

- **Response**:

```json
{
  "status": "updated",
  "new_status": "published"
}
```

### Change Video Category

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/{video_id}/category`
- **Method**: `PATCH`
- **Request Body**:

```json
{
  "old_category_id": "65f...",
  "new_category_id": "65f..."
}
```

- **Description**: Moves a video from one category to another. Updates the video document and the per-video record in `analysis_history`; recomputes metadata, `video_count`, and `video_ids` for both categories. Category IDs are MongoDB `_id` values.
- **Response**: `{"ok": true, "video_id": "...", "old_category": "Tutorials", "new_category": "Reviews"}`

### Delete Video

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/{video_id}`
- **Method**: `DELETE`
- **Description**: Permanently deletes a video and its assets. Cleans up R2 storage, removes it from all queues (posting/schedule), updates category video counts, and deletes analysis history.
- **Response**: `{"ok": true, "video_id": "...", "deleted": true}`

### Extract Content Params

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/{video_id}/extract-params`
- **Method**: `POST`
- **Description**: Uses Gemini to extract content parameter values from a video's title, description, and tags based on the channel's `content_params` collection. Sets `verification_status: "unverified"`.
- **Response**:

```json
{
  "ok": true,
  "video_id": "uuid-1234",
  "content_params": { "simulation_type": "battle", "music": "Epic Orchestral" },
  "verification_status": "unverified"
}
```

### Bulk Extract Content Params

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/extract-params/all`
- **Method**: `POST`
- **Description**: Extracts content parameters for every video that doesn't have them yet.
- **Response**: `{"ok": true, "extracted": 42, "total": 45}`

### Verify Video (Category + Content Params)

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/{video_id}/verify-params`
- **Method**: `POST`
- **Description**: Marks a video as verified (`verification_status: "verified"`). Optionally pass corrected `category` and/or `content_params` in the body to override AI-assigned values.
- **Request Body** (optional):

```json
{
  "category": "battle",
  "content_params": { "simulation_type": "survival", "music": "Dramatic Piano" }
}
```

- **Response**: `{"ok": true, "video_id": "...", "category": "battle", "content_params": {...}, "verification_status": "verified"}`

### Upload Video File

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/{video_id}/upload`
- **Method**: `POST`
- **Content-Type**: `multipart/form-data`
- **Form Fields**:
  - `file`: The actual video file.
- **Description**: Uploads the video file to R2 for an existing `todo` video, changing its status to `ready` and placing it in the ready queue.
- **Response**: Returns the updated Video object (status becomes `ready`) and `queue_position`.

### Create Ad-hoc Video

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/create`
- **Method**: `POST`
- **Content-Type**: `multipart/form-data`
- **Form Fields**:
  - `file` (required): The video file.
  - `title` (required): Video title.
  - `description` (optional): Video description.
  - `tags` (optional): Comma-separated string or JSON array.
  - `category` (optional): Category name. If omitted, defaults to `"Uncategorized"`.
  - `content_params` (optional): JSON string of key-value content parameters.
- **Description**: Creates an ad-hoc (unplanned) video directly in `ready` status, uploads the file to R2, and adds it to the posting queue. If both `category` and `content_params` are provided, the video is marked `verified`. Otherwise it is `unverified` with `category: "Uncategorized"` and `content_params: null` — the next sync will run Gemini extraction on it.
- **Response**: Returns the created Video object and `queue_position`.

### Schedule Ready Video(s)

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/{video_id}/schedule`
- **Method**: `POST`
- **Path params:** `video_id` — the UUID of a single video **OR** `"all"` to schedule every video in the ready queue.

**Request Body (Optional, for single video_id only):**

```json
{
  "scheduled_at": "2026-03-10T14:30:00+05:30"
}
```

- **Description**: Schedules video(s) on the channel's platform. Computes `scheduled_at` publish times from the channel's `best_posting_times` analysis (unless manually provided). **YouTube**: downloads from R2, uploads to YouTube as private with `publishAt` immediately. **Instagram**: queues for the background auto-publisher (polls every 5 min) which uploads and publishes the reel when `scheduled_at` arrives. Requires an analysis with `best_posting_times` and a valid platform token.
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

### Sync Videos from Platform

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/sync`
- **Method**: `POST`
- **Request Body**: (Optional, to provide classification instructions to Gemini)

```json
{
  "new_category_description": "Keep categories broad like Rankings, Comparisons, etc."
}
```

- **What it does** (auto-detects platform from channel's `platform` field):
  - **YouTube**: Fetches all videos from the YouTube channel, refreshes metadata, reconciles scheduled→published, imports new videos with content params + category via Gemini. Also processes any existing unverified videos (e.g. ad-hoc uploads) through Gemini extraction.
  - **Instagram**: Fetches all reels via Graph API, fetches per-reel insights (views, reach, saves, shares), refreshes metrics for existing reels, imports new reels with content params + category via Gemini. Title is extracted from the first line of the caption, hashtags become tags. Also processes any existing unverified videos through Gemini extraction.

- **Response**:

```json
{
  "ok": true,
  "synced": 5,
  "synced_published": 4,
  "synced_scheduled": 1,
  "reconciled": 2,
  "metadata_refreshed": 45,
  "unverified_extracted": 2,
  "categories_created": ["Tutorials"],
  "videos": [{ "title": "New Video Title", "category": "Tutorials", "status": "published" }]
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
    { "day_of_week": "monday", "video_count": 2, "times": ["14:00", "18:00"] }
  ],
  "content_param_analysis": [
    {
      "param_name": "simulation_type",
      "best_values": ["battle"],
      "worst_values": ["puzzle"],
      "insight": "..."
    }
  ],
  "best_combinations": [
    {
      "params": { "simulation_type": "battle", "music": "Epic" },
      "reasoning": "..."
    }
  ],
  "analysis_status": {
    "ready_for_analysis": 3,
    "unverified": 4,
    "not_ready_yet": 2
  }
}
```

- `ready_for_analysis`: published videos not yet in `analysis_history`, older than 3 days
- `unverified`: videos with extracted content params awaiting verification
- `not_ready_yet`: published videos not yet in `analysis_history`, less than 3 days old

### Trigger Analysis Update

- **Endpoint**: `/api/v1/channels/{channel_id}/analysis/update`
- **Method**: `POST`
- **Description**: Two-step pipeline: (1) per-video analysis with stats snapshot + AI insight stored in `analysis_history`, (2) channel summary aggregation. Includes subscriber count and subscribers gained per video.
- **Response**: Returns the updated channel summary.

### Delete Analysis

- **Endpoint**: `/api/v1/channels/{channel_id}/analysis/`
- **Method**: `DELETE`
- **Description**: Deletes the channel summary, all per-video analysis records, resets category scores/video_count/video_ids/metadata to zero, and zeros out content_params value scores. Forces a full re-analysis on next `POST /update`.
- **Response**: `{"ok": true, "channel_id": "...", "analysis_deleted": true, "analysis_history_deleted": 42, "categories_reset": 5, "content_params_reset": 3}`

### Get Per-Video Analyses (History)

- **Endpoint**: `/api/v1/channels/{channel_id}/analysis/history`
- **Method**: `GET`
- **Query Params**:
  - `from` (optional, string): Filter `published_at >= from` (IST). e.g. `2026-02-08` or `2026-02-08T20:00:00`
  - `to` (optional, string): Filter `published_at <= to` (IST). e.g. `2026-02-08` or `2026-02-08T23:59:59`
  - `limit` (optional, int): Max results; if omitted, returns entire history
- **Response**: Array of per-video analysis objects.

```json
[
  {
    "channel_id": "ch1",
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
    "from": "2026-02-01T00:00:00",
    "to": "2026-02-15T00:00:00",
    "video_count": 10,
    "avg_views": 12000,
    "avg_engagement_rate": 4.5,
    "total_subscribers_gained": 500,
    "avg_performance_rating": 72.3
  },
  "period_2": {
    "from": "2026-02-16T00:00:00",
    "to": "2026-03-01T00:00:00",
    "video_count": 12,
    "avg_views": 18000,
    "avg_engagement_rate": 5.8,
    "total_subscribers_gained": 850,
    "avg_performance_rating": 81.5
  }
}
```

> **Note:** To view scheduled videos, use `GET /api/v1/channels/{channel_id}/videos?status_filter=scheduled`. The `scheduled_at` field on each video shows the YouTube publish time. To schedule all ready videos at once, use `POST /api/v1/channels/{channel_id}/videos/all/schedule`.
