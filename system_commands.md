# System and Infrastructure Commands

This document contains the commands necessary to manage the automation server infrastructure and background services.

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
