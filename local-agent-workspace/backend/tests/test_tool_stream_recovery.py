import copy
import json

import httpx
import pytest

from local_agent.agents import AgentManager, OUTPUT_LIMIT_CONTINUATION, OUTPUT_LIMIT_HISTORY_PLACEHOLDER
from local_agent.config import Settings
from local_agent.store import Store


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(tmp_path / "tests.sqlite3")
    yield AgentManager(store, settings), store.create(settings.values)
    store.db.close()


def call(call_id="write", arguments='{"path":"file.txt","content":"hello"}'):
    return {"id": call_id, "type": "function", "function": {"name": "write_file", "arguments": arguments}}


def response(calls=(), finish="tool_calls", text=""):
    delta = {"content": text}
    if calls:
        delta["tool_calls"] = [{"index": index, **item} for index, item in enumerate(calls)]
    chunk = {"choices": [{"delta": delta, "finish_reason": finish}]}
    return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")


def mock_gateway(monkeypatch, gateway):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(gateway)))


@pytest.mark.parametrize("finish, calls, expected_error", [
    (None, [call()], "ended before completion"),
    ("stop", [call()], "ended before completion"),
    ("tool_calls", [call(), call("second", '{"path":"unfinished')], "invalid or incomplete"),
    ("tool_calls", [call(arguments="[]")], "invalid or incomplete"),
    ("tool_calls", [call(arguments='{"value":NaN}')], "invalid or incomplete"),
    ("tool_calls", [call(), call()], "invalid or incomplete"),
])
async def test_incomplete_batch_never_executes_or_poisons_next_turn(runtime, monkeypatch, finish, calls, expected_error):
    manager, session = runtime
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return response(calls, finish, "I started preparing the change.")
        return response(finish="stop", text="Ready to continue.")

    async def execute(*args):
        pytest.fail("No call in an incomplete or invalid batch may execute")

    manager.execute_tool = execute
    mock_gateway(monkeypatch, gateway)
    with pytest.raises(ValueError, match=expected_error):
        await manager.run_databricks(session, "Make the change.")
    assert len(requests) == 1
    assert not any(message.get("tool_calls") for message in session["wire"])
    assert "I started preparing the change." in session["wire"][-1]["content"]
    assert "No tool calls from this response ran" in session["wire"][-1]["content"]
    saved = manager.store.get(session["id"])
    restarted = AgentManager(manager.store, manager.settings)
    assert await restarted.run_databricks(saved, "Continue in smaller steps.") is True
    assert len(requests) == 2
    assert not any(message.get("tool_calls") for message in requests[-1]["messages"])


