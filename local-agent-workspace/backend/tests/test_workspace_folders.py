"""Folders added to Workspace -> Files: storage, validation, UI-only file access, and agent isolation."""
import json
import os
import stat

import httpx
import pytest
from fastapi.testclient import TestClient

from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.store import Store
from local_agent.workspace_folders import FILE_NAME, MAX_FOLDERS, WorkspaceFolders

ROUTE = "/api/workspace-folders"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('project')\n")
    other = tmp_path / "other"
    (other / "sub").mkdir(parents=True)
    (other / "notes.txt").write_text("outside notes\n")
    (other / "sub" / "deep.txt").write_text("deep\n")
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    return settings, project, other


class Client:
    def __init__(self, client):
        self.client = client
        self.headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}

    def get(self, route, **params):
        return self.client.get(route, params=params, headers=self.headers)

    def post(self, route, body=None, **params):
        return self.client.post(route, params=params, json=body, headers=self.headers)

    def put(self, route, body, **params):
        return self.client.put(route, params=params, json=body, headers=self.headers)

    def delete(self, route, body, **params):
        return self.client.request("DELETE", route, params=params, json=body, headers=self.headers)

    def session(self, **body):
        return self.post("/api/sessions", body or None).json()["id"]


def app_client(settings):
    return TestClient(create_app(settings))


def paths(response):
    assert response.status_code == 200, response.text
    return [item["path"] for item in response.json()["folders"]]


def test_add_list_remove_round_trip_persists_across_app_instances(setup, tmp_path):
    settings, project, other = setup
    second = tmp_path / "second"
    second.mkdir()
    with app_client(settings) as raw:
        client = Client(raw)
        sid = client.session()
        empty = client.get(ROUTE, session_id=sid).json()
        assert empty == {"workspace": str(project), "folders": []}
        added = client.post(ROUTE, {"path": str(other)}, session_id=sid)
        assert added.json() == {"workspace": str(project), "folders": [{"path": str(other), "missing": False}],
                                "added": str(other)}
        assert paths(client.post(ROUTE, {"path": str(second)}, session_id=sid)) == [str(other), str(second)]
        # Adding the same folder again (after resolving) is a no-op that returns the list.
        assert paths(client.post(ROUTE, {"path": str(other / "sub" / "..")}, session_id=sid)) == [str(other), str(second)]
        # Without a conversation, the project comes from the settings.
        assert paths(client.get(ROUTE)) == [str(other), str(second)]

    stored = settings.state_dir / FILE_NAME
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert json.loads(stored.read_text()) == {"version": 1, "projects": {str(project): [str(other), str(second)]}}
    assert not [name for name in os.listdir(settings.state_dir) if name.endswith(".tmp")]

    with app_client(settings) as raw:
        client = Client(raw)
        assert paths(client.get(ROUTE)) == [str(other), str(second)]
        assert paths(client.delete(ROUTE, {"path": str(other)})) == [str(second)]
        missing = client.delete(ROUTE, {"path": str(other)})
        assert missing.status_code == 404 and missing.json()["detail"] == "This folder is not in Files."
        assert paths(client.delete(ROUTE, {"path": str(second)})) == []
    assert json.loads(stored.read_text()) == {"version": 1, "projects": {}}


def test_body_is_strict(setup):
    settings, _, other = setup
    with app_client(settings) as raw:
        client = Client(raw)
        for body in ({"path": ""}, {"path": 5}, {"path": "x" * 4097}, {"path": str(other), "extra": 1}, {}):
            assert client.post(ROUTE, body).status_code == 422, body
        assert paths(client.get(ROUTE)) == []


def test_routes_need_the_local_token(setup):
    settings, _, other = setup
    with app_client(settings) as raw:
        assert raw.get(ROUTE).status_code == 403
        assert raw.post(ROUTE, json={"path": str(other)}).status_code == 403
        assert not (settings.state_dir / FILE_NAME).exists()


def test_refusals(setup, tmp_path):
    settings, project, other = setup
    (project / "inner").mkdir()
    (settings.state_dir / "nested").mkdir()
    cases = [
        (str(tmp_path / "nowhere"), "This folder does not exist"),
        (str(other / "notes.txt"), "This is not a folder"),
        ("/", "The filesystem root cannot be added to Files."),
        (str(project), "This folder is inside the project; it is already in Files."),
        (str(project / "inner"), "This folder is inside the project; it is already in Files."),
        ("inner", "This folder is inside the project; it is already in Files."),  # relative to the project
        (str(settings.state_dir), "state folder or inside it"),
        (str(settings.state_dir / "nested"), "state folder or inside it"),
        (str(tmp_path), "contains the app's state folder"),
    ]
    with app_client(settings) as raw:
        client = Client(raw)
        for path, message in cases:
            response = client.post(ROUTE, {"path": path})
            assert response.status_code == 400, path
            assert message in response.json()["detail"], (path, response.json())
        assert paths(client.get(ROUTE)) == []


