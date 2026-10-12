"""Continue signals, the live task checklist and get_task, and the Markdown transcript export."""
import asyncio
import json
from email.message import Message

import httpx
import pytest
from fastapi.testclient import TestClient

from local_agent.agents import AgentManager, public_session
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.feature_tools import FEATURE_TOOLS
from local_agent.instructions import load_project_instructions
from local_agent.permissions import tool_decision
from local_agent.store import Store
from local_agent.telemetry import begin_inference_call
from local_agent.tool_profiles import tool_allowed
from local_agent.tools import TOOL_DEFINITIONS, WorkspaceTools
from local_agent.transcript import transcript



def ended(message):
    """An idle status message without its turn_ended time (checked by the attention tests)."""
    assert message.get("turn_ended"), message
    return {key: value for key, value in message.items() if key != "turn_ended"}

class Socket:
    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)

    def of(self, kind):
        return [item for item in self.sent if item["type"] == kind]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "configured-secret-value")
    store = Store(settings.state_dir / "conversation.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    tools = WorkspaceTools(str(project))
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance["text"], guidance["warnings"])
    socket = Socket()
    manager.listeners[session["id"]] = {socket}
    yield manager, session, tools, socket
    store.db.close()


def gateway(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handler)))


def chunk(delta, finish):
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"


def reply(text="Done.", finish="stop"):
    return httpx.Response(200, text=chunk({"content": text}, finish) + "data: [DONE]\n\n")


def tool_call(name="list_tasks", arguments="{}"):
    call = {"index": 0, "id": "call", "type": "function", "function": {"name": name, "arguments": arguments}}
    return httpx.Response(200, text=chunk({"tool_calls": [call]}, "tool_calls") + "data: [DONE]\n\n")


# Continue button: how a turn ended reaches the page with the idle status.

async def test_idle_status_reports_a_step_limit(runtime, monkeypatch):
    manager, session, _, socket = runtime
    manager.settings.values["max_agent_steps"] = 1
    gateway(monkeypatch, lambda request: tool_call())
    manager.start(session["id"], "Plan the work")
    await manager.tasks[session["id"]]
    assert ended(socket.of("status")[-1]) == {"type": "status", "status": "idle", "terminal_reason": "step_limit"}
    notice = manager.store.get(session["id"])["events"][-1]
    assert notice["type"] == "notice" and notice["terminal_reason"] == "step_limit"


async def test_idle_status_reports_an_output_limit_error(runtime, monkeypatch):
    manager, session, _, socket = runtime
    manager.settings.values["max_agent_steps"] = 1
    gateway(monkeypatch, lambda request: reply("A very long answer", finish="length"))
    manager.start(session["id"], "Write everything")
    await manager.tasks[session["id"]]
    assert ended(socket.of("status")[-1]) == {"type": "status", "status": "idle", "terminal_reason": "inference_error"}
    error = manager.store.get(session["id"])["events"][-1]
    assert error["type"] == "error" and error["error_kind"] == "output_limit"


async def test_idle_status_reports_stop_and_then_completion(runtime, monkeypatch):
    manager, session, _, socket = runtime
    entered = asyncio.Event()

    answers = []

    async def answer(request):
        if answers:
            return answers.pop(0)
        entered.set()
        await asyncio.Event().wait()
    gateway(monkeypatch, answer)
    manager.start(session["id"], "Long task")
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(manager.stop(session["id"]), 2)
    assert ended(socket.of("status")[-1]) == {"type": "status", "status": "idle", "terminal_reason": "stopped"}

    # A later completed turn replaces the reason, so the page hides Continue again.
    answers += [reply(), reply("Short title")]
    manager.start(session["id"], "Continue from where you stopped.")
    await manager.tasks[session["id"]]
    assert ended(socket.of("status")[-1]) == {"type": "status", "status": "idle", "terminal_reason": "completed"}
    assert all("terminal_reason" not in item for item in socket.of("status") if item["status"] != "idle")


