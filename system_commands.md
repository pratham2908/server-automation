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

## 🔑 YouTube Token Re-authentication

If you need to re-authenticate the YouTube token (e.g. after adding a new OAuth scope), do this **locally** (it requires a browser):

```bash
# 1. Delete the old token
rm youtube_token.json

# 2. Start the server — it will open a browser for OAuth consent
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 3. Grant access in the browser (approve all scopes)
# The new token is saved automatically to youtube_token.json

# 4. Copy the new token to the production server
scp -i ssh-key-2.key youtube_token.json ubuntu@68.233.115.135:~/automation-server/youtube_token.json

# 5. Restart the production server
ssh -i ssh-key-2.key ubuntu@68.233.115.135 "sudo systemctl restart automation-server"
```

Current OAuth scopes: `youtube.upload`, `youtube.readonly`, `yt-analytics.readonly`

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
