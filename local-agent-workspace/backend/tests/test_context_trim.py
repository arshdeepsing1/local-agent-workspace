"""Live context trim (Story 4, decision 4c): when a request passes the start mark, one pass replaces old tool results
in what the model receives with short notices, and records the trim on the event (context_trim). The event keeps its
full result; the summarizer, export and rewind see the chat as before."""
import copy
import hashlib
import json

import httpx
import pytest

from local_agent import context
from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.context import CLEAR_KEEP_TURNS, CLEAR_MIN_BYTES, estimate_tokens, prepare_context, summary_transcript
from local_agent.store import Store

SYSTEM = {"role": "system", "content": "Rules."}
TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "Read a file.", "parameters": {}}}]
WINDOW = 65_536


def digest(value):
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# --- Hand-built histories ------------------------------------------------------------------------------------------

def call(number, name, **arguments):
    return {"id": f"call_{number}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def page(path, lines, start=1):
    """A read_file result of `lines` numbered lines, as the tool returns it."""
    body = "\n".join(f"{number}: line {number} of {path} " + "x" * 40 for number in range(start, start + lines))
    return json.dumps({"path": path, "content": body, "start_line": start, "total_lines": start + lines - 1}, indent=2)


def command_output(label, kilobytes):
    return json.dumps({"job_id": f"job-{label}", "state": "completed", "exit_code": 0,
                       "output": "\n".join(f"{label} output line {number} " + "y" * 60
                                           for number in range(kilobytes * 14))}, indent=2)


class History:
    """A wire and its events, built turn by turn."""

    def __init__(self):
        self.wire, self.events, self.count = [], [], 0

    def user(self, text):
        self.count += 1
        self.wire.append({"role": "user", "content": text})
        self.events.append({"id": f"e_user_{self.count}", "type": "user", "text": text, "in_wire": True})

    def reply(self, text):
        self.count += 1
        self.wire.append({"role": "assistant", "content": text})
        self.events.append({"id": f"e_reply_{self.count}", "type": "assistant", "text": text})

    def tool(self, name, arguments, output, state="completed", call_id=None, **fields):
        """One assistant response with one tool call and its result; the tool event's id."""
        self.count += 1
        item = call(self.count, name, **arguments)
        if call_id:
            item["id"] = call_id  # A provider (or a test) may reuse an id across turns.
        self.wire.append({"role": "assistant", "content": "", "tool_calls": [item]})
        self.wire.append({"role": "tool", "tool_call_id": item["id"], "content": output})
        event = {"id": f"e_tool_{self.count}", "type": "tool", "name": name, "input": arguments, "output": output,
                 "call_id": item["id"], "state": state, **fields}
        self.events.append(event)
        return event["id"]

    def completed(self):
        return {event["call_id"] for event in self.events if event["type"] == "tool" and event["state"] == "completed"}

    def event(self, event_id):
        return next(event for event in self.events if event["id"] == event_id)

    def session(self, **fields):
        return {"id": "s1", "events": self.events, "wire": self.wire, **fields}


def long_history(turns=8, first=None):
    """Each turn reads one shared page, its own module, runs a command and reads a small file. first(history), when
    given, adds calls to turn 1."""
    history = History()
    for turn in range(1, turns + 1):
        history.user(f"Turn {turn}: please look at module {turn}.")
        if turn == 1 and first is not None:
            first(history)
        history.tool("read_file", {"path": "src/shared.py"}, page("src/shared.py", 60))
        history.tool("read_file", {"path": f"src/module_{turn}.py"}, page(f"src/module_{turn}.py", 50))
        history.tool("run_command", {"command": f"pytest tests/test_{turn}.py -q"}, command_output(f"t{turn}", 6))
        history.tool("read_file", {"path": f"src/small_{turn}.py"}, page(f"src/small_{turn}.py", 3))
        history.reply(f"Turn {turn} done.")
    return history


async def no_summary(*args):
    raise AssertionError("no compaction expected")


# --- Snapshots of today's behaviour (taken before the trim code existed) -----------------------------------------------

SUMMARIZER_SNAPSHOT = ('886d31f65bd7fdc3', {'elided_bytes': 36858, 'superseded': 7, 'shortened': 3, 'cleared': 10,
                        'cleared_bytes': 50830})
SUMMARIZER_HANDOFF_SNAPSHOT = ('aa9b6e63103e1599', {'elided_bytes': 34067, 'superseded': 7, 'shortened': 2, 'cleared': 12,
                                'cleared_bytes': 60480})
SUMMARIZER_PLAIN_SNAPSHOT = ('8d91604c0c15df58', {'elided_bytes': 78914, 'superseded': 0, 'shortened': 22})
REQUEST_SNAPSHOT = "8fe2f94390b9e833"


def test_the_summarizers_transcript_is_byte_identical_to_before_the_trim_code():
    history = long_history()
    done = history.completed()
    cut = len(history.wire) - 7
    text, stats = summary_transcript(history.wire[:cut], history.wire[cut:], 4_000, done, keep_turns=CLEAR_KEEP_TURNS)
    assert (digest(text), stats) == SUMMARIZER_SNAPSHOT
    text, stats = summary_transcript(history.wire[:cut], history.wire[cut:], 4_000, done, keep_turns=2, handoff=True)
    assert (digest(text), stats) == SUMMARIZER_HANDOFF_SNAPSHOT
    text, stats = summary_transcript(history.wire[:cut], history.wire[cut:], 1_500)
    assert (digest(text), stats) == SUMMARIZER_PLAIN_SNAPSHOT


async def test_the_request_is_byte_identical_to_before_when_nothing_is_trimmed():
    history = long_history(4)
    messages, state, info = await prepare_context(history.wire, None, SYSTEM, TOOLS, 131_072, no_summary,
                                                  completed_calls=history.completed())
    assert digest(messages) == REQUEST_SNAPSHOT
    assert messages == [SYSTEM, *history.wire]


# --- Through the manager with a fake gateway -----------------------------------------------------------------------------

REAL_CLIENT = httpx.AsyncClient
MANAGER_SNAPSHOT = "e87ace1edc18a5a7"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path / "app")
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="", compaction_handoffs=False, context_window=WINDOW,
                           max_output_tokens=4_096)
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    session = store.create(settings.values)
    session.update(title="Trim", title_generated=True, permission_mode="bypassPermissions")
    store.save(session)
    yield AgentManager(store, settings), session
    store.db.close()


def load(session, history):
    """The chat holds this history (deep copies: the test keeps its own to compare with)."""
    session["wire"], session["events"] = copy.deepcopy(history.wire), copy.deepcopy(history.events)
    return session


