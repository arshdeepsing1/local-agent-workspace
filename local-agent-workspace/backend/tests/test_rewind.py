"""Rewind to a user message: the agent's file edits (code), the conversation in place (after an
archived backup), or both, optionally resending an edited message."""
import asyncio
import json
import re
import time

import httpx
import pytest

from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.recovery import _snapshot
from local_agent.store import Store


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    app = create_app(settings)
    manager = app.state.manager
    session = manager.store.create(settings.values)
    session.update(title="Edits", title_generated=True)
    manager.store.save(session)
    yield app, manager, session, project
    manager.store.db.close()


async def client_for(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        token = (await client.get("/api/bootstrap")).json()["token"]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                             headers={"X-Local-Token": token})


class Chat:
    """Builds a three-turn conversation whose turns edit files through the checkpointed file tools."""

    def __init__(self, manager, session, project):
        self.manager, self.session, self.project = manager, session, project
        self.tools = manager.session_tools(session)

    def turn(self, number, edits=(), session_id=None):
        event_id = f"u{number}"
        self.session["events"] += [{"id": event_id, "type": "user", "created": time.time(), "text": f"Turn {number}",
                                    "in_wire": True},
                                   {"id": f"a{number}", "type": "assistant", "created": time.time(), "text": "Done.",
                                    "request_info": {"model": "test-model", "status": "completed",
                                                     "finish_reason": "stop"}}]
        self.session["wire"] += [{"role": "user", "content": f"Turn {number}"}, {"role": "assistant", "content": "Done."}]
        for path, content in edits:
            self.edit(path, content, event_id)
        self.session["terminal_reason"] = "completed"
        self.manager.store.save(self.session)

    def edit(self, path, content, turn_id, session_id=None):
        self.manager.checkpoints.apply_edit(
            self.tools, "write_file", {"path": path, "content": content}, session_id=session_id or self.session["id"],
            turn_id=turn_id, on_applied=lambda record: self.manager.changes.track(record, self.tools))

    def read(self, name):
        path = self.project / name
        return path.read_text() if path.exists() else None

    def files(self):
        return {name: self.read(name) for name in ("a.txt", "b.txt")}


def standard(runtime):
    """a.txt exists before the chat; turn 1 edits it, turns 2 and 3 edit a.txt and b.txt (created in turn 2)."""
    _, manager, session, project = runtime
    (project / "a.txt").write_text("a0")
    chat = Chat(manager, session, project)
    chat.turn(1, [("a.txt", "a1")])
    chat.turn(2, [("a.txt", "a2"), ("b.txt", "b2")])
    chat.turn(3, [("a.txt", "a3"), ("b.txt", "b3")])
    return chat


def jsonl(manager, session_id):
    return manager.store._path(session_id).read_bytes()


async def test_code_rewind_restores_files_to_before_the_turn_and_recovery_can_undo_it(runtime):
    app, manager, session, _ = runtime
    chat = standard(runtime)
    events = list(manager.store.get(session["id"])["events"])
    async with await client_for(app) as client:
        preview = (await client.get(f"/api/sessions/{session['id']}/rewind-preview",
                                    params={"before_event_id": "u2"})).json()
        assert preview["code"] == {"possible": True, "reason": None, "files": [
            {"path": "a.txt", "status": "restore"}, {"path": "b.txt", "status": "restore"}]}
        assert preview["conversation"] == {"possible": True, "reason": None, "events_removed": 4,
                                           "messages_removed": 4, "summary_dropped": False}
        response = await client.post(f"/api/sessions/{session['id']}/rewind", json={"before_event_id": "u2", "code": True})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["files"] == [{"path": "a.txt", "status": "restore"}, {"path": "b.txt", "status": "restore"}]
        assert body["backup_session_id"] is None and body["resent"] is False
        assert chat.files() == {"a.txt": "a1", "b.txt": None}
        assert manager.store.get(session["id"])["events"] == events  # Code only: the conversation stays.
        # One reverse checkpoint per file (no turn), restorable from Recovery.
        listed = (await client.get("/api/checkpoints", params={"session_id": session["id"]})).json()
        reverse = [item for item in listed if item["turn_id"] is None]
        assert sorted(item["path"] for item in reverse) == ["a.txt", "b.txt"]
        assert all(item["status"] == "restored" for item in listed if item["turn_id"] in ("u2", "u3"))
        assert all(item["status"] == "applied" for item in listed if item["turn_id"] == "u1")
        undo = next(item for item in reverse if item["path"] == "a.txt")
        check = (await client.get(f"/api/checkpoints/{undo['id']}/preview", params={"session_id": session["id"]})).json()
        restored = await client.post(f"/api/checkpoints/{undo['id']}/restore", params={"session_id": session["id"]},
                                     json={"expected_current_hash": check["expected_current_hash"]})
        assert restored.status_code == 200, restored.text
        assert chat.read("a.txt") == "a3"
        # A second rewind finds nothing left to do for these turns.
        again = (await client.get(f"/api/sessions/{session['id']}/rewind-preview",
                                  params={"before_event_id": "u2"})).json()
        assert again["code"]["files"] == [] and again["code"]["possible"] is False


