from __future__ import annotations

from starlette.requests import Request

from app.middleware import _loggable_headers


def _request(headers: dict[str, str]) -> Request:
    return Request({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers.items()]})


def test_credentials_are_redacted_and_other_headers_kept() -> None:
    logged = _loggable_headers(_request({"authorization": "Bearer abc", "x-api-key": "k", "user-agent": "curl"}))
    assert logged == {"authorization": "[redacted]", "x-api-key": "[redacted]", "user-agent": "curl"}
