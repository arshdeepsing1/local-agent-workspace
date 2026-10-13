"""Diagnostics after edits: new syntax, JSON, TOML and merge-conflict problems only."""
import sys

import pytest

from local_agent import diagnostics
from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.instructions import load_project_instructions
from local_agent.store import Store

VERSION = f"Python {sys.version_info.major}.{sys.version_info.minor} parser; ignore if the project targets a newer Python"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    store = Store(settings.state_dir / "diagnostics.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session["permission_mode"] = "acceptEdits"
    tools = manager.session_tools(session)
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance["text"], guidance["warnings"])
    tools.skill_signature = ""
    yield manager, session, project, tools
    store.db.close()


async def tool(runtime, name, arguments, call_id="call"):
    manager, session, _, tools = runtime
    return await manager.execute_tool(session, tools, name, arguments, call_id)


async def test_a_new_python_syntax_error_is_appended_with_the_parser_version(runtime):
    _, _, project, _ = runtime
    (project / "app.py").write_text("def f():\n    return 1\n")
    output = await tool(runtime, "edit_file", {"path": "app.py", "old_text": "return 1", "new_text": "return (1"})
    assert output.startswith("--- app.py")
    assert "\n\nDiagnostics for app.py:\n- line 2, column 12: SyntaxError: " in output
    assert output.endswith(f"({VERSION})")


async def test_an_existing_problem_is_not_repeated_and_clean_files_add_nothing(runtime):
    _, _, project, _ = runtime
    (project / "broken.py").write_text("x = (\n\ny = 2\n")
    output = await tool(runtime, "edit_file", {"path": "broken.py", "old_text": "y = 2", "new_text": "y = 3"})
    assert "Diagnostics" not in output
    output = await tool(runtime, "write_file", {"path": "clean.py", "content": "print('ok')\n"}, "clean")
    assert "Diagnostics" not in output
    # A new file with a problem: there was no earlier state to have it.
    output = await tool(runtime, "write_file", {"path": "new.json", "content": '{"a": 1,}\n'}, "json")
    assert "Diagnostics for new.json:\n- line 1, column 9: invalid JSON: " in output


def test_json_toml_notebooks_and_conflict_markers():
    assert diagnostics.check("a.json", '{"a": 1}', '{"a": 1') == ["line 1, column 8: invalid JSON: Expecting ',' delimiter"]
    assert diagnostics.check("n.ipynb", "{}", "{") and not diagnostics.check("n.ipynb", "{", "{")
    [toml] = diagnostics.check("pyproject.toml", "[project]\nname = 'x'\n", "[project]\nname = \n")
    assert toml.startswith("invalid TOML: ")
    conflict = "a\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n"
    assert diagnostics.check("notes.md", "a\n", conflict) == ["lines 2, 4, 6: unresolved merge-conflict markers"]
    assert diagnostics.check("notes.md", conflict, conflict + "more\n") == []
    # A Markdown heading underline alone is not a conflict marker.
    assert diagnostics.check("notes.md", None, "Title\n=======\n") == []
    assert diagnostics.check("data.txt", None, "plain text\n") == []


def test_python_checks_never_run_code_and_survive_deep_nesting(tmp_path):
    marker = tmp_path / "ran"
    assert diagnostics.check("x.py", None, f"open({str(marker)!r}, 'w').write('x')\n") == []
    assert not marker.exists()
    [message] = diagnostics.check("deep.py", None, "x = " + "(" * 100_000 + ")" * 100_000 + "\n")
    assert "could not be checked" in message or "SyntaxError" in message
    # Warnings (an invalid escape) are not reported and do not leak.
    assert diagnostics.check("w.py", None, "x = '\\d'\n") == []


def test_report_is_bounded():
    entries = [f"problem {number} " + "x" * 400 for number in range(8)]
    original = diagnostics.check
    try:
        diagnostics.check = lambda *args: entries
        text = diagnostics.report("f.py", None, "")
    finally:
        diagnostics.check = original
    assert text.startswith("\n\nDiagnostics for f.py:\n- problem 0")
    assert text.count("\n- ") <= 5 and len(text) < 1_100
    assert diagnostics.report("f.py", None, "print(1)\n") == ""


async def test_a_move_reports_problems_at_the_destination(runtime):
    _, _, project, _ = runtime
    (project / "data.txt").write_text("{not json}\n")
    output = await tool(runtime, "move_path", {"path": "data.txt", "destination": "data.json"}, "move")
    assert output.startswith("Moved data.txt to data.json.\n\nDiagnostics for data.json:\n- line 1, column 2: invalid JSON")
