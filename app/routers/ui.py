from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse
import asyncio
from app.logger import get_logs
import json

router = APIRouter(tags=["ui"])

@router.get("/logs", response_class=HTMLResponse)
async def get_log_viewer():
    """Returns a premium live log viewer page."""
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

            * {
                box-sizing: border-box;
                margin: 0;
                padding: 0;
            }

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
                animation: pulse 2s infinite;
            }

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
                font-size: 0.9rem;
                line-height: 1.6;
                scroll-behavior: smooth;
            }

            .log-line {
                margin-bottom: 0.5rem;
                white-space: pre-wrap;
                word-break: break-all;
                border-left: 2px solid transparent;
                padding-left: 0.75rem;
                transition: background 0.2s;
            }

            .log-line:hover {
                background: rgba(255, 255, 255, 0.03);
            }

            .level-INFO { color: var(--text-main); }
            .level-SUCCESS { color: var(--success); border-left-color: var(--success); }
            .level-WARNING { color: var(--warning); border-left-color: var(--warning); }
            .level-ERROR { color: var(--error); border-left-color: var(--error); }
            .level-CRITICAL { color: var(--error); font-weight: bold; border-left-color: var(--error); background: rgba(248, 113, 113, 0.1); }

            .timestamp { color: var(--text-muted); margin-right: 0.5rem; font-size: 0.8rem; }
            .logger-name { color: var(--accent); margin-right: 0.5rem; }

            /* Scrollbar styling */
            ::-webkit-scrollbar { width: 8px; }
            ::-webkit-scrollbar-track { background: var(--bg-dark); }
            ::-webkit-scrollbar-thumb { background: #334155; border-radius: 4px; }
            ::-webkit-scrollbar-thumb:hover { background: #475569; }

            .controls {
                position: fixed;
                bottom: 2rem;
                right: 2rem;
                display: flex;
                gap: 1rem;
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

            .btn:hover {
                background: #334155;
                border-color: var(--primary);
            }

            .btn.active {
                background: var(--primary);
                color: var(--bg-dark);
                font-weight: 600;
            }
        </style>
    </head>
    <body>
        <header>
            <div class="logo">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
                <span>Automation Server Logs</span>
            </div>
            <div class="status">
                <div class="status-dot"></div>
                <span id="connection-status">Live</span>
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
            
            let autoscroll = true;
            let lastSeenLogs = [];

            function formatLogLine(line) {
                const regex = /^\[(.*?)\] \[(.*?)\] \[(.*?)\] (.*)$/;
                const match = line.match(regex);
                
                if (match) {
                    const [_, timestamp, level, logger, message] = match;
                    return `
                        <div class="log-line level-${level}">
                            <span class="timestamp">${timestamp}</span>
                            <span class="logger-name">[${logger}]</span>
                            <span class="message">${message}</span>
                        </div>
                    `;
                }
                return `<div class="log-line">${line}</div>`;
            }

            async function fetchLogs() {
                try {
                    const response = await fetch('/api/v1/logs');
                    if (!response.ok) throw new Error('Failed to fetch');
                    const logs = await response.json();
                    
                    // Find where the new logs start
                    let newLogsIndex = 0;
                    if (lastSeenLogs.length > 0) {
                        const lastSeen = lastSeenLogs[lastSeenLogs.length - 1];
                        newLogsIndex = logs.lastIndexOf(lastSeen) + 1;
                        
                        // If we can't find the last log, it might have rolled over or been duplicated
                        // In that case, we might miss some or show duplicates, but lastIndexOf is usually safe
                        if (newLogsIndex === 0 && logs.length > 0) {
                             // If the last seen log isn't in the new list at all, just take everything
                             // unless the list is exactly the same as before
                             if (JSON.stringify(logs) === JSON.stringify(lastSeenLogs)) {
                                 newLogsIndex = logs.length;
                             }
                        }
                    }

                    const newLogs = logs.slice(newLogsIndex);
                    if (newLogs.length > 0) {
                        newLogs.forEach(log => {
                            logContainer.insertAdjacentHTML('beforeend', formatLogLine(log));
                        });
                        lastSeenLogs = logs;
                        
                        if (autoscroll) {
                            logContainer.scrollTop = logContainer.scrollHeight;
                        }
                    }
                    connectionStatus.textContent = 'Live';
                    connectionStatus.parentElement.querySelector('.status-dot').style.backgroundColor = 'var(--success)';
                } catch (err) {
                    console.error('Error fetching logs:', err);
                    connectionStatus.textContent = 'Disconnected';
                    connectionStatus.parentElement.querySelector('.status-dot').style.backgroundColor = 'var(--error)';
                }
            }

            clearBtn.onclick = () => {
                logContainer.innerHTML = '';
                lastSeenLogs = [];
            };

            copyBtn.onclick = () => {
                const text = Array.from(logContainer.querySelectorAll('.log-line'))
                    .map(el => el.innerText)
                    .join('\n');
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

            // Poll every 1 second for simplicity, or we could use SSE
            setInterval(fetchLogs, 1000);
            fetchLogs(); // Initial fetch
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@router.get("/api/v1/logs")
async def get_logs_api():
    """Returns the current log buffer as JSON."""
    return get_logs()