async def test_drift_refuses_the_whole_rewind_and_changes_nothing(runtime):
    app, manager, session, project = runtime
    chat = standard(runtime)
    (project / "b.txt").write_text("my own change")
    async with await client_for(app) as client:
        response = await client.post(f"/api/sessions/{session['id']}/rewind",
                                     json={"before_event_id": "u2", "code": True, "conversation": True})
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["files"] == ["b.txt"]
        assert detail["message"] == (
            "Cannot rewind files: b.txt changed after the agent's last edit or between its edits (for example you "
            "saved them, used Undo, or a command or subagent changed them). Nothing was changed.")
        preview = (await client.get(f"/api/sessions/{session['id']}/rewind-preview",
                                    params={"before_event_id": "u2"})).json()
        assert [(item["path"], item["status"]) for item in preview["code"]["files"]] == [
            ("a.txt", "restore"), ("b.txt", "drifted")]
        assert preview["code"]["possible"] is False and preview["code"]["reason"].startswith("Cannot rewind files: b.txt")
    assert chat.files() == {"a.txt": "a3", "b.txt": "my own change"}
    assert len(manager.store.list()) == 1  # No backup was written.
    # The manager itself checks every file before writing any.
    with pytest.raises(ValueError, match="Cannot rewind files: b.txt"):
        manager.checkpoints.rewind(chat.tools, session["id"], ["u2", "u3"])
    assert chat.files() == {"a.txt": "a3", "b.txt": "my own change"}


async def interleaved(runtime, foreign):
    """Turn 2 edits a.txt, something else writes it, turn 3's agent edit keeps that write."""
    app, manager, session, project = runtime
    (project / "a.txt").write_text("a0")
    chat = Chat(manager, session, project)
    chat.turn(1, [("b.txt", "b1")])
    chat.turn(2, [("a.txt", "a2"), ("b.txt", "b2")])
    async with await client_for(app) as client:
        await foreign(client, chat)
        chat.turn(3)
        chat.edit("a.txt", chat.read("a.txt") + " plus agent", "u3")
        before = chat.files()
        response = await client.post(f"/api/sessions/{session['id']}/rewind", json={"before_event_id": "u2", "code": True})
        assert response.status_code == 409
        assert response.json()["detail"]["files"] == ["a.txt"]
        preview = (await client.get(f"/api/sessions/{session['id']}/rewind-preview",
                                    params={"before_event_id": "u2"})).json()
        assert [(item["path"], item["status"]) for item in preview["code"]["files"]] == [
            ("a.txt", "interleaved"), ("b.txt", "restore")]
    assert chat.files() == before


async def test_an_editor_save_between_agent_edits_is_never_reverted(runtime):
    async def save(client, chat):
        response = await client.put("/api/file", json={"path": "a.txt", "content": "user text", "original": "a2",
                                                       "session_id": chat.session["id"]})
        assert response.status_code == 200
    await interleaved(runtime, save)


async def test_an_undo_between_agent_edits_is_never_reverted(runtime):
    async def undo(client, chat):
        changes = (await client.get(f"/api/sessions/{chat.session['id']}/changes")).json()
        entry = next(item for item in changes["files"] if item["path"] == "a.txt")
        response = await client.post(f"/api/sessions/{chat.session['id']}/changes/undo",
                                     json={"files": [{"path": "a.txt", "current_hash": entry["current_hash"]}]})
        assert response.status_code == 200, response.text
        assert chat.read("a.txt") == "a0"
    await interleaved(runtime, undo)


async def test_a_subagent_edit_between_agent_edits_is_never_reverted(runtime):
    async def subagent(client, chat):
        chat.edit("a.txt", "child text", "child-turn", session_id="child-session")
    await interleaved(runtime, subagent)