def test_unlistable_folder_is_refused(setup, monkeypatch):
    settings, _, other = setup
    real_access = os.access
    monkeypatch.setattr("local_agent.workspace_folders.os.access",
                        lambda path, mode: False if str(path) == str(other) else real_access(path, mode))
    with app_client(settings) as raw:
        response = Client(raw).post(ROUTE, {"path": str(other)})
        assert response.status_code == 400
        assert "cannot list this folder" in response.json()["detail"]


def test_at_most_twenty_folders(setup, tmp_path):
    settings, _, _ = setup
    folders = []
    for index in range(MAX_FOLDERS + 1):
        folder = tmp_path / "many" / f"f{index:02d}"
        folder.mkdir(parents=True)
        folders.append(str(folder))
    with app_client(settings) as raw:
        client = Client(raw)
        for folder in folders[:MAX_FOLDERS]:
            assert client.post(ROUTE, {"path": folder}).status_code == 200
        refused = client.post(ROUTE, {"path": folders[-1]})
        assert refused.status_code == 400
        assert refused.json()["detail"] == "Files can show at most 20 added folders; remove one first."
        # Re-adding a listed folder is still a no-op, not a refusal.
        assert paths(client.post(ROUTE, {"path": folders[0]})) == folders[:MAX_FOLDERS]


def test_invalid_file_reads_as_empty_refuses_changes_and_is_kept(setup, caplog):
    settings, _, other = setup
    stored = settings.state_dir / FILE_NAME
    for content in (b"{not json", json.dumps({"version": 2, "projects": {}}).encode(),
                    json.dumps({"version": 1, "projects": {"/srv/x": "not a list"}}).encode()):
        stored.write_bytes(content)
        with app_client(settings) as raw:
            client = Client(raw)
            assert paths(client.get(ROUTE)) == []
            for response in (client.post(ROUTE, {"path": str(other)}), client.delete(ROUTE, {"path": str(other)})):
                assert response.status_code == 409
                assert response.json()["detail"] == ("The folder list file could not be read; "
                                                     "fix or remove workspace_folders.json in the state folder.")
            assert client.get("/api/files", path=str(other)).status_code == 400
        assert stored.read_bytes() == content
    warnings = [record for record in caplog.records if "could not be read" in record.getMessage()]
    assert len(warnings) == 3  # Once per app instance, not once per request.


def test_missing_entry_is_flagged_and_can_be_removed(setup):
    settings, _, other = setup
    with app_client(settings) as raw:
        client = Client(raw)
        assert paths(client.post(ROUTE, {"path": str(other)})) == [str(other)]
        (other / "sub" / "deep.txt").unlink()
        (other / "sub").rmdir()
        (other / "notes.txt").unlink()
        other.rmdir()
        listed = client.get(ROUTE).json()["folders"]
        assert listed == [{"path": str(other), "missing": True}]
        assert paths(client.get(ROUTE)) == [str(other)]  # Kept, not deleted on read.
        assert paths(client.delete(ROUTE, {"path": str(other)})) == []


def test_files_and_editor_work_inside_an_added_folder_only(setup, tmp_path):
    settings, _, other = setup
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    (stranger / "private.txt").write_text("private\n")
    with app_client(settings) as raw:
        client = Client(raw)
        sid = client.session()
        for params in ({"session_id": sid}, {}):
            assert client.get("/api/files", path=str(other), **params).status_code == 400
            assert client.get("/api/file", path=str(other / "notes.txt"), **params).status_code == 400
        client.post(ROUTE, {"path": str(other)}, session_id=sid)
        for params in ({"session_id": sid}, {}):
            listing = client.get("/api/files", path=str(other), **params)
            assert listing.status_code == 200, listing.text
            assert [entry["path"] for entry in listing.json()] == [str(other / "sub"), str(other / "notes.txt")]
            opened = client.get("/api/file", path=str(other / "notes.txt"), **params)
            assert opened.json()["content"] == "outside notes\n"
        saved = client.put("/api/file", {"path": str(other / "notes.txt"), "content": "edited\n",
                                         "original": "outside notes\n", "session_id": sid})
        assert saved.status_code == 200, saved.text
        assert (other / "notes.txt").read_text() == "edited\n"
        # A folder that is not added stays refused, for browsing, reading and saving.
        assert client.get("/api/files", path=str(stranger), session_id=sid).status_code == 400
        assert client.get("/api/file", path=str(stranger / "private.txt"), session_id=sid).status_code == 400
        refused = client.put("/api/file", {"path": str(stranger / "private.txt"), "content": "x\n",
                                           "original": "private\n", "session_id": sid})
        assert refused.status_code == 400
        assert (stranger / "private.txt").read_text() == "private\n"
        # Other routes keep the conversation's own grants.
        assert client.get(f"/api/sessions/{sid}").json()["allowed_directories"] == []


