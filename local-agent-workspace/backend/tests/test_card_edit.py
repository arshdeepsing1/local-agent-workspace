"""Edit before approving: the user changes a waiting card's command, file content or replacement text."""
import asyncio
import json

import httpx
import pytest

from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.instructions import load_project_instructions
from local_agent.tools import WorkspaceTools


@pytest.fixture
async def live(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
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
    manager.store.db.close()


async def card(manager):
    for _ in range(300):
        if manager.pending:
            key = next(iter(manager.pending))
            return key, next(item for item in manager.get(key[0])["events"] if item["id"] == key[1])
        await asyncio.sleep(0.01)
    raise AssertionError("Expected an approval card")


async def edit(client, key, changes):
    return await client.post(f"/api/sessions/{key[0]}/approvals/{key[1]}/edit", json={"input": changes})


def recorder(manager):
    calls = []

    async def command_tool(session, event, arguments, **options):
        calls.append({"command": arguments["command"], **options})
        return {"state": "completed", "exit_code": 0, "output": "recorded"}
    manager.command_tool = command_tool
    return calls


async def test_an_edited_command_runs_instead_of_the_proposed_one(live):
    manager, session, _, tools, client = live
    calls = recorder(manager)
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": "rm -rf build", "timeout_seconds": 30}, "c"))
    key, event = await card(manager)
    assert event["editable"] == ["command"]
    expires = event["approval_expires"]
    response = await edit(client, key, {"command": "rm -rf build/cache"})
    assert response.json() == {"ok": True}
    assert event["input"] == {"command": "rm -rf build/cache", "timeout_seconds": 30} and event["edited"] is True
    assert event["preview"].startswith("rm -rf build/cache\n\nTimeout: 30 seconds")
    assert event["rule_options"]["argv"] == ["rm", "-rf", "build/cache"]
    assert event["approval_expires"] == expires and key in manager.pending
    manager.decide(*key, True)
    output = await asyncio.wait_for(task, 5)
    assert output.startswith("[The user edited this command before approving it. It ran as: rm -rf build/cache. "
                             "Your proposed command did not run.]\n")
    assert calls == [{"command": "rm -rf build/cache"}]  # Approved on a card: through the shell, as approved.
    assert event["edited"] is True and "editable" not in event


async def test_an_edit_that_a_rule_blocks_is_refused_and_the_card_stays(live):
    manager, session, _, tools, client = live
    calls = recorder(manager)
    session["allow_rules"] = [{"behavior": "deny", "tool": "run_command", "prefix": ["git", "push"]}]
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {"command": "git status"}, "c"))
    key, event = await card(manager)
    response = await edit(client, key, {"command": "git push --force"})
    assert response.status_code == 400
    assert response.json()["detail"].startswith("The edited action is blocked: Blocked by a deny rule for this chat")
    assert event["input"] == {"command": "git status"} and key in manager.pending
    for changes in ({"timeout_seconds": "5"}, {"command": "git status", "background": "true"}, {}):
        bad = await edit(client, key, changes)
        assert (bad.status_code, bad.json()["detail"]) == (400, "Only the command can be edited."), changes
    assert (await edit(client, key, {"command": ""})).status_code == 400  # Validation of the new command.
    manager.decide(*key, False)
    await task
    late = await edit(client, key, {"command": "git status -s"})
    assert (late.status_code, late.json()["detail"]) == (400, "This approval is no longer pending.")
    assert calls == []


async def test_a_file_edit_rebuilds_the_preview_and_checks_for_drift(live):
    manager, session, project, tools, client = live
    (project / "a.py").write_text("x = 1\n")
    task = asyncio.create_task(manager.execute_tool(session, tools, "edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}, "e"))
    key, event = await card(manager)
    assert event["editable"] == ["new_text"] and "+x = 2" in event["preview"]
    assert (await edit(client, key, {"old_text": "x"})).json()["detail"] == "Only the replacement text can be edited."
    assert (await edit(client, key, {"new_text": "x = 3"})).json() == {"ok": True}
    assert "+x = 3" in event["preview"] and "+x = 2" not in event["preview"]
    manager.decide(*key, True)
    output = await asyncio.wait_for(task, 5)
    assert output.startswith("[The user edited the replacement text before approving it. Read the file before editing it again.]")
    assert (project / "a.py").read_text() == "x = 3\n"

    # The file changes on disk after the card was shown: the edit is refused.
    task = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": "b.txt", "content": "one"}, "w"))
    key, event = await card(manager)
    assert event["editable"] == ["content"]
    (project / "b.txt").write_text("someone else\n")
    drift = await edit(client, key, {"content": "two"})
    assert (drift.status_code, drift.json()["detail"]) == (
        400, "The file changed since this card was shown. Decline it and ask the agent to read the file again.")
    manager.decide(*key, False)
    await task


async def test_schema_and_size_limits_apply_to_edits(live):
    manager, session, project, tools, client = live
    tools.write_limit = 1_000_000  # The largest write limit in Settings: the tool's own argument limit still applies.
    task = asyncio.create_task(manager.execute_tool(session, tools, "write_file", {"path": "c.txt", "content": "x"}, "w"))
    key, event = await card(manager)
    over_limit = await edit(client, key, {"content": "y" * (520 * 1024)})
    assert over_limit.status_code == 400 and "limit" in over_limit.json()["detail"]
    too_large = await edit(client, key, {"content": "y" * (601 * 1024)})
    assert too_large.status_code == 400 and too_large.json()["detail"] == "The edited text is too large."
    tools.write_limit = 80_000
    beyond_write_limit = await edit(client, key, {"content": "y" * 90_000})
    assert beyond_write_limit.json()["detail"] == "File content must be UTF-8 text without NUL bytes and at most 80 KB."
    assert (await client.post(f"/api/sessions/{key[0]}/approvals/{key[1]}/edit",
                              json={"input": {"content": 5}})).status_code == 422
    assert event["input"] == {"path": "c.txt", "content": "x"}
    manager.decide(*key, True)
    await task
    assert (project / "c.txt").read_text() == "x"


async def test_mcp_activity_log_and_multi_edit_cards_cannot_be_edited(live):
    manager, session, project, tools, client = live

    class Connection:
        tool_names = {"mcp__docs__search"}
        definitions = [{"function": {"name": "mcp__docs__search", "parameters": {"type": "object"}}}]

        async def call(self, name, arguments):
            return {"ok": True}
    manager.extension_connections[session["id"]] = Connection()
    for name, arguments in (("mcp__docs__search", {"q": "x"}), ("insert_activity_log", {"path": "log.md"}),
                            ("edit_file", {"path": "a.txt", "edits": [{"old_text": "a", "new_text": "b"}]})):
        (project / "a.txt").write_text("a\n")
        task = asyncio.create_task(manager.execute_tool(session, tools, name, arguments, name))
        key, event = await card(manager)
        assert event["editable"] == []
        response = await edit(client, key, {"new_text": "c"})
        assert (response.status_code, response.json()["detail"]) == (400, "This action cannot be edited."), name
        manager.decide(*key, False)
        await task
    assert (project / "a.txt").read_text() == "a\n"
    missing = await client.post(f"/api/sessions/{session['id']}/approvals/missing/edit", json={"input": {"command": "x"}})
    assert (missing.status_code, missing.json()["detail"]) == (400, "This approval is no longer pending.")
    extra = await client.post(f"/api/sessions/{session['id']}/approvals/missing/edit", json={"input": {}, "x": 1})
    assert extra.status_code == 422
    json.dumps(extra.json())
