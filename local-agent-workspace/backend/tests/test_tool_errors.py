"""Structured tool errors: the model gets {error_kind, message, hint}; the card keeps plain text."""
import json

import httpx
import pytest

from local_agent.agents import AgentManager, repair_tool_history
from local_agent.config import Settings
from local_agent.store import Store
from local_agent.tool_errors import classify, tool_error_text
from local_agent.tools import WorkspaceTools


@pytest.mark.parametrize("exc, kind, hinted", [
    (TimeoutError(), "timeout", True),
    (FileNotFoundError(2, "No such file or directory"), "not_found", True),
    (IsADirectoryError(21, "Is a directory"), "wrong_type", False),
    (NotADirectoryError(20, "Not a directory"), "wrong_type", False),
    (PermissionError(13, "Permission denied"), "permission_denied", False),
    (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"), "encoding", False),
    (KeyError("path"), "invalid_arguments", True),
    (TypeError("bad"), "invalid_arguments", True),
    (ValueError("Invalid tool arguments at $.path: JSON Schema type constraint failed."), "invalid_arguments", True),
    (ValueError("Command output limit must be an integer from 1,024 to 1,000,000 bytes."), "invalid_arguments", True),
    (ValueError("background must be true or false."), "invalid_arguments", True),
    (ValueError("Give either start_line or tail_lines, not both."), "invalid_arguments", True),
    (ValueError("regex and case_sensitive must be booleans."), "invalid_arguments", True),
    (ValueError("Credential files are excluded from file tools."), "excluded", True),
    (ValueError("This internal or generated directory is excluded."), "excluded", True),
    (ValueError("/srv/x needs access approval."), "access_required", False),
    (ValueError("Project instructions changed before execution. Review the refreshed guidance and retry."),
     "guidance_changed", True),
    (ValueError("Project instructions changed while preparing this action. Review the refreshed guidance before "
                "retrying. No file was changed and no command ran."), "guidance_changed", True),
    (ValueError("Project or skill instructions changed. Review refreshed guidance before using MCP tools."),
     "guidance_changed", True),
    (ValueError("The file changed while waiting for approval. Read it again and propose a fresh edit."), "stale_file", True),
    (ValueError("The file changed before execution. Read it again and propose a fresh edit."), "stale_file", True),
    (ValueError("old_text must match exactly once; include more surrounding context."), "edit_mismatch", True),
    (ValueError("Cell id abc was not found."), "not_found", False),
    (ValueError("Job not found in this conversation."), "not_found", False),
    (ValueError("Tool is not available in this turn."), "unavailable_tool", False),
    (ValueError("Search scan limit reached: narrow the path."), "limit", True),
    (ValueError("Content exceeds the 80 KB write limit."), "limit", True),
    # outline_file and project_overview (u-large-inputs).
    (ValueError("Only regular files can be outlined, and this is a folder; use project_overview or find_files for "
                "folders."), "wrong_type", False),
    (ValueError("Only regular text files can be outlined."), "wrong_type", False),
    (ValueError("Only regular text files can be outlined; use notebook_read for Jupyter notebooks."), "wrong_type",
     False),
    (ValueError("This file exceeds the 8 MB outline limit; use search_files (for example regex=true with ^#{1,3} or "
                "^\\s*(def|class)) to find sections."), "limit", True),
    (ValueError("Binary files cannot be outlined."), "wrong_type", False),
    (ValueError("Outlining this file took longer than 3 seconds; use search_files to find sections."), "limit", True),
    (ValueError("offset is beyond the 2 outline entries."), "invalid_arguments", True),
    (ValueError("Not a directory: project_overview needs a folder; use outline_file for a file."), "wrong_type", False),
    (ValueError("include_ignored must be a boolean."), "invalid_arguments", True),
    # Messages of the M2 tools (search, read, jobs, file edits, notebooks, move/delete).
    (ValueError("output_mode must be content, files_with_matches or count."), "invalid_arguments", True),
    (ValueError("sort must be path or modified."), "invalid_arguments", True),
    (ValueError("pattern must be 1 to 500 characters."), "invalid_arguments", True),
    (ValueError("until_regex must be text of at most 500 characters."), "invalid_arguments", True),
    (ValueError("Invalid until_regex: missing ) at position 1"), "invalid_arguments", True),
    (ValueError("Invalid regular expression: unterminated character set"), "invalid_arguments", True),
    (ValueError("Nested {…} groups are not supported in pattern."), "invalid_arguments", True),
    (ValueError("A {…} group in pattern can have at most 20 alternatives."), "invalid_arguments", True),
    (ValueError("Search text cannot be empty."), "invalid_arguments", True),
    (ValueError("Use either start_line or tail_lines, not both."), "invalid_arguments", True),
    (ValueError("offset is beyond the 12 matching files."), "invalid_arguments", True),
    (ValueError("from_offset is beyond the job output (40 characters)."), "invalid_arguments", True),
    (ValueError("index 9 is out of range (the notebook has 3 cells)."), "invalid_arguments", True),
    (ValueError("insert needs source."), "invalid_arguments", True),
    (ValueError("edit_mode must be replace, insert, delete or clear_outputs."), "invalid_arguments", True),
    (ValueError("Put replace_all inside each edit when using edits."), "invalid_arguments", True),
    (ValueError("Command must contain text and be at most 50,000 characters."), "invalid_arguments", True),
    (ValueError("Not a Jupyter notebook: the file has no cells list."), "wrong_type", False),
    (ValueError("Not a Jupyter notebook: every cell must be an object."), "wrong_type", False),
    (ValueError("Binary files cannot be read as text."), "wrong_type", False),
    (ValueError("Search path must be a regular file or directory."), "wrong_type", False),
    (ValueError("destination is a folder; give the full new file path."), "wrong_type", False),
    (ValueError("path does not exist."), "not_found", False),
    (ValueError("Command job not found."), "not_found", False),
    (ValueError("The job is no longer available."), "not_found", False),
    (ValueError("until_regex took too long; simplify it."), "limit", True),
    (ValueError("Listing files took longer than 10 seconds; narrow the path."), "limit", True),
    (ValueError("More than 200000 files under this path; narrow the path."), "limit", True),
    (ValueError("The file is 900 KB, over the file write limit (80 KB, set in Settings)."), "limit", True),
    (ValueError("At most four commands can run at once. Stop or wait for a running command."), "limit", True),
    (ValueError("Line 3 is too long for ranged reading (128 KB limit)."), "limit", True),
    (ValueError("The file changed while it was read; read it again."), "stale_file", True),
    (ValueError("File changed or the checkpoint was already restored; request a new recovery preview."), "stale_file", True),
    (ValueError("Read a.py again before overwriting it: its earlier contents were summarized out of your context at "
                "compaction. Use read_file, then write again."), "stale_file", True),
    # Commands run without a shell, and the state-folder exclusion (command_exec, git_tools, tools).
    (ValueError("The app's settings and conversation folder is excluded from file tools."), "excluded", True),
    (ValueError("Command not found on PATH: tool"), "not_found", False),
    (ValueError("Command not found: ./tool"), "not_found", False),
    (ValueError("Git is not installed or not on PATH."), "not_found", False),
    (ValueError("Not a read-only command."), "command_refused", True),
    (ValueError("Not a read-only Git command."), "command_refused", True),
    (ValueError("This Git option is not allowed in a read-only command."), "command_refused", True),
    (ValueError("git resolves to /srv/x/bin/git, inside the project or a folder granted to this chat, "
                "so it does not run automatically."), "command_refused", True),
    (ValueError("This repository is a partial clone, and this Git version cannot switch off fetching missing "
                "objects, so read-only Git does not run automatically."), "command_refused", True),
    (ValueError("A command run without a shell needs 1 to 64 arguments, each text of at most 1,000 characters "
                "without NUL."), "invalid_arguments", True),
    (ValueError("/bin/ls is not available, so this read-only command does not run automatically."),
     "command_refused", True),
    (ValueError("Read-only commands run in the foreground only."), "invalid_arguments", True),
    (ValueError("Something else went wrong."), "error", False),
    (OSError("disk full"), "error", False),
])
def test_classify_rules(exc, kind, hinted):
    found, hint = classify(exc, str(exc))
    assert found == kind
    assert (hint is not None) == hinted


def test_a_guidance_change_is_not_called_a_stale_file():
    message = "Project instructions changed before execution. Review the refreshed guidance and retry."
    assert json.loads(tool_error_text(ValueError(message), message)) == {
        "error_kind": "guidance_changed", "message": message,
        "hint": "Re-read the refreshed project instructions, then retry."}


def test_an_exception_can_name_its_own_kind_and_hint():
    error = ValueError("Notebook cell 3 is not code.")
    error.error_kind, error.hint = "wrong_type", "Use a code cell id."
    assert classify(error, str(error)) == ("wrong_type", "Use a code cell id.")
    assert json.loads(tool_error_text(error, "Notebook cell 3 is not code.")) == {
        "error_kind": "wrong_type", "message": "Notebook cell 3 is not code.", "hint": "Use a code cell id."}


def test_error_text_is_json_without_a_hint_when_none_applies():
    assert json.loads(tool_error_text(ValueError("Odd"), "Odd – ü")) == {"error_kind": "error", "message": "Odd – ü"}
    assert "ü" in tool_error_text(ValueError("x"), "ü")  # Not escaped, like other tool output.


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    settings.redact = lambda text: text.replace("private-token", "[REDACTED]")
    store = Store(tmp_path / "tests.sqlite3")
    session = store.create(settings.values)
    session.update(title="Errors", title_generated=True, permission_mode="bypassPermissions")
    manager = AgentManager(store, settings)
    yield manager, session, WorkspaceTools(str(project), "", unrestricted=True), project
    store.db.close()


def frame(delta, finish=None):
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"


async def test_mismatching_edit_gives_the_model_a_structured_error_and_the_card_plain_text(runtime, monkeypatch):
    manager, session, _, project = runtime
    (project / "a.txt").write_text("alpha\n")
    requests = []
    edit = {"path": "a.txt", "old_text": "beta", "new_text": "gamma"}

    async def gateway(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, text=frame({"tool_calls": [{"index": 0, "id": "edit", "function": {
                "name": "edit_file", "arguments": json.dumps(edit)}}]}, "tool_calls") + "data: [DONE]\n\n")
        return httpx.Response(200, text=frame({"content": "I will read it first."}, "stop") + "data: [DONE]\n\n")

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(gateway)))
    await manager.run(session, "Change beta")
    sent = requests[1]["messages"][-1]
    assert sent["role"] == "tool" and sent["tool_call_id"] == "edit"
    error = json.loads(sent["content"])
    assert error["error_kind"] == "edit_mismatch" and "old_text" in error["message"]
    assert error["hint"].startswith("Read the file again and copy old_text exactly")
    card = next(event for event in session["events"] if event.get("call_id") == "edit")
    assert card["state"] == "error" and card["output"] == error["message"]
    assert card["model_output"] == sent["content"]
    assert (project / "a.txt").read_text() == "alpha\n"