def test_credential_files_stay_refused_inside_an_added_folder(setup):
    settings, _, other = setup
    (other / ".env").write_text("TOKEN=secret\n")
    (other / "databricks.txt").write_text("DATABRICKS_TOKEN=secret\n")
    settings.values["env_file"] = str(other / "databricks.txt")
    with app_client(settings) as raw:
        client = Client(raw)
        client.post(ROUTE, {"path": str(other)})
        names = [entry["name"] for entry in client.get("/api/files", path=str(other)).json()]
        assert names == ["sub", "notes.txt"]
        for name in (".env", "databricks.txt"):
            response = client.get("/api/file", path=str(other / name))
            assert response.status_code == 400
            assert "Credential files are excluded" in response.json()["detail"]
            assert "secret" not in response.text


def test_a_listed_folder_later_replaced_by_a_link_to_the_state_folder_is_not_browsable(setup, tmp_path):
    settings, _, other = setup
    with app_client(settings) as raw:
        client = Client(raw)
        client.post(ROUTE, {"path": str(other)})
        for item in sorted(other.rglob("*"), reverse=True):
            item.rmdir() if item.is_dir() else item.unlink()
        other.rmdir()
        other.symlink_to(settings.state_dir, target_is_directory=True)
        assert client.get("/api/files", path=str(other)).status_code == 400
        assert client.get("/api/file", path=str(other / "settings.json")).status_code == 400


def test_lists_are_kept_per_project(setup, tmp_path):
    settings, project, other = setup
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "w.txt").write_text("w\n")
    with app_client(settings) as raw:
        client = Client(raw)
        first = client.session()
        client.post(ROUTE, {"path": str(other)}, session_id=first)
        # A second conversation in the same project shares the list.
        assert paths(client.get(ROUTE, session_id=client.session())) == [str(other)]
        settings.values["workspace"] = str(worktree)
        second = client.session()
        listed = client.get(ROUTE, session_id=second).json()
        assert listed == {"workspace": str(worktree), "folders": []}
        assert client.get("/api/files", path=str(other), session_id=second).status_code == 400
        assert paths(client.get(ROUTE, session_id=first)) == [str(other)]
        assert paths(client.post(ROUTE, {"path": str(project)}, session_id=second)) == [str(project)]
        assert paths(client.get(ROUTE, session_id=first)) == [str(other)]


# The list never widens what the agent may do.

async def test_agent_tools_still_ask_for_folder_access_inside_an_added_folder(setup):
    settings, project, other = setup
    folders = WorkspaceFolders(settings)
    assert folders.add(str(project), str(other)) == str(other)
    store = Store(settings.state_dir / "agent.sqlite3")
    try:
        manager = AgentManager(store, settings)
        session = store.create(settings.values)
        session["permission_mode"] = "manual"
        asked = []

        async def decline(session, event):
            asked.append((event["name"], event["input"]))
            return False
        manager.approve = decline
        tools = manager.session_tools(session)
        read = await manager.execute_tool(session, tools, "read_file", {"path": str(other / "notes.txt")}, "read")
        write = await manager.execute_tool(session, tools, "write_file",
                                           {"path": str(other / "new.txt"), "content": "agent\n"}, "write")
        assert asked == [("access_directory", {"path": str(other)}), ("access_directory", {"path": str(other)})]
        assert "declined" in read and "outside notes" not in read
        assert "declined" in write
        assert not (other / "new.txt").exists()
        assert session.get("allowed_directories", []) == []
    finally:
        store.db.close()


def _stream(text="Done.", call=None):
    delta = {"tool_calls": [{"index": 0, **call}]} if call else {"content": text}
    chunk = {"choices": [{"delta": delta, "finish_reason": "tool_calls" if call else "stop"}]}
    return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")


async def test_an_agent_turn_does_not_read_an_added_folder_without_access(setup, monkeypatch):
    settings, project, other = setup
    WorkspaceFolders(settings).add(str(project), str(other))
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    requests = []

    async def gateway(request):
        payload = json.loads(request.content)
        if "tools" not in payload:
            return _stream("Title")
        requests.append(payload)
        if len(requests) == 1:
            arguments = json.dumps({"path": str(other / "notes.txt")})
            return _stream(call={"id": "r1", "type": "function", "function": {"name": "read_file", "arguments": arguments}})
        return _stream()

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(gateway)))
    store = Store(settings.state_dir / "turn.sqlite3")
    try:
        manager = AgentManager(store, settings)
        session = store.create(settings.values)
        session["permission_mode"] = "manual"
        asked = []

        async def decline(session, event):
            asked.append(event["name"])
            return False
        manager.approve = decline
        await manager.run(session, "Read the notes")
        assert asked == ["access_directory"]
        assert len(requests) == 2
        assert "outside notes" not in json.dumps(requests[-1])
        assert session.get("allowed_directories", []) == []
    finally:
        store.db.close()
