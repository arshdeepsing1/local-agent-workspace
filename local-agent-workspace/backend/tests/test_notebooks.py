"""notebook_read and notebook_edit: paging, cell edits that keep the file's format, limits and guards."""
import asyncio
import difflib
import json
import os

import pytest

from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.instructions import load_project_instructions
from local_agent.notebooks import plan_edit, preview, read_notebook
from local_agent.store import Store
from local_agent.tools import WorkspaceTools


def notebook(cells, minor=5, ids=True):
    result = {"cells": [], "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                                        "language_info": {"name": "python"}},
              "nbformat": 4, "nbformat_minor": minor}
    for number, (cell_type, source, outputs) in enumerate(cells):
        cell = {"cell_type": cell_type, "metadata": {}, "source": source.splitlines(True)}
        if ids:
            cell["id"] = f"cell{number:04d}"
        if cell_type == "code":
            cell.update(execution_count=number + 1 if outputs else None, outputs=outputs)
        result["cells"].append(cell)
    return result


def jupyter(value, indent=1):
    """The text Jupyter writes."""
    return json.dumps(value, indent=indent, sort_keys=True, ensure_ascii=False) + "\n"


STREAM = {"name": "stdout", "output_type": "stream", "text": ["hello\n"]}
IMAGE = {"data": {"image/png": "iVBORw0KGgo=", "text/plain": ["<Figure>"]}, "metadata": {}, "output_type": "display_data"}
SAMPLE = notebook([("markdown", "# Title\n", []), ("code", "print('hello')\nplot()\n", [STREAM, IMAGE]),
                   ("code", "x = 1\n", [])])


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / "analysis.ipynb").write_text(jupyter(SAMPLE))
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    store = Store(settings.state_dir / "notebooks.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session["permission_mode"] = "acceptEdits"
    tools = manager.session_tools(session)
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance["text"], guidance["warnings"])
    tools.skill_signature = ""
    cards = []

    async def approve(session, event):
        cards.append(event)
        return True
    manager.approve = approve
    yield manager, session, project, tools, cards
    store.db.close()


async def tool(runtime, name, arguments, call_id="call"):
    manager, session, _, tools, _ = runtime
    output = await manager.execute_tool(session, tools, name, arguments, call_id)
    return output, next(item for item in reversed(session["events"]) if item.get("call_id") == call_id)


# --- Reading -----------------------------------------------------------------------------

def test_read_lists_cells_with_ids_sources_and_output_summaries(tmp_path):
    (tmp_path / "n.ipynb").write_text(jupyter(SAMPLE))
    result = read_notebook(WorkspaceTools(str(tmp_path)), "n.ipynb")
    assert {key: result[key] for key in ("path", "nbformat", "language", "total_cells", "next_offset")} == {
        "path": "n.ipynb", "nbformat": "4.5", "language": "python", "total_cells": 3, "next_offset": None}
    assert result["cells"][0] == {"index": 0, "id": "cell0000", "cell_type": "markdown", "source": "# Title\n"}
    assert result["cells"][1] == {"index": 1, "id": "cell0001", "cell_type": "code", "source": "print('hello')\nplot()\n",
                                  "execution_count": 2, "outputs": "2 outputs: stream, display_data (image/png)"}
    assert result["cells"][2]["outputs"] == ""


def test_read_pages_by_cell_count_and_by_size(tmp_path):
    tools = WorkspaceTools(str(tmp_path))
    (tmp_path / "many.ipynb").write_text(jupyter(notebook([("code", f"x = {n}\n", []) for n in range(30)])))
    first = read_notebook(tools, "many.ipynb")
    assert len(first["cells"]) == 20 and first["next_offset"] == 20
    rest = read_notebook(tools, "many.ipynb", offset=20, max_cells=100)
    assert [cell["index"] for cell in rest["cells"]] == list(range(20, 30)) and rest["next_offset"] is None

    (tmp_path / "big.ipynb").write_text(jupyter(notebook([("code", "y = 1\n" * 2_000, []) for _ in range(5)])))
    page = read_notebook(tools, "big.ipynb", max_cells=100)
    assert len(json.dumps(page, indent=2).encode()) <= 8_000
    assert page["cells"][0]["source_truncated"] is True and len(page["cells"][0]["source"]) <= 4_000
    assert page["next_offset"] == len(page["cells"])


@pytest.mark.parametrize("text", ["not json", "[1, 2]", '{"cells": "none"}'])
def test_read_refuses_files_that_are_not_notebooks(tmp_path, text):
    (tmp_path / "bad.ipynb").write_text(text)
    with pytest.raises(ValueError, match="^Not a Jupyter notebook: "):
        read_notebook(WorkspaceTools(str(tmp_path)), "bad.ipynb")