async def test_unchanged_files_are_skipped_and_ungranted_files_block(runtime, tmp_path):
    app, manager, session, project = runtime
    chat = standard(runtime)
    (project / "a.txt").write_text("a1")  # Already as it was before turn 2 (the user put it back).
    (project / "b.txt").unlink()
    async with await client_for(app) as client:
        response = await client.post(f"/api/sessions/{session['id']}/rewind", json={"before_event_id": "u2", "code": True})
        assert response.status_code == 200, response.text
        assert response.json()["files"] == [{"path": "a.txt", "status": "unchanged"},
                                            {"path": "b.txt", "status": "unchanged"}]
    assert chat.files() == {"a.txt": "a1", "b.txt": None}
    assert not [item for item in manager.checkpoints.list(session["id"]) if item["turn_id"] is None]

    # A file in a folder whose grant was removed cannot be rewound.
    outside = tmp_path / "outside"
    outside.mkdir()
    fresh = manager.store.create(manager.settings.values)
    fresh["allowed_directories"] = [str(outside)]
    manager.store.save(fresh)
    other = Chat(manager, fresh, project)
    other.turn(1, [(str(outside / "c.txt"), "c1")])
    fresh["allowed_directories"] = []
    manager.store.save(fresh)
    async with await client_for(app) as client:
        preview = (await client.get(f"/api/sessions/{fresh['id']}/rewind-preview", params={"before_event_id": "u1"})).json()
        [entry] = preview["code"]["files"]
        assert entry["status"] == "unavailable" and entry["error"]
        assert preview["code"]["possible"] is False
        response = await client.post(f"/api/sessions/{fresh['id']}/rewind", json={"before_event_id": "u1", "code": True})
        assert response.status_code == 409
    assert (outside / "c.txt").read_text() == "c1"


async def test_code_rewind_after_a_conversation_rewind_includes_the_removed_turns(runtime):
    app, manager, session, _ = runtime
    chat = standard(runtime)
    async with await client_for(app) as client:
        response = await client.post(f"/api/sessions/{session['id']}/rewind",
                                     json={"before_event_id": "u3", "conversation": True})
        assert response.status_code == 200, response.text
        assert chat.files() == {"a.txt": "a3", "b.txt": "b3"}
        response = await client.post(f"/api/sessions/{session['id']}/rewind", json={"before_event_id": "u2", "code": True})
        assert response.status_code == 200, response.text
        assert response.json()["files"] == [{"path": "a.txt", "status": "restore"}, {"path": "b.txt", "status": "restore"}]
    assert chat.files() == {"a.txt": "a1", "b.txt": None}


async def test_conversation_rewind_in_place_keeps_the_chat_and_writes_an_archived_backup(runtime):
    app, manager, session, _ = runtime
    chat = standard(runtime)
    session = manager.store.get(session["id"])
    session.update(pinned=True, permission_mode="acceptEdits", allowed_directories=["/srv/x"],
                   allow_rules=[{"tool": "edits"}], active_skills=["review"], file_views={"/srv/x/a.txt": "seen"},
                   queued_messages=[{"id": "q", "text": "Later"}], pending_context=[{"command": "ls", "output": "x"}],
                   inference_calls=[{"id": "call-1", "purpose": "agent", "status": "completed"}],
                   instruction_directories=["docs"], tool_profile="read_only",
                   worktree={"id": "wt-1", "branch": "chat/x", "source_workspace": "/srv/x"})
    manager.store.save(session)
    async with await client_for(app) as client:
        response = await client.post(f"/api/sessions/{session['id']}/rewind",
                                     json={"before_event_id": "u2", "conversation": True})
        assert response.status_code == 200, response.text
        body = response.json()
    assert body["session"]["id"] == session["id"] and body["files"] == [] and body["resent"] is False
    rewound = manager.store.get(session["id"])
    assert [event["id"] for event in rewound["events"]] == ["u1", "a1"]
    assert rewound["wire"] == [{"role": "user", "content": "Turn 1"}, {"role": "assistant", "content": "Done."}]
    assert rewound["file_views"] == {}
    assert rewound["queued_messages"] == [{"id": "q", "text": "Later"}]
    assert rewound["pending_context"] == [{"command": "ls", "output": "x"}]
    assert rewound["inference_calls"] == [{"id": "call-1", "purpose": "agent", "status": "completed"}]
    assert rewound["permission_mode"] == "acceptEdits" and rewound["allow_rules"] == [{"tool": "edits"}]
    assert rewound["pinned"] is True and rewound["terminal_reason"] == "completed"
    assert chat.files() == {"a.txt": "a3", "b.txt": "b3"}  # Conversation only: files stay.
    backup = manager.store.get(body["backup_session_id"])
    assert backup["title"] == "Edits (before rewind)" and backup["archived"] is True and "pinned" not in backup
    assert [event["id"] for event in backup["events"]] == [event["id"] for event in session["events"]]
    assert backup["wire"] == session["wire"]
    assert backup["permission_mode"] == "manual"
    assert backup["allowed_directories"] == backup["allow_rules"] == backup["active_skills"] == []
    assert backup.get("inference_calls", []) == [] and backup.get("command_jobs", []) == []
    for key in ("queued_messages", "pending_context", "file_views", "instruction_directories", "context_info"):
        assert not backup.get(key)
    assert backup["tool_profile"] == "read_only"
    # Display metadata, not authority: the backup is grouped with its project and found next to its worktree.
    assert backup["worktree"] == {"id": "wt-1", "branch": "chat/x", "source_workspace": "/srv/x"}
    assert backup["provenance"]["kind"] == "rewind_backup"
    assert backup["provenance"]["source_session_id"] == session["id"]
    assert backup["created"] == session["created"]


