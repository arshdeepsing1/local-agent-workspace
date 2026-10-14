"""Read before overwriting, and telling the model which files changed since it last saw them."""
import json
import os

import httpx
import pytest

from local_agent import agents
from local_agent.activity import ACTIVITY_MARKER
from local_agent.agents import AgentManager, public_session
from local_agent.config import Settings
from local_agent.instructions import load_project_instructions
from local_agent.store import Store
from local_agent.tools import WorkspaceTools

NOT_READ = "Read note.txt before overwriting it: this conversation has not read it yet."
CHANGED = "Read note.txt again before overwriting it: it changed since you last read or wrote it"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / "note.txt").write_text("original\n")
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "views.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session["permission_mode"] = "acceptEdits"
    tools = WorkspaceTools(str(project))
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance["text"], guidance["warnings"])
    tools.skill_signature = ""
    yield manager, session, project, tools
    store.db.close()


async def read(manager, session, tools, path="note.txt"):
    return await manager.execute_tool(session, tools, "read_file", {"path": path}, f"read-{path}")


async def write(manager, session, tools, content, path="note.txt"):
    return await manager.execute_tool(session, tools, "write_file", {"path": path, "content": content}, f"write-{path}")


def event(session, call_id):
    return next(item for item in reversed(session["events"]) if item.get("call_id") == call_id)


async def test_a_read_summarized_at_compaction_must_be_read_again(runtime):
    manager, session, project, tools = runtime
    await read(manager, session, tools)
    target = str((project / "note.txt").resolve())
    session["file_views"], session["context_state"] = agents.forget_summarized_reads(
        session["file_views"], {"summary": "s", "through": 2, "compactions": 1}, [target])
    output = await write(manager, session, tools, "new\n")
    assert "Read note.txt again before overwriting it: its earlier contents were summarized out of your context" in output
    assert (project / "note.txt").read_text() == "original\n"
    await read(manager, session, tools)
    await write(manager, session, tools, "new\n")
    assert (project / "note.txt").read_text() == "new\n"


async def test_overwriting_is_refused_before_a_read_without_asking_the_user(runtime):
    manager, session, project, tools = runtime
    session["permission_mode"] = "manual"
    asked = []

    async def approve(session, event):
        asked.append(event["name"])
        return True
    manager.approve = approve

    output = await write(manager, session, tools, "replaced\n")
    assert output.startswith(NOT_READ) and "Use read_file, then write again." in output
    assert (project / "note.txt").read_text() == "original\n"
    assert event(session, "write-note.txt")["state"] == "rejected"
    assert asked == [] and not manager.checkpoints.list(session["id"])


async def test_overwriting_is_allowed_after_a_read(runtime):
    manager, session, project, tools = runtime
    await read(manager, session, tools)
    output = await write(manager, session, tools, "replaced\n")
    assert "+replaced" in output
    assert (project / "note.txt").read_text() == "replaced\n"
    info = os.stat(project / "note.txt")
    assert session["file_views"] == {str(project / "note.txt"): [info.st_size, info.st_mtime_ns]}
    # The conversation wrote it last, so another write needs no new read.
    assert "+again" in await write(manager, session, tools, "again\n")


async def test_overwriting_is_refused_after_an_outside_change_until_read_again(runtime):
    manager, session, project, tools = runtime
    await read(manager, session, tools)
    (project / "note.txt").write_text("the user's own edit\n")
    output = await write(manager, session, tools, "replaced\n")
    assert output.startswith(CHANGED) and "the user edited it, undid an agent change, or a command changed it" in output
    assert (project / "note.txt").read_text() == "the user's own edit\n"
    await read(manager, session, tools)
    assert "+replaced" in await write(manager, session, tools, "replaced\n")


async def test_overwriting_is_refused_after_an_undo_in_the_review(runtime):
    manager, session, project, tools = runtime
    await read(manager, session, tools)
    await write(manager, session, tools, "agent version, longer\n")
    current = manager.changes.list(session["id"], tools)["files"][0]["current_hash"]
    manager.changes.act(session["id"], tools, "undo", [{"path": "note.txt", "current_hash": current}])
    assert (project / "note.txt").read_text() == "original\n"

    output = await write(manager, session, tools, "agent version, longer\n")
    assert output.startswith(CHANGED)
    assert (project / "note.txt").read_text() == "original\n"


async def test_edit_file_needs_no_read_and_records_the_result(runtime):
    manager, session, _, tools = runtime
    output = await manager.execute_tool(session, tools, "edit_file",
                                        {"path": "note.txt", "old_text": "original", "new_text": "edited"}, "edit")
    assert "+edited" in output
    assert "+rewritten" in await write(manager, session, tools, "rewritten\n")


async def test_new_files_and_the_activity_log_need_no_read(runtime):
    manager, session, project, tools = runtime
    assert "+fresh" in await write(manager, session, tools, "fresh\n", path="new/fresh.txt")
    handoff = project / "handoff.md"
    handoff.write_text(f"# Handoff\n{ACTIVITY_MARKER}\n")
    output = await manager.execute_tool(session, tools, "insert_activity_log", {"path": "handoff.md"}, "log")
    assert json.loads(output)["placement"] == "marker"
    assert ACTIVITY_MARKER not in handoff.read_text()
    assert str(handoff) in session["file_views"]