def gateway(monkeypatch, requests):
    """A fake endpoint that answers 'Done.' and appends the messages of each agent request to requests."""
    from local_agent.prompt_cache import unmarked

    def handle(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Title"}, "finish_reason": "stop"}]})
        sent = unmarked(body)
        requests.append(sent["messages"])
        chunk = {"choices": [{"delta": {"content": "Done."}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": estimate_tokens(sent["messages"], sent["tools"]), "completion_tokens": 5}}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(handle)))


async def test_with_the_setting_off_the_request_is_byte_identical_to_before(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values["context_trim"] = False  # A key the code before this change never read.
    sent = []
    gateway(monkeypatch, sent)
    await manager.run(load(session, long_history(8)), "Turn 9: next.")
    assert digest(sent[-1][1:]) == MANAGER_SNAPSHOT
    assert not any("context_trim" in event for event in session["events"])


# --- The pass (context.prepare_context with trim=) -----------------------------------------------------------------------

INPUT_BUDGET = context.effective_input_budget(WINDOW, 4_096)


def tokens(percent):
    return INPUT_BUDGET * percent // 100


async def prepare(history, start=70, target=55, notices=None, **options):
    """(messages, state, info, marks) for the history, with the trim pass on. The pass adds its notices (wire
    position: text) to notices, which the next call can be given to see them."""
    marks = []
    messages, state, info = await prepare_context(
        history.wire, None, SYSTEM, TOOLS, WINDOW, no_summary, reply_reserve=4_096,
        completed_calls=history.completed(), trim={"start": start, "target": target, "on_trim": marks.extend},
        notices={} if notices is None else notices, **options)
    return messages, state, info, marks


def tool_messages(messages):
    return [message for message in messages if message.get("role") == "tool"]


async def test_the_pass_does_not_start_under_the_start_mark():
    history = long_history(4)  # About 37% of the input budget.
    before = copy.deepcopy(history.wire)
    messages, _, info, marks = await prepare(history)
    assert marks == [] and messages == [SYSTEM, *before] and history.wire == before
    assert info["estimated_tokens"] < tokens(70)


async def test_the_pass_stops_at_or_under_the_target_and_not_before():
    history = long_history(8)  # About 74% of the input budget: over the start mark, under the compaction threshold.
    assert tokens(70) < estimate_tokens([SYSTEM, *history.wire], TOOLS) < tokens(90)
    messages, _, info, marks = await prepare(history)
    assert marks and info["estimated_tokens"] <= tokens(55)
    # One mark fewer would have left the request over the target.
    assert info["estimated_tokens"] + marks[-1]["freed_tokens"] > tokens(55)
    assert info["estimated_tokens"] == estimate_tokens(messages, TOOLS)
    assert "compaction" not in info


async def test_superseded_reads_go_first_then_large_old_results_oldest_first():
    history = long_history(8)
    messages, _, _, marks = await prepare(history)
    names = {item["call_id"]: item for item in marks}
    superseded = [mark for mark in marks if mark["mode"] == "dropped" and mark["name"] == "read_file"
                  and "shared.py" in history.wire[mark["index"] - 1]["tool_calls"][0]["function"]["arguments"]]
    assert superseded, "the earlier reads of src/shared.py are dropped"
    rest = [mark for mark in marks if mark not in superseded]
    assert marks[:len(superseded)] == superseded and rest, "every superseded read comes before the other results"
    assert [mark["index"] for mark in superseded] == sorted(mark["index"] for mark in superseded)
    assert [mark["index"] for mark in rest] == sorted(mark["index"] for mark in rest)
    assert all(history.wire[mark["index"]]["role"] == "tool" for mark in marks) and len(names) == len(marks)
    # The latest read of the shared page (in the last turn) is the content that stays.
    assert any("src/shared.py" in message["content"] and "line 60 of" in message["content"]
               for message in tool_messages(messages))


async def test_what_the_pass_never_touches():
    history = long_history(8)
    original = copy.deepcopy(history.wire)
    messages, _, _, marks = await prepare(history)
    sent = messages[1:]
    assert len(sent) == len(original)
    last_turn_starts = [index for index, message in enumerate(original) if message["role"] == "user"]
    kept_from = last_turn_starts[-CLEAR_KEEP_TURNS]
    for index, (before, after) in enumerate(zip(original, sent)):
        if index >= kept_from or before["role"] != "tool":
            assert after == before, index  # The last three turns, users, assistant text and tool calls.
        elif index not in {mark["index"] for mark in marks}:
            assert after == before
    small = [index for index, message in enumerate(original) if message["role"] == "tool"
             and len(message["content"].encode()) < CLEAR_MIN_BYTES]
    assert small and all(sent[index] == original[index] for index in small)
    # The stored history is not touched at all: the notices are in the request only.
    assert history.wire == original
    assert {index for index, (before, after) in enumerate(zip(original, sent)) if before != after} == {
        mark["index"] for mark in marks}


async def test_every_tool_call_keeps_a_tool_message_with_its_id():
    history = long_history(8)
    messages, *_ = await prepare(history)
    calls = [item["id"] for message in messages for item in message.get("tool_calls") or []]
    answers = [message["tool_call_id"] for message in tool_messages(messages)]
    assert calls == answers and all(isinstance(message["content"], str) and message["content"]
                                    for message in tool_messages(messages))


async def test_a_result_that_was_already_trimmed_is_not_trimmed_again():
    history = long_history(8)
    notices = {}
    _, _, _, first = await prepare(history, notices=notices)
    marked = {mark["index"]: mark["content"] for mark in first}
    again, _, info, second = await prepare(history, start=50, target=40, notices=notices)  # Over this lower start mark.
    assert second and not {mark["index"] for mark in second} & set(marked)
    for index, content in marked.items():
        assert again[index + 1]["content"] == content  # Same text as the first pass made.


async def test_error_results_stay_unless_large_and_then_keep_their_error_text():
    history = long_history(8)
    failing = history.tool("run_command", {"command": "make"}, json.dumps(
        {"job_id": "j", "state": "failed", "exit_code": 2, "output": "noise\n" * 2_000 + "make: *** [all] Error 2"}),
        state="completed")
    # Move the failing call into the first turn: it is old, large and failed.
    history.wire[1:1] = history.wire[-2:]
    del history.wire[-2:]
    messages, _, _, marks = await prepare(history, target=20)
    assert failing and any(mark["name"] == "run_command" and mark["mode"] == "shortened" for mark in marks)
    failed = next(message for message in tool_messages(messages) if "make: *** [all] Error 2" in message["content"])
    assert "Error 2" in failed["content"] and len(failed["content"]) < 3_000


async def test_a_manual_compaction_still_runs_and_skips_the_pass():
    history = long_history(8)
    summaries = []

    async def summarize(previous, chunk, limit):
        summaries.append(chunk)
        return "## Goal and current status\nWork."
    marks = []
    _, _, info = await prepare_context(history.wire, None, SYSTEM, TOOLS, WINDOW, summarize, reply_reserve=4_096,
                                       force_compact=True, completed_calls=history.completed(),
                                       trim={"start": 70, "target": 55, "on_trim": marks.extend})
    assert info["compactions"] == 1 and summaries and marks == []


# --- Settings ----------------------------------------------------------------------------------------------------------

@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(tmp_path), env_file="")
    return settings


def test_the_settings_default_to_on_at_70_and_55(settings):
    assert (settings.values["context_trim"], settings.values["context_trim_start_percent"],
            settings.values["context_trim_target_percent"]) == (True, 70, 55)


@pytest.mark.parametrize("values, message", [
    ({"context_trim": "yes"}, "Trim old tool results must be on or off."),
    ({"context_trim_start_percent": 39}, "Start trimming at must be a whole number from 40 to 90"),
    ({"context_trim_start_percent": 91, "auto_compact_percent": 100}, "Start trimming at must be a whole number from 40 to 90"),
    ({"context_trim_start_percent": 70.0}, "Start trimming at must be a whole number from 40 to 90"),
    ({"context_trim_start_percent": True}, "Start trimming at must be a whole number from 40 to 90"),
    ({"context_trim_target_percent": 19}, "Trim down to must be a whole number from 20 to 85"),
    ({"context_trim_target_percent": 86}, "Trim down to must be a whole number from 20 to 85"),
    ({"context_trim_target_percent": 55.5}, "Trim down to must be a whole number from 20 to 85"),
])
def test_the_trim_settings_are_validated(settings, values, message):
    with pytest.raises(ValueError, match=message):
        settings.update(values)
    assert settings.values["context_trim_start_percent"] == 70 and settings.values["context_trim_target_percent"] == 55


@pytest.mark.parametrize("values", [
    {"context_trim_start_percent": 90},
    {"context_trim_start_percent": 60, "auto_compact_percent": 60},
    {"auto_compact_percent": 55},  # The start mark stays at 70: the run-time rule moves it, not the save.
    {"context_trim_target_percent": 85},
    {"context_trim_start_percent": 50, "context_trim_target_percent": 48},
])
def test_marks_that_do_not_fit_each_other_or_the_threshold_are_saved(settings, values):
    saved = settings.update(values)
    assert all(saved[key] == value for key, value in values.items())


@pytest.mark.parametrize("saved, compact, marks", [
    ((70, 55), 90, (70, 55)),                # Both fit: used as saved.
    ((70, 55), 75, (70, 55)),                # Start is exactly compact - 5.
    ((70, 55), 74, (69, 55)),                # Start moves to compact - 5.
    ((70, 55), 60, (55, 50)),                # The target follows the start: start - 5.
    ((70, 55), 50, (45, 40)),                # The lowest compaction threshold.
    ((90, 85), 100, (90, 85)),
    ((90, 85), 95, (90, 85)),
    ((90, 85), 90, (85, 80)),
    ((40, 20), 100, (40, 20)),
    ((60, 58), 100, (60, 55)),               # A target above start - 5 follows the start.
])
def test_the_run_time_marks_follow_the_compaction_threshold(saved, compact, marks):
    from local_agent.agents import trim_marks
    assert trim_marks({"context_trim_start_percent": saved[0], "context_trim_target_percent": saved[1],
                       "auto_compact_percent": compact}) == marks


def test_the_effective_start_is_floored_at_20_and_below_25_the_trim_does_not_run(monkeypatch):
    from local_agent import config
    from local_agent.agents import trim_marks
    base = {"context_trim_start_percent": 70, "context_trim_target_percent": 55}
    assert trim_marks({**base, "auto_compact_percent": 30}) == (70, 55)  # Not a valid threshold: the default's.
    monkeypatch.setattr(config, "MIN_AUTO_COMPACT_PERCENT", 10)  # Only so the floor can be reached.
    assert trim_marks({**base, "auto_compact_percent": 30}) == (25, 20)
    assert trim_marks({**base, "auto_compact_percent": 29}) is None
    assert trim_marks({**base, "auto_compact_percent": 10}) is None
    # The same function the chat uses: an unusable saved value means the defaults.
    assert trim_marks({}) == (70, 55)


def test_valid_trim_settings_are_saved_and_survive_a_reload(settings):
    settings.update({"context_trim": False, "context_trim_start_percent": 40, "context_trim_target_percent": 35})
    assert json.loads(settings.path.read_text())["context_trim_start_percent"] == 40
    reloaded = Settings(settings.state_dir)
    assert (reloaded.values["context_trim"], reloaded.values["context_trim_start_percent"],
            reloaded.values["context_trim_target_percent"]) == (False, 40, 35)
    settings.update({"context_trim_start_percent": 89, "auto_compact_percent": 90, "context_trim_target_percent": 84})


def test_hand_edited_trim_settings_that_do_not_fit_mean_the_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "settings.json").write_text(json.dumps(
        {"context_trim": "no", "context_trim_start_percent": 95, "context_trim_target_percent": "x"}))
    values = Settings(tmp_path / "state").values
    assert (values["context_trim"], values["context_trim_start_percent"], values["context_trim_target_percent"]) == (
        True, 70, 55)


def test_the_settings_route_takes_and_rejects_the_trim_fields(settings, tmp_path):
    from fastapi.testclient import TestClient
    from local_agent.api import create_app
    with TestClient(create_app(settings)) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        base = {"workspace": str(tmp_path), "model": "test-model", "env_file": ""}
        ok = client.put("/api/settings", headers=headers, json={**base, "context_trim_start_percent": 60,
                                                                 "context_trim_target_percent": 50})
        assert ok.status_code == 200 and ok.json()["context_trim_start_percent"] == 60
        assert client.put("/api/settings", headers=headers, json={**base, "context_trim_start_percent": 95}).status_code == 422
        late = client.put("/api/settings", headers=headers, json={**base, "auto_compact_percent": 55})
        assert late.status_code == 200 and late.json()["auto_compact_percent"] == 55
        assert late.json()["context_trim_start_percent"] == 60  # Kept as saved; the run-time rule moves the mark.
        assert late.json()["context_trim_effective"] == {"start": 50, "target": 45}  # What a chat really runs at.


def test_the_effective_marks_are_in_the_public_settings_even_for_a_target_above_the_start(settings):
    settings.values.update(context_trim_start_percent=40, context_trim_target_percent=85)  # Saved: no rejection.
    assert settings.public()["context_trim_effective"] == {"start": 40, "target": 35}
    settings.values.update(context_trim_start_percent=70, context_trim_target_percent=55)
    assert settings.public()["context_trim_effective"] == {"start": 70, "target": 55}
    settings.values["auto_compact_percent"] = 60
    assert settings.public()["context_trim_effective"] == {"start": 55, "target": 50}
    settings.values["context_trim"] = False
    assert settings.public()["context_trim_effective"] is None


# --- A live turn through the manager -------------------------------------------------------------------------------------

NOTICE = "to save context (about"


def trim_notices(session):
    return [event for event in session["events"] if event["type"] == "notice" and NOTICE in event.get("text", "")]


def trimmed_events(session):
    return [event for event in session["events"] if "context_trim" in event]


async def trimmed_chat(runtime, monkeypatch, turns=8, **fields):
    """The chat of a long history after one turn that passed the start mark: (manager, session, requests, history)."""
    manager, session = runtime
    session.update(fields)
    sent = []
    gateway(monkeypatch, sent)
    history = long_history(turns)
    await manager.run(load(session, history), f"Turn {turns + 1}: next.")
    return manager, session, sent, history


async def test_a_turn_over_the_start_mark_trims_once_and_records_it_on_the_events(runtime, monkeypatch):
    manager, session, sent, history = await trimmed_chat(runtime, monkeypatch)
    marked = trimmed_events(session)
    assert marked and len(sent) == 1
    for event in marked:
        mark = event["context_trim"]
        assert set(mark) == {"mode", "by", "at", "freed_tokens", "content"}
        assert mark["mode"] in ("dropped", "shortened") and mark["by"] == "auto"
        assert isinstance(mark["at"], float) and type(mark["freed_tokens"]) is int and mark["freed_tokens"] > 0
        assert event["output"] == history.event(event["id"])["output"]  # The card keeps the full result.
    assert any(event["context_trim"]["mode"] == "dropped" for event in marked)
    assert any(event["context_trim"]["mode"] == "shortened" for event in marked)
    # The model got notices in their place.
    results = {message["tool_call_id"]: message["content"] for message in sent[0] if message.get("role") == "tool"}
    for event in marked:
        assert results[event["call_id"]].count("by the app to save context") == 1
        assert event["name"] in results[event["call_id"]]
    # The full results are still in the saved history and the conversation file.
    assert [message["content"] for message in session["wire"] if message["role"] == "tool"] == [
        message["content"] for message in history.wire if message["role"] == "tool"]
    reloaded = manager.store.get(session["id"])
    assert [event["context_trim"] for event in reloaded["events"] if "context_trim" in event] == [
        event["context_trim"] for event in marked]
    assert not any("context_trim" in message for message in reloaded["wire"])


async def test_one_notice_event_per_pass_names_the_count_and_never_reaches_the_model(runtime, monkeypatch):
    manager, session, sent, _ = await trimmed_chat(runtime, monkeypatch)
    [notice] = trim_notices(session)
    marked = trimmed_events(session)
    freed = sum(event["context_trim"]["freed_tokens"] for event in marked)
    assert notice["text"] == f"Shortened {len(marked)} old tool results to save context (about {freed:,} tokens)."
    assert NOTICE not in json.dumps(sent) and "old tool results" not in json.dumps(sent)


async def test_later_requests_send_the_same_bytes_and_the_pass_does_not_run_again(runtime, monkeypatch):
    manager, session, sent, _ = await trimmed_chat(runtime, monkeypatch)
    results = [index for index, message in enumerate(session["wire"]) if message["role"] == "tool"]
    positions = [index for index in results if sent[0][index + 1]["content"] != session["wire"][index]["content"]]
    assert positions
    await manager.run(session, "Turn 10: again.")
    await manager.run(session, "Turn 11: once more.")
    assert len(sent) == 3 and len(trim_notices(session)) == 1
    for index in positions:  # The system message comes first in a request.
        assert json.dumps(sent[0][index + 1]) == json.dumps(sent[1][index + 1]) == json.dumps(sent[2][index + 1])
    assert sent[1][:len(sent[0])][1:-1] == sent[0][1:-1]  # The earlier messages are the same bytes too.


async def test_a_subagent_chat_trims_like_any_chat(runtime, monkeypatch):
    manager, session, sent, _ = await trimmed_chat(runtime, monkeypatch, is_subagent=True)
    assert trimmed_events(session) and len(trim_notices(session)) == 1


async def test_nothing_is_marked_with_the_setting_off_even_over_the_mark(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values["context_trim"] = False
    sent = []
    gateway(monkeypatch, sent)
    await manager.run(load(session, long_history(8)), "Turn 9: next.")
    assert not trimmed_events(session) and not trim_notices(session)
    assert not any("context_trim" in message for message in session["wire"])
    assert estimate_tokens(sent[0], []) > tokens(70)


async def test_a_chat_saved_before_the_trim_existed_behaves_as_before(runtime, monkeypatch):
    manager, session = runtime
    sent = []
    gateway(monkeypatch, sent)
    await manager.run(load(session, long_history(4)), "Turn 5: next.")  # Under the start mark.
    assert not trimmed_events(session) and sent[0][1:-1] == long_history(4).wire


# --- Compaction, rewind, export, import and fork after a trim ------------------------------------------------------------

async def compact(wire, completed):
    chunks = []

    async def summarize(previous, chunk, limit):
        chunks.append(chunk)
        return "## Goal and current status\nWork."
    _, state, info = await prepare_context(wire, None, SYSTEM, TOOLS, WINDOW, summarize, reply_reserve=4_096,
                                           force_compact=True, completed_calls=completed)
    return chunks, state, info


async def test_a_compaction_after_a_trim_summarizes_the_full_results():
    plain, trimmed = long_history(8), long_history(8)
    _, _, _, marks = await prepare(trimmed)
    assert marks
    expected, _, _ = await compact(plain.wire, plain.completed())
    chunks, state, info = await compact(trimmed.wire, trimmed.completed())
    assert chunks == expected and "by the app to save context" not in "".join(chunks)
    assert info["compactions"] == 1 and state["through"] > 0


def cut(session, before):
    from local_agent import history as chat_history
    return chat_history.cut_session(session, before)[0]


async def test_rewinding_before_a_trimmed_events_turn_removes_the_event_and_its_mark_together(runtime, monkeypatch):
    manager, session, _, history = await trimmed_chat(runtime, monkeypatch)
    marked = trimmed_events(session)
    older = [event for event in marked if event["id"] in {e["id"] for e in history.events[:12]}]
    assert older, "a result of the first two turns was trimmed"
    third = next(event["id"] for event in session["events"] if event.get("text", "").startswith("Turn 3"))
    after_cut = cut(session, third)  # Turn 3 and everything after it go.
    kept = {event["id"] for event in after_cut["events"]}
    assert all(event["id"] in kept for event in older)
    assert [event["context_trim"] for event in after_cut["events"] if "context_trim" in event] == [
        event["context_trim"] for event in marked if event["id"] in kept]
    # The marks live on the events only, so the cut wire holds none.
    assert not any("context_trim" in message for message in after_cut["wire"])
    first = next(event["id"] for event in session["events"] if event.get("text", "").startswith("Turn 1:"))
    emptied = cut(session, first)
    assert not trimmed_events(emptied) and not any("context_trim" in message for message in emptied["wire"])
    assert len(trimmed_events(session)) == len(marked)  # The chat itself is not changed.


def export(session):
    from local_agent.portability import export_bundle
    return export_bundle([session], session["id"])


async def test_an_export_strips_the_marks_and_carries_the_full_results(runtime, monkeypatch):
    manager, session, _, history = await trimmed_chat(runtime, monkeypatch)
    assert trimmed_events(session)
    bundle = export(session)
    text = json.dumps(bundle)
    assert "context_trim" not in text and "by the app to save context" not in text
    exported = bundle["sessions"][0]
    assert [message["content"] for message in exported["wire"] if message["role"] == "tool"] == [
        message["content"] for message in history.wire if message["role"] == "tool"]
    assert [event["output"] for event in exported["events"] if event["type"] == "tool"] == [
        event["output"] for event in history.events if event["type"] == "tool"]


async def test_an_imported_or_forked_chat_has_full_results_no_marks_and_a_consistent_wire(runtime, monkeypatch, tmp_path):
    from local_agent.portability import fresh_copies
    manager, session, _, history = await trimmed_chat(runtime, monkeypatch)
    session["terminal_reason"] = "completed"
    for kind in ("import", "fork"):
        sessions, root = fresh_copies(export(session), str(tmp_path), kind=kind)
        [copy_] = sessions
        assert not trimmed_events(copy_) and not any("context_trim" in message for message in copy_["wire"])
        assert [m["content"] for m in copy_["wire"] if m["role"] == "tool"][:len(history.events)] == [
            m["content"] for m in history.wire if m["role"] == "tool"]
        # Every tool call of the copy has its result, under the call ids the copy's events name.
        answered = [m["tool_call_id"] for m in copy_["wire"] if m["role"] == "tool"]
        called = [c["id"] for m in copy_["wire"] for c in m.get("tool_calls") or []]
        assert answered == called
        assert {e["call_id"] for e in copy_["events"] if e["type"] == "tool"} == set(called)
        # The rule: export strips the marks, so the copy is untrimmed and its meter is counted from its own wire.
        info, sender = copy_["context_info"], session["context_info"]
        assert info["trimmed"] == {"events": 0, "tokens": 0} and "last_request" not in info
        assert info["estimated_tokens"] > sender["estimated_tokens"]  # The sender's estimate counted the notices.
        assert info["is_estimate"] is True and info["used_tokens"] == info["estimated_tokens"]
        assert info["percent_used"] == round(100 * info["used_tokens"] / info["limit_tokens"])
        assert info["next_estimate"]["tokens"] == info["estimated_tokens"] and "percent_used" in info and "warn" in info
        full = estimate_tokens(context.context_messages(copy_["wire"], copy_.get("context_state")), [])
        assert abs(info["breakdown"]["messages_and_results"] - full) <= 0.15 * full


def old_portability():
    """portability.py as the last commit has it (before the trim), loaded as a module of its own."""
    import importlib.util
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    shown = subprocess.run(["git", "-C", str(root), "show", "HEAD:local-agent-workspace/backend/local_agent/portability.py"],
                           capture_output=True, text=True, check=True).stdout
    path = Path(__file__).parent / "_old_portability_for_test.py"
    path.write_text(shown)
    try:
        spec = importlib.util.spec_from_file_location("local_agent._old_portability", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        path.unlink()
    return module


async def test_an_older_app_version_accepts_a_bundle_exported_after_a_trim(runtime, monkeypatch, tmp_path):
    manager, session, _, history = await trimmed_chat(runtime, monkeypatch)
    old = old_portability()
    assert "context_trim" not in old.Event.model_fields
    bundle = export(session)
    parsed = old.validate_bundle(json.loads(json.dumps(bundle)))
    sessions, _ = old.fresh_copies(parsed, str(tmp_path))
    assert [m["content"] for m in sessions[0]["wire"] if m["role"] == "tool"][:3] == [
        m["content"] for m in history.wire if m["role"] == "tool"][:3]
    # The old exporter would have refused the marks themselves: that is why the new one strips them.
    with pytest.raises(ValueError):
        old.validate_bundle(json.loads(json.dumps({**bundle, "sessions": [{**bundle["sessions"][0], "wire": [
            {**message, "context_trim": {"mode": "dropped", "content": "x"}} if message["role"] == "tool" else message
            for message in bundle["sessions"][0]["wire"]]}]})))


async def test_a_session_saved_with_marks_loads_back_with_the_same_notices(runtime, monkeypatch):
    manager, session, sent, _ = await trimmed_chat(runtime, monkeypatch)
    reloaded = manager.store.get(session["id"])
    again = []
    gateway(monkeypatch, again)
    manager.live.pop(session["id"], None)
    await manager.run(reloaded, "Turn 10: after a reload.")
    marked = [index for index, message in enumerate(reloaded["wire"]) if message["role"] == "tool"
              and sent[0][index + 1]["content"] != message["content"]]
    assert marked and all(json.dumps(sent[0][index + 1]) == json.dumps(again[0][index + 1]) for index in marked)
    assert len(trim_notices(reloaded)) == 1


# --- The largest items (context_info.largest_items and trimmed) --------------------------------------------------------

async def test_largest_items_are_at_most_ten_sorted_with_the_right_can_trim(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values["context_trim"] = False
    gateway(monkeypatch, [])
    history = long_history(8)
    await manager.run(load(session, history), "Turn 9: next.")
    items = session["context_info"]["largest_items"]
    assert 0 < len(items) <= 10
    assert [item["tokens"] for item in items] == sorted((item["tokens"] for item in items), reverse=True)
    assert all(set(item) == {"event_id", "kind", "label", "tokens", "can_trim"} for item in items)
    assert all(len(item["label"]) <= 160 and type(item["tokens"]) is int for item in items)
    by_event = {event["id"]: event for event in session["events"]}
    turn_starts = [index for index, message in enumerate(session["wire"]) if message["role"] == "user"]
    last_three = {message["tool_call_id"] for message in session["wire"][turn_starts[-3]:] if message["role"] == "tool"}
    for item in items:
        event = by_event[item["event_id"]]
        assert item["kind"] in ("tool_result", "file_read")
        assert item["kind"] == ("file_read" if event["name"] == "read_file" else "tool_result")
        assert item["can_trim"] is (event["call_id"] not in last_three)
        assert item["label"].startswith(event["name"] + ": ")
    assert any(item["can_trim"] for item in items) and not any(item["can_trim"] for item in items
                                                                if by_event[item["event_id"]]["call_id"] in last_three)
    assert session["context_info"]["trimmed"] == {"events": 0, "tokens": 0}


async def test_messages_the_summary_and_attachments_are_listed_but_never_trimmable():
    from local_agent.context import context_items
    history = long_history(2)
    history.wire[0]["content"] = "Please read this. " * 4_000
    history.wire[1:1] = [{"role": "assistant", "content": "Understood. " * 3_000}]
    state = {"summary": "Earlier decisions. " * 2_000, "through": 0, "compactions": 1}
    kinds = {item["kind"]: item for item in context_items(history.wire, state, 1.0, limit=50)}
    assert kinds["summary"]["can_trim"] is False and kinds["message"]["can_trim"] is False
    attachment = context_items([{"role": "user", "content": "Look.\n\n---\n[Command /deploy" + "x" * 5_000}], {}, 1.0)
    assert attachment[0]["kind"] == "attachment" and attachment[0]["can_trim"] is False


async def test_largest_items_follow_the_trim_and_the_totals_count_the_marks(runtime, monkeypatch):
    manager, session, _, history = await trimmed_chat(runtime, monkeypatch)
    marked = {event["id"]: event for event in trimmed_events(session)}
    info = session["context_info"]
    assert info["trimmed"] == {"events": len(marked),
                               "tokens": sum(event["context_trim"]["freed_tokens"] for event in marked.values())}
    for item in info["largest_items"]:
        event = marked.get(item["event_id"])
        if event and event["context_trim"]["mode"] == "dropped":
            assert item["can_trim"] is False  # Dropped: nothing more to take.
        if event and event["context_trim"]["mode"] == "shortened":
            assert item["can_trim"] is True  # A shortened result can still be dropped.
    # The sizes are those of what a request carries, so a trimmed result is small.
    assert all(item["tokens"] < 2_000 for item in info["largest_items"] if item["event_id"] in marked)


async def test_a_label_is_redacted_and_cut_to_160_characters(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values["context_trim"] = False
    gateway(monkeypatch, [])
    history = long_history(5)
    secret = "sk-" + "a1b2c3d4" * 6
    history.tool("run_command", {"command": f"curl -H 'Authorization: Bearer {secret}' https://example.test/" + "p" * 300},
                 command_output("secret", 20))
    history.reply("ok")
    for turn in range(3):
        history.user(f"Later {turn}.")
        history.reply("fine")
    await manager.run(load(session, history), "Now?")
    labels = [item["label"] for item in session["context_info"]["largest_items"]]
    assert any(label.startswith("run_command: curl") for label in labels)
    assert all(secret not in label and len(label) <= 160 for label in labels)


async def test_a_chat_with_a_low_compaction_threshold_trims_at_the_fitted_marks(runtime):
    manager, session = runtime
    manager.settings.values["auto_compact_percent"] = 60  # Saved start 70 is not below it: the chat's start is 55.
    plan = manager.trim_settings(session)
    assert (plan["start"], plan["target"]) == (55, 50)
    manager.settings.values["context_trim"] = False
    assert manager.trim_settings(session) is None


# === Remediation after the review (H2, M2 to M5, L2) ======================================================================

from pathlib import Path


def notice_check(messages):
    """Every trimmed result of a request names the tool and the subject of the call it answers (the nearest preceding
    call with its id). Returns how many trimmed results there were."""
    current, trimmed = {}, 0
    for message in messages:
        for item in message.get("tool_calls") or []:
            current[item["id"]] = (item["function"]["name"], json.loads(item["function"]["arguments"]))
        if message.get("role") == "tool" and "by the app to save context" in message["content"]:
            name, arguments = current[message["tool_call_id"]]
            subject = arguments.get("path") or arguments.get("command")
            assert f"{name} result for {subject}" in message["content"], (name, subject, message["content"][:160])
            trimmed += 1
    return trimmed


def reuse_id(history, first_path, second_path):
    """The call that read second_path gets the id of the call that read first_path (providers reuse ids)."""
    def call_id_of(path):
        return next(item["id"] for message in history.wire for item in message.get("tool_calls") or []
                    if json.loads(item["function"]["arguments"]).get("path") == path)
    old, new = call_id_of(second_path), call_id_of(first_path)
    for message in history.wire:
        for item in message.get("tool_calls") or []:
            item["id"] = new if item["id"] == old else item["id"]
        if message.get("tool_call_id") == old:
            message["tool_call_id"] = new
    for event in history.events:
        if event.get("call_id") == old:
            event["call_id"] = new
    return new


async def test_a_reused_call_id_never_puts_another_calls_tool_or_subject_in_a_notice(runtime, monkeypatch):
    manager, session = runtime
    history = long_history(8)
    reply, reply_event = history.wire.pop(), history.events.pop()
    first = next(m["tool_call_id"] for m in history.wire if m["role"] == "tool" and "src/module_1.py" in m["content"])
    history.tool("run_command", {"command": "rm -rf build"}, json.dumps(
        {"job_id": "job-rm", "state": "completed", "exit_code": 0, "output": "removed"}), call_id=first)
    history.wire.append(reply)
    history.events.append(reply_event)
    sent = []
    gateway(monkeypatch, sent)
    await manager.run(load(session, history), "Turn 9: next.")
    assert notice_check(sent[0]) >= 3
    module = next(m for m in sent[0] if m.get("role") == "tool" and "src/module_1.py" in m["content"])
    assert "read_file result for src/module_1.py" in module["content"] and "rm -rf" not in module["content"]
    assert "Read the file again" in module["content"]
    marked = [event for event in trimmed_events(session) if event["call_id"] == first]
    assert [event["name"] for event in marked] == ["read_file"]  # The command that reused the id is not marked.


async def test_a_users_drop_trims_the_result_of_the_event_it_names_not_an_earlier_one_with_the_same_id(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values["context_trim"] = False
    history = long_history(7)
    shared = reuse_id(history, "src/module_2.py", "src/module_4.py")
    sent = []
    gateway(monkeypatch, sent)
    await manager.run(load(session, history), "Turn 8: next.")
    events = [event for event in session["events"] if event.get("call_id") == shared]
    assert len(events) == 2 and "module_2" in events[0]["input"]["path"] and "module_4" in events[1]["input"]["path"]
    await manager.user_trim(session, events[1]["id"], "dropped")
    assert "context_trim" in events[1] and "context_trim" not in events[0]
    await manager.run(session, "Turn 9: again.")
    results = [m for m in sent[-1] if m.get("tool_call_id") == shared]
    assert len(results) == 2
    assert "src/module_2.py" in results[0]["content"] and "Cleared by the app" not in results[0]["content"]
    assert "Cleared by the app" in results[1]["content"] and "src/module_4.py" in results[1]["content"]


# --- M5: the stored wire stays clean --------------------------------------------------------------------------------------

async def test_a_trim_leaves_the_stored_wire_byte_identical_and_the_notice_lives_on_the_event(runtime, monkeypatch):
    manager, session, sent, history = await trimmed_chat(runtime, monkeypatch)
    marked = trimmed_events(session)
    assert marked
    assert json.dumps(session["wire"][:len(history.wire)], sort_keys=True) == json.dumps(history.wire, sort_keys=True)
    assert not any("context_trim" in message for message in session["wire"])
    reloaded = manager.store.get(session["id"])
    assert not any("context_trim" in message for message in reloaded["wire"])
    results = {message["tool_call_id"]: message["content"] for message in sent[0] if message.get("role") == "tool"}
    for event in marked:
        mark = event["context_trim"]
        assert set(mark) == {"mode", "by", "at", "freed_tokens", "content"}
        assert results[event["call_id"]] == mark["content"] != event["output"]


def old_context():
    """context.py as the last commit has it (before the trim), loaded as a module of its own."""
    import importlib.util
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[2]
    shown = subprocess.run(["git", "-C", str(root), "show", "HEAD:local-agent-workspace/backend/local_agent/context.py"],
                           capture_output=True, text=True, check=True).stdout
    path = Path(__file__).parent / "_old_context_for_test.py"
    path.write_text(shown)
    try:
        spec = importlib.util.spec_from_file_location("local_agent._old_context", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        path.unlink()
    return module


async def test_the_previous_app_version_builds_a_clean_request_from_a_chat_saved_after_a_trim(runtime, monkeypatch):
    manager, session, sent, history = await trimmed_chat(runtime, monkeypatch)
    assert trimmed_events(session)
    saved = manager.store.get(session["id"])
    messages = old_context().context_messages(saved["wire"], saved.get("context_state"))
    assert {key for message in messages for key in message} <= {"role", "content", "tool_calls", "tool_call_id"}
    # It sends the full results: the previous version's normal behaviour.
    assert [m["content"] for m in messages if m["role"] == "tool"][:len(history.events)] == [
        m["content"] for m in history.wire if m["role"] == "tool"]


def old_form(history, positions):
    """The earlier form of the mark: the private field in the wire message, the event without the notice text."""
    session = history.session()
    for position in positions:
        message = session["wire"][position]
        event = next(item for item in session["events"] if item.get("call_id") == message["tool_call_id"])
        message["context_trim"] = {"mode": "dropped", "content": f"[Cleared by the app to save context: n{position}]"}
        event["context_trim"] = {"mode": "dropped", "by": "auto", "at": 1.0, "freed_tokens": 500}
    return session


def test_a_chat_saved_with_the_private_field_in_its_wire_is_repaired_once():
    from local_agent.context import repair_trim_marks
    history = long_history(8)
    tools = [index for index, message in enumerate(history.wire) if message["role"] == "tool"][:3]
    session = old_form(history, tools)
    clean = [{key: value for key, value in message.items() if key != "context_trim"} for message in session["wire"]]
    repair_trim_marks(session)
    assert session["wire"] == clean
    marks = [event["context_trim"] for event in session["events"] if "context_trim" in event]
    assert [mark["content"] for mark in marks] == [f"[Cleared by the app to save context: n{p}]" for p in tools]
    assert all(mark["by"] == "auto" and mark["freed_tokens"] == 500 for mark in marks)
    once = copy.deepcopy(session)
    repair_trim_marks(session)
    assert session == once  # Idempotent.


async def test_a_chat_saved_with_the_private_field_sends_its_notices_and_loses_the_field(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values["context_trim"] = False
    history = long_history(8)
    tools = [index for index, message in enumerate(history.wire) if message["role"] == "tool"][:3]
    old = old_form(history, tools)
    session["wire"], session["events"] = old["wire"], old["events"]
    sent = []
    gateway(monkeypatch, sent)
    await manager.run(session, "Turn 9: next.")
    assert not any("context_trim" in message for message in session["wire"])
    for position in tools:
        assert sent[0][position + 1]["content"] == f"[Cleared by the app to save context: n{position}]"


# --- H2: results the app never trims -------------------------------------------------------------------------------------

BIG_TEXT = "The user's own words. " * 120  # About 2.6 KB: over the size the pass looks at.


def special_results(history):
    """Old, large results of the tools whose results the app keeps whole, plus decisions recorded in a result."""
    kinds = [("ask_user", {"question": "Which database?"}, json.dumps({"answer": BIG_TEXT})),
             ("submit_plan", {"plan": "Do it"}, json.dumps({"decision": "approved with edits", "plan": BIG_TEXT})),
             ("submit_report", {"summary": "s"}, BIG_TEXT),
             ("remember", {"text": "note"}, json.dumps({"saved": BIG_TEXT})),
             ("create_task", {"title": "t"}, json.dumps({"id": "t1", "title": BIG_TEXT})),
             ("update_task", {"task_id": "t1"}, json.dumps({"id": "t1", "title": BIG_TEXT})),
             ("list_tasks", {}, json.dumps({"tasks": BIG_TEXT})),
             ("get_task", {"task_id": "t1"}, json.dumps({"id": "t1", "description": BIG_TEXT})),
             ("use_skill", {"skill_id": "deploy"}, json.dumps({"message": BIG_TEXT})),
             ("list_skills", {}, json.dumps({"skills": BIG_TEXT})),
             ("access_directory", {"path": "/data"}, json.dumps({"granted": BIG_TEXT})),
             ("delegate_task", {"task": "look"}, json.dumps({"child_session_id": "c" * 36, "result": BIG_TEXT}))]
    ids = {name: history.tool(name, arguments, output + " " * 20_000 * 0)
           for name, arguments, output in kinds}
    ids["declined"] = history.tool("run_command", {"command": "rm -rf /"}, BIG_TEXT, state="rejected")
    ids["edited"] = history.tool("run_command", {"command": "ls"},
                                 "[The user edited this command before approving it. It ran as: ls. Your proposed command "
                                 "did not run.]\n" + BIG_TEXT)
    return ids


async def test_the_pass_never_trims_the_users_answers_decisions_and_the_state_the_model_must_keep(runtime, monkeypatch):
    manager, session = runtime
    ids = {}
    history = long_history(8, first=lambda h: ids.update(special_results(h)))
    sent = []
    gateway(monkeypatch, sent)
    await manager.run(load(session, history), "Turn 9: next.")
    marked = {event["id"] for event in trimmed_events(session)}
    assert marked, "the pass ran"
    assert not marked & set(ids.values()), {name for name, i in ids.items() if i in marked}
    # Every one of them reached the model whole.
    for event in session["events"]:
        if event["id"] in ids.values():
            sent_result = next(m for m in sent[0] if m.get("tool_call_id") == event["call_id"])
            assert sent_result["content"] == event["output"]
    # The meter lists them as not trimmable, so the page shows no buttons; and the route refuses them.
    for item in session["context_info"]["largest_items"]:
        if item["event_id"] in ids.values():
            assert item["can_trim"] is False
    for name, event_id in ids.items():
        for mode in ("shortened", "dropped"):
            with pytest.raises(ValueError):
                manager.trim_choice(session, event_id, mode)


async def test_a_subagent_report_may_be_shortened_with_the_way_to_read_it_again_but_never_dropped(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values["context_trim"] = False
    report = json.dumps({"subagent_id": "a1b2c3d4-0000-4000-8000-000000000001", "status": "completed",
                         "result": {"summary": "Findings. " * 300}})
    ids = {}
    history = long_history(7, first=lambda h: ids.update(report=h.tool("get_subagent_result", {"subagent_id": "a1b2c3d4"}, report)))
    sent = []
    gateway(monkeypatch, sent)
    await manager.run(load(session, history), "Turn 9: next.")
    with pytest.raises(ValueError):
        manager.trim_choice(session, ids["report"], "dropped")
    await manager.user_trim(session, ids["report"], "shortened")
    await manager.run(session, "Turn 10: again.")
    shown = next(m for m in sent[-1] if m.get("tool_call_id") == next(
        e["call_id"] for e in session["events"] if e["id"] == ids["report"]))["content"]
    assert "Shortened by the app" in shown and "get_subagent_result" in shown and "a1b2c3d4-0000-4000-8000-000000000001" in shown
    assert next(e for e in session["events"] if e["id"] == ids["report"])["context_trim"]["mode"] == "shortened"


# --- L2: the recorded mode is the one that was sent ----------------------------------------------------------------------------

async def test_shortening_a_small_result_records_dropped_because_the_cleared_notice_is_what_was_sent(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values["context_trim"] = False
    history = long_history(8)
    sent = []
    gateway(monkeypatch, sent)
    await manager.run(load(session, history), "Turn 9: next.")
    small = next(e for e in session["events"] if e.get("name") == "read_file" and "small_1" in e["input"]["path"])
    await manager.user_trim(session, small["id"], "shortened")
    assert small["context_trim"]["mode"] == "dropped" and small["context_trim"]["content"].startswith("[Cleared by the app")
    with pytest.raises(ValueError):
        manager.trim_choice(session, small["id"], "dropped")
    assert small["context_trim"]["freed_tokens"] > 0


# --- M3 and M2 through the real tools: a scripted model that reads files ------------------------------------------------------

def files_of(session, turns, per_turn, kilobytes=11):
    folder = Path(session["workspace"]) / "data"
    folder.mkdir(exist_ok=True)
    for turn in range(1, turns + 1):
        for index in range(per_turn):
            body = "\n".join(f"turn {turn} file {index} line {line} " + "z" * 60 for line in range(kilobytes * 1000 // 80))
            (folder / f"f_{turn}_{index}.txt").write_text(body)
    (folder / "shared.txt").write_text("\n".join(f"shared line {line} " + "s" * 60 for line in range(kilobytes * 1000 // 80)))


def scripted(monkeypatch, requests, per_turn=3, shared=False):
    """A model that reads per_turn files in the first response of a turn (ids call_1, call_2, ... reused every turn)
    and answers 'Done.' to their results. With shared, its first read is always data/shared.txt."""
    from local_agent.prompt_cache import unmarked
    turn = {"number": 0}

    def handle(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Title"}, "finish_reason": "stop"}]})
        sent = unmarked(body)
        messages = sent["messages"]
        requests.append(messages)
        usage = {"prompt_tokens": estimate_tokens(messages, sent["tools"]), "completion_tokens": 5}
        if messages[-1].get("role") == "user":
            turn["number"] += 1
            calls = [{"index": index, "id": f"call_{index + 1}", "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"path": "data/shared.txt" if shared and index == 0
                                                              else f"data/f_{turn['number']}_{index}.txt"})}}
                     for index in range(per_turn)]
            chunk = {"choices": [{"delta": {"tool_calls": calls}, "finish_reason": "tool_calls"}], "usage": usage}
        else:
            chunk = {"choices": [{"delta": {"content": "Done."}, "finish_reason": "stop"}], "usage": usage}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(handle)))


def pass_turns(session):
    """The turn (1 for the first) in which each trim pass ran."""
    events = session["events"]
    return [sum(1 for item in events[:index] if item["type"] == "user") for index, event in enumerate(events)
            if event["type"] == "notice" and NOTICE in event.get("text", "")]


async def test_heavy_turns_pass_at_most_once_per_four_turns_and_never_in_consecutive_turns(runtime, monkeypatch):
    manager, session = runtime
    files_of(session, 11, 3)
    sent = []
    scripted(monkeypatch, sent)
    for turn in range(1, 12):
        await manager.run(session, f"Turn {turn}: read the next files.")
    turns = pass_turns(session)
    assert turns and len(turns) <= 3, turns  # The reviewer's shape ran a pass in every turn.
    assert all(later - earlier >= 4 for earlier, later in zip(turns, turns[1:])), turns
    assert session["context_state"]["compactions"] <= 2


async def test_a_pass_that_frees_little_does_not_run(runtime, monkeypatch):
    manager, session = runtime
    files_of(session, 5, 1, kilobytes=3)
    manager.settings.values.update(context_trim_start_percent=40, context_trim_target_percent=35)
    sent = []
    scripted(monkeypatch, sent, per_turn=1)
    for turn in range(1, 6):
        await manager.run(session, f"Turn {turn}: read the next file.")
    assert pass_turns(session) == []  # Over 40% of nothing: a 3 KB result frees far less than a tenth of the budget.


async def test_a_pass_that_cannot_reach_the_target_when_compaction_is_due_is_skipped_and_the_chat_compacts():
    history = long_history(8)
    summaries, marks = [], []

    async def summarize(previous, chunk, limit):
        summaries.append(chunk)
        return "## Goal and current status\nWork."
    _, _, info = await prepare_context(history.wire, None, SYSTEM, TOOLS, 24_576, summarize, reply_reserve=4_096,
                                       auto_compact_percent=50, completed_calls=history.completed(),
                                       trim={"start": 40, "target": 20, "on_trim": marks.extend})
    assert marks == [] and info["compactions"] == 1 and summaries


async def test_a_trimmed_read_no_longer_satisfies_the_read_before_write_guard_but_a_later_full_read_does(runtime, monkeypatch):
    manager, session = runtime
    files_of(session, 9, 3)
    sent = []
    scripted(monkeypatch, sent, shared=True)
    for turn in range(1, 10):
        await manager.run(session, f"Turn {turn}: read the next files.")
    tools = manager.session_tools(session)
    marked = [event for event in trimmed_events(session) if event["name"] == "read_file"]
    assert marked
    gone = [event for event in marked if "shared" not in event["input"]["path"]]
    assert gone
    for event in gone:
        refusal = AgentManager.overwrite_refusal(session, tools, event["input"]["path"])
        assert refusal and "again before overwriting" in refusal, event["input"]["path"]
    # shared.txt was read in every turn: the earlier reads are dropped, the latest one is in context and keeps its view.
    assert AgentManager.overwrite_refusal(session, tools, "data/shared.txt") is None
    latest = [event for event in session["events"] if event.get("name") == "read_file" and "shared" in event["input"]["path"]][-1]
    assert "context_trim" not in latest


async def test_a_users_drop_of_a_read_makes_the_model_read_the_file_again_before_overwriting_it(runtime, monkeypatch):
    manager, session = runtime
    manager.settings.values["context_trim"] = False
    files_of(session, 8, 1)
    sent = []
    scripted(monkeypatch, sent, per_turn=1)
    for turn in range(1, 9):
        await manager.run(session, f"Turn {turn}: read the next file.")
    tools = manager.session_tools(session)
    first = next(e for e in session["events"] if e.get("name") == "read_file")
    assert AgentManager.overwrite_refusal(session, tools, first["input"]["path"]) is None
    await manager.user_trim(session, first["id"], "dropped")
    refusal = AgentManager.overwrite_refusal(session, tools, first["input"]["path"])
    assert refusal and "again before overwriting" in refusal
    later = next(e for e in session["events"] if e.get("name") == "read_file" and e is not first)
    assert AgentManager.overwrite_refusal(session, tools, later["input"]["path"]) is None  # Its own read is whole.
