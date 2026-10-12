"""Review of agent file changes: the changed-files list, diffs, Keep and Undo."""
import stat

import pytest
from fastapi.testclient import TestClient

from local_agent.api import create_app
from local_agent.changes import ChangeReview
from local_agent.config import Settings
from local_agent.recovery import CheckpointManager
from local_agent.store import Store
from local_agent.tools import WorkspaceTools


@pytest.fixture
def review(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "state.sqlite3")
    checkpoints = CheckpointManager(store)
    yield ChangeReview(store, checkpoints), checkpoints, WorkspaceTools(str(workspace)), store
    store.db.close()


def agent_edit(review, name, session_id="chat", **arguments):
    changes, checkpoints, tools, _ = review
    return checkpoints.apply_edit(tools, name, arguments, session_id=session_id, turn_id="turn",
                                  on_applied=lambda record: changes.track(record, tools))


def files(review, session_id="chat"):
    changes, _, tools, _ = review
    return {item["path"]: item for item in changes.list(session_id, tools)["files"]}


def act(review, action, path, session_id="chat", hunk=None, **extra):
    changes, _, tools, _ = review
    request = {"path": path, "current_hash": files(review, session_id)[path]["current_hash"], "hunk": hunk}
    if hunk is not None:
        request["baseline_hash"] = changes.diff(session_id, tools, path)["baseline_hash"]
    return changes.act(session_id, tools, action, [{**request, **extra}])


def test_list_compares_with_the_file_before_the_first_agent_edit(review):
    changes, checkpoints, tools, _ = review
    (tools.root / "app.py").write_text("one\ntwo\nthree\n")
    agent_edit(review, "edit_file", path="app.py", old_text="two", new_text="TWO")
    agent_edit(review, "edit_file", path="app.py", old_text="three\n", new_text="three\nfour\n")
    agent_edit(review, "write_file", path="docs/new.md", content="# New\n")
    # Editor saves are the user's own edits, not agent changes to review.
    checkpoints.apply_edit(tools, "write_file", {"path": "manual.txt", "content": "mine"}, session_id="chat")
    result = changes.list("chat", tools)
    assert [item["path"] for item in result["files"]] == ["app.py", "docs/new.md"]
    assert {key: result["files"][0][key] for key in ("status", "added", "removed")} == {"status": "modified", "added": 2, "removed": 1}
    assert {key: result["files"][1][key] for key in ("status", "added", "removed")} == {"status": "created", "added": 1, "removed": 0}
    assert (result["added"], result["removed"]) == (3, 1)
    assert changes.list("other-chat", tools)["files"] == []

    diff = changes.diff("chat", tools, "app.py")
    assert [(line["kind"], line["text"]) for line in diff["lines"]] == [
        ("context", "one"), ("removed", "two"), ("added", "TWO"), ("context", "three"), ("added", "four")]
    assert diff["hunks"] == [{"index": 0, "line": 1, "removed": 1, "added": 1}, {"index": 1, "line": 4, "removed": 0, "added": 1}]
    assert [(line.get("old"), line.get("new")) for line in diff["lines"]] == [(1, 1), (2, None), (None, 2), (3, 3), (None, 4)]


def test_keep_accepts_the_file_and_undo_restores_exact_bytes_and_mode(review):
    changes, checkpoints, tools, _ = review
    target = tools.root / "script.sh"
    target.write_bytes(b"#!/bin/sh\r\necho old\r\n")
    target.chmod(0o751)
    agent_edit(review, "write_file", path="script.sh", content="#!/bin/sh\necho new\n")
    agent_edit(review, "write_file", path="kept.txt", content="keep me")
    act(review, "keep", "kept.txt")
    assert (tools.root / "kept.txt").read_text() == "keep me"
    assert list(files(review)) == ["script.sh"]

    result = act(review, "undo", "script.sh")
    assert result == {"files": [], "added": 0, "removed": 0}
    assert target.read_bytes() == b"#!/bin/sh\r\necho old\r\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o751
    # The undo is an ordinary checkpoint, so Recovery can bring the agent's version back.
    undo = checkpoints.list("chat")[0]
    assert undo["turn_id"] is None
    preview = checkpoints.preview(undo["id"], tools, "chat")
    checkpoints.restore(undo["id"], tools, preview["expected_current_hash"], "chat")
    assert target.read_text() == "#!/bin/sh\necho new\n"


