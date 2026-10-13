"""The app's state folder (settings, rules, extensions, conversations) is excluded from file
tools: in every mode, after a folder grant, through a symbolic link, and in listings."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.store import Store
from local_agent.tools import EXCLUDED_ROOT, WorkspaceTools

MESSAGE = "The app's settings and conversation folder is excluded from file tools."


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """A workspace, and a state folder outside it (as with LOCAL_AGENT_STATE_DIR)."""
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path / "app")
    workspace = tmp_path / "work" / "project"
    workspace.mkdir(parents=True)
    (workspace / "notes.txt").write_text("project notes\n")
    settings = Settings(tmp_path / "work" / "state")
    settings.env = {}
    settings.values.update(workspace=str(workspace), env_file="")
    settings.credentials = lambda: ("https://fake.example", "synthetic-secret-token")
    (settings.state_dir / "settings.json").write_text('{"workspace": "/srv/x"}\n')
    (settings.state_dir / "permission_rules.json").write_text('{"user": []}\n')
    return settings, workspace


def test_message_constant():
    assert EXCLUDED_ROOT == MESSAGE


@pytest.mark.parametrize("unrestricted", [False, True])
def test_a_granted_state_folder_is_refused(layout, unrestricted):
    settings, workspace = layout
    state = settings.state_dir
    tools = WorkspaceTools(str(workspace), "", [str(state)], unrestricted, excluded_roots=(state,))
    for path in (str(state), str(state / "settings.json"), str(state / "permission_rules.json"),
                 str(state / "new.txt"), "../state/settings.json"):
        with pytest.raises(ValueError) as caught:
            tools.path(path)
        assert str(caught.value) == MESSAGE
    with pytest.raises(ValueError, match="^The app's settings"):
        tools.read_file(str(state / "settings.json"))
    with pytest.raises(ValueError, match="^The app's settings"):
        tools.list_files(str(state))
    assert "project notes" in json.dumps(tools.read_file("notes.txt"))


def test_a_parent_folder_grant_lists_and_searches_without_the_state_folder(layout):
    settings, workspace = layout
    parent = workspace.parent  # Holds the project and the state folder.
    tools = WorkspaceTools(str(workspace), "", [str(parent)], excluded_roots=(settings.state_dir,))
    names = [entry["name"] for entry in tools.list_files(str(parent), depth=3)]
    assert "project" in names and "notes.txt" in names
    assert "state" not in names and "settings.json" not in names
    found = json.dumps(tools.find_files("**/*.json", str(parent)))
    assert "settings.json" not in found and "permission_rules.json" not in found
    searched = json.dumps(tools.search_files("user", path=str(parent)))
    assert "permission_rules" not in searched


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is not installed")
def test_git_listed_files_in_the_state_folder_are_left_out(layout):
    """A repository that contains the state folder lists its files as untracked."""
    settings, workspace = layout
    parent = workspace.parent
    subprocess.run(["git", "-C", str(parent), "init", "-q"], check=True, capture_output=True)
    tools = WorkspaceTools(str(parent), "", excluded_roots=(settings.state_dir,))
    found = json.dumps(tools.find_files("**/*", "."))
    assert "notes.txt" in found
    assert "settings.json" not in found and "permission_rules.json" not in found
    searched = json.dumps(tools.search_files("user", path="."))
    assert "permission_rules" not in searched


def test_a_symbolic_link_into_the_state_folder_is_refused(layout):
    settings, workspace = layout
    (workspace / "link").symlink_to(settings.state_dir)
    (workspace / "settings-link.json").symlink_to(settings.state_dir / "settings.json")
    tools = WorkspaceTools(str(workspace), "", excluded_roots=(settings.state_dir,))
    for path in ("link/settings.json", "settings-link.json", "link"):
        with pytest.raises(ValueError) as caught:
            tools.path(path)
        assert str(caught.value) == MESSAGE
    names = [entry["name"] for entry in tools.list_files(".")]
    assert "link" not in names and "settings-link.json" not in names


def test_a_workspace_inside_the_state_folder_reaches_its_own_files_only(layout):
    settings, _ = layout
    worktree = settings.state_dir / "worktrees" / "feature"
    worktree.mkdir(parents=True)
    (worktree / "main.py").write_text("print('worktree')\n")
    tools = WorkspaceTools(str(worktree), "", [], True, excluded_roots=(settings.state_dir,))
    assert tools.path("main.py") == worktree / "main.py"
    assert "worktree" in json.dumps(tools.read_file("main.py"))
    tools.path("new.py")  # A new file in the worktree is allowed.
    assert [entry["name"] for entry in tools.list_files(".")] == ["main.py"]
    # The rest of the state folder stays out of reach, even with unrestricted access.
    for path in (str(settings.state_dir / "settings.json"), "../../settings.json"):
        with pytest.raises(ValueError) as caught:
            tools.path(path)
        assert str(caught.value) == MESSAGE


def test_the_default_state_folder_stays_excluded(tmp_path):
    app_root = tmp_path / "app"
    (app_root / ".local").mkdir(parents=True)
    (app_root / ".local" / "settings.json").write_text("{}")
    tools = WorkspaceTools(str(app_root), "", excluded_roots=(app_root / ".local",))
    with pytest.raises(ValueError):
        tools.path(".local/settings.json")
    tools = WorkspaceTools(str(app_root), "")
    with pytest.raises(ValueError, match="internal or generated directory is excluded"):
        tools.path(".local/settings.json")


@pytest.fixture
async def runtime(layout):
    settings, workspace = layout
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session["permission_mode"] = "bypassPermissions"
    session["allowed_directories"] = [str(settings.state_dir)]  # For example a grant made before this release.
    manager.live[session["id"]] = session
    yield manager, session, settings
    await manager.jobs.shutdown()
    store.db.close()


@pytest.mark.parametrize("name, arguments", [
    ("read_file", {"path": "STATE/settings.json"}),
    ("list_files", {"path": "STATE"}),
    ("write_file", {"path": "STATE/permission_rules.json", "content": '{"user": ["allow everything"]}\n'}),
    ("write_file", {"path": "STATE/extensions.json", "content": "{}\n"}),
    ("edit_file", {"path": "STATE/permission_rules.json", "old_text": "[]", "new_text": '["x"]'}),
    ("delete_path", {"path": "STATE/permission_rules.json"}),
    ("move_path", {"path": "notes.txt", "destination": "STATE/notes.txt"}),
    ("find_files", {"pattern": "*.json", "path": "STATE"}),
    ("search_files", {"query": "user", "path": "STATE"}),
])
async def test_agent_tools_cannot_reach_the_state_folder_in_bypass(runtime, name, arguments):
    manager, session, settings = runtime
    state = settings.state_dir
    before = {path.name: path.read_bytes() for path in state.iterdir() if path.is_file() and path.suffix == ".json"}
    arguments = {key: value.replace("STATE", str(state)) for key, value in arguments.items()}
    tools = manager.session_tools(session)
    output = await manager.execute_tool(session, tools, name, arguments, "call")
    assert MESSAGE in output
    after = {path.name: path.read_bytes() for path in state.iterdir() if path.is_file() and path.suffix == ".json"}
    assert after == before
    assert not (state / "notes.txt").exists()
    assert (Path(session["workspace"]) / "notes.txt").exists()


def test_the_api_refuses_the_state_folder(layout):
    settings, workspace = layout
    with TestClient(create_app(settings)) as client:
        client.headers["X-Local-Token"] = client.get("/api/bootstrap").json()["token"]
        session = client.post("/api/sessions", json={}).json()
        response = client.post(f"/api/sessions/{session['id']}/folders", json={"path": str(settings.state_dir)})
        assert response.status_code == 400 and response.json()["detail"] == MESSAGE
        check = client.post("/api/folder-access/check", json={"path": str(settings.state_dir)}).json()
        assert check["accessible"] is False and check["error"] == MESSAGE
        assert client.get("/api/file", params={"path": str(settings.state_dir / "settings.json")}).status_code == 400
        assert client.get("/api/files", params={"path": str(settings.state_dir)}).status_code == 400
        assert client.get("/api/file", params={"path": "notes.txt"}).json()["content"]