async def test_rewind_both_and_resend_sends_the_cut_history_plus_the_new_message(runtime, monkeypatch):
    app, manager, session, _ = runtime
    chat = standard(runtime)
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"New answer."},"finish_reason":"stop"}]}'
                                        "\n\ndata: [DONE]\n\n")
    client_class = httpx.AsyncClient
    async with await client_for(app) as client:
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(
            **kwargs, transport=httpx.MockTransport(gateway)))
        response = await client.post(f"/api/sessions/{session['id']}/rewind", json={
            "before_event_id": "u2", "conversation": True, "code": True, "resend_text": "  Do it differently  "})
        monkeypatch.undo()
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["resent"] is True and body["backup_session_id"]
        await manager.tasks[session["id"]]
    assert chat.files() == {"a.txt": "a1", "b.txt": None}
    assert len(requests) == 1
    assert requests[0]["messages"][1:] == [{"role": "user", "content": "Turn 1"}, {"role": "assistant", "content": "Done."},
                                           {"role": "user", "content": "Do it differently"}]
    saved = manager.store.get(session["id"])
    assert [event["text"] for event in saved["events"] if event["type"] == "user"] == ["Turn 1", "Do it differently"]


def gateway_for(monkeypatch, requests):
    """Answers every model request with 'Answer.' and records the request bodies."""
    async def gateway(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"Answer."},"finish_reason":"stop"}]}'
                                        "\n\ndata: [DONE]\n\n")
    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(
        **{"transport": httpx.MockTransport(gateway), **kwargs}))


async def idle(manager, session_id):
    for _ in range(500):
        if not manager.busy_task(session_id) and not [
                task for task in manager.__dict__.get("_queue_tasks", ()) if not task.done()]:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("The chat did not become idle.")


async def test_a_message_sent_as_plain_text_is_resent_as_plain_text(runtime, monkeypatch):
    """Retry and Edit of a message sent as plain text send it again without a command, skill or @ file."""
    app, manager, session, project = runtime
    standard(runtime)
    skill = project / ".agents/skills/handoff/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Handoff rules.")
    (project / "notes.txt").write_text("Notes.")
    requests = []
    async with await client_for(app) as client:
        gateway_for(monkeypatch, requests)
        response = await client.post(f"/api/sessions/{session['id']}/rewind", json={
            "before_event_id": "u3", "conversation": True, "resend_text": "/handoff of @notes.txt", "resend_verbatim": True})
        assert response.status_code == 200 and response.json()["resent"] is True, response.text
        await idle(manager, session["id"])
        monkeypatch.undo()
    assert requests[-1]["messages"][-1] == {"role": "user", "content": "/handoff of @notes.txt"}
    user = [event for event in manager.store.get(session["id"])["events"] if event["type"] == "user"][-1]
    assert user["verbatim"] is True and "skills" not in user and "attachments" not in user


