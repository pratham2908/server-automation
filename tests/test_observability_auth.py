"""The AI Costs tab was empty for everyone because its endpoints only accepted ``?api_key=``.

The Analyzer sends the key as an ``x-api-key`` header like it does everywhere
else, so every request to the observability routes came back 422 and the tab had
nothing to show. The HTML dashboards authenticate by query because a browser
navigation cannot set headers. Both have to keep working.
"""

from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import GLOBAL_API_KEY

client = TestClient(app)


def test_the_header_the_analyzer_sends_is_accepted():
    response = client.get("/dashboard", headers={"x-api-key": GLOBAL_API_KEY})
    assert response.status_code == 200, response.text[:300]


def test_the_query_key_the_html_pages_use_still_works():
    assert client.get("/dashboard", params={"api_key": GLOBAL_API_KEY}).status_code == 200


def test_no_key_is_refused_with_401_not_a_validation_error():
    assert client.get("/dashboard").status_code == 401


def test_a_wrong_key_is_refused_in_either_form():
    assert client.get("/dashboard", headers={"x-api-key": "nope"}).status_code == 401
    assert client.get("/dashboard", params={"api_key": "nope"}).status_code == 401


def test_the_cost_endpoints_take_the_header_too():
    """They share the dependency with the dashboard; a 422 here is the exact old failure."""
    for path in ("/api/v1/observability/ai-costs/summary", "/api/v1/observability/ai-calls"):
        response = client.get(path, headers={"x-api-key": "nope"})
        assert response.status_code == 401, f"{path} -> {response.status_code} (422 means it still wants ?api_key=)"
