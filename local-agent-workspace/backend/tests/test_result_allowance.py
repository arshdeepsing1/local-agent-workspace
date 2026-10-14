"""Tool output bounds: the per-response result allowance and the 128 KiB hard cap (u-large-inputs item 5)."""
import json

import httpx
import pytest

from local_agent import agents, result_budget
from local_agent.agents import AgentManager, repair_tool_history
from local_agent.config import Settings
from local_agent.result_budget import (TOOL_RESULT_HARD_LIMIT, ResultAllowance, bound_result, cap_text,
                                       shorten_result)
from local_agent.store import Store
from local_agent.tools import WorkspaceTools

ORIGINAL_CLIENT = httpx.AsyncClient


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="", compaction_handoffs=False)
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session.update(title="Allowance", title_generated=True, permission_mode="bypassPermissions")
    yield manager, session, project
    store.db.close()


def frame(delta, finish=None):
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"


def calls_stream(calls, prefix="call"):
    parts = [{"index": index, "id": f"{prefix}{index}", "function": {"name": name, "arguments": json.dumps(arguments)}}
             for index, (name, arguments) in enumerate(calls)]
    return httpx.Response(200, text=frame({"content": "", "tool_calls": parts}, "tool_calls") + "data: [DONE]\n\n")


def done():
    return httpx.Response(200, text=frame({"content": "Done."}, "stop") + "data: [DONE]\n\n")


def gateway(monkeypatch, steps):
    """Streamed agent requests get the next step's response; compaction requests a summary."""
    requests, summaries = [], []

    async def handle(request):
        body = json.loads(request.content)
        if body.get("stream") is False:
            summaries.append(body)
            return httpx.Response(200, json={"choices": [{"message": {"content": "## Goal and current status\nOK"},
                                                          "finish_reason": "stop"}]})
        requests.append(body)
        step = steps[min(len(requests), len(steps)) - 1]
        return step(body) if callable(step) else step
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: ORIGINAL_CLIENT(
        **kwargs, transport=httpx.MockTransport(handle)))
    return requests, summaries


def tool_events(session, name=None):
    return [event for event in session["events"] if event["type"] == "tool" and (name is None or event["name"] == name)]


def tool_messages(session):
    return {message["tool_call_id"]: message["content"] for message in session["wire"] if message["role"] == "tool"}


def page_file(project, name, lines=420):
    (project / name).write_text("".join(f"{name} line {number:04d} " + "z" * 60 + "\n" for number in range(lines)))


async def test_reads_beyond_the_allowance_are_deferred_and_not_seen(runtime, monkeypatch):
    manager, session, project = runtime
    names = [f"f{index}.txt" for index in range(8)]
    for name in names:
        page_file(project, name)
    requests, summaries = gateway(monkeypatch, [
        calls_stream([("read_file", {"path": name}) for name in names]),
        lambda body: calls_stream([("write_file", {"path": withheld[0], "content": "new\n"}),
                                   ("write_file", {"path": admitted[0], "content": "new\n"})], "write"),
        # Asked again alone in a later response, a deferred read fits: each response has its own allowance.
        lambda body: calls_stream([("read_file", {"path": withheld[-1]})], "again"),
        done()])
    withheld, admitted = [], []
    original = manager.execute_tool

    async def spy(session, tools, name, arguments, call_id, origin=None, allowance=None):
        output = await original(session, tools, name, arguments, call_id, origin=origin, allowance=allowance)
        if name == "read_file":
            (withheld if "result_deferred" in output else admitted).append(arguments["path"])
        return output
    monkeypatch.setattr(manager, "execute_tool", spy)
    await manager.run(session, "Read every file")
    assert session["terminal_reason"] == "completed" and len(requests) == 4
    again = admitted.pop()  # The repeated read in the third response was sent.
    assert again == withheld[-1]
    assert 2 <= len(admitted) < 8 and admitted == names[:len(admitted)] and withheld == names[len(admitted):]
    events = tool_events(session, "read_file")[:8]
    wire = tool_messages(session)
    for event in events:
        assert event["state"] == "completed" and json.loads(event["output"])["path"] in names
        if event["input"]["path"] in withheld:
            deferred = json.loads(event["model_output"])
            assert deferred["error_kind"] == "result_deferred" and "already use this step's context allowance" in \
                deferred["message"] and deferred["hint"].startswith("Call it again in your next response")
            assert event["result_withheld"]["allowance_tokens"] > 0
            assert wire[event["call_id"]] == event["model_output"]
        else:
            assert "model_output" not in event and wire[event["call_id"]] == event["output"]
    views = session["file_views"]
    assert {path for path in names if str(project / path) in views} == {*admitted, again}
    refused, written = tool_events(session, "write_file")
    assert refused["state"] == "rejected" and (project / withheld[0]).read_text() != "new\n"
    assert written["state"] == "completed" and (project / admitted[0]).read_text() == "new\n"
    # The second request carried every admitted result and fit the budget.
    assert sum("result_deferred" in message.get("content", "") for message in requests[1]["messages"]
               if message["role"] == "tool") == len(withheld)


