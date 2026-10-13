"""Approval quick wins: "Allow for this chat" rules, decline notes, and 409s that name the blocking chat."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.instructions import load_project_instructions
from local_agent.planning import DelegateManager
from local_agent.portability import export_bundle
from local_agent.store import Store
from local_agent.tools import WorkspaceTools

WAIT_FOR = asyncio.wait_for


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    store = Store(settings.state_dir / "rules.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    tools = WorkspaceTools(str(project))
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance["text"], guidance["warnings"])
    tools.skill_signature = ""
    yield manager, session, project, tools
    store.db.close()


async def card(manager):
    """The (session id, event id) of the next pending approval card."""
    for _ in range(300):
        if manager.pending:
            return next(iter(manager.pending))
        await asyncio.sleep(0.01)
    raise AssertionError("Expected an approval card")


async def answer(manager, *args, **kwargs):
    session_id, event_id = await card(manager)
    manager.decide(session_id, event_id, *args, **kwargs)
    while (session_id, event_id) in manager.pending:  # Until the card has its answer.
        await asyncio.sleep(0.01)
    return event_id


async def run(manager, session, tools, name, arguments, call_id="call"):
    """Run a tool that must finish without an approval card."""
    output = await WAIT_FOR(manager.execute_tool(session, tools, name, arguments, call_id), 10)
    assert not manager.pending
    return output, session["events"][-1] if session["events"][-1].get("call_id") == call_id else next(
        event for event in reversed(session["events"]) if event.get("call_id") == call_id)


def demo_connection(manager, session, names=("mcp__demo__echo", "mcp__demo__other")):
    called = []

    class Connection:
        tool_names = set(names)
        definitions = [{"function": {"name": name, "parameters": {"type": "object"}}} for name in names]

        async def call(self, name, arguments):
            called.append(name)
            return {"ok": True}
    manager.extension_connections[session["id"]] = Connection()
    return called


# "Allow for this chat"

class Socket:
    def __init__(self):
        self.messages = []

    async def send_json(self, data):
        self.messages.append(data)


async def test_an_allowed_command_runs_again_only_with_the_exact_same_text(runtime):
    manager, session, project, tools = runtime
    socket = Socket()
    manager.listeners[session["id"]] = {socket}
    first = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": "  touch one.txt\n"}, "first"))
    await answer(manager, True, remember=True)
    await first
    assert session["allow_rules"] == [{"tool": "run_command", "command": "touch one.txt"}]
    assert {"type": "permissions", "permission_mode": "manual", "allowed_directories": [],
            "allow_rules": session["allow_rules"], "disabled_tools": [], "project_rules": [], "user_rules": [],
            "rules_problem": None} in socket.messages
    assert manager.store.get(session["id"])["allow_rules"] == session["allow_rules"]
    (project / "one.txt").unlink()

    # The same command, apart from surrounding whitespace, runs without a card.
    output, event = await run(manager, session, tools, "run_command", {"command": "touch one.txt "}, "again")
    assert event["allowed_by_rule"] is True and event["state"] == "completed" and "approval" not in event
    assert (project / "one.txt").exists()

    # Different text still asks: no prefixes, no reformatting.
    for index, command in enumerate(("touch one.txt && touch two.txt", "touch  one.txt", "touch one.txt two.txt", "TOUCH one.txt")):
        task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": command}, f"other{index}"))
        await answer(manager, False)
        assert (await task).startswith("User declined this action.")
        assert "allowed_by_rule" not in session["events"][-1]
    assert not (project / "two.txt").exists()


@pytest.mark.parametrize("mode", ["manual", "auto", "acceptEdits"])
async def test_rules_apply_in_asking_modes_but_plan_still_denies(runtime, mode):
    manager, session, project, tools = runtime
    session.update(permission_mode=mode, allow_rules=[{"tool": "run_command", "command": "touch made.txt"}])
    _, event = await run(manager, session, tools, "run_command", {"command": "touch made.txt"})
    assert event["allowed_by_rule"] is True and (project / "made.txt").exists()

    (project / "made.txt").unlink()
    session["permission_mode"] = "plan"
    output, event = await run(manager, session, tools, "run_command", {"command": "touch made.txt"}, "plan")
    assert output.startswith("Plan mode is read-only") and event["state"] == "rejected"
    assert "allowed_by_rule" not in event and not (project / "made.txt").exists()


async def test_bypass_is_unchanged_and_never_marks_a_rule(runtime):
    manager, session, project, tools = runtime
    session.update(permission_mode="bypassPermissions", allow_rules=[{"tool": "edits"}])
    _, event = await run(manager, session, tools, "write_file", {"path": "a.txt", "content": "x"})
    assert event["state"] == "completed" and "allowed_by_rule" not in event


async def test_an_edits_rule_covers_writes_edits_and_the_activity_log_only(runtime):
    manager, session, project, tools = runtime
    first = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": "a.txt", "content": "one\n"}, "w1"))
    await answer(manager, True, remember=True)
    await first
    assert session["allow_rules"] == [{"tool": "edits"}]
    await run(manager, session, tools, "edit_file", {"path": "a.txt", "old_text": "one", "new_text": "two"}, "e1")
    await run(manager, session, tools, "insert_activity_log", {"path": "log.md"}, "log")
    assert (project / "a.txt").read_text() == "two\n" and (project / "log.md").exists()
    assert all(event.get("allowed_by_rule") for event in session["events"][1:] if event.get("call_id") in ("e1", "log"))
    # Commands are not edits.
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": "touch x"}, "cmd"))
    await answer(manager, False)
    await task
    assert not (project / "x").exists()


async def test_an_edits_rule_keeps_credential_exclusions_and_folder_access_cards(runtime, tmp_path):
    manager, session, project, tools = runtime
    session["allow_rules"] = [{"tool": "edits"}]
    output, event = await run(manager, session, tools, "write_file", {"path": ".env", "content": "TOKEN=x"}, "secret")
    assert "excluded" in output and event["state"] == "error" and not (project / ".env").exists()
    outside = tmp_path / "outside"
    outside.mkdir()
    task = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": str(outside / "a.txt"), "content": "x"}, "out"))
    _, event_id = await card(manager)
    access = next(event for event in session["events"] if event["id"] == event_id)
    assert access["name"] == "access_directory"
    # A folder grant is already per chat, so this card cannot become a rule.
    with pytest.raises(ValueError, match="cannot be allowed for the whole chat"):
        manager.decide(session["id"], event_id, True, remember=True)
    manager.decide(session["id"], event_id, False)
    assert (await task).startswith("User declined folder access.")
    assert session["allow_rules"] == [{"tool": "edits"}] and not (outside / "a.txt").exists()


async def test_project_guidance_checks_still_apply_to_allowed_actions(runtime):
    manager, session, project, tools = runtime
    session["allow_rules"] = [{"tool": "edits"}]
    (project / "AGENTS.md").write_text("New project rules.\n")
    output, event = await run(manager, session, tools, "write_file", {"path": "a.txt", "content": "x"})
    assert output.startswith("Project instructions were discovered or changed") and not (project / "a.txt").exists()


async def test_mcp_and_hook_rules_match_the_exact_tool_and_hook(runtime):
    manager, session, project, tools = runtime
    called = demo_connection(manager, session)
    first = asyncio.create_task(manager.execute_tool(session, tools, "mcp__demo__echo", {"value": 1}, "m1"))
    await answer(manager, True, remember=True)
    await first
    _, event = await run(manager, session, tools, "mcp__demo__echo", {"value": 2}, "m2")
    assert event["allowed_by_rule"] is True and called == ["mcp__demo__echo", "mcp__demo__echo"]
    other = asyncio.create_task(manager.execute_tool(session, tools, "mcp__demo__other", {}, "m3"))
    await answer(manager, False)
    await other
    assert called == ["mcp__demo__echo", "mcp__demo__echo"]

    manager.extensions.update_config({"hooks": [
        {"id": "check", "enabled": True, "event": "before_tool", "command": "true", "tools": ["mcp__demo__echo"]},
        {"id": "audit", "enabled": True, "event": "after_tool", "command": "true", "tools": ["mcp__demo__echo"]}]})
    task = asyncio.create_task(manager.execute_tool(session, tools, "mcp__demo__echo", {"value": 3}, "m4"))
    hook_event = await answer(manager, True, remember=True)
    assert next(event for event in session["events"] if event["id"] == hook_event)["name"] == "hook_before_tool"
    await answer(manager, True)  # The after-tool hook is a different hook.
    await task
    assert {"tool": "hook", "hook": "check"} in session["allow_rules"]
    assert {"tool": "hook", "hook": "audit"} not in session["allow_rules"]
    task = asyncio.create_task(manager.execute_tool(session, tools, "mcp__demo__echo", {"value": 4}, "m5"))
    await answer(manager, False)  # Only the "audit" hook asks now.
    await task
    hooks = [event for event in session["events"] if event.get("name") == "hook_before_tool"]
    assert hooks[-1]["allowed_by_rule"] is True and hooks[-1]["state"] == "completed"


async def test_remember_is_only_for_approvals_and_limited_to_one_hundred_rules(runtime):
    manager, session, _, tools = runtime
    session["allow_rules"] = [{"tool": "run_command", "command": f"echo {index}"} for index in range(100)]
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": "echo new"}, "call"))
    session_id, event_id = await card(manager)
    with pytest.raises(ValueError, match="Only an approval"):
        manager.decide(session_id, event_id, False, remember=True)
    with pytest.raises(ValueError, match="already allows 100"):
        manager.decide(session_id, event_id, True, remember=True)
    with pytest.raises(ValueError, match="only be sent with Decline"):
        manager.decide(session_id, event_id, True, note="Fine")
    manager.decide(session_id, event_id, True)
    await task
    assert len(session["allow_rules"]) == 100


# Decline with a note

async def test_a_decline_note_reaches_the_model_and_the_card(runtime):
    manager, session, project, tools = runtime
    task = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": "a.txt", "content": "x"}, "call"))
    await answer(manager, False, note="  Use the parser module instead.  ")
    output = await task
    assert output == ("User declined this action. Their note: Use the parser module instead. "
                      "Do not retry it without a new instruction.")
    event = session["events"][-1]
    assert (event["state"], event["approval"], event["decline_note"]) == ("rejected", "declined", "Use the parser module instead.")
    assert manager.store.get(session["id"])["events"][-1]["decline_note"] == "Use the parser module instead."
    # The note survives an export of the history.
    exported = export_bundle([session], session["id"])["sessions"][0]["events"][-1]
    assert exported["decline_note"] == "Use the parser module instead."

    # Without a note the result is unchanged.
    task = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": "a.txt", "content": "x"}, "plain"))
    await answer(manager, False, note="   ")
    assert await task == "User declined this action. Do not retry it without a new instruction."
    assert "decline_note" not in session["events"][-1]


async def test_decline_notes_for_mcp_folder_access_and_hooks(runtime, tmp_path):
    manager, session, project, tools = runtime
    demo_connection(manager, session)
    task = asyncio.create_task(manager.execute_tool(session, tools, "mcp__demo__echo", {}, "mcp"))
    await answer(manager, False, note="Not on production.")
    assert await task == ("User declined this MCP action. Their note: Not on production. "
                          "Do not retry it without a new instruction.")

    outside = tmp_path / "outside"
    outside.mkdir()
    task = asyncio.create_task(manager.execute_tool(session, tools, "list_files", {"path": str(outside)}, "list"))
    await answer(manager, False, note="That folder is private.")
    assert await task == ("User declined folder access. Their note: That folder is private. "
                          "Do not retry or use a command to circumvent this decision.")
    listing = next(event for event in session["events"] if event.get("call_id") == "list")
    assert listing["decline_note"] == "That folder is private."

    manager.extensions.update_config({"hooks": [{"id": "check", "enabled": True, "event": "before_tool", "command": "true"}]})
    session["permission_mode"] = "acceptEdits"
    task = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": "a.txt", "content": "x"}, "hooked"))
    await answer(manager, False, note="Skip the hook today.")
    output = json.loads(await task)["message"]  # A blocked tool is a structured error for the model.
    assert output.endswith(": User declined this hook. Their note: Skip the hook today. "
                           "Do not retry write_file without a new instruction.") and not (project / "a.txt").exists()


# Rules over the API: persistence, removal, and copies

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    destination = tmp_path / "destination"
    destination.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    return settings, project, destination


def connect(app):
    client = TestClient(app)
    client.__enter__()
    client.headers["X-Local-Token"] = client.get("/api/bootstrap").json()["token"]
    return client


def test_approval_body_is_strict(api):
    settings, _, _ = api
    app = create_app(settings)
    client = connect(app)
    try:
        sid = client.post("/api/sessions").json()["id"]
        route = f"/api/sessions/{sid}/approvals/missing"
        for body in ({"allowed": True, "remember": "yes"}, {"allowed": True, "remember": 1},
                     {"allowed": False, "note": "x" * 2001}, {"allowed": False, "note": 5}):
            assert client.post(route, json=body).status_code == 422, body
        assert client.post(route, json={"allowed": False, "note": "x" * 2000}).status_code == 400  # Not pending.
    finally:
        client.__exit__(None, None, None)


async def test_approving_over_the_api_can_remember_the_action(api):
    settings, project, _ = api
    app = create_app(settings)
    manager = app.state.manager
    session = manager.store.create(settings.values)
    tools = WorkspaceTools(str(project))
    tools.instruction_signature, tools.skill_signature = ("", []), ""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
        task = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": "a.txt", "content": "x"}, "call"))
        session_id, event_id = await card(manager)
        route = f"/api/sessions/{session_id}/approvals/{event_id}"
        assert (await client.post(route, json={"allowed": False, "remember": True})).status_code == 400
        assert (await client.post(route, json={"allowed": True, "remember": True})).json() == {"ok": True}
        await task
    assert manager.store.get(session["id"])["allow_rules"] == [{"tool": "edits"}]
    assert (project / "a.txt").read_text() == "x"
    manager.store.db.close()


def test_rules_are_listed_survive_a_restart_and_can_be_removed_while_busy(api):
    settings, _, _ = api
    rules = [{"tool": "run_command", "command": "make test"}, {"tool": "edits"}, {"tool": "hook", "hook": "check"}]
    app = create_app(settings)
    client = connect(app)
    try:
        session = app.state.manager.store.create(settings.values)
        session.update(title="Fix the parser", allow_rules=list(rules))
        app.state.manager.store.save(session)
        sid = session["id"]
    finally:
        client.__exit__(None, None, None)

    app = create_app(settings)
    client = connect(app)
    try:
        assert client.get(f"/api/sessions/{sid}").json()["allow_rules"] == rules
        # Rules stay out of the sidebar list and the small header record that list reads.
        assert [item for item in client.get("/api/sessions").json() if "allow_rules" in item] == []
        header = app.state.manager.store._path(sid).read_text(encoding="utf-8").splitlines()[0]
        assert "make test" not in header and "allow_rules" not in header
        assert client.post("/api/sessions").json()["allow_rules"] == []
        route = f"/api/sessions/{sid}/allow-rules"
        assert client.request("DELETE", route, json={"tool": "edits", "extra": 1}).status_code == 422
        # Removing a rule only makes the agent ask again, so it is allowed while the chat is busy.
        app.state.manager.statuses[sid] = "awaiting_approval"
        busy = client.request("DELETE", route, json={"tool": "hook", "hook": "check"})
        assert busy.status_code == 200 and busy.json()["status"] == "awaiting_approval"
        assert busy.json()["allow_rules"] == rules[:2]
        rules = rules[:2]
        app.state.manager.statuses[sid] = "idle"
        with client.websocket_connect(f"/api/sessions/{sid}/stream",
                                      subprotocols=["local-workspace", client.headers["X-Local-Token"]]) as socket:
            assert socket.receive_json()["session"]["allow_rules"] == rules  # The snapshot carries the rules.
            assert socket.receive_json()["type"] == "changes"
            removed = client.request("DELETE", route, json={"tool": "run_command", "command": "make test"}).json()
            assert removed["allow_rules"] == rules[1:]
            message = socket.receive_json()
            assert message["type"] == "permissions" and message["allow_rules"] == rules[1:]
        assert app.state.manager.store.get(sid)["allow_rules"] == rules[1:]
    finally:
        client.__exit__(None, None, None)


async def test_fork_and_import_start_without_rules(api):
    settings, _, destination = api
    app = create_app(settings)
    manager = app.state.manager
    source = manager.store.create(settings.values)
    source.update(title="Done", terminal_reason="completed", allow_rules=[{"tool": "edits"}],
                  wire=[{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello."}],
                  events=[{"id": "u", "type": "user", "text": "Hi"},
                          {"id": "t", "type": "tool", "name": "write_file", "state": "completed", "allowed_by_rule": True,
                           "input": {"path": "a.txt"}, "output": "ok"},
                          {"id": "a", "type": "assistant", "text": "Hello."}])
    manager.store.save(source)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
        bundle = (await client.get(f"/api/sessions/{source['id']}/export")).json()
        assert "allow_rules" not in bundle["sessions"][0]
        assert bundle["sessions"][0]["events"][1]["allowed_by_rule"] is True
        imported = (await client.post("/api/sessions/import", params={"workspace": str(destination)}, json=bundle)).json()
        fork = (await client.post(f"/api/sessions/{source['id']}/fork")).json()
        assert imported["session"]["allow_rules"] == [] and fork["allow_rules"] == []
        assert manager.store.get(fork["id"])["allow_rules"] == []
        # A bundle cannot carry rules into a copy.
        bundle["sessions"][0]["allow_rules"] = [{"tool": "edits"}]
        assert (await client.post("/api/sessions/import", params={"workspace": str(destination)}, json=bundle)).status_code == 400
    manager.store.db.close()


async def test_a_subagent_inherits_its_parents_rules_without_sharing_them(runtime):
    manager, parent, project, _ = runtime
    parent.update(allow_rules=[{"tool": "run_command", "command": "touch child.txt"}])
    manager.store.save(parent)
    observed = {}

    async def worker(session, prompt):
        observed["rules"] = [dict(rule) for rule in session["allow_rules"]]
        session["allow_rules"].append({"tool": "edits"})
        await manager.event(session, "assistant", text="Done.")
    manager.run_databricks = worker
    result = await DelegateManager(manager).delegate(parent, "Make the file")
    assert observed["rules"] == [{"tool": "run_command", "command": "touch child.txt"}]
    assert manager.store.get(result["child_session_id"])["allow_rules"][-1] == {"tool": "edits"}
    assert manager.store.get(parent["id"])["allow_rules"] == [{"tool": "run_command", "command": "touch child.txt"}]


# 409 errors name the blocking chat

def test_busy_errors_name_the_blocking_chat_and_why(api):
    settings, project, _ = api
    app = create_app(settings)
    client = connect(app)
    manager = app.state.manager
    try:
        blocker = manager.store.create(settings.values)
        blocker["title"] = "Fix the parser"
        manager.store.save(blocker)
        selected = client.post("/api/sessions").json()["id"]
        manager.statuses[blocker["id"]] = "awaiting_approval"
        # Undo in another chat of the same workspace is blocked by the waiting one.
        undo = client.post(f"/api/sessions/{selected}/changes/undo", json={"files": [{"path": "a.txt"}]})
        assert undo.status_code == 409
        assert undo.json()["detail"] == {
            "message": "“Fix the parser” is waiting for your approval. Answer or stop it before undoing agent changes.",
            "session_id": blocker["id"], "title": "Fix the parser"}
        restore = client.post(f"/api/checkpoints/missing/restore?session_id={selected}", json={"expected_current_hash": "x"})
        assert restore.status_code == 409 and restore.json()["detail"]["session_id"] == blocker["id"]

        manager.statuses[blocker["id"]] = "running"
        cases = [("PUT", f"/api/sessions/{blocker['id']}/model", {"model": "other"}, "changing the model"),
                 ("POST", f"/api/sessions/{blocker['id']}/folders", {"path": str(project)}, "changing folder access"),
                 ("POST", f"/api/sessions/{blocker['id']}/compact", {}, "compacting context"),
                 ("POST", f"/api/sessions/{blocker['id']}/fork", None, "exporting or forking this conversation"),
                 ("GET", f"/api/sessions/{blocker['id']}/export", None, "exporting or forking this conversation"),
                 ("PUT", f"/api/sessions/{blocker['id']}/subagent-profile", {"tool_profile": "read_only"}, "changing this conversation")]
        for method, path, body, action in cases:
            response = client.request(method, path, json=body)
            assert response.status_code == 409, path
            assert response.json()["detail"] == {"message": f"“Fix the parser” is still running. Stop it before {action}.",
                                                 "session_id": blocker["id"], "title": "Fix the parser"}, path
        # Permission modes change at any time, also while a chat runs or compacts.
        assert client.put(f"/api/sessions/{blocker['id']}/permissions", json={"permission_mode": "plan"}).status_code == 200
        manager.statuses[blocker["id"]] = "compacting"
        compacting = client.put(f"/api/sessions/{blocker['id']}/permissions", json={"permission_mode": "manual"})
        assert compacting.status_code == 200 and compacting.json()["status"] == "compacting"
        manager.statuses[blocker["id"]] = "idle"
        assert client.put(f"/api/sessions/{blocker['id']}/permissions", json={"permission_mode": "plan"}).status_code == 200
    finally:
        client.__exit__(None, None, None)


async def test_a_running_command_names_the_chat_that_started_it(api):
    settings, project, _ = api
    app = create_app(settings)
    manager = app.state.manager
    owner = manager.store.create(settings.values)
    owner["title"] = "Long build"
    manager.store.save(owner)
    job = await manager.jobs.start(command="sleep 5", workspace=str(project), session_id=owner["id"], background=True)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            response = await client.post(f"/api/sessions/{owner['id']}/changes/undo", json={"files": [{"path": "a.txt"}]})
            assert response.status_code == 409
            assert response.json()["detail"] == {
                "message": "A command started in “Long build” is still running. Stop it in Terminal before undoing agent changes.",
                "session_id": owner["id"], "title": "Long build"}
    finally:
        await manager.jobs.stop(job["id"])
        await manager.jobs.shutdown()
        manager.store.db.close()


def test_rules_saved_in_an_older_header_record_still_load(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    try:
        session = store.create({"workspace": str(tmp_path), "model": "test-model"})
        path = store._path(session["id"])
        lines = path.read_text(encoding="utf-8").splitlines()
        header = json.loads(lines[0])
        header["data"]["allow_rules"] = [{"tool": "edits"}]
        path.write_text("\n".join([json.dumps(header), *lines[1:]]) + "\n", encoding="utf-8")
        assert store.get(session["id"])["allow_rules"] == [{"tool": "edits"}]
        store.save(store.get(session["id"]))
        assert "allow_rules" not in path.read_text(encoding="utf-8").splitlines()[0]
        assert store.get(session["id"])["allow_rules"] == [{"tool": "edits"}]
    finally:
        store.db.close()
