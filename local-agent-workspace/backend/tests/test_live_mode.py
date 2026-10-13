"""Changing the permission mode, rules or turned-off tools while a turn runs or a card waits."""
import asyncio
import json

import httpx
import pytest

from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.instructions import load_project_instructions
from local_agent.permissions import mode_prompt
from local_agent.tools import WorkspaceTools

SWITCHED = "Not run: the user switched this chat to {} mode while this card was waiting. "


@pytest.fixture
async def live(tmp_path, monkeypatch):
    """An app, an async client and a conversation whose turn is 'running' (so routes see the live session)."""
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    app = create_app(settings)
    manager = app.state.manager
    session = manager.store.create(settings.values)
    manager.live[session["id"]] = session
    tools = WorkspaceTools(str(project))
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance["text"], guidance["warnings"])
    tools.skill_signature = ""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
        yield manager, session, project, tools, client
    await manager.jobs.shutdown()
    manager.store.db.close()


async def card(manager, name=None):
    for _ in range(300):
        for key in manager.pending:
            event = next(item for item in manager.get(key[0])["events"] if item["id"] == key[1])
            if name is None or event["name"] == name:
                return key, event
        await asyncio.sleep(0.01)
    raise AssertionError("Expected an approval card")


def recorder(manager):
    """Replace command_tool; record how each command would run (argv and read_only, or the shell)."""
    calls = []

    async def command_tool(session, event, arguments, **options):
        calls.append({"command": arguments["command"], **options})
        return {"state": "completed", "exit_code": 0, "output": "recorded"}
    manager.command_tool = command_tool
    return calls


async def put_mode(client, session, mode, **extra):
    return await client.put(f"/api/sessions/{session['id']}/permissions", json={"permission_mode": mode, **extra})


async def test_switching_to_accept_edits_approves_a_waiting_write(live):
    manager, session, project, tools, client = live
    task = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": "a.txt", "content": "x"}, "w"))
    _, event = await card(manager)
    response = await put_mode(client, session, "acceptEdits")
    assert response.status_code == 200 and response.json()["permission_mode"] == "acceptEdits"
    await asyncio.wait_for(task, 5)
    assert (event["approval"], event["approved_by_mode"], event["state"]) == ("approved", "acceptEdits", "completed")
    assert (project / "a.txt").read_text() == "x"
    notice = [item["text"] for item in session["events"] if item["type"] == "notice"]
    assert "Permission mode changed to Accept edits." in notice
    assert manager.store.get(session["id"])["permission_mode"] == "acceptEdits"


async def test_switching_to_plan_blocks_a_waiting_command(live):
    manager, session, project, tools, client = live
    calls = recorder(manager)
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": "touch x"}, "c"))
    (_, event_id), event = await card(manager)
    assert (await put_mode(client, session, "plan")).status_code == 200
    output = await asyncio.wait_for(task, 5)
    assert output.startswith(SWITCHED.format("Plan") + "Plan mode is read-only.")
    assert (event["state"], event["approval"], event["blocked_reason"]) == ("rejected", "blocked", output)
    assert calls == []
    # A late answer to the card is refused.
    late = await client.post(f"/api/sessions/{session['id']}/approvals/{event_id}", json={"allowed": True})
    assert late.status_code == 400


async def test_bypass_needs_confirmation_and_then_approves_folder_access_without_a_grant(live, tmp_path):
    manager, session, _, tools, client = live
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.txt").write_text("hello\n")
    task = asyncio.create_task(manager.execute_tool(session, tools, "read_file", {"path": str(outside / "data.txt")}, "r"))
    _, access = await card(manager)
    assert access["name"] == "access_directory"
    response = await put_mode(client, session, "bypassPermissions")
    assert (response.status_code, response.json()["detail"]) == (400, "Confirm switching to Bypass permissions.")
    assert manager.pending and session["permission_mode"] == "manual"
    assert (await put_mode(client, session, "bypassPermissions", confirm=True)).status_code == 200
    output = json.loads(await asyncio.wait_for(task, 5))
    assert output["content"] == "1: hello"
    assert access["approved_by_mode"] == "bypassPermissions" and access["state"] == "completed"
    assert session.get("allowed_directories", []) == [] and tools.allowed_directories == []


async def test_a_write_waiting_on_folder_access_is_blocked_by_plan(live, tmp_path):
    manager, session, _, tools, client = live
    session["permission_mode"] = "acceptEdits"
    outside = tmp_path / "outside"
    outside.mkdir()
    task = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": str(outside / "a.txt"), "content": "x"}, "w"))
    _, access = await card(manager)
    assert (await put_mode(client, session, "plan")).status_code == 200
    output = await asyncio.wait_for(task, 5)
    assert output.startswith(SWITCHED.format("Plan") + "Plan mode is read-only.")
    assert access["approval"] == "blocked" and not (outside / "a.txt").exists()
    write = next(item for item in session["events"] if item.get("call_id") == "w")
    assert (write["state"], write["approval"]) == ("rejected", "blocked")