async def test_a_resend_with_a_refused_file_changes_nothing(runtime):
    """The resend's own checks run before the backup and the cut: a 400 leaves the chat as it was."""
    app, manager, session, project = runtime
    chat = standard(runtime)
    (project / ".env").write_text("TOKEN=abc\n")
    before = jsonl(manager, session["id"])
    async with await client_for(app) as client:
        for text, message in (("Turn 2, see @.env", "@.env: Credential files are excluded from file tools."),
                              (" ".join(f"@f{n}.txt" for n in range(6)), "Attach at most 5 files to one message.")):
            if "f0" in text:
                for n in range(6):
                    (project / f"f{n}.txt").write_text("x\n")
            response = await client.post(f"/api/sessions/{session['id']}/rewind", json={
                "before_event_id": "u2", "conversation": True, "code": True, "resend_text": text})
            assert (response.status_code, response.json()["detail"]) == (400, message)
    assert jsonl(manager, session["id"]) == before
    assert len(manager.store.list()) == 1  # No archived backup.
    assert chat.files() == {"a.txt": "a3", "b.txt": "b3"}
    assert session["id"] not in manager.tasks


async def test_a_resend_with_a_command_or_skill_that_cannot_be_used_changes_nothing(runtime):
    """A command template that cannot be used (a broken file, a refused @ file in its text) or skills over
    the per-message limits are refused before the backup and the cut, as for a typed @ file."""
    app, manager, session, project = runtime
    chat = standard(runtime)
    (project / ".env").write_text("TOKEN=abc\n")
    folder = project / ".agents/commands"
    folder.mkdir(parents=True)
    (folder / "broken.md").write_text("---\ndescription: never closed\n")
    (folder / "env.md").write_text("Look at @.env")
    for skill_id in ("s1", "s2", "s3", "s4"):
        skill = project / ".agents/skills" / skill_id / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"Rules {skill_id}.")
    before = jsonl(manager, session["id"])
    async with await client_for(app) as client:
        for text, message in (("/broken", "/broken could not be used: .agents/commands/broken.md: The frontmatter has no closing"),
                              ("/env", "@.env: Credential files are excluded from file tools."),
                              ("Use /s1 /s2 /s3 /s4", "Select at most three skills in one message.")):
            response = await client.post(f"/api/sessions/{session['id']}/rewind", json={
                "before_event_id": "u2", "conversation": True, "resend_text": text})
            assert response.status_code == 400 and response.json()["detail"].startswith(message), response.text
    assert jsonl(manager, session["id"]) == before
    assert len(manager.store.list()) == 1  # No archived backup.
    assert chat.files() == {"a.txt": "a3", "b.txt": "b3"}
    assert session["id"] not in manager.tasks


async def test_a_resend_attaches_again_the_command_output_the_removed_message_carried(runtime, monkeypatch):
    """Retry (and Edit, and a conversation rewind) of a message sent with ! output: the output waits
    again, so the resend carries it as the first send did."""
    app, manager, session, _ = runtime
    requests = []
    gateway_for(monkeypatch, requests)
    manager.start_shell(session["id"], "echo UNIQUE-SHELL-OUTPUT; exit 1")
    await idle(manager, session["id"])
    manager.start(session["id"], "Fix the failure above")
    await idle(manager, session["id"])
    first = requests[-1]["messages"][-1]["content"]
    assert "UNIQUE-SHELL-OUTPUT" in first and not manager.store.get(session["id"]).get("pending_context")
    user = [event for event in manager.store.get(session["id"])["events"] if event["type"] == "user"][-1]
    note = {"kind": "shell", "command": "echo UNIQUE-SHELL-OUTPUT; exit 1", "exit_code": 1, "text": "UNIQUE-SHELL-OUTPUT\n"}
    async with await client_for(app) as client:
        # A conversation-only rewind: the output waits for the next message again.
        preview = await client.get(f"/api/sessions/{session['id']}/rewind-preview", params={"before_event_id": user["id"]})
        assert preview.json()["conversation"]["possible"] is True
        response = await client.post(f"/api/sessions/{session['id']}/rewind", json={
            "before_event_id": user["id"], "conversation": True})
        assert response.status_code == 200, response.text
        assert response.json()["session"]["pending_context"] == [{key: note[key] for key in ("kind", "command", "exit_code")}]
        assert manager.store.get(session["id"])["pending_context"] == [note]
        manager.start(session["id"], "Fix the failure above")
        await idle(manager, session["id"])
        user = [event for event in manager.store.get(session["id"])["events"] if event["type"] == "user"][-1]
        # What Retry sends.
        response = await client.post(f"/api/sessions/{session['id']}/rewind", json={
            "before_event_id": user["id"], "conversation": True, "resend_text": user["text"]})
        assert response.status_code == 200 and response.json()["resent"] is True
        await idle(manager, session["id"])
    nonce = re.compile(r"\b[0-9a-f]{12}\b")  # New for each message.
    assert nonce.sub("N", requests[-1]["messages"][-1]["content"]) == nonce.sub("N", first)
    saved = manager.store.get(session["id"])
    retried = [event for event in saved["events"] if event["type"] == "user"]
    assert len(retried) == 1 and retried[0]["attachments"] == [{"kind": "shell", "command": note["command"], "exit_code": 1}]
    assert not saved.get("pending_context")


