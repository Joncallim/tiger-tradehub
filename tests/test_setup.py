import json

from dotenv import dotenv_values
from fastapi.testclient import TestClient

from tradehub.app import app, clear_runtime_caches
from tradehub.config import MIN_API_TOKEN_LENGTH


def client():
    return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))


def test_setup_env_generates_token_and_health_can_use_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clear_runtime_caches()

    try:
        response = client().post("/setup/env", json={})
        values = dotenv_values(tmp_path / ".env")
        token = values["TRADEHUB_API_TOKEN"]
        health = client().get("/health", headers={"Authorization": f"Bearer {token}"})
    finally:
        clear_runtime_caches()

    assert response.status_code == 200
    assert response.json()["generated_api_token"] is True
    assert token is not None
    assert len(token) >= MIN_API_TOKEN_LENGTH
    assert values["TRADEHUB_DRY_RUN"] == "true"
    assert health.status_code == 200
    assert health.json()["dry_run"] is True


def test_setup_status_masks_existing_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    token = "existing-token-with-enough-length"
    (tmp_path / ".env").write_text(f"TRADEHUB_API_TOKEN={token}\n", encoding="utf-8")
    clear_runtime_caches()

    try:
        response = client().get("/setup/status")
    finally:
        clear_runtime_caches()

    assert response.status_code == 200
    body = response.json()
    assert body["setup_complete"] is True
    assert body["api_token_preview"] == "exis...ngth"
    assert token not in response.text


def test_setup_writes_mcp_config_without_losing_existing_servers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "claude_desktop_config.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"other": {"command": "other-mcp"}}}),
        encoding="utf-8",
    )
    clear_runtime_caches()

    try:
        env_response = client().post(
            "/setup/env",
            json={"TRADEHUB_API_TOKEN": "custom-token-with-enough-length"},
        )
        mcp_response = client().post(
            "/setup/mcp-config",
            json={"config_path": str(config_path), "command": "/tmp/tradehub-mcp"},
        )
    finally:
        clear_runtime_caches()

    config = json.loads(config_path.read_text(encoding="utf-8"))
    tradehub = config["mcpServers"]["tiger-tradehub"]

    assert env_response.status_code == 200
    assert mcp_response.status_code == 200
    assert mcp_response.json()["backup_path"] == str(config_path.resolve()) + ".bak"
    assert (tmp_path / "claude_desktop_config.json.bak").exists()
    assert config["mcpServers"]["other"] == {"command": "other-mcp"}
    assert tradehub["command"] == "/tmp/tradehub-mcp"
    assert tradehub["env"] == {
        "TRADEHUB_BASE_URL": "http://127.0.0.1:8787",
        "TRADEHUB_API_TOKEN": "custom-token-with-enough-length",
    }


def test_setup_routes_are_local_only():
    remote_client = TestClient(app, base_url="http://127.0.0.1", client=("192.0.2.10", 50000))

    response = remote_client.get("/setup/status")

    assert response.status_code == 403


def test_setup_routes_reject_non_local_host_header():
    response = TestClient(app, base_url="http://evil.example", client=("127.0.0.1", 50000)).get(
        "/setup/status"
    )

    assert response.status_code == 403


def test_setup_writes_reject_non_local_origin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clear_runtime_caches()

    try:
        response = client().post(
            "/setup/env",
            json={},
            headers={"Origin": "https://evil.example"},
        )
    finally:
        clear_runtime_caches()

    assert response.status_code == 403
    assert not (tmp_path / ".env").exists()


def test_setup_writes_require_json_content_type(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clear_runtime_caches()

    try:
        response = client().post(
            "/setup/env",
            content="TRADEHUB_API_TOKEN=bad",
            headers={"Content-Type": "text/plain"},
        )
    finally:
        clear_runtime_caches()

    assert response.status_code == 415
    assert not (tmp_path / ".env").exists()


def test_setup_page_sends_no_store_security_headers():
    response = client().get("/setup")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_protected_routes_report_configuration_error_without_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clear_runtime_caches()

    try:
        response = client().get("/health", headers={"Authorization": "Bearer missing-token"})
    finally:
        clear_runtime_caches()

    assert response.status_code == 503
    assert response.json()["detail"]["message"] == (
        "TradeHub is not configured; open /setup on this machine"
    )
