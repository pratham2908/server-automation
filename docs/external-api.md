# External API — Channel Onboarding Guide

The Channel API lets your application upload videos directly to a specific channel, set metadata, schedule publishing, and trigger publish — without seeing the channel's platform OAuth credentials.

**Base URL:** `https://{your-api-host}/api/v1/ext`

---

## Quick Start

Get from zero to your first video in the Ready tab in under 5 minutes.

1. **Get your channel key** — a platform admin generates it from the Developer Keys panel in channel settings. You receive the raw key once; save it. Format: `ckey_mychannel_A3kxF7…`
2. **Note your channel ID** — the short slug used in every URL path (e.g. `histriphy`)
3. **Upload a video and confirm it appears in the Ready tab**

```bash
# Upload
curl -X POST "https://{your-api-host}/api/v1/ext/histriphy/upload" \
  -H "X-Channel-Api-Key: ckey_histriphy_A3kxF7…" \
  -F "file=@my-video.mp4" \
  -F "title=My First Upload"

# Response: {"ok": true, "video_id": "vid_abc123", "status": "ready", "analysis_queued": true}
```

> After upload, AI analysis runs in the background. `packaging_status` moves `analyzing` → `completed` in 1–3 minutes. Poll the list endpoint before scheduling if you want AI-suggested metadata applied first.

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

## Endpoints

### POST `/api/v1/ext/{channel_id}/upload`

Upload a video file. Stores in R2 and queues AI analysis automatically.

**Request:** `multipart/form-data`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | file | **required** | Video file. MP4 recommended. |
| `title` | string | optional | Initial title. |
| `description` | string | optional | Video description. |
| `tags` | string | optional | Comma-separated: `history,facts,rome` |
| `scheduled_at` | string | optional | UTC ISO 8601 to schedule on upload. |

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

### GET `/api/v1/ext/{channel_id}/videos`

List channel videos. OAuth tokens and internal fields are stripped from the response.

**Query params:**

| Param | Type | Description |
|-------|------|-------------|
| `status` | string | Filter: `ready`, `scheduled`, `published`, `uploading` |

**Response:**
```json
{
  "videos": [
    {
      "video_id":          "vid_abc123",
      "title":             "How the Roman Empire Fell",
      "description":       "A deep-dive into the fall of Rome…",
      "tags":              ["history", "rome", "documentary"],
      "status":            "ready",
      "packaging_status":  "completed",
      "scheduled_at":      null,
      "published_at":      null
    }
  ]
}
```

---

### PATCH `/api/v1/ext/{channel_id}/videos/{video_id}/metadata`

Update title, description, or tags. All fields optional — send only what changes. Server verifies video belongs to the given channel.

**Request:** `application/json`

```json
{ "title": "New Title", "description": "New desc", "tags": ["a", "b"] }
```

**Response:** `{ "ok": true }`

---

### POST `/api/v1/ext/{channel_id}/videos/{video_id}/schedule`

Set a UTC publish time. The scheduler handles it automatically.

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

Publish the video immediately using the channel's stored OAuth credentials. Creator app never sees the tokens.

> **Note:** This publishes right now. For timed publishing, use the schedule endpoint instead.

**Response:**
```json
{ "ok": true, "platform_id": "dQw4w9WgXcQ" }
```

(`platform_id` is the YouTube video ID or Instagram media ID.)

---

## End-to-End Workflow

```
Upload → Wait for analysis → (Optional: apply metadata) → Schedule → Done
```

1. **Upload** — `POST /upload` with file. Save `video_id`.
2. **Wait for analysis** — poll `GET /videos` until `packaging_status == "completed"` (1–3 min).
3. *(Optional)* **Apply AI metadata** — read packaging suggestions from the list response, apply with `PATCH /metadata`.
4. **Schedule** — `POST /schedule` with a future UTC datetime.
5. *(Alternative)* **Publish now** — `POST /publish` for immediate publishing.
6. **Confirm** — `GET /videos` and verify `status == "published"` with a non-null `published_at`.

### Full Python Example

```python
import requests, time
from datetime import datetime, timezone, timedelta

BASE    = "https://{your-api-host}/api/v1/ext"
KEY     = "ckey_histriphy_A3kxF7…"
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

# 2. Wait for AI analysis
while True:
    videos = requests.get(f"{BASE}/{CHANNEL}/videos", headers=HEADERS).json()["videos"]
    v = next(x for x in videos if x["video_id"] == video_id)
    if v["packaging_status"] in ("completed", "failed"):
        break
    time.sleep(15)

# 4. Schedule for tomorrow 9 AM UTC
publish_at = (
    datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0)
    + timedelta(days=1)
)
requests.post(
    f"{BASE}/{CHANNEL}/videos/{video_id}/schedule",
    headers=HEADERS,
    json={"scheduled_at": publish_at.isoformat()},
).raise_for_status()
print(f"Scheduled for {publish_at.isoformat()}")
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
