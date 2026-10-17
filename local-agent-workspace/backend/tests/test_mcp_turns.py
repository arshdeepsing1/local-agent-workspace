"""MCP in chat turns: a failing server is skipped with one notice, per-chat server switches, the
32-function limit, annotations and images on cards."""
import json
import os
import sys
from contextlib import asynccontextmanager

import httpx
import pytest

from local_agent.agents import MAX_REQUEST_TOOLS, AgentManager
from local_agent.config import Settings
from local_agent.store import Store


class Socket:
    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "synthetic-token")
    store = Store(settings.state_dir / "turns.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session["permission_mode"] = "bypassPermissions"
    socket = Socket()
    manager.listeners[session["id"]] = {socket}
    yield manager, session, tmp_path, socket
    store.db.close()


@pytest.fixture
def local_server(tmp_path):
    server = tmp_path / "mcp_server.py"
    server.write_text('''import sys
from pathlib import Path
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
Path(sys.argv[1]).write_text("started")
server = MCPServer("Local test", log_level="ERROR")
@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def echo(text: str) -> str:
    return "echo " + text
server.run()
''')
    return {"id": "local", "name": "Local test", "transport": "stdio", "command": sys.executable,
            "args": [str(server), str(tmp_path / "local.started")], "enabled": True}


def stream(text=None, call=None):
    delta = {"content": text} if call is None else {"tool_calls": [{"index": 0, "id": call.get("id", "call"), "type": "function",
                                                                    "function": {"name": call["name"], "arguments": call.get("arguments", "{}")}}]}
    return httpx.Response(200, text="data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": "tool_calls" if call else "stop"}]})
                          + "\n\ndata: [DONE]\n\n")


def gateway(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handler)))


def notices(session):
    return [event["text"] for event in session["events"] if event["type"] == "notice"]


async def test_a_failing_server_is_skipped_with_one_notice_per_chat(runtime, local_server, monkeypatch):
    manager, session, tmp_path, socket = runtime
    broken = {**local_server, "id": "broken", "name": "Broken", "command": str(tmp_path / "missing-server")}
    manager.extensions.update_config({"servers": [broken, local_server]})
    sent = []

    async def handler(request):
        payload = json.loads(request.content)
        sent.append([tool["function"]["name"] for tool in payload["tools"]])
        if payload["messages"][-1]["role"] == "user":
            return stream(call={"name": "mcp__local__echo", "arguments": '{"text": "hi"}'})
        assert payload["messages"][-1]["content"].startswith('{\n  "output": "echo hi"')
        return stream("Done.")
    gateway(monkeypatch, handler)
    await manager.run(session, "Use the tool")
    assert session["terminal_reason"] == "completed"
    assert "mcp__local__echo" in sent[0] and not any(name.startswith("mcp__broken__") for name in sent[0])
    [notice] = notices(session)
    assert notice.startswith("MCP server Broken was skipped this turn: could not connect or list tools")
    assert "Its tools are unavailable; other servers work." in notice
    assert [(item["id"], item["status"], item["tool_count"]) for item in session["mcp_status"]] == [
        ("broken", "failed", 0), ("local", "connected", 1)]
    assert session["mcp_status"][1]["server_info"]["name"] == "Local test"
    assert any(item["type"] == "mcp_status" for item in socket.sent)
    # The next turn: the server is still broken (and waits before it is tried again), so no new notice.
    await manager.run(session, "Again")
    assert session["terminal_reason"] == "completed" and len(notices(session)) == 1
    assert session["mcp_status"][0]["status"] == "failed" and session["mcp_status"][0]["retry_seconds"] >= 1
    # The tool card shows the server's annotation; it never changes a permission.
    card = next(event for event in session["events"] if event.get("name") == "mcp__local__echo")
    assert card["mcp_annotations"] == {"read_only": True}
    # The status is not part of an export.
    from local_agent.portability import export_bundle
    assert "mcp_status" not in json.dumps(export_bundle([session], session["id"]))


async def test_a_server_turned_off_in_the_chat_never_starts(runtime, local_server, monkeypatch):
    manager, session, tmp_path, _ = runtime
    manager.extensions.update_config({"servers": [local_server]})
    await manager.set_disabled_tools(session, ["mcp__local__*"])
    sent = []

    async def handler(request):
        sent.append([tool["function"]["name"] for tool in json.loads(request.content)["tools"]])
        if len(sent) == 1:
            return stream(call={"name": "mcp__local__echo", "arguments": '{"text": "hi"}'})
        return stream("Done.")
    gateway(monkeypatch, handler)
    await manager.run(session, "Try the tool")
    assert not (tmp_path / "local.started").exists()
    assert not any(name.startswith("mcp__") for name in sent[0])
    assert session["mcp_status"] == [{"id": "local", "name": "Local test", "status": "off", "tool_count": 0}]
    card = next(event for event in session["events"] if event.get("name") == "mcp__local__echo")
    assert card["state"] == "rejected" and "turned off" in card["output"]