async def test_a_mode_switch_after_folder_access_is_seen_before_the_write(live, tmp_path):
    manager, session, _, tools, client = live
    session["permission_mode"] = "acceptEdits"
    outside = tmp_path / "outside"
    outside.mkdir()
    task = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": str(outside / "a.txt"), "content": "x"}, "w"))
    (sid, event_id), _ = await card(manager)
    # The user approves folder access, but switched to Manual first: the write now needs its own card.
    session["permission_mode"] = "manual"
    manager.decide(sid, event_id, True)
    _, write = await card(manager, "write_file")
    assert write["call_id"] == "w"
    assert (await put_mode(client, session, "plan")).status_code == 200
    assert (await asyncio.wait_for(task, 5)).startswith(SWITCHED.format("Plan"))
    assert not (outside / "a.txt").exists()


async def test_dont_ask_blocks_waiting_cards_and_folder_access(live, tmp_path):
    manager, session, project, tools, client = live
    recorder(manager)
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": "make"}, "c"))
    _, event = await card(manager)
    assert (await put_mode(client, session, "dontAsk")).status_code == 200
    output = await asyncio.wait_for(task, 5)
    assert output == SWITCHED.format("Don't ask") + (
        "Don't ask mode blocks actions that no rule allows, without showing an approval card. Continue without it "
        "and tell the user which action needs a rule or another mode.")
    assert event["approval"] == "blocked"
    outside = tmp_path / "outside"
    outside.mkdir()
    output = await manager.execute_tool(session, tools, "list_files", {"path": str(outside)}, "l")
    assert output == ("Not run: this path is outside the project and folder access needs your approval, which "
                      "Don't ask mode does not show.")
    listing = next(item for item in session["events"] if item.get("call_id") == "l")
    assert (listing["approval"], listing["blocked_reason"]) == ("blocked", output) and "declined" not in output
    assert not any(item.get("name") == "access_directory" for item in session["events"])
    # A waiting folder-access card is blocked with the same text.
    session["permission_mode"] = "manual"
    task = asyncio.create_task(manager.execute_tool(session, tools, "list_files", {"path": str(outside)}, "l2"))
    _, access = await card(manager)
    assert (await put_mode(client, session, "dontAsk")).status_code == 200
    assert await asyncio.wait_for(task, 5) == output
    assert access["approval"] == "blocked"


async def test_rule_changes_resolve_or_block_waiting_cards(live):
    manager, session, project, tools, client = live
    calls = recorder(manager)
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": "npm test"}, "c1"))
    _, event = await card(manager)
    added = await client.post("/api/permission-rules", json={"scope": "project", "session_id": session["id"], "rule": {
        "tool": "run_command", "behavior": "allow", "text": "npm test", "match": "exact"}})
    assert added.status_code == 200
    await asyncio.wait_for(task, 5)
    assert (event["approval"], event["allowed_by_rule"], event["rule_scope"]) == ("approved", True, "project")
    # Resolved by the rule, the command runs as the rule allows it: without a shell.
    assert calls == [{"command": "npm test", "argv": ["npm", "test"], "read_only": False}]

    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": "git push"}, "c2"))
    _, event = await card(manager)
    await client.post("/api/permission-rules", json={"scope": "user", "rule": {
        "tool": "run_command", "behavior": "deny", "text": "git push", "match": "prefix"}})
    output = await asyncio.wait_for(task, 5)
    assert output.startswith("Blocked by a deny rule for all projects") and event["approval"] == "blocked"

    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": "make"}, "c3"))
    _, event = await card(manager)
    disabled = await client.put(f"/api/sessions/{session['id']}/disabled-tools", json={"tools": ["run_command"]})
    assert disabled.status_code == 200
    assert await asyncio.wait_for(task, 5) == "The user turned off run_command in this chat. Use other tools."
    assert event["approval"] == "blocked" and len(calls) == 1


async def test_a_card_answered_with_a_project_rule_over_the_api_updates_other_chats(live):
    manager, session, project, tools, client = live
    calls = recorder(manager)
    other = manager.store.create(manager.settings.values)
    manager.live[other["id"]] = other
    first = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": "make lint"}, "a"))
    (sid, eid), _ = await card(manager)
    second = asyncio.create_task(manager.execute_tool(other, tools, "run_command", {"command": "make lint"}, "b"))
    while len(manager.pending) < 2:
        await asyncio.sleep(0.01)
    answer = await client.post(f"/api/sessions/{sid}/approvals/{eid}", json={"allowed": True, "scope": "project"})
    assert answer.json() == {"ok": True}
    await asyncio.wait_for(asyncio.gather(first, second), 5)
    blocked_by_rule = next(item for item in other["events"] if item.get("call_id") == "b")
    assert (blocked_by_rule["approval"], blocked_by_rule["rule_scope"]) == ("approved", "project")
    # The card the user approved ran through the shell; the one a rule resolved ran without it.
    assert calls == [{"command": "make lint"}, {"command": "make lint", "argv": ["make", "lint"], "read_only": False}]
    too_big = await client.post(f"/api/sessions/{sid}/approvals/missing", json={"allowed": True, "rule": "x"})
    assert too_big.status_code == 422


