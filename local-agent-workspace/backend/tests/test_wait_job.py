"""wait_job: wait for a job of this conversation to end or to print an expected line."""
import asyncio
import json
import shlex
import sys
import time

import pytest

from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.permissions import READ_TOOLS
from local_agent.store import Store
from local_agent.tool_profiles import READ_TOOLS as PROFILE_READ_TOOLS
from local_agent.tools import TOOL_DEFINITIONS, WorkspaceTools


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://fake.example", "synthetic-secret-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    manager.live[session["id"]] = session
    yield manager, session, WorkspaceTools(session["workspace"])
    await manager.jobs.shutdown()
    store.db.close()


async def background(manager, session, command):
    return await manager.jobs.start(command, session["workspace"], session_id=session["id"], background=True)


async def wait(manager, session, tools, **arguments):
    output = await manager.execute_tool(session, tools, "wait_job", arguments, "wait")
    try:
        return json.loads(output)
    except ValueError:
        return output


def test_wait_job_is_a_read_tool_listed_before_stop_job():
    names = [item["function"]["name"] for item in TOOL_DEFINITIONS]
    assert names.index("get_job_output") + 1 == names.index("wait_job") == names.index("stop_job") - 1
    assert "wait_job" in READ_TOOLS and "wait_job" not in PROFILE_READ_TOOLS


async def test_wait_until_a_line_matches(runtime):
    manager, session, tools = runtime
    job = await background(manager, session, "printf 'booting\\n'; sleep .2; printf 'READY on 8042\\n'; sleep 30")
    result = await asyncio.wait_for(wait(manager, session, tools, job_id=job["id"], until_regex=r"READY on \d+",
                                         from_offset=0), 10)
    assert result["reason"] == "matched" and result["state"] == "running"
    assert result["match"] == {"line": "READY on 8042", "offset": len("booting\n")}
    assert result["output"] == "booting\nREADY on 8042\n" and result["output_start"] == 0
    assert result["next_offset"] == result["total_chars"] == len(result["output"])
    # Waiting again from next_offset only counts new output.
    again = await wait(manager, session, tools, job_id=job["id"], until_regex="READY", from_offset=result["next_offset"],
                       timeout_seconds=1)
    assert again["reason"] == "timeout" and again["match"] is None and again["output"] == ""
    assert manager.jobs.get(job["id"])["state"] == "running"


async def test_wait_until_the_job_exits(runtime):
    manager, session, tools = runtime
    job = await background(manager, session, "sleep .2; printf 'all done'; exit 4")
    result = await asyncio.wait_for(wait(manager, session, tools, job_id=job["id"]), 10)
    assert result["reason"] == "exited" and result["state"] == "failed" and result["exit_code"] == 4
    assert result["output"] == "all done" and result["match"] is None
    # A finished job's final line without a newline is matched too.
    result = await wait(manager, session, tools, job_id=job["id"], until_regex="done$", from_offset=0)
    assert result["reason"] == "matched" and result["match"]["line"] == "all done"


async def test_wait_times_out_without_stopping_the_job(runtime):
    manager, session, tools = runtime
    job = await background(manager, session, "sleep 30")
    started = time.monotonic()
    result = await wait(manager, session, tools, job_id=job["id"], timeout_seconds=1)
    assert result["reason"] == "timeout" and 0.9 <= time.monotonic() - started < 3
    assert manager.jobs.get(job["id"])["state"] == "running"


async def test_from_offset_in_the_middle_of_a_line(runtime):
    manager, session, tools = runtime
    job = await background(manager, session, "printf 'abc MARK\\nxyz MARK\\n'")
    await manager.jobs.wait(job["id"])
    result = await wait(manager, session, tools, job_id=job["id"], until_regex="MARK", from_offset=6)
    assert result["match"] == {"line": "xyz MARK", "offset": 9}
    assert result["output"] == "RK\nxyz MARK\n" and result["output_start"] == 6
    result = await wait(manager, session, tools, job_id=job["id"], until_regex="^MARK", from_offset=4)
    assert result["match"] == {"line": "MARK", "offset": 4}