async def test_plan_mode_offers_no_mcp_tool_and_a_turn_that_starts_in_plan_starts_no_server(runtime, local_server,
                                                                                             monkeypatch):
    manager, session, tmp_path, _ = runtime
    manager.extensions.update_config({"servers": [local_server]})
    exactly_builtins(manager, session, 20)
    session["permission_mode"] = "plan"  # Plan refuses every MCP call (permissions.tool_decision).
    definitions, capped = manager.offered_tools(session, mcp_tools(3))
    assert not any(item["function"]["name"].startswith("mcp__") for item in definitions) and capped == []
    session["permission_mode"] = "manual"
    assert [item["function"]["name"] for item in manager.offered_tools(session, mcp_tools(3))[0][-3:]] == [
        f"mcp__many__tool{n:02d}" for n in range(3)]
    session.update(permission_mode="plan", disabled_tools=[])
    sent = []

    async def handler(request):
        sent.append([tool["function"]["name"] for tool in json.loads(request.content)["tools"]])
        return stream("Done.")
    gateway(monkeypatch, handler)
    await manager.run(session, "Plan the change")
    assert session["terminal_reason"] == "completed" and "submit_plan" in sent[0]
    assert not any(name.startswith("mcp__") for name in sent[0]) and not (tmp_path / "local.started").exists()
    # Out of Plan, the next turn starts the server and offers its tools.
    session["permission_mode"] = "bypassPermissions"
    await manager.run(session, "Now use it")
    assert "mcp__local__echo" in sent[1] and (tmp_path / "local.started").exists()


async def test_a_subagent_inherits_the_server_switch(runtime, local_server, monkeypatch):
    manager, session, tmp_path, _ = runtime
    manager.extensions.update_config({"servers": [local_server]})
    session["disabled_tools"] = ["mcp__local__*"]
    manager.store.save(session)
    gateway(monkeypatch, lambda request: stream("Child report."))
    result = await manager.delegates.delegate(session, "Look around", agent_type="general")
    assert result["status"] == "completed"
    child = manager.get(result["session_id"]) if "session_id" in result else next(
        item for item in manager.store.list() if item.get("parent_session_id") == session["id"])
    assert "mcp__local__*" in manager.get(child["id"])["disabled_tools"]
    assert not (tmp_path / "local.started").exists()


def big_server(tmp_path, count=32):
    """A stdio server with count tools: alone it takes all the room of a turn's MCP discovery (MAX_TOOLS)."""
    path = tmp_path / "big_server.py"
    path.write_text(f'''import sys
from pathlib import Path
from mcp.server import MCPServer
Path(sys.argv[1]).write_text("started")
server = MCPServer("Big test", log_level="ERROR")
for n in range({count}):
    server.add_tool(lambda x: x, name=f"big{{n:02d}}", description="Echo.")
server.run()
''')
    return {"id": "big", "name": "Big test", "transport": "stdio", "command": sys.executable,
            "args": [str(path), str(tmp_path / "big.started")], "enabled": True}


@pytest.mark.parametrize("narrowing", ["disallowed_tools: [mcp__big__*]", "tools: [read_file, mcp__local__echo]"])
async def test_a_server_an_agent_may_use_no_tool_of_never_starts_in_its_subagent(runtime, local_server, monkeypatch,
                                                                                 narrowing):
    manager, session, tmp_path, _ = runtime
    agents = tmp_path / "project" / ".agents" / "agents"
    agents.mkdir(parents=True)
    (agents / "narrow.md").write_text(f"---\ndescription: Test agent.\nprofile: inherit\n{narrowing}\n---\nWork.\n")
    manager.extensions.update_config({"servers": [big_server(tmp_path), local_server]})
    sent = []

    async def handler(request):
        sent.append([tool["function"]["name"] for tool in json.loads(request.content)["tools"]])
        return stream("Child report.")
    gateway(monkeypatch, handler)
    result = await manager.delegates.delegate(session, "Look around", agent_type="narrow")
    assert result["status"] == "completed"
    child = next(item for item in manager.store.list() if item.get("parent_session_id") == session["id"])
    # The big server would have taken all the room and pushed the allowed one out (skipped).
    assert not (tmp_path / "big.started").exists() and (tmp_path / "local.started").exists()
    assert [(item["id"], item["status"]) for item in manager.get(child["id"])["mcp_status"]] == [
        ("big", "off"), ("local", "connected")]
    assert [name for name in sent[0] if name.startswith("mcp__")] == ["mcp__local__echo"]


