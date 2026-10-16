"""Importing .mcp.json and claude_desktop_config.json as disabled entries (mcp_import.py)."""
import json

import pytest
from fastapi.testclient import TestClient

from local_agent import mcp_import
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.extensions import validate_config

PROJECT = {"mcpServers": {
    "GitHub Server": {"command": "github-mcp-server", "args": ["stdio"],
                      "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_syntheticsyntheticsynthetic12", "LOG_LEVEL": "${LEVEL}",
                              "PATH": "/srv/other/bin"}, "cwd": "/srv/other"},
    "docs": {"type": "http", "url": "https://mcp.example.com/mcp",
             "headers": {"Authorization": "Bearer ${DOCS_TOKEN}", "X-Api-Key": "synthetic-plain-key-value"}},
    "legacy": {"type": "sse", "url": "https://mcp.example.com/sse"},
    "leaky": {"command": "server", "args": ["--token=ghp_syntheticsyntheticsynthetic12"]},
    "nothing": {"note": "no command"},
}}
DESKTOP = {"globalShortcut": "Ctrl+Space", "mcpServers": {
    "docs": {"command": "/srv/other/docs-server", "args": []},
    "123 tools!": {"command": "tools"},
}}


def test_both_formats_become_disabled_entries_with_references_and_no_values():
    preview = mcp_import.parse(PROJECT, existing_ids=["docs"])
    text = json.dumps(preview)
    assert "ghp_synthetic" not in text and "synthetic-plain-key-value" not in text and "/srv/other/bin" not in text
    github, docs = preview["entries"]
    assert github == {"id": "github-server", "enabled": False, "name": "GitHub Server", "transport": "stdio",
                      "command": "github-mcp-server", "args": ["stdio"],
                      "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": {"secret": "GITHUB_PERSONAL_ACCESS_TOKEN"},
                              "LOG_LEVEL": {"secret": "LEVEL"}}}
    assert docs["id"] == "docs-2" and docs["transport"] == "http" and docs["enabled"] is False
    assert docs["headers"] == {"Authorization": {"secret": "DOCS_TOKEN", "prefix": "Bearer "},
                               "X-Api-Key": {"secret": "X_API_KEY"}}
    assert preview["needs"] == ["GITHUB_PERSONAL_ACCESS_TOKEN", "LEVEL", "DOCS_TOKEN", "X_API_KEY"]
    assert {item["name"]: item["reason"] for item in preview["skipped"]}.keys() == {"legacy", "leaky", "nothing"}
    assert "SSE" in next(item["reason"] for item in preview["skipped"] if item["name"] == "legacy")
    assert preview["ignored"] == [{"name": "GitHub Server", "fields": ["env PATH", "cwd"]}]
    # Every entry is one validate_config accepts, so Save works once the form holds them.
    assert validate_config({"servers": preview["entries"]})["servers"] == preview["entries"]
    desktop = mcp_import.parse(DESKTOP)
    assert [entry["id"] for entry in desktop["entries"]] == ["docs", "mcp-123-tools"]
    assert all(entry["enabled"] is False for entry in desktop["entries"])


def test_ids_are_sanitized_and_deduplicated():
    taken = {"a" * 20}
    servers = {name: {"command": "x"} for name in ["A" * 30, "A" * 30 + " ", "!!!", "Ünïcode name"]}
    ids = [entry["id"] for entry in mcp_import.parse({"mcpServers": servers}, taken)["entries"]]
    assert ids == ["aaaaaaaaaaaaaaaaaa-2", "aaaaaaaaaaaaaaaaaa-3", "server", "n-code-name"]
    assert all(len(item) <= 20 for item in ids)


def test_text_that_is_not_a_configuration_is_refused():
    with pytest.raises(ValueError, match="Not valid JSON"):
        mcp_import.parse_text("{")
    with pytest.raises(ValueError, match="mcpServers"):
        mcp_import.parse_text("[]")
    with pytest.raises(ValueError, match="No mcpServers"):
        mcp_import.parse_text('{"other": 1}')
    with pytest.raises(ValueError, match="64 KB"):
        mcp_import.parse_text(" " * (64 * 1024 + 1))
    # A value holding the workspace token is never echoed.
    preview = mcp_import.parse({"mcpServers": {"x": {"command": "run", "args": ["synthetic-workspace-token"]}}},
                               tokens=["synthetic-workspace-token"])
    assert preview["entries"] == [] and "synthetic-workspace-token" not in json.dumps(preview)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(workspace), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "synthetic-token")
    app = create_app(settings)
    with TestClient(app) as client:
        client.headers["X-Local-Token"] = client.get("/api/bootstrap").json()["token"]
        yield client, workspace, tmp_path / "home", app.state.manager


def test_import_routes_preview_without_saving(client):
    client, workspace, home, manager = client
    sid = client.post("/api/sessions").json()["id"]
    assert client.post("/api/extensions/import", params={"session_id": sid}, json={"source": "project"}).status_code == 404
    (workspace / ".mcp.json").write_text(json.dumps(PROJECT))
    preview = client.post("/api/extensions/import", params={"session_id": sid}, json={"source": "project"}).json()
    assert preview["source"] == "project" and preview["path"] == ".mcp.json"
    assert [entry["id"] for entry in preview["entries"]] == ["github-server", "docs"]
    assert "ghp_synthetic" not in json.dumps(preview)
    assert manager.extensions.public_config() == {"servers": [], "hooks": []}  # Nothing saved or started.
    desktop = mcp_import.desktop_config_path()
    desktop.parent.mkdir(parents=True)
    desktop.write_text(json.dumps(DESKTOP))
    assert [entry["name"] for entry in client.post("/api/extensions/import", json={"source": "desktop"}).json()["entries"]] == [
        "docs", "123 tools!"]
    pasted = client.post("/api/extensions/import", json={"source": "text", "text": json.dumps(DESKTOP)}).json()
    assert pasted["source"] == "text" and len(pasted["entries"]) == 2
    assert client.post("/api/extensions/import", json={"source": "text", "text": "{"}).status_code == 400
    # Saved entries are disabled: the server never starts until the user enables it.
    saved = client.put("/api/extensions", json={"servers": preview["entries"], "hooks": []}).json()
    assert all(server["enabled"] is False for server in saved["servers"])


def test_symbolic_links_large_and_credential_files_are_refused(client, tmp_path):
    client, workspace, home, manager = client
    sid = client.post("/api/sessions").json()["id"]
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(PROJECT))
    (workspace / ".mcp.json").symlink_to(outside)
    refused = client.post("/api/extensions/import", params={"session_id": sid}, json={"source": "project"})
    assert refused.status_code == 400 and "symbolic link" in refused.json()["detail"]
    (workspace / ".mcp.json").unlink()
    (workspace / ".mcp.json").write_text(" " * (64 * 1024 + 1))
    assert "larger than 64 KB" in client.post("/api/extensions/import", params={"session_id": sid},
                                              json={"source": "project"}).json()["detail"]
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps(DESKTOP))
    with pytest.raises(ValueError, match="never imported"):
        mcp_import.read_config_file(credentials, str(credentials))
    (workspace / ".env").mkdir()
    with pytest.raises(ValueError, match="Credential"):
        manager.session_tools(manager.get(sid)).path(".env/.mcp.json")
