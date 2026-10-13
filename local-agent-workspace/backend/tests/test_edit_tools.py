"""edit_file (replace_all, edits[]), line endings, the file write limit and new-file modes."""
import difflib
import json
import os
import random
import stat
import time

import pytest
from fastapi.testclient import TestClient

from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.activity import activity_content
from local_agent.edits import line_opcodes, plan_change, preview_diff
from local_agent.instructions import load_project_instructions
from local_agent.recovery import CheckpointManager
from local_agent.store import Store
from local_agent.tools import WorkspaceTools, write_limit_setting


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    store = Store(settings.state_dir / "edits.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session["permission_mode"] = "acceptEdits"
    yield manager, session, project
    store.db.close()


def tools_for(manager, session):
    """The tools a turn would use, with the current settings (the write limit included)."""
    tools = manager.session_tools(session)
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance["text"], guidance["warnings"])
    tools.skill_signature = ""
    return tools


async def tool(manager, session, name, arguments, call_id="call"):
    output = await manager.execute_tool(session, tools_for(manager, session), name, arguments, call_id)
    return output, next(item for item in reversed(session["events"]) if item.get("call_id") == call_id)


def changed_lines(diff):
    lines = diff.splitlines()
    return ([line for line in lines if line.startswith("-") and not line.startswith("---")],
            [line for line in lines if line.startswith("+") and not line.startswith("+++")])


# --- Line endings -----------------------------------------------------------------------

async def test_crlf_round_trip_keeps_every_ending_and_shows_one_changed_line(runtime):
    manager, session, project = runtime
    target = project / "win.txt"
    target.write_bytes(b"one\r\ntwo\r\nthree\r\nfour\r\n")
    session["permission_mode"] = "manual"
    previews = []

    async def approve(session, event):
        previews.append(event["preview"])
        return True
    manager.approve = approve
    await manager.event(session, "user", text="Edit it")
    output, event = await tool(manager, session, "edit_file", {"path": "win.txt", "old_text": "two\nthree", "new_text": "TWO\nthree"})
    assert event["state"] == "completed", output
    assert target.read_bytes() == b"one\r\nTWO\r\nthree\r\nfour\r\n"
    assert changed_lines(previews[0]) == (["-two"], ["+TWO"])
    assert changed_lines(output) == (["-two"], ["+TWO"])
    diff = manager.changes.diff(session["id"], tools_for(manager, session), "win.txt")
    assert len(diff["hunks"]) == 1 and diff["added"] == 1 and diff["removed"] == 1


def test_mixed_endings_keep_their_per_line_endings_in_edits_and_replace_all():
    mixed = "a\r\nb\nc\r\nd\n"
    # New lines take the majority ending (CRLF on a tie); untouched lines keep theirs.
    assert plan_change("edit_file", {"old_text": "b", "new_text": "B\nB2"}, mixed, 80_000).text == "a\r\nB\r\nB2\nc\r\nd\n"
    assert plan_change("edit_file", {"old_text": "b\nc", "new_text": "X"}, mixed, 80_000).text == "a\r\nX\r\nd\n"
    plan = plan_change("edit_file", {"old_text": "x", "new_text": "z", "replace_all": True}, "x\r\nx\ny\r\nx\n", 80_000)
    assert plan.text == "z\r\nz\ny\r\nz\n" and plan.prefix == "Applied 1 edit (3 replacements).\n\n"
    # Replaced endings take the majority ending (here a tie, so CRLF).
    plan = plan_change("edit_file", {"old_text": "x\n", "new_text": "z\n", "replace_all": True}, "x\r\nx\ny\r\nx\n", 80_000)
    assert plan.text == "z\r\nz\r\ny\r\nz\r\n"
    lf_majority = "p\nq\nr\r\ns\n"  # one CRLF, three LF
    assert plan_change("edit_file", {"old_text": "q", "new_text": "Q\nQ"}, lf_majority, 80_000).text == "p\nQ\nQ\nr\r\ns\n"


