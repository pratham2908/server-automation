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

            .toolbar {
                padding: 0.75rem 2rem;
                background: rgba(30, 41, 59, 0.6);
                border-bottom: 1px solid rgba(255, 255, 255, 0.05);
                display: flex;
                align-items: center;
                gap: 0.75rem;
                flex-wrap: wrap;
                z-index: 9;
            }

            .filter-group {
                display: flex;
                gap: 0.35rem;
            }

            .filter-btn {
                background: var(--bg-dark);
                border: 1px solid rgba(255, 255, 255, 0.1);
                color: var(--text-muted);
                padding: 0.3rem 0.65rem;
                border-radius: 0.4rem;
                cursor: pointer;
                font-size: 0.75rem;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.03em;
                transition: all 0.15s;
            }
            .filter-btn:hover { border-color: var(--primary); color: var(--text-main); }
            .filter-btn.active { background: var(--primary); color: var(--bg-dark); border-color: var(--primary); }
            .filter-btn.active-error { background: var(--error); color: #fff; border-color: var(--error); }
            .filter-btn.active-warning { background: var(--warning); color: var(--bg-dark); border-color: var(--warning); }
            .filter-btn.active-success { background: var(--success); color: var(--bg-dark); border-color: var(--success); }
            .filter-btn.active-info { background: var(--primary); color: var(--bg-dark); border-color: var(--primary); }

            .search-box {
                flex: 1;
                min-width: 160px;
                max-width: 320px;
                position: relative;
            }
            .search-box input {
                width: 100%;
                background: var(--bg-dark);
                border: 1px solid rgba(255, 255, 255, 0.1);
                color: var(--text-main);
                padding: 0.35rem 0.65rem 0.35rem 2rem;
                border-radius: 0.4rem;
                font-size: 0.8rem;
                font-family: 'Fira Code', monospace;
                outline: none;
                transition: border-color 0.15s;
            }
            .search-box input:focus { border-color: var(--primary); }
            .search-box input::placeholder { color: rgba(148, 163, 184, 0.5); }
            .search-box svg {
                position: absolute;
                left: 0.55rem;
                top: 50%;
                transform: translateY(-50%);
                color: var(--text-muted);
                pointer-events: none;
            }

            .match-count {
                font-size: 0.7rem;
                color: var(--text-muted);
                padding: 0.25rem 0.5rem;
                background: var(--bg-dark);
                border-radius: 0.3rem;
                white-space: nowrap;
            }

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

            .log-line.hidden, .request-box.hidden { display: none; }

            /* Request Box Styles */
            .request-box {
                margin: 0.75rem 0;
                background: rgba(30, 41, 59, 0.4);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 0.75rem;
                overflow: hidden;
                transition: all 0.2s ease;
            }
            .request-box[open] {
                border-color: rgba(255, 255, 255, 0.2);
                box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.2);
                background: rgba(30, 41, 59, 0.6);
            }
            .request-box summary {
                padding: 0.75rem 1rem;
                display: flex;
                align-items: center;
                gap: 1rem;
                cursor: pointer;
                list-style: none;
                user-select: none;
                font-weight: 500;
            }
            .request-box summary::-webkit-details-marker { display: none; }
            .request-box .method {
                font-size: 0.7rem;
                padding: 0.15rem 0.5rem;
                border-radius: 4px;
                background: var(--bg-dark);
                font-weight: 700;
                color: var(--primary);
                min-width: 50px;
                text-align: center;
            }
            .request-box .path { flex: 1; color: var(--text-main); font-family: var(--font-mono); font-size: 0.8rem; overflow: hidden; text-overflow: ellipsis; }
            .request-box .status { font-size: 0.75rem; font-weight: 700; border-radius: 4px; padding: 0.15rem 0.4rem; }
            .request-box .st-success { color: var(--success); background: rgba(74, 222, 128, 0.1); }
            .request-box .st-warning { color: var(--warning); background: rgba(251, 191, 36, 0.1); }
            .request-box .st-error { color: var(--error); background: rgba(248, 113, 113, 0.1); }
            .request-box .duration { color: var(--text-muted); font-size: 0.7rem; font-family: var(--font-mono); }
            .request-box .req-id { color: rgba(255,255,255,0.2); font-size: 0.65rem; }

            .metadata-content {
                padding: 1rem;
                border-top: 1px solid rgba(255, 255, 255, 0.05);
                font-size: 0.75rem;
                color: var(--text-muted);
                line-height: 1.6;
            }
            .metadata-content pre {
                margin-top: 0.5rem;
                background: rgba(0,0,0,0.2);
                padding: 0.75rem;
                border-radius: 0.5rem;
                overflow-x: auto;
                font-family: var(--font-mono);
            }
            .box-success { border-left: 3px solid var(--success); }
            .box-warning { border-left: 3px solid var(--warning); }
            .box-error { border-left: 3px solid var(--error); }
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

        <div class="toolbar">
            <div class="filter-group">
                <button class="filter-btn active" data-level="all">ALL</button>
                <button class="filter-btn" data-level="error">ERROR</button>
                <button class="filter-btn" data-level="warning">WARNING</button>
                <button class="filter-btn" data-level="success">SUCCESS</button>
                <button class="filter-btn" data-level="info">INFO</button>
            </div>
            <div class="search-box">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
                <input id="search-input" type="text" placeholder="Search logs..." />
            </div>
            <span id="match-count" class="match-count" style="display:none"></span>
        </div>

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
            const searchInput = document.getElementById('search-input');
            const matchCountEl = document.getElementById('match-count');
            const filterBtns = document.querySelectorAll('.filter-btn');
            
            let autoscroll = true;
            let eventSource = null;
            let activeLevel = 'all';
            let searchTerm = '';

            function classifyLevel(line) {
                const lower = line.toLowerCase();
                if (lower.includes('[error]') || lower.includes('[critical]') || lower.includes('error') || lower.includes('failed') || lower.includes('exception'))
                    return 'error';
                if (lower.includes('[warning]') || lower.includes('warning'))
                    return 'warning';
                if (lower.includes('[success]') || lower.includes('success') || lower.includes('successfully') ||
                    lower.includes('started') || lower.includes('analyzed') || lower.includes('completed') ||
                    line.includes('\\u2705') || line.includes('\\u2728') || line.includes('\\ud83d'))
                    return 'success';
                return 'info';
            }

            function shouldShow(el) {
                const level = el.getAttribute('data-level') || 'info';
                const rawText = (el.getAttribute('data-raw') || el.innerText).toLowerCase();

                if (activeLevel !== 'all' && level !== activeLevel) return false;
                if (searchTerm && !rawText.includes(searchTerm.toLowerCase())) return false;
                return true;
            }

            function applyFilters() {
                const allLines = logContainer.querySelectorAll('.log-line, .request-box');
                let visible = 0;
                allLines.forEach(el => {
                    const show = shouldShow(el);
                    el.classList.toggle('hidden', !show);
                    if (show) visible++;
                });
                if (searchTerm || activeLevel !== 'all') {
                    matchCountEl.textContent = `${visible} match${visible !== 1 ? 'es' : ''}`;
                    matchCountEl.style.display = '';
                } else {
                    matchCountEl.style.display = 'none';
                }
            }

            filterBtns.forEach(btn => {
                btn.addEventListener('click', () => {
                    const level = btn.getAttribute('data-level');
                    if (activeLevel === level && level !== 'all') {
                        activeLevel = 'all';
                    } else {
                        activeLevel = level;
                    }
                    filterBtns.forEach(b => {
                        b.classList.remove('active', 'active-error', 'active-warning', 'active-success', 'active-info');
                    });
                    const activeBtn = document.querySelector(`.filter-btn[data-level="${activeLevel}"]`);
                    if (activeLevel === 'all') {
                        activeBtn.classList.add('active');
                    } else {
                        activeBtn.classList.add('active-' + activeLevel);
                    }
                    applyFilters();
                });
            });

            let searchDebounce = null;
            searchInput.addEventListener('input', () => {
                clearTimeout(searchDebounce);
                searchDebounce = setTimeout(() => {
                    searchTerm = searchInput.value.trim();
                    applyFilters();
                }, 200);
            });

            function formatLogLine(line) {
                if (!line.trim()) return '';
                
                if (line.includes('REQUEST_BOX:')) {
                    try {
                        const jsonStr = line.split('REQUEST_BOX:')[1].trim();
                        const data = JSON.parse(jsonStr);
                        const statusClass = data.status >= 400 ? 'error' : (data.status >= 300 ? 'warning' : 'success');
                        const level = data.status >= 400 ? 'error' : (data.status >= 300 ? 'warning' : 'success');
                        
                        return `
                        <details class="request-box box-${statusClass}" data-level="${level}" data-raw="${data.method} ${data.path} ${data.status} ${data.query || ''}">
                            <summary>
                                <span class="method">${data.method}</span>
                                <span class="path">${data.path}</span>
                                <span class="status st-${statusClass}">${data.status}</span>
                                <span class="duration">${data.duration_ms}ms</span>
                                <span class="req-id">#${data.id}</span>
                            </summary>
                            <div class="metadata-content">
                                <strong>Query:</strong> <span style="color:var(--text-main)">${data.query || 'None'}</span><br>
                                <div style="margin-top:0.5rem"><strong>Headers:</strong></div>
                                <pre>${JSON.stringify(data.headers, null, 2)}</pre>
                            </div>
                        </details>`;
                    } catch (e) {
                         console.error('Failed to parse request box:', e);
                    }
                }

                const level = classifyLevel(line);
                let className = '';
                if (level === 'error') className = 'line-error';
                else if (level === 'warning') className = 'line-warning';
                else if (level === 'success') className = 'line-success';
                else className = 'line-info';

                let formattedLine = line
                    .replace(/INFO:/g, '<span class="keyword-info">INFO:</span>')
                    .replace(/ERROR:/g, '<span class="keyword-error">ERROR:</span>')
                    .replace(/WARNING:/g, '<span class="keyword-warning">WARNING:</span>');

                const hidden = (activeLevel !== 'all' && level !== activeLevel) ||
                               (searchTerm && !line.toLowerCase().includes(searchTerm.toLowerCase()));

                return `<div class="log-line ${className}${hidden ? ' hidden' : ''}" data-level="${level}" data-raw="${line.replace(/"/g, '&quot;')}">${formattedLine}</div>`;
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

            clearBtn.onclick = () => {
                logContainer.innerHTML = '';
                matchCountEl.style.display = 'none';
            };

            copyBtn.onclick = () => {
                const text = Array.from(logContainer.querySelectorAll('.log-line:not(.hidden)'))
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
