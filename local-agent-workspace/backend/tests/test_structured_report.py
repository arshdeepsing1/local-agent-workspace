"""submit_report: a subagent's structured final report (summary, findings with path and line, files changed,
next steps). It ends the subagent's delegated turn, the parent's result carries it next to the prose output,
an invalid one is a tool error the model can retry, and a subagent that never calls it reports in prose."""
import asyncio
import json

import pytest

from local_agent.feature_tools import FEATURE_TOOLS
from local_agent.planning import (REPORT, REPORT_SUBMITTED, REPORT_SUBMITTED_REPLY, RESULT_BYTES, bounded_result,
                                  report_text)

from test_parallel_subagents import (Gateway, calls_reply, card, delegate, eventually, make_runtime, reply, run_turn,
                                     tool_messages)

REPORT_ARGUMENTS = {"summary": "Two call sites read the setting.",
                    "findings": [{"path": "a.txt", "line": 1, "text": "Reads it at start."}, {"text": "No tests cover it."}],
                    "files_changed": [], "next_steps": ["Add a test."]}


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    async for value in make_runtime(tmp_path, monkeypatch):
        yield value

def child_steps(*responses):
    return lambda count, body: asyncio.sleep(0, responses[min(count, len(responses)) - 1])


def offered(body):
    return {tool["function"]["name"] for tool in body["tools"]}


def test_the_tool_is_short_and_bounded():
    definition = next(item["function"] for item in FEATURE_TOOLS if item["function"]["name"] == "submit_report")
    assert len(definition["description"]) <= 400
    properties = definition["parameters"]["properties"]
    assert definition["parameters"]["required"] == ["summary"]
    assert (properties["summary"]["minLength"], properties["summary"]["maxLength"]) == (1, 2000)
    assert properties["findings"]["maxItems"] == 30 and properties["files_changed"]["maxItems"] == 20
    assert properties["next_steps"]["maxItems"] == 10
    assert "submit_report" in REPORT


async def test_a_report_ends_the_subagents_turn_and_reaches_the_parent(runtime, monkeypatch):
    manager, parent, _ = runtime
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a")), lambda: reply("Parent done.")],
                      {"Survey": child_steps(calls_reply(("submit_report", REPORT_ARGUMENTS, "s1")))})
    saved = await run_turn(manager, parent)
    assert len(gateway.requests["Survey"]) == 1  # No request after the report.
    assert "submit_report" in offered(gateway.requests["Survey"][0])
    assert "submit_report" not in offered(gateway.requests["parent"][0])
    result = json.loads(card(saved, "a")["output"])
    assert result["report"] == REPORT_ARGUMENTS and result["output"] == "" and result["status"] == "completed"
    child = manager.store.get(result["child_session_id"])
    assert child["wire"][-1] == {"role": "assistant", "content": REPORT_SUBMITTED_REPLY}
    tool = next(event for event in child["events"] if event.get("name") == "submit_report")
    assert (tool["state"], tool["output"]) == ("completed", REPORT_SUBMITTED)
    assert child["terminal_reason"] == "completed"
    assert report_text(result["report"]) == ("Summary: Two call sites read the setting.\nFindings:\n"
                                             "- a.txt:1: Reads it at start.\n- No tests cover it.\nNext steps:\n- Add a test.")


@pytest.mark.parametrize("background", [False, True])
async def test_an_invalid_report_is_a_tool_error_and_the_model_retries(runtime, monkeypatch, background):
    manager, parent, _ = runtime
    bad = {"findings": [{"path": "a.txt", "line": 0}]}  # No summary, a finding without text, line 0.
    gateway = Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a", **({"background": True} if background
                                                                                    else {}))),
                                    lambda: reply("Parent done.")],
                      {"Survey": child_steps(calls_reply(("submit_report", bad, "s1")),
                                             calls_reply(("submit_report", {"summary": "Fixed."}, "s2")))})
    saved = await run_turn(manager, parent)
    if background:
        await eventually(lambda: manager.delegates.background_running(parent["id"]) == 0)
        saved = manager.store.get(parent["id"])
        result = card(saved, "a")["delegation_result"]
        assert saved["events"][-1]["text"] == "Background subagent general finished: Survey"
    else:
        result = json.loads(card(saved, "a")["output"])
    assert len(gateway.requests["Survey"]) == 2
    child = manager.store.get(result["child_session_id"])
    first = dict(tool_messages(child["wire"]))["s1"]
    assert '"error_kind": "invalid_arguments"' in first and REPORT_SUBMITTED not in first
    # The refused call stays an error in the child's transcript, but the report it then sent completes the run.
    assert [event["state"] for event in child["events"] if event.get("name") == "submit_report"] == ["error", "completed"]
    assert (result["status"], result["terminal_reason"], child["terminal_reason"]) == ("completed", "completed", "completed")
    assert result["report"] == {"summary": "Fixed."} and not result["output"].startswith("Errors:")