def test_undo_of_a_created_file_deletes_it(review):
    changes, _, tools, _ = review
    agent_edit(review, "write_file", path="nested/new.txt", content="")
    assert files(review)["nested/new.txt"]["status"] == "created"
    assert changes.diff("chat", tools, "nested/new.txt")["hunks"] == []  # An empty new file has no lines.
    act(review, "undo", "nested/new.txt")
    assert not (tools.root / "nested" / "new.txt").exists()
    assert files(review) == {}


def test_single_changes_can_be_kept_or_undone(review):
    changes, _, tools, _ = review
    target = tools.root / "notes.md"
    target.write_text("a\nb\nc\nd\ne\n")
    agent_edit(review, "write_file", path="notes.md", content="a\nB\nc\nd\ne\nf\n")
    act(review, "keep", "notes.md", hunk=1)  # Keep the added "f"; the B change stays pending.
    assert target.read_text() == "a\nB\nc\nd\ne\nf\n"
    diff = changes.diff("chat", tools, "notes.md")
    assert [(line["kind"], line["text"]) for line in diff["lines"] if line["kind"] != "context"] == [("removed", "b"), ("added", "B")]
    assert files(review)["notes.md"]["added"] == 1

    result = act(review, "undo", "notes.md", hunk=0)  # Undo the remaining change: nothing is left to review.
    assert result["files"] == []
    assert target.read_text() == "a\nb\nc\nd\ne\nf\n"


def test_undoing_one_change_keeps_the_others_pending(review):
    _, _, tools, _ = review
    target = tools.root / "notes.md"
    target.write_text("a\nb\nc\nd\n")
    agent_edit(review, "write_file", path="notes.md", content="A\nb\nc\nD")
    act(review, "undo", "notes.md", hunk=0)
    assert target.read_text() == "a\nb\nc\nD"
    assert files(review)["notes.md"]["added"] == 1
    diff = review[0].diff("chat", tools, "notes.md")
    assert [line for line in diff["lines"] if line["kind"] == "added"] == [
        {"kind": "added", "text": "D", "new": 4, "hunk": 0, "newline": False}]


def test_stale_reviews_change_nothing(review):
    changes, _, tools, _ = review
    for name in ("first.txt", "second.txt"):
        (tools.root / name).write_text("before\n")
        agent_edit(review, "write_file", path=name, content="after\n")
    listed = files(review)
    diff = changes.diff("chat", tools, "first.txt")
    (tools.root / "second.txt").write_text("after\nexternal\n")
    # One stale file stops the whole undo before any file is written.
    with pytest.raises(ValueError, match="second.txt changed since you reviewed it"):
        changes.act("chat", tools, "undo", [{"path": path, "current_hash": listed[path]["current_hash"]} for path in listed])
    assert (tools.root / "first.txt").read_text() == "after\n"
    for request in ({"hunk": 0, "baseline_hash": "old"}, {"hunk": 5, "baseline_hash": diff["baseline_hash"]}):
        with pytest.raises(ValueError, match="changed since you reviewed it"):
            changes.act("chat", tools, "keep", [{"path": "first.txt", "current_hash": diff["current_hash"], **request}])
    with pytest.raises(ValueError, match="Review the file"):
        changes.act("chat", tools, "undo", [{"path": "first.txt"}])
    with pytest.raises(ValueError, match="No agent changes"):
        changes.act("chat", tools, "undo", [{"path": "unchanged.txt", "current_hash": "x"}])
    assert (tools.root / "first.txt").read_text() == "after\n"
    # Keeping a whole file needs no hash: it only stops tracking it.
    changes.act("chat", tools, "keep", [{"path": "second.txt"}])
    assert list(files(review)) == ["first.txt"]