# --- Planning edits ---------------------------------------------------------------------------

def cells_of(text):
    return json.loads(text)["cells"]


def test_replace_clears_outputs_of_a_changed_code_cell_and_shows_one_cell():
    before = jupyter(SAMPLE)
    after, summary = plan_edit(before, {"cell_id": "cell0001", "source": "print('bye')\nplot()"})
    cell = cells_of(after)[1]
    assert cell["source"] == ["print('bye')\n", "plot()"]
    assert cell["outputs"] == [] and cell["execution_count"] is None
    text = preview("analysis.ipynb", summary)
    assert text.startswith("Notebook analysis.ipynb: replace cell 1 (code, id cell0001)\n")
    assert "-print('hello')\n+print('bye')" in text and text.endswith("Its outputs will be cleared.")
    # Other cells and metadata are untouched.
    assert cells_of(after)[0] == cells_of(before)[0] and json.loads(after)["metadata"] == SAMPLE["metadata"]


def test_changing_a_cell_type_adds_or_removes_code_fields():
    after, _ = plan_edit(jupyter(SAMPLE), {"index": 0, "source": "import os\n", "cell_type": "code"})
    assert cells_of(after)[0]["outputs"] == [] and cells_of(after)[0]["execution_count"] is None
    after, summary = plan_edit(jupyter(SAMPLE), {"index": 1, "source": "Some *text*\n", "cell_type": "markdown"})
    assert "outputs" not in cells_of(after)[1] and "execution_count" not in cells_of(after)[1]
    assert summary["cleared"]


def test_insert_creates_ids_where_the_format_has_them():
    after, summary = plan_edit(jupyter(SAMPLE), {"edit_mode": "insert", "cell_id": "cell0000", "source": "a = 2\n"})
    new = cells_of(after)[1]
    assert new["cell_type"] == "code" and new["source"] == ["a = 2\n"] and new["outputs"] == []
    assert len(new["id"]) == 8 and int(new["id"], 16) >= 0 and summary["index"] == 1
    old_format = notebook([("code", "x\n", [])], minor=4, ids=False)
    after, _ = plan_edit(jupyter(old_format), {"edit_mode": "insert", "index": 1, "source": "y\n", "cell_type": "markdown"})
    assert "id" not in cells_of(after)[1] and cells_of(after)[1]["cell_type"] == "markdown"
    after, _ = plan_edit(jupyter(SAMPLE), {"edit_mode": "insert", "source": "z\n"})
    assert cells_of(after)[-1]["source"] == ["z\n"] and len(cells_of(after)) == 4


def test_delete_and_clear_outputs():
    after, summary = plan_edit(jupyter(SAMPLE), {"edit_mode": "delete", "index": 2})
    assert [cell["id"] for cell in cells_of(after)] == ["cell0000", "cell0001"]
    assert preview("n.ipynb", summary).startswith("Notebook n.ipynb: delete cell 2 (code, id cell0002)\n")
    after, summary = plan_edit(jupyter(SAMPLE), {"edit_mode": "clear_outputs", "cell_id": "cell0001"})
    assert cells_of(after)[1]["outputs"] == [] and cells_of(after)[1]["source"] == SAMPLE["cells"][1]["source"]
    assert preview("n.ipynb", summary).endswith("Its outputs will be cleared.")
    after, summary = plan_edit(jupyter(SAMPLE), {"edit_mode": "clear_outputs"})
    assert all(cell.get("outputs", []) == [] for cell in cells_of(after))
    assert preview("n.ipynb", summary) == "Notebook n.ipynb: clear outputs of all 3 cells"


@pytest.mark.parametrize("indent, newline", [(1, True), (2, False), (4, True)])
def test_a_no_op_edit_is_byte_identical(indent, newline):
    text = json.dumps(SAMPLE, indent=indent, sort_keys=True, ensure_ascii=False) + ("\n" if newline else "")
    after, _ = plan_edit(text, {"cell_id": "cell0001", "source": "print('hello')\nplot()\n"})
    assert after == text
    # Unicode stays as written.
    unicode = jupyter(notebook([("markdown", "Grüße, 世界\n", [])]))
    assert plan_edit(unicode, {"index": 0, "source": "Grüße, 世界\n"})[0] == unicode
    # Another format is rewritten the way Jupyter writes notebooks.
    compact = json.dumps(SAMPLE)
    assert plan_edit(compact, {"index": 0, "source": "# Title\n"})[0] == jupyter(SAMPLE)


