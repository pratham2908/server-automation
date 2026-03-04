# YouTube Automation Server – Documentation

## Table of Contents

- [Authentication](#authentication)
- [API Endpoints](#api-endpoints)
  - [Health](#health)
  - [Channels](#channels)
  - [Videos](#videos)
  - [Categories](#categories)
  - [Analysis](#analysis)
  - [Posting](#posting)
- [Database Schema](#database-schema)
  - [channels](#collection-channels)
  - [videos](#collection-videos)
  - [video_queue](#collection-video_queue)
  - [categories](#collection-categories)
  - [analysis](#collection-analysis)
- [Services Architecture](#services-architecture)
- [Data Flow Diagrams](#data-flow-diagrams)

---

## Authentication

**All endpoints** (except `/health`) require an API key passed in the `X-API-Key` header.

```
X-API-Key: your-secret-key
```

- The key is validated against the `API_KEY` value in `.env`.
- Invalid or missing keys return `401 Unauthorized`.

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

#### `DELETE /{channel_id}` — Delete a channel

**⚠️ Destructive operation.** Removes the channel AND all associated data:

- All videos in the `videos` collection for this channel
- All entries in `video_queue` for this channel
- All categories for this channel
- The analysis document for this channel

**Response (200):** `{"ok": true, "channel_id": "tech-tips", "deleted": true}`

---

### Videos

Prefix: `/api/v1/channels/{channel_id}/videos`

Manages the video list — both manually created to-do items and AI-generated suggestions.

---

#### `GET /` — List videos

Returns all videos for a channel, with optional filtering and suggestion marking.

**Query params:**

| Param           | Type   | Default | Description                                                                                           |
| --------------- | ------ | ------- | ----------------------------------------------------------------------------------------------------- |
| `status_filter` | string | `all`   | Filter by status: `todo`, `in_queue`, `done`, or `all`                                                |
| `suggest_n`     | int    | —       | If provided, marks the top N to-do videos as `suggested=true` (ordered by category score, best first) |

**How `suggest_n` works:**

1. Resets all previously suggested videos (`suggested=false`)
2. Fetches active categories sorted by score (highest first)
3. Sorts to-do videos by their category's score
4. Marks the top N as `suggested=true`
5. Returns the full video list

**Response (200):**

```json
[
  {
    "channel_id": "tech-tips",
    "video_id": "550e8400-e29b-41d4-a716-446655440000",
    "title": "10 VS Code Tricks You Didn't Know",
    "description": "In this video...",
    "tags": ["vscode", "productivity", "coding"],
    "category": "Tutorials",
    "topic": "VS Code productivity hacks",
    "status": "todo",
    "suggested": true,
    "basis_factor": "Auto-generated from analysis v3",
    "youtube_video_id": null,
    "r2_object_key": null,
    "metadata": {
      "views": null,
      "engagement": null,
      "avg_percentage_viewed": null
    },
    "created_at": "2024-01-15T10:30:00Z",
    "updated_at": "2024-01-15T10:30:00Z"
  }
]
```

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

1. Fetches all videos from the channel's uploads playlist (paginated)
2. Skips any already in the `videos` collection (by `youtube_video_id`)
3. Categorizes new videos in batches of 5 via Gemini (reuses existing categories, creates new ones only if needed)
4. Auto-creates new categories with `score: 0` and `video_count: 0` (scores/counts are updated later during analysis)
5. Inserts videos as `done` with `category` and `topic` assigned; `created_at` is set to the **YouTube publish date**

**Response (200):**

```json
{
  "ok": true,
  "synced": 15,
  "categories_created": ["Tutorials", "Reviews", "Vlogs"],
  "videos": [
    {
      "title": "10 VS Code Tricks",
      "category": "Tutorials",
      "topic": "VS Code productivity"
    },
    {
      "title": "iPhone 16 Review",
      "category": "Reviews",
      "topic": "iPhone 16 deep dive"
    }
  ]
}
```

---

#### `PATCH /{video_id}/status` — Update video status

Changes a video's status.

**Path params:** `video_id` — the UUID of the video

**Request body:**

```json
{
  "status": "done" // "todo", "in_queue", or "done"
}
```

**Side effects:**

- When marking a video as `done`, the corresponding category's `video_count` is incremented by 1.

**Response (200):**

```json
{ "ok": true, "video_id": "550e8400-...", "status": "done" }
```

---

#### `POST /queue` — Add video to posting queue

Creates a new video record AND adds it to the posting queue. The video file is streamed to Cloudflare R2.

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
  "category": "Tutorials", // optional
  "topic": "VS Code productivity hacks", // optional
  "basis_factor": "Manual upload" // optional
}
```

**What happens:**

1. Generates a UUID for `video_id`
2. Streams the file to R2 at `{channel_id}/{video_id}.mp4`
3. Creates a video document in the `videos` collection with status `in_queue`
4. Creates a queue entry in `video_queue` with the next available position

**Response (201):**

```json
{
  "ok": true,
  "video": { ...full video document... },
  "queue_position": 3
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

---

### Analysis

Prefix: `/api/v1/channels/{channel_id}/analysis`

AI-powered channel analysis using Gemini. Analyzes video performance and generates insights.

---

#### `POST /update` — Run full analysis update

**⚠️ Heavy endpoint** — calls YouTube API + Gemini AI. May take 30+ seconds.

**What happens (step by step):**

1. **Fetch done videos** from DB for this channel
2. **Compute delta** — compare with already-analysed video IDs to find new ones
3. **Exclude recent videos** — skip any with `created_at` less than 3 days ago (hard limit, no exceptions)
4. **Early exit** if no new videos to analyse
5. **Fetch YouTube stats** (views, likes, comments) for new videos that have a `youtube_video_id`
6. **Send to Gemini in batches of 5** — each batch receives the running analysis from prior batches, so insights accumulate incrementally
7. **Save updated analysis** to DB (increments version number)
8. **Save audit snapshot** to `analysis_history` collection
9. **Run to-do engine:**
   - Updates **all category scores** from Gemini's analysis output
   - Increments **category video_count** for each newly analysed video
   - Archives categories with score < 30 AND ≥ 5 videos
   - Generates new to-do video ideas (title/description/tags) via Gemini for each active category
   - Inserts new to-do videos into the `videos` collection

**Response (200):**

```json
{
  "channel_id": "tech-tips",
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
      "best_description_template": "In this video, I'll show you...",
      "best_tags": ["tutorial", "howto", "tips"],
      "score": 85.5
    }
  ],
  "analysis_done_video_ids": ["vid1", "vid2", "vid3"],
  "version": 3,
  "created_at": "2024-01-15T10:30:00Z",
  "updated_at": "2024-01-20T14:00:00Z"
}
```

---

#### `GET /latest` — Get latest analysis

Returns the most recent analysis document for the channel.

**Response (200):** Same format as the POST response above.

**Errors:** `404` — No analysis exists yet for this channel.

---

### Posting

Prefix: `/api/v1/channels/{channel_id}/posting`

Manages the video posting queue and handles YouTube uploads.

---

#### `GET /queue` — View posting queue

Returns the current posting queue sorted by position, enriched with video metadata.

**Response (200):**

```json
[
  {
    "position": 1,
    "video_id": "550e8400-...",
    "added_at": "2024-01-15T10:30:00Z",
    "title": "10 VS Code Tricks",
    "category": "Tutorials"
  }
]
```

---

#### `POST /upload-all` — Upload all queued videos to YouTube

Processes the entire queue in order. For each video:

1. **Download** from Cloudflare R2 to a temp file
2. **Upload** to YouTube via resumable upload (10MB chunks, private by default)
3. **Update** the video record: set `youtube_video_id`, change status from `in_queue` → `done`
4. **Remove** from the posting queue
5. **Clean up** the temp file

**Response (200):**

```json
{
  "ok": true,
  "uploaded": 2,
  "failed": 1,
  "details": [
    {
      "video_id": "vid1",
      "status": "uploaded",
      "youtube_video_id": "dQw4w..."
    },
    {
      "video_id": "vid2",
      "status": "uploaded",
      "youtube_video_id": "xYz1a..."
    },
    { "video_id": "vid3", "status": "failed" }
  ]
}
```

**Notes:**

- Videos are uploaded as **private** by default (change visibility on YouTube after review).
- Failed uploads don't stop the queue — the next video is attempted.
- Temp files are always cleaned up, even on failure.

---

## Database Schema

Database: **MongoDB Atlas** (database name from `MONGODB_DB_NAME` env var, default: `youtube_automation`)

All collections are shared across channels, with `channel_id` as a discriminator field.

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
  "topic": "VS Code productivity hacks", // the core content idea
  "status": "todo", // "todo", "in_queue", or "done"
  "suggested": false, // true when marked by suggest_n
  "basis_factor": "Auto-generated...", // why this video was created
  "youtube_video_id": null, // set after YouTube upload
  "r2_object_key": "tech-tips/vid.mp4", // set when file is stored in R2
  "metadata": {
    "views": null, // from YouTube stats
    "engagement": null, // from YouTube stats
    "avg_percentage_viewed": null // from YouTube stats
  },
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

- `todo` → Video idea exists (AI-generated or manual), not yet produced
- `in_queue` → Video file uploaded to R2, waiting in posting queue
- `done` → Video has been uploaded to YouTube

---

### Collection: `video_queue`

Stores the ordered posting queue. Each entry references a video by `video_id`.

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

- Entries are removed after successful YouTube upload.
- Position determines upload order in `upload-all`.

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
  "video_count": 12, // incremented when videos marked done
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

---

### Collection: `analysis`

Stores the AI-generated channel analysis. **One document per channel.**

```json
{
  "_id": "ObjectId",
  "channel_id": "tech-tips",
  "best_posting_times": [
    {
      "day_of_week": "monday",
      "video_count": 2, // post 2 videos on Monday
      "times": ["10:00", "14:00"] // exactly video_count entries
    }
  ],
  "category_analysis": [
    {
      "category": "Tutorials",
      "best_title_patterns": ["How to...", "10 Things..."],
      "best_description_template": "In this video...",
      "best_tags": ["tutorial", "howto"],
      "score": 85.5
    }
  ],
  "analysis_done_video_ids": ["vid1", "vid2"], // prevents re-analysing
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

Audit trail — one document per analysis run. Stores the inputs and outputs of each run for later review.

```json
{
  "_id": "ObjectId",
  "channel_id": "tech-tips",
  "version": 3,
  "input_videos": [                // videos sent to Gemini
    {"title": "...", "category": "...", "topic": "...", "tags": [...], "stats": {...}}
  ],
  "new_video_ids": ["vid5", "vid6"],   // video_ids that were new in this run
  "result": {
    "best_posting_times": [...],       // Gemini's output
    "category_analysis": [...]
  },
  "total_analysed_count": 20,          // total videos analysed so far
  "batch_count": 2,                    // how many Gemini batches were used
  "created_at": "datetime"
}
```

**Indexes:**
| Fields | Type | Purpose |
|---|---|---|
| `(channel_id, created_at)` | Compound (desc) | Fast reverse-chronological audit queries |

---

## Services Architecture

### R2 Service (`app/services/r2.py`)

- **Upload**: Streams file to R2 using `upload_fileobj` (never loads full file into memory)
- **Download**: Streams file from R2 to a temp file, returns the temp file path
- **Delete**: Removes an object from R2
- **Object key format**: `{channel_id}/{video_id}.mp4`

### YouTube Service (`app/services/youtube.py`)

- **Auth**: OAuth2 with stored token (auto-refreshes, initial setup requires browser consent)
- **Get channel info**: Fetches channel metadata (name, subscribers, etc.)
- **Get video stats**: Fetches views/likes/comments for batches of up to 50 videos
- **Upload video**: Resumable upload in 10MB chunks, defaults to private

### Gemini Service (`app/services/gemini.py`)

- **Analyze videos**: Sends video performance data + previous analysis → returns updated analysis JSON
- **Generate content**: Given a category + its analysis insights → generates title, description, tags for a new video
- **Model**: `gemini-3.1-pro-preview`
- **Output**: Forces JSON response via `response_mime_type="application/json"`

### Analysis Engine (`app/services/analysis_engine.py`)

Orchestrates the full analysis pipeline: delta computation → YouTube stats → **Gemini analysis in batches of 5** (each batch receives the running analysis, so insights accumulate) → DB save → to-do engine.

### To-do Engine (`app/services/todo_engine.py`)

Post-analysis step: archives underperforming categories, generates new video ideas via Gemini for active categories.

---

## Data Flow Diagrams

### Video Upload Flow

```
Client                    Server                   R2                YouTube
  |                         |                      |                    |
  |-- POST /videos/queue -->|                      |                    |
  |   (file + metadata)     |                      |                    |
  |                         |-- upload_fileobj --->|                    |
  |                         |   (streaming)        |                    |
  |                         |                      |                    |
  |                         |-- insert video doc (MongoDB)              |
  |                         |-- insert queue entry (MongoDB)            |
  |<-- 201 {video, pos} ----|                      |                    |
  |                         |                      |                    |
  |-- POST /upload-all ---->|                      |                    |
  |                         |-- download_fileobj --|                    |
  |                         |   (to temp file)     |                    |
  |                         |                      |                    |
  |                         |-- resumable upload ----------------->|
  |                         |   (10MB chunks)      |                    |
  |                         |                      |                    |
  |                         |-- update youtube_video_id (MongoDB)  |
  |                         |-- delete queue entry (MongoDB)       |
  |                         |-- delete temp file   |                    |
  |<-- {uploaded, failed} --|                      |                    |
```

### Analysis Flow

```
Client                    Server              YouTube API         Gemini AI
  |                         |                      |                   |
  |-- POST /analysis/update |                      |                   |
  |                         |-- fetch done videos (MongoDB)            |
  |                         |-- compute delta (new vs analysed)        |
  |                         |                      |                   |
  |                         |-- get_video_stats -->|                   |
  |                         |<-- views/likes ------|                   |
  |                         |                      |                   |
  |                         |-- analyze_videos ---------------------->|
  |                         |   (video data + prev analysis)          |
  |                         |<-- updated analysis --------------------|
  |                         |                      |                   |
  |                         |-- save analysis (MongoDB)                |
  |                         |                      |                   |
  |                         |-- archive bad categories (MongoDB)       |
  |                         |-- generate_video_content -------------->|
  |                         |<-- title/desc/tags ----------------------|
  |                         |-- insert todo videos (MongoDB)           |
  |                         |                      |                   |
  |<-- updated analysis ----|                      |                   |
```