async def test_missing_file_and_schema_errors(runtime):
    manager, session, tools, _ = runtime
    missing = json.loads(await manager.execute_tool(session, tools, "read_file", {"path": "nope.txt"}, "r1"))
    assert missing["error_kind"] == "not_found" and missing["hint"] == "Check the path with find_files or list_files."
    schema = json.loads(await manager.execute_tool(session, tools, "read_file", {"path": 5}, "r2"))
    assert schema["error_kind"] == "invalid_arguments" and schema["message"].startswith("Invalid tool arguments")
    assert session["events"][-1]["output"] == schema["message"]


async def test_tool_errors_are_redacted_for_the_model(runtime):
    manager, session, tools, _ = runtime

    async def fail(*args):
        raise ValueError("private-token was not found")
    tools.execute = fail
    output = await manager.execute_tool(session, tools, "list_files", {"path": "."}, "l1")
    assert "private-token" not in output and json.loads(output)["message"] == "[REDACTED] was not found"


async def test_decisions_stay_plain_text(runtime):
    manager, session, tools, _ = runtime
    session["permission_mode"] = "plan"
    output = await manager.execute_tool(session, tools, "write_file", {"path": "b.txt", "content": "x"}, "w1")
    assert output.startswith("Plan mode is read-only.")
    assert "model_output" not in session["events"][-1]


