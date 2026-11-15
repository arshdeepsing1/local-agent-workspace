"""A lone UTF-16 surrogate from the model ("\\ud83d" in its JSON) must never make a chat unsavable.

json.loads accepts an escaped lone surrogate, but strict UTF-8 cannot hold it. The text is cleaned where it is
parsed (a pair split across two stream chunks is joined), the store writes U+FFFD for any that is left, and a
turn always settles."""
import json

import httpx
import pytest

from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.model_stream import well_formed
from local_agent.store import Store

HIGH, LOW = "\ud83d", "\ude00"
EMOJI = "\U0001F600"


def prepared(session_id="example"):
    return {"id": session_id, "title": "Example", "created": 10, "updated": 20,
            "workspace": "/project", "model": "model", "events": [], "wire": []}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    store = Store(tmp_path / "tests.sqlite3")
    session = store.create(settings.values)
    session.update(title="Surrogates", title_generated=True, model="test-endpoint", permission_mode="bypassPermissions")
    manager = AgentManager(store, settings)
    sent = []

    async def record(session_id, message):
        sent.append(message)
    manager.broadcast = record
    yield manager, session, sent
    store.db.close()


def mock_gateway(monkeypatch, gateway):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(gateway)))


def sse(*deltas, finish="stop"):
    chunks = [{"choices": [{"delta": delta, "finish_reason": None}]} for delta in deltas]
    chunks[-1]["choices"][0]["finish_reason"] = finish
    return "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def replying(*deltas):
    async def gateway(request):
        return httpx.Response(200, text=sse(*deltas))
    return gateway


def reply_of(session):
    return next(event for event in reversed(session["events"]) if event["type"] == "assistant")


def test_a_lone_surrogate_in_a_reply_does_not_make_the_chat_unsavable(tmp_path):
    """A reply holding a lone UTF-16 surrogate (a model's JSON "\\ud83d" escape) used to make every save raise
    UnicodeEncodeError, which the draft checkpoint does not treat as a storage error."""
    store = Store(tmp_path / "state.db")
    session = prepared()
    store.save(session)
    session["events"].append({"id": "e1", "type": "assistant", "text": "emoji cut in half: \ud83d"})
    store.save(session)  # Must not raise.
    assert [event["id"] for event in store.get("example")["events"]] == ["e1"]


def test_the_store_writes_a_lone_surrogate_as_the_replacement_character(tmp_path):
    store = Store(tmp_path / "state.db")
    session = prepared()
    session["events"].append({"id": "e1", "type": "assistant", "text": "a" + HIGH + "b" + LOW + "c"})
    session["wire"].append({"role": "assistant", "content": "x" * 3000 + HIGH})  # Long strings take the cached path.
    store.save(session)
    raw = (store.conversations / "example.jsonl").read_bytes().decode("utf-8")  # Valid UTF-8.
    assert all(json.loads(line) for line in raw.splitlines())
    loaded = store.get("example")
    assert loaded["events"][0]["text"] == "a�b�c"
    assert loaded["wire"][0]["content"] == "x" * 3000 + "�"


def test_well_formed_joins_a_pair_and_replaces_the_rest():
    assert well_formed(HIGH + LOW) == EMOJI
    assert well_formed("a" + HIGH) == "a�"
    assert well_formed(LOW + "a") == "�a"
    assert well_formed("plain text é") == "plain text é"


async def test_a_pair_split_across_two_chunks_becomes_the_real_character(runtime, monkeypatch):
    manager, session, sent = runtime
    mock_gateway(monkeypatch, replying({"content": "smile " + HIGH}, {"content": LOW + " done"}))
    await manager.run(session, "Hello")
    assert reply_of(session)["text"] == f"smile {EMOJI} done"
    streamed = "".join(message["text"] for message in sent if message.get("type") == "delta")
    assert streamed == f"smile {EMOJI} done"
    assert session["wire"][-1]["content"] == f"smile {EMOJI} done"
    assert manager.statuses[session["id"]] != "running"


async def test_a_lone_surrogate_in_a_streamed_reply_settles_the_turn(runtime, monkeypatch):
    manager, session, sent = runtime
    turns = []

    async def gateway(request):
        turns.append(request)
        text = sse({"content": "cut " + HIGH}, {"content": " off"}) if len(turns) == 1 else sse({"content": "Second."})
        return httpx.Response(200, text=text)
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert reply_of(session)["text"] == "cut � off"
    assert manager.statuses[session["id"]] == "idle" and session["id"] not in manager.live
    saved = manager.store.get(session["id"])
    assert [event["text"] for event in saved["events"] if event["type"] == "assistant"] == ["cut � off"]
    second = manager.store.get(session["id"])
    await manager.run(second, "Again")
    assert [event["text"] for event in second["events"] if event["type"] == "assistant"][-1] == "Second."
    assert manager.statuses[session["id"]] == "idle"


