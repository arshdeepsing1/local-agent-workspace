import asyncio
import json

import pytest

from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.instructions import load_project_instructions
from local_agent.store import Store
from local_agent.tools import WorkspaceTools


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "synthetic-token")
    store = Store(settings.state_dir / "dispatch.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session["permission_mode"] = "bypassPermissions"
    tools = WorkspaceTools(str(project))
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance["text"], guidance["warnings"])
    tools.skill_signature = ""
    yield manager, session, tools
    await manager.jobs.shutdown()
    store.db.close()


def configure(manager, phase="before_tool", **fields):
    manager.extensions.update_config({"hooks": [{"id": "check", "event": phase, "command": "true", "enabled": True, **fields}]})


def mcp_connection(manager, session, invoke):
    class Connection:
        tool_names = {"mcp__demo__echo"}
        definitions = [{"function": {"name": "mcp__demo__echo", "parameters": {
            "type": "object", "required": ["text"], "additionalProperties": False,
            "properties": {"text": {"type": "string"}}}}}]
        call = staticmethod(invoke)
    manager.extension_connections[session["id"]] = Connection()


@pytest.mark.parametrize("name,arguments", [
    ("write_file", {"path": "../external.txt"}), ("run_command", {"command": 123}),
    ("run_command", {"command": "true", "unexpected": True}),
    ("create_task", {"title": "Task", "unexpected": True}), ("mcp__demo__echo", {"text": 4}),
    ("unavailable_tool", {}),
])
async def test_schema_rejection_precedes_approvals_hooks_and_dispatch(runtime, name, arguments):
    manager, session, tools = runtime
    session["permission_mode"] = "manual"
    configure(manager)
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Invalid arguments reached an action")

    manager.approve = manager.extensions.run_hook = tools.execute = forbidden
    mcp_connection(manager, session, forbidden)
    output = await manager.execute_tool(session, tools, name, arguments, "invalid")
    assert not calls
    assert "constraint failed" in output or "not available" in output
    assert session["events"][0]["state"] == "error"
    assert not manager.checkpoints.list() and not manager.task_board.list(session["id"])


@pytest.mark.parametrize("selectors,expected", [(None, 2), (["read_file"], 2), (["read_files"], 0), ([], 0)])
async def test_hooks_match_exact_selectors_with_stable_call_ids(runtime, selectors, expected):
    manager, session, tools = runtime
    configure(manager, **({} if selectors is None else {"tools": selectors}))
    (tools.root / "note.txt").write_text("hello")
    payloads = []

    async def hook(hook, payload, workspace):
        payloads.append(payload)
        return {"output": "ok", "exit_code": 0}

    manager.extensions.run_hook = hook
    for call_id in ("call-one", "call-two"):
        await manager.execute_tool(session, tools, "read_file", {"path": "note.txt"}, call_id)
    assert len(payloads) == expected
    if expected:
        assert [payload["call_id"] for payload in payloads] == ["call-one", "call-two"]
        events = [event for event in session["events"] if event.get("name") == "hook_before_tool"]
        assert [event["input"]["call_id"] for event in events] == ["call-one", "call-two"]
        assert all("call_id" not in event for event in events)


async def test_failure_hook_has_original_redacted_error_and_cannot_replace_it(runtime):
    manager, session, tools = runtime
    configure(manager, "tool_failure", tools=["read_file"])
    payloads = []

    async def fail(*args):
        raise ValueError("synthetic-token original tool failure")

    async def hook(hook, payload, workspace):
        payloads.append(payload)
        return {"output": "failure hook also failed", "exit_code": 9}

    tools.execute = fail
    manager.extensions.run_hook = hook
    output = await manager.execute_tool(session, tools, "read_file", {"path": "note.txt"}, "failed-call")
    assert "original tool failure" in output and "synthetic-token" not in output
    assert "failure hook also failed" not in output
    assert payloads == [{"event": "tool_failure", "session_id": session["id"], "tool": "read_file",
        "arguments": {"path": "note.txt"}, "call_id": "failed-call", "error": output, "outcome": "error"}]
    assert session["events"][0]["output"] == output
    assert "original tool error is unchanged" in session["events"][-1]["text"]


