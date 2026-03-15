from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse
import asyncio
from app.logger import get_logs
import os

router = APIRouter(tags=["ui"])

@router.get("/logs", response_class=HTMLResponse)
async def get_log_viewer():
    """Returns a premium live log viewer page using EventSource for real-time streaming."""
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Live Logs | YouTube Automation</title>
        <link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500&family=Inter:wght@400;600&display=swap" rel="stylesheet">
        <style>
            :root {
                --bg-dark: #0f172a;
                --bg-card: #1e293b;
                --text-main: #f8fafc;
                --text-muted: #94a3b8;
                --primary: #38bdf8;
                --accent: #818cf8;
                --success: #4ade80;
                --warning: #fbbf24;
                --error: #f87171;
            }

            * { box-sizing: border-box; margin: 0; padding: 0; }

            body {
                font-family: 'Inter', sans-serif;
                background-color: var(--bg-dark);
                color: var(--text-main);
                height: 100vh;
                display: flex;
                flex-direction: column;
                overflow: hidden;
            }

            header {
                padding: 1rem 2rem;
                background: rgba(30, 41, 59, 0.8);
                backdrop-filter: blur(12px);
                border-bottom: 1px solid rgba(255, 255, 255, 0.1);
                display: flex;
                justify-content: space-between;
                align-items: center;
                z-index: 10;
            }

            .logo {
                display: flex;
                align-items: center;
                gap: 0.75rem;
                font-weight: 600;
                font-size: 1.25rem;
                color: var(--primary);
            }

            .status {
                display: flex;
                align-items: center;
                gap: 0.5rem;
                font-size: 0.875rem;
                color: var(--text-muted);
            }

            .status-dot {
                width: 8px;
                height: 8px;
                background-color: var(--success);
                border-radius: 50%;
                box-shadow: 0 0 8px var(--success);
            }

            .status-dot.live { animation: pulse 2s infinite; }
            .status-dot.error { background-color: var(--error); box-shadow: 0 0 8px var(--error); }

            @keyframes pulse {
                0% { opacity: 1; }
                50% { opacity: 0.5; }
                100% { opacity: 1; }
            }

            #log-container {
                flex: 1;
                overflow-y: auto;
                padding: 1.5rem 2rem;
                font-family: 'Fira Code', monospace;
                font-size: 0.85rem;
                line-height: 1.5;
                scroll-behavior: smooth;
            }

            .log-line {
                margin-bottom: 0.25rem;
                white-space: pre-wrap;
                word-break: break-all;
                border-left: 2px solid transparent;
                padding-left: 0.75rem;
                transition: background 0.1s;
                color: var(--text-muted);
            }

            .log-line:hover { background: rgba(255, 255, 255, 0.03); }

            .line-error, .line-critical { color: var(--error); border-left-color: var(--error); }
            .line-warning { color: var(--warning); border-left-color: var(--warning); }
            .line-info { color: var(--text-main); }
            .line-success { color: var(--success); border-left-color: var(--success); }

            .keyword-info { color: var(--primary); font-weight: 500; }
            .keyword-error { color: var(--error); font-weight: 600; }
            .keyword-warning { color: var(--warning); font-weight: 500; }

            ::-webkit-scrollbar { width: 8px; }
            ::-webkit-scrollbar-track { background: var(--bg-dark); }
            ::-webkit-scrollbar-thumb { background: #334155; border-radius: 4px; }
            ::-webkit-scrollbar-thumb:hover { background: #475569; }

            .controls {
                position: fixed;
                bottom: 2rem;
                right: 2rem;
                display: flex;
                gap: 0.75rem;
            }

            .btn {
                background: var(--bg-card);
                border: 1px solid rgba(255, 255, 255, 0.1);
                color: var(--text-main);
                padding: 0.5rem 1rem;
                border-radius: 0.5rem;
                cursor: pointer;
                font-size: 0.875rem;
                display: flex;
                align-items: center;
                gap: 0.5rem;
                transition: all 0.2s;
                backdrop-filter: blur(8px);
            }

            .btn:hover { background: #334155; border-color: var(--primary); }
            .btn.active { background: var(--primary); color: var(--bg-dark); font-weight: 600; }
        </style>
    </head>
    <body>
        <header>
            <div class="logo">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
                <span>System Logs (journalctl)</span>
            </div>
            <div class="status">
                <div id="status-dot" class="status-dot live"></div>
                <span id="connection-status">Live Stream</span>
            </div>
        </header>

        <main id="log-container"></main>

        <div class="controls">
            <button id="copy-btn" class="btn">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                Copy
            </button>
            <button id="clear-btn" class="btn">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                Clear
            </button>
            <button id="autoscroll-btn" class="btn active">
                Autoscroll: On
            </button>
        </div>

        <script>
            const logContainer = document.getElementById('log-container');
            const clearBtn = document.getElementById('clear-btn');
            const copyBtn = document.getElementById('copy-btn');
            const autoscrollBtn = document.getElementById('autoscroll-btn');
            const connectionStatus = document.getElementById('connection-status');
            const statusDot = document.getElementById('status-dot');
            
            let autoscroll = true;
            let eventSource = null;

            function formatLogLine(line) {
                if (!line.trim()) return '';
                
                let className = '';
                const lowerLine = line.toLowerCase();
                if (lowerLine.includes('error') || lowerLine.includes('failed') || lowerLine.includes('exception')) {
                    className = 'line-error';
                } else if (lowerLine.includes('warning')) {
                    className = 'line-warning';
                } else if (lowerLine.includes('info')) {
                    className = 'line-info';
                } else if (lowerLine.includes('success') || lowerLine.includes('started')) {
                    className = 'line-success';
                }

                let formattedLine = line
                    .replace(/INFO:/g, '<span class="keyword-info">INFO:</span>')
                    .replace(/ERROR:/g, '<span class="keyword-error">ERROR:</span>')
                    .replace(/WARNING:/g, '<span class="keyword-warning">WARNING:</span>');

                return `<div class="log-line ${className}">${formattedLine}</div>`;
            }

            function connect() {
                if (eventSource) eventSource.close();
                
                eventSource = new EventSource('/api/v1/logs/stream');
                
                eventSource.onmessage = (event) => {
                    const line = event.data;
                    const logEntry = formatLogLine(line);
                    if (logEntry) {
                        logContainer.insertAdjacentHTML('beforeend', logEntry);
                        if (autoscroll) {
                            logContainer.scrollTop = logContainer.scrollHeight;
                        }
                    }
                    
                    connectionStatus.textContent = 'Live Stream';
                    statusDot.className = 'status-dot live';
                    statusDot.style.opacity = '1';
                };

                eventSource.onerror = (err) => {
                    console.error('SSE Error:', err);
                    connectionStatus.textContent = 'Reconnecting...';
                    statusDot.className = 'status-dot error';
                    statusDot.style.opacity = '0.5';
                };
            }

            clearBtn.onclick = () => { logContainer.innerHTML = ''; };

            copyBtn.onclick = () => {
                const text = Array.from(logContainer.querySelectorAll('.log-line'))
                    .map(el => el.innerText)
                    .join('\\n');
                navigator.clipboard.writeText(text).then(() => {
                    const originalText = copyBtn.innerHTML;
                    copyBtn.textContent = 'Copied!';
                    setTimeout(() => copyBtn.innerHTML = originalText, 2000);
                });
            };

            autoscrollBtn.onclick = () => {
                autoscroll = !autoscroll;
                autoscrollBtn.textContent = `Autoscroll: ${autoscroll ? 'On' : 'Off'}`;
                autoscrollBtn.classList.toggle('active', autoscroll);
            };

            connect();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@router.get("/api/v1/logs/stream")
async def stream_logs():
    """Streams journalctl logs in real-time using Server-Sent Events."""
    async def log_generator():
        # Fallback to internal logs if not posix
        if os.name != 'posix':
            yield "data: [Internal Log Fallback (Non-Linux OS)]\n\n"
            for log in get_logs():
                yield f"data: {log}\n\n"
            return

        try:
            # Absolute path to journalctl
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/journalctl", "-u", "automation-server", "-f", "-n", "50",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            while True:
                line = await process.stdout.readline()
                if not line:
                    if process.returncode is not None:
                        error = await process.stderr.read()
                        err_msg = error.decode().strip()
                        yield f"data: [Stream Disconnected (Code {process.returncode}): {err_msg}]\n\n"
                        break
                    # Keep-alive in case of silence
                    await asyncio.sleep(0.5)
                    continue
                
                clean_line = line.decode().strip()
                if clean_line:
                    yield f"data: {clean_line}\n\n"
                
        except Exception as e:
            yield f"data: [Error: {str(e)}]\n\n"
            for log in get_logs():
                yield f"data: {log}\n\n"

    return StreamingResponse(
        log_generator(), 
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

@router.get("/api/v1/logs")
async def get_logs_api():
    """Fallback JSON endpoint for internal log buffer."""
    return get_logs()
