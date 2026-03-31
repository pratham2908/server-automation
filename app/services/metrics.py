import time
import psutil
import os
from collections import deque
from threading import Lock
from typing import Dict, List, Optional
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)

class MetricsService:
    """Service to track in-memory metrics for the automation server."""
    
    def __init__(self):
        self.lock = Lock()
        self.start_time = time.time()
        
        # Requests metrics
        self.total_requests = 0
        self.status_codes = {200: 0, 201: 0, 400: 0, 401: 0, 403: 0, 404: 0, 500: 0}
        self.request_durations = deque(maxlen=1000) # Last 1000 request durations (ms)
        self.last_requests = deque(maxlen=20) # Last 20 request metadata
        
        # Background tasks metrics
        self.tasks: Dict[str, Dict] = {
            "auto_publisher": {"status": "idle", "last_run": None, "count": 0, "errors": 0},
            "comment_analysis": {"status": "idle", "last_run": None, "count": 0, "errors": 0},
            "comment_reply": {"status": "idle", "last_run": None, "count": 0, "errors": 0},
        }

        # AI metrics
        self.ai_calls = 0
        self.ai_errors = 0
        self.ai_total_latency = 0.0
        self.ai_model_usage = {} # model -> count
        self.ai_last_calls = deque(maxlen=20) # Last 20 AI calls

    def record_request(self, method: str, path: str, status: int, duration_ms: float):
        """Records an HTTP request's metrics."""
        with self.lock:
            self.total_requests += 1
            self.status_codes[status] = self.status_codes.get(status, 0) + 1
            self.request_durations.append(duration_ms)
            self.last_requests.append({
                "time": datetime.now(timezone.utc).isoformat(),
                "method": method,
                "path": path,
                "status": status,
                "duration_ms": round(duration_ms, 2)
            })

    def record_ai_call(self, model: str, duration_ms: float, status: str = "success"):
        """Records an AI (Gemini) call metrics."""
        with self.lock:
            self.ai_calls += 1
            self.ai_total_latency += duration_ms
            self.ai_model_usage[model] = self.ai_model_usage.get(model, 0) + 1
            if status != "success":
                self.ai_errors += 1
            
            self.ai_last_calls.append({
                "time": datetime.now(timezone.utc).isoformat(),
                "model": model,
                "duration_ms": round(duration_ms, 2),
                "status": status
            })

    def track_task_start(self, task_name: str):
        """Marks a background task as running."""
        with self.lock:
            if task_name in self.tasks:
                self.tasks[task_name]["status"] = "running"
                self.tasks[task_name]["last_run_start"] = datetime.now(timezone.utc).isoformat()

    def track_task_end(self, task_name: str, result: str = "success"):
        """Marks a background task as finished."""
        with self.lock:
            if task_name in self.tasks:
                self.tasks[task_name]["status"] = "idle"
                self.tasks[task_name]["last_run"] = datetime.now(timezone.utc).isoformat()
                self.tasks[task_name]["count"] += 1
                if result != "success":
                    self.tasks[task_name]["errors"] += 1

    def get_system_stats(self) -> Dict:
        """Fetches current system-level statistics."""
        try:
            # CPU
            cpu_percent = psutil.cpu_percent(interval=None)
            cpu_count = psutil.cpu_count()
            
            # Memory
            mem = psutil.virtual_memory()
            mem_total = mem.total / (1024 ** 3) # GB
            mem_used = mem.percent
            
            # Disk
            disk = psutil.disk_usage('/')
            disk_total = disk.total / (1024 ** 3) # GB
            disk_used = disk.percent
            
            # Process specific
            process = psutil.Process(os.getpid())
            proc_mem = process.memory_info().rss / (1024 ** 2) # MB
            proc_cpu = process.cpu_percent(interval=None)
            uptime = time.time() - self.start_time
            
            return {
                "cpu": {"percent": cpu_percent, "cores": cpu_count},
                "mem": {"percent": mem_used, "total_gb": round(mem_total, 2)},
                "disk": {"percent": disk_used, "total_gb": round(disk_total, 2)},
                "process": {
                    "mem_mb": round(proc_mem, 2),
                    "cpu_percent": proc_cpu,
                    "uptime_seconds": int(uptime)
                }
            }
        except Exception as e:
            logger.error(f"Failed to fetch system stats: {e}")
            return {"error": str(e)}

    def get_summary(self) -> Dict:
        """Returns a full summary of dashboard data."""
        with self.lock:
            avg_duration = sum(self.request_durations) / len(self.request_durations) if self.request_durations else 0
            
            return {
                "server_time": datetime.now(timezone.utc).isoformat(),
                "uptime_human": self._format_uptime(time.time() - self.start_time),
                "requests": {
                    "total": self.total_requests,
                    "status_counts": self.status_codes,
                    "avg_duration_ms": round(avg_duration, 2),
                    "recent": list(self.last_requests)
                },
                "ai": {
                    "total_calls": self.ai_calls,
                    "avg_latency_ms": round(self.ai_total_latency / self.ai_calls, 2) if self.ai_calls > 0 else 0,
                    "errors": self.ai_errors,
                    "error_rate": round(self.ai_errors / self.ai_calls * 100, 2) if self.ai_calls > 0 else 0,
                    "model_usage": self.ai_model_usage,
                    "history": list(self.ai_last_calls)
                },
                "tasks": self.tasks,
                "system": self.get_system_stats()
            }

    def _format_uptime(self, seconds: float) -> str:
        days, rem = divmod(int(seconds), 86400)
        hours, rem = divmod(rem, 3600)
        minutes, rem = divmod(rem, 60)
        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m {rem}s"

    async def persist_snapshot(self, db):
        """Persists a snapshot of the current metrics to the database."""
        summary = self.get_summary()
        # Remove volatile 'recent' list to save space
        if "requests" in summary:
            summary["requests"]["recent"] = []
        if "ai" in summary:
            summary["ai"]["history"] = []
        
        await db.metrics_history.insert_one(summary)
        logger.info("Persisted metrics snapshot to DB")

# Global instance
metrics_service = MetricsService()