def test_files_edited_back_or_by_other_chats_are_tracked_correctly(review):
    changes, _, tools, store = review
    target = tools.root / "shared.txt"
    target.write_text("original")
    agent_edit(review, "write_file", path="shared.txt", content="first chat")
    agent_edit(review, "write_file", session_id="other", path="shared.txt", content="second chat")
    assert files(review)["shared.txt"]["status"] == "modified"
    act(review, "undo", "shared.txt", session_id="other")
    assert target.read_text() == "first chat"
    agent_edit(review, "write_file", path="shared.txt", content="original")
    assert files(review) == {}  # Edited back to where it started.
    agent_edit(review, "write_file", path="gone.txt", content="x")
    changes.delete_session("chat")
    assert store.db.execute("SELECT COUNT(*) FROM agent_changes").fetchone()[0] == 0


def test_unreadable_files_are_listed_and_can_still_be_kept(review):
    changes, _, tools, _ = review
    agent_edit(review, "write_file", path="data.txt", content="text")
    (tools.root / "data.txt").write_bytes(b"binary\x00")
    item = files(review)["data.txt"]
    assert item["status"] == "unavailable" and "Binary" in item["error"] and item["current_hash"] is None
    changes.act("chat", tools, "keep", [{"path": "data.txt", "current_hash": None}])
    assert files(review) == {}


def test_tracked_external_paths_follow_folder_grants(review, tmp_path):
    changes, checkpoints, tools, _ = review
    outside = tmp_path / "outside"
    outside.mkdir()
    granted = WorkspaceTools(str(tools.root), allowed_directories=[str(outside)])
    checkpoints.apply_edit(granted, "write_file", {"path": str(outside / "x.txt"), "content": "x"}, session_id="chat",
                           turn_id="turn", on_applied=lambda record: changes.track(record, granted))
    assert changes.list("chat", granted)["files"][0]["path"] == str(outside / "x.txt")
    revoked = changes.list("chat", tools)["files"][0]
    assert revoked["status"] == "unavailable" and "access approval" in revoked["error"]
    with pytest.raises(ValueError, match="access approval"):
        changes.diff("chat", tools, str(outside / "x.txt"))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    workspace = tmp_path / "project"
    workspace.mkdir()
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(workspace), env_file="")
    settings.credentials = lambda: ("https://fake.example", "synthetic-test-token")
    app = create_app(settings)
    with TestClient(app) as client:
        token = client.get("/api/bootstrap").json()["token"]
        client.headers["X-Local-Token"] = token
        client.token = token
        yield client, app.state.manager, workspace


