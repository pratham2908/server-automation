# External API — Channel Onboarding Guide

The Channel API lets your application upload videos directly to a specific channel, set metadata, schedule publishing, and trigger publish — without seeing the channel's platform OAuth credentials.

**Base URL:** `https://{your-api-host}/api/v1/ext`

---

## Quick Start

Get from zero to your first video in the Ready tab in under 5 minutes.

1. **Get your channel key** — a platform admin generates it from the Developer Keys panel in channel settings. You receive the raw key once; save it. Format: `ckey_mychannel_A3kxF7…`
2. **Note your channel ID** — the short slug used in every URL path (e.g. `histriphy`)
3. **Check what you can do** — call `GET /` to get the live API contract at any time
4. **Upload a video and confirm it appears in the Ready tab**

```bash
# Upload
curl -X POST "https://{your-api-host}/api/v1/ext/histriphy/upload" \
  -H "X-Channel-Api-Key: ckey_histriphy_A3kxF7…" \
  -F "file=@my-video.mp4" \
  -F "title=My First Upload"

# Response: {"ok": true, "video_id": "vid_abc123", "status": "ready", "analysis_queued": true}

# Poll until AI analysis finishes
curl "https://{your-api-host}/api/v1/ext/histriphy/videos/vid_abc123" \
  -H "X-Channel-Api-Key: ckey_histriphy_A3kxF7…"

# Keep polling until: {"packaging_status": "completed", ...}
```

> After upload, AI analysis runs in the background. `packaging_status` moves `analyzing` → `completed` in 1–3 minutes. Poll `GET /videos/{video_id}` instead of the full list — it's faster and only fetches what you need.

---

## Authentication

Include your channel key in every request:

```
X-Channel-Api-Key: ckey_histriphy_A3kxF7…
```

| Property | Value |
|----------|-------|
| Format | `ckey_{channel_id}_{random_token}` |
| Entropy | ~192 bits (URL-safe base64, 32 bytes) |
| Visibility | Shown **once** at generation — server stores only a SHA-256 hash |
| Scope | Single channel. A key for channel A is rejected on channel B's endpoints. |
| Rotation | Admin regenerates at any time; old key stops working immediately. |

- Missing/invalid key → **401 Unauthorized**
- Valid key, wrong channel → **403 Forbidden**

Do not include the key in URLs, query params, or client-side code. Store in environment variables or a secrets manager.

---

## Video Status Lifecycle

The main `status` field tracks the publishing lifecycle. `packaging_status` is a separate field that tracks AI analysis progress.

**`status` values:**

| Value | Meaning |
|-------|---------|
| `ready` | File stored in R2. AI analysis may still be running — check `packaging_status`. |
| `queued` | Scheduled for a future publish time. |
| `scheduled` | Queued at a specific time (Instagram only). |
| `published` | Live on the platform. |
| `failed` | Publishing failed. |

**`packaging_status` values (AI analysis):**

| Value | Meaning |
|-------|---------|
| *(absent)* | Analysis hasn't started yet — poll again in a moment. |
| `analyzing` | AI is processing: retention scoring, title/description/tag suggestions. |
| `completed` | AI done. Safe to read suggestions, schedule, or publish. |
| `failed` | AI failed. Video can still be published with its original metadata. |

---

## Endpoints

### GET `/api/v1/ext/{channel_id}/`

Returns the full API contract — all endpoints, request/response shapes, status definitions, and error codes. Check `api_version` on each call to detect breaking changes automatically.

**Response:** structured JSON document describing every available operation.

---

### POST `/api/v1/ext/{channel_id}/upload`

Upload a video file. Stores in R2 and queues AI analysis automatically.

**Request:** `multipart/form-data`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | file | **required** | Video file. MP4 recommended. |
| `title` | string | optional | Initial title. Falls back to filename if omitted. |
| `description` | string | optional | Video description. |
| `tags` | string | optional | Comma-separated: `history,facts,rome` |
| `scheduled_at` | string | optional | UTC ISO 8601 to auto-schedule on upload. |

**Response:**
```json
{
  "ok": true,
  "video_id": "vid_abc123",
  "status": "ready",
  "analysis_queued": true
}
```

---

### GET `/api/v1/ext/{channel_id}/videos/{video_id}`

Fetch a single video by its `video_id`. Use this to poll `packaging_status` after upload without fetching the full list.

**Response:** single video object (same fields as list entries — see below).

```json
{
  "video_id":         "vid_abc123",
  "title":            "How the Roman Empire Fell",
  "description":      "A deep-dive into the fall of Rome…",
  "tags":             ["history", "rome", "documentary"],
  "status":           "ready",
  "packaging_status": "completed",
  "scheduled_at":     null,
  "published_at":     null,
  "created_at":       "2026-07-24T10:00:00+00:00"
}
```

---

### GET `/api/v1/ext/{channel_id}/videos`

List all channel videos. OAuth tokens and internal fields are stripped from the response.

**Query params:**

| Param | Type | Description |
|-------|------|-------------|
| `status` | string | Filter: `ready`, `queued`, `published`, `scheduled` |

**Response:**
```json
{
  "videos": [
    {
      "video_id":         "vid_abc123",
      "title":            "How the Roman Empire Fell",
      "description":      "A deep-dive into the fall of Rome…",
      "tags":             ["history", "rome", "documentary"],
      "status":           "ready",
      "packaging_status": "completed",
      "scheduled_at":     null,
      "published_at":     null,
      "created_at":       "2026-07-24T10:00:00+00:00"
    }
  ]
}
```

---

### PATCH `/api/v1/ext/{channel_id}/videos/{video_id}/metadata`

Update title, description, or tags. All fields optional — send only what changes. `tags` is a full replacement, not an append. Server verifies video belongs to the given channel.

