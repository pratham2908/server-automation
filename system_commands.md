# System and Infrastructure Commands

This document contains the commands necessary to manage the automation server infrastructure and background services.

---

## 🚀 Deploy Workflow (commit → push → deploy)

When asked to **commit changes, push, and deploy** (or similar), run these steps in order:

1. **Commit** with an appropriate message (e.g. based on git status / changes).
2. **Push**: `git push origin main`
3. **Deploy to Oracle server**: SSH in, pull code, restart the service:

   Connection details are in `.env`: `ORACLE_SERVER_SSH_COMMAND`, and the app directory on the server is `automation-server`. From the **project root** (so `ssh-key-2.key` resolves), run:

   ```bash
   ssh -i ssh-key-2.key ubuntu@68.233.115.135 "cd automation-server && git pull && sudo systemctl restart automation-server"
   ```

   Or using the env value: run the command stored in `ORACLE_SERVER_SSH_COMMAND` with the remote part appended, e.g.  
   `"<ORACLE_SERVER_SSH_COMMAND>" "cd automation-server && git pull && sudo systemctl restart automation-server"`

4. **Confirm it actually came up.** A restart that reports `active` can still be a crash loop —
   systemd restarts a failing process, so `is-active` says `active` while the app is dying on boot.
   Check the health endpoint from inside the box, giving uvicorn ~15s to bind:

   ```bash
   ssh -i ssh-key-2.key ubuntu@68.233.115.135 "sleep 15; systemctl is-active automation-server; curl -s -o /dev/null -w 'health:%{http_code}\n' http://127.0.0.1:8000/health"
   ```

   `health:200` means it is up. Anything else — read the log:
   `sudo journalctl -u automation-server -n 30 --no-pager`

---

## 📦 The One AI SDK — ship it separately, every time it changes

**Read this before deploying if you have touched the One AI SDK.**

`requirements.txt` declares the SDK as an editable local path:

```
-e ../../one-ai/sdk/python
```

That resolves on the dev Mac. It cannot resolve on the server — from
`/home/ubuntu/automation-server` it points at `/home/one-ai/sdk/python`, which does not exist. So
the SDK is **not** installed by `git pull`, and `pip install -r requirements.txt` aborts on that
line. The server has a plain (non-editable) copy in its venv, installed by hand.

The consequence: **a change to the SDK does not reach production by deploying automation-server.**
The server keeps running its existing copy — no error, no warning, just the old code answering
requests. Ship it explicitly:

Run this from anywhere — it holds the key in a variable because the `cd` into the SDK directory
breaks the usual relative `-i ssh-key-2.key` (that mistake fails with
`Permission denied (publickey)`, which reads like an access problem rather than a wrong path):

```bash
KEY=~/work/Code/content-manager/automation-server/ssh-key-2.key

cd ~/work/Code/one-ai/sdk/python
tar czf /tmp/one-ai-sdk.tgz --exclude='*.egg-info' --exclude='__pycache__' --exclude='tests' .
scp -i "$KEY" /tmp/one-ai-sdk.tgz ubuntu@68.233.115.135:/tmp/

ssh -i "$KEY" ubuntu@68.233.115.135 \
  "rm -rf ~/one-ai-sdk && mkdir -p ~/one-ai-sdk && tar xzf /tmp/one-ai-sdk.tgz -C ~/one-ai-sdk \
   && ~/automation-server/venv/bin/pip install ~/one-ai-sdk \
   && sudo systemctl restart automation-server"
```

Then confirm with the health check in step 4 above.

Notes:

- **The restart is not optional.** `pip install` swaps the files on disk, but the running process
  already has the old module imported. Skipping it looks exactly like a deploy that did not take.
- **Bumping the SDK version is not required.** pip reinstalls from a local path even when the
  version is unchanged (verified — it uninstalls and reinstalls).
- **A fresh server or rebuilt venv needs this too**, plus `ONE_AI_URL` and `ONE_AI_API_KEY` in the
  server's `.env`. Without the key the app does not start: `get_one_ai()` is called during lifespan
  startup, so a missing key is a boot failure, not a failed AI call.
- **Adding any new Python dependency** means installing it directly on the server — the
  `pip install -r requirements.txt` route dies on the editable path above.

This whole section disappears the day the SDK is installable from the server — a git URL in
`requirements.txt` (or a published package) would make it a normal `git pull && pip install`.

---

## 🔐 SSH Access

To connect to the production Ubuntu server from your local machine, run:

```bash
ssh -i ssh-key-2.key ubuntu@68.233.115.135
```

---

## ⚙️ Server Management (systemd)

The FastAPI server is running as a background service managed by `systemd`. It is configured to start automatically on boot and restart if it crashes.

Once you have SSH'd into the server, use the following commands to manage the service:

### 🔄 Restart the Server

_Use this after pulling new code changes from git or updating the `.env` file._

```bash
sudo systemctl restart automation-server
```

### 🛑 Stop the Server

_Stops the server from running in the background._

```bash
sudo systemctl stop automation-server
```

### ▶️ Start the Server

_Starts the server if it was previously stopped._

```bash
sudo systemctl start automation-server
```

### 📊 Check Server Status

_Checks if the service is active, running, or has encountered any errors._

```bash
sudo systemctl status automation-server
```

_(Press `q` to exit the status view)_

---

## 📜 Viewing Logs

If you want to see the application's output, errors, or print statements, you need to check the system journal.

### View Real-Time Logs (Follow)

_Streams the logs to your terminal in real-time. Equivalent to watching the terminal running the server locally._

```bash
sudo journalctl -u automation-server -f
```

_(Press `Ctrl + C` to stop watching)_

### View Recent Logs

_View the last 50 lines of logs without streaming._

```bash
sudo journalctl -u automation-server -n 50 --no-pager
```

---

## 🔑 YouTube Token Management (Per-Channel)

Each channel has its own OAuth tokens stored in the `youtube_tokens` field of its document in the `channels` collection in MongoDB.

### Token provisioning (via frontend)

YouTube tokens are now stored in the database on each channel document.
The frontend completes the Google OAuth consent flow in the browser, then
stores the tokens via:

```
POST /api/v1/channels/{channel_id}/youtube-token
```

To check token status:

```
GET /api/v1/channels/{channel_id}/youtube-token/status
```

To get a fresh access token (auto-refreshes if expired):

```
GET /api/v1/channels/{channel_id}/youtube-token
```

### Setting up OAuth client credentials

Store the Google OAuth client ID and secret in the DB (one-time setup):

```
PUT /api/v1/channels/config/youtube-oauth
{"client_id": "...", "client_secret": "..."}
```

Current OAuth scopes: `youtube.upload`, `youtube.readonly`, `youtube.force-ssl`, `yt-analytics.readonly`

---

## 💻 Local Development

If you need to run the server locally on your own machine for testing:

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