async def test_the_allowance_counts_large_tool_definitions(runtime, monkeypatch):
    manager, session, project = runtime
    manager.settings.values.update(context_window=32768, max_output_tokens=4096)
    names = [f"r{index}.txt" for index in range(6)]
    for name in names:
        page_file(project, name, 110)
    # Large external (MCP-like) tool definitions: about 8,000 estimated tokens.
    extra = [{"type": "function", "function": {"name": f"mcp__big__tool{index}", "description": "d" * 6000,
                                                "parameters": {"type": "object", "properties": {}}}}
             for index in range(4)]
    step_definitions = manager.step_definitions
    monkeypatch.setattr(manager, "step_definitions", lambda session, external: [*step_definitions(session, external),
                                                                                *extra])
    session["wire"] = [message for number in range(3) for message in (
        {"role": "user", "content": f"Earlier question {number} " + "q" * 9000},
        {"role": "assistant", "content": f"Earlier answer {number}."})]
    requests, summaries = gateway(monkeypatch, [calls_stream([("read_file", {"path": name}) for name in names]),
                                                done()])
    await manager.run(session, "Read them")
    errors = [event["text"] for event in session["events"] if event["type"] == "error"]
    assert errors == [] and session["terminal_reason"] == "completed" and len(requests) == 2
    assert any("result_deferred" in event.get("model_output", "") for event in tool_events(session, "read_file"))


async def test_a_single_read_larger_than_the_allowance_asks_for_a_smaller_range(runtime):
    manager, session, project = runtime
    page_file(project, "big.txt")
    tools = manager.session_tools(session)
    allowance = ResultAllowance(100)
    output = await manager.execute_tool(session, tools, "read_file", {"path": "big.txt"}, "c1", allowance=allowance)
    deferred = json.loads(output)
    assert deferred["error_kind"] == "result_deferred" and "is larger than this step's context allowance (100 tokens)" \
        in deferred["message"]
    assert deferred["hint"].startswith("Read a smaller range (start_line and max_lines), or use outline_file")
    event, = tool_events(session, "read_file")
    assert event["result_withheld"]["allowance_tokens"] == 100 and event["result_withheld"]["estimated_tokens"] > 100
    assert str(project / "big.txt") not in (session.get("file_views") or {})


