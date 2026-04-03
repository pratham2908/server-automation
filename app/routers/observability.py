from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import HTMLResponse
from app.services.metrics import metrics_service
from app.config import get_settings
from app.database import get_db

router = APIRouter(tags=["observability"])

async def verify_api_key(api_key: str = Query(...)):
    settings = get_settings()
    if api_key != settings.API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    return api_key

@router.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard(api_key: str = Depends(verify_api_key)):
    """A premium, high-fidelity observability dashboard for the automation server."""
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Observability | Automation Server</title>
        <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet">
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            :root {
                --bg: #020617;
                --card-bg: rgba(30, 41, 59, 0.7);
                --card-border: rgba(255, 255, 255, 0.1);
                --text-primary: #f8fafc;
                --text-secondary: #94a3b8;
                --primary: #38bdf8;
                --primary-glow: rgba(56, 189, 248, 0.3);
                --success: #4ade80;
                --warning: #fbbf24;
                --error: #f43f5e;
                --ai-purple: #a855f7;
                --accent: #818cf8;
                --glass: rgba(15, 23, 42, 0.8);
            }

            * { box-sizing: border-box; margin: 0; padding: 0; }

            body {
                font-family: 'Plus Jakarta Sans', sans-serif;
                background-color: var(--bg);
                color: var(--text-primary);
                min-height: 100vh;
                background-image: 
                    radial-gradient(circle at 0% 0%, rgba(56, 189, 248, 0.08) 0%, transparent 50%),
                    radial-gradient(circle at 100% 100%, rgba(129, 140, 248, 0.08) 0%, transparent 50%);
                padding-bottom: 4rem;
            }

            nav {
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 1.5rem 2rem;
                background: var(--glass);
                backdrop-filter: blur(12px);
                border-bottom: 1px solid var(--card-border);
                position: sticky;
                top: 0;
                z-index: 100;
            }

            .logo {
                display: flex;
                align-items: center;
                gap: 0.75rem;
                font-weight: 800;
                font-size: 1.25rem;
                letter-spacing: -0.02em;
                color: var(--text-primary);
            }

            .logo span { color: var(--primary); }

            .status-indicator {
                display: flex;
                align-items: center;
                gap: 0.75rem;
                font-size: 0.875rem;
                font-weight: 600;
                background: rgba(0,0,0,0.3);
                padding: 0.5rem 1rem;
                border-radius: 2rem;
                border: 1px solid var(--card-border);
            }

            .dot {
                width: 8px;
                height: 8px;
                border-radius: 50%;
                background: var(--success);
                box-shadow: 0 0 12px var(--success);
            }

            .container {
                max-width: 1400px;
                margin: 2rem auto;
                padding: 0 2rem;
                display: grid;
                grid-template-columns: repeat(4, 1fr);
                gap: 1.5rem;
            }

            .card {
                background: var(--card-bg);
                backdrop-filter: blur(8px);
                border-radius: 1.25rem;
                border: 1px solid var(--card-border);
                padding: 1.5rem;
                transition: transform 0.2s, border-color 0.2s;
            }

            .card:hover { transform: translateY(-2px); border-color: rgba(255,255,255,0.2); }

            .span-2 { grid-column: span 2; }
            .span-3 { grid-column: span 3; }
            .span-4 { grid-column: span 4; }

            .stat-header {
                display: flex;
                justify-content: space-between;
                align-items: flex-start;
                margin-bottom: 1rem;
            }

            .stat-title {
                font-size: 0.875rem;
                font-weight: 600;
                color: var(--text-secondary);
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }

            .stat-value {
                font-size: 2rem;
                font-weight: 800;
                margin-top: 0.5rem;
            }

            .stat-icon {
                background: rgba(255,255,255,0.05);
                padding: 0.5rem;
                border-radius: 0.75rem;
                color: var(--primary);
            }

            /* Resource Gauges */
            .gauge-container {
                margin-top: 1.5rem;
            }

            .gauge-label {
                display: flex;
                justify-content: space-between;
                margin-bottom: 0.5rem;
                font-size: 0.875rem;
                font-weight: 500;
            }

            .gauge-bar {
                height: 8px;
                background: rgba(255,255,255,0.05);
                border-radius: 4px;
                overflow: hidden;
                margin-bottom: 1rem;
            }

            .gauge-fill {
                height: 100%;
                background: linear-gradient(90deg, var(--primary), var(--accent));
                transition: width 1s cubic-bezier(0.4, 0, 0.2, 1);
            }

            /* Log Viewer Mini */
            .log-section {
                height: 400px;
                display: flex;
                flex-direction: column;
            }

            .log-header {
                display: flex;
                justify-content: space-between;
                margin-bottom: 1rem;
            }

            .log-content {
                flex: 1;
                background: #000;
                border-radius: 0.75rem;
                padding: 1rem;
                font-family: 'Fira Code', monospace;
                font-size: 0.8rem;
                overflow-y: auto;
                border: 1px solid var(--card-border);
            }

            .log-line { margin-bottom: 0.25rem; color: #a1a1aa; }
            .log-error { color: var(--error); }
            .log-warning { color: var(--warning); }
            .log-success { color: var(--success); }

            /* Charts */
            .chart-container { height: 250px; width: 100%; }

            /* Grid Layout adjustments */
            @media (max-width: 1024px) {
                .container { grid-template-columns: repeat(2, 1fr); }
                .span-2, .span-3, .span-4 { grid-column: span 2; }
            }

            @media (max-width: 640px) {
                .container { grid-template-columns: 1fr; }
                .span-2, .span-3, .span-4 { grid-column: span 1; }
            }

            .badge {
                padding: 0.25rem 0.6rem;
                border-radius: 0.5rem;
                font-size: 0.75rem;
                font-weight: 700;
                text-transform: uppercase;
            }
            .badge-success { background: rgba(74, 222, 128, 0.1); color: var(--success); }
            .badge-warning { background: rgba(251, 191, 36, 0.1); color: var(--warning); }
            .badge-error { background: rgba(244, 63, 94, 0.1); color: var(--error); }

            table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
            th { text-align: left; padding: 0.75rem; color: var(--text-secondary); font-size: 0.75rem; border-bottom: 1px solid var(--card-border); }
            td { padding: 0.75rem; font-size: 0.875rem; border-bottom: 1px solid rgba(255,255,255,0.02); }

            .btn-action {
                background: var(--primary);
                color: #000;
                border: none;
                padding: 0.5rem 1rem;
                border-radius: 0.5rem;
                font-weight: 700;
                cursor: pointer;
                transition: all 0.2s;
            }
            .btn-action:hover { opacity: 0.9; transform: scale(1.05); }

            ::-webkit-scrollbar { width: 6px; height: 6px; }
            ::-webkit-scrollbar-track { background: transparent; }
            ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 10px; }
        </style>
    </head>
    <body>
        <nav>
            <div class="logo">
                <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"></path></svg>
                <span>Antigravity<span>OS</span></span>
            </div>
            <div style="display:flex; gap: 1rem; align-items: center">
                <div id="connection-indicator" class="status-indicator">
                    <div class="dot"></div>
                    <span id="uptime-text">Uptime: --</span>
                </div>
                <button class="btn-action" onclick="window.location.href='/system?api_key=' + (window.apiKey || '')">System Control</button>
            </div>
        </nav>

        <div class="container">
            <!-- Stats -->
            <div class="card">
                <div class="stat-header">
                    <div class="stat-title">Requests</div>
                    <div class="stat-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg></div>
                </div>
                <div id="total-requests" class="stat-value" style="font-size: 1.5rem">0</div>
            </div>
            <div class="card">
                <div class="stat-header">
                    <div class="stat-title">Srv Latency</div>
                    <div class="stat-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg></div>
                </div>
                <div id="avg-latency" class="stat-value" style="font-size: 1.5rem">0ms</div>
            </div>
            <div class="card">
                <div class="stat-header">
                    <div class="stat-title">Err Rate</div>
                    <div class="stat-icon" style="color: var(--error)"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"></path></svg></div>
                </div>
                <div id="error-rate" class="stat-value" style="font-size: 1.5rem">0.0%</div>
            </div>
            <div class="card">
                <div class="stat-header">
                    <div class="stat-title">AI Calls</div>
                    <div class="stat-icon" style="color: var(--ai-purple)"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a10 10 0 1 0 10 10H12V2Z"></path></svg></div>
                </div>
                <div id="ai-total-calls" class="stat-value" style="font-size: 1.5rem">0</div>
            </div>
            <div class="card">
                <div class="stat-header">
                    <div class="stat-title">AI Latency</div>
                    <div class="stat-icon" style="color: var(--ai-purple)"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="12 6 12 12 16 14"></polyline></svg></div>
                </div>
                <div id="ai-avg-latency" class="stat-value" style="font-size: 1.5rem">0ms</div>
            </div>
            <div class="card">
                <div class="stat-header">
                    <div class="stat-title">AI Err Rate</div>
                    <div class="stat-icon" style="color: var(--error)"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg></div>
                </div>
                <div id="ai-error-rate" class="stat-value" style="font-size: 1.5rem">0.0%</div>
            </div>

            <div class="card">
                <div class="stat-header">
                    <div class="stat-title">Active Workers</div>
                    <div class="stat-icon" style="color: var(--success)"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"></path></svg></div>
                </div>
                <div id="active-tasks" class="stat-value" style="font-size: 1.5rem">0</div>
            </div>
            <div class="card">
                <div class="stat-header">
                    <div class="stat-title">DB Documents</div>
                    <div class="stat-icon" style="color: var(--accent)"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"></ellipse><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"></path><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"></path></svg></div>
                </div>
                <div id="total-documents" class="stat-value" style="font-size: 1.5rem">0</div>
            </div>

            <!-- Resource Usage -->
            <div class="card span-2">
                <h3 style="margin-bottom: 1.5rem">System Resources</h3>
                <div class="gauge-container">
                    <div class="gauge-label">
                        <span>CPU Usage</span>
                        <span id="cpu-percent">0%</span>
                    </div>
                    <div class="gauge-bar"><div id="cpu-fill" class="gauge-fill" style="width: 0%"></div></div>
                    
                    <div class="gauge-label">
                        <span>Memory Usage</span>
                        <span id="mem-percent">0%</span>
                    </div>
                    <div class="gauge-bar"><div id="mem-fill" class="gauge-fill" style="width: 0%; background-color: var(--accent)"></div></div>
                    
                    <div class="gauge-label">
                        <span>Disk Usage</span>
                        <span id="disk-percent">0%</span>
                    </div>
                    <div class="gauge-bar"><div id="disk-fill" class="gauge-fill" style="width: 0%; background-color: var(--warning)"></div></div>
                    
                    <div style="display:flex; justify-content: space-between; margin-top: 1rem; color: var(--text-secondary); font-size: 0.75rem">
                        <span id="process-mem">RSS: 0MB</span>
                        <span id="os-platform">Server: Linux / Prodn</span>
                    </div>
                </div>
            </div>

            <!-- Traffic Sentiment -->
            <div class="card span-2">
                <h3 style="margin-bottom: 1rem">API Traffic Sentiment</h3>
                <div class="chart-container">
                    <canvas id="trafficChart"></canvas>
                </div>
            </div>

            <!-- AI Model Distribution -->
            <div class="card span-2">
                <h3 style="margin-bottom: 1rem">AI Model Distribution</h3>
                <div class="chart-container">
                    <canvas id="modelChart"></canvas>
                </div>
            </div>

            <!-- Table: Recent Requests -->
            <div class="card span-4">
                <div style="display:flex; justify-content: space-between; align-items: center">
                    <h3>Recent API Traffic</h3>
                    <div style="font-size: 0.75rem; color: var(--secondary)">Auto-refreshing every 2s</div>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>Method</th>
                            <th>Path</th>
                            <th>Status</th>
                            <th>Latency</th>
                            <th>Timestamp</th>
                        </tr>
                    </thead>
                    <tbody id="request-table">
                        <!-- Filled by JS -->
                    </tbody>
                </table>
            </div>

            <!-- Database Counts -->
            <div class="card span-2">
                <h3>Content Inventory</h3>
                <div style="display:grid; grid-template-columns: repeat(2, 1fr); gap: 1rem; margin-top: 1.5rem">
                    <div style="background: rgba(255,255,255,0.03); padding: 1.25rem; border-radius: 0.75rem; text-align: center">
                        <div style="color: var(--text-secondary); font-size: 0.7rem; text-transform: uppercase">Channels</div>
                        <div id="count-channels" style="font-size: 1.75rem; font-weight: 800; color: var(--primary)">0</div>
                    </div>
                    <div style="background: rgba(255,255,255,0.03); padding: 1.25rem; border-radius: 0.75rem; text-align: center">
                        <div style="color: var(--text-secondary); font-size: 0.7rem; text-transform: uppercase">Videos</div>
                        <div id="count-videos" style="font-size: 1.75rem; font-weight: 800; color: var(--success)">0</div>
                    </div>
                    <div style="background: rgba(255,255,255,0.03); padding: 1.25rem; border-radius: 0.75rem; text-align: center">
                        <div style="color: var(--text-secondary); font-size: 0.7rem; text-transform: uppercase">Categories</div>
                        <div id="count-categories" style="font-size: 1.75rem; font-weight: 800; color: var(--accent)">0</div>
                    </div>
                    <div style="background: rgba(255,255,255,0.03); padding: 1.25rem; border-radius: 0.75rem; text-align: center">
                        <div style="color: var(--text-secondary); font-size: 0.7rem; text-transform: uppercase">Analyses</div>
                        <div id="count-history" style="font-size: 1.75rem; font-weight: 800; color: var(--warning)">0</div>
                    </div>
                </div>
            </div>

            <!-- Background Workers -->
            <div class="card span-2">
                <h3>Background Workers</h3>
                <div id="task-list" style="margin-top: 1rem; display: flex; flex-direction: column; gap: 1rem">
                    <!-- Filled by JS -->
                </div>
            </div>

            <!-- Live Logs Window -->
            <div class="card span-2 log-section">
                <div class="log-header">
                    <h3>Live System Feed</h3>
                    <a href="/logs?api_key=""" + api_key + """" style="color: var(--primary); font-size: 0.75rem; font-weight: 700; text-decoration: none">View Full Logs →</a>
                </div>
                <div id="log-feed" class="log-content">
                    <div class="log-line">_ Connecting to system socket...</div>
                </div>
            </div>
        </div>

        <script>
            window.apiKey = '""" + api_key + """';
            let trafficChart, modelChart;
            
            // Initialize Chart
            function initChart() {
                const ctx = document.getElementById('trafficChart').getContext('2d');
                trafficChart = new Chart(ctx, {
                    type: 'doughnut',
                    data: {
                        labels: ['Successful (2xx)', 'User Error (4xx)', 'Server Error (5xx)'],
                        datasets: [{
                            data: [1, 0, 0],
                            backgroundColor: ['#4ade80', '#fbbf24', '#f43f5e'],
                            borderWidth: 0, hoverOffset: 10
                        }]
                    },
                    options: {
                        responsive: true, maintainAspectRatio: false,
                        plugins: { legend: { position: 'bottom', labels: { color: '#94a3b8', font: { size: 10 } } } },
                        cutout: '70%',
                    }
                });

                const mctx = document.getElementById('modelChart').getContext('2d');
                modelChart = new Chart(mctx, {
                    type: 'pie',
                    data: {
                        labels: ['gemini-3-flash', 'gemini-2.5-pro', 'gemini-2.5-flash'],
                        datasets: [{
                            data: [0, 0, 0],
                            backgroundColor: ['#38bdf8', '#818cf8', '#a855f7'],
                            borderWidth: 0,
                        }]
                    },
                    options: {
                        responsive: true, maintainAspectRatio: false,
                        plugins: { legend: { position: 'bottom', labels: { color: '#94a3b8', font: { size: 10 } } } },
                    }
                });
            }

            function safeSet(id, value) {
                const el = document.getElementById(id);
                if (el) el.textContent = value;
            }

            function safeStyle(id, prop, value) {
                const el = document.getElementById(id);
                if (el) el.style[prop] = value;
            }

            function updateStats(data) {
                try {
                    safeSet('total-requests', data.requests.total);
                    safeSet('avg-latency', data.requests.avg_duration_ms + 'ms');
                    safeSet('uptime-text', 'Uptime: ' + data.uptime_human);
                } catch (e) { console.warn('Stats: requests section error', e); }

                try {
                    if (data.ai) {
                        safeSet('ai-total-calls', data.ai.total_calls || 0);
                        safeSet('ai-avg-latency', (data.ai.avg_latency_ms || 0).toFixed(0) + 'ms');
                        safeSet('ai-error-rate', (data.ai.error_rate || 0).toFixed(1) + '%');

                        const usage = data.ai.model_usage || {};
                        if (Object.keys(usage).length > 0) {
                            modelChart.data.labels = Object.keys(usage);
                            modelChart.data.datasets[0].data = Object.values(usage);
                            modelChart.update();
                        }
                    }
                } catch (e) { console.warn('Stats: AI section error', e); }

                try {
                    const statusCounts = data.requests.status_counts || {};
                    const total = data.requests.total || 0;
                    let errorRate = "0.0";
                    if (total > 0) {
                        const errors = (statusCounts['500'] || 0) + (statusCounts['400'] || 0) + (statusCounts['401'] || 0) + (statusCounts['403'] || 0);
                        errorRate = (errors / total * 100).toFixed(1);
                    }
                    safeSet('error-rate', errorRate + '%');

                    const s2 = (statusCounts['200'] || 0) + (statusCounts['201'] || 0);
                    const s4 = (statusCounts['400'] || 0) + (statusCounts['401'] || 0) + (statusCounts['403'] || 0) + (statusCounts['404'] || 0);
                    const s5 = (statusCounts['500'] || 0);
                    trafficChart.data.datasets[0].data = [s2, s4, s5];
                    trafficChart.update();
                } catch (e) { console.warn('Stats: error rate section error', e); }

                try {
                    if (data.database) {
                        safeSet('count-channels', data.database.channels);
                        safeSet('count-videos', data.database.videos);
                        safeSet('count-categories', data.database.categories);
                        safeSet('count-history', data.database.history);
                        const totalDocs = (data.database.channels || 0) + (data.database.videos || 0) + (data.database.categories || 0) + (data.database.history || 0);
                        safeSet('total-documents', totalDocs);
                    }
                } catch (e) { console.warn('Stats: database section error', e); }

                try {
                    if (data.system && data.system.cpu) {
                        const sys = data.system;
                        safeSet('cpu-percent', sys.cpu.percent + '%');
                        safeStyle('cpu-fill', 'width', sys.cpu.percent + '%');

                        safeSet('mem-percent', sys.mem.percent + '%');
                        safeStyle('mem-fill', 'width', sys.mem.percent + '%');

                        safeSet('disk-percent', sys.disk.percent + '%');
                        safeStyle('disk-fill', 'width', sys.disk.percent + '%');

                        if (sys.process) safeSet('process-mem', 'RSS: ' + sys.process.mem_mb + 'MB');
                    }
                } catch (e) { console.warn('Stats: system section error', e); }

                try {
                    let activeCount = 0;
                    let taskHtml = '';
                    for (const [name, task] of Object.entries(data.tasks || {})) {
                        if (task.status === 'running') activeCount++;
                        const statusClass = task.status === 'running' ? 'badge-success' : 'badge-warning';
                        taskHtml += `
                            <div style="display:flex; justify-content: space-between; align-items: center; background: rgba(255,255,255,0.03); padding: 0.75rem; border-radius: 0.75rem">
                                <div>
                                    <div style="font-weight:700; text-transform: capitalize">${name.replace(/_/g, ' ')}</div>
                                    <div style="font-size:0.7rem; color: var(--text-secondary)">Last: ${task.last_run ? new Date(task.last_run).toLocaleTimeString() : 'Never'}</div>
                                </div>
                                <div class="badge ${statusClass}">${task.status}</div>
                            </div>
                        `;
                    }
                    safeSet('active-tasks', activeCount);
                    const taskListEl = document.getElementById('task-list');
                    if (taskListEl) taskListEl.innerHTML = taskHtml;
                } catch (e) { console.warn('Stats: tasks section error', e); }

                try {
                    let tableHtml = '';
                    const recent = data.requests.recent || [];
                    [...recent].reverse().forEach(req => {
                        const statusClass = req.status >= 500 ? 'badge-error' : (req.status >= 400 ? 'badge-warning' : 'badge-success');
                        tableHtml += `
                            <tr>
                                <td><span style="font-weight:700; color: var(--primary)">${req.method}</span></td>
                                <td title="${req.path}"><code style="font-size:0.7rem">${req.path.substring(0, 30)}${req.path.length > 30 ? '...' : ''}</code></td>
                                <td><span class="badge ${statusClass}">${req.status}</span></td>
                                <td>${req.duration_ms}ms</td>
                                <td style="font-size:0.7rem; color: var(--text-secondary)">${new Date(req.time).toLocaleTimeString()}</td>
                            </tr>
                        `;
                    });
                    const reqTable = document.getElementById('request-table');
                    if (reqTable) reqTable.innerHTML = tableHtml;
                } catch (e) { console.warn('Stats: request table error', e); }
            }

            async function fetchData() {
                try {
                    const res = await fetch(\`/api/v1/observability/metrics?api_key=\${window.apiKey}\`);
                    const data = await res.json();
                    updateStats(data);

                } catch (e) {
                    console.error("Dashboard fetch error:", e);
                }
            }

            // Live Logs
            function initLogs() {
                const logFeed = document.getElementById('log-feed');
                const eventSource = new EventSource('/api/v1/logs/stream');
                
                eventSource.onmessage = (event) => {
                    const line = event.data;
                    if (line.includes('REQUEST_BOX')) return; // Skip box logs in tiny view
                    
                    const el = document.createElement('div');
                    el.className = 'log-line';
                    
                    if (line.toLowerCase().includes('error')) el.classList.add('log-error');
                    else if (line.toLowerCase().includes('warning')) el.classList.add('log-warning');
                    else if (line.toLowerCase().includes('success') || line.includes('✅')) el.classList.add('log-success');
                    
                    el.textContent = line.substring(0, 150) + (line.length > 150 ? '...' : '');
                    logFeed.appendChild(el);
                    logFeed.scrollTop = logFeed.scrollHeight;
                    
                    if (logFeed.children.length > 50) logFeed.removeChild(logFeed.firstChild);
                };
            }

            window.onload = () => {
                initChart();
                initLogs();
                fetchData();
                setInterval(fetchData, 2000); // 2s refresh
            };
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@router.get("/api/v1/observability/metrics")
async def get_metrics_api(api_key: str = Depends(verify_api_key)):
    """Returns detailed metrics for the dashboard, including database stats."""
    db = get_db()

    counts = {
        "channels": await db.channels.count_documents({}),
        "videos": await db.videos.count_documents({}),
        "categories": await db.categories.count_documents({}),
        "history": await db.analysis_history.count_documents({}),
    }

    summary = metrics_service.get_summary()
    summary["database"] = counts
    return summary