async def test_a_trailing_lone_surrogate_ends_up_as_the_replacement_character(runtime, monkeypatch):
    manager, session, sent = runtime
    mock_gateway(monkeypatch, replying({"content": "end " + HIGH}))
    await manager.run(session, "Hello")
    assert reply_of(session)["text"] == "end �"
    assert session["wire"][-1]["content"] == "end �"


async def test_a_lone_surrogate_in_tool_arguments_settles_the_turn(runtime, monkeypatch):
    manager, session, sent = runtime
    calls = []

    async def gateway(request):
        calls.append(request)
        if len(calls) == 1:
            arguments = json.dumps({"path": "notes-" + HIGH + ".txt"})
            return httpx.Response(200, text=sse({"tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                                                                 "function": {"name": "read_file", "arguments": arguments}}]},
                                                finish="tool_calls"))
        return httpx.Response(200, text=sse({"content": "Done."}))
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    tool = next(event for event in session["events"] if event["type"] == "tool")
    assert tool["input"]["path"] == "notes-�.txt"
    assert manager.statuses[session["id"]] == "idle" and len(calls) == 2
    saved = manager.store.get(session["id"])
    assert next(event for event in saved["events"] if event["type"] == "tool")["input"]["path"] == "notes-�.txt"


async def test_an_unexpected_error_from_a_save_still_settles_the_turn(runtime, monkeypatch):
    manager, session, sent = runtime
    mock_gateway(monkeypatch, replying({"content": "Fine."}))
    original, failed = manager.store.save, []

    def flaky(target, *arguments, **options):
        if not failed and target.get("turn_ended") is not None:
            failed.append(True)
            raise ValueError("unexpected")
        return original(target, *arguments, **options)
    monkeypatch.setattr(manager.store, "save", flaky)
    await manager.run(session, "Hello")
    assert failed and manager.statuses[session["id"]] != "running" and session["id"] not in manager.live


def test_a_very_deeply_nested_value_is_cleaned_without_recursion():
    from local_agent.model_stream import well_formed_json
    value = {"key" + HIGH: HIGH}
    for _ in range(2000):
        value = {"inner": [value, 1]}
    cleaned = well_formed_json(value)
    for _ in range(2000):
        assert list(cleaned) == ["inner"] and cleaned["inner"][1] == 1
        cleaned = cleaned["inner"][0]
    assert cleaned == {"key�": "�"}


def test_well_formed_json_keeps_key_and_list_order():
    from local_agent.model_stream import well_formed_json
    assert list(well_formed_json({"b": 1, "a" + HIGH + LOW: [3, "x" + HIGH, 2], "c": None}).items()) == [
        ("b", 1), ("a" + EMOJI, [3, "x�", 2]), ("c", None)]


def summary_delta(text, signature=None):
    item = {"type": "summary_text", "text": text, **({"signature": signature} if signature else {})}
    return {"content": [{"type": "reasoning", "summary": [item]}]}


def test_the_collector_joins_a_split_pair_and_never_raises_on_a_lone_surrogate():
    from local_agent.reasoning_replay import Collector
    split = Collector()
    split.add(summary_delta("plan " + HIGH))
    split.add(summary_delta(LOW + " more", "sig"))
    assert split.result()[0]["summary"][0]["text"] == f"plan {EMOJI} more"
    lone = Collector()
    lone.add(summary_delta("plan " + HIGH, "sig"))
    assert lone.result()[0]["summary"][0]["text"] == "plan �"
    signed = Collector()
    signed.add(summary_delta("exact é text", "sig"))
    assert signed.result()[0]["summary"][0] == {"type": "summary_text", "text": "exact é text", "signature": "sig"}


async def test_list_shaped_reasoning_with_a_split_pair_keeps_the_emoji_and_the_reply(runtime, monkeypatch):
    manager, session, sent = runtime
    mock_gateway(monkeypatch, replying(summary_delta("plan " + HIGH), summary_delta(LOW + " more"),
                                       {"content": "The answer."}))
    await manager.run(session, "Hello")
    reply = reply_of(session)
    assert reply["reasoning_summary"] == f"plan {EMOJI} more" and reply["text"] == "The answer."
    assert not [event for event in session["events"] if event["type"] == "error"]


async def test_list_shaped_reasoning_with_a_lone_surrogate_keeps_the_reply(runtime, monkeypatch):
    manager, session, sent = runtime
    mock_gateway(monkeypatch, replying(summary_delta("plan " + HIGH), {"content": "The answer."}))
    await manager.run(session, "Hello")
    reply = reply_of(session)
    assert reply["reasoning_summary"] == "plan �" and reply["text"] == "The answer."
    assert not [event for event in session["events"] if event["type"] == "error"]


async def test_a_compaction_summary_with_a_lone_surrogate_completes(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session["wire"] = [{"role": "user", "content": "Earlier decision."}, {"role": "assistant", "content": "Done."},
                       {"role": "user", "content": "What is next?"}, {"role": "assistant", "content": "Run tests."}]
    store.save(session)

    async def gateway(request):
        body = {"choices": [{"message": {"content": "Migration done " + HIGH + " and compatible."},
                             "finish_reason": "stop"}], "usage": {}}
        # The model's JSON escapes the lone half ("\\ud83d"); httpx's json= would refuse to encode it.
        return httpx.Response(200, content=json.dumps(body).encode(), headers={"content-type": "application/json"})
    mock_gateway(monkeypatch, gateway)
    manager.start_compaction(session["id"], "")
    await manager.tasks[session["id"]]
    saved = store.get(session["id"])
    handoffs = list((project / "handoffs" / "auto").glob("*-compaction-*.md"))
    assert len(handoffs) == 1 and "Migration done � and compatible." in handoffs[0].read_text(encoding="utf-8")
    assert not [event for event in saved["events"] if event["type"] == "error"]
    store.db.close()


class Stalled(httpx.AsyncByteStream):
    """A streamed body that sends its first frame, then waits for release before the rest."""

    def __init__(self, first, rest, release):
        self.first, self.rest, self.release = first, rest, release

    async def __aiter__(self):
        yield self.first.encode()
        await self.release.wait()
        yield self.rest.encode()


async def test_a_stream_stalled_after_a_trailing_high_surrogate_keeps_the_session_encodable(runtime, monkeypatch):
    import asyncio
    from local_agent.agents import public_session
    manager, session, sent = runtime
    release = asyncio.Event()
    frame = lambda delta, finish=None: "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"

    async def gateway(request):
        return httpx.Response(200, stream=Stalled(frame({"content": "stall " + HIGH}),
                                                  frame({"content": LOW + " end"}, "stop") + "data: [DONE]\n\n", release))
    mock_gateway(monkeypatch, gateway)
    task = asyncio.create_task(manager.run(session, "Hello"))
    for _ in range(200):
        if any(event["type"] == "assistant" and event["text"].startswith("stall") for event in session["events"]):
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the stream never started")
    for event in session["events"]:
        json.dumps(event, ensure_ascii=False).encode("utf-8")
    json.dumps(public_session(session), ensure_ascii=False).encode("utf-8")
    json.dumps(sent, ensure_ascii=False).encode("utf-8")
    assert reply_of(session)["text"] == "stall "
    release.set()
    await task
    assert reply_of(session)["text"] == f"stall {EMOJI} end"


@pytest.mark.parametrize("chunks, expected", [
    (["smile " + HIGH, LOW + " done"], f"smile {EMOJI} done"),
    (["end " + HIGH], "end �"),
    (["start ", LOW + "tail"], "start �tail"),
    (["a" + HIGH, "b"], "a�b"),
])
async def test_the_page_receives_exactly_the_saved_text(runtime, monkeypatch, chunks, expected):
    manager, session, sent = runtime
    mock_gateway(monkeypatch, replying(*[{"content": chunk} for chunk in chunks]))
    await manager.run(session, "Hello")
    assert reply_of(session)["text"] == expected
    assert "".join(message["text"] for message in sent if message.get("type") == "delta") == expected
    json.dumps(sent, ensure_ascii=False).encode("utf-8")


async def test_a_pair_split_around_a_tool_call_delta_still_joins(runtime, monkeypatch):
    manager, session, sent = runtime
    call = {"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]}
    answers = iter([sse({"content": "x" + HIGH}, call, {"content": LOW}, finish="tool_calls"), sse({"content": "Done."})])

    async def gateway(request):
        return httpx.Response(200, text=next(answers))
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert "".join(message["text"] for message in sent if message.get("type") == "delta") == f"x{EMOJI}Done."


async def test_an_error_chunk_with_a_lone_surrogate_gives_an_encodable_error_event(runtime, monkeypatch):
    manager, session, sent = runtime

    async def gateway(request):
        return httpx.Response(200, text="data: " + json.dumps({"error": "boom " + HIGH}) + "\n\ndata: [DONE]\n\n")
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    error = next(event for event in session["events"] if event["type"] == "error")
    json.dumps(error, ensure_ascii=False).encode("utf-8")
    assert "boom �" in error["text"]
    json.dumps(sent, ensure_ascii=False).encode("utf-8")


async def test_a_value_error_from_a_save_is_logged_and_does_not_blame_the_disk(runtime, monkeypatch, caplog):
    import logging
    manager, session, sent = runtime
    mock_gateway(monkeypatch, replying({"content": "Fine."}))
    original, failed = manager.store.save, []

    def flaky(target, *arguments, **options):
        if not failed and target.get("turn_ended") is not None:
            failed.append(True)
            raise ValueError("unexpected shape")
        return original(target, *arguments, **options)
    monkeypatch.setattr(manager.store, "save", flaky)
    with caplog.at_level(logging.ERROR):
        await manager.run(session, "Hello")
    assert any("unexpected shape" in (record.exc_text or "") or "unexpected shape" in record.getMessage()
               or (record.exc_info and "unexpected shape" in str(record.exc_info[1])) for record in caplog.records)
    errors = [event["text"] for event in session["events"] if event["type"] == "error"]
    assert errors and not any("disk space" in text for text in errors)


def tool_reply(arguments):
    return sse({"tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                                "function": {"name": "read_file", "arguments": arguments}}]}, finish="tool_calls")


def raw_text(prefix, body, suffix="b"):
    return '{"path": "a' + prefix + body + suffix + '"}'


ESC_HIGH, ESC_LOW = "\\ud83d", "\\ude00"


@pytest.mark.parametrize("arguments, expected", [
    (raw_text(HIGH, ESC_LOW), "a" + EMOJI + "b"),                 # a real high half, then an escaped low half
    (raw_text(ESC_HIGH, LOW), "a" + EMOJI + "b"),                 # an escaped high half, then a real low half
    (raw_text(ESC_HIGH, ESC_LOW), "a" + EMOJI + "b"),             # both escaped
    (raw_text(HIGH, LOW), "a" + EMOJI + "b"),                     # both real
    (raw_text(HIGH, ""), "a�b"),                             # a lone real high half
    (raw_text(ESC_HIGH, ""), "a�b"),                         # a lone escaped high half
    (raw_text(LOW, ""), "a�b"),                              # a lone real low half
    (raw_text(ESC_LOW, ""), "a�b"),                          # a lone escaped low half
    (raw_text(HIGH, "\\u0041"), "a�Ab"),                     # a real high half before another escape
])
async def test_tool_arguments_with_real_and_escaped_halves_agree_everywhere(runtime, monkeypatch, arguments, expected):
    manager, session, sent = runtime
    calls = []

    async def gateway(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, text=tool_reply(arguments))
        return httpx.Response(200, text=sse({"content": "Done."}))
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    tool = next(event for event in session["events"] if event["type"] == "tool")
    assert tool["input"]["path"] == expected
    sent_back = next(message for message in session["wire"] if message.get("tool_calls"))["tool_calls"][0]
    assert json.loads(sent_back["function"]["arguments"])["path"] == expected  # The wire agrees with the tool.
    for text in (json.dumps(session["events"], ensure_ascii=False), json.dumps(session["wire"], ensure_ascii=False),
                 sent_back["function"]["arguments"], calls[1].content.decode("utf-8")):
        text.encode("utf-8")
    saved = manager.store.get(session["id"])
    assert next(event for event in saved["events"] if event["type"] == "tool")["input"]["path"] == expected
    assert len(calls) == 2 and manager.statuses[session["id"]] == "idle"


@pytest.mark.parametrize("arguments", [
    '{"path": "a' + HIGH + 'b"',                  # unterminated, a real lone half
    '{"path": "a' + ESC_HIGH + 'b"',              # unterminated, an escaped lone half
    '{"path": "a' + HIGH + ESC_LOW + 'b" nope}',  # not JSON, a real half before an escaped one
])
async def test_invalid_tool_arguments_with_a_lone_half_end_in_the_tool_error_with_well_formed_text(
        runtime, monkeypatch, arguments):
    manager, session, sent = runtime
    calls = []

    async def gateway(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, text=tool_reply(arguments))
        return httpx.Response(200, text=sse({"content": "Done."}))
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    assert not [event for event in session["events"] if event["type"] == "tool"]
    assert [call.get("retry_reason") for call in session["inference_calls"]] == [None, "invalid_tool_arguments"]
    failed = next(event for event in session["events"] if event["type"] == "assistant")
    assert failed["request_info"]["error_kind"] == "invalid_tool_arguments"
    placeholder = next(message for message in session["wire"] if message.get("tool_calls") is None
                       and "tool arguments" in str(message.get("content")))
    assert placeholder
    json.dumps(session["events"], ensure_ascii=False).encode("utf-8")
    json.dumps(session["wire"], ensure_ascii=False).encode("utf-8")
    for message in session["wire"]:
        for call in message.get("tool_calls") or []:
            call["function"]["arguments"].encode("utf-8")
    manager.store.get(session["id"])["events"]
    assert manager.statuses[session["id"]] == "idle"