@pytest.mark.parametrize("arguments, message", [
    ({"cell_id": "nope", "source": "x"}, "Cell id 'nope' was not found; call notebook_read to see cell ids."),
    ({"index": 3, "source": "x"}, "index 3 is out of range (the notebook has 3 cells)."),
    ({"index": 0, "cell_id": "cell0000", "source": "x"}, "Give either cell_id or index, not both."),
    ({"index": 0}, "replace needs source."),
    ({"edit_mode": "insert", "index": 0}, "insert needs source."),
])
def test_edit_errors(arguments, message):
    with pytest.raises(ValueError) as error:
        plan_edit(jupyter(SAMPLE), arguments)
    assert str(error.value) == message


# --- The agent tools ----------------------------------------------------------------------------

async def test_edit_needs_a_current_read_and_makes_one_checkpoint_with_review_undo(runtime):
    manager, session, project, tools, cards = runtime
    path = project / "analysis.ipynb"
    original = path.read_bytes()
    output, event = await tool(runtime, "notebook_edit", {"path": "analysis.ipynb", "index": 2, "source": "x = 2\n"}, "unread")
    assert event["state"] == "rejected" and output.startswith("Read analysis.ipynb before editing it")
    assert output.endswith("Use notebook_read, then edit it again.")

    output, event = await tool(runtime, "notebook_read", {"path": "analysis.ipynb"}, "read")
    assert event["state"] == "completed" and json.loads(output)["total_cells"] == 3
    path.write_text(path.read_text() + " ")  # Changed after the read.
    output, event = await tool(runtime, "notebook_edit", {"path": "analysis.ipynb", "index": 2, "source": "x = 2\n"}, "stale")
    assert event["state"] == "rejected" and output.startswith("Read analysis.ipynb again before editing it")
    path.write_bytes(original)

    await tool(runtime, "notebook_read", {"path": "analysis.ipynb"}, "read-again")
    await manager.event(session, "user", text="Edit the notebook")
    output, event = await tool(runtime, "notebook_edit", {"path": "analysis.ipynb", "cell_id": "cell0001", "source": "print(1)\n"})
    assert event["state"] == "completed", output
    assert output.startswith("Notebook analysis.ipynb: replace cell 1 (code, id cell0001)")
    assert cards == []  # Accept edits: no card.
    assert len(manager.checkpoints.list(session["id"])) == 1
    assert cells_of(path.read_text())[1]["outputs"] == []
    # It remembers the written file, so a second edit needs no new read.
    output, event = await tool(runtime, "notebook_edit", {"path": "analysis.ipynb", "edit_mode": "delete", "index": 0}, "again")
    assert event["state"] == "completed", output
    [entry] = manager.changes.list(session["id"], tools)["files"]
    manager.changes.act(session["id"], tools, "undo", [{"path": "analysis.ipynb", "current_hash": entry["current_hash"]}])
    assert path.read_bytes() == original
    from local_agent.activity import activity_log
    from local_agent.transcript import transcript
    assert "**Edited notebook** (`notebook_edit`)" in activity_log(session)[0]
    assert "- Edited notebook `analysis.ipynb`" in transcript(session)


async def test_manual_mode_asks_with_the_cell_preview(runtime):
    manager, session, project, tools, cards = runtime
    session["permission_mode"] = "manual"
    await tool(runtime, "notebook_read", {"path": "analysis.ipynb"}, "read")
    output, event = await tool(runtime, "notebook_edit", {"path": "analysis.ipynb", "edit_mode": "clear_outputs"})
    assert event["state"] == "completed", output
    assert [card["name"] for card in cards] == ["notebook_edit"]
    assert cards[0]["preview"] == "Notebook analysis.ipynb: clear outputs of all 3 cells"


async def test_a_300_kb_notebook_is_editable_at_the_default_limit_and_1_mb_is_refused(runtime):
    manager, session, project, tools, _ = runtime
    image = {**IMAGE, "data": {"image/png": "A" * 300_000}}
    path = project / "large.ipynb"
    path.write_text(jupyter(notebook([("code", "plot()\n", [image]), ("markdown", "Notes\n", [])])))
    assert 300_000 < path.stat().st_size < 1_000_000 and tools.write_limit == 80_000
    await tool(runtime, "notebook_read", {"path": "large.ipynb"}, "read")
    output, event = await tool(runtime, "notebook_edit", {"path": "large.ipynb", "index": 1, "source": "More notes\n"})
    assert event["state"] == "completed", output
    assert cells_of(path.read_text())[1]["source"] == ["More notes\n"]

    huge = project / "huge.ipynb"
    huge.write_text(jupyter(notebook([("code", "plot()\n", [{**IMAGE, "data": {"image/png": "B" * 1_100_000}}])])))
    await tool(runtime, "notebook_read", {"path": "huge.ipynb"}, "read-huge")
    output, event = await tool(runtime, "notebook_edit", {"path": "huge.ipynb", "edit_mode": "clear_outputs"}, "huge")
    assert event["state"] == "error"
    assert "notebook_edit works on notebooks up to 1,000 KB. Clear outputs with run_command" in output
    assert "jupyter nbconvert --clear-output --inplace" in output


