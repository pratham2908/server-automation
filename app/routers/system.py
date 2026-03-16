from fastapi import APIRouter, Request, HTTPException, status, Query, Depends
from fastapi.responses import HTMLResponse
import asyncio
import os
import shutil
from app.config import get_settings

router = APIRouter(tags=["system"])

async def verify_api_key(api_key: str = Query(...)):
    settings = get_settings()
    if api_key != settings.API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    return api_key

def has_systemctl():
    return shutil.which("systemctl") is not None

async def run_shell_command(command: list[str]) -> tuple[int, str, str]:
    if not command:
        return 1, "", "No command provided"
        
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        return process.returncode or 0, stdout.decode().strip(), stderr.decode().strip()
    except Exception as e:
        return 1, "", str(e)

@router.get("/system", response_class=HTMLResponse)
async def system_dashboard(api_key: str = Depends(verify_api_key)):
    """A premium dashboard to manage the automation server."""
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Server Control | YouTube Automation</title>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet">
        <style>
            :root {
                --bg-dark: #0f172a;
                --bg-card: #1e293b;
                --bg-accent: #334155;
                --text-main: #f8fafc;
                --text-muted: #94a3b8;
                --primary: #38bdf8;
                --primary-hover: #0ea5e9;
                --success: #4ade80;
                --warning: #fbbf24;
                --error: #f87171;
                --font-sans: 'Inter', sans-serif;
                --font-mono: 'Fira Code', monospace;
            }

            * { box-sizing: border-box; margin: 0; padding: 0; }

            body {
                font-family: var(--font-sans);
                background-color: var(--bg-dark);
                color: var(--text-main);
                min-height: 100vh;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                padding: 2rem;
            }

            .container {
                width: 100%;
                max-width: 800px;
                background: var(--bg-card);
                border-radius: 1.5rem;
                border: 1px solid rgba(255, 255, 255, 0.1);
                box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
                overflow: hidden;
                animation: fadeIn 0.5s ease-out;
            }

            @keyframes fadeIn {
                from { opacity: 0; transform: translateY(20px); }
                to { opacity: 1; transform: translateY(0); }
            }

            header {
                padding: 2rem;
                background: rgba(255, 255, 255, 0.03);
                border-bottom: 1px solid rgba(255, 255, 255, 0.1);
                display: flex;
                justify-content: space-between;
                align-items: center;
            }

            .logo-section h1 {
                font-size: 1.5rem;
                font-weight: 700;
                letter-spacing: -0.025em;
                color: var(--text-main);
                display: flex;
                align-items: center;
                gap: 0.75rem;
            }

            .logo-icon {
                color: var(--primary);
                animation: pulse 4s infinite;
            }

            @keyframes pulse {
                0%, 100% { transform: scale(1); opacity: 1; }
                50% { transform: scale(1.1); opacity: 0.8; }
            }

            .status-badge {
                display: flex;
                align-items: center;
                gap: 0.5rem;
                background: var(--bg-dark);
                padding: 0.5rem 1rem;
                border-radius: 2rem;
                font-size: 0.875rem;
                font-weight: 600;
                border: 1px solid rgba(255, 255, 255, 0.1);
            }

            .dot {
                width: 8px;
                height: 8px;
                border-radius: 50%;
                background: var(--text-muted);
            }

            .status-active .dot { background: var(--success); box-shadow: 0 0 10px var(--success); }
            .status-inactive .dot { background: var(--error); box-shadow: 0 0 10px var(--error); }
            .status-loading .dot { background: var(--warning); animation: flash 1s infinite; }

            @keyframes flash {
                0%, 100% { opacity: 1; }
                50% { opacity: 0.3; }
            }

            .content {
                padding: 2rem;
            }

            .console {
                background: #000;
                border-radius: 1rem;
                padding: 1.25rem;
                font-family: var(--font-mono);
                font-size: 0.85rem;
                line-height: 1.6;
                color: #d1d5db;
                margin-bottom: 2rem;
                max-height: 300px;
                overflow-y: auto;
                border: 1px solid rgba(255, 255, 255, 0.15);
                white-space: pre-wrap;
            }

            .console::-webkit-scrollbar { width: 6px; }
            .console::-webkit-scrollbar-thumb { background: #334155; border-radius: 10px; }

            .controls {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
                gap: 1rem;
            }

            .btn {
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 0.5rem;
                padding: 1rem;
                border-radius: 0.75rem;
                border: none;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
                color: white;
                font-size: 0.95rem;
            }

            .btn-status { background: var(--bg-accent); }
            .btn-status:hover { background: #475569; }

            .btn-restart { background: #6366f1; }
            .btn-restart:hover { background: #4f46e5; transform: translateY(-2px); }
            .btn-restart:active { transform: translateY(0); }

            .btn-stop { background: #ef4444; }
            .btn-stop:hover { background: #dc2626; transform: translateY(-2px); }
            .btn-stop:active { transform: translateY(0); }

            .btn-start { background: #10b981; }
            .btn-start:hover { background: #059669; transform: translateY(-2px); }

            .btn:disabled {
                opacity: 0.5;
                cursor: not-allowed;
                transform: none !important;
            }

            footer {
                padding: 1.5rem 2rem;
                background: rgba(0, 0, 0, 0.2);
                border-top: 1px solid rgba(255, 255, 255, 0.05);
                display: flex;
                justify-content: space-between;
                align-items: center;
                font-size: 0.75rem;
                color: var(--text-muted);
            }

            .links { display: flex; gap: 1.5rem; }
            .links a { color: var(--text-muted); text-decoration: none; transition: color 0.2s; }
            .links a:hover { color: var(--primary); }

            .toast {
                position: fixed;
                bottom: 2rem;
                left: 50%;
                transform: translateX(-50%);
                padding: 0.75rem 1.5rem;
                border-radius: 2rem;
                background: var(--bg-card);
                border: 1px solid var(--primary);
                color: white;
                font-size: 0.875rem;
                font-weight: 500;
                box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1);
                opacity: 0;
                transition: opacity 0.3s;
                pointer-events: none;
                z-index: 100;
            }
            .toast.show { opacity: 1; }
        </style>
    </head>
    <body>
        <div class="container">
            <header>
                <div class="logo-section">
                    <h1>
                        <svg class="logo-icon" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                            <rect x="2" y="2" width="20" height="8" rx="2" ry="2"></rect>
                            <rect x="2" y="14" width="20" height="8" rx="2" ry="2"></rect>
                            <line x1="6" y1="6" x2="6.01" y2="6"></line>
                            <line x1="6" y1="18" x2="6.01" y2="18"></line>
                        </svg>
                        Server Manager
                    </h1>
                </div>
                <div id="status-badge" class="status-badge status-loading">
                    <div class="dot"></div>
                    <span id="status-text">Checking...</span>
                </div>
            </header>

            <main class="content">
                <div id="console" class="console">_ Initializing dashboard...</div>
                
                <div class="controls">
                    <button id="refresh-btn" class="btn btn-status">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6"></path><path d="M1 20v-6h6"></path><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>
                        Refresh Status
                    </button>
                    <button id="restart-btn" class="btn btn-restart">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 2v6h-6"></path><path d="M3 12a9 9 0 0 1 15-6.7L21 8"></path><path d="M3 22v-6h6"></path><path d="M21 12a9 9 0 0 1-15 6.7L3 16"></path></svg>
                        Restart App
                    </button>
                    <button id="stop-btn" class="btn btn-stop">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2" ry="2"></rect></svg>
                        Stop App
                    </button>
                </div>
            </main>

            <footer>
                <div>&copy; 2026 YouTube Automation</div>
                <div class="links">
                    <a href="/docs">Docs</a>
                    <a href="/logs?api_key=""" + api_key + """">Logs</a>
                    <a href="/health">Health</a>
                </div>
            </footer>
        </div>

        <div id="toast" class="toast">Action updated successfully</div>

        <script>
            const apiKey = '""" + api_key + """';
            const consoleEl = document.getElementById('console');
            const statusBadge = document.getElementById('status-badge');
            const statusText = document.getElementById('status-text');
            const toast = document.getElementById('toast');

            function log(msg, type = 'info') {
                const time = new Date().toLocaleTimeString();
                const prefix = `[${time}] `;
                consoleEl.innerText += `\\n${prefix}${msg}`;
                consoleEl.scrollTop = consoleEl.scrollHeight;
            }

            function showToast(msg) {
                toast.textContent = msg;
                toast.classList.add('show');
                setTimeout(() => toast.classList.remove('show'), 3000);
            }

            async function callApi(action) {
                log(`Executing: ${action.toUpperCase()}...`);
                try {
                    const res = await fetch(`/api/system/${action}?api_key=${apiKey}`, { method: 'POST' });
                    const data = await res.json();
                    
                    if (data.ok) {
                        log(`Success: ${data.message || 'Command executed'}`);
                        showToast(`Server ${action} successful`);
                        if (action === 'restart' || action === 'stop') {
                            log('Note: Connection will drop as server restarts/stops.', 'warning');
                            setTimeout(refreshStatus, 5000);
                        } else {
                            refreshStatus();
                        }
                    } else {
                        log(`Error: ${data.detail || data.error}`, 'error');
                        showToast(`Action failed: ${action}`);
                    }
                } catch (e) {
                    log(`Network Error: ${e.message}`, 'error');
                }
            }

            async function refreshStatus() {
                statusBadge.className = 'status-badge status-loading';
                statusText.textContent = 'Refreshing...';
                
                try {
                    const res = await fetch(`/api/system/status?api_key=${apiKey}`);
                    if (res.status === 401) {
                        log('Error: Unauthorized. Invalid API Key.', 'error');
                        return;
                    }
                    const data = await res.json();
                    
                    if (data.active) {
                        statusBadge.className = 'status-badge status-active';
                        statusText.textContent = 'Active';
                        log('Status: Online and Healthy');
                    } else {
                        statusBadge.className = 'status-badge status-inactive';
                        statusText.textContent = 'Inactive';
                        log(`Status: ${data.status || 'Offline'}`, 'warning');
                    }
                } catch (e) {
                    statusBadge.className = 'status-badge status-inactive';
                    statusText.textContent = 'Offline';
                    log('Error: Could not reach server.', 'error');
                }
            }

            document.getElementById('refresh-btn').onclick = refreshStatus;
            document.getElementById('restart-btn').onclick = () => {
                if(confirm('Are you sure you want to RESTART the application?')) callApi('restart');
            };
            document.getElementById('stop-btn').onclick = () => {
                if(confirm('Are you sure you want to STOP the application? This will require manual SSH to start it again.')) callApi('stop');
            };

            // Initial load
            refreshStatus();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@router.get("/api/system/status")
async def get_status(api_key: str = Depends(verify_api_key)):
    if not has_systemctl():
        return {"ok": True, "active": True, "status": "mock", "raw": "Running on development environment (no systemctl)."}
    
    code, stdout, stderr = await run_shell_command(["systemctl", "is-active", "automation-server"])
    is_active = stdout == "active"
    
    _, full_status, _ = await run_shell_command(["systemctl", "status", "automation-server"])
    
    return {
        "ok": True, 
        "active": is_active, 
        "status": stdout, 
        "raw": full_status
    }

@router.post("/api/system/restart")
async def restart_server(api_key: str = Depends(verify_api_key)):
    if not has_systemctl():
        return {"ok": False, "error": "Not supported on this OS"}
    
    asyncio.create_task(run_shell_command(["sudo", "systemctl", "restart", "automation-server"]))
    return {"ok": True, "message": "Restart command sent"}

@router.post("/api/system/stop")
async def stop_server(api_key: str = Depends(verify_api_key)):
    if not has_systemctl():
        return {"ok": False, "error": "Not supported on this OS"}
    
    asyncio.create_task(run_shell_command(["sudo", "systemctl", "stop", "automation-server"]))
    return {"ok": True, "message": "Stop command sent"}

@router.post("/api/system/start")
async def start_server(api_key: str = Depends(verify_api_key)):
    if not has_systemctl():
        return {"ok": False, "error": "Not supported on this OS"}
    
    code, stdout, stderr = await run_shell_command(["sudo", "systemctl", "start", "automation-server"])
    return {"ok": code == 0, "message": "Start command executed", "output": stdout, "error": stderr}