def test_bare_cr_is_preserved_by_edit_file():
    assert plan_change("edit_file", {"old_text": "c", "new_text": "C"}, "a\rb\nc\n", 80_000).text == "a\rb\nC\n"
    assert plan_change("edit_file", {"old_text": "c", "new_text": "C"}, "a\rb\r\nc\r\n", 80_000).text == "a\rb\r\nC\r\n"


def test_write_file_keeps_pure_crlf_and_the_unchanged_lines_of_mixed_files():
    assert plan_change("write_file", {"content": "a\nB\nc\n"}, "a\r\nb\r\nc\r\n", 80_000).text == "a\r\nB\r\nc\r\n"
    mixed = "a\r\nb\nc\r\nd\n"
    assert plan_change("write_file", {"content": "a\nB\nc\nd\n"}, mixed, 80_000).text == "a\r\nB\r\nc\r\nd\n"
    # Content with its own CR, files without CRLF and new files are written as given.
    assert plan_change("write_file", {"content": "x\r\ny\n"}, mixed, 80_000).text == "x\r\ny\n"
    assert plan_change("write_file", {"content": "x\n"}, "y\n", 80_000).text == "x\n"
    assert plan_change("write_file", {"content": "x\n"}, None, 80_000).text == "x\n"


async def test_write_file_over_a_crlf_file_keeps_crlf(runtime):
    manager, session, project = runtime
    target = project / "win.txt"
    target.write_bytes(b"one\r\ntwo\r\n")
    await tool(manager, session, "read_file", {"path": "win.txt"}, "read")
    output, event = await tool(manager, session, "write_file", {"path": "win.txt", "content": "one\ntwo\nthree\n"})
    assert event["state"] == "completed", output
    assert target.read_bytes() == b"one\r\ntwo\r\nthree\r\n"
    assert changed_lines(output) == ([], ["+three"])


def test_editor_save_of_lf_text_over_a_mixed_file_keeps_its_endings(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    workspace = tmp_path / "project"
    workspace.mkdir()
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(workspace), env_file="")
    settings.credentials = lambda: ("https://fake.example", "synthetic-test-token")
    target = workspace / "mixed.txt"
    target.write_bytes(b"a\r\nb\nc\r\nd\n")
    with TestClient(create_app(settings)) as client:
        client.headers["X-Local-Token"] = client.get("/api/bootstrap").json()["token"]
        opened = client.get("/api/file", params={"path": "mixed.txt"}).json()["content"]
        assert opened == "a\nb\nc\nd\n"  # The editor shows LF.
        body = {"path": "mixed.txt", "original": opened, "content": "a\nB\nc\nd\n"}
        assert client.put("/api/file", json=body).status_code == 200
    assert target.read_bytes() == b"a\r\nB\r\nc\r\nd\n"


def test_preview_diff_uses_lf_views_and_names_ending_only_changes():
    assert preview_diff("f", "a\r\nb\r\n", "a\nb\n") == "Only line endings change."
    assert preview_diff("f", "a\n", "a\n") == "No changes."
    diff = preview_diff("f", "x\n" * 20 + "old\n" + "y\n" * 20, "x\n" * 20 + "new\n" + "y\n" * 20)
    assert "@@ -18,7 +18,7 @@" in diff and changed_lines(diff) == (["-old"], ["+new"])
    assert "\\ No newline at end of file" in preview_diff("f", "a", "b")


