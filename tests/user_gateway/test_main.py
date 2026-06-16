from __future__ import annotations

from fastapi.testclient import TestClient

from services.user_gateway.app import main as gateway_main


def test_user_gateway_health_endpoints(tmp_path, monkeypatch) -> None:
    db_path = str(tmp_path / "user_gateway.db")
    monkeypatch.setattr(gateway_main.auth_session_repo, "db_path", db_path)
    monkeypatch.setattr(gateway_main.operator_auth_repo, "db_path", db_path)

    with TestClient(gateway_main.app) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        startup = client.get("/health/startup")

    assert live.status_code == 200
    assert live.json()["service"] == "user_gateway"
    assert ready.status_code == 200
    assert ready.json()["service"] == "user_gateway"
    assert startup.status_code == 200
    assert startup.json()["service"] == "user_gateway"
