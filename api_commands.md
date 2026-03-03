# API Commands Reference

Base URL: `http://localhost:8000`
Replace `your-api-key` with your `API_KEY` from `.env`.
Replace `ch1` with your actual channel ID.

---

## Server Start Commands

```bash
# Activate virtual environment
source .venv/bin/activate

# Development (with hot-reload)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Production
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1

# Interactive API docs (open in browser)
open http://localhost:8000/docs
```

---

## Health Check

```bash
curl http://localhost:8000/health
```

---

## Channels

```bash
# Register a channel (auto-fetches name, description, stats from YouTube)
curl -X POST http://localhost:8000/api/v1/channels/ \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"youtube_channel_id": "UCxxxxxxxx"}'

# Register with a custom slug
curl -X POST http://localhost:8000/api/v1/channels/ \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"youtube_channel_id": "UCxxxxxxxx", "channel_id": "my-channel"}'

# List all channels
curl http://localhost:8000/api/v1/channels/ \
  -H "X-API-Key: your-api-key"

# Get a single channel
curl http://localhost:8000/api/v1/channels/ch1 \
  -H "X-API-Key: your-api-key"

# Refresh channel data from YouTube (re-fetches stats, name, etc.)
curl -X POST http://localhost:8000/api/v1/channels/ch1/refresh \
  -H "X-API-Key: your-api-key"

# Update a channel
curl -X PATCH http://localhost:8000/api/v1/channels/ch1 \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"name": "Updated Channel Name"}'

# Delete a channel (also removes all its videos, categories, analysis, queue)
curl -X DELETE http://localhost:8000/api/v1/channels/ch1 \
  -H "X-API-Key: your-api-key"
```

---

## Categories

```bash
# Add a single category
curl -X POST http://localhost:8000/api/v1/channels/ch1/categories/ \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"name": "Tutorials", "description": "How-to videos", "score": 80}'

# Add multiple categories at once
curl -X POST http://localhost:8000/api/v1/channels/ch1/categories/ \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '[
    {"name": "Tutorials", "description": "How-to videos", "score": 80},
    {"name": "Reviews", "description": "Product reviews", "score": 70}
  ]'

# List all categories
curl http://localhost:8000/api/v1/channels/ch1/categories/ \
  -H "X-API-Key: your-api-key"

# List only active categories
curl "http://localhost:8000/api/v1/channels/ch1/categories/?status_filter=active" \
  -H "X-API-Key: your-api-key"

# Update a category (replace CATEGORY_ID with actual _id from list response)
curl -X PATCH http://localhost:8000/api/v1/channels/ch1/categories/CATEGORY_ID \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"score": 90, "description": "Updated description"}'
```

---

## Videos

```bash
# List all videos
curl http://localhost:8000/api/v1/channels/ch1/videos/ \
  -H "X-API-Key: your-api-key"

# List only todo videos
curl "http://localhost:8000/api/v1/channels/ch1/videos/?status_filter=todo" \
  -H "X-API-Key: your-api-key"

# List done videos
curl "http://localhost:8000/api/v1/channels/ch1/videos/?status_filter=done" \
  -H "X-API-Key: your-api-key"

# List in_queue videos
curl "http://localhost:8000/api/v1/channels/ch1/videos/?status_filter=in_queue" \
  -H "X-API-Key: your-api-key"

# List videos with top 3 suggestions
curl "http://localhost:8000/api/v1/channels/ch1/videos/?suggest_n=3" \
  -H "X-API-Key: your-api-key"

# Add video to queue (with file upload — sets status to in_queue)
curl -X POST http://localhost:8000/api/v1/channels/ch1/videos/queue \
  -H "X-API-Key: your-api-key" \
  -F "file=@/path/to/video.mp4" \
  -F 'body={"title":"My Video","description":"Video description","tags":["tag1","tag2"],"category":"Tutorials","topic":"My video topic"}'

# Mark a video as done (replace VIDEO_ID)
curl -X PATCH http://localhost:8000/api/v1/channels/ch1/videos/VIDEO_ID/status \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"status": "done"}'

# Mark a video back to todo
curl -X PATCH http://localhost:8000/api/v1/channels/ch1/videos/VIDEO_ID/status \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"status": "todo"}'
```

---

## Analysis

```bash
# Run full analysis update (calls YouTube API + Gemini — may take a while)
curl -X POST http://localhost:8000/api/v1/channels/ch1/analysis/update \
  -H "X-API-Key: your-api-key"

# Get latest analysis
curl http://localhost:8000/api/v1/channels/ch1/analysis/latest \
  -H "X-API-Key: your-api-key"
```

---

## Posting

```bash
# View posting queue
curl http://localhost:8000/api/v1/channels/ch1/posting/queue \
  -H "X-API-Key: your-api-key"

# Upload all queued videos to YouTube (processes one by one)
curl -X POST http://localhost:8000/api/v1/channels/ch1/posting/upload-all \
  -H "X-API-Key: your-api-key"
```

---