async def test_job_results_are_charged_never_deferred(runtime):
    manager, session, project = runtime
    tools = manager.session_tools(session)
    ran = json.loads(await manager.execute_tool(session, tools, "run_command",
                                                {"command": "printf 'job output line\\n%.0s' 1 2 3"}, "c1"))
    job_id = ran["job_id"]
    allowance = ResultAllowance(0)
    for name, arguments in (("get_job_output", {"job_id": job_id}), ("wait_job", {"job_id": job_id, "from_offset": 0}),
                            ("list_jobs", {})):
        output = await manager.execute_tool(session, tools, name, arguments, f"c-{name}", allowance=allowance)
        assert "result_deferred" not in output and "result_shortened" not in output
    assert allowance.left < 0
    total = len(manager.jobs.get(job_id)["output"])
    assert manager.__dict__["_job_seen"][job_id][0] == total
    # The next wait_job starts after the output the model saw.
    waited = json.loads(await manager.execute_tool(session, tools, "wait_job", {"job_id": job_id}, "c-again"))
    assert waited.get("output", "") == ""


async def test_a_command_beyond_the_allowance_is_shortened_not_withheld(runtime):
    manager, session, project = runtime
    tools = manager.session_tools(session)
    script = project / "count.sh"
    script.write_text("echo ran >> runs.txt\nfor i in $(seq 1 400); do echo \"line $i of the output\"; done\n"
                      "echo FINAL-FAILURE-DETAIL\nexit 3\n")
    output = await manager.execute_tool(session, tools, "run_command", {"command": "sh count.sh"}, "c1",
                                        allowance=ResultAllowance(10))
    shortened = json.loads(output)
    assert shortened["result_shortened"] is True and shortened["exit_code"] == 3 and shortened["job_id"]
    assert shortened["hint"].startswith("This step's context allowance was used up")
    assert "FINAL-FAILURE-DETAIL" in shortened["output"][-500:] and len(output) < 3000
    event, = tool_events(session, "run_command")
    assert event["result_shortened"] is True and event["model_output"] == output
    assert len(event["output"]) > len(output) and json.loads(event["output"])["exit_code"] == 3
    assert (project / "runs.txt").read_text() == "ran\n"


async def test_a_huge_result_is_capped_for_the_card_and_the_model(runtime, monkeypatch):
    manager, session, project = runtime
    tools = manager.session_tools(session)

    async def huge(self, name, arguments, **options):
        return {"files": ["a.py"], "content": "é" * 150_000 + "END", "total": 1}
    monkeypatch.setattr(WorkspaceTools, "execute", huge)
    output = await manager.execute_tool(session, tools, "find_files", {"pattern": "*.py"}, "c1")
    assert len(output.encode()) <= TOOL_RESULT_HARD_LIMIT
    parsed = json.loads(output)
    assert parsed["truncated"] is True and parsed["omitted_bytes"] > 0 and parsed["content"].endswith("END")
    event, = tool_events(session, "find_files")
    assert event["output"] == output

    async def failing(self, name, arguments, **options):
        raise ValueError("bad " + "x" * 300_000)
    monkeypatch.setattr(WorkspaceTools, "execute", failing)
    output = await manager.execute_tool(session, tools, "find_files", {"pattern": "*.py"}, "c2")
    event = tool_events(session, "find_files")[-1]
    assert len(output.encode()) <= TOOL_RESULT_HARD_LIMIT and len(event["output"].encode()) <= TOOL_RESULT_HARD_LIMIT
    assert event["state"] == "error" and output == event["model_output"]


def test_bound_result_keeps_valid_json_for_many_small_values():
    result = {"items": [f"value {index}" for index in range(40_000)]}
    bounded = bound_result(result, 50_000)
    text = json.dumps(bounded, indent=2)
    assert len(text.encode()) <= 50_000 and bounded["truncated"] is True and json.loads(text)["excerpt"]
    bounded = bound_result("é" * 1000, 500)
    assert bounded.startswith("é") and bounded.endswith("é") and len(bounded.encode()) <= 500
    assert len(cap_text("y" * 500, 200).encode()) <= 200


