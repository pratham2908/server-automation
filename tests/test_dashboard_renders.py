"""The dashboard page must actually render.

It 500'd in production for months: a refactor deleted
``MetricsService.cleanse_legacy_metrics`` but left the route calling it, and
nothing exercised the page itself — only the JSON endpoints it fetches. An
AttributeError in a route body is invisible to every check that stops at import.

These tests hit the HTML routes the way a browser does.
"""

from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import GLOBAL_API_KEY

client = TestClient(app)

# The HTML pages, and the query parameter each one authenticates with.
HTML_PAGES = ["/dashboard", "/system"]


def test_dashboard_renders_with_a_valid_key():
    response = client.get("/dashboard", params={"api_key": GLOBAL_API_KEY})
    assert response.status_code == 200, response.text[:500]
    assert "<!DOCTYPE html>" in response.text


def test_dashboard_rejects_a_bad_key():
    assert client.get("/dashboard", params={"api_key": "nope"}).status_code == 401


def test_every_html_page_renders():
    """Guards the whole family, so the next page added is covered too."""
    for path in HTML_PAGES:
        response = client.get(path, params={"api_key": GLOBAL_API_KEY})
        assert response.status_code == 200, f"{path} → {response.status_code}: {response.text[:300]}"
        assert "<!DOCTYPE html>" in response.text, f"{path} did not return an HTML document"
