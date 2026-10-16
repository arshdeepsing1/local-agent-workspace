"""Before a paid summary request (M5e): tool results of the older summarized turns are cleared from the summary
request only, so a long chat makes fewer or smaller summary requests; the stored history and the agent's context
never change. Also the meter's rows per selected skill, and the summary prompts' note on the app record (CMP-9)."""
import json

import httpx
import pytest

from local_agent import context
from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.context import (CLEAR_MIN_BYTES, FILES_NOTE, build_condense_messages, build_handoff_messages,
                                 build_summary_messages, prepare_context, summary_transcript)
from local_agent.store import Store

REAL_CLIENT = httpx.AsyncClient
SYSTEM = {"role": "system", "content": "Rules."}


def user(text):
    return {"role": "user", "content": text}


def read_call(call_id, path):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {
        "name": "read_file", "arguments": json.dumps({"path": path})}}]}


def result(call_id, text):
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def long_chat(turns, size=20_000):
    """turns user requests, each reading its own large file and answering."""
    wire = []
    for number in range(turns):
        wire += [user(f"Turn {number}: read part {number}."), read_call(f"r{number}", f"part{number}.txt"),
                 result(f"r{number}", json.dumps({"path": f"part{number}.txt", "content": f"line {number} " * (size // 8)})),
                 {"role": "assistant", "content": f"Part {number} says what it says."}]
    return wire


def transcript_messages(archived, completed=frozenset(), **options):
    text, stats = summary_transcript(archived, [], 64_000, set(completed), **options)
    return json.loads(text), stats


def test_older_turns_lose_their_large_results_in_the_summary_request_only():
    archived = long_chat(5)
    archived[2 * 4 + 2] = result("r2", "small result")  # Turn 2's result is small: kept.
    stored = json.dumps(archived)
    messages, stats = transcript_messages(archived, keep_turns=3)
    contents = [message["content"] for message in messages if message["role"] == "tool"]
    assert contents[0].startswith("[Output cleared by the app before summarizing: 17 KB from read_file part0.txt, "
                                  "in an older turn. Record only what the conversation did with it; do not guess what "
                                  "it said.]")
    assert contents[1].startswith("[Output cleared by the app before summarizing:") and "part1.txt" in contents[1]
    assert contents[2] == "small result" and all(text.startswith('{"path"') for text in contents[3:])
    assert stats["cleared"] == 2 and stats["cleared_bytes"] > 2 * 17_000
    # The pairing stays: every result keeps its tool_call_id; the assistant's replies stay whole.
    assert [message.get("tool_call_id") for message in messages if message["role"] == "tool"] == [
        f"r{number}" for number in range(5)]
    assert [message["content"] for message in messages if message["role"] == "assistant" and message["content"]] == [
        f"Part {number} says what it says." for number in range(5)]
    assert json.dumps(archived) == stored  # The history is never changed.
    # Without keep_turns (the summarizer view of an older caller) nothing is cleared, and the stats are as before.
    messages, stats = transcript_messages(archived)
    assert "Output cleared" not in json.dumps(messages) and set(stats) == {"elided_bytes", "superseded", "shortened"}
    # Three turns or fewer: all kept. Results under the threshold: kept.
    assert transcript_messages(long_chat(3), keep_turns=3)[1]["cleared"] == 0
    assert transcript_messages(long_chat(6, CLEAR_MIN_BYTES - 200), keep_turns=3)[1]["cleared"] == 0


def test_a_failed_command_keeps_the_end_of_its_output_and_a_superseded_read_its_own_marker():
    failure = json.dumps({"job_id": "j1", "state": "failed", "exit_code": 1,
                          "output": "log line\n" * 600 + "FAILED tests/test_x.py::test_y - assert 1 == 2\n"})
    archived = [user("Run the tests"), {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": "pytest"})}}]},
        result("c1", failure), {"role": "assistant", "content": "It failed."},
        user("Read it"), read_call("r1", "a.py"), result("r1", "x" * 5_000), {"role": "assistant", "content": "ok"},
        user("Read it again"), read_call("r2", "a.py"), result("r2", "x" * 5_000), {"role": "assistant", "content": "ok"},
        user("Next"), {"role": "assistant", "content": "ok"}, user("Last"), {"role": "assistant", "content": "ok"}]
    messages, stats = transcript_messages(archived, {"c1", "r1", "r2"}, keep_turns=3, handoff=True)
    shown = [message["content"] for message in messages if message["role"] == "tool"]
    assert shown[0].startswith("[Output cleared by the app before summarizing: ") and " from run_command, in an older " \
        "turn. The handoff's activity log lists the call. " in shown[0]
    stub, tail = shown[0].split("]\nThe failed command's output ended with:\n")
    assert tail.endswith("log line\nlog line\nFAILED tests/test_x.py::test_y - assert 1 == 2")
    assert tail.startswith("log line\n") and len(tail.encode()) <= 600  # Whole lines from the end.
    # The first read of a.py was read again later: it keeps the superseded marker, which says why.
    assert shown[1].startswith("[The app omitted this read_file result for a.py before summarizing")
    # The second read is in the latest three turns: whole.
    assert shown[2] == "x" * 5_000 and stats["cleared"] == 1 and stats["superseded"] == 1


def summary_counter():
    calls = []

    async def summarize(previous, chunk, limit):
        calls.append(chunk)
        return "## Goal and current status\nReading the parts."
    return calls, summarize


async def test_a_replayed_long_chat_makes_fewer_and_smaller_summary_requests(monkeypatch):
    wire = [*long_chat(14), user("Now summarize what the parts say.")]

    async def compact():
        calls, summarize = summary_counter()
        _, state, info = await prepare_context(wire, {}, SYSTEM, [], 32_768, summarize, reply_reserve=4_096,
                                               force_compact=True)
        return calls, info["compaction"]
    cleared_calls, cleared = await compact()
    monkeypatch.setattr(context, "CLEAR_KEEP_TURNS", 10 ** 6)  # As before: every result goes to the summarizer.
    full_calls, full = await compact()
    assert len(cleared_calls) < len(full_calls) and sum(map(len, cleared_calls)) < sum(map(len, full_calls)) // 3
    assert cleared["cleared_results"] == 11 and cleared["cleared_bytes"] > 11 * 17_000
    assert "cleared_results" not in full and cleared["summarized_messages"] == full["summarized_messages"]
    # The latest three summarized turns kept their results.
    assert all(f"line {number} " in "".join(cleared_calls) for number in (11, 12, 13))
    assert "line 10 " not in "".join(cleared_calls)


async def test_an_in_turn_compaction_keeps_its_results():
    wire = [user("Read everything")]
    for number in range(12):
        wire += [read_call(f"c{number}", f"f{number}.txt"), result(f"c{number}", "y" * 12_000)]
    calls, summarize = summary_counter()
    _, _, info = await prepare_context(wire, {}, SYSTEM, [], 32_768, summarize, reply_reserve=4_096)
    assert info.get("compacted_in_turn") and "cleared_results" not in info["compaction"]
    assert "Output cleared" not in "".join(calls)


def test_the_summary_handoff_and_condense_prompts_say_the_app_records_the_files():
    assert FILES_NOTE == ('The app records which files were read and changed and keeps that list next to the summary, '
                          'so under "Files read and changed" name only the files that matter for the work ahead, and '
                          'why. ')
    for messages in (build_summary_messages("", "[]"), build_handoff_messages("", "[]"),
                     build_condense_messages("## Goal and current status\nx", 4_000)):
        assert FILES_NOTE in messages[0]["content"]


# The meter's rows per selected skill

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path / "app")
    project = tmp_path / "project"
    for skill_id, text in (("review", "Review rule: trace behavior. " * 20), ("style", "Style rule: short names.")):
        path = project / ".agents/skills" / skill_id / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(text)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    session = store.create(settings.values)
    session.update(title="Skills", title_generated=True, permission_mode="bypassPermissions")
    store.save(session)
    yield AgentManager(store, settings), session
    store.db.close()


def gateway(monkeypatch):
    async def handle(request):
        chunk = {"choices": [{"delta": {"content": "Done."}, "finish_reason": "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(handle)))


def skill_rows(session):
    return [(source["path"], source["scope"]) for source in session["context_info"]["instruction_sources"]
            if source["path"].startswith("Skill /")]


async def test_each_selected_skill_has_its_own_row(runtime, monkeypatch):
    manager, session = runtime
    gateway(monkeypatch)
    manager.select_skill(session, manager.session_tools(session), "style")
    await manager.run(session, "/review the parser")
    assert skill_rows(session) == [("Skill /style", "this chat"), ("Skill /review", "this message")]
    rows = {source["path"]: source for source in session["context_info"]["instruction_sources"]}
    assert rows["Skill /review"]["estimated_tokens"] > rows["Skill /style"]["estimated_tokens"] > 0
    assert rows["Skill /review"]["status"] == "loaded"
    # The next message selects none: only the chat's skill stays listed.
    await manager.run(session, "Thanks")
    assert skill_rows(session) == [("Skill /style", "this chat")]