def test_a_one_megabyte_file_of_identical_lines_is_edited_quickly(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "state.sqlite3")
    manager = CheckpointManager(store)
    tools = WorkspaceTools(str(workspace), write_limit=1_000_000)
    for name, ending in (("lf.txt", "\n"), ("crlf.txt", "\r\n")):
        blank = " " * (20 - len(ending)) + ending
        text = blank * 25_000 + "target" + ending + blank * 24_999
        assert len(text.encode()) <= 1_000_000
        (workspace / name).write_bytes(text.encode())
        started = time.monotonic()
        diff = manager.apply_edit(tools, "edit_file", {"path": name, "old_text": "target", "new_text": "changed"})
        assert time.monotonic() - started < 1
        assert changed_lines(diff) == (["-target"], ["+changed"])
        assert (workspace / name).read_bytes() == text.replace("target", "changed").encode()
    store.db.close()


# --- replace_all and edits[] -------------------------------------------------------------

async def test_replace_all_reports_its_replacements(runtime):
    manager, session, project = runtime
    (project / "a.py").write_text("x = 1\nprint(x)\nx += x\n")
    output, _ = await tool(manager, session, "edit_file", {"path": "a.py", "old_text": "x", "new_text": "y", "replace_all": True})
    assert output.startswith("Applied 1 edit (4 replacements).\n\n")
    assert (project / "a.py").read_text() == "y = 1\nprint(y)\ny += y\n"
    (project / "b.txt").write_text("k k k\n")
    output, _ = await tool(manager, session, "edit_file", {"path": "b.txt", "old_text": "k", "new_text": "m", "replace_all": True})
    assert output.startswith("Applied 1 edit (3 replacements).")
    # A single plain edit keeps the old result: just the diff.
    output, _ = await tool(manager, session, "edit_file", {"path": "b.txt", "old_text": "m m m", "new_text": "done"})
    assert output.startswith("--- b.txt")


async def test_edits_apply_in_order_with_one_card_and_one_checkpoint(runtime):
    manager, session, project = runtime
    session["permission_mode"] = "manual"
    target = project / "app.py"
    target.write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n")
    cards = []

    async def approve(session, event):
        cards.append(event["name"])
        return True
    manager.approve = approve
    edits = [{"old_text": "return 1", "new_text": "return 10"},
             {"old_text": "return 10\n", "new_text": "return 10  # ten\n"},  # Sees the first edit's text.
             {"old_text": "return", "new_text": "yield", "replace_all": True}]
    output, event = await tool(manager, session, "edit_file", {"path": "app.py", "edits": edits})
    assert event["state"] == "completed", output
    assert output.startswith("Applied 3 edits (4 replacements).\n\n")
    assert target.read_text() == "def f():\n    yield 10  # ten\n\n\ndef g():\n    yield 2\n"
    assert cards == ["edit_file"]
    assert len(manager.checkpoints.list(session["id"])) == 1


async def test_an_error_in_any_edit_writes_nothing(runtime):
    manager, session, project = runtime
    target = project / "app.py"
    target.write_bytes(b"alpha\nbeta\nbeta\n")
    edits = [{"old_text": "alpha", "new_text": "ALPHA"}, {"old_text": "ALPHA", "new_text": "A"},
             {"old_text": "gamma", "new_text": "G"}]
    output, event = await tool(manager, session, "edit_file", {"path": "app.py", "edits": edits})
    assert event["state"] == "error"
    assert "edits[2]: old_text was not found (after the earlier edits in this call)." in output
    assert "read_file line numbers are not part of the text" in output
    output, _ = await tool(manager, session, "edit_file", {"path": "app.py", "edits": [{"old_text": "beta", "new_text": "B"}]})
    assert "edits[0]: old_text matches 2 times; include more surrounding context so it matches once, or set replace_all=true." in output
    assert target.read_bytes() == b"alpha\nbeta\nbeta\n"
    assert manager.checkpoints.list(session["id"]) == [] and manager.changes.list(session["id"], tools_for(manager, session))["files"] == []