async def test_mcp_error_result_keeps_after_hook_and_adds_failure_hook(runtime):
    manager, session, tools = runtime
    manager.extensions.update_config({"hooks": [{"id": phase, "event": phase, "command": "true", "enabled": True,
        "tools": ["mcp__demo__echo"]} for phase in ("after_tool", "tool_failure")]})
    payloads = []

    async def invoke(*args):
        return {"output": "Remote action failed", "is_error": True}

    async def hook(hook, payload, workspace):
        payloads.append(payload)
        return {"output": "ok", "exit_code": 0}

    manager.extensions.run_hook = hook
    mcp_connection(manager, session, invoke)
    output = await manager.execute_tool(session, tools, "mcp__demo__echo", {"text": "hi"}, "mcp-call")
    assert json.loads(output)["is_error"] is True
    assert [payload["event"] for payload in payloads] == ["after_tool", "tool_failure"]
    assert all(payload["call_id"] == "mcp-call" for payload in payloads)
    assert session["events"][0]["state"] == "error"


@pytest.mark.parametrize("state,background,expected", [("failed", False, 1), ("timed_out", False, 1),
    ("cancelled", False, 0), ("completed", False, 0), ("failed", True, 0), ("running", True, 0)])
async def test_failure_hook_only_for_returned_foreground_command_failures(runtime, state, background, expected):
    manager, session, tools = runtime
    configure(manager, "tool_failure", tools=["run_command"])
    payloads = []
    result = {"state": state, "background": background, "exit_code": 1 if state == "failed" else None,
              "output": "original command result"}

    async def command(*args):
        return result

    async def hook(hook, payload, workspace):
        payloads.append(payload)
        return {"output": "ok", "exit_code": 0}

    manager.command_tool = command
    manager.extensions.run_hook = hook
    output = await manager.execute_tool(session, tools, "run_command", {"command": "true", "background": background}, "command")
    assert json.loads(output) == result
    assert session["events"][0]["state"] == "completed"
    assert len(payloads) == expected
    if expected:
        assert payloads[0]["result"] == result and payloads[0]["error"] == output


async def test_failure_hook_still_requires_separate_approval(runtime):
    manager, session, tools = runtime
    session["permission_mode"] = "manual"
    configure(manager, "tool_failure")
    approvals = []

    async def deny(session, event):
        approvals.append(event["name"])
        return False

    async def forbidden(*args):
        pytest.fail("Declined failure hook must not start")

    manager.approve = deny
    manager.extensions.run_hook = forbidden
    output = await manager.execute_tool(session, tools, "read_file", {"path": "missing.txt"}, "missing")
    assert approvals == ["hook_tool_failure"]
    assert "no such file" in output.lower()


@pytest.mark.parametrize("profile", ["read_only", "file_editor"])
async def test_restricted_profiles_never_run_hook_commands(runtime, profile):
    manager, session, tools = runtime
    session["tool_profile"] = profile
    configure(manager, "tool_failure")

    async def forbidden(*args):
        pytest.fail("A restricted profile ran a hook")

    manager.extensions.run_hook = forbidden
    await manager.execute_tool(session, tools, "read_file", {"path": "missing.txt"}, "missing")
    assert not any(event.get("name", "").startswith("hook_") for event in session["events"])


async def test_stop_during_tool_does_not_launch_failure_hook(runtime):
    manager, session, tools = runtime
    configure(manager, "tool_failure")
    started = asyncio.Event()

    async def block(*args):
        started.set()
        await asyncio.Event().wait()

    async def forbidden(*args):
        pytest.fail("Cancellation launched a failure hook")

    tools.execute = block
    manager.extensions.run_hook = forbidden
    task = asyncio.create_task(manager.execute_tool(session, tools, "read_file", {"path": "note.txt"}, "cancelled"))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not any(event.get("name", "").startswith("hook_") for event in session["events"])