def mcp_tools(count):
    return [{"type": "function", "function": {"name": f"mcp__many__tool{n:02d}", "description": "External MCP tool",
                                              "parameters": {"type": "object", "properties": {}}}} for n in range(count)]


def exactly_builtins(manager, session, count):
    """Turn off built-in tools until exactly count are offered (more tools may be added later). The helpers that can
    join mid-turn are offered already, so they keep no slot (joinable_helpers)."""
    session.update(tool_output_offered=True, findings_tool=True, background_subagents_offered=True)
    names = [item["function"]["name"] for item in manager.step_definitions(session, [])]
    assert len(names) >= count
    session["disabled_tools"] = names[count:]
    assert len(manager.step_definitions(session, [])) == count


async def test_at_most_32_functions_built_ins_first(runtime):
    manager, session, _, _ = runtime
    exactly_builtins(manager, session, 27)
    definitions, capped = manager.offered_tools(session, mcp_tools(10))
    assert len(definitions) == MAX_REQUEST_TOOLS == 32
    assert [item["function"]["name"] for item in definitions[27:]] == [f"mcp__many__tool{n:02d}" for n in range(5)]
    assert capped == [f"mcp__many__tool{n:02d}" for n in range(5, 10)]
    # Turning an MCP tool off frees its slot for the next one.
    session["disabled_tools"].append("mcp__many__tool00")
    definitions, capped = manager.offered_tools(session, mcp_tools(10))
    assert definitions[-1]["function"]["name"] == "mcp__many__tool05" and capped[0] == "mcp__many__tool06"


def test_the_largest_built_in_tool_set_fits_the_request_limit(runtime):
    from local_agent.agents import TOOL_DEFINITIONS
    from local_agent.feature_tools import FEATURE_TOOLS
    manager, session, _, _ = runtime
    # Every built-in tool a chat can get at once (get_tool_output, report_findings and get_subagent_result offered, a
    # search provider set, nothing turned off) fits; submit_report is for subagents only, submit_plan for Plan. Only
    # then is there no room left for MCP tools (they are capped with a notice).
    manager.settings.values.update(web_search_provider="searxng", web_search_url="http://127.0.0.1:8888")
    session.update(tool_output_offered=True, findings_tool=True, background_subagents_offered=True, disabled_tools=[])
    definitions, capped = manager.offered_tools(session, [])
    assert len(definitions) == len(TOOL_DEFINITIONS) + len(FEATURE_TOOLS) - 2 == MAX_REQUEST_TOOLS and capped == []
    assert not {"submit_report", "submit_plan"} & {item["function"]["name"] for item in definitions}
    # Plan mode adds submit_plan and leaves out the tools it always refuses (edits, remember, stop_job).
    session["permission_mode"] = "plan"
    names = {item["function"]["name"] for item in manager.offered_tools(session, [])[0]}
    left_out = {"write_file", "edit_file", "insert_activity_log", "move_path", "delete_path", "notebook_edit",
                "remember", "stop_job"}
    assert "submit_plan" in names and not names & left_out and len(names) == MAX_REQUEST_TOOLS - len(left_out) + 1
    session["permission_mode"] = "manual"
    # Without a background subagent (the usual case) one place stays free.
    session.pop("background_subagents_offered")
    assert len(manager.offered_tools(session, [])[0]) == MAX_REQUEST_TOOLS - 1
    # A subagent gets submit_report but never delegate_task, remember, submit_plan or get_subagent_result.
    session.update(is_subagent=True, background_subagents_offered=True)
    names = {item["function"]["name"] for item in manager.offered_tools(session, [])[0]}
    assert "submit_report" in names and not names & {"delegate_task", "remember", "submit_plan", "get_subagent_result"}


