import copy
import json
from datetime import datetime

import httpx
import pytest

from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.context import (
    HANDOFF_MAX_TOKENS, HANDOFF_POINTER_FILES, SAFETY_MARGIN, SUMMARY_MAX_TOKENS, SUMMARY_PREFIX,
    build_handoff_messages, context_messages, estimate_tokens, handoff_output_tokens, handoff_pointer,
    prepare_context, summary_byte_limit,
)
from local_agent.store import Store


SYSTEM = {"role": "system", "content": "Follow the user's current request."}
TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]
WINDOW = 65536
HANDOFF_TOKENS = handoff_output_tokens(WINDOW)
LIMIT = summary_byte_limit(WINDOW - 8192 - SAFETY_MARGIN)
DOC = "## Current status\nMigration verified.\n\n## Work done and decisions\n" + "Detail with paths. " * 1200


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


def long_history():
    return [message for i in range(4) for message in (
        user(f"Earlier request {i}: " + "x" * 45000), assistant(f"Completed step {i}."))] + [user("Latest request")]


# --- Context preparation --------------------------------------------------------

def test_handoff_output_reserve_scales_with_the_context_window():
    assert handoff_output_tokens(16384) == SUMMARY_MAX_TOKENS
    assert handoff_output_tokens(65536) == 8192
    assert handoff_output_tokens(131000) == HANDOFF_MAX_TOKENS == 16000


def test_pointer_lists_newest_files_first_and_only_with_a_summary():
    files = [{"path": f"handoffs/auto/h-{n}.md", "compaction": n} for n in range(1, 8)]
    pointer = handoff_pointer(files)
    listed = [line for line in pointer.splitlines() if line.startswith("- ")]
    assert listed[0] == "- handoffs/auto/h-7.md (compaction 7)"
    assert len(listed) == HANDOFF_POINTER_FILES and "h-2.md" not in pointer
    assert handoff_pointer([]) == handoff_pointer(None) == handoff_pointer([{"compaction": 1}]) == ""
    wire = [user("Current")]
    assert context_messages(wire, {"summary": "", "handoff_files": files}) == wire
    assert context_messages(wire, {"summary": "Earlier", "handoff_files": files[:1]})[0] == user(
        SUMMARY_PREFIX + "Earlier" + handoff_pointer(files[:1]))


async def test_compaction_requests_a_handoff_saves_it_and_points_to_it():
    requests, saved, condensed = [], [], []

    async def summarize(previous, chunk, limit_bytes):
        request = build_handoff_messages(previous, chunk, "", HANDOFF_TOKENS)
        assert estimate_tokens(request) <= WINDOW - HANDOFF_TOKENS - SAFETY_MARGIN
        requests.append(chunk)
        return DOC

    async def condense(summary, limit_bytes):
        condensed.append(summary)
        return "Migration verified; details in the handoff."

    async def save(documents, compaction):
        saved.append((documents, compaction))
        return True

    path = "handoffs/auto/2026-09-26-migration-compaction-3.md"
    state = {"summary": "Prior", "through": 0, "compactions": 2,
             "handoff_files": [{"path": "handoffs/auto/old.md", "compaction": 2}]}
    wire = long_history()
    original = copy.deepcopy((wire, state))
    messages, updated, info = await prepare_context(
        wire, state, SYSTEM, TOOLS, WINDOW, summarize, condense=condense, handoff_path=path, save_handoff=save)
    assert saved == [([DOC.strip()] * len(requests), 3)]
    assert condensed and updated["summary"] == "Migration verified; details in the handoff."
    assert updated["handoff_files"] == [{"path": "handoffs/auto/old.md", "compaction": 2},
                                        {"path": path, "compaction": 3}]
    assert messages[1]["content"].index(path) < messages[1]["content"].index("handoffs/auto/old.md")
    assert info["handoff_path"] == path and info["estimated_tokens"] <= info["input_budget"]
    assert (wire, state) == original