async def test_a_change_during_the_approval_wait_is_refused(runtime):
    manager, session, project, tools, _ = runtime
    session["permission_mode"] = "manual"
    path = project / "analysis.ipynb"
    await tool(runtime, "notebook_read", {"path": "analysis.ipynb"}, "read")

    async def approve(session, event):
        path.write_text(path.read_text().replace("Title", "Changed"))
        os.utime(path, ns=(1, 1))
        return True
    manager.approve = approve
    output, event = await tool(runtime, "notebook_edit", {"path": "analysis.ipynb", "index": 2, "source": "y\n"})
    assert event["state"] == "error" and "The file changed while waiting for approval." in output
    assert manager.checkpoints.list(session["id"]) == []


async def test_notebook_read_is_logged_and_allowed_in_plan_mode(runtime):
    manager, session, project, tools, _ = runtime
    session["permission_mode"] = "plan"
    output, event = await tool(runtime, "notebook_read", {"path": "analysis.ipynb", "offset": 1, "max_cells": 1})
    assert event["state"] == "completed" and json.loads(output)["next_offset"] == 2
    from local_agent.activity import activity_log
    from local_agent.transcript import transcript
    assert "**Read notebook** (`notebook_read`)" in activity_log(session)[0]
    assert "- Read notebook `analysis.ipynb`" in transcript(session)


# --- Review findings: long cells, format, ids, results, diagnostics, large notebooks --------

def test_a_long_cell_is_read_in_full_through_source_offset(tmp_path):
    tools = WorkspaceTools(str(tmp_path))
    source = "".join(f"value_{n} = {n}\n" for n in range(1_500))
    (tmp_path / "long.ipynb").write_text(jupyter(notebook([("code", source, []), ("markdown", "end\n", [])])))
    page = read_notebook(tools, "long.ipynb")
    first = page["cells"][0]
    assert first["source_truncated"] is True and first["source_chars"] == len(source)
    assert first["next_source_offset"] == len(first["source"]) == 4_000
    pieces, start = [first["source"]], first["next_source_offset"]
    while start is not None:
        part = read_notebook(tools, "long.ipynb", offset=0, source_offset=start)
        assert len(json.dumps(part, indent=2).encode()) <= 8_000
        [cell] = part["cells"]
        assert cell["index"] == 0 and cell["source_offset"] == start and part["next_offset"] == 1
        pieces.append(cell["source"])
        start = cell.get("next_source_offset")
    assert "".join(pieces) == source
    with pytest.raises(ValueError, match="offset 5 is out of range"):
        read_notebook(tools, "long.ipynb", offset=5, source_offset=0)
    from local_agent.tools import TOOL_DEFINITIONS
    definition = next(item["function"] for item in TOOL_DEFINITIONS if item["function"]["name"] == "notebook_read")
    assert "source_offset" in definition["parameters"]["properties"] and "source_offset" in definition["description"]


async def test_the_notebook_read_tool_passes_source_offset(runtime):
    manager, session, project, tools, _ = runtime
    output, event = await tool(runtime, "notebook_read", {"path": "analysis.ipynb", "offset": 1, "source_offset": 6})
    assert event["state"] == "completed", output
    assert json.loads(output)["cells"] == [{"index": 1, "id": "cell0001", "cell_type": "code", "source": "'hello')\nplot()\n",
                                            "source_offset": 6, "execution_count": 2,
                                            "outputs": "2 outputs: stream, display_data (image/png)"}]


@pytest.mark.parametrize("dump", [
    lambda value: json.dumps(value, indent=2, ensure_ascii=False) + "\n",  # Unsorted keys, as some tools write.
    lambda value: json.dumps(value, indent=1, sort_keys=True, ensure_ascii=True) + "\n",  # Escaped non-ASCII.
])
def test_other_json_layouts_are_kept_outside_the_edited_cell(dump):
    value = notebook([("markdown", "# Notes – café\n", []), ("code", "x = 1\n", [])])
    value = {"nbformat": value["nbformat"], "nbformat_minor": 5, "metadata": value["metadata"], "cells": value["cells"]}
    before = dump(value)
    after, summary = plan_edit(before, {"index": 0, "source": "# Notes v2\n"})
    changed = [line for line in difflib.ndiff(before.splitlines(), after.splitlines()) if line[:1] in "+-"]
    assert len(changed) == 2 and changed[0].startswith("-") and "# Notes" in changed[0]
    assert changed[1].startswith("+") and changed[1].endswith('"# Notes v2\\n"')
    assert summary["reformatted"] is False
    assert "rewritten" not in preview("n.ipynb", summary)