**Request:** `application/json`

```json
{ "title": "New Title", "description": "New desc", "tags": ["a", "b"] }
```

**Response:** `{ "ok": true }`

---

### POST `/api/v1/ext/{channel_id}/videos/{video_id}/schedule`

Set a UTC publish time. Video must be in `ready` or `queued` status. If already queued, this reschedules to the new time.

**Request:** `application/json`

```json
{ "scheduled_at": "2026-07-25T09:00:00Z" }
```

`scheduled_at` is **required** and must be a future UTC ISO 8601 datetime. Past datetimes are rejected with 422.

**Response:**
```json
{ "ok": true, "scheduled_at": "2026-07-25T09:00:00+00:00" }
```

---

### POST `/api/v1/ext/{channel_id}/videos/{video_id}/publish`

Queue the video for immediate publishing. The background publisher picks it up within minutes. Video must be in `ready` status.

Publishing is **asynchronous** — the platform ID (YouTube video ID / Instagram media ID) is not returned immediately. Poll `GET /videos/{video_id}` until `status == "published"` to confirm and retrieve the platform ID via the sync endpoint.

> **Note:** For timed publishing, use the schedule endpoint instead.

**Response:**
```json
{
  "ok": true,
  "queued": true,
  "message": "Queued for immediate publishing. Poll GET /videos/{video_id} to confirm status == 'published'."
}
```

---

### POST `/api/v1/ext/{channel_id}/sync`

Sync this channel's video library with the platform — identical to the "Sync Videos" button in the dashboard.

Fetches all videos from YouTube or Instagram and:
- Updates existing records: title, description, views, likes, comments, thumbnail URL, and `status`
- Imports any videos that exist on the platform but are not yet in the system

**Response:**
```json
{
  "ok": true,
  "synced": 3
}
```

`synced` is the count of new videos imported from the platform.

---

## End-to-End Workflow

```
Upload → Poll for analysis → (Optional: apply metadata) → Schedule or Publish → Poll for confirmation
```

1. **Upload** — `POST /upload` with file. Save `video_id`.
2. **Poll for analysis** — `GET /videos/{video_id}` every 5–10 s until `packaging_status == "completed"` (1–3 min).
3. *(Optional)* **Apply AI metadata** — use `PATCH /metadata` with AI-suggested title/description/tags.
4. **Schedule** — `POST /schedule` with a future UTC datetime.
5. *(Alternative)* **Publish now** — `POST /publish` for immediate publishing. Poll `GET /videos/{video_id}` until `status == "published"`.
6. *(Optional)* **Sync** — `POST /sync` to pull the full video library from the platform: refreshes views/likes/comments and imports any videos not yet in the system.

### Full Python Example

```python
import requests, time
from datetime import datetime, timezone, timedelta

BASE = "https://{your-api-host}/api/v1/ext"
KEY = "ckey_histriphy_A3kxF7…"
CHANNEL = "histriphy"
HEADERS = {"X-Channel-Api-Key": KEY}

# 1. Upload
with open("rome-falls.mp4", "rb") as f:
    resp = requests.post(
        f"{BASE}/{CHANNEL}/upload",
        headers=HEADERS,
        files={"file": ("rome-falls.mp4", f, "video/mp4")},
        data={"title": "How Rome Fell", "tags": "history,rome"},
    )
resp.raise_for_status()
video_id = resp.json()["video_id"]
print(f"Uploaded → {video_id}")

# 2. Poll for AI analysis (single-video endpoint — efficient)
while True:
    v = requests.get(f"{BASE}/{CHANNEL}/videos/{video_id}", headers=HEADERS).json()
    if v.get("packaging_status") in ("completed", "failed"):
        break
    time.sleep(10)
print(f"Analysis: {v['packaging_status']}")

# 3. Schedule for tomorrow 9 AM UTC
publish_at = datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
requests.post(
    f"{BASE}/{CHANNEL}/videos/{video_id}/schedule",
    headers=HEADERS,
    json={"scheduled_at": publish_at.isoformat()},
).raise_for_status()
print(f"Scheduled for {publish_at.isoformat()}")

# 4. (Later) Sync full library from platform — refreshes metrics, imports new videos
result = requests.post(f"{BASE}/{CHANNEL}/sync", headers=HEADERS).json()
print(f"Sync complete — {result.get('synced', 0)} new videos imported")
```

---

## Error Reference

All errors return: `{ "detail": "human-readable message" }`

| Status | Meaning | When | What to do |
|--------|---------|------|-----------|
| 200 | OK | Request succeeded | — |
| 401 | Unauthorized | Missing, invalid, or revoked key | Check header is set. If rotated, get new key from admin. |
| 403 | Forbidden | Valid key, wrong channel | Verify `{channel_id}` matches the key's channel. |
| 404 | Not Found | `video_id` doesn't exist or belongs to different channel | Confirm video_id from upload response. Check channel_id. |
| 422 | Validation Error | Missing required field, wrong type, past datetime in schedule | Read `detail` field — it names the specific field and reason. |
| 503 | Service Unavailable | AI service not yet initialised (rare, on cold start) | Retry after a few seconds. |
| 500 | Server Error | R2 failure, platform API error, database error | Retry after a delay. Contact admin with `video_id` and timestamp if it persists. |

### Python Error Handling

```python
try:
    r = requests.post(url, headers=HEADERS, json=body)
    r.raise_for_status()
except requests.HTTPError as e:
    status = e.response.status_code
    detail = e.response.json().get("detail", "unknown error")
    if status == 401:
        raise RuntimeError("Invalid or revoked channel key") from e
    elif status == 403:
        raise RuntimeError("Key used on wrong channel") from e
    elif status == 422:
        raise ValueError(f"Validation failed: {detail}") from e
    else:
        raise
```
