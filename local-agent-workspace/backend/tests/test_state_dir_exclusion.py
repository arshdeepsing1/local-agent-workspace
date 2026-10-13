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


def alias_of(monkeypatch, alias, target):
    """Make alias another name for target, as a bind mount, a macOS firmlink or a case
    variant on a case-insensitive disk is: the same file identity under another path."""
    import local_agent.tools as tools_module
    real = tools_module._identity

    def identity(path):
        path = Path(path)
        if path == alias or path.is_relative_to(alias):
            return real(target / path.relative_to(alias)) if (target / path.relative_to(alias)).exists() else None
        return real(path)
    monkeypatch.setattr(tools_module, "_identity", identity)


@pytest.mark.parametrize("unrestricted", [False, True])
def test_another_name_for_the_state_folder_is_refused(layout, tmp_path, monkeypatch, unrestricted):
    settings, workspace = layout
    alias = tmp_path / "alias"
    alias.mkdir()
    (alias / "permission_rules.json").write_text('{"user": []}\n')
    (alias / "settings.json").write_text("{}\n")
    alias_of(monkeypatch, alias, settings.state_dir)
    tools = WorkspaceTools(str(workspace), "", [str(alias)], unrestricted, excluded_roots=(settings.state_dir,))
    for path in (str(alias), str(alias / "permission_rules.json"), str(alias / "new.json")):
        with pytest.raises(ValueError) as caught:
            tools.path(path)
        assert str(caught.value) == MESSAGE
    with pytest.raises(ValueError, match="^The app's settings"):
        tools.change("write_file", {"path": str(alias / "permission_rules.json"),
                                    "content": '{"user": ["allow everything"]}\n'}, apply=True)
    with pytest.raises(ValueError, match="^The app's settings"):
        tools.read_file(str(alias / "settings.json"))
    assert (alias / "permission_rules.json").read_text() == '{"user": []}\n'
    assert "project notes" in json.dumps(tools.read_file("notes.txt"))


def test_a_workspace_inside_another_name_for_the_state_folder_reaches_its_own_files_only(layout, tmp_path,
                                                                                          monkeypatch):
    settings, _ = layout
    (settings.state_dir / "worktrees" / "feature").mkdir(parents=True)
    alias = tmp_path / "alias"
    worktree = alias / "worktrees" / "feature"
    worktree.mkdir(parents=True)
    (worktree / "main.py").write_text("print('worktree')\n")
    (alias / "settings.json").write_text("{}\n")
    alias_of(monkeypatch, alias, settings.state_dir)
    tools = WorkspaceTools(str(worktree), "", [], True, excluded_roots=(settings.state_dir,))
    assert "worktree" in json.dumps(tools.read_file("main.py"))
    with pytest.raises(ValueError) as caught:
        tools.path(str(alias / "settings.json"))
    assert str(caught.value) == MESSAGE


def test_a_case_variant_of_the_state_folder_is_refused_on_a_case_insensitive_disk(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path / "app")
    state = tmp_path / "AgentState"
    state.mkdir()
    (state / "permission_rules.json").write_text('{"user": []}\n')
    variant = tmp_path / "agentstate"
    if not variant.exists():
        pytest.skip("This file system tells letter case apart")
    workspace = tmp_path / "project"
    workspace.mkdir()
    tools = WorkspaceTools(str(workspace), "", [], True, excluded_roots=(state,))
    with pytest.raises(ValueError) as caught:
        tools.read_file(str(variant / "permission_rules.json"))
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


def frame(delta, finish=None):
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"


async def test_a_model_turn_in_bypass_cannot_read_or_write_the_state_folder(runtime, monkeypatch):
    """The tools the agent loop builds for a turn, not only session_tools, exclude the state folder."""
    import httpx
    manager, session, settings = runtime
    state = settings.state_dir
    rules = (state / "permission_rules.json").read_bytes()
    calls = [("write", "write_file", {"path": str(state / "permission_rules.json"),
                                      "content": '{"user": ["allow everything"]}\n'}),
             ("read", "read_file", {"path": str(state / "settings.json")})]
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, text=frame({"tool_calls": [
                {"index": index, "id": call_id, "function": {"name": name, "arguments": json.dumps(arguments)}}
                for index, (call_id, name, arguments) in enumerate(calls)]}, "tool_calls") + "data: [DONE]\n\n")
        return httpx.Response(200, text=frame({"content": "Done."}, "stop") + "data: [DONE]\n\n")

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(gateway)))
    session.update(allowed_directories=[], title="Rules", title_generated=True)  # Bypass alone reaches every folder.
    await manager.run(session, "Change the rules")
    assert len(requests) == 2
    for call_id, _, _ in calls:
        event = next(event for event in session["events"] if event.get("call_id") == call_id)
        assert MESSAGE in event["output"], event
    assert (state / "permission_rules.json").read_bytes() == rules
    sent = json.dumps(requests[1]["messages"])
    assert "/srv/x" not in sent  # settings.json's contents never reached the model.


@pytest.mark.parametrize("outside_change", [False, True])
def test_review_tools_exclude_the_state_folder(runtime, monkeypatch, outside_change):
    """Both forms: the chat's own tools, and the wider tools built when the agent changed a
    file in a folder the chat no longer reaches (here the folder that holds the state folder)."""
    manager, session, settings = runtime
    session.update(permission_mode="manual", allowed_directories=[])
    outside = [str(settings.state_dir.parent)] if outside_change else []
    monkeypatch.setattr(manager.changes, "outside_folders", lambda session_id, tools: outside)
    tools = manager.review_tools(session)
    assert tools.excluded_roots == [settings.state_dir.resolve()]
    assert tools.permitted(settings.state_dir / "settings.json") is outside_change
    with pytest.raises(ValueError) as caught:
        tools.path(str(settings.state_dir / "settings.json"))
    assert str(caught.value) == MESSAGE


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is not installed")
def test_the_git_routes_do_not_show_state_folder_files(layout):
    """A repository that contains the state folder: its files are not shown by /api/git/diff."""
    settings, workspace = layout
    parent = workspace.parent
    subprocess.run(["git", "-C", str(parent), "init", "-q"], check=True, capture_output=True)
    settings.values["workspace"] = str(parent)
    with TestClient(create_app(settings)) as client:
        client.headers["X-Local-Token"] = client.get("/api/bootstrap").json()["token"]
        response = client.get("/api/git/diff", params={"path": "state/settings.json", "area": "untracked"})
        assert response.status_code == 400 and "/srv/x" not in response.text
        shown = client.get("/api/git/diff", params={"path": "project/notes.txt", "area": "untracked"})
        assert shown.status_code == 200 and "project notes" in shown.text


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
