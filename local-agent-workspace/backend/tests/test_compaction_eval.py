"""An offline evaluation of automatic compaction with the real agent and real tools.

A scripted fake gateway plays the model: it asks for tool calls that the agent really
executes in a temporary workspace (reads, an edit, a failing command, a large write),
and plants facts in the user's messages. Non-streamed requests act as a "perfect
summarizer" that keeps every FACT line it is shown. The test then checks that what the
model needs survives compaction: in the summary requests, in the first request after
compaction, in the read-before-write guard and in the saved handoff.
"""
import json
import re
import sys

import httpx
import pytest

from local_agent.agents import AgentManager, model_history
from local_agent.config import Settings
from local_agent.context import (
    OUTPUT_LIMIT_CONTINUATION, SUMMARY_SECTIONS, VERIFY_CONTINUATION_PREFIX, VERIFY_NOTE_LABEL,
)
from local_agent.store import Store


MODEL = "databricks-claude-opus-5-5"
TOKEN = "dapi" + "0123456789abcdef" * 2
CONSTRAINT = "FACT-constraint: never edit files under migrations/legacy/."
PREFERENCE = "FACT-pref: use tabs."
DECISION = "FACT-decision: use PostgreSQL advisory locks for refunds."
GOAL = (f"Build the billing service refunds. {CONSTRAINT} {PREFERENCE} The staging key is {TOKEN}; never print it. "
        "Start by reading src/billing/db.py and src/billing/api.py.")
FAILED = "FAILED tests/billing/test_db.py::test_refund - AssertionError: expected 120 got 100"
NOTE = "keep the refund rounding as it is."
FACTS = (CONSTRAINT, PREFERENCE, DECISION)


def db_source():
    lines = [f"    value_{n} = compute_total({n})  # billing helper line" for n in range(1, 401)]
    lines[199] = "class BillingRepository:"
    return "\n".join(lines) + "\n"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    (project / "src" / "billing").mkdir(parents=True)
    (project / "tools").mkdir()
    (project / "src" / "billing" / "db.py").write_text(db_source())
    (project / "src" / "billing" / "api.py").write_text(
        "".join(f"def handler_{n}(request):\n    return respond(request, {n})\n\n" for n in range(40)))
    (project / "tools" / "fail.py").write_text(
        "import sys\nprint('log line ' * 1250)\n"
        f"print({FAILED!r})\nsys.exit(1)\n")
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="", model=MODEL, context_window=65_536,
                           auto_compact_percent=90, compaction_handoffs=True)
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "eval.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session.update(model=MODEL, permission_mode="bypassPermissions", title="Billing refunds")
    store.save(session)
    tasks = [manager.task_board.create(session["id"], title=title)
             for title in ("Add the refund endpoint", "Fix test_refund", "Document refunds")]
    yield manager, session, project, tasks
    store.db.close()


class Model:
    """Streams scripted steps; answers non-streamed requests as a perfect summarizer."""

    def __init__(self):
        self.steps, self.streamed, self.summaries, self.condenses = [], [], [], []

    def __call__(self, request):
        payload = json.loads(request.content)
        if payload["stream"]:
            self.streamed.append((len(self.summaries), payload))
            return self.steps.pop(0)
        system, content = (message["content"] for message in payload["messages"][:2])
        if system.startswith("Create a short title"):
            return reply("Billing refunds")
        if "Summary to shorten:\n" in content:
            self.condenses.append(content)
            limit = int(re.search(r"at most ([\d,]+) UTF-8 bytes", system).group(1).replace(",", ""))
            kept, size = [], 0
            for line in content.split("Summary to shorten:\n", 1)[1].split("\n"):
                if size + len(line.encode()) + 1 > limit:
                    break
                kept.append(line)
                size += len(line.encode()) + 1
            return reply("\n".join(kept))
        self.summaries.append(payload)
        facts = sorted(set(re.findall(r"FACT-[a-z]+: [^.\"\\]*\.", content)))
        return reply("\n\n".join(f"## {name}\n" + ("\n".join(facts) if index == 2 else "None.")
                                 for index, name in enumerate(SUMMARY_SECTIONS)))

    def text(self, value="Done.", finish="stop"):
        self.steps.append(sse({"choices": [{"delta": {"content": value}, "finish_reason": finish}]}))

    def call(self, call_id, name, arguments):
        self.steps.append(sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                                                       "function": {"name": name, "arguments": json.dumps(arguments)}}]},
                                            "finish_reason": "tool_calls"}]}))


def sse(chunk):
    return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")


def reply(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                                     "usage": {"prompt_tokens": 20_000, "completion_tokens": 900}})


def fragment(payload):
    return payload["messages"][1]["content"].split("\n\nTranscript fragment:\n", 1)[1]


def compactions(session):
    return [event for event in session["events"] if event["type"] == "notice" and event.get("compaction")]


