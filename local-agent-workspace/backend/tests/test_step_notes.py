"""Step notices out of the system message's tail (M5h): at the full prompt-cache level the changed-files and
background-jobs notices end the request's last message, after its cache marker, so a notice that appears or goes
never turns the next step into a cache miss. Checked with the offline simulator's prefix cache."""
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from local_agent.agents import STEP_NOTE, AgentManager, step_note
from local_agent.config import Settings
from local_agent.prompt_cache import CACHE_CONTROL, marked, markers, unmarked
from local_agent.store import Store

OPUS = "databricks-claude-opus-5-5"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "simulate_databricks.py"
REAL_CLIENT = httpx.AsyncClient
MARK = {"cache_control": CACHE_CONTROL}
CHANGED = ("Files changed since you last read or wrote them (by the user, an undo, or a command): note.txt. Read a "
           "file again before editing it.")
NOTE = STEP_NOTE.format(notice=CHANGED)


def request(*messages):
    return {"messages": [{"role": "system", "content": "Stable rules.\nAdditional allowed folders: []"}, *messages],
            "tools": [{"type": "function", "function": {"name": "read_file"}}], "stream": True, "max_tokens": 1024}


CALLS = {"role": "assistant", "content": None, "tool_calls": [
    {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]}


def test_the_note_stays_after_the_marker_at_every_level():
    user = request({"role": "user", "content": "Hello" + NOTE})
    sent = marked(user, 6, "full", len(NOTE))
    assert sent["messages"][-1]["content"] == [{"type": "text", "text": "Hello", **MARK}, {"type": "text", "text": NOTE}]
    assert markers(sent) == 2 and unmarked(sent) == user
    tool = request({"role": "user", "content": "Read it"}, CALLS, {"role": "tool", "tool_call_id": "c1", "content": "one" + NOTE})
    sent = marked(tool, 6, "full", len(NOTE))
    assert sent["messages"][-1]["content"] == [{"type": "text", "text": "one", **MARK}, {"type": "text", "text": NOTE}]
    assert unmarked(sent) == tool
    # A request resent at the documented forms: a user message is split the same way; a tool result stays one
    # string, after the marked tool call.
    sent = marked(user, 6, "messages", len(NOTE))
    assert sent["messages"][-1]["content"] == [{"type": "text", "text": "Hello", **MARK}, {"type": "text", "text": NOTE}]
    sent = marked(tool, 6, "messages", len(NOTE))
    assert sent["messages"][-1]["content"] == "one" + NOTE and sent["messages"][2]["tool_calls"][0]["cache_control"] == CACHE_CONTROL
    assert markers(sent) == 1 and unmarked(sent) == tool
    # Without a note, or with one as long as the message, the whole message is one marked part, as before.
    whole = request({"role": "user", "content": NOTE})
    assert marked(whole, 6, "full", len(NOTE))["messages"][-1]["content"] == [{"type": "text", "text": NOTE, **MARK}]
    assert marked(user, 6, "full")["messages"][-1]["content"] == [{"type": "text", "text": "Hello" + NOTE, **MARK}]


def test_only_a_user_message_or_tool_result_with_text_takes_the_note():
    assert step_note([{"role": "user", "content": "Hi"}], CHANGED) == NOTE
    assert step_note([{"role": "tool", "tool_call_id": "c", "content": "x"}], CHANGED) == NOTE
    assert NOTE.startswith("\n\n[Note from the app, not the user's words or tool output: ")
    for history in ([], [{"role": "assistant", "content": "Done."}], [{"role": "user", "content": ""}],
                    [{"role": "user", "content": [{"type": "text", "text": "x"}]}]):
        assert step_note(history, CHANGED) == ""
    assert step_note([{"role": "user", "content": "Hi"}], "") == ""


@pytest.fixture(scope="module")
def simulator():
    spec = importlib.util.spec_from_file_location("simulate_databricks", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path / "app")
    project = tmp_path / "project"
    project.mkdir()
    (project / "note.txt").write_text("first\n")
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    session = store.create(settings.values)
    session.update(title="Notes", title_generated=True, model=OPUS, permission_mode="bypassPermissions")
    store.save(session)
    yield AgentManager(store, settings), session, project
    store.db.close()


def sse(delta, finish="stop"):
    chunk = {"choices": [{"delta": delta, "finish_reason": finish}]}
    return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")


def reply(text="Done."):
    return sse({"content": text})


def call_reply(name, arguments, call_id):
    return sse({"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                "function": {"name": name, "arguments": json.dumps(arguments)}}]}, "tool_calls")


def mock_gateway(monkeypatch, respond):
    bodies = []

    async def handle(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "A title"}, "finish_reason": "stop"}]})
        bodies.append(body)
        return respond(body)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(handle)))
    return bodies


