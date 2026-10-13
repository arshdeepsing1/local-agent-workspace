"""Automatic Keep: an optional setting that keeps a chat's agent changes when its reply ends."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.store import Store
from local_agent.tools import WorkspaceTools


class Socket:
    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(json.loads(json.dumps(data)))

    def changes(self):
        return [item["changes"] for item in self.sent if item["type"] == "changes"]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "keep.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session.update(permission_mode="bypassPermissions", title_generated=True)
    store.save(session)
    socket = Socket()
    manager.listeners[session["id"]] = {socket}
    yield manager, session, project, socket
    store.db.close()


def edit(manager, session, project, name="a.txt"):
    tools = WorkspaceTools(str(project))
    manager.checkpoints.apply_edit(tools, "write_file", {"path": name, "content": "agent\n"}, session_id=session["id"],
                                   turn_id="turn", on_applied=lambda record: manager.track_change(session, record, tools))


def stream(content=None, call=None):
    delta = {"content": content} if call is None else {"tool_calls": [{"index": 0, "id": "call", "type": "function", "function": call}]}
    return httpx.Response(200, text="data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": "stop" if call is None else "tool_calls"}]}) + "\n\ndata: [DONE]\n\n")


async def test_with_the_setting_on_a_finished_reply_keeps_its_changes_and_pushes_the_empty_list(runtime, monkeypatch):
    manager, session, project, socket = runtime
    manager.settings.values["auto_keep_changes"] = True

    async def handler(request):
        if json.loads(request.content)["messages"][-1]["role"] == "tool":
            return stream("Written.")
        return stream(call={"name": "write_file", "arguments": json.dumps({"path": "a.txt", "content": "agent\n"})})
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handler)))
    await manager.run(session, "Write a.txt")
    assert (project / "a.txt").read_text() == "agent\n"
    pushed = socket.changes()
    assert [item["path"] for item in pushed[0]["files"]] == ["a.txt"]  # Listed while the reply ran.
    assert pushed[-1] == {"files": [], "added": 0, "removed": 0}
    assert socket.sent[-1]["type"] == "changes"
    assert not manager.changes.has(session["id"])
    # Recovery still holds the file tool's checkpoint.
    assert [item["path"] for item in manager.checkpoints.list(session["id"])] == ["a.txt"]


async def test_with_the_setting_off_changes_stay_listed(runtime):
    manager, session, project, socket = runtime
    assert manager.settings.values["auto_keep_changes"] is False
    edit(manager, session, project)
    await manager.status(session, "idle", turn_ended=True)
    assert manager.changes.has(session["id"])
    assert [item["path"] for item in socket.changes()[-1]["files"]] == ["a.txt"]


async def test_a_manual_compaction_does_not_keep_changes(runtime):
    manager, session, project, socket = runtime
    manager.settings.values["auto_keep_changes"] = True
    edit(manager, session, project)
    await manager.status(session, "idle")  # A compaction outside a turn ends without turn_ended.
    assert manager.changes.has(session["id"])
    assert [item["path"] for item in socket.changes()[-1]["files"]] == ["a.txt"]


async def test_a_subagent_reply_ending_leaves_the_parents_rolled_up_rows(runtime):
    manager, parent, project, socket = runtime
    manager.settings.values["auto_keep_changes"] = True
    child = manager.store.create({"workspace": parent["workspace"], "model": parent["model"]})
    child.update(parent_session_id=parent["id"], is_subagent=True)
    manager.store.save(child)
    edit(manager, child, project)
    assert manager.changes.has(child["id"]) and manager.changes.has(parent["id"])
    await manager.status(child, "idle", turn_ended=True)
    assert not manager.changes.has(child["id"])
    listed = manager.changes.list(parent["id"], manager.review_tools(parent))["files"]
    assert [(item["path"], item["via_session_id"]) for item in listed] == [("a.txt", child["id"])]
    await manager.status(parent, "idle", turn_ended=True)
    assert not manager.changes.has(parent["id"])


def test_the_setting_is_validated_and_saved(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    with pytest.raises(ValueError, match="Automatic Keep must be on or off."):
        settings.update({"auto_keep_changes": "yes"})
    with TestClient(create_app(settings)) as client:
        client.headers["X-Local-Token"] = client.get("/api/bootstrap").json()["token"]
        body = {"workspace": str(project), "model": "test-model", "env_file": ""}
        assert client.put("/api/settings", json={**body, "auto_keep_changes": 1}).status_code == 422
        assert client.put("/api/settings", json={**body, "auto_keep_changes": "true"}).status_code == 422
        assert client.get("/api/bootstrap").json()["settings"]["auto_keep_changes"] is False
        assert client.put("/api/settings", json={**body, "auto_keep_changes": True}).json()["auto_keep_changes"] is True
        assert client.put("/api/settings", json=body).json()["auto_keep_changes"] is True  # Left out: unchanged.
    assert Settings(tmp_path / "state").values["auto_keep_changes"] is True