def test_restart_marks_a_cut_short_turn_interrupted(runtime):
    manager, session, _, _ = runtime
    session["terminal_reason"] = "completed"
    session["events"] += [
        {"id": "user", "type": "user", "text": "Work", "created": 1},
        {"id": "reply", "type": "assistant", "text": "Part", "created": 2, "request_info": {"model": "m", "status": "running"}},
    ]
    manager.store.save(session)
    other = manager.store.create(manager.settings.values)
    other["terminal_reason"] = "completed"
    manager.store.save(other)
    AgentManager(manager.store, manager.settings)
    saved = manager.store.get(session["id"])
    assert saved["terminal_reason"] == "interrupted"
    assert saved["events"][-1]["request_info"] == {"model": "m", "status": "interrupted", "error_kind": "incomplete_response"}
    assert manager.store.get(other["id"])["terminal_reason"] == "completed"


@pytest.mark.parametrize("purpose,in_turn,expected", [
    ("title", False, "completed"),        # killed while naming a finished turn
    ("compaction", False, "completed"),   # killed during a manual compaction
    ("compaction", True, "interrupted"),  # killed while compacting inside a turn
    ("agent", False, "interrupted"),      # killed while the model was answering
])
def test_restart_interrupts_only_a_turn_in_progress(runtime, purpose, in_turn, expected):
    manager, session, _, _ = runtime
    session["terminal_reason"] = "completed"
    session["events"] += [
        {"id": "user", "type": "user", "text": "Work", "created": 1},
        {"id": "reply", "type": "assistant", "text": "Done", "created": 2, "request_info": {"model": "m", "status": "completed"}},
    ]
    begin_inference_call(session, purpose, "m")
    if in_turn:
        session["turn_running"] = True
    manager.store.save(session)
    AgentManager(manager.store, manager.settings)
    saved = manager.store.get(session["id"])
    assert saved["terminal_reason"] == expected
    assert saved["inference_calls"][-1]["status"] == "interrupted"
    assert "turn_running" not in saved
    assert [event.get("request_info", {}).get("status") for event in saved["events"]] == [None, "completed"]


async def test_turn_marker_covers_the_model_loop_but_not_naming(runtime, monkeypatch):
    manager, session, _, _ = runtime
    seen = []

    async def answer(request):
        seen.append((json.loads(request.content).get("stream"), manager.store.get(session["id"]).get("turn_running")))
        return reply("Done.") if len(seen) == 1 else httpx.Response(200, json={"choices": [{"message": {"content": "Title"}}]})
    gateway(monkeypatch, answer)
    manager.start(session["id"], "Work")
    await manager.tasks[session["id"]]
    assert seen[0] == (True, True)
    assert len(seen) == 2 and seen[1][1] is None
    saved = manager.store.get(session["id"])
    assert "turn_running" not in saved and saved["terminal_reason"] == "completed"
    assert "turn_running" not in public_session({**saved, "turn_running": True})


# Live task checklist and get_task.

async def test_task_tools_push_the_task_list(runtime):
    manager, session, tools, socket = runtime
    created = json.loads(await manager.execute_tool(session, tools, "create_task", {"title": "Write tests"}, "c1"))
    assert [item["tasks"] for item in socket.of("tasks")] == [manager.task_board.list(session["id"])]
    assert socket.of("tasks")[0]["tasks"][0]["title"] == "Write tests"
    await manager.execute_tool(session, tools, "update_task", {"task_id": created["id"], "status": "in_progress"}, "u1")
    assert socket.of("tasks")[-1]["tasks"][0]["status"] == "in_progress"
    # Reading tasks and a failed update change nothing, so nothing is pushed.
    await manager.execute_tool(session, tools, "list_tasks", {}, "l1")
    await manager.execute_tool(session, tools, "get_task", {"task_id": created["id"]}, "g1")
    await manager.execute_tool(session, tools, "update_task", {"task_id": "missing", "status": "completed"}, "u2")
    assert len(socket.of("tasks")) == 2