def test_a_notebook_that_cannot_be_reproduced_says_it_will_be_rewritten():
    before = json.dumps(SAMPLE, indent=3) + "\n"
    after, summary = plan_edit(before, {"index": 0, "source": "# New title\n"})
    assert after == jupyter(json.loads(after))
    card = preview("n.ipynb", summary)
    assert card.endswith("The notebook's JSON will be rewritten in Jupyter's format (indent 1, sorted keys), "
                         "so lines outside this cell may change too.")
    assert "JSON was rewritten" in preview("n.ipynb", summary, done=True)


async def test_an_inserted_cell_has_the_id_the_card_showed(runtime):
    manager, session, project, tools, cards = runtime
    session["permission_mode"] = "manual"
    await tool(runtime, "notebook_read", {"path": "analysis.ipynb"}, "read")
    output, event = await tool(runtime, "notebook_edit", {"path": "analysis.ipynb", "edit_mode": "insert", "index": 1,
                                                          "source": "y = 2\n"})
    assert event["state"] == "completed", output
    written = cells_of((project / "analysis.ipynb").read_text())[1]["id"]
    assert len(written) == 8 and int(written, 16) >= 0
    assert cards[0]["preview"].startswith(f"Notebook analysis.ipynb: insert cell 1 (code, id {written})")
    assert output.startswith(f"Notebook analysis.ipynb: insert cell 1 (code, id {written})")
    assert written not in {cell.get("id") for cell in SAMPLE["cells"]}


async def test_results_say_what_happened(runtime):
    manager, session, project, tools, _ = runtime
    await tool(runtime, "notebook_read", {"path": "analysis.ipynb"}, "read")
    output, _ = await tool(runtime, "notebook_edit", {"path": "analysis.ipynb", "index": 0, "source": "# Title\n"}, "same")
    assert output.startswith("No changes. Notebook analysis.ipynb: replace cell 0 (markdown, id cell0000)")
    output, _ = await tool(runtime, "notebook_edit", {"path": "analysis.ipynb", "index": 1, "source": "plot()\n"}, "edit")
    assert output.endswith("Its outputs were cleared.") and "will be" not in output


async def test_conflict_markers_added_to_a_cell_are_reported(runtime):
    manager, session, project, tools, _ = runtime
    await tool(runtime, "notebook_read", {"path": "analysis.ipynb"}, "read")
    source = "<<<<<<< HEAD\nx = 1\n=======\nx = 2\n>>>>>>> branch\n"
    output, event = await tool(runtime, "notebook_edit", {"path": "analysis.ipynb", "index": 2, "source": source})
    assert event["state"] == "completed"
    assert output.endswith("\n\nDiagnostics for analysis.ipynb:\n- cell 2: unresolved merge-conflict markers")
    # Another cell edited while cell 2 still has its markers: they are not reported again.
    output, _ = await tool(runtime, "notebook_edit", {"path": "analysis.ipynb", "index": 0, "source": "# T\n"}, "next")
    assert "Diagnostics" not in output


class Page:
    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)


async def test_an_edit_in_a_large_notebook_does_not_block_the_server(runtime):
    manager, session, project, tools, _ = runtime
    manager.listeners[session["id"]] = {page := Page()}
    stream = {"name": "stdout", "output_type": "stream", "text": ["1\n"]}
    path = project / "many.ipynb"
    path.write_text(jupyter(notebook([("code", f"x{n} = compute({n})\nprint(x{n})\n", [stream]) for n in range(1_500)])))
    assert path.stat().st_size > 400_000
    await tool(runtime, "notebook_read", {"path": "many.ipynb"}, "read")
    started = asyncio.get_running_loop().time()
    output, event = await tool(runtime, "notebook_edit", {"path": "many.ipynb", "index": 700, "source": "y = 2\n"})
    assert asyncio.get_running_loop().time() - started < 2, "the change review diff blocked the event loop"
    assert event["state"] == "completed", output
    [entry] = next(item["changes"] for item in page.sent if item["type"] == "changes")["files"]
    assert entry["added"] + entry["removed"] < 20  # The one cell, not the whole file.