async def test_invalid_arguments_and_other_conversations(runtime):
    manager, session, tools = runtime
    job = await background(manager, session, "printf 'twelve chars'")
    await manager.jobs.wait(job["id"])
    invalid = await wait(manager, session, tools, job_id=job["id"], until_regex="(")
    assert invalid["error_kind"] == "invalid_arguments" and invalid["message"].startswith("Invalid until_regex")
    beyond = await wait(manager, session, tools, job_id=job["id"], from_offset=13)
    assert beyond["message"] == "from_offset is beyond the job output (12 characters)."
    other = manager.store.create(manager.settings.values)
    foreign = await manager.jobs.start("sleep 30", session["workspace"], session_id=other["id"], background=True)
    assert (await wait(manager, session, tools, job_id=foreign["id"]))["message"] == "Job not found in this conversation."
    assert manager.jobs.get(foreign["id"])["state"] == "running"


async def test_cancelling_the_turn_ends_the_wait_but_not_the_job(runtime):
    manager, session, tools = runtime
    job = await background(manager, session, "sleep 30")
    task = asyncio.create_task(wait(manager, session, tools, job_id=job["id"], timeout_seconds=60))
    await asyncio.sleep(.2)
    assert not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert manager.jobs.get(job["id"])["state"] == "running"


async def test_a_pathological_regex_is_stopped_without_blocking_the_event_loop(runtime):
    manager, session, tools = runtime
    script = "import sys, time; sys.stdout.write('a' * 70000 + '!\\n'); sys.stdout.flush(); time.sleep(30)"
    job = await background(manager, session, shlex.quote(sys.executable) + " -c " + shlex.quote(script))
    async with asyncio.timeout(5):
        while not manager.jobs.get(job["id"])["output"].endswith("!\n"):
            await asyncio.sleep(.02)
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(.001)

    beating = asyncio.create_task(heartbeat())
    try:
        result = await asyncio.wait_for(wait(manager, session, tools, job_id=job["id"], until_regex="(a|aa)+$",
                                             from_offset=0), 5)
    finally:
        beating.cancel()
        await asyncio.gather(beating, return_exceptions=True)
    assert result["message"] == "until_regex took too long; simplify it." and result["error_kind"] == "limit"
    assert ticks >= 2


async def test_a_regex_whose_counted_repeats_expand_too_far_is_refused_before_compiling(runtime):
    # Compiling '(?:a{3000}){3000}' alone blocks the event loop for seconds; '(?:x{65535}){65535}' needs gigabytes.
    manager, session, tools = runtime
    job = await background(manager, session, "printf 'ready'")
    await manager.jobs.wait(job["id"])
    for pattern in ("(?:a{3000}){3000}", "(?:x{65535}){65535}", "(?x)#[\n(?:a{3000}){3000}", "a{10000000}"):
        started = time.monotonic()
        result = await wait(manager, session, tools, job_id=job["id"], until_regex=pattern, from_offset=0)
        assert time.monotonic() - started < 1
        assert result["error_kind"] == "invalid_arguments" and "counted repeats" in result["message"], result
    result = await wait(manager, session, tools, job_id=job["id"], until_regex=r"(?:r\w{1,3}){1}y{1}", from_offset=0)
    assert result["match"]["line"] == "ready"


async def test_long_new_output_is_cut_to_its_end(runtime):
    manager, session, tools = runtime
    job = await background(manager, session, "seq 1 5000")
    await manager.jobs.wait(job["id"])
    result = await wait(manager, session, tools, job_id=job["id"], from_offset=0)
    total = len(manager.jobs.get(job["id"])["output"])
    assert result["output_start"] == total - 4000 and result["output"].endswith("5000\n")
    assert len(json.dumps(result, indent=2).encode()) <= 8000
    job = await background(manager, session, shlex.quote(sys.executable) + " -c " + shlex.quote("print('\\u00e9' * 3000)"))
    await manager.jobs.wait(job["id"])
    result = await wait(manager, session, tools, job_id=job["id"], from_offset=0)
    assert len(json.dumps(result, indent=2).encode()) <= 8000 and result["output_start"] > 0


async def test_a_removed_job_ends_the_wait(runtime):
    manager, session, tools = runtime
    job = await background(manager, session, "sleep 30")
    watcher = asyncio.create_task(manager.jobs.watch(job["id"], 0, None, 30))
    await asyncio.sleep(.1)
    manager.jobs._jobs.pop(job["id"])
    await manager.jobs._changed()
    with pytest.raises(ValueError, match="no longer available"):
        await asyncio.wait_for(watcher, 5)