@pytest.mark.parametrize("arguments, message", [
    ({"old_text": "a", "new_text": "b", "edits": [{"old_text": "a", "new_text": "b"}]},
     "Use either old_text and new_text, or edits, not both."),
    ({}, "Give old_text and new_text, or edits."),
    ({"old_text": "a"}, "Give old_text and new_text, or edits."),
    ({"old_text": "", "new_text": "b"}, "old_text cannot be empty."),
    ({"edits": [{"old_text": "a a a", "new_text": "b"}, {"old_text": "", "new_text": "c"}]}, "edits[1]: old_text cannot be empty."),
    ({"old_text": "a", "new_text": "b"}, "old_text matches 3 times; include more surrounding context"),
    ({"edits": [{"old_text": "a a a", "new_text": "b"}], "replace_all": True},
     "Put replace_all inside each edit when using edits."),
])
def test_edit_argument_errors(arguments, message):
    with pytest.raises(ValueError) as error:
        plan_change("edit_file", arguments, "a a a\n", 80_000)
    assert message in str(error.value)


@pytest.mark.parametrize("defaults", [{"replace_all": False}, {"old_text": None, "new_text": None},
                                      {"replace_all": None, "old_text": None}])
def test_schema_defaults_next_to_edits_count_as_absent(defaults):
    plan = plan_change("edit_file", {"edits": [{"old_text": "a a a", "new_text": "b"}], **defaults}, "a a a\n", 80_000)
    assert plan == ("b\n", "Applied 1 edit (1 replacement).\n\n")
    plan = plan_change("edit_file", {"old_text": "a a a", "new_text": "b", "edits": None}, "a a a\n", 80_000)
    assert plan == ("b\n", "")


async def test_edit_file_needs_an_existing_file(runtime):
    manager, session, project = runtime
    output, event = await tool(manager, session, "edit_file", {"path": "missing.txt", "old_text": "a", "new_text": "b"})
    assert "edit_file needs an existing file; use write_file to create it." in output
    assert event["state"] == "error" and not (project / "missing.txt").exists()


# --- The file write limit -----------------------------------------------------------------

def test_write_limit_setting_accepts_16_to_1000_kb():
    assert write_limit_setting({}) == 80_000
    assert write_limit_setting({"write_limit_kb": 200}) == 200_000
    for value in (15, 1001, "200", 200.0, True, None):
        assert write_limit_setting({"write_limit_kb": value}) == 80_000
    assert WorkspaceTools("/srv/x", write_limit=5).write_limit == 16_000
    assert WorkspaceTools("/srv/x", write_limit=5_000_000).write_limit == 1_000_000


async def test_the_write_limit_setting_governs_new_content_but_never_review_or_undo(runtime):
    manager, session, project = runtime
    big = "".join(f"line {number:06d} of a large generated file\n" for number in range(4_000))  # about 150 KB
    assert 140_000 < len(big) < 160_000
    await manager.event(session, "user", text="Write it")
    output, event = await tool(manager, session, "write_file", {"path": "big.txt", "content": big}, "default")
    assert event["state"] == "error" and "at most 80 KB" in output and not (project / "big.txt").exists()

    manager.settings.values["write_limit_kb"] = 200
    output, event = await tool(manager, session, "write_file", {"path": "big.txt", "content": big}, "allowed")
    assert event["state"] == "completed", output
    assert (project / "big.txt").read_text() == big

    manager.settings.values["write_limit_kb"] = 16
    tools = tools_for(manager, session)
    diff = manager.changes.diff(session["id"], tools, "big.txt")
    assert diff["status"] == "created" and diff["added"] == 4_000
    current = manager.changes.list(session["id"], tools)["files"][0]["current_hash"]
    manager.changes.act(session["id"], tools, "undo", [{"path": "big.txt", "current_hash": current}])
    assert not (project / "big.txt").exists()
    # The editor follows the setting.
    (project / "medium.txt").write_text("m" * 20_000)
    with pytest.raises(ValueError, match=r"too large for the text editor \(16 KB limit\)"):
        tools.read_file("medium.txt")