async def test_a_change_during_the_approval_wait_is_refused(runtime):
    manager, session, project, tools = runtime
    session["permission_mode"] = "manual"
    await read(manager, session, tools)

    async def approve(session, event):
        # Same content, but the file was rewritten while the card was open.
        os.utime(project / "note.txt", ns=(1, 1))
        return True
    manager.approve = approve
    output = await write(manager, session, tools, "replaced\n")
    assert CHANGED in output
    assert event(session, "write-note.txt")["state"] == "error"
    assert (project / "note.txt").read_text() == "original\n"


def test_refusals_name_the_action_and_forgetting_drops_a_view(runtime):
    manager, session, project, tools = runtime
    retries = {"overwriting": "Use read_file, then write again.", "deleting": "Use read_file, then delete it again.",
               "editing": "Use notebook_read, then edit it again."}
    for verb, retry in retries.items():
        refusal = manager.overwrite_refusal(session, tools, "note.txt", verb)
        assert refusal == f"Read note.txt before {verb} it: this conversation has not read it yet. {retry}"
    manager.remember_file(session, project / "note.txt")
    assert manager.overwrite_refusal(session, tools, "note.txt", "deleting") is None
    os.utime(project / "note.txt", ns=(1, 1))
    refusal = manager.overwrite_refusal(session, tools, "note.txt", "editing")
    assert refusal.startswith("Read note.txt again before editing it: it changed since you last read or wrote it")
    assert refusal.endswith("Use notebook_read, then edit it again.")
    manager.forget_file(session, project / "note.txt")
    assert session["file_views"] == {}


def test_views_are_bounded_and_private(runtime, monkeypatch):
    manager, session, project, _ = runtime
    monkeypatch.setattr(agents, "FILE_VIEW_LIMIT", 3)
    for index in range(5):
        path = project / f"f{index}.txt"
        path.write_text("x")
        manager.remember_file(session, path)
    manager.remember_file(session, project / "f2.txt")  # refreshed entries become the most recent
    assert list(session["file_views"]) == [str(project / name) for name in ("f3.txt", "f4.txt", "f2.txt")]

    manager.store.save(session)
    assert manager.store.get(session["id"])["file_views"] == session["file_views"]
    assert "file_views" not in public_session(session)
    assert "file_views" not in manager.store.list()[0]
    with (manager.store.conversations / f"{session['id']}.jsonl").open() as source:
        assert "file_views" not in source.readline()  # not in the small header read for listing


def test_notice_names_changed_and_deleted_files_and_forgets_deleted_ones(runtime):
    manager, session, project, tools = runtime
    for index in range(13):
        (project / f"f{index:02d}.txt").write_text("x")
        manager.remember_file(session, project / f"f{index:02d}.txt")
    assert manager.changed_files_notice(session, tools) == ""
    for index in range(12):
        (project / f"f{index:02d}.txt").write_text("changed")
    (project / "f11.txt").unlink()

    notice = manager.changed_files_notice(session, tools)
    assert notice == ("Files changed since you last read or wrote them (by the user, an undo, or a command): "
                      "f11.txt, f10.txt, f09.txt, f08.txt, f07.txt, f06.txt, f05.txt, f04.txt, f03.txt, f02.txt "
                      "and 2 more. Read a file again before editing it.")
    assert str(project / "f11.txt") not in session["file_views"]
    assert "f11.txt" not in manager.changed_files_notice(session, tools)


def stream(text="Done.", call=None):
    delta = {"tool_calls": [{"index": 0, **call}]} if call else {"content": text}
    chunk = {"choices": [{"delta": delta, "finish_reason": "tool_calls" if call else "stop"}]}
    return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")


def mock_gateway(monkeypatch, gateway):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(gateway)))


async def test_the_model_is_told_at_the_end_of_the_system_message(runtime, monkeypatch):
    manager, session, project, _ = runtime
    (project / "AGENTS.md").write_text("Project rule: keep tests green.\n")
    session["allowed_directories"] = [str(project.parent / "extra")]
    requests = []

    async def gateway(request):
        payload = json.loads(request.content)
        if "tools" not in payload:
            return stream("Note title")  # the conversation title request
        requests.append(payload)
        if len(requests) == 1:
            return stream(call={"id": "r1", "type": "function",
                                "function": {"name": "read_file", "arguments": json.dumps({"path": "note.txt"})}})
        return stream()

    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Read the note")
    assert "Files changed since" not in requests[-1]["messages"][0]["content"]

    (project / "note.txt").write_text("changed by the user\n")
    await manager.run(session, "Continue")
    system = requests[-1]["messages"][0]["content"]
    assert system.endswith("\nFiles changed since you last read or wrote them (by the user, an undo, or a command): "
                           "note.txt. Read a file again before editing it.")
    # Stable text first; the folder list, time and notice last.
    assert (system.index("Workspace: ") < system.index("Each of your responses can contain")
            < system.index("Project rule: keep tests green.") < system.index("Additional allowed folders: ")
            < system.index("Current local date: ") < system.index("Files changed since"))
    assert "cache_control" not in json.dumps(requests[-1])
