from collections import deque
from datetime import datetime, timezone
from typing import Any


class MetricsService:
    def __init__(self):
        # Requests metrics
        self.total_requests = 0
        self.status_codes = {200: 0, 201: 0, 400: 0, 401: 0, 403: 0, 404: 0, 500: 0}
        self.request_durations: deque[float] = deque(maxlen=1000)  # Last 1000 request durations (ms)
        self.last_requests: deque[dict[str, Any]] = deque(maxlen=20)  # Last 20 request metadata

        # Background tasks metrics
        self.tasks: dict[str, dict] = {
            "sync_analysis": {"last_run": None, "last_status": "none", "count": 0},
            "velocity_booster": {"last_run": None, "last_status": "none", "count": 0},
            "comment_analysis": {"last_run": None, "last_status": "none", "count": 0},
        }

        # AI metrics
        self.ai_calls = 0
        self.ai_errors = 0
        self.ai_total_latency = 0.0
        self.ai_model_usage: dict[str, int] = {}  # model -> count
        self.ai_last_calls: deque[dict[str, Any]] = deque(maxlen=20)  # Last 20 AI calls

        # External API metrics (YouTube, Instagram, etc.)
        self.external_calls = 0
        self.external_errors = 0
        self.external_total_latency = 0.0
        self.external_platform_usage: dict[str, int] = {}  # platform -> count
        self.external_last_calls: deque[dict[str, Any]] = deque(maxlen=20)  # Last 20 external calls

        # Endpoint stats
        self.endpoint_stats: dict[str, dict] = {}  # key: "METHOD PATH" -> {count, avg_ms, errors}

    def record_request(self, method: str, path: str, status_code: int, duration_ms: float):
        self.total_requests += 1
        self.status_codes[status_code] = self.status_codes.get(status_code, 0) + 1
        self.request_durations.append(duration_ms)

        # Track by endpoint
        key = f"{method} {path}"
        if key not in self.endpoint_stats:
            self.endpoint_stats[key] = {"count": 0, "total_ms": 0.0, "errors": 0}

        self.endpoint_stats[key]["count"] += 1
        self.endpoint_stats[key]["total_ms"] += duration_ms
        if status_code >= 400:
            self.endpoint_stats[key]["errors"] += 1

        self.last_requests.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "method": method,
                "path": path,
                "status_code": status_code,
                "duration_ms": round(duration_ms, 2),
            }
        )

    def record_ai_call(self, model: str, duration_ms: float, success: bool = True):
        self.ai_calls += 1
        if not success:
            self.ai_errors += 1
        self.ai_total_latency += duration_ms
        self.ai_model_usage[model] = self.ai_model_usage.get(model, 0) + 1
        self.ai_last_calls.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model": model,
                "duration_ms": round(duration_ms, 2),
                "success": success,
            }
        )

    def record_external_call(self, platform: str, duration_ms: float, success: bool = True):
        self.external_calls += 1
        if not success:
            self.external_errors += 1
        self.external_total_latency += duration_ms
        self.external_platform_usage[platform] = self.external_platform_usage.get(platform, 0) + 1
        self.external_last_calls.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "platform": platform,
                "duration_ms": round(duration_ms, 2),
                "success": success,
            }
        )

    def record_task_run(self, task_name: str, status: str = "success"):
        if task_name in self.tasks:
            self.tasks[task_name]["last_run"] = datetime.now(timezone.utc).isoformat()
            self.tasks[task_name]["last_status"] = status
            self.tasks[task_name]["count"] += 1

    async def persist_snapshot(self, db: Any):
        summary = self.get_summary()
        await db.metrics_snapshots.insert_one({**summary, "timestamp": datetime.now(timezone.utc)})

    def get_summary(self) -> dict[str, Any]:

        avg_latency = sum(self.request_durations) / len(self.request_durations) if self.request_durations else 0

        return {
            "server_time": datetime.now(timezone.utc).isoformat(),
            "requests": {
                "total": self.total_requests,
                "avg_latency_ms": round(avg_latency, 2),
                "status_codes": self.status_codes,
                "recent": list(self.last_requests),
            },
            "ai": {
                "total_calls": self.ai_calls,
                "error_rate": round(self.ai_errors / self.ai_calls * 100, 1) if self.ai_calls else 0,
                "avg_latency_ms": round(self.ai_total_latency / self.ai_calls, 2) if self.ai_calls else 0,
                "model_usage": self.ai_model_usage,
                "recent": list(self.ai_last_calls),
            },
            "external_api": {
                "total_calls": self.external_calls,
                "error_rate": round(self.external_errors / self.external_calls * 100, 1) if self.external_calls else 0,
                "avg_latency_ms": round(self.external_total_latency / self.external_calls, 2)
                if self.external_calls
                else 0,
                "platform_usage": self.external_platform_usage,
                "recent": list(self.external_last_calls),
            },
            "background_tasks": self.tasks,
            "endpoints": [
                {
                    "endpoint": k,
                    "calls": v["count"],
                    "avg_ms": round(v["total_ms"] / v["count"], 2),
                    "errors": v["errors"],
                }
                for k, v in sorted(self.endpoint_stats.items(), key=lambda x: x[1]["count"], reverse=True)
            ],
        }


metrics_service = MetricsService()