async def test_failed_save_drops_the_pointer_but_keeps_the_compaction():
    async def summarize(previous, chunk, limit_bytes):
        return "Short handoff."

    async def save(documents, compaction):
        return False

    messages, updated, info = await prepare_context(
        long_history(), {}, SYSTEM, TOOLS, WINDOW, summarize, handoff_path="handoffs/auto/h.md", save_handoff=save)
    assert updated["compactions"] == 1 and updated["summary"] == "Short handoff."
    assert "handoff_files" not in updated and "handoff_path" not in info
    assert "handoffs/auto" not in messages[1]["content"]


async def test_without_a_handoff_path_compaction_is_a_plain_summary():
    async def summarize(previous, chunk, limit_bytes):
        return "Plain summary."

    messages, updated, info = await prepare_context(long_history(), {}, SYSTEM, TOOLS, WINDOW, summarize)
    assert "handoff_files" not in updated and "handoff_path" not in info
    assert messages[1]["content"] == SUMMARY_PREFIX + "Plain summary."


# --- Agent integration ------------------------------------------------------------

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="", context_window=WINDOW)
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session["title"] = "Run the nightly job locally"
    session["wire"] = long_history()[:-1]
    session["events"] = [{"id": "c1", "type": "tool", "name": "run_command", "created": 1790238001.0,
                          "state": "completed", "input": {"command": "podman ps -a"},
                          "output": json.dumps({"job_id": "j1", "state": "completed", "exit_code": 0})}]
    store.save(session)
    yield manager, session, project
    store.db.close()


def mock_gateway(monkeypatch, gateway):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(gateway)))


def stream(text="Done."):
    chunk = {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}
    return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")


def reply(text, finish_reason="stop"):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": finish_reason}],
                                     "usage": {"prompt_tokens": 50000, "completion_tokens": 6000}})