async def test_get_task_returns_details_for_this_conversation_only(runtime):
    manager, session, tools, _ = runtime
    first = manager.task_board.create(session["id"], "Parse input", "Handle empty files and BOMs.")
    second = manager.task_board.create(session["id"], "Write output", depends_on=[first["id"]])
    result = json.loads(await manager.execute_tool(session, tools, "get_task", {"task_id": second["id"]}, "g1"))
    assert result == {"id": second["id"], "title": "Write output", "description": "", "status": "pending",
                      "depends_on": [first["id"]]}
    result = json.loads(await manager.execute_tool(session, tools, "get_task", {"task_id": first["id"]}, "g2"))
    assert result["description"] == "Handle empty files and BOMs."

    other = manager.store.create(manager.settings.values)
    output = await manager.execute_tool(other, tools, "get_task", {"task_id": first["id"]}, "g3")
    assert json.loads(output) == {"error_kind": "not_found", "message": "Task not found in this conversation."}
    assert other["events"][-1]["state"] == "error"
    assert other["events"][-1]["output"] == "Task not found in this conversation."


@pytest.mark.parametrize("mode", ["manual", "auto", "acceptEdits", "plan", "bypassPermissions"])
async def test_get_task_runs_without_approval_in_every_mode(runtime, mode):
    manager, session, tools, _ = runtime
    task = manager.task_board.create(session["id"], "Check")
    session["permission_mode"] = mode

    async def unexpected(*args):
        pytest.fail("get_task asked for approval")
    manager.approve = unexpected
    assert tool_decision(mode, "get_task", {}) == "allow"
    result = json.loads(await manager.execute_tool(session, tools, "get_task", {"task_id": task["id"]}, "g"))
    assert result["title"] == "Check"


@pytest.mark.parametrize("profile", ["read_only", "file_editor", "inherit"])
async def test_get_task_is_in_every_tool_profile(runtime, monkeypatch, profile):
    manager, session, _, _ = runtime
    session["tool_profile"] = profile
    assert tool_allowed(session, "get_task")
    seen = []

    async def handler(request):
        seen.append([tool["function"]["name"] for tool in json.loads(request.content)["tools"]])
        return reply()
    gateway(monkeypatch, handler)
    await manager.run_databricks(session, "Which task is next?")
    assert "get_task" in seen[0]
    assert len([*TOOL_DEFINITIONS, *FEATURE_TOOLS]) == 17


# API: task routes push the list, the stream sends it on connect, and the transcript export.

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(workspace), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "configured-secret-value")
    app = create_app(settings)
    with TestClient(app) as client:
        token = client.get("/api/bootstrap").json()["token"]
        client.headers["X-Local-Token"] = token
        client.token = token
        yield client, app.state.manager


def connect(client, session_id):
    return client.websocket_connect(f"/api/sessions/{session_id}/stream", subprotocols=["local-workspace", client.token])


def opened(socket):
    """The snapshot and the changes list every new stream starts with."""
    assert [socket.receive_json()["type"] for _ in range(2)] == ["snapshot", "changes"]


def rename(client, session_id, title):
    """Pushes { type: 'title' }: a message that marks where the expected ones end."""
    assert client.put(f"/api/sessions/{session_id}/title", json={"title": title}).status_code == 200


