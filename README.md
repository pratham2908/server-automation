# YouTube Automation Server

FastAPI server for automated multi-channel YouTube video management — including video queuing, analysis, category management, and automated posting.

## Tech Stack

- **Framework**: FastAPI (Python 3.11+)
- **Database**: MongoDB Atlas via `motor` (async)
- **Storage**: Cloudflare R2 (S3-compatible) for video files
- **AI**: Google Gemini for analysis & content generation
- **YouTube**: YouTube Data API v3 for stats & uploads

## Quick Start

### 1. Clone & Install

```bash
git clone <repo-url>
cd automation-server
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your credentials
```

**Required credentials:**

| Variable                     | Where to get it                               |
| ---------------------------- | --------------------------------------------- |
| `API_KEY`                    | Generate any strong secret                    |
| `MONGODB_URI`                | MongoDB Atlas → Connect → Connection String   |
| `R2_*`                       | Cloudflare Dashboard → R2 → API Tokens        |
| `GEMINI_API_KEY`             | Google AI Studio → API Keys                   |
| `YOUTUBE_CLIENT_SECRET_JSON` | Google Cloud Console → OAuth 2.0 Client       |
| `YOUTUBE_TOKEN_JSON`         | Auto-generated on first run (browser consent) |

### 3. MongoDB Atlas Setup

1. Create a cluster at [cloud.mongodb.com](https://cloud.mongodb.com)
2. Under **Network Access**, whitelist your server's IP
3. Under **Database Access**, create a user
4. Copy the `mongodb+srv://` connection string into `.env`

### 4. YouTube OAuth Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Enable the **YouTube Data API v3**
3. Create an **OAuth 2.0 Client ID** (Desktop application)
4. Download the client secret JSON and set `YOUTUBE_CLIENT_SECRET_JSON` path
5. On first server start, a browser window will open for consent — the token is saved to `YOUTUBE_TOKEN_JSON`

### 5. Run

```bash
# Development
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Production (Oracle VPS)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

API docs: [http://localhost:8000/docs](http://localhost:8000/docs)

## API Overview

All endpoints require `X-API-Key` header. Scoped to channels via `{channel_id}`.

| Router     | Prefix                                     | Key Endpoints                                      |
| ---------- | ------------------------------------------ | -------------------------------------------------- |
| Videos     | `/api/v1/channels/{channel_id}/videos`     | GET `/`, PATCH `/{video_id}/status`, POST `/queue` |
| Categories | `/api/v1/channels/{channel_id}/categories` | GET `/`, POST `/`, PATCH `/{category_id}`          |
| Analysis   | `/api/v1/channels/{channel_id}/analysis`   | POST `/update`, GET `/latest`                      |
| Posting    | `/api/v1/channels/{channel_id}/posting`    | GET `/queue`, POST `/upload-all`                   |

## Production Deployment (Oracle VPS)

```bash
# Install as systemd service
sudo tee /etc/systemd/system/youtube-automation.service << 'EOF'
[Unit]
Description=YouTube Automation Server
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/automation-server
ExecStart=/home/ubuntu/automation-server/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
EnvironmentFile=/home/ubuntu/automation-server/.env

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable youtube-automation
sudo systemctl start youtube-automation
```

Use **Caddy** or **Nginx** as a reverse proxy for TLS termination on port 443.