async def test_an_edit_of_a_finished_subagent_chats_first_message_attaches_files_and_output(runtime, monkeypatch):
    """Only the delegated prompt itself skips @ and ! expansion; after the delegation ended, an Edit or
    Retry of that first message is the user's own message."""
    app, manager, _, project = runtime
    (project / "notes.txt").write_text("hello notes\n")
    requests = []
    gateway_for(monkeypatch, requests)
    child = manager.store.create(manager.settings.values)
    child.update(title="Child", title_generated=True, is_subagent=True, parent_session_id="parent-x",
                 parent_event_id="evt-x", parent_call_id="call-x", delegation={"status": "running"})
    manager.store.save(child)
    manager.start(child["id"], "Delegated task, see @notes.txt")
    await idle(manager, child["id"])
    assert requests[-1]["messages"][-1]["content"] == "Delegated task, see @notes.txt"
    saved = manager.store.get(child["id"])
    saved["delegation"] = {"status": "completed", "terminal_reason": "completed"}
    saved["pending_context"] = [{"kind": "shell", "command": "ls", "exit_code": 0, "text": "UNIQUE-LS-OUTPUT"}]
    manager.store.save(saved)
    first = next(event for event in saved["events"] if event["type"] == "user")
    async with await client_for(app) as client:
        response = await client.post(f"/api/sessions/{child['id']}/rewind", json={
            "before_event_id": first["id"], "conversation": True, "resend_text": "Redo it, see @notes.txt"})
        assert response.status_code == 200 and response.json()["resent"] is True, response.text
        await idle(manager, child["id"])
    sent = requests[-1]["messages"][-1]["content"]
    assert "UNIQUE-LS-OUTPUT" in sent and "hello notes" in sent
    saved = manager.store.get(child["id"])
    assert not saved.get("pending_context")
    user = next(event for event in saved["events"] if event["type"] == "user")
    assert "origin" not in user and saved["turn_delegated"] is False
    assert [item["kind"] for item in user["attachments"]] == ["file", "shell"]


async def test_request_validation(runtime):
    app, manager, session, _ = runtime
    standard(runtime)
    async with await client_for(app) as client:
        url = f"/api/sessions/{session['id']}/rewind"
        cases = [({"before_event_id": "u2"}, "Choose what to rewind."),
                 ({"before_event_id": "u2", "code": True, "resend_text": "x"}, "Resending needs a conversation rewind."),
                 ({"before_event_id": "u2", "conversation": True, "resend_text": " \n "}, "Enter a message."),
                 ({"before_event_id": "a2", "conversation": True}, "Choose one of your messages in this conversation.")]
        for body, message in cases:
            response = await client.post(url, json=body)
            assert (response.status_code, response.json()["detail"]) == (400, message)
        assert (await client.post(url, json={"before_event_id": "u2", "conversation": "yes"})).status_code == 422
    assert len(manager.store.list()) == 1


async def test_a_failed_save_after_files_were_rewound_says_so(runtime):
    app, manager, session, _ = runtime
    chat = standard(runtime)
    save = manager.store.save

    def failing(target):
        if target["id"] == session["id"]:
            raise OSError(28, "No space left on device")
        save(target)
    manager.store.save = failing
    async with await client_for(app) as client:
        response = await client.post(f"/api/sessions/{session['id']}/rewind",
                                     json={"before_event_id": "u2", "conversation": True, "code": True})
    assert response.status_code == 500
    message = response.json()["detail"]["message"]
    assert message.startswith("Files were rewound, but the conversation could not be saved:")
    assert message.endswith("Rewound files can be restored from Agent tools → Checkpoints.")
    assert chat.files() == {"a.txt": "a1", "b.txt": None}