def test_shorten_result_keeps_short_values_and_the_ends_of_long_ones():
    result = {"job_id": "j", "state": "failed", "exit_code": 2, "background": False, "next_offset": None,
              "total_chars": 9000, "omitted": {"start": 1, "end": 2}, "output": "a" * 4000 + "TAIL" * 200}
    short = shorten_result(result)
    for key in ("job_id", "state", "exit_code", "background", "next_offset", "total_chars", "omitted"):
        assert short[key] == result[key]
    assert short["output"].endswith("TAIL" * 125) and short["omitted_bytes"] > 0 and short["result_shortened"]
    text = shorten_result("x" * 5000)
    assert text["result_shortened"] is True and len(text["excerpt"]) < 1600


def test_the_restored_tool_message_is_what_the_model_saw():
    call = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}
    wire = [{"role": "user", "content": "Read"}, {"role": "assistant", "content": None, "tool_calls": [call]}]
    deferred = json.dumps({"error_kind": "result_deferred", "message": "m", "hint": "h"})
    events = [{"id": "e1", "type": "tool", "name": "read_file", "input": {"path": "a.txt"}, "call_id": "c1",
               "state": "completed", "output": '{"content": "full"}', "model_output": deferred,
               "result_withheld": {"estimated_tokens": 9, "allowance_tokens": 1}}]
    repaired, _ = repair_tool_history(wire, events, {})
    assert repaired[-1] == {"role": "tool", "tool_call_id": "c1", "content": deferred}


async def test_without_an_allowance_results_are_not_deferred(runtime):
    """Verify runs and direct calls pass no allowance."""
    manager, session, project = runtime
    page_file(project, "v.txt")
    tools = manager.session_tools(session)
    output = await manager.execute_tool(session, tools, "read_file", {"path": "v.txt"}, "c1", origin="verify")
    assert json.loads(output)["path"] == "v.txt"