def call(call_id="reused", path="note.txt"):
    return {"id": call_id, "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": path})}}


def test_history_repair_aligns_reused_ids_by_what_the_model_saw():
    first_error = tool_error_text(FileNotFoundError(2, "missing"), "note.txt was not found")
    second_error = tool_error_text(ValueError("too large"), "note.txt exceeds the limit")
    wire = [{"role": "assistant", "content": None, "tool_calls": [call()]},
            {"role": "tool", "tool_call_id": "reused", "content": first_error},
            {"role": "assistant", "content": None, "tool_calls": [call()]}]
    events = [{"id": "e1", "type": "tool", "call_id": "reused", "name": "read_file", "input": {"path": "note.txt"},
               "state": "error", "output": "note.txt was not found", "model_output": first_error},
              {"id": "e2", "type": "tool", "call_id": "reused", "name": "read_file", "input": {"path": "note.txt"},
               "state": "error", "output": "note.txt exceeds the limit", "model_output": second_error}]
    repaired, _ = repair_tool_history(wire, events, {})
    assert repaired[-1] == {"role": "tool", "tool_call_id": "reused", "content": second_error}
    # Older records without model_output still compare and fill with output.
    legacy = [{key: value for key, value in event.items() if key != "model_output"} for event in events]
    wire[1]["content"] = "note.txt was not found"
    repaired, _ = repair_tool_history(wire, legacy, {})
    assert repaired[-1]["content"] == "note.txt exceeds the limit"