async def test_a_failed_backup_changes_nothing(runtime):
    app, manager, session, _ = runtime
    chat = standard(runtime)
    before = jsonl(manager, session["id"])

    def failing(sessions):
        raise OSError(28, "No space left on device")
    manager.store.insert_sessions = failing
    async with await client_for(app) as client:
        response = await client.post(f"/api/sessions/{session['id']}/rewind",
                                     json={"before_event_id": "u2", "conversation": True, "code": True})
    assert response.status_code == 400
    assert chat.files() == {"a.txt": "a3", "b.txt": "b3"}
    assert jsonl(manager, session["id"]) == before


async def test_busy_chats_block_a_rewind(runtime):
    app, manager, session, _ = runtime
    standard(runtime)
    other = manager.store.create(manager.settings.values)
    other["title"] = "Neighbour"
    manager.store.save(other)
    async with await client_for(app) as client:
        manager.statuses[session["id"]] = "running"
        response = await client.post(f"/api/sessions/{session['id']}/rewind",
                                     json={"before_event_id": "u2", "conversation": True})
        assert response.status_code == 409 and response.json()["detail"]["session_id"] == session["id"]
        manager.statuses[session["id"]] = "idle"
        manager.statuses[other["id"]] = "awaiting_approval"
        response = await client.post(f"/api/sessions/{session['id']}/rewind", json={"before_event_id": "u2", "code": True})
        assert response.status_code == 409
        assert response.json()["detail"]["session_id"] == other["id"]
        assert "rewinding files" in response.json()["detail"]["message"]
        # A preview is read-only and works while busy.
        manager.statuses[session["id"]] = "running"
        preview = await client.get(f"/api/sessions/{session['id']}/rewind-preview", params={"before_event_id": "u2"})
        assert preview.status_code == 200


async def test_a_legacy_unaligned_conversation_is_refused_without_changes(runtime):
    app, manager, session, _ = runtime
    chat = standard(runtime)
    session = manager.store.get(session["id"])
    for event in session["events"]:
        event.pop("in_wire", None)
    session["events"][2]["text"] = "Edited in an older version"
    manager.store.save(session)
    before = jsonl(manager, session["id"])
    async with await client_for(app) as client:
        response = await client.post(f"/api/sessions/{session['id']}/rewind",
                                     json={"before_event_id": "u2", "conversation": True, "code": True})
        assert response.status_code == 400
        assert response.json()["detail"].startswith("This conversation was saved by an earlier version")
        preview = (await client.get(f"/api/sessions/{session['id']}/rewind-preview",
                                    params={"before_event_id": "u2"})).json()
        assert preview["conversation"]["possible"] is False and preview["code"]["possible"] is True
    assert chat.files() == {"a.txt": "a3", "b.txt": "b3"}
    assert jsonl(manager, session["id"]) == before
    assert len(manager.store.list()) == 1


async def test_move_and_delete_are_rewound_through_their_checkpoints(runtime):
    app, manager, session, project = runtime
    (project / "a.txt").write_text("a0")
    (project / "gone.txt").write_text("keep me")
    chat = Chat(manager, session, project)
    chat.turn(1)
    manager.checkpoints.apply_move(chat.tools, "a.txt", "moved.txt", session_id=session["id"], turn_id="u1")
    manager.checkpoints.apply_delete(chat.tools, "gone.txt", session_id=session["id"], turn_id="u1")
    assert not (project / "a.txt").exists() and not (project / "gone.txt").exists()
    async with await client_for(app) as client:
        response = await client.post(f"/api/sessions/{session['id']}/rewind", json={"before_event_id": "u1", "code": True})
        assert response.status_code == 200, response.text
    assert (project / "a.txt").read_text() == "a0" and (project / "gone.txt").read_text() == "keep me"
    assert not (project / "moved.txt").exists()


def test_store_is_reachable(tmp_path):
    # The JSONL helper reads the real conversation file (guards the byte-identical checks above).
    store = Store(tmp_path / "s.sqlite3")
    try:
        session = store.create({"workspace": str(tmp_path), "model": "test-model"})
        assert store._path(session["id"]).read_bytes()
    finally:
        store.db.close()
    assert _snapshot(tmp_path / "missing")["exists"] is False