async def test_a_failing_recheck_leaves_the_card_pending(live):
    manager, session, project, tools, client = live
    task = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": "a.txt", "content": "x"}, "w"))
    key, event = await card(manager)

    def broken(change=None):
        raise RuntimeError("secret text from the card")
    manager.pending_checks[key] = broken
    response = await put_mode(client, session, "acceptEdits")
    assert response.status_code == 200 and response.json()["status"] == "awaiting_approval"
    assert key in manager.pending and not manager.pending[key].done()
    manager.decide(*key, False)
    await asyncio.wait_for(task, 5)
    assert event["approval"] == "declined"


async def test_a_running_subagent_follows_the_parents_mode(live):
    manager, session, project, tools, client = live
    session["permission_mode"] = "acceptEdits"
    seen = {}

    async def worker(child, prompt):
        child_tools = WorkspaceTools(str(project))
        child_tools.instruction_signature, child_tools.skill_signature = tools.instruction_signature, ""
        seen["output"] = await manager.execute_tool(child, child_tools, "run_command", {"command": "make"}, "child")
        await manager.event(child, "assistant", text="Done.")
    manager.run_databricks = worker
    delegate = asyncio.create_task(manager.execute_tool(session, tools, "delegate_task", {"task": "Build"}, "d"))
    (child_id, _), _ = await card(manager)
    assert child_id != session["id"]
    child = manager.get(child_id)
    assert (await put_mode(client, session, "plan")).status_code == 200
    await asyncio.wait_for(delegate, 5)
    assert child["permission_mode"] == "plan" and manager.store.get(child_id)["permission_mode"] == "plan"
    assert seen["output"].startswith(SWITCHED.format("Plan") + "Plan mode is read-only.")
    assert seen["output"].endswith("Return the plan as your final reply.")
    assert "Permission mode changed to Plan." in [item.get("text") for item in child["events"]]


async def test_a_mode_change_reaches_the_interactions_hook(live):
    manager, session, _, _, client = live
    changes = []

    class Interactions:
        async def mode_changed(self, changed, old, new):
            changes.append((changed["id"], old, new))
    manager.interactions = Interactions()
    await put_mode(client, session, "plan")
    await put_mode(client, session, "manual")
    assert changes == [(session["id"], "manual", "plan"), (session["id"], "plan", "manual")]


def stream(text="Done.", call=None):
    delta = {"tool_calls": [{"index": 0, **call}]} if call else {"content": text}
    chunk = {"choices": [{"delta": delta, "finish_reason": "tool_calls" if call else "stop"}]}
    return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")


async def test_each_model_step_follows_the_current_mode_and_tools(live, monkeypatch, tmp_path):
    manager, session, project, _, client = live
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.txt").write_text("outside\n")
    requests = []

    async def gateway(request):
        payload = json.loads(request.content)
        if "tools" not in payload:
            return stream("Title")
        requests.append(payload)
        if len(requests) == 1:
            # Between the two steps the user switches modes and turns off a tool.
            await manager.change_mode(session, "bypassPermissions")
            await manager.set_disabled_tools(session, ["write_file"])
            return stream(call={"id": "r1", "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"path": str(outside / "data.txt")})}})
        return stream()
    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(**kwargs, transport=httpx.MockTransport(gateway)))
    manager.live.pop(session["id"])
    await asyncio.wait_for(manager.run(session, "Read the outside file"), 10)
    first, second = requests[0], requests[1]
    assert mode_prompt("manual") in first["messages"][0]["content"]
    assert mode_prompt("bypassPermissions") in second["messages"][0]["content"]
    assert mode_prompt("manual") not in second["messages"][0]["content"]
    assert "write_file" in {item["function"]["name"] for item in first["tools"]}
    assert "write_file" not in {item["function"]["name"] for item in second["tools"]}
    read = next(item for item in session["events"] if item.get("call_id") == "r1")
    assert read["state"] == "completed" and "outside" in read["output"]
    assert not any(item.get("name") == "access_directory" for item in session["events"])


async def test_tools_unrestricted_follows_the_mode_on_every_tool_call(live, tmp_path):
    manager, session, _, tools, _ = live
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.txt").write_text("a")
    session["permission_mode"] = "bypassPermissions"
    tools.unrestricted = False  # Stale from an earlier mode: execute_tool follows the current one.
    assert json.loads(await manager.execute_tool(session, tools, "read_file", {"path": str(outside / "a.txt")}, "1"))["content"] == "1: a"
    assert tools.unrestricted is True and not manager.pending
    session["permission_mode"] = "manual"
    task = asyncio.create_task(manager.execute_tool(session, tools, "read_file", {"path": str(outside / "a.txt")}, "2"))
    (sid, eid), access = await card(manager)
    assert access["name"] == "access_directory" and tools.unrestricted is False
    manager.decide(sid, eid, False)
    await task


def test_agent_manager_keeps_a_two_argument_approve():
    import inspect
    assert list(inspect.signature(AgentManager.approve).parameters) == ["self", "session", "event"]
