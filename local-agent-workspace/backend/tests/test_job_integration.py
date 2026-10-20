import asyncio
import json
import shlex
import sys
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from local_agent.agents import AgentManager, public_session
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.jobs import TIMEOUT_NOT_STOPPED
from local_agent.store import Store
from local_agent.tools import WorkspaceTools


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://fake.example", "synthetic-secret-token")
    return settings


@pytest.fixture
async def runtime(settings):
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    manager.live[session["id"]] = session
    yield manager, session, WorkspaceTools(session["workspace"])
    await manager.jobs.shutdown()
    store.db.close()


async def test_the_compatibility_command_endpoint_says_when_a_timed_out_command_did_not_end(settings, monkeypatch,
                                                                                            unkillable_group):
    import httpx
    app = create_app(settings)
    manager = app.state.manager
    start = manager.jobs.start

    async def short_start(*args, **kwargs):
        return await start(*args, **{**kwargs, "timeout_seconds": 1})
    monkeypatch.setattr(manager.jobs, "start", short_start)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            response = await asyncio.wait_for(client.post("/api/command", json={"text": "printf partial; sleep 30"}), 10)
    assert response.status_code == 408
    assert response.json()["detail"] == TIMEOUT_NOT_STOPPED


@pytest.mark.parametrize("mode", ["plan", "manual", "acceptEdits", "auto"])
async def test_background_commands_obey_permissions(runtime, mode):
    manager, session, tools = runtime
    session["permission_mode"] = mode
    manager.approve = AsyncMock(return_value=False)
    output = await manager.execute_tool(session, tools, "run_command", {
        "command": "touch must-not-exist", "background": True}, "denied")
    assert not (tools.root / "must-not-exist").exists()
    assert not manager.jobs.list(session_id=session["id"])
    assert "read-only" in output if mode == "plan" else "declined" in output
    if mode == "plan":
        manager.approve.assert_not_awaited()
    else:
        assert "Background job" in session["events"][-1]["preview"]


async def test_foreground_streams_before_completion_and_returns_paged_output(runtime):
    manager, session, tools = runtime
    session["permission_mode"] = "bypassPermissions"
    first_output = asyncio.Event()
    async def broadcast(sid, data):
        if data.get("event", {}).get("output") == "first\n":
            first_output.set()
    manager.broadcast = broadcast
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {
        "command": "printf 'first\\n'; sleep .3; printf 'last\\n'"}, "stream"))
    await asyncio.wait_for(first_output.wait(), 2)
    assert not task.done()
    # The running card carries the job's start time, so the page can show how long it has run.
    event = next(event for event in session["events"] if event.get("call_id") == "stream")
    assert event["state"] == "running" and event["job_started"] == manager.jobs.get(event["job_id"])["created"]
    result = json.loads(await task)
    assert result["output"] == "first\nlast\n"
    assert result["state"] == "completed"
    page = json.loads(await manager.execute_tool(session, tools, "get_job_output", {
        "job_id": result["job_id"], "offset": 6, "max_chars": 3}, "page"))
    assert page["output"] == "las"
    assert page["next_offset"] == 9


async def test_cancel_foreground_stops_job_but_completed_background_launch_survives(runtime):
    manager, session, tools = runtime
    session["permission_mode"] = "bypassPermissions"
    background = json.loads(await manager.execute_tool(session, tools, "run_command", {
        "command": "sleep 30", "background": True}, "background"))
    task = asyncio.create_task(manager.execute_tool(session, tools, "run_command", {
        "command": "sleep 30"}, "foreground"))
    async with asyncio.timeout(2):
        while session["events"][-1].get("call_id") != "foreground" or not session["events"][-1].get("job_id"):
            await asyncio.sleep(.01)
    foreground_id = session["events"][-1]["job_id"]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert manager.jobs.get(foreground_id)["state"] == "cancelled"
    assert manager.jobs.get(background["job_id"])["state"] == "running"
    # Stopping a job is a command action: Plan refuses it, Auto runs it without a card.
    session["permission_mode"] = "plan"
    refused = await manager.execute_tool(session, tools, "stop_job", {"job_id": background["job_id"]}, "plan-stop")
    assert refused.startswith("Plan mode is read-only") and manager.jobs.get(background["job_id"])["state"] == "running"
    session["permission_mode"] = "auto"
    stopped = json.loads(await manager.execute_tool(session, tools, "stop_job", {
        "job_id": background["job_id"]}, "stop"))
    assert stopped["state"] == "cancelled"