async def test_activity_log_reports_the_budget_of_the_current_write_limit(runtime):
    manager, session, project = runtime
    manager.settings.values["write_limit_kb"] = 16
    (project / "handoff.md").write_text("h" * 15_500)
    await tool(manager, session, "read_file", {"path": "handoff.md"}, "read")
    output, event = await tool(manager, session, "insert_activity_log", {"path": "handoff.md"}, "log")
    assert event["state"] == "error"
    assert "too close to the 16 KB limit for an activity log" in output


async def test_new_files_get_the_umask_mode_and_existing_files_keep_theirs(runtime):
    manager, session, project = runtime
    umask = os.umask(0)
    os.umask(umask)
    await tool(manager, session, "write_file", {"path": "new/file.txt", "content": "hello\n"})
    assert stat.S_IMODE((project / "new" / "file.txt").stat().st_mode) == 0o666 & ~umask
    (project / "script.sh").write_text("echo\n")
    (project / "script.sh").chmod(0o700)
    await tool(manager, session, "edit_file", {"path": "script.sh", "old_text": "echo", "new_text": "echo hi"})
    assert stat.S_IMODE((project / "script.sh").stat().st_mode) == 0o700


def test_edit_file_definition_offers_replace_all_and_edits():
    from local_agent.tools import TOOL_DEFINITIONS
    definition = next(item["function"] for item in TOOL_DEFINITIONS if item["function"]["name"] == "edit_file")
    parameters = definition["parameters"]
    assert parameters["required"] == ["path"]
    assert parameters["properties"]["edits"]["maxItems"] == 50
    assert parameters["properties"]["edits"]["items"]["additionalProperties"] is False
    assert "Line endings (LF or CRLF) are kept." in definition["description"]
    json.dumps(parameters)  # Serializable for the request.


# --- Large files: diffs stay fast, results stay bounded ------------------------------------

class Page:
    """An open conversation page: execute_tool then publishes the change review to it."""
    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)


def valid_opcodes(old, new, codes):
    i = j = 0
    for tag, i1, i2, j1, j2 in codes:
        assert (i1, j1) == (i, j)
        if tag == "equal":
            assert old[i1:i2] == new[j1:j2]
        i, j = i2, j2
    assert (i, j) == (len(old), len(new))


def test_line_opcodes_match_difflib_on_small_inputs_and_stay_fast_on_large_ones():
    generator = random.Random(7)
    for _ in range(300):
        old = [generator.choice("abcd") + "\n" for _ in range(generator.randint(0, 12))]
        new = [generator.choice("abcd") + "\n" for _ in range(generator.randint(0, 12))]
        assert line_opcodes(old, new) == difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
    renamed = [f"name_{i} = {i}\n" if i % 2 else f"keep {i}\n" for i in range(20_000)]
    blank = ["\n"] * 50_000
    edited_blank = blank[:100] + ["x\n", "y\n"] + blank[101:45_000] + ["z\n"] + blank[45_001:]
    cases = {
        "every other line changed": (renamed, [line.replace("name_", "renamed_") for line in renamed]),
        "identical lines, two distant changes": (blank, edited_blank),
        "nothing in common": ([f"{i}\n" for i in range(20_000)], [f"other {i}\n" for i in range(20_003)]),
    }
    for label, (old, new) in cases.items():
        started = time.monotonic()
        codes = line_opcodes(old, new)
        assert time.monotonic() - started < 1, label
        valid_opcodes(old, new, codes)
    changed = [code for code in line_opcodes(blank, edited_blank) if code[0] != "equal"]
    assert len(changed) == 2  # Two small changes, not one block of 45,000 lines.


