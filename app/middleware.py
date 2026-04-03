
import time
import json
import uuid
from fastapi import Request, Response
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
        
        # Metadata for exclusion
        is_meta_endpoint = "/observability/metrics" in path or "/dashboard" in path or "/logs/stream" in path or "/health" in path
        
        # Don't log large binary uploads or logs/stream
        if "/logs/stream" in path or "/upload" in path or "/create" in path or "/dashboard" in path:
             # Just basic info for heavy endpoints
             response = await call_next(request)
             duration = (time.time() - start_time) * 1000
             status_code = response.status_code
             logger.info(f"[REQUEST] {method} {path} | {status_code} | {duration:.2f}ms")
             # Record high-level metrics for performance monitoring (unless it's a meta-endpoint)
             if not is_meta_endpoint:
                 metrics_service.record_request(method, path, status_code, duration)
             return response

        # Capture all headers as-is
        headers = dict(request.headers)

        try:
            response = await call_next(request)
        except Exception as e:
            # Log the crash
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
            logger.error(f"REQUEST_BOX: {json.dumps(error_data)}")
            if not is_meta_endpoint:
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

        # Log specialized "box" format for UI
        logger.info(f"REQUEST_BOX: {json.dumps(log_data)}")
        
        # Record the standard metrics
        if not is_meta_endpoint:
            metrics_service.record_request(method, path, status_code, duration)
        
        return response