async def test_model_cannot_inspect_or_stop_another_conversations_job(runtime):
    manager, session, tools = runtime
    other = manager.store.create(manager.settings.values)
    job = await manager.jobs.start("sleep 30", session["workspace"], session_id=other["id"], background=True)
    for name in ("get_job_output", "stop_job"):
        result = await manager.execute_tool(session, tools, name, {"job_id": job["id"]}, name)
        assert "not found in this conversation" in result
    assert manager.jobs.get(job["id"])["state"] == "running"
    assert json.loads(await manager.execute_tool(session, tools, "list_jobs", {}, "list")) == {"jobs": [], "next_offset": None}


def test_public_session_does_not_expose_canonical_command_history():
    session = {"id": "chat", "events": [], "wire": [], "command_jobs": [{"command": "private"}]}
    assert "command_jobs" not in public_session(session)


async def test_unicode_job_output_pages_bound_serialized_size_without_losing_characters(runtime):
    manager, _, _ = runtime
    job = {"id": "unicode", "state": "completed", "exit_code": 0, "output": "😀\n" * 3000,
           "truncated": False, "background": False}
    parts, offset = [], 0
    while True:
        page = manager.job_output(job, offset, 8000)
        assert len(json.dumps(page, indent=2).encode()) <= 8000
        parts.append(page["output"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert "".join(parts) == job["output"]


def test_job_api_is_scoped_validated_and_persists_on_refresh(settings):
    with TestClient(create_app(settings)) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        first = client.post("/api/sessions", headers=headers).json()["id"]
        second = client.post("/api/sessions", headers=headers).json()["id"]
        query = f"?session_id={first}"
        assert client.post("/api/jobs" + query, json={"command": "pwd"}).status_code == 403
        for values in ({"timeout_seconds": 0}, {"timeout_seconds": 3601}, {"max_output_bytes": 100}, {"background": "yes"}):
            assert client.post("/api/jobs" + query, headers=headers, json={"command": "pwd", **values}).status_code == 422
        job = client.post("/api/jobs" + query, headers=headers, json={
            "command": "printf 'early\\n'; sleep 30", "background": True}).json()
        job_url = f"/api/jobs/{job['id']}"
        for suffix in ("", f"?session_id={second}"):
            assert client.get(job_url + suffix, headers=headers).status_code == 404
            assert client.post(job_url + "/stop" + suffix, headers=headers).status_code == 404
        deadline = time.monotonic() + 2
        while True:
            snapshot = client.get(job_url + query, headers=headers).json()
            if "early" in snapshot["output"]:
                break
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert snapshot["state"] == "running"
        summaries = client.get("/api/jobs" + query, headers=headers).json()
        assert summaries[0]["id"] == job["id"] and "output" not in summaries[0]
        stopped = client.post(job_url + "/stop" + query, headers=headers).json()
        assert stopped["state"] == "cancelled"
        assert client.get(job_url + query, headers=headers).json()["output"] == stopped["output"]
        assert client.delete(f"/api/sessions/{first}", headers=headers).status_code == 200
        assert client.app.state.manager.jobs.get(job["id"]) is None


def test_app_shutdown_stops_background_jobs(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        job = client.post("/api/jobs", headers=headers, json={"command": "sleep 30", "background": True}).json()
    with TestClient(create_app(settings)) as restarted:
        headers = {"X-Local-Token": restarted.get("/api/bootstrap").json()["token"]}
        restored = restarted.get(f"/api/jobs/{job['id']}", headers=headers).json()
        assert restored["state"] == "cancelled"


async def test_the_apps_shutdown_leaves_a_stopped_notice_for_the_agents_background_job(settings):
    """The app's shutdown stops the agent's running background job: after the restart its chat has the end notice,
    so the job's card reads that it was stopped, as Workspace → Progress does."""
    import httpx
    app = create_app(settings)
    manager = app.state.manager
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            session_id = (await client.post("/api/sessions")).json()["id"]
            session = manager.get(session_id)
            session["permission_mode"] = "bypassPermissions"
            started = json.loads(await manager.execute_tool(session, manager.session_tools(session), "run_command",
                                                            {"command": "sleep 30", "background": True}, "call-1"))
            await wait_for_turn_end(manager, session)
    with TestClient(create_app(settings)) as restarted:
        headers = {"X-Local-Token": restarted.get("/api/bootstrap").json()["token"]}
        job = restarted.get(f"/api/jobs/{started['job_id']}?session_id={session_id}", headers=headers).json()
        assert job["state"] == "cancelled"
        events = restarted.get(f"/api/sessions/{session_id}", headers=headers).json()["events"]
        assert [(event["text"], event["job_id"], event["job_state"]) for event in events if event["type"] == "notice"] == [
            ("Background job stopped: sleep 30", started["job_id"], "cancelled")]


async def test_a_failed_command_shows_the_start_and_the_end_of_its_output(runtime):
    manager, session, tools = runtime
    session["permission_mode"] = "bypassPermissions"
    result = json.loads(await manager.execute_tool(session, tools, "run_command", {"command": "seq 1 20000; exit 3",
                                                                                      "max_output_bytes": 200_000}, "f"))
    assert result["state"] == "failed" and result["excerpt"] == "head_and_tail"
    assert result["output"].startswith("1\n2\n") and result["output"].endswith("19999\n20000\n")
    head = result["output"].split("\n[… ")[0]
    assert result["next_offset"] == result["omitted"]["start"] == len(head) <= 1500
    assert result["omitted"]["end"] == result["total_chars"] - len(result["output"].split(" …]\n", 1)[1])
    assert f"read them with get_job_output offset={result['next_offset']}" in result["output"]
    assert len(json.dumps(result, indent=2).encode()) <= 8000
    middle = json.loads(await manager.execute_tool(session, tools, "get_job_output", {
        "job_id": result["job_id"], "offset": result["next_offset"]}, "m"))
    assert middle["offset"] == result["next_offset"] and middle["output"]

    passed = json.loads(await manager.execute_tool(session, tools, "run_command", {
        "command": "seq 1 20000", "max_output_bytes": 200_000}, "p"))
    assert passed["state"] == "completed" and "excerpt" not in passed
    assert passed["output"].startswith("1\n2\n") and passed["next_offset"] == len(passed["output"])

    script = "import sys; sys.stdout.write('\\u00e9\\u4e2d' * 10000); sys.exit(1)"
    command = f"{sys.executable} -c {shlex.quote(script)}"
    wide = json.loads(await manager.execute_tool(session, tools, "run_command", {"command": command}, "w"))
    assert wide["state"] == "failed" and len(json.dumps(wide, indent=2).encode()) <= 8000
    assert wide["excerpt"] == "head_and_tail" and wide["output"].startswith("é中")


async def test_head_and_tail_says_when_the_end_of_the_output_was_not_kept(runtime):
    manager, session, tools = runtime
    session["permission_mode"] = "bypassPermissions"
    result = json.loads(await manager.execute_tool(session, tools, "run_command", {
        "command": "seq 1 30000; echo 'ERROR: the real failure'; exit 2"}, "t"))
    assert result["truncated"] is True and result["excerpt"] == "head_and_tail"
    assert "ERROR" not in result["output"]
    assert result["output"].endswith("[… output after this point was not kept (over max_output_bytes); rerun with a "
                                     "larger max_output_bytes, or write it to a file and use read_file with tail_lines …]")
    assert len(json.dumps(result, indent=2).encode()) <= 8000


def test_head_and_tail_falls_back_to_the_first_page_or_the_whole_output(runtime):
    manager, _, _ = runtime
    job = {"id": "j", "state": "failed", "exit_code": 1, "truncated": False, "background": False, "output": "x" * 5000}
    page = manager.first_page(job)
    assert page["output"] == "x" * 5000 and page["next_offset"] is None and "excerpt" not in page
    job["output"] = "\U0001f600" * 20000  # 12 bytes of JSON each: only a shorter start fits, and no end.
    page = manager.first_page(job)
    assert page["excerpt"] == "head_and_tail" and 200 <= page["next_offset"] < 1500
    assert page["output"].endswith(f"read them with get_job_output offset={page['next_offset']} …]\n")
    assert len(json.dumps(page, indent=2).encode()) <= 8000
    job.update(output="y" * 20000, state="completed")
    assert manager.first_page(job) == manager.job_output(job)


async def test_commands_get_named_environment_variables_but_never_credentials(runtime, monkeypatch):
    manager, session, tools = runtime
    session["permission_mode"] = "bypassPermissions"
    for name, value in (("MY_TOOL_HOME", "/srv/tools"), ("DBRICKS_TOKEN", "never"), ("LD_PRELOAD", "/srv/x.so"),
                        ("BASH_ENV", "/srv/startup"), ("OTHER_SETTING", "unlisted")):
        monkeypatch.setenv(name, value)
    manager.settings.values["command_env"] = ["MY_TOOL_HOME", "DBRICKS_TOKEN", "LD_PRELOAD", "BASH_ENV"]
    script = "import os, json; print(json.dumps(sorted(k for k in os.environ if k not in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'SHELL', 'USER', 'LOGNAME', 'VIRTUAL_ENV', 'PWD', 'SHLVL', '_', 'OLDPWD', 'LC_CTYPE', '__CF_USER_TEXT_ENCODING'))))"
    command = f"{sys.executable} -c {shlex.quote(script)}"
    result = json.loads(await manager.execute_tool(session, tools, "run_command", {"command": command}, "env"))
    assert json.loads(result["output"]) == ["MY_TOOL_HOME"]
    assert manager.command_environment() == {"MY_TOOL_HOME": "/srv/tools"}

    monkeypatch.setenv("MY_TOOL_HOME", "/srv/synthetic-secret-token/bin")  # Contains the configured token.
    assert manager.command_environment() == {}
    for setting in ("MY_TOOL_HOME", ["1BAD"], ["A"] * 33, [7]):
        manager.settings.values["command_env"] = setting
        assert manager.command_environment() == {}

    monkeypatch.setenv("MY_TOOL_HOME", "/srv/tools")
    manager.settings.values["command_env"] = ["MY_TOOL_HOME"]
    manager.extensions.update_config({"hooks": [{"id": "envcheck", "event": "after_tool", "enabled": True,
                                                 "command": command, "timeout_seconds": 10}]})
    hook = manager.extensions.public_config()["hooks"][0]
    outcome = await manager.extensions.run_hook(hook, {"event": "after_tool"}, session["workspace"])
    assert json.loads(outcome["output"]) == []  # Hooks get no pass-through.


async def wait_for_turn_end(manager, session):
    manager.store.save(session)
    manager.live.pop(session["id"], None)


async def test_a_background_job_that_ends_after_the_turn_leaves_one_notice(runtime):
    manager, session, tools = runtime
    session["permission_mode"] = "bypassPermissions"
    sent = []

    async def broadcast(session_id, data):
        sent.append(data)
    manager.broadcast = broadcast
    started = json.loads(await manager.execute_tool(session, tools, "run_command", {
        "command": "sleep .3;   printf  finished;\n exit 2", "background": True}, "bg"))
    await wait_for_turn_end(manager, session)
    await asyncio.wait_for(manager.jobs.wait(started["job_id"]), 10)
    stored = manager.store.get(session["id"])
    notices = [event for event in stored["events"] if event["type"] == "notice"]
    assert [event["text"] for event in notices] == [
        "Background job failed (exit 2): sleep .3; printf finished; exit 2"]
    assert notices[0]["job_id"] == started["job_id"]
    assert (notices[0]["job_state"], notices[0]["exit_code"]) == ("failed", 2)  # The job's card shows how it ended.
    assert [data["event"]["id"] for data in sent if data.get("event", {}).get("type") == "notice"] == [notices[0]["id"]]
    await manager.job_update(manager.jobs.get(started["job_id"]))  # A repeated update adds nothing.
    assert len([event for event in manager.store.get(session["id"])["events"] if event["type"] == "notice"]) == 1
    assert manager.live.get(session["id"]) is None  # The conversation was not made live.


async def test_a_slow_page_does_not_lose_the_notice(runtime, monkeypatch):
    from local_agent import jobs
    manager, session, tools = runtime
    session["permission_mode"] = "bypassPermissions"
    monkeypatch.setattr(jobs, "UPDATE_TIMEOUT_SECONDS", .3)
    started = json.loads(await manager.execute_tool(session, tools, "run_command", {
        "command": "sleep .3; printf done", "background": True}, "bg"))
    await wait_for_turn_end(manager, session)

    class SlowSocket:
        async def send_json(self, data):
            await asyncio.sleep(5)

        async def close(self, code=1000):
            pass
    manager.listeners[session["id"]] = {SlowSocket()}
    begun = time.monotonic()
    await asyncio.wait_for(manager.jobs.wait(started["job_id"]), 10)
    assert time.monotonic() - begun < 3
    stored = manager.store.get(session["id"])
    assert [event["text"] for event in stored["events"] if event["type"] == "notice"] == [
        "Background job completed (exit 0): sleep .3; printf done"]


async def test_terminal_jobs_get_no_notice_and_a_stopped_agent_job_gets_one_for_its_card_only(runtime):
    manager, session, tools = runtime
    session["permission_mode"] = "bypassPermissions"
    session["jobs_reported_through"] = 0.0
    terminal = await manager.jobs.start("printf mine", session["workspace"], session_id=session["id"], background=True)
    stopped = json.loads(await manager.execute_tool(session, tools, "run_command", {
        "command": "sleep 30", "background": True}, "bg"))
    await wait_for_turn_end(manager, session)
    await manager.jobs.wait(terminal["id"])
    await manager.jobs.stop(stopped["job_id"])
    stored = manager.store.get(session["id"])
    notices = [event for event in stored["events"] if event["type"] == "notice"]
    job = manager.jobs.get(stopped["job_id"])
    assert [(event["text"], event["job_id"], event["job_state"], event["exit_code"]) for event in notices] == [
        ("Background job stopped: sleep 30", stopped["job_id"], "cancelled", job["exit_code"])]
    # The page shows it on the job's card; the agent's next request names only jobs that completed, failed or
    # timed out.
    assert manager.background_jobs_notice(stored) == ""
    assert "printf mine" not in json.dumps(stored["events"])


async def test_a_restart_leaves_an_interrupted_notice_for_the_agents_running_background_job(runtime):
    manager, session, _ = runtime
    session["events"].append({"id": "card", "type": "tool", "name": "run_command", "job_id": "job-1",
                              "state": "completed", "input": {"command": "sleep 99", "background": True}})
    session["jobs_reported_through"] = 0.0
    await wait_for_turn_end(manager, session)
    now = time.time()
    # Still running when the app stopped without its clean shutdown.
    manager.jobs._persist({"id": "job-1", "session_id": session["id"], "workspace": session["workspace"],
                           "command": "sleep 99", "state": "running", "created": now, "updated": now, "exit_code": None,
                           "output": "", "truncated": False, "timeout_seconds": 60, "max_output_bytes": 80000,
                           "background": True, "origin": "agent"})
    restarted = AgentManager(manager.store, manager.settings)
    try:
        stored = manager.store.get(session["id"])
        notices = [event for event in stored["events"] if event["type"] == "notice"]
        assert [(event["text"], event["job_id"], event["job_state"], event["exit_code"]) for event in notices] == [
            ("Background job interrupted: sleep 99", "job-1", "interrupted", None)]
        assert restarted.background_jobs_notice(stored) == ""  # The agent is not told, as before.
        # A second start adds no second notice.
        again = AgentManager(manager.store, manager.settings)
        await again.jobs.shutdown()
        assert len([event for event in manager.store.get(session["id"])["events"] if event["type"] == "notice"]) == 1
    finally:
        await restarted.jobs.shutdown()


async def test_a_stopped_jobs_notice_never_reaches_the_model(settings, monkeypatch):
    """The end notice of a stopped job is for the page: the chat's history (the wire, which is also all that
    compaction summarizes) is unchanged, and the agent's next request names no ended job and has no notice text."""
    import httpx
    store = Store(settings.state_dir / "stopped.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session["permission_mode"] = "bypassPermissions"
    requests = []
    replies = [{"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {
                   "name": "run_command", "arguments": json.dumps({"command": "sleep 30", "background": True})}}]},
               {"content": "Started."},
               {"content": "It was stopped."}]

    async def gateway(request):
        payload = json.loads(request.content)
        if "tools" not in payload:
            return httpx.Response(200, text='data: {"choices": [{"delta": {"content": "Title"}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n')
        requests.append(payload)
        delta = replies[len(requests) - 1]
        chunk = {"choices": [{"delta": delta, "finish_reason": "tool_calls" if "tool_calls" in delta else "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(gateway)))
    try:
        await manager.run(session, "Start a server")
        job_id = next(event["job_id"] for event in session["events"] if event.get("name") == "run_command")
        wire = json.loads(json.dumps(store.get(session["id"])["wire"]))
        await manager.jobs.stop(job_id)
        session = store.get(session["id"])  # As the app loads an idle conversation.
        assert [(event["text"], event["job_state"]) for event in session["events"] if event["type"] == "notice"] == [
            ("Background job stopped: sleep 30", "cancelled")]
        assert session["wire"] == wire
        await manager.run(session, "Is it up?")
        assert len(requests) == 3
        # The history it sends is the one before the stop, then the new message; the system message is the same.
        assert requests[2]["messages"][1:] == [*requests[1]["messages"][1:], {"role": "assistant", "content": "Started."},
                                               {"role": "user", "content": "Is it up?"}]
        assert requests[2]["messages"][0] == requests[1]["messages"][0]
        assert "Background job stopped" not in json.dumps(requests[2])
    finally:
        await manager.jobs.shutdown()
        store.db.close()


async def test_a_deleted_conversation_gets_no_notice_and_is_not_recreated(runtime):
    manager, session, tools = runtime
    session["permission_mode"] = "bypassPermissions"
    started = json.loads(await manager.execute_tool(session, tools, "run_command", {
        "command": "sleep 30", "background": True}, "bg"))
    await wait_for_turn_end(manager, session)
    await manager.jobs.remove_session(session["id"])
    # Deleting the chat stopped its job: that stop is no end notice, not even in the chat about to be deleted.
    assert not [event for event in manager.store.get(session["id"])["events"] if event["type"] == "notice"]
    manager.store.delete(session["id"])
    assert manager.jobs.get(started["job_id"]) is None
    assert manager.store.get(session["id"]) is None
    await asyncio.sleep(.1)
    assert not manager.store._path(session["id"]).exists()
    assert not [event for event in session["events"] if event["type"] == "notice"]


async def test_finished_jobs_are_reported_once_and_never_on_a_first_step(runtime):
    manager, session, tools = runtime
    session["permission_mode"] = "bypassPermissions"
    old = json.loads(await manager.execute_tool(session, tools, "run_command", {
        "command": "printf old", "background": True}, "old"))
    await manager.jobs.wait(old["job_id"])
    assert "jobs_reported_through" not in session
    assert manager.background_jobs_notice(session) == ""  # The first step only starts the clock.
    assert manager.background_jobs_notice(session) == ""
    jobs = []
    for index in range(7):
        jobs.append(json.loads(await manager.execute_tool(session, tools, "run_command", {
            "command": f"printf {index}; exit {index % 2}", "background": True}, f"n{index}")))
        await manager.jobs.wait(jobs[-1]["job_id"])
    text = manager.background_jobs_notice(session)
    assert text.startswith(f"Background jobs finished since your last step: {jobs[0]['job_id'][:8]} `printf 0; exit 0` "
                           "completed (exit 0); " + f"{jobs[1]['job_id'][:8]} `printf 1; exit 1` failed (exit 1); ")
    assert text.endswith("; and 2 more. Use get_job_output to read their output.")
    assert old["job_id"][:8] not in text
    assert manager.background_jobs_notice(session) == ""
    assert "jobs_reported_through" not in public_session(session)


async def test_the_next_model_request_reports_a_finished_background_job_once(settings, monkeypatch):
    import httpx
    store = Store(settings.state_dir / "notices.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session["permission_mode"] = "bypassPermissions"
    requests = []
    replies = [{"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {
                   "name": "run_command", "arguments": json.dumps({"command": "sleep .2; printf built", "background": True})}}]},
               {"content": "Started."},
               {"tool_calls": [{"index": 0, "id": "c2", "type": "function", "function": {
                   "name": "list_jobs", "arguments": "{}"}}]},
               {"content": "Done."},
               {"tool_calls": [{"index": 0, "id": "c3", "type": "function", "function": {
                   "name": "run_command", "arguments": json.dumps({"command": "sleep .2; printf again", "background": True})}}]},
               None,  # wait_job on that job: filled in below.
               {"content": "Built again."}]

    async def gateway(request):
        payload = json.loads(request.content)
        if "tools" not in payload:
            return httpx.Response(200, text='data: {"choices": [{"delta": {"content": "Title"}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n')
        requests.append(payload)
        delta = replies[len(requests) - 1]
        if delta is None:
            again = next(event["job_id"] for event in reversed(session["events"]) if event.get("name") == "run_command")
            delta = {"tool_calls": [{"index": 0, "id": "c4", "type": "function", "function": {
                "name": "wait_job", "arguments": json.dumps({"job_id": again, "timeout_seconds": 10})}}]}
        chunk = {"choices": [{"delta": delta, "finish_reason": "tool_calls" if "tool_calls" in delta else "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(gateway)))
    try:
        await manager.run(session, "Build it in the background")
        job_id = next(event["job_id"] for event in session["events"] if event.get("name") == "run_command")
        await asyncio.wait_for(manager.jobs.wait(job_id), 10)
        session = store.get(session["id"])  # As the app loads an idle conversation.
        await manager.run(session, "Is it done?")
        systems = [payload["messages"][0]["content"] for payload in requests]
        assert [text.count("Background jobs finished") for text in systems] == [0, 0, 1, 0]
        assert (f"Background jobs finished since your last step: {job_id[:8]} `sleep .2; printf built` completed (exit 0). "
                "Use get_job_output to read their output.") in systems[2]
        assert [event["text"] for event in session["events"] if event["type"] == "notice"] == [
            "Background job completed (exit 0): sleep .2; printf built"]
        # wait_job already showed that the next job ended: the following request does not repeat it.
        await manager.run(session, "Build it again and wait")
        systems = [payload["messages"][0]["content"] for payload in requests]
        waited = next(event for event in session["events"] if event.get("name") == "wait_job")
        assert json.loads(waited["output"])["reason"] == "exited"
        assert [text.count("Background jobs finished") for text in systems] == [0, 0, 1, 0, 0, 0, 0]
    finally:
        await manager.jobs.shutdown()
        store.db.close()


async def test_a_job_ending_while_its_chat_is_live_leaves_one_notice(runtime):
    manager, session, tools = runtime
    session["permission_mode"] = "bypassPermissions"
    sent = []

    async def broadcast(session_id, data):
        sent.append((session_id, data))
    manager.broadcast = broadcast
    started = json.loads(await manager.execute_tool(session, tools, "run_command", {
        "command": "sleep .2; printf ready", "background": True}, "bg"))
    waited = json.loads(await asyncio.wait_for(manager.execute_tool(session, tools, "wait_job", {
        "job_id": started["job_id"], "timeout_seconds": 10}, "w"), 10))
    assert waited["reason"] == "exited"
    assert manager.live[session["id"]] is session  # Still live: the turn goes on.
    notices = [event for event in session["events"] if event["type"] == "notice"]
    assert [(event["text"], event["job_id"], event["job_state"], event["exit_code"]) for event in notices] == [
        ("Background job completed (exit 0): sleep .2; printf ready", started["job_id"], "completed", 0)]
    stored = manager.store.get(session["id"])
    assert [event["id"] for event in stored["events"] if event["type"] == "notice"] == [notices[0]["id"]]
    assert [data["event"]["id"] for sid, data in sent
            if sid == session["id"] and data.get("event", {}).get("type") == "notice"] == [notices[0]["id"]]


async def test_notices_redact_secret_shapes_in_the_command(runtime):
    manager, session, tools = runtime
    session["permission_mode"] = "bypassPermissions"
    secret = "ghp_" + "A1b2" * 6  # A token shape that redact_secrets replaces.
    session["jobs_reported_through"] = 0.0
    started = json.loads(await manager.execute_tool(session, tools, "run_command", {
        "command": f"sleep .2; printf ok # {secret}", "background": True}, "bg"))
    await wait_for_turn_end(manager, session)
    await asyncio.wait_for(manager.jobs.wait(started["job_id"]), 10)
    stored = manager.store.get(session["id"])
    texts = [event["text"] for event in stored["events"] if event["type"] == "notice"]
    assert texts == ["Background job completed (exit 0): sleep .2; printf ok # [REDACTED]"]
    report = manager.background_jobs_notice(stored)
    assert started["job_id"][:8] in report and "[REDACTED]" in report and secret not in report


async def test_a_timed_out_background_jobs_notice_says_so_without_an_exit_code(runtime):
    manager, session, _ = runtime
    session["events"].append({"id": "card", "type": "tool", "name": "run_command", "job_id": "job-1",
                              "state": "completed", "input": {"command": "sleep 99", "background": True}})
    await manager.job_update({"id": "job-1", "session_id": session["id"], "background": True, "state": "timed_out",
                              "exit_code": None, "command": "sleep 99", "output": ""})
    notice = session["events"][-1]
    assert (notice["text"], notice["job_id"], notice["job_state"], notice["exit_code"]) == (
        "Background job timed out: sleep 99", "job-1", "timed_out", None)


async def test_run_command_jobs_are_the_agents_and_terminal_and_bang_commands_are_the_users(settings):
    """Job rows (GET /api/jobs) say who started each job, so the page can tell the agent's commands from the
    user's; a job recorded before origin existed is the agent's when its chat has the job's run_command card."""
    import httpx
    app = create_app(settings)
    manager = app.state.manager
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            session_id = (await client.post("/api/sessions")).json()["id"]
            query = f"?session_id={session_id}"
            terminal = (await client.post("/api/jobs" + query, json={"command": "printf terminal"})).json()
            assert terminal["origin"] == "user"
            await manager.jobs.wait(terminal["id"])
            session = manager.get(session_id)
            session["permission_mode"] = "bypassPermissions"
            manager.live[session_id] = session
            agent = json.loads(await manager.execute_tool(session, manager.session_tools(session), "run_command",
                                                          {"command": "printf agent"}, "call-1"))
            manager.live.pop(session_id)
            manager.start_shell(session_id, "printf bang")
            await asyncio.wait_for(manager.tasks[session_id], 10)
            bang = next(event["job_id"] for event in manager.get(session_id)["events"] if event.get("name") == "user_command")
            rows = (await client.get("/api/jobs" + query)).json()
            assert {row["id"]: row["origin"] for row in rows} == {terminal["id"]: "user", agent["job_id"]: "agent",
                                                                  bang: "user"}
            for job_id in (agent["job_id"], terminal["id"]):
                manager.jobs._jobs[job_id].pop("origin")  # As jobs recorded before origin existed.
            # Such a job is the agent's when the chat has its run_command card, else the user's.
            rows = (await client.get("/api/jobs" + query)).json()
            assert {row["id"]: row["origin"] for row in rows} == {terminal["id"]: "user", agent["job_id"]: "agent",
                                                                  bang: "user"}
            assert (await client.get(f"/api/jobs/{agent['job_id']}" + query)).json()["origin"] == "agent"
            assert (await client.get(f"/api/jobs/{terminal['id']}" + query)).json()["origin"] == "user"