def test_review_api_lists_diffs_keeps_and_undoes(client):
    client, manager, workspace = client
    session_id = client.post("/api/sessions").json()["id"]
    (workspace / "app.py").write_text("print('old')\n")
    tools = WorkspaceTools(str(workspace))
    for arguments in ({"path": "app.py", "content": "print('new')\n"}, {"path": "new.txt", "content": "hi\n"}):
        manager.checkpoints.apply_edit(tools, "write_file", arguments, session_id=session_id, turn_id="turn",
                                       on_applied=lambda record: manager.changes.track(record, tools))
    base = f"/api/sessions/{session_id}/changes"
    with client.websocket_connect(f"/api/sessions/{session_id}/stream", subprotocols=["local-workspace", client.token]) as socket:
        assert socket.receive_json()["type"] == "snapshot"
        pushed = socket.receive_json()
        assert pushed["type"] == "changes" and [item["path"] for item in pushed["changes"]["files"]] == ["app.py", "new.txt"]
        listed = client.get(base).json()
        assert listed == pushed["changes"]
        diff = client.get(base + "/diff", params={"path": "app.py"}).json()
        assert [line["kind"] for line in diff["lines"]] == ["removed", "added"]
        assert client.get(base + "/diff", params={"path": "missing.txt"}).status_code == 400

        # A running conversation in the same workspace blocks Undo but not Keep.
        manager.statuses[session_id] = "running"
        hashes = {item["path"]: item["current_hash"] for item in listed["files"]}
        undo = {"files": [{"path": "app.py", "current_hash": hashes["app.py"]}]}
        assert client.post(base + "/undo", json=undo).status_code == 409
        kept = client.post(base + "/keep", json={"files": [{"path": "new.txt", "current_hash": hashes["new.txt"]}]})
        assert [item["path"] for item in kept.json()["files"]] == ["app.py"]
        assert socket.receive_json() == {"type": "changes", "changes": kept.json()}
        manager.statuses[session_id] = "idle"

        assert client.post(base + "/undo", json=undo).json() == {"files": [], "added": 0, "removed": 0}
        assert socket.receive_json()["changes"]["files"] == []
    assert (workspace / "app.py").read_text() == "print('old')\n"
    assert (workspace / "new.txt").read_text() == "hi\n"
    assert client.post(base + "/merge", json=undo).status_code == 404
    assert client.post(base + "/keep", json={"files": []}).status_code == 422
    assert client.delete(f"/api/sessions/{session_id}").status_code == 200
    assert client.get(base).status_code == 404


def test_editor_save_refreshes_counts_without_tracking_new_files(client):
    client, manager, workspace = client
    session_id = client.post("/api/sessions").json()["id"]
    tools = WorkspaceTools(str(workspace))
    manager.checkpoints.apply_edit(tools, "write_file", {"path": "a.txt", "content": "agent\n"}, session_id=session_id,
                                   turn_id="turn", on_applied=lambda record: manager.changes.track(record, tools))
    (workspace / "b.txt").write_text("b")
    with client.websocket_connect(f"/api/sessions/{session_id}/stream", subprotocols=["local-workspace", client.token]) as socket:
        socket.receive_json(), socket.receive_json()
        pushed = []
        for path, original in (("b.txt", "b"), ("a.txt", "agent\n")):
            body = {"path": path, "original": original, "content": original + "user\n", "session_id": session_id}
            assert client.put("/api/file", json=body).status_code == 200
            pushed.append(socket.receive_json()["changes"])
    assert [[(item["path"], item["added"]) for item in changes["files"]] for changes in pushed] == [[("a.txt", 1)], [("a.txt", 2)]]


async def test_agent_tool_edits_are_tracked_and_pushed_but_declined_ones_are_not(tmp_path, monkeypatch):
    from local_agent.agents import AgentManager
    from local_agent.instructions import load_project_instructions

    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    store = Store(settings.state_dir / "changes.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    tools = WorkspaceTools(str(project))
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance["text"], guidance["warnings"])

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, data):
            self.sent.append(data)

    socket = Socket()
    manager.listeners[session["id"]] = {socket}
    await manager.event(session, "user", text="Edit the note")
    (project / "note.txt").write_text("before\n")

    async def refuse(*args):
        return False
    manager.approve = refuse
    await manager.execute_tool(session, tools, "write_file", {"path": "note.txt", "content": "declined\n"}, "denied")
    assert manager.changes.list(session["id"], tools)["files"] == []
    session["permission_mode"] = "acceptEdits"
    await manager.execute_tool(session, tools, "edit_file", {"path": "note.txt", "old_text": "before", "new_text": "after"}, "edit")
    pushed = [item["changes"] for item in socket.sent if item["type"] == "changes"]
    assert [(item["path"], item["added"], item["removed"]) for item in pushed[-1]["files"]] == [("note.txt", 1, 1)]
    socket.sent.clear()
    await manager.status(session, "idle")
    assert [item["type"] for item in socket.sent] == ["status", "changes"]
    store.db.close()
