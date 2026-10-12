"""Hidden continuation after a response whose tool calls could not run.

Invalid or incomplete tool arguments, and a stream that closed cleanly without a
usable finish reason, get up to two hidden retry requests. A provider safety
stop, an empty response and a stream cut mid-event end the turn instead.
"""
import json

import httpx
import pytest

from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.context import INCOMPLETE_RESPONSE_CONTINUATION, INVALID_ARGUMENTS_CONTINUATION
from local_agent.model_stream import MAX_TOOL_CALLS
from local_agent.store import Store


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    settings.redact = lambda text: text.replace("private-token", "[REDACTED]")
    store = Store(tmp_path / "tests.sqlite3")
    session = store.create(settings.values)
    session.update(title="Repairs", title_generated=True)
    yield AgentManager(store, settings), session
    store.db.close()


def frame(delta, finish=None):
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"


def reply(text="Done.", finish="stop"):
    return httpx.Response(200, text=frame({"content": text}, finish) + "data: [DONE]\n\n")


def tool_reply(calls, text="", finish="tool_calls"):
    parts = [{"index": index, "id": f"call-{index}", "function": {"name": name, "arguments": arguments}}
             for index, (name, arguments) in enumerate(calls)]
    return httpx.Response(200, text=frame({"content": text, "tool_calls": parts}, finish) + "data: [DONE]\n\n")


BROKEN = [("list_files", "{}"), ("write_file", '{"path": "a.txt", "content": "unfinished')]


def mock_gateway(monkeypatch, gateway):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(gateway)))


def scripted(monkeypatch, responses):
    """Answer streamed requests with the given responses in order; record their bodies."""
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        return responses[min(len(requests), len(responses)) - 1]()
    mock_gateway(monkeypatch, gateway)
    return requests


def no_adjacent_users(messages):
    roles = [message["role"] for message in messages]
    return all(not (first == second == "user") for first, second in zip(roles, roles[1:]))


def replies(session):
    return [event for event in session["events"] if event["type"] == "assistant"]


async def test_invalid_arguments_get_a_hidden_retry_and_the_turn_completes(runtime, monkeypatch):
    manager, session = runtime
    requests = scripted(monkeypatch, [lambda: tool_reply(BROKEN, "Writing the file."), lambda: reply("Wrote it.")])
    manager.execute_tool = lambda *args: pytest.fail("No call from an invalid batch may run")
    await manager.run(session, "Write a.txt")

    assert len(requests) == 2
    placeholder, continuation = requests[1]["messages"][-2:]
    assert continuation == {"role": "user", "content": INVALID_ARGUMENTS_CONTINUATION}
    assert placeholder["role"] == "assistant" and "tool_calls" not in placeholder
    assert placeholder["content"].startswith("Writing the file.\n\n[The model returned invalid or incomplete tool arguments.")
    # The detail names the call without echoing its content.
    assert "Tool call 2 (write_file) has arguments that are not valid JSON (error at character" in placeholder["content"]
    assert "unfinished" not in placeholder["content"]
    assert no_adjacent_users(requests[1]["messages"])

    first, second = replies(session)
    # Not 'interrupted', which would offer Continue.
    assert first["request_info"]["status"] == "error" and first["request_info"]["error_kind"] == "invalid_tool_arguments"
    assert second["text"] == "Wrote it." and second["request_info"]["status"] == "completed"
    assert [event["text"] for event in session["events"] if event["type"] == "notice"] == [
        "The model returned invalid tool arguments; none of its tool calls ran. Asking it to retry automatically (1 of 2)."]
    assert not any(event["type"] == "error" for event in session["events"])
    assert session["terminal_reason"] == "completed"
    assert [call.get("retry_reason") for call in session["inference_calls"]] == [None, "invalid_tool_arguments"]


async def test_three_invalid_responses_end_the_turn_after_two_retries(runtime, monkeypatch):
    manager, session = runtime
    requests = scripted(monkeypatch, [lambda: tool_reply(BROKEN)])
    await manager.run(session, "Write a.txt")
    assert len(requests) == 3
    assert session["events"][-1]["type"] == "error" and session["events"][-1]["error_kind"] == "invalid_tool_arguments"
    assert session["terminal_reason"] == "inference_error"
    assert [text[-9:] for text in (event["text"] for event in session["events"] if event["type"] == "notice")] == [
        "(1 of 2).", "(2 of 2)."]


