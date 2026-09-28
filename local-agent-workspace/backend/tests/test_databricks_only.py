import json

import httpx
from fastapi.testclient import TestClient

from local_agent.api import create_app
from local_agent.config import Settings


def test_legacy_settings_do_not_restore_removed_options(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    monkeypatch.setenv("LOCAL_AGENT_RUNTIME", "claude")
    state = tmp_path / "state"
    state.mkdir()
    editable = {"workspace": str(tmp_path), "model": "test-model", "env_file": ""}
    obsolete = {"runtime": "claude", "claude_cli_path": "/old/cli",
                "claude_mcp_config": "/old/mcp.json", "claude_skills": True}
    (state / "settings.json").write_text(json.dumps({**editable, **obsolete}))
    settings = Settings(state)
    settings.credentials = lambda: ("https://gateway.example", "fake-token")
    expected = {**editable, "context_window": 131072, "max_output_tokens": 8192, "max_agent_steps": 32,
                "compaction_handoffs": True}
    assert settings.values == expected
    with TestClient(create_app(settings)) as client:
        bootstrap = client.get("/api/bootstrap").json()
        assert set(bootstrap["settings"]) == {*expected, "host", "configured"}
        headers = {"X-Local-Token": bootstrap["token"]}
        # Old clients may still send these keys, but they cannot enable a mode.
        response = client.put("/api/settings", headers=headers, json={**editable, **obsolete})
        assert response.status_code == 200
        assert json.loads((state / "settings.json").read_text()) == expected
        assert client.put("/api/settings", headers=headers, json=editable).status_code == 200
        session = client.post("/api/sessions", headers=headers).json()
        assert "runtime" not in session
        assert "sdk_id" not in session
        assert session["events"] == []


def test_discovery_uses_databricks_serving_endpoints(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "fake-token")
    requested = []

    async def gateway(request):
        requested.append(str(request.url))
        return httpx.Response(200, json={"endpoints": [
            {"name": "test-chat"}, {"name": "test-embedding"}]})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(gateway)))
    with TestClient(create_app(settings)) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        response = client.get("/api/connection", headers=headers)
        assert response.json() == {"connected": True, "models": ["test-chat"], "error": None}
    assert requested == ["https://gateway.example/api/2.0/serving-endpoints"]
