# Frontend API Reference

**Base URL**: `http://localhost:8000`  
_(Or `http://68.233.115.135:8000` in production)_

**Authentication**: All requests under `/api/v1/` require the following header:

```http
X-API-Key: <your-api-key>
```

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
  - `suggest_n=3` (optional, brings top N suggestions first)
- **Response**: Array of Video objects.

```json
[
  {
    "_id": "651f8a8...",
    "channel_id": "ch1",
    "video_id": "uuid-1234",
    "title": "How to code",
    "description": "...",
    "tags": ["coding", "tutorial"],
    "category": "Tutorials",
    "topic": "Python basics",
    "status": "todo",
    "suggested": false,
    "basis_factor": "Auto-generated...",
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
    }
  }
]
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

- **Response**:

```json
{
  "status": "updated",
  "new_status": "published"
}
```

### Upload to Queue (Mark Ready)

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/{video_id}/queue`
- **Method**: `POST`
- **Content-Type**: `multipart/form-data`
- **Form Fields**:
  - `file`: The actual video file.
- **Description**: Uploads the video file to R2 for an existing `todo` video, changing its status to `ready` and placing it in the posting queue.
- **Response**: Returns the updated Video object (status becomes `ready`) and `queue_position`.

### Schedule Ready Video(s)

- **Endpoint**: `/api/v1/channels/{channel_id}/videos/{video_id}/schedule`
- **Method**: `POST`
- **Path Params**: `video_id` — a specific video UUID **OR** `"all"` to schedule every video in the posting queue.
- **Description**: Moves video(s) from `ready` (posting_queue) to `scheduled` (schedule_queue). Computes `scheduled_at` publish times from the channel's `best_posting_times` analysis, skipping any slots already occupied by previously scheduled videos. Requires an analysis with `best_posting_times` to exist.
- **Response**:

```json
{
  "ok": true,
  "scheduled_count": 2,
  "videos": [
    {
      "video_id": "550e8400-...",
      "title": "10 VS Code Tricks",
      "scheduled_at": "2026-03-10T10:00:00+05:30",
      "schedule_position": 1
    },
    {
      "video_id": "660f9500-...",
      "title": "iPhone 16 Review",
      "scheduled_at": "2026-03-10T14:00:00+05:30",
      "schedule_position": 2
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

- **Response**:

```json
{
  "status": "sync started in background"
}
```

---

## Analysis

### Get Latest Analysis

- **Endpoint**: `/api/v1/channels/{channel_id}/analysis/latest`
- **Method**: `GET`
- **Response**: Analysis object.

```json
{
  "channel_id": "ch1",
  "version": 2,
  "category_analysis": [
    {
      "category": "Tutorials",
      "score": 85.5,
      "reasoning": "High engagement on these videos."
    }
  ],
  "best_times_to_post": ["14:00", "18:00"]
}
```

### Trigger Analysis Update

- **Endpoint**: `/api/v1/channels/{channel_id}/analysis/update`
- **Method**: `POST`
- **Description**: Recalculates category scores and analysis using YouTube stats and Gemini.
- **Response**: Returns the new Analysis object.

### Update To-Do List (Generate Videos)

- **Endpoint**: `/api/v1/channels/{channel_id}/analysis/updateToDoList`
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

## Posting

### View Schedule Queue

- **Endpoint**: `/api/v1/channels/{channel_id}/posting/queue`
- **Method**: `GET`
- **Response**: Array of queue items with scheduled publish times.

```json
[
  {
    "position": 1,
    "video_id": "uuid-1234",
    "added_at": "2026-03-01T12:00:00Z",
    "scheduled_at": "2026-03-10T10:00:00+05:30",
    "title": "10 VS Code Tricks",
    "category": "Tutorials"
  }
]
```

### Upload All Scheduled

- **Endpoint**: `/api/v1/channels/{channel_id}/posting/upload-all`
- **Method**: `POST`
- **Description**: Triggers the server to pop all videos from the `schedule_queue` and upload them to YouTube one-by-one. Each video is uploaded as private with YouTube's `publishAt` set to the video's `scheduled_at` time, so YouTube auto-publishes at the correct moment.
- **Response**:

```json
{
  "ok": true,
  "uploaded": 2,
  "failed": 0,
  "details": [
    {
      "video_id": "uuid-1234",
      "status": "uploaded",
      "youtube_video_id": "dQw4w..."
    }
  ]
}
```