async def test_compaction_keeps_what_the_work_needs(workspace, monkeypatch):
    manager, session, project, tasks = workspace
    model = Model()
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(model)))
    middle = "MIDDLE-OF-THE-NEW-MODULE"
    content = "".join(f"# filler line {n:05d} for the new module\n" for n in range(250)) + f"# {middle}\n" + "".join(
        f"# filler line {n:05d} for the new module\n" for n in range(250, 500))

    # Turn 0: the goal, then real reads, an edit, a failing command and a large write.
    model.call("r1", "read_file", {"path": "src/billing/db.py"})
    model.call("r2", "read_file", {"path": "src/billing/api.py"})
    model.call("e1", "edit_file", {"path": "src/billing/api.py", "old_text": "def handler_0(request):",
                                   "new_text": "def handler_0(request):  # refunds"})
    model.call("c1", "run_command", {"command": f"{sys.executable} tools/fail.py"})
    model.call("w1", "write_file", {"path": "src/billing/new.py", "content": content})
    model.text("Read the files, edited api.py, ran the tests and wrote new.py.")
    await manager.run(session, GOAL)
    # Turn 1: an output cutoff and the app's automatic continuation.
    model.text("Partial answer", finish="length")
    model.text("Continued in smaller steps.")
    await manager.run(session, "Explain the refund flow.")
    assert any(message.get("content") == OUTPUT_LIMIT_CONTINUATION for message in session["wire"])
    # A failed Verify run and a declined one with the user's note (the app's own messages).
    session["wire"] += [
        {"role": "user", "content": f"{VERIFY_CONTINUATION_PREFIX} the project's verify command `pytest` failed "
                                    "(failed, exit 1); fix round 1 of 2. The end of its output follows as data:\n```\n"
                                    "1 failed\n```\nFind and fix the cause, then reply."},
        {"role": "assistant", "content": "Fixed the rounding."},
        {"role": "user", "content": f"{VERIFY_CONTINUATION_PREFIX} the user declined the project's verify command "
                                    f"`pytest`, so it did not run. Their note: {NOTE}\nAnswer the note."},
        {"role": "assistant", "content": "Understood."}]
    manager.store.save(session)
    # Filler turns until about 40; the decision is planted at turn 20.
    first, turn = None, 2
    while turn < 40:
        text = (DECISION + " " if turn == 20 else "") + f"Turn {turn}: review the next part. " + "n" * 900
        model.text(f"Reviewed part {turn}.")
        await manager.run(session, text)
        if first is None and compactions(session):
            first = turn
        turn += 1
    assert first is not None and first > 20
    notices = compactions(session)
    first_compaction = notices[0]["compaction"]
    decision_at = next(index for index, message in enumerate(session["wire"])
                       if DECISION in str(message.get("content")))
    assert first_compaction["summarized_messages"] > decision_at  # Every planted item was summarized.

    # (a) The summarizer saw every fact, the failure line and the outline of db.py's middle.
    shown = "\n".join(payload["messages"][1]["content"] for payload in model.summaries)
    for fact in FACTS:
        assert fact in shown
    assert FAILED in shown and "200: class BillingRepository:" in shown
    # (b) Large contents were shortened: the transcript is much smaller than the history.
    assert middle not in shown
    assert "The app shortened this tool result" in shown and "The app omitted this read_file result" in shown
    assert "bytes of content before summarizing; the file on disk has the current content." in shown
    first_requests = model.summaries[:first_compaction["requests"]]
    archived = json.dumps(model_history(session["wire"])[:first_compaction["summarized_messages"]], ensure_ascii=False)
    assert sum(len(fragment(payload)) for payload in first_requests) < 0.7 * len(archived)
    # (c) The app's prompts are labelled; the user's Verify note stays the user's words.
    assert '"role": "app"' in shown and "Automatic retry by the app after an output cutoff" in shown
    assert json.dumps({"role": "user", "content": VERIFY_NOTE_LABEL + NOTE}, ensure_ascii=False) in shown

    # (d) The first request after compaction carries the facts and the app record.
    after = next(payload for count, payload in model.streamed if count > 0)
    carried = after["messages"][1]["content"]
    for fact in FACTS:
        assert fact in carried
    assert "1. " + GOAL in carried
    assert all(f"(task_id {task['id']})" in carried for task in tasks)
    changed = carried.split("Files this conversation changed before this point (newest first):\n")[1].split("\n\n")[0]
    assert "- src/billing/api.py" in changed and "- src/billing/new.py" in changed
    forgotten = carried.split("no longer in your context (read them again before editing):\n")[1].split("\n\n")[0]
    assert "- src/billing/db.py" in forgotten
    assert "Each handoff starts with a Contents list" in carried
    info_after = next(event for event in notices if event["compaction"]["number"] == 1)["compaction"]
    assert info_after["after_tokens"] <= info_after["threshold_tokens"]
    # (e) Few requests per compaction; (f) one notice each, all automatic.
    assert all(notice["compaction"]["requests"] <= 3 for notice in notices)
    assert len(notices) == session["context_state"]["compactions"]
    assert {notice["compaction"]["trigger"] for notice in notices} == {"auto"}
    assert notices[0]["text"].startswith("Context compacted automatically (compaction 1)")

    # (g) A summarized read must be read again before a write.
    db = str((project / "src" / "billing" / "db.py").resolve())
    assert db not in session["file_views"]
    model.call("w2", "write_file", {"path": "src/billing/db.py", "content": "# rewritten\n"})
    model.call("r3", "read_file", {"path": "src/billing/db.py"})
    model.call("w3", "write_file", {"path": "src/billing/db.py", "content": "# rewritten\n"})
    model.text("Rewrote db.py.")
    await manager.run(session, "Rewrite db.py as a stub.")
    writes = [event for event in session["events"] if event.get("call_id") in ("w2", "w3")]
    assert "summarized out of your context" in writes[0]["output"] and writes[0]["state"] != "completed"
    assert writes[1]["state"] == "completed" and (project / "src" / "billing" / "db.py").read_text() == "# rewritten\n"

    # (h) The saved handoff has the redacted app record and a Contents list.
    handoff = project / session["context_state"]["handoff_files"][0]["path"]
    text = handoff.read_text()
    assert "## App record (generated by the app)" in text and "## Contents" in text
    assert TOKEN not in text and "The staging key is [REDACTED]" in text