async def test_repair_count_resets_after_a_response_whose_tools_ran(runtime, monkeypatch):
    manager, session = runtime
    requests = scripted(monkeypatch, [
        lambda: tool_reply(BROKEN), lambda: tool_reply(BROKEN), lambda: tool_reply([("list_files", "{}")]),
        lambda: tool_reply(BROKEN), lambda: tool_reply(BROKEN), lambda: reply("All done.")])
    await manager.run(session, "Inspect and write")
    assert len(requests) == 6 and replies(session)[-1]["text"] == "All done."
    assert session["terminal_reason"] == "completed"


async def test_too_many_tool_calls_get_a_placeholder_reply_before_the_retry(runtime, monkeypatch):
    manager, session = runtime
    calls = [("list_files", "{}")] * (MAX_TOOL_CALLS + 1)
    requests = scripted(monkeypatch, [lambda: tool_reply(calls, "Listing everything."), lambda: reply("Fewer calls.")])
    manager.execute_tool = lambda *args: pytest.fail("No call from an oversized batch may run")
    await manager.run(session, "List all folders")
    assert len(requests) == 2
    placeholder, continuation = requests[1]["messages"][-2:]
    assert placeholder["role"] == "assistant" and "more than 64 tool calls" in placeholder["content"]
    assert placeholder["content"].startswith("Listing everything.")
    assert continuation["content"] == INVALID_ARGUMENTS_CONTINUATION
    assert no_adjacent_users(requests[1]["messages"]) and no_adjacent_users(session["wire"])
    assert session["terminal_reason"] == "completed"


async def test_clean_end_without_a_finish_reason_is_repaired(runtime, monkeypatch):
    manager, session = runtime
    requests = scripted(monkeypatch, [lambda: reply("I will now", None), lambda: reply("Finished.")])
    await manager.run(session, "Do it")
    assert len(requests) == 2
    assert requests[1]["messages"][-1] == {"role": "user", "content": INCOMPLETE_RESPONSE_CONTINUATION}
    assert requests[1]["messages"][-2]["content"].startswith("I will now\n\n[The model response ended before completion.")
    assert [event["text"] for event in session["events"] if event["type"] == "notice"] == [
        "The model response ended early; none of its tool calls ran. Asking it to continue automatically (1 of 2)."]
    assert replies(session)[0]["request_info"]["error_kind"] == "incomplete_response"
    assert session["inference_calls"][-1]["retry_reason"] == "incomplete_response"
    assert session["terminal_reason"] == "completed"


async def test_content_filter_stop_is_shown_and_never_re_prompted(runtime, monkeypatch):
    manager, session = runtime
    requests = scripted(monkeypatch, [lambda: reply("I can't", "content_filter"), lambda: reply("Retried.")])
    await manager.run(session, "Do it")
    assert len(requests) == 1
    assert replies(session)[0]["request_info"]["finish_reason"] == "content_filter"
    assert session["events"][-1]["type"] == "error" and session["events"][-1]["error_kind"] == "incomplete_response"
    assert not any(event["type"] == "notice" for event in session["events"])
    assert session["wire"][-1]["role"] == "assistant"


async def test_no_hidden_retry_without_another_agent_step(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values["max_agent_steps"] = 1
    requests = scripted(monkeypatch, [lambda: tool_reply(BROKEN), lambda: reply()])
    await manager.run(session, "Write a.txt")
    assert len(requests) == 1 and session["events"][-1]["error_kind"] == "invalid_tool_arguments"


async def test_unanswered_hidden_retry_is_removed_before_the_next_real_prompt(runtime, monkeypatch):
    manager, session = runtime
    requests = scripted(monkeypatch, [lambda: tool_reply(BROKEN, "Started."), lambda: httpx.Response(500, text="Down"),
                                      lambda: reply("Recovered.")])
    await manager.run(session, "Write a.txt")
    assert session["wire"][-1] == {"role": "user", "content": INVALID_ARGUMENTS_CONTINUATION}
    restarted = AgentManager(manager.store, manager.settings)
    saved = restarted.get(session["id"])
    await restarted.run(saved, "Try again with a smaller file.")
    sent = requests[-1]["messages"]
    assert sent[-1] == {"role": "user", "content": "Try again with a smaller file."}
    assert INVALID_ARGUMENTS_CONTINUATION not in json.dumps(sent) and no_adjacent_users(sent)