def note_of(body):
    """The step note a request carries: the unmarked part after the last message's marker, or None."""
    content = body["messages"][-1]["content"]
    return content[1]["text"] if isinstance(content, list) and len(content) == 2 else None


def in_system_tail(body):
    """The same request with its note at the end of the system message, where it went before."""
    note = note_of(body)
    moved = json.loads(json.dumps(body))
    if note:
        moved["messages"][-1]["content"] = moved["messages"][-1]["content"][:1]
        moved["messages"][0]["content"][1]["text"] += "\n" + note[len(STEP_NOTE.split("{")[0]):-1]
    return moved


def cache_reads(simulator, bodies):
    """(read, written) per request through the simulator's 5-minute prefix cache, one request per second."""
    cache = simulator.PromptCache()
    return [cache.use(OPUS, body, now=1000 + index)[:2] for index, body in enumerate(bodies)]


async def test_a_notice_that_appears_or_goes_keeps_the_cached_conversation(runtime, monkeypatch, simulator):
    manager, session, project = runtime
    script = [lambda: call_reply("read_file", {"path": "note.txt"}, "r1"), reply,
              lambda: call_reply("list_tasks", {}, "c2"), reply,
              lambda: call_reply("read_file", {"path": "note.txt"}, "r2"), reply]
    bodies = mock_gateway(monkeypatch, lambda body: script.pop(0)())
    await manager.run(session, "Read the note")
    (project / "note.txt").write_text("changed by the user\n")
    await manager.run(session, "Continue")  # The notice appears, and stays until the file is read again.
    await manager.run(session, "Read it again")  # Read again: the next request has no notice.
    assert len(bodies) == 6
    # The model sees the notice, in the requests from the change until the read, and only there.
    assert [note_of(body) for body in bodies] == [None, None, NOTE, NOTE, NOTE, None]
    assert all(CHANGED not in json.dumps(body["messages"][0]) for body in bodies)
    # Every request reads back everything the previous one wrote: no step is a cache miss.
    reads = cache_reads(simulator, bodies)
    for (read, written), (next_read, _) in zip(reads, reads[1:]):
        assert next_read == read + written > 0
    # In the system tail (as before), the request where the notice appears and the one after the read would only
    # read the system message's stable part.
    before = cache_reads(simulator, [in_system_tail(body) for body in bodies])
    assert 0 < before[2][0] < reads[1][0] + reads[1][1] and 0 < before[5][0] < reads[4][0] + reads[4][1]
    assert before[3][0] == before[2][0] + before[2][1]  # While the notice stays, the tail does not change.
    # The note is never stored: not in the history, the events or an export.
    stored = manager.store.get(session["id"])
    assert "Note from the app" not in json.dumps(stored["wire"]) + json.dumps(stored["events"])
    calls = [call for call in session["inference_calls"] if call["purpose"] == "agent"]
    assert [call.get("system_tail_changed") for call in calls] == [None] * 6


@pytest.mark.parametrize("model,setting", [(OPUS, "messages"), (OPUS, "off"), ("test-endpoint", "auto")])
async def test_without_the_full_level_the_notice_ends_the_system_message(runtime, monkeypatch, model, setting):
    manager, session, project = runtime
    session["model"] = model
    manager.settings.values["prompt_caching"] = setting
    script = [lambda: call_reply("read_file", {"path": "note.txt"}, "r1"), reply, reply]
    bodies = mock_gateway(monkeypatch, lambda body: script.pop(0)())
    await manager.run(session, "Read the note")
    (project / "note.txt").write_text("changed by the user\n")
    await manager.run(session, "Continue")
    assert unmarked(bodies[-1])["messages"][0]["content"].endswith("\n" + CHANGED)
    assert "Note from the app" not in json.dumps(bodies)


