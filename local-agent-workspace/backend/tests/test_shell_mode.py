"""! shell mode (CC-60): one line the user runs in the project folder; its output goes
with the next message."""
import asyncio
import json
import re

import httpx
import pytest

from local_agent.agents import AgentManager, public_session
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.store import Store
from local_agent.transcript import transcript

GITHUB_TOKEN = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0" * 2


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session.update(title="Shell", title_generated=True, permission_mode="plan")
    store.save(session)
    yield manager, session, project
    store.db.close()


async def finish(manager, session_id):
    for _ in range(1000):
        task = manager.tasks.get(session_id)
        if task is None or task.done():
            await asyncio.sleep(0.02)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("The task did not finish.")


def command_event(session):
    return next(event for event in session["events"] if event.get("name") == "user_command")


async def test_a_command_runs_in_plan_mode_and_waits_for_the_next_message(runtime):
    manager, session, project = runtime
    (project / "a.txt").write_text("x")
    manager.start_shell(session["id"], "ls && exit 3")
    await finish(manager, session["id"])
    stored = manager.get(session["id"])
    event = command_event(stored)
    assert event["type"] == "tool" and event["input"] == {"command": "ls && exit 3"} and "call_id" not in event
    assert event["state"] == "error" and event["job_id"]
    page = json.loads(event["output"])
    assert page["output"] == "a.txt\n" and page["exit_code"] == 3
    assert stored["pending_context"] == [{"kind": "shell", "command": "ls && exit 3", "exit_code": 3, "text": "a.txt\n"}]
    assert manager.statuses[session["id"]] == "idle" and stored["permission_mode"] == "plan"
    public = public_session(stored)
    assert public["pending_context"] == [{"kind": "shell", "command": "ls && exit 3", "exit_code": 3}]
    assert "a.txt" not in json.dumps(public["pending_context"])


async def test_long_output_is_bounded_and_secrets_are_redacted(runtime):
    manager, session, _ = runtime
    manager.start_shell(session["id"], f"echo {GITHUB_TOKEN}; echo token private-token-value; head -c 20000 /dev/zero | tr '\\0' 'z'")
    await finish(manager, session["id"])
    stored = manager.get(session["id"])
    assert len(command_event(stored)["output"].encode()) <= 8000
    note = stored["pending_context"][0]
    assert GITHUB_TOKEN not in note["text"] and "[REDACTED]" in note["text"]
    assert note["text"].endswith("\n[Output shortened.]") and len(note["text"]) < 8000


async def test_the_next_prompt_carries_the_fenced_output_once(runtime, monkeypatch):
    manager, session, _ = runtime
    manager.start_shell(session["id"], "echo hello")
    await finish(manager, session["id"])
    requests = []
    client = httpx.AsyncClient

    async def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, text="data: " + json.dumps({"choices": [{"delta": {"content": "Seen."},
                                                                             "finish_reason": "stop"}]}) + "\n\ndata: [DONE]\n\n")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**{"transport": httpx.MockTransport(handle), **kwargs}))
    manager.start(session["id"], "What did it print?")
    await finish(manager, session["id"])
    stored = manager.get(session["id"])
    content = requests[0]["messages"][-1]["content"]
    nonce = re.search(r"<<<ATTACHMENT ([0-9a-f]{12}) command", content).group(1)
    assert content.startswith("What did it print?\n\n---\nContext attached by the app")
    assert f"<<<ATTACHMENT {nonce} command run by the user with ! (exit 0)>>>\n$ echo hello\nhello\n\n<<<END {nonce}>>>" in content
    assert "pending_context" not in stored
    user = [event for event in stored["events"] if event["type"] == "user"][-1]
    assert user["text"] == "What did it print?"
    assert user["attachments"] == [{"kind": "shell", "command": "echo hello", "exit_code": 0}]
    assert "_Attached when sent: output of `! echo hello` (exit 0)_" in transcript(stored)
    manager.start(session["id"], "And now?")
    await finish(manager, session["id"])
    assert requests[1]["messages"][-1] == {"role": "user", "content": "And now?"}


async def test_a_turn_that_fails_before_sending_keeps_the_output(runtime):
    manager, session, _ = runtime
    manager.start_shell(session["id"], "echo kept")
    await finish(manager, session["id"])

    def missing():
        raise ValueError("Databricks credentials are not configured.")
    manager.settings.credentials = missing
    manager.start(session["id"], "Look at it")
    await finish(manager, session["id"])
    stored = manager.get(session["id"])
    assert stored["terminal_reason"] == "error"
    assert stored["pending_context"][0]["text"] == "kept\n"


async def test_stop_cancels_the_command_without_a_note(runtime):
    manager, session, _ = runtime
    manager.start_shell(session["id"], "sleep 30")
    for _ in range(500):
        if any(event.get("job_id") for event in manager.get(session["id"])["events"]):
            break
        await asyncio.sleep(0.01)
    await manager.stop(session["id"])
    stored = manager.get(session["id"])
    event = command_event(stored)
    assert event["state"] == "cancelled" and manager.jobs.get(event["job_id"])["state"] == "cancelled"
    assert "pending_context" not in stored and manager.statuses[session["id"]] == "idle"


def test_only_one_line_runs(runtime):
    manager, session, _ = runtime
    with pytest.raises(ValueError, match="^Shell mode runs one line. Remove the line breaks or use Workspace → Terminal.$"):
        manager.start_shell(session["id"], "echo one\necho two")
    assert session["id"] not in manager.tasks


async def test_the_shell_route_checks_the_line_and_a_busy_chat(runtime):
    manager, _, _ = runtime
    app = create_app(manager.settings)
    agents = app.state.manager
    session = agents.store.create(manager.settings.values)
    sid = session["id"]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            headers = {"X-Local-Token": (await client.get("/api/bootstrap")).json()["token"]}
            path = f"/api/sessions/{sid}/shell"
            broken = await client.post(path, headers=headers, json={"command": "echo a\necho b"})
            assert broken.status_code == 400 and broken.json()["detail"].startswith("Shell mode runs one line.")
            assert (await client.post(path, headers=headers, json={"command": "x" * 2001})).status_code == 422
            agents.statuses[sid] = "running"
            assert (await client.post(path, headers=headers, json={"command": "echo a"})).status_code == 409
            agents.statuses[sid] = "idle"
            started = await client.post(path, headers=headers, json={"command": "echo a"})
            assert started.status_code == 202 and started.json() == {"ok": True}
            await finish(agents, sid)
            assert agents.get(sid)["pending_context"][0]["text"] == "a\n"
    finally:
        await agents.jobs.shutdown()
        agents.store.db.close()
