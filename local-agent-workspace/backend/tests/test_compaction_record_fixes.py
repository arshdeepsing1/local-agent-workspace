"""The app notes a compaction keeps next to the summary: files forgotten at earlier compactions stay
listed until read again, and the notes are fitted to their limit by whole lines (agent level)."""
import json
import re

import httpx
import pytest

from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.store import Store

from test_compaction_eval import Model, compactions, workspace  # noqa: F401  (workspace is a fixture)

MARK = "no longer in your context"
REREAD_HEADING = ("Files read before this compaction whose contents are no longer in your context (read them again "
                  "before editing):")
CHANGED_HEADING = "Files this conversation changed before this point (newest first):"
TASKS_HEADING = "Task checklist when this compaction ran (list_tasks shows the current state):"


def reread_list(content):
    if MARK not in content:
        return None
    return [line[2:] for line in content.split(MARK, 1)[1].split("\n\n")[0].split("\n") if line.startswith("- ")]


async def test_files_forgotten_at_an_earlier_compaction_stay_listed(workspace, monkeypatch):  # noqa: F811
    manager, session, project, _ = workspace
    model = Model()
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(model)))
    model.call("r1", "read_file", {"path": "src/billing/db.py"})
    model.text("Read db.py.")
    await manager.run(session, "Read src/billing/db.py and remember its structure.")
    listed = {}
    for turn in range(1, 200):
        model.text(f"Reviewed part {turn}.")
        before = len(compactions(session))
        await manager.run(session, f"Turn {turn}: review the next part. " + "n" * 4000)
        count = len(compactions(session))
        if count > before:
            listed[count] = reread_list(model.streamed[-1][1]["messages"][1]["content"])
        if count >= 3:
            break
    assert set(listed) == {1, 2, 3}
    assert all(names == ["src/billing/db.py"] for names in listed.values()), listed
    assert "db.py" not in str(session.get("file_views") or {})


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    yield manager, session, project
    store.db.close()


def remember(session, project, name):
    path = project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{name} contents\n")
    AgentManager.remember_file(session, path)
    return str(path.resolve())


def test_an_earlier_forgotten_file_is_listed_once_newest_first_until_read_again(runtime):
    manager, session, project = runtime
    older, reread, gone = (remember(session, project, name) for name in ("older.py", "reread.py", "gone.py"))
    current = remember(session, project, "current.py")
    session["file_views"] = {path: view for path, view in session["file_views"].items() if path in (reread, current)}
    (project / "gone.py").unlink()
    # Forgotten at earlier compactions (oldest first); reread.py was read again since.
    session["context_state"] = {"summary": "Earlier", "through": 2, "compactions": 1,
                                "summarized_reads": [older, gone, reread]}
    record, holder = manager.compaction_record(session, manager.session_tools(session))
    notes = record([{"role": "user", "content": "Earlier"}, {"role": "assistant", "content": "Done."},
                    {"role": "user", "content": "Now"}], 2)
    assert sorted(holder["forgotten"]) == sorted([reread, current])  # Only this compaction's: pruning input.
    assert reread_list(notes) == ["current.py", "reread.py", "older.py"]  # gone.py no longer exists.


def test_the_notes_fit_their_limit_by_whole_lines(runtime):
    manager, session, project = runtime
    board = manager.task_board
    tasks = [board.create(session["id"], title=f"Task {n}: " + "write the billing export step " * 6 + f"part {n}")
             for n in range(20)]
    deep = "src/main/java/com/example/billing/service/internal/module_number_{n}/{kind}Implementation.java"
    changed = [deep.format(n=n, kind="Changed") for n in range(30)]
    for name in [deep.format(n=n, kind="Read") for n in range(30)]:
        remember(session, project, name)
    wire = [{"role": "user", "content": "Build it"}]
    session["events"] = []
    for n, path in enumerate(changed):
        call = {"id": f"w{n}", "type": "function", "function": {"name": "write_file",
                                                                "arguments": json.dumps({"path": path})}}
        wire += [{"role": "assistant", "content": None, "tool_calls": [call]},
                 {"role": "tool", "tool_call_id": f"w{n}", "content": "ok"}]
        session["events"].append({"id": f"event-w{n}", "type": "tool", "name": "write_file", "call_id": f"w{n}",
                                  "state": "completed", "input": {"path": path}, "output": "{}"})
    wire += [{"role": "assistant", "content": "Done."}, {"role": "user", "content": "Next"}]
    record, _ = manager.compaction_record(session, manager.session_tools(session))
    full = record(wire, len(wire) - 1)
    assert len(full.encode()) > 8_000  # Without a limit nothing is left out.
    notes = record(wire, len(wire) - 1, 4_000)
    assert len(notes.encode()) <= 4_000
    for heading in (TASKS_HEADING, CHANGED_HEADING, REREAD_HEADING):
        assert notes.count(heading) == 1
    ids = re.findall(r"\(task_id ([^)]*)\)", notes)
    assert ids and all(task_id in {task["id"] for task in tasks} for task_id in ids)
    task_part, changed_part, reread_part = notes.split("\n\n")
    assert re.search(r"\n- … \d+ more open tasks \(list_tasks shows them\)\nand 0 completed tasks\.$", task_part)
    assert re.search(r"\n- … \d+ more$", changed_part)
    assert re.search(r"\n- … \d+ more \(read any file again before editing it\)$", reread_part)
    known = set(changed) | {deep.format(n=n, kind="Read") for n in range(30)}
    paths = [line[2:] for line in notes.split("\n") if line.startswith("- src/")]
    assert len(paths) > 10 and all(path in known for path in paths)
