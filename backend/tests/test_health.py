from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError


def test_health_ok(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "rematch-backend"
    assert "version" in body


def test_platform_health_probes_are_ready_without_auth(client: TestClient) -> None:
    expected = {
        "/health/startup": "startup",
        "/health/live": "liveness",
        "/health/ready": "readiness",
    }

    for path, check in expected.items():
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "check": check}


def test_readiness_fails_when_database_is_unavailable(
    client: TestClient, monkeypatch
) -> None:
    class BrokenEngine:
        def connect(self):
            raise SQLAlchemyError("database details must not be returned")

    monkeypatch.setattr("app.api.routes.health.get_engine", lambda: BrokenEngine())

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "check": "readiness"}
    assert "database details" not in response.text
    assert client.get("/health/live").status_code == 200
