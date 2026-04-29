from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_check():
    """Verify that the health check endpoint returns 200 OK."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_schema():
    """Verify that the API schema endpoint is functional."""
    response = client.get("/api/schema")
    assert response.status_code == 200
    data = response.json()
    assert "service" in data
    assert "endpoints" in data