async def test_a_shortened_mcp_result_can_be_read_from_the_start(runtime):
    from local_agent.result_budget import ResultAllowance
    manager, session, _, _ = runtime
    whole = "".join(f"line {number:05d}\n" for number in range(2000))
    results = manager.results.chat(session["id"])

    class Connection:
        definitions = mcp_tools(2)
        tool_names = {"mcp__many__tool00", "mcp__many__tool01"}
        annotations = {}

        async def call(self, name, arguments):
            if name == "mcp__many__tool00":
                return {"output": whole[:7000], "is_error": False, "truncated": True, "result_id": results.add(whole),
                        "next_offset": 7000, "total_chars": len(whole)}
            return {"output": "short " + "y" * 3000, "is_error": False, "truncated": False}
    manager.extension_connections[session["id"]] = Connection()
    tools = manager.session_tools(session)
    # The response's allowance shortens the first page: the model is pointed at offset 0, not past the omitted part.
    output = json.loads(await manager.execute_tool(session, tools, "mcp__many__tool00", {}, "c1", allowance=ResultAllowance(10)))
    assert output["result_shortened"] is True and output["next_offset"] == 0 and "get_tool_output" in output["hint"]
    assert manager.results.page(session["id"], output["result_id"], 0, 8000)["output"].startswith("line 00000\n")
    assert session["tool_output_offered"] is True
    # A result whose first page was all of it is kept now, so its omitted part can be read too.
    output = json.loads(await manager.execute_tool(session, tools, "mcp__many__tool01", {}, "c2", allowance=ResultAllowance(10)))
    assert output["result_shortened"] is True and output["next_offset"] == 0
    assert manager.results.page(session["id"], output["result_id"], 0, 8000)["output"] == "short " + "y" * 3000


async def test_capped_tools_are_named_once_and_refused(runtime, monkeypatch):
    manager, session, _, socket = runtime
    exactly_builtins(manager, session, 27)
    calls = []

    class Connection:
        definitions = mcp_tools(10)
        tool_names = {item["function"]["name"] for item in definitions}
        status = [{"id": "many", "name": "Many", "status": "connected", "tools": sorted(tool_names)}]

        async def discover(self):
            return self.definitions

        async def call(self, name, arguments):
            calls.append(name)
            return {"output": "ran", "is_error": False, "truncated": False}

    @asynccontextmanager
    async def turn(**options):
        yield Connection()
    manager.extensions.turn = turn
    requests = []

    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert len(payload["tools"]) <= 32
        if len(requests) == 1:
            return stream(call={"name": "mcp__many__tool09"})
        return stream("Done.")
    gateway(monkeypatch, handler)
    await manager.run(session, "Use the last tool")
    assert calls == []
    card = next(event for event in session["events"] if event.get("name") == "mcp__many__tool09")
    assert card["state"] == "error" and "not offered in this request" in card["output"]
    [notice] = notices(session)
    assert notice.startswith("Databricks accepts at most 32 tools per request, so 5 MCP tools were not offered: "
                             "mcp__many__tool05, mcp__many__tool06")
    assert session["mcp_status"][0]["capped"] == [f"mcp__many__tool{n:02d}" for n in range(5, 10)]
    await manager.run(session, "Again")
    assert len(notices(session)) == 1


async def test_mcp_images_reach_the_card_never_the_model(runtime, monkeypatch, tmp_path):
    manager, session, _, _ = runtime
    image = {"id": manager.results.add_image(session["id"], "image/png", "aGVsbG8="), "mime_type": "image/png", "bytes": 5}

    class Connection:
        definitions = mcp_tools(1)
        tool_names = {"mcp__many__tool00"}

        async def discover(self):
            return self.definitions

        async def call(self, name, arguments):
            return {"output": "[image/png, 5 bytes; shown in the chat, not sent to the model]", "is_error": False,
                    "truncated": False, "images": [image]}

    @asynccontextmanager
    async def turn(**options):
        yield Connection()
    manager.extensions.turn = turn
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return stream(call={"name": "mcp__many__tool00"}) if len(requests) == 1 else stream("Done.")
    gateway(monkeypatch, handler)
    await manager.run(session, "Show the chart")
    card = next(event for event in session["events"] if event.get("name") == "mcp__many__tool00")
    assert card["mcp_images"] == [image] and "aGVsbG8=" not in json.dumps(requests[1])
    assert "images" not in json.loads(requests[1]["messages"][-1]["content"])