async def test_scattered_changes_in_a_large_file_do_not_block_the_server(runtime):
    manager, session, project = runtime
    manager.settings.values["write_limit_kb"] = 1000
    manager.listeners[session["id"]] = {page := Page()}
    # A name on every other line of a 400 KB file: difflib alone is quadratic on this.
    text = "".join(f"value_{i} = old_name({i})\n" if i % 2 else f"# note {i}\n" for i in range(20_000))
    assert len(text) > 400_000
    (project / "big.py").write_text(text)
    started = time.monotonic()
    output, event = await tool(manager, session, "edit_file",
                               {"path": "big.py", "old_text": "old_name", "new_text": "new_name", "replace_all": True})
    assert time.monotonic() - started < 3
    assert event["state"] == "completed"
    assert output.startswith("Applied 1 edit (10000 replacements).")
    assert (project / "big.py").read_text() == text.replace("old_name", "new_name")
    review = next(item["changes"] for item in page.sent if item["type"] == "changes")
    assert review["files"][0]["added"] == review["files"][0]["removed"] == 10_000
    tools = tools_for(manager, session)
    started = time.monotonic()
    diff = manager.changes.diff(session["id"], tools, "big.py")
    assert time.monotonic() - started < 2 and len(diff["hunks"]) == 10_000
    # Recovery's preview of the same checkpoint is fast too.
    [record] = manager.checkpoints.list(session["id"])
    started = time.monotonic()
    preview = manager.checkpoints.preview(record["id"], tools, session["id"])
    assert time.monotonic() - started < 2 and preview["can_restore"] is True


async def test_the_model_gets_a_bounded_diff_and_the_review_shows_all_of_it(runtime):
    manager, session, project = runtime
    text = "".join(f"counter_{i} += step\n" for i in range(3_000))
    (project / "loop.py").write_text(text)
    output, _ = await tool(manager, session, "edit_file",
                           {"path": "loop.py", "old_text": "step", "new_text": "increment", "replace_all": True})
    assert output.startswith("Applied 1 edit (3000 replacements).\n\n--- loop.py")
    assert len(output.encode()) < 8_300
    assert output.endswith("more lines); the change review shows it in full.")
    assert "Diagnostics" not in output
    diff = manager.changes.diff(session["id"], tools_for(manager, session), "loop.py")
    assert diff["added"] == diff["removed"] == 3_000
    # A small edit's result is unchanged.
    output, _ = await tool(manager, session, "edit_file",
                           {"path": "loop.py", "old_text": "counter_7 +=", "new_text": "counter_7 -="}, "small")
    assert "truncated" not in output
    assert changed_lines(output) == (["-counter_7 += increment"], ["+counter_7 -= increment"])


async def test_an_edit_to_a_file_already_over_the_limit_names_its_size_and_the_setting(runtime):
    manager, session, project = runtime
    (project / "large.py").write_text("UNIQUE = 1\n" + "# padding line\n" * 8_000)
    output, event = await tool(manager, session, "edit_file", {"path": "large.py", "old_text": "UNIQUE = 1", "new_text": "UNIQUE = 2"})
    assert event["state"] == "error"
    assert "The file is 120 KB, over the file write limit (80 KB, set in Settings)." in output
    assert "run_command" in output and "UNIQUE = 1" in (project / "large.py").read_text()
    # New content over the limit keeps the content message.
    (project / "small.txt").write_text("x\n")
    with pytest.raises(ValueError, match="at most 80 KB"):
        plan_change("edit_file", {"old_text": "x", "new_text": "y" * 90_000}, "x\n", 80_000)


def test_the_activity_log_names_the_limit_it_was_trimmed_to(tmp_path):
    events = [{"id": str(number), "type": "tool", "name": "run_command", "state": "completed", "created": 1790238000.0,
               "input": {"command": f"echo {number} " + "x" * 200}} for number in range(200)]
    content, stats = activity_content({"id": "s", "workspace": str(tmp_path), "events": events}, None, limit=16_000)
    assert stats["commands_omitted"] > 0 and len(content.encode()) <= 16_000
    assert f"{stats['commands_omitted']} earlier commands were omitted to stay within the 16 KB file limit." in content
