import json
import time
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.logger import get_logger
from app.services.metrics import metrics_service

logger = get_logger("http")


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Generate a unique request ID
        request_id = str(uuid.uuid4())[:8]
        start_time = time.time()

        # Basic metadata
        method = request.method
        path = request.url.path
        query = str(request.query_params)

        # Metadata for exclusion (don't track system/internal monitoring in business metrics)
        # Include both legacy /api/errors and /api/v1/errors (analyzer uses v1).
        is_meta_endpoint = any(
            ex in path
            for ex in [
                "/observability/metrics",
                "/dashboard",
                "/logs",
                "/health",
                "/api/schema",
                "/favicon.ico",
                "/robots.txt",
                "/docs",
                "/redoc",
                "/api/errors",
                "/api/v1/errors",
            ]
        )

        # Don't log large binary uploads or logs/stream
        if "/logs/stream" in path or "/upload" in path or "/create" in path or "/dashboard" in path:
            # Just basic info for heavy endpoints
            response = await call_next(request)
            duration = (time.time() - start_time) * 1000
            status_code = response.status_code
            # Record high-level metrics for performance monitoring (unless it's a meta-endpoint)
            if not is_meta_endpoint:
                logger.info(f"[REQUEST] {method} {path} | {status_code} | {duration:.2f}ms")
                metrics_service.record_request(method, path, status_code, duration)
            return response

        # Capture all headers as-is
        headers = dict(request.headers)

        try:
            response = await call_next(request)
        except Exception as e:
            # Log the crash to the Error Queue (only for non-meta endpoints)
            if not is_meta_endpoint:
                try:
                    from app.database import get_db
                    from app.services.errors import get_error_service

                    db = get_db()
                    error_service = get_error_service(db)
                    
                    # Normalize path to group crashes by endpoint (remove numeric/UUID IDs)
                    import re
                    normalized_path = re.sub(r"\/[0-9a-f-]{8,}", "/{id}", path) # UUIDs
                    normalized_path = re.sub(r"\/[0-9]+", "/{id}", normalized_path) # Numeric IDs
                    
                    await error_service.log_error(
                        feature=f"API: {method} {normalized_path}",
                        message=f"Unhandled exception: {str(e)}",
                        exception=e,
                        context={
                            "method": method,
                            "path": path,
                            "request_id": request_id,
                            "query": query,
                        },
                    )
                except Exception as log_err:
                    logger.error(f"Failed to log API crash to Error Queue: {log_err}")

            # Log the crash for console/journalctl
            duration = (time.time() - start_time) * 1000
            error_data = {
                "id": request_id,
                "method": method,
                "path": path,
                "status": 500,
                "duration_ms": f"{duration:.2f}",
                "error": str(e),
                "headers": headers,
                "query": query,
            }
            if not is_meta_endpoint:
                logger.error(f"REQUEST_BOX: {json.dumps(error_data)}")
                metrics_service.record_request(method, path, 500, duration)
            raise e

        duration = (time.time() - start_time) * 1000
        status_code = response.status_code

        # Build boxed log data
        log_data = {
            "id": request_id,
            "method": method,
            "path": path,
            "status": status_code,
            "duration_ms": f"{duration:.2f}",
            "query": query,
            "headers": headers,
        }

        # Log specialized "box" format for UI (unless it's a meta-endpoint)
        if not is_meta_endpoint:
            logger.info(f"REQUEST_BOX: {json.dumps(log_data)}")
            # Record the standard metrics
            metrics_service.record_request(method, path, status_code, duration)

        return response