def test_api_routes_for_images_and_the_server_switch(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from local_agent.api import create_app
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(workspace), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "synthetic-token")
    app = create_app(settings)
    with TestClient(app) as client:
        client.headers["X-Local-Token"] = client.get("/api/bootstrap").json()["token"]
        manager = app.state.manager
        manager.extensions.update_config({"servers": [{"id": "docs", "name": "Docs", "transport": "stdio",
                                                       "command": "docs-server", "enabled": False}]})
        sid = client.post("/api/sessions").json()["id"]
        listed = client.get("/api/tools", params={"session_id": sid}).json()
        assert listed["servers"] == [{"id": "docs", "name": "Docs", "enabled": False}]
        saved = client.put(f"/api/sessions/{sid}/disabled-tools", json={"tools": ["mcp__docs__*"]})
        assert saved.status_code == 200 and saved.json()["disabled_tools"] == ["mcp__docs__*"]
        assert client.put(f"/api/sessions/{sid}/disabled-tools", json={"tools": ["mcp__Bad__*"]}).status_code == 400
        image_id = manager.results.add_image(sid, "image/png", "aGVsbG8=")
        assert client.get(f"/api/sessions/{sid}/tool-images/{image_id}").json() == {"mime_type": "image/png", "data": "aGVsbG8="}
        assert client.get(f"/api/sessions/{sid}/tool-images/img-000000000000").status_code == 404
        # The last turn's MCP status (errors, stderr tails) stays out of the often fetched list.
        session = manager.get(sid)
        session["mcp_status"] = [{"id": "docs", "name": "Docs", "status": "failed", "error": "e", "stderr_tail": "x" * 2000}]
        manager.store.save(session)
        assert "mcp_status" not in next(item for item in client.get("/api/sessions").json() if item["id"] == sid)
        assert client.get(f"/api/sessions/{sid}").json()["mcp_status"][0]["status"] == "failed"
        # Deleting the chat empties its part of the store.
        assert client.delete(f"/api/sessions/{sid}").status_code == 200
        assert manager.results.image(sid, image_id) is None
    assert not os.environ.get("NEVER_SET_BY_TESTS")


async def test_a_helper_joining_mid_turn_never_pushes_out_an_offered_mcp_tool(runtime, monkeypatch):
    """With the default Settings, get_tool_output, report_findings and get_subagent_result keep a slot while MCP
    tools are offered: the long result that adds get_tool_output leaves the turn's MCP tools as they were."""
    manager, session, _, _ = runtime
    builtins = len(manager.step_definitions(session, []))
    room = MAX_REQUEST_TOOLS - builtins - 3
    assert room >= 1
    whole = "".join(f"line {number:05d}\n" for number in range(2000))
    results = manager.results.chat(session["id"])

    class Connection:
        definitions = mcp_tools(room + 3)
        tool_names = {item["function"]["name"] for item in definitions}
        status = [{"id": "many", "name": "Many", "status": "connected", "tools": sorted(tool_names)}]

        async def discover(self):
            return self.definitions

        async def call(self, name, arguments):
            return {"output": whole[:7000], "is_error": False, "truncated": True, "result_id": results.add(whole),
                    "next_offset": 7000, "total_chars": len(whole)}

    @asynccontextmanager
    async def turn(**options):
        yield Connection()
    manager.extensions.turn = turn
    requests = []

    async def handler(request):
        payload = json.loads(request.content)
        requests.append([tool["function"]["name"] for tool in payload["tools"]])
        if len(requests) == 1:
            return stream(call={"name": f"mcp__many__tool{room - 1:02d}"})
        return stream("Done.")
    gateway(monkeypatch, handler)
    await manager.run(session, "Use the last offered tool")
    first, second = requests
    mcp = [f"mcp__many__tool{n:02d}" for n in range(room)]
    assert [name for name in first if name.startswith("mcp__")] == mcp and "get_tool_output" not in first
    assert [name for name in second if name.startswith("mcp__")] == mcp and "get_tool_output" in second
    assert len(first) == MAX_REQUEST_TOOLS - 3 and len(second) == MAX_REQUEST_TOOLS - 2
    [notice] = notices(session)
    assert notice.startswith("Databricks accepts at most 32 tools per request, so 3 MCP tools were not offered: ")
    # Once every helper has joined, the slots they kept are theirs: the MCP room is the same.
    session.update(findings_tool=True, background_subagents_offered=True)
    definitions, capped = manager.offered_tools(session, Connection.definitions)
    assert len(definitions) == MAX_REQUEST_TOOLS and [item["function"]["name"] for item in definitions[-room:]] == mcp
    # A helper the chat turned off keeps no slot.
    session.update(findings_tool=False, disabled_tools=["report_findings"])
    assert len(manager.offered_tools(session, Connection.definitions)[1]) == 2