async def test_a_subagent_without_a_report_still_reports_in_prose(runtime, monkeypatch):
    manager, parent, _ = runtime
    Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a")), lambda: reply("Parent done.")],
            {"Survey": child_steps(reply("Found it at a.txt:1."))})
    saved = await run_turn(manager, parent)
    result = json.loads(card(saved, "a")["output"])
    assert result["output"] == "Found it at a.txt:1." and "report" not in result


async def test_submit_report_needs_a_delegated_turn_of_a_subagent(runtime):
    manager, parent, _ = runtime
    tools = manager.session_tools(parent)
    output = await manager.execute_tool(parent, tools, "submit_report", {"summary": "x"}, "s")
    assert output == "submit_report is only for subagents: answer in your reply."
    child = manager.store.create(manager.settings.values)
    child.update(is_subagent=True, parent_session_id=parent["id"], turn_delegated=False)
    output = await manager.execute_tool(child, manager.session_tools(child), "submit_report", {"summary": "x"}, "s")
    assert "No parent chat waits for a report in this turn" in output
    # A subagent chat the user continues is not offered it; its parent's delegated turn is.
    names = lambda: {item["function"]["name"] for item in manager.offered_tools(child, [])[0]}  # noqa: E731
    assert "submit_report" not in names()
    child["turn_delegated"] = True
    assert "submit_report" in names()
    child["turn_delegated"] = False
    # Plan mode's texts name submit_report only while a parent waits for one.
    child["permission_mode"] = "plan"
    write = ("write_file", {"path": "b.txt", "content": "x"})
    assert manager.permission(child, manager.session_tools(child), *write).message.endswith(
        "Return the plan as your final reply.")
    child["turn_delegated"] = True
    assert manager.permission(child, manager.session_tools(child), *write).message.endswith(
        "Return the plan in your report (submit_report).")


async def test_a_report_is_redacted(runtime, monkeypatch):
    manager, parent, _ = runtime
    secret = {"summary": "The token is synthetic-token.", "findings": [{"path": "a.txt", "text": "synthetic-token here"}]}
    Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a")), lambda: reply("Parent done.")],
            {"Survey": child_steps(calls_reply(("submit_report", secret, "s1")))})
    saved = await run_turn(manager, parent)
    report = json.loads(card(saved, "a")["output"])["report"]
    assert report == {"summary": "The token is [REDACTED].", "findings": [{"path": "a.txt", "text": "[REDACTED] here"}]}


async def test_a_report_is_bounded():
    huge = {"summary": "s" * 2000, "findings": [{"path": f"src/file{index}.py", "line": index + 1, "text": "t" * 500}
                                                for index in range(30)],
            "files_changed": [f"src/file{index}.py" for index in range(20)], "next_steps": ["n" * 500] * 10}
    result = bounded_result({"child_session_id": "c", "status": "completed", "changed_files": ["a"] * 20,
                             "report": huge, "output": ""})
    assert len(json.dumps(result, indent=2).encode()) <= RESULT_BYTES
    assert result["report"]["findings_total"] == 30 and len(result["report"]["findings"]) < 30
    assert result["output"] == ""


async def test_an_earlier_steps_text_does_not_go_with_the_report(runtime, monkeypatch):
    import httpx
    from test_parallel_subagents import frame
    manager, parent, _ = runtime
    narrated = httpx.Response(200, text=frame({"content": "Let me look around first.", "tool_calls": [
        {"index": 0, "id": "l1", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}]},
        "tool_calls") + "data: [DONE]\n\n")
    Gateway(monkeypatch, [lambda: calls_reply(delegate("Survey", "a")), lambda: reply("Parent done.")],
            {"Survey": child_steps(narrated, calls_reply(("submit_report", {"summary": "Done."}, "s1")))})
    saved = await run_turn(manager, parent)
    result = json.loads(card(saved, "a")["output"])
    assert result["report"] == {"summary": "Done."} and result["output"] == ""