def gateway_for(requests, handoff=DOC, finish_reason="stop"):
    async def gateway(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["stream"]:
            return stream()
        if payload["messages"][0]["content"].startswith("Write a detailed handoff document"):
            return reply(handoff, finish_reason)
        if "Summary to shorten" in payload["messages"][1]["content"]:
            return reply("Condensed: steps 0-3 done; see the handoff.")
        return reply("Plain summary of steps 0-3.")
    return gateway


def expected_name(n=1):
    return f"{datetime.now():%Y-%m-%d}-run-the-nightly-job-locally-compaction-{n}.md"


async def test_automatic_compaction_saves_a_handoff_and_points_the_model_to_it(runtime, monkeypatch):
    manager, session, project = runtime
    requests = []
    mock_gateway(monkeypatch, gateway_for(requests))
    await manager.run(session, "Continue")

    handoff = project / "handoffs" / "auto" / expected_name()
    text = handoff.read_text()
    assert text.startswith("# Handoff: Run the nightly job locally (compaction 1)")
    assert DOC.strip() in text and "## Activity log (generated by the app)" in text and "podman ps -a" in text
    handoff_requests = [r for r in requests if not r["stream"] and r["max_tokens"] == HANDOFF_TOKENS]
    assert handoff_requests and all("Summary to shorten" not in r["messages"][1]["content"] for r in handoff_requests)
    assert any("Summary to shorten" in r["messages"][1]["content"] for r in requests if not r["stream"])
    agent = next(r for r in requests if r["stream"])
    assert f"handoffs/auto/{expected_name()}" in agent["messages"][1]["content"]
    assert session["context_state"]["handoff_files"] == [{"path": f"handoffs/auto/{expected_name()}", "compaction": 1}]
    notices = [e["text"] for e in session["events"] if e["type"] == "notice"]
    assert any("Saved a detailed handoff" in text for text in notices)
    assert not any("condensed it" in text for text in notices)
    compactions = [c for c in session["inference_calls"] if c["purpose"] == "compaction"]
    assert {c["max_output_tokens"] for c in compactions} == {HANDOFF_TOKENS, SUMMARY_MAX_TOKENS}


async def test_disabled_setting_keeps_plain_summaries_and_writes_no_file(runtime, monkeypatch):
    manager, session, project = runtime
    manager.settings.values["compaction_handoffs"] = False
    requests = []
    mock_gateway(monkeypatch, gateway_for(requests))
    await manager.run(session, "Continue")
    assert session["context_state"]["summary"] == "Plain summary of steps 0-3."
    assert not (project / "handoffs").exists() and "handoff_files" not in session["context_state"]
    assert {r["max_tokens"] for r in requests if not r["stream"]} == {SUMMARY_MAX_TOKENS}


async def test_existing_file_gets_a_unique_name_and_a_cut_off_handoff_is_kept(runtime, monkeypatch):
    manager, session, project = runtime
    folder = project / "handoffs" / "auto"
    folder.mkdir(parents=True)
    (folder / expected_name()).write_text("someone else's handoff")
    mock_gateway(monkeypatch, gateway_for([], handoff="Partial handoff text", finish_reason="length"))
    await manager.run(session, "Continue")
    assert (folder / expected_name()).read_text() == "someone else's handoff"
    unique = folder / expected_name().replace(".md", f"-{session['id'][:8]}.md")
    assert "Partial handoff text\n\n[This handoff was cut off at the output limit.]" in unique.read_text()
    assert session["context_state"]["compactions"] == 1
    assert not [e for e in session["events"] if e["type"] == "error"]


@pytest.mark.parametrize("blocker", ["file", "symlink"])
async def test_unsavable_handoff_warns_and_compaction_continues(runtime, monkeypatch, tmp_path, blocker):
    manager, session, project = runtime
    outside = tmp_path / "outside"
    outside.mkdir()
    if blocker == "file":
        (project / "handoffs").write_text("not a folder")
    else:
        (project / "handoffs").symlink_to(outside, target_is_directory=True)
    requests = []
    mock_gateway(monkeypatch, gateway_for(requests))
    await manager.run(session, "Continue")
    assert session["context_state"]["compactions"] == 1
    assert "handoff_files" not in session["context_state"]
    assert not list(outside.rglob("*"))
    assert any("Could not save the compaction handoff" in e["text"] for e in session["events"] if e["type"] == "notice")
    agent = next(r for r in requests if r["stream"])
    assert "handoffs/auto" not in agent["messages"][1]["content"]


async def test_manual_compaction_uses_the_preservation_note_and_numbers_files(runtime, monkeypatch):
    manager, session, project = runtime
    session["context_state"] = {"summary": "Earlier", "through": 0, "compactions": 1,
                                "handoff_files": [{"path": "handoffs/auto/first.md", "compaction": 1}]}
    session["wire"].append(user("What is next?"))
    manager.store.save(session)
    requests = []
    mock_gateway(monkeypatch, gateway_for(requests))
    manager.start_compaction(session["id"], "Keep the node-pin decision")
    await manager.tasks[session["id"]]
    saved = manager.store.get(session["id"])
    assert (project / "handoffs" / "auto" / expected_name(2)).exists()
    assert all("Keep the node-pin decision" in r["messages"][1]["content"] for r in requests)
    assert [item["compaction"] for item in saved["context_state"]["handoff_files"]] == [1, 2]
    restarted = AgentManager(manager.store, manager.settings).get(session["id"])
    assert restarted["context_state"]["handoff_files"] == saved["context_state"]["handoff_files"]


async def test_very_long_handoff_notes_the_omitted_activity_log(runtime, monkeypatch):
    manager, session, project = runtime
    mock_gateway(monkeypatch, gateway_for([], handoff="Long detail. " * 7000))
    await manager.run(session, "Continue")
    text = (project / "handoffs" / "auto" / expected_name()).read_text()
    assert "Activity log omitted: this handoff already fills the 80 KB file limit" in text
    assert "## Activity log (generated by the app)" not in text