def test_task_routes_push_the_list_and_the_stream_sends_it_on_connect(client):
    client, _ = client
    session_id = client.post("/api/sessions").json()["id"]
    with connect(client, session_id) as socket:
        opened(socket)
        task = client.post(f"/api/sessions/{session_id}/tasks", json={"title": "Review"}).json()
        rename(client, session_id, "After create")
        assert socket.receive_json() == {"type": "tasks", "tasks": [task]}
        assert socket.receive_json() == {"type": "title", "title": "After create"}
        updated = client.patch(f"/api/sessions/{session_id}/tasks/{task['id']}", json={"status": "in_progress"}).json()
        rename(client, session_id, "After update")
        assert socket.receive_json() == {"type": "tasks", "tasks": [updated]}
        assert socket.receive_json() == {"type": "title", "title": "After update"}
        # A rejected change pushes nothing.
        assert client.patch(f"/api/sessions/{session_id}/tasks/{task['id']}", json={"status": "bad"}).status_code == 400
        rename(client, session_id, "After rejection")
        assert socket.receive_json() == {"type": "title", "title": "After rejection"}
    with connect(client, session_id) as socket:
        opened(socket)
        rename(client, session_id, "After connect")
        assert socket.receive_json() == {"type": "tasks", "tasks": [updated]}
        assert socket.receive_json() == {"type": "title", "title": "After connect"}


def test_stream_sends_no_task_message_without_tasks(client):
    client, _ = client
    session_id = client.post("/api/sessions").json()["id"]
    other = client.post("/api/sessions").json()["id"]
    client.post(f"/api/sessions/{other}/tasks", json={"title": "Elsewhere"})
    with connect(client, session_id) as socket:
        opened(socket)
        rename(client, session_id, "Renamed")
        # The first message after the snapshot is not a task list.
        assert socket.receive_json() == {"type": "title", "title": "Renamed"}


def test_transcript_export_is_the_script_markdown_with_redaction(client):
    client, manager = client
    session_id = client.post("/api/sessions").json()["id"]
    session = manager.get(session_id)
    session.update(title="Fix the CSV export!", created=1767225600)
    secret_title = manager.store.create(manager.settings.values)
    secret_title.update(title="Why does configured-secret-value fail with password=hunter22", created=1767225600)
    manager.store.save(secret_title)
    session["events"] += [
        {"id": "u", "type": "user", "text": "The value configured-secret-value came from the profile; password=hunter22", "created": 1767225600},
        {"id": "a", "type": "assistant", "text": "# Plan\n\n```python\nprint('ok')\n```", "created": 1767225601},
        {"id": "t", "type": "tool", "name": "run_command", "state": "completed", "input": {"command": "ls"},
         "output": json.dumps({"state": "completed", "exit_code": 0, "output": "a.csv\n"})},
    ]
    manager.store.save(session)
    response = client.get(f"/api/sessions/{session_id}/transcript.md")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    disposition = Message()
    disposition["content-disposition"] = response.headers["content-disposition"]
    assert disposition.get_content_disposition() == "attachment"
    name = disposition.get_filename()
    assert name.endswith(f"-fix-the-csv-export-{session_id[:8]}.md") and name[:4].isdigit()
    text = response.text
    assert text == transcript(manager.store.get(session_id), redact=manager.settings.redact)
    assert text.startswith("# Transcript: Fix the CSV export!")
    assert "### Plan" in text and "print('ok')" in text and "a.csv" in text
    # The configured credential (no common secret shape here) and a common secret shape.
    assert "configured-secret-value" not in text and "hunter22" not in text
    assert "The value [REDACTED] came from the profile; password=[REDACTED]" in text
    # A secret in the title stays out of the file name too.
    disposition = Message()
    disposition["content-disposition"] = client.get(f"/api/sessions/{secret_title['id']}/transcript.md").headers["content-disposition"]
    name = disposition.get_filename()
    assert name.endswith(f"-why-does-redacted-fail-with-password-redacted-{secret_title['id'][:8]}.md"), name
    assert "secret" not in name and "hunter" not in name
    assert client.get("/api/sessions/missing/transcript.md").status_code == 404
    # The local token is required, like every other API route.
    del client.headers["X-Local-Token"]
    assert client.get(f"/api/sessions/{session_id}/transcript.md").status_code == 403