async def test_output_limit_retry_history_is_bounded_persisted_and_resets_after_valid_tool_batch(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values.update(max_output_tokens=4096, max_agent_steps=6)
    requests, executed = [], []

    async def gateway(request):
        requests.append(json.loads(request.content))
        if len(requests) in (1, 3):
            return response([call(f"unfinished-{len(requests)}", '{"path":')], "length", "Partial draft")
        if len(requests) == 2:
            return response([call("compact")])
        return response(finish="stop", text="Finished in smaller steps.")

    async def execute(session, tools, name, values, call_id):
        executed.append((name, values, call_id))
        return "Compact file written."

    manager.execute_tool = execute
    mock_gateway(monkeypatch, gateway)
    assert await manager.run_databricks(session, "Make the change.") is True
    assert len(requests) == 4
    assert all(item["max_tokens"] == 4096 for item in requests)
    assert executed == [("write_file", {"path": "file.txt", "content": "hello"}, "compact")]
    for index in (1, 3):
        assert requests[index]["messages"][-1]["role"] == "user"
        assert requests[index]["messages"][-1]["content"] == OUTPUT_LIMIT_CONTINUATION
    assert [item["content"] for item in session["wire"] if item["role"] == "user"] == [
        "Make the change.", OUTPUT_LIMIT_CONTINUATION, OUTPUT_LIMIT_CONTINUATION]
    limited = [item for item in session["wire"] if item.get("content") == OUTPUT_LIMIT_HISTORY_PLACEHOLDER]
    assert len(limited) == 2
    assert len(OUTPUT_LIMIT_HISTORY_PLACEHOLDER) < 250
    assert "Partial draft" not in json.dumps(session["wire"])
    assert not any(call["id"].startswith("unfinished") for message in session["wire"]
                   for call in message.get("tool_calls", []))
    assert [item["role"] for item in session["wire"]] == [
        "user", "assistant", "user", "assistant", "tool", "assistant", "user", "assistant"]
    limited_events = [item for item in session["events"]
                      if item.get("request_info", {}).get("error_kind") == "output_limit"]
    assert len(limited_events) == 2 and all(item["text"] == "Partial draft" for item in limited_events)
    assert all(item["request_info"]["status"] == "interrupted" for item in limited_events)
    notices = [item["text"] for item in session["events"] if item["type"] == "notice"]
    assert len(notices) == 2 and all("(1 of 2)" in text for text in notices)

    session.update(title="Existing conversation", title_generated=True)
    manager.store.save(session)
    restarted = AgentManager(manager.store, manager.settings)
    saved = restarted.get(session["id"])
    await restarted.run(saved, "Check the completed work.")
    assert len(requests) == 5
    roles = [item["role"] for item in requests[-1]["messages"][1:]]
    assert roles == ["user", "assistant", "user", "assistant", "tool", "assistant", "user", "assistant", "user"]
    assert [item["text"] for item in saved["events"] if item["type"] == "user"] == ["Check the completed work."]
    assert OUTPUT_LIMIT_CONTINUATION in json.dumps(restarted.get(session["id"])["wire"])


async def test_output_limit_stops_after_two_automatic_retries(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values["max_agent_steps"] = 4
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        return response([call(arguments='{"path":')], "length", "Partial draft")

    manager.execute_tool = lambda *args: pytest.fail("An output-limited tool call must not execute")
    mock_gateway(monkeypatch, gateway)
    with pytest.raises(ValueError, match="after two automatic retries"):
        await manager.run_databricks(session, "Make the change.")
    assert len(requests) == 3
    limited_events = [item for item in session["events"]
                      if item.get("request_info", {}).get("error_kind") == "output_limit"]
    assert [item["request_info"]["status"] for item in limited_events] == [
        "interrupted", "interrupted", "error"]
    assert [item["role"] for item in session["wire"]] == ["user", "assistant", "user", "assistant", "user", "assistant"]
    assert all(item["content"] == OUTPUT_LIMIT_HISTORY_PLACEHOLDER
               for item in session["wire"] if item["role"] == "assistant")
    assert "Partial draft" not in json.dumps(session["wire"])
    notices = [item["text"] for item in session["events"] if item["type"] == "notice"]
    assert ["(1 of 2)" in notices[0], "(2 of 2)" in notices[1]] == [True, True]


async def test_output_limit_on_final_configured_step_does_not_overrun_budget(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values["max_agent_steps"] = 1
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        return response([call(arguments='{"path":')], "length", "Partial draft")

    manager.execute_tool = lambda *args: pytest.fail("An output-limited tool call must not execute")
    mock_gateway(monkeypatch, gateway)
    with pytest.raises(ValueError, match=r"final configured model request \(1\)"):
        await manager.run_databricks(session, "Make the change.")
    assert len(requests) == 1
    assert session["wire"][-1] == {"role": "assistant", "content": OUTPUT_LIMIT_HISTORY_PLACEHOLDER}
    assert session["events"][-1]["text"] == "Partial draft"
    assert not any(item["type"] == "notice" for item in session["events"])


async def test_unanswered_internal_continuation_is_removed_before_next_real_user_turn(runtime, monkeypatch):
    manager, session = runtime
    session.update(title="Existing conversation", title_generated=True)
    manager.store.save(session)
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return response([call(arguments='{"path":')], "length", "Visible partial draft")
        if len(requests) == 2:
            return httpx.Response(503, text="Unavailable")
        return response(finish="stop", text="Recovered on the next user turn.")

    manager.execute_tool = lambda *args: pytest.fail("No tool from the limited response may execute")
    mock_gateway(monkeypatch, gateway)
    with pytest.raises(ValueError, match="HTTP 503"):
        await manager.run_databricks(session, "Start the task.")
    assert session["wire"][-1] == {"role": "user", "content": OUTPUT_LIMIT_CONTINUATION}
    assert manager.store.get(session["id"])["wire"][-1]["content"] == OUTPUT_LIMIT_CONTINUATION

    restarted = AgentManager(manager.store, manager.settings)
    saved = restarted.get(session["id"])
    await restarted.run(saved, "Use this real follow-up instead.")
    assert len(requests) == 3
    sent = requests[-1]["messages"][1:]
    assert [item["role"] for item in sent] == ["user", "assistant", "user"]
    assert sent[-1]["content"] == "Use this real follow-up instead."
    assert OUTPUT_LIMIT_CONTINUATION not in json.dumps(restarted.get(session["id"])["wire"])
    assert [item["text"] for item in saved["events"] if item["type"] == "user"] == [
        "Use this real follow-up instead."]


@pytest.mark.parametrize("has_result", [False, True])
async def test_legacy_malformed_request_is_omitted_without_losing_archive_or_completed_actions(runtime, monkeypatch, has_result):
    manager, session = runtime
    completed = call("completed")
    malformed = call("broken", '{"path":"large-file.py"')
    session["wire"] = [
        {"role": "user", "content": "Write the first file."},
        {"role": "assistant", "content": None, "tool_calls": [completed]},
        {"role": "tool", "tool_call_id": "completed", "content": "First file was written."},
        {"role": "user", "content": "Write another file."},
        {"role": "assistant", "content": "Preparing another file.", "tool_calls": [malformed]},
    ]
    if has_result:
        session["wire"].append({"role": "tool", "tool_call_id": "broken", "content":
                                "Execution interrupted; the outcome is unknown. Check current state before retrying."})
    original = copy.deepcopy(session["wire"])
    requests = []

    async def gateway(request):
        payload = json.loads(request.content)
        requests.append(payload)
        for message in payload["messages"]:
            for tool_call in message.get("tool_calls", []):
                assert isinstance(json.loads(tool_call["function"]["arguments"]), dict)
        return response(finish="stop", text="The first file is complete; I will inspect the remaining work.")

    async def execute(*args):
        pytest.fail("Continuing old history must not rerun completed or broken tool calls")

    manager.execute_tool = execute
    mock_gateway(monkeypatch, gateway)
    assert await manager.run_databricks(session, "Is it done?") is True
    assert len(requests) == 1
    assert session["wire"][:len(original)] == original
    assert manager.store.get(session["id"])["wire"][:len(original)] == original
    sent = requests[0]["messages"]
    assert any(message.get("tool_calls") == [completed] for message in sent)
    assert any(message.get("content") == "First file was written." for message in sent)
    assert not any(item["id"] == "broken" for message in sent for item in message.get("tool_calls", []))
    assert not any(message.get("tool_call_id") == "broken" for message in sent)
    assert any("Historical tool request omitted" in str(message.get("content")) for message in sent)
    assert any("unknown" in str(message.get("content")) for message in sent)


async def test_legacy_mixed_batch_preserves_recorded_success_and_compaction_offsets(runtime, monkeypatch):
    manager, session = runtime
    session["wire"] = [
        {"role": "user", "content": "Earlier request."},
        {"role": "assistant", "content": "Earlier response."},
        {"role": "user", "content": "Make two changes."},
        {"role": "assistant", "content": None, "tool_calls": [call(), call("broken", '{"path":')]},
        {"role": "tool", "tool_call_id": "write", "content": "File written successfully."},
        {"role": "tool", "tool_call_id": "broken", "content": "Invalid tool arguments: unexpected EOF"},
    ]
    session["context_state"] = {"summary": "Earlier request completed.", "through": 2, "compactions": 1}
    original = copy.deepcopy(session["wire"])

    async def gateway(request):
        messages = json.loads(request.content)["messages"]
        assert any(message.get("content") == "Make two changes." for message in messages)
        assert any("File written successfully." in str(message.get("content")) for message in messages)
        assert not any(message.get("tool_calls") or message["role"] == "tool" for message in messages)
        return response(finish="stop", text="One file was written; the other call was invalid.")

    mock_gateway(monkeypatch, gateway)
    assert await manager.run_databricks(session, "Continue.") is True
    assert session["wire"][:len(original)] == original
    assert session["context_state"]["through"] == 2
    assert session["context_state"]["compactions"] == 1


async def test_legacy_recovery_keeps_later_valid_exchange_with_reused_id(runtime, monkeypatch):
    manager, session = runtime
    session["wire"] = [
        {"role": "user", "content": "Try the change."},
        {"role": "assistant", "content": None, "tool_calls": [call(arguments='{"path":')]},
        {"role": "tool", "tool_call_id": "write", "content": "Invalid arguments; not executed."},
        {"role": "user", "content": "Try again."},
        {"role": "assistant", "content": None, "tool_calls": [call()]},
        {"role": "tool", "tool_call_id": "write", "content": "File written successfully."},
    ]

    async def gateway(request):
        messages = json.loads(request.content)["messages"]
        results = [message for message in messages if message["role"] == "tool"]
        assert results == [{"role": "tool", "tool_call_id": "write", "content": "File written successfully."}]
        assert messages[messages.index(results[0]) - 1]["tool_calls"] == [call()]
        return response(finish="stop", text="The retry succeeded.")

    mock_gateway(monkeypatch, gateway)
    assert await manager.run_databricks(session, "Is it done?") is True
