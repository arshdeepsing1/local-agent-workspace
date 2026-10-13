"""A subagent's file edits are listed in its own review and rolled up into its parent's."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from local_agent.api import create_app
from local_agent.config import Settings


def stream(content=None, call=None):
    delta = {"content": content} if call is None else {"tool_calls": [{"index": 0, "id": "call", "type": "function", "function": call}]}
    return httpx.Response(200, text="data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": "stop" if call is None else "tool_calls"}]}) + "\n\ndata: [DONE]\n\n")


class Socket:
    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(json.loads(json.dumps(data)))  # A copy: events change after they are sent.


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("base\n")
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "synthetic-test-token")

    async def handler(request):
        # The child edits a.txt (from whatever the parent left there); the parent delegates.
        messages = json.loads(request.content)["messages"]
        child = any(message["role"] == "user" and "Delegated task:" in str(message.get("content")) for message in messages)
        if messages[-1]["role"] == "tool":
            return stream("Child done." if child else "Parent done.")
        if child:
            text = (project / "a.txt").read_text().strip()
            return stream(call={"name": "edit_file", "arguments": json.dumps({"path": "a.txt", "old_text": text, "new_text": "child"})})
        return stream(call={"name": "delegate_task", "arguments": json.dumps({"task": "Edit a.txt"})})
    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(**kwargs, transport=httpx.MockTransport(handler)))
    application = create_app(settings)
    with TestClient(application) as client:
        client.headers["X-Local-Token"] = client.get("/api/bootstrap").json()["token"]
        manager = application.state.manager
        parent = manager.get(client.post("/api/sessions").json()["id"])
        parent.update(permission_mode="bypassPermissions", title_generated=True)
        manager.store.save(parent)
        yield client, manager, parent, project


def delegate(client, manager, parent):
    """Run one parent turn (as the app starts it) that delegates; returns the child's id."""
    async def turn():
        manager.start(parent["id"], "Have a subagent edit a.txt")
        await manager.tasks[parent["id"]]
    client.portal.call(turn)
    events = manager.get(parent["id"])["events"]
    return next(event["child_session_id"] for event in reversed(events) if event.get("child_session_id"))


def changes(client, session_id):
    return client.get(f"/api/sessions/{session_id}/changes").json()["files"]


def test_a_subagent_edit_is_listed_and_pushed_in_the_parent_and_the_parent_undo_is_recoverable(app):
    client, manager, parent, project = app
    socket = Socket()
    manager.listeners[parent["id"]] = {socket}
    child_id = delegate(client, manager, parent)
    assert (project / "a.txt").read_text() == "child\n"
    listed = changes(client, parent["id"])
    assert [(item["path"], item["via_session_id"]) for item in listed] == [("a.txt", child_id)]
    assert "source" not in listed[0]
    # Pushed to the parent's page when the child edits, while delegate_task still runs.
    pushed = next(index for index, item in enumerate(socket.sent)
                  if item["type"] == "changes" and [file["path"] for file in item["changes"]["files"]] == ["a.txt"])
    finished = next(index for index, item in enumerate(socket.sent) if item["type"] == "event"
                    and item["event"].get("name") == "delegate_task" and item["event"].get("state") == "completed")
    assert pushed < finished
    assert [item["path"] for item in changes(client, child_id)] == ["a.txt"]
    assert "via_session_id" not in changes(client, child_id)[0]

    undo = {"files": [{"path": "a.txt", "current_hash": listed[0]["current_hash"]}]}
    assert client.post(f"/api/sessions/{parent['id']}/changes/undo", json=undo).json()["files"] == []
    assert (project / "a.txt").read_text() == "base\n"
    restores = manager.checkpoints.list(parent["id"])
    assert [(item["path"], item["session_id"]) for item in restores] == [("a.txt", parent["id"])]
    # The child's list compares with its own baseline: the file is back there, so nothing is left.
    assert changes(client, child_id) == []


def test_an_earlier_parent_edit_keeps_the_older_baseline(app):
    client, manager, parent, project = app
    tools = manager.session_tools(parent)
    from local_agent.instructions import load_project_instructions
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance["text"], guidance["warnings"])
    client.portal.call(manager.execute_tool, parent, tools, "edit_file", {"path": "a.txt", "old_text": "base", "new_text": "parent"}, "edit")
    child_id = delegate(client, manager, parent)
    diff = client.get(f"/api/sessions/{parent['id']}/changes/diff", params={"path": "a.txt"}).json()
    assert [(line["kind"], line["text"]) for line in diff["lines"]] == [("removed", "base"), ("added", "child")]
    child_diff = client.get(f"/api/sessions/{child_id}/changes/diff", params={"path": "a.txt"}).json()
    assert [(line["kind"], line["text"]) for line in child_diff["lines"]] == [("removed", "parent"), ("added", "child")]
    assert changes(client, parent["id"])[0]["via_session_id"] == child_id


def test_deleting_the_parent_removes_only_its_own_rows(app):
    client, manager, parent, _ = app
    child_id = delegate(client, manager, parent)
    assert manager.changes.has(parent["id"]) and manager.changes.has(child_id)
    assert client.delete(f"/api/sessions/{parent['id']}").status_code == 200
    assert not manager.changes.has(parent["id"])
    assert [item["path"] for item in changes(client, child_id)] == ["a.txt"]