async def test_a_line_printed_after_the_last_result_still_counts_by_default(runtime):
    manager, session, tools = runtime
    session["permission_mode"] = "bypassPermissions"
    started = json.loads(await manager.execute_tool(session, tools, "run_command", {
        "command": "sleep .2; printf 'Local: http://localhost:5173 ready\\n'; sleep 30", "background": True}, "bg"))
    assert started["total_chars"] == 0
    async with asyncio.timeout(5):  # The line is printed before the model calls wait_job.
        while "ready" not in manager.jobs.get(started["job_id"])["output"]:
            await asyncio.sleep(.02)
    result = await wait(manager, session, tools, job_id=started["job_id"], until_regex="ready", timeout_seconds=1)
    assert result["reason"] == "matched" and result["output_start"] == 0
    assert result["match"]["line"] == "Local: http://localhost:5173 ready"
    # The default then starts after what that result showed.
    again = await wait(manager, session, tools, job_id=started["job_id"], until_regex="ready", timeout_seconds=1)
    assert again["reason"] == "timeout" and again["output_start"] == result["total_chars"]
    await manager.jobs.stop(started["job_id"])


# Sizing waits to the job (expected durations, idle output, suggested next wait)

def test_suggested_waits_follow_earlier_runs_or_back_off():
    from local_agent.jobs import suggested_wait
    assert suggested_wait(None, 0) is None
    assert suggested_wait(40, 10) == 40  # 30 seconds left, a quarter more, plus 2.
    assert suggested_wait(3, 1) == 5  # At least 5 seconds.
    assert suggested_wait(1000, 0) == 300  # At most the wait_job limit.
    assert suggested_wait(10, 20, previous=30) == 60  # Running longer than usual: twice the last wait.
    assert suggested_wait(None, 5, previous=3) == 10
    assert suggested_wait(None, 5, previous=200) == 300


async def test_a_wait_returns_when_the_output_goes_quiet(runtime):
    manager, session, tools = runtime
    job = await background(manager, session, "printf 'listening on 8000\\n'; sleep 30")
    async with asyncio.timeout(5):
        while not manager.jobs.get(job["id"])["output"]:
            await asyncio.sleep(.02)
    started = time.monotonic()
    result = await wait(manager, session, tools, job_id=job["id"], idle_seconds=1, from_offset=0)
    assert result["reason"] == "idle" and 0.9 <= time.monotonic() - started < 3
    assert result["output"] == "listening on 8000\n" and result["state"] == "running"
    # No earlier run says how long it takes: the next wait doubles the default one.
    assert result["waited_seconds"] == 60 and result["next_wait_seconds"] == 120
    assert "expected_seconds" not in result and result["elapsed_seconds"] >= 0.9
    invalid = await wait(manager, session, tools, job_id=job["id"], idle_seconds=0)
    assert invalid["message"] == "Invalid tool arguments at idle_seconds: JSON Schema minimum constraint failed."


async def test_earlier_runs_of_the_same_command_size_the_default_wait(runtime):
    manager, session, tools = runtime
    command = "sleep .3; printf done"
    for _ in range(2):
        finished = await background(manager, session, command)
        await manager.jobs.wait(finished["id"])
    other = await background(manager, session, "sleep .1")  # Another command does not count.
    await manager.jobs.wait(other["id"])
    job = await background(manager, session, command)
    expected = manager.jobs.expected_seconds(job)
    assert 0.25 <= expected < 2
    result = await wait(manager, session, tools, job_id=job["id"])
    assert result["reason"] == "exited" and result["waited_seconds"] == 5  # Not the 60-second default.
    assert result["expected_seconds"] == round(expected, 1) and "next_wait_seconds" not in result
    # A run that was stopped says nothing about how long the command takes.
    stopped = await background(manager, session, "sleep 30")
    await manager.jobs.stop(stopped["id"])
    assert manager.jobs.expected_seconds(await background(manager, session, "sleep 30")) is None


async def test_a_background_command_says_how_long_earlier_runs_took(runtime):
    manager, session, tools = runtime
    session["permission_mode"] = "bypassPermissions"
    command = "sleep .2; printf built"
    first = json.loads(await manager.execute_tool(session, tools, "run_command", {"command": command, "background": True}, "c1"))
    assert "expected_seconds" not in first
    await manager.jobs.wait(first["job_id"])
    second = json.loads(await manager.execute_tool(session, tools, "run_command", {"command": command, "background": True}, "c2"))
    assert 0.15 <= second["expected_seconds"] < 2 and second["next_wait_seconds"] == 5
    await manager.jobs.wait(second["job_id"])
