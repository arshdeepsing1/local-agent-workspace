"""Per-project settings (the Verify command) kept in app state, and their API."""
import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.projects import DEFAULTS, MAX_PROJECTS, ProjectSettings


def test_missing_or_corrupt_file_reads_as_defaults(tmp_path):
    projects = ProjectSettings(tmp_path)
    assert projects.get("/srv/app") == {"verify_command": "", "verify_timeout_seconds": 600, "verify_max_rounds": 2}
    (tmp_path / "projects.json").write_text("{not json")
    assert projects.get("/srv/app") == DEFAULTS
    (tmp_path / "projects.json").write_text(json.dumps({"version": 1, "projects": {"/srv/app": {
        "verify_command": "x\0y", "verify_timeout_seconds": 5}}}))
    assert projects.get("/srv/app") == DEFAULTS


def test_update_validates_strips_and_writes_a_private_file_atomically(tmp_path):
    projects = ProjectSettings(tmp_path)
    saved = projects.update("/srv/app", {"verify_command": "  pytest -q  ", "verify_max_rounds": 3})
    assert saved == {"verify_command": "pytest -q", "verify_timeout_seconds": 600, "verify_max_rounds": 3}
    assert projects.get("/srv/app") == saved and projects.get("/srv/other") == DEFAULTS
    path = tmp_path / "projects.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    data = json.loads(path.read_text())
    assert data["version"] == 1 and set(data["projects"]["/srv/app"]) == {*DEFAULTS, "updated"}
    assert [name for name in os.listdir(tmp_path) if name.endswith(".tmp")] == []
    # A partial update keeps the other values.
    assert projects.update("/srv/app", {"verify_timeout_seconds": 30})["verify_command"] == "pytest -q"


@pytest.mark.parametrize("values, message", [
    ({"verify_command": "x" * 2001}, "at most 2,000"),
    ({"verify_command": "pytest\0"}, "NUL"),
    ({"verify_command": 5}, "text"),
    ({"verify_timeout_seconds": 9}, "10 to 3,600"),
    ({"verify_timeout_seconds": 3601}, "10 to 3,600"),
    ({"verify_timeout_seconds": True}, "10 to 3,600"),
    ({"verify_max_rounds": 0}, "1 to 3"),
    ({"verify_max_rounds": 4}, "1 to 3"),
])
def test_invalid_values_are_refused(tmp_path, values, message):
    with pytest.raises(ValueError, match=message):
        ProjectSettings(tmp_path).update("/srv/app", values)
    assert not (tmp_path / "projects.json").exists()


def test_at_most_500_projects_are_kept_dropping_the_oldest(tmp_path, monkeypatch):
    projects = ProjectSettings(tmp_path)
    data = {"version": 1, "projects": {f"/srv/p{index}": {**DEFAULTS, "verify_command": "make", "updated": index}
                                       for index in range(MAX_PROJECTS)}}
    (tmp_path / "projects.json").write_text(json.dumps(data))
    projects.update("/srv/new", {"verify_command": "make test"})
    stored = json.loads((tmp_path / "projects.json").read_text())["projects"]
    assert len(stored) == MAX_PROJECTS and "/srv/p0" not in stored and "/srv/p1" in stored and "/srv/new" in stored


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    worktree = tmp_path / "worktree"
    project.mkdir()
    worktree.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    with TestClient(create_app(settings)) as client:
        client.headers["X-Local-Token"] = client.get("/api/bootstrap").json()["token"]
        yield client, settings, project, worktree


def test_project_settings_api_round_trip_per_workspace(api):
    client, settings, project, worktree = api
    manager = client.app.state.manager
    first = manager.store.create(settings.values)
    second = manager.store.create({**settings.values, "workspace": str(worktree)})
    query = f"/api/project-settings?session_id={first['id']}"
    assert client.get(query).json() == {"workspace": str(project.resolve()), **DEFAULTS}
    saved = client.put(query, json={"verify_command": "pytest -q", "verify_timeout_seconds": 120, "verify_max_rounds": 1})
    assert saved.status_code == 200
    assert saved.json() == {"workspace": str(project.resolve()), "verify_command": "pytest -q",
                            "verify_timeout_seconds": 120, "verify_max_rounds": 1}
    assert client.get(query).json()["verify_command"] == "pytest -q"
    # The default workspace (no conversation) is the same project; a worktree is another one.
    assert client.get("/api/project-settings").json()["verify_command"] == "pytest -q"
    assert client.get(f"/api/project-settings?session_id={second['id']}").json()["verify_command"] == ""
    # Kept in app state, never in the repository or a conversation export.
    assert (settings.state_dir / "projects.json").exists()
    assert not any("pytest -q" in path.read_text(errors="ignore") for path in project.rglob("*") if path.is_file())
    exported = client.get(f"/api/sessions/{first['id']}/export")
    assert exported.status_code == 200 and "pytest -q" not in exported.text
    # Clearing the command turns Verify off.
    assert client.put(query, json={"verify_command": ""}).json()["verify_command"] == ""


@pytest.mark.parametrize("body", [
    {"verify_command": "pytest", "extra": 1},
    {"verify_timeout_seconds": "60"},
    {"verify_max_rounds": 1.0},
    {"verify_command": "x" * 2001},
])
def test_project_settings_api_is_strict(api, body):
    client, *_ = api
    assert client.put("/api/project-settings", json=body).status_code == 422


def test_project_settings_api_reports_validation_and_missing_conversations(api):
    client, *_ = api
    response = client.put("/api/project-settings", json={"verify_timeout_seconds": 5})
    assert response.status_code == 400 and "10 to 3,600" in response.json()["detail"]
    assert client.get("/api/project-settings?session_id=missing").status_code == 404
    assert client.put("/api/project-settings", headers={"X-Local-Token": "wrong"}, json={}).status_code == 403