def test_the_allowance_arithmetic():
    allowance = ResultAllowance(1000, scale=1.5)
    cost = allowance.cost("x" * 300)
    assert cost == -(-(100 + result_budget.RESULT_FRAMING_TOKENS) * 3 // 2)
    assert allowance.admit("x" * 300) and allowance.left == 1000 - cost
    assert not allowance.admit("x" * 3000) and allowance.left == 1000 - cost
    allowance.charge("x" * 3000)
    assert allowance.left < 0
    assert agents.ALLOWANCE_MARGIN_TOKENS == 1024


@pytest.mark.parametrize("window, output, count, refusals", [(32768, 4096, 64, False), (24576, 2048, 48, True)])
async def test_many_reads_and_refusals_in_one_response_never_end_with_a_budget_error(runtime, monkeypatch, window,
                                                                                       output, count, refusals):
    """Every call's text reaches the next request: room for one deferred-read notice per call is kept,
    and notices and refusals are charged too, so in-turn compaction keeps them all."""
    manager, session, project = runtime
    manager.settings.values.update(context_window=window, max_output_tokens=output)
    names = [f"f{index:02d}.txt" for index in range(count)]
    for name in names:
        (project / name).write_text("".join(f"line {number:03d} " + "y" * 50 + "\n" for number in range(60)))
    calls = [("read_file", {"path": name}) for name in names]
    if refusals:
        session["disabled_tools"] = ["find_files"]
        calls[1::8] = [("find_files", {"pattern": f"*{index}.py"}) for index in range(len(calls[1::8]))]
    requests, summaries = gateway(monkeypatch, [calls_stream(calls), done()])
    made = []

    class Recorded(ResultAllowance):
        def __init__(self, *args, **options):
            super().__init__(*args, **options)
            made.append(self)
    monkeypatch.setattr(agents, "ResultAllowance", Recorded)
    await manager.run(session, "Read every file " + "r" * 3000)
    errors = [event["text"] for event in session["events"] if event["type"] == "error"]
    assert errors == [] and session["terminal_reason"] == "completed" and len(requests) == 2
    assert any("result_deferred" in event.get("model_output", "") for event in tool_events(session, "read_file"))
    if refusals:
        assert {event["state"] for event in tool_events(session, "find_files")} == {"rejected"}
    # Every call was settled against the allowance, and the next request carried a message for each.
    allowance, = made
    assert allowance.settled == count and allowance.pending == 0
    assert sum(message["role"] == "tool" for message in requests[1]["messages"]) == count


async def test_the_allowance_uses_the_scale_the_response_reported(runtime, monkeypatch):
    """The model response's reported input tokens change the estimate scale of the next request; the
    allowance follows that scale, not the one from before the response."""
    from local_agent.context import estimate_tokens
    manager, session, project = runtime
    manager.settings.values.update(context_window=32768, max_output_tokens=4096)
    names = [f"f{index:02d}.txt" for index in range(12)]
    for name in names:
        (project / name).write_text("".join(f"line {number:03d} " + "y" * 50 + "\n" for number in range(60)))

    def reported(response, body):
        usage = {"prompt_tokens": int(estimate_tokens(body["messages"], body["tools"]) * 1.4), "completion_tokens": 10}
        return httpx.Response(200, text=response.text.replace(
            "data: [DONE]", "data: " + json.dumps({"choices": [], "usage": usage}) + "\n\ndata: [DONE]"))
    requests, summaries = gateway(monkeypatch, [
        lambda body: reported(calls_stream([("read_file", {"path": name}) for name in names]), body),
        lambda body: reported(done(), body)])
    await manager.run(session, "Read every file")
    errors = [event["text"] for event in session["events"] if event["type"] == "error"]
    assert errors == [] and session["terminal_reason"] == "completed" and len(requests) == 2
    assert any("result_deferred" in event.get("model_output", "") for event in tool_events(session, "read_file"))


async def test_the_users_answers_and_plan_feedback_are_always_sent_whole(runtime, monkeypatch):
    manager, session, project = runtime
    tools = manager.session_tools(session)
    answer = "Use the staging database. " + "Keep the column order as in the spec; " * 25 + "FINAL: never drop users."
    answers = {"answers": [{"question": f"Question {index}?", "selected": [], "other": answer} for index in range(4)]}
    feedback = {"approved": False, "feedback": "Split step two. " + "Explain the migration order. " * 70}

    async def handle(session, event, name, arguments):
        return answers if name == "ask_user" else feedback
    monkeypatch.setattr(manager.interactions, "handle", handle)
    allowance = ResultAllowance(10)
    output = await manager.execute_tool(session, tools, "ask_user", {"questions": [
        {"question": "Which database?", "header": "DB", "options": [{"label": "staging"}, {"label": "prod"}]}]}, "c1",
        allowance=allowance)
    assert json.loads(output) == answers
    output = await manager.execute_tool(session, tools, "submit_plan", {"plan": "1. Migrate\n2. Test"}, "c2",
                                        allowance=allowance)
    assert json.loads(output) == feedback and allowance.left < 0
    for event in tool_events(session):
        assert "result_shortened" not in event and "model_output" not in event


async def test_a_small_action_result_is_sent_unchanged_when_the_allowance_is_used_up(runtime):
    manager, session, project = runtime
    tools = manager.session_tools(session)
    allowance = ResultAllowance(0)
    output = await manager.execute_tool(session, tools, "run_command", {"command": "echo hi"}, "c1",
                                        allowance=allowance)
    result = json.loads(output)
    assert "result_shortened" not in result and "hint" not in result and result["exit_code"] == 0
    event, = tool_events(session, "run_command")
    assert event["output"] == output and "model_output" not in event and "result_shortened" not in event
    assert allowance.left < 0  # Sent, so charged.


async def test_only_a_shortened_command_points_at_get_job_output(runtime):
    manager, session, project = runtime
    tools = manager.session_tools(session)
    created = json.loads(await manager.execute_tool(session, tools, "create_task", {
        "title": "Long task", "description": "Describe the migration. " * 150}, "c1"))
    output = json.loads(await manager.execute_tool(session, tools, "get_task", {"task_id": created["id"]}, "c2",
                                                   allowance=ResultAllowance(10)))
    assert output["result_shortened"] is True and output["omitted_bytes"] > 0
    assert output["hint"] == result_budget.SHORTENED_HINT and "get_job_output" not in output["hint"]
    assert "get_job_output" in result_budget.shortened_hint("run_command")


def test_room_for_every_calls_notice_is_kept_until_it_is_settled():
    single = ResultAllowance(10_000)
    allowance = ResultAllowance(10_000, calls=3)
    stub = allowance.stub
    assert stub >= allowance.cost(result_budget.deferred_text("read_file", 10 ** 9, 10 ** 9))
    assert allowance.left == 10_000 - 3 * stub and single.left == 10_000
    big = "x" * ((10_000 - 2 * stub - result_budget.RESULT_FRAMING_TOKENS) * 3)
    assert allowance.cost(big) == 10_000 - 2 * stub
    # The first result may use everything except the room kept for the other two calls.
    assert allowance.admit(big) and allowance.left == 0 and allowance.settled == 1
    # A small result fits in its own call's notice room; a larger one does not.
    assert not allowance.admit("y" * 3000) and allowance.settled == 1
    allowance.charge(result_budget.deferred_text("read_file", 140, 10_000))
    assert allowance.settled == 2 and allowance.left >= 0
    allowance.charge("The user turned off find_files in this chat. Use other tools.")
    assert allowance.settled == 3 and allowance.left >= 0
    # With no room left, a result as small as a notice still uses its own call's room.
    assert ResultAllowance(2 * stub, calls=2).admit("y" * (stub - result_budget.RESULT_FRAMING_TOKENS) * 3)


async def test_a_withheld_reread_neither_supersedes_the_real_read_nor_keeps_the_file_seen(runtime):
    """Compaction past a real read, with only a withheld re-read of the same file after the cut:
    the summarizer still gets the real read's content, and the file must be read again before a write."""
    from local_agent.agents import completed_call_ids, forget_summarized_reads
    from local_agent.context import summary_transcript
    manager, session, project = runtime
    page_file(project, "a.txt")
    tools = manager.session_tools(session)
    real = await manager.execute_tool(session, tools, "read_file", {"path": "a.txt"}, "c1",
                                      allowance=ResultAllowance(10 ** 6))
    withheld = await manager.execute_tool(session, tools, "read_file", {"path": "a.txt"}, "c2",
                                          allowance=ResultAllowance(10))
    assert "result_deferred" not in real and "result_deferred" in withheld
    key = str(project / "a.txt")
    assert key in session["file_views"]  # The real read's view is restored.

    def read_call(call_id):
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": call_id, "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}]}
    wire = [{"role": "user", "content": "Read a.txt"}, read_call("c1"), {"role": "tool", "tool_call_id": "c1", "content": real},
            read_call("c2"), {"role": "tool", "tool_call_id": "c2", "content": withheld}]
    completed = completed_call_ids(session)
    assert completed == {"c1"}
    text, stats = summary_transcript(wire[:3], wire[3:], 10 ** 9, completed)
    assert stats["superseded"] == 0 and "a.txt line 0000" in text
    record, holder = manager.compaction_record(session, tools)
    assert "a.txt" in record(wire, 3) and holder["forgotten"] == [key]
    session["file_views"], _ = forget_summarized_reads(session["file_views"], {}, holder["forgotten"])
    output = await manager.execute_tool(session, tools, "write_file", {"path": "a.txt", "content": "new\n"}, "c3")
    assert tool_events(session, "write_file")[-1]["state"] in ("rejected", "error") and "read" in output.lower()
    assert (project / "a.txt").read_text() != "new\n"
