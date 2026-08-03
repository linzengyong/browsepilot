from fastapi.testclient import TestClient

from browsepilot.app.config import Settings
from browsepilot.app.main import create_app


def test_settings_defaults():
    s = Settings(_env_file=None)
    assert s.default_model == "deepseek/deepseek-chat"
    assert s.navigation_policy == "allow_https"
    assert s.allowed_domains == []
    assert s.max_steps == 30


def test_health_endpoint():
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == "2.0.0"


def test_health_reflects_settings():
    client = TestClient(create_app(Settings(_env_file=None, default_model="test/model")))
    assert client.get("/health").json()["model"] == "test/model"