async def test_a_resend_with_fewer_markers_keeps_the_note_after_them(runtime, monkeypatch):
    manager, session, project = runtime
    script = [lambda: call_reply("read_file", {"path": "note.txt"}, "r1"), reply]
    bodies = mock_gateway(monkeypatch, lambda body: script.pop(0)())
    await manager.run(session, "Read the note")
    (project / "note.txt").write_text("changed by the user\n")
    # A route that takes only the documented forms: list content for role system or tool is refused.
    answers = [httpx.Response(400, json={"message": "messages[0].content: Input should be a valid string"}),
               call_reply("list_tasks", {}, "c2"), reply()]
    bodies = mock_gateway(monkeypatch, lambda body: answers.pop(0))
    await manager.run(session, "Continue")
    first, resent, after = bodies
    assert note_of(first) == NOTE and markers(first) == 2
    # The resend at the documented forms keeps the note after the marker: [marked text, note].
    assert resent["messages"][-1]["content"][1] == {"type": "text", "text": NOTE} and markers(resent) == 1
    assert unmarked(first) == unmarked(resent)
    # The next step starts at the remembered level, which keeps the notice in the system message.
    assert manager.cache_levels == {OPUS: "messages"} and markers(after) == 1
    assert after["messages"][0]["content"].endswith("\n" + CHANGED) and "Note from the app" not in json.dumps(after)


async def test_a_background_job_notice_moves_too(runtime, monkeypatch):
    manager, session, _ = runtime
    texts = iter(["", "Background jobs finished since your last step: 1234abcd `make` completed (exit 0). Use "
                      "get_job_output to read their output.", ""])
    monkeypatch.setattr(manager, "step_notices", lambda session, tools: next(texts))
    script = [lambda: call_reply("list_tasks", {}, "c1"), lambda: call_reply("list_tasks", {}, "c2"), reply]
    bodies = mock_gateway(monkeypatch, lambda body: script.pop(0)())
    await manager.run(session, "Wait for the build")
    assert [bool(note_of(body)) for body in bodies] == [False, True, False]
    assert "Background jobs finished since your last step" in note_of(bodies[1])
    assert all("Background jobs finished" not in json.dumps(body["messages"][0]) for body in bodies)


def test_the_simulated_model_reads_the_users_words_without_the_note(simulator):
    jobs = STEP_NOTE.format(notice="Background jobs finished since your last step: 1234abcd `make` completed (exit 0). "
                                   "Use get_job_output to read their output.")
    body = {"messages": [{"role": "system", "content": "Rules."}, {"role": "user", "content": [
        {"type": "text", "text": "hello there", **MARK}, {"type": "text", "text": jobs}]}],
            "tools": [{"type": "function", "function": {"name": name, "parameters": {}}}
                      for name in ("run_command", "wait_job", "read_file", "project_overview")]}
    assert simulator.Brain().respond(body) == (None, [])  # Neither the job nor the read scenario.
    body["messages"][1]["content"][0]["text"] = "read the project"
    assert simulator.Brain().respond(body)[1][0]["name"] == "project_overview"


async def test_compaction_sees_the_history_without_the_note(runtime, monkeypatch):
    # An automatic continuation must stay recognisable as the app's (is_continuation): the note joins the
    # request only after the context was prepared.
    from local_agent import agents
    from local_agent.context import OUTPUT_LIMIT_CONTINUATION, is_continuation
    manager, session, _ = runtime
    seen, prepare = [], agents.prepare_context

    async def watched(wire, *args, **options):
        seen.append(wire[-1])
        return await prepare(wire, *args, **options)
    monkeypatch.setattr(agents, "prepare_context", watched)
    texts = iter(["", CHANGED])
    monkeypatch.setattr(manager, "step_notices", lambda session, tools: next(texts))
    answers = [sse({"content": "Partial"}, "length"), reply()]
    bodies = mock_gateway(monkeypatch, lambda body: answers.pop(0))
    await manager.run(session, "Write a long answer")
    assert seen[1] == {"role": "user", "content": OUTPUT_LIMIT_CONTINUATION} and is_continuation(seen[1])
    assert bodies[1]["messages"][-1]["content"] == [{"type": "text", "text": OUTPUT_LIMIT_CONTINUATION, **MARK},
                                                    {"type": "text", "text": NOTE}]
