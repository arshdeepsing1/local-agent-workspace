"""move_path and delete_path: checkpoints, review and Undo, guards, permissions and logs."""
import os

import pytest

from local_agent.activity import activity_log
from local_agent.agents import AgentManager
from local_agent.approvals import rule_for
from local_agent.config import Settings
from local_agent.instructions import load_project_instructions
from local_agent.permissions import tool_decision
from local_agent.store import Store
from local_agent.transcript import transcript

NOTE = "first line\nsecond line\n"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / "note.txt").write_text(NOTE)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    store = Store(settings.state_dir / "ops.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session["permission_mode"] = "acceptEdits"
    tools = manager.session_tools(session)
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance["text"], guidance["warnings"])
    tools.skill_signature = ""
    cards = []

    async def approve(session, event):
        cards.append((event["name"], event.get("preview", "")))
        return True
    manager.approve = approve
    yield manager, session, project, tools, cards
    store.db.close()


async def tool(runtime, name, arguments, call_id="call"):
    manager, session, _, tools, _ = runtime
    output = await manager.execute_tool(session, tools, name, arguments, call_id)
    return output, next(item for item in reversed(session["events"]) if item.get("call_id") == call_id)


async def read(runtime, path="note.txt"):
    return await tool(runtime, "read_file", {"path": path}, f"read-{path}")


async def test_move_creates_and_deletes_with_checkpoints_and_undo_restores_both(runtime):
    manager, session, project, tools, cards = runtime
    await manager.event(session, "user", text="Rename it")
    output, event = await tool(runtime, "move_path", {"path": "note.txt", "destination": "docs/renamed.txt"})
    assert output == "Moved note.txt to docs/renamed.txt." and event["state"] == "completed"
    assert not (project / "note.txt").exists() and (project / "docs" / "renamed.txt").read_text() == NOTE
    assert cards == []  # Accept edits runs it without a card.
    records = manager.checkpoints.list(session["id"])
    assert sorted((item["path"], item["before_exists"], item["after_exists"]) for item in records) == [
        ("docs/renamed.txt", False, True), ("note.txt", True, False)]
    assert all(item["turn_id"] for item in records)
    # The conversation remembers the new path, not the old one.
    assert str(project / "docs" / "renamed.txt") in session["file_views"]
    assert str(project / "note.txt") not in session["file_views"]

    files = manager.changes.list(session["id"], tools)["files"]
    assert {item["path"]: item["status"] for item in files} == {"docs/renamed.txt": "created", "note.txt": "deleted"}
    manager.changes.act(session["id"], tools, "undo",
                        [{"path": item["path"], "current_hash": item["current_hash"]} for item in files])
    assert (project / "note.txt").read_text() == NOTE and not (project / "docs" / "renamed.txt").exists()


async def test_delete_needs_a_read_and_undo_restores_the_file(runtime):
    manager, session, project, tools, _ = runtime
    output, event = await tool(runtime, "delete_path", {"path": "note.txt"}, "unread")
    assert event["state"] == "rejected"
    assert output.startswith("Read note.txt before deleting it: this conversation has not read it yet.")
    assert (project / "note.txt").exists()

    await read(runtime)
    output, event = await tool(runtime, "delete_path", {"path": "note.txt"}, "delete")
    assert output == "Deleted note.txt. Undo restores it from the change review." and event["state"] == "completed"
    assert not (project / "note.txt").exists()
    assert str(project / "note.txt") not in session["file_views"]
    [entry] = manager.changes.list(session["id"], tools)["files"]
    assert entry["status"] == "deleted" and entry["removed"] == 2
    manager.changes.act(session["id"], tools, "undo", [{"path": "note.txt", "current_hash": entry["current_hash"]}])
    assert (project / "note.txt").read_text() == NOTE


async def test_manual_mode_asks_once_with_a_preview_and_plan_mode_denies(runtime):
    manager, session, project, tools, cards = runtime
    session["permission_mode"] = "manual"
    (project / "old.txt").write_text("old\n")
    await read(runtime, "old.txt")
    await tool(runtime, "move_path", {"path": "note.txt", "destination": "old.txt", "overwrite": True})
    await read(runtime, "old.txt")
    await tool(runtime, "delete_path", {"path": "old.txt"}, "delete")
    assert [name for name, _ in cards] == ["move_path", "delete_path"]
    assert cards[0][1].startswith("Move note.txt → old.txt\n\nReplaces the existing file:\n")
    assert "-old\n+first line\n+second line" in cards[0][1]
    assert cards[1][1].startswith("Delete old.txt (2 lines)\n\n") and "-second line" in cards[1][1]
    assert not (project / "old.txt").exists() and not (project / "note.txt").exists()

    session["permission_mode"] = "plan"
    (project / "keep.txt").write_text("keep\n")
    output, event = await tool(runtime, "delete_path", {"path": "keep.txt"}, "plan")
    assert event["state"] == "rejected" and "Plan mode is read-only" in output
    assert (project / "keep.txt").exists() and len(cards) == 2


def test_permission_modes_and_the_edits_rule_cover_move_and_delete():
    for name in ("move_path", "delete_path", "notebook_edit"):
        assert tool_decision("acceptEdits", name, {}) == "allow"
        assert tool_decision("auto", name, {}) == "allow"
        assert tool_decision("manual", name, {}) == "ask"
        assert tool_decision("plan", name, {}) == "deny"
        assert tool_decision("bypassPermissions", name, {}) == "allow"
        assert rule_for(name, {"path": "x"}) == {"tool": "edits"}
    assert tool_decision("plan", "notebook_read", {}) == "allow"


async def test_the_edits_rule_allows_a_move_without_a_card(runtime):
    manager, session, project, tools, cards = runtime
    session["permission_mode"] = "manual"
    session["allow_rules"] = [{"tool": "edits"}]
    output, event = await tool(runtime, "move_path", {"path": "note.txt", "destination": "moved.txt"})
    assert output == "Moved note.txt to moved.txt." and event.get("allowed_by_rule") is True
    assert cards == []


async def test_an_existing_destination_needs_overwrite_and_a_read(runtime):
    manager, session, project, tools, _ = runtime
    (project / "taken.txt").write_text("taken\n")
    output, event = await tool(runtime, "move_path", {"path": "note.txt", "destination": "taken.txt"}, "no-overwrite")
    assert "destination already exists; set overwrite=true to replace it (read it first)." in output
    output, event = await tool(runtime, "move_path", {"path": "note.txt", "destination": "taken.txt", "overwrite": True}, "unread")
    assert event["state"] == "rejected" and output.startswith("Read taken.txt before overwriting it")
    assert (project / "taken.txt").read_text() == "taken\n" and (project / "note.txt").exists()
    assert manager.checkpoints.list(session["id"]) == []

    await read(runtime, "taken.txt")
    output, _ = await tool(runtime, "move_path", {"path": "note.txt", "destination": "taken.txt", "overwrite": True}, "ok")
    assert output == "Moved note.txt to taken.txt." and (project / "taken.txt").read_text() == NOTE


@pytest.mark.parametrize("name", ["move_path", "delete_path"])
async def test_a_change_during_the_approval_wait_is_refused(runtime, name):
    manager, session, project, tools, _ = runtime
    session["permission_mode"] = "manual"
    await read(runtime)

    async def approve(session, event):
        (project / "note.txt").write_text("changed by the user\n")
        return True
    manager.approve = approve
    arguments = {"path": "note.txt", **({"destination": "moved.txt"} if name == "move_path" else {})}
    output, event = await tool(runtime, name, arguments)
    assert event["state"] == "error" and "The file changed while waiting for approval." in output
    assert (project / "note.txt").read_text() == "changed by the user\n" and not (project / "moved.txt").exists()
    assert manager.checkpoints.list(session["id"]) == []


@pytest.mark.parametrize("name", ["move_path", "delete_path"])
async def test_folders_binary_files_missing_files_and_same_paths_are_refused(runtime, name):
    manager, session, project, tools, _ = runtime
    (project / "folder").mkdir()
    (project / "folder" / "inside.txt").write_text("x\n")
    (project / "image.bin").write_bytes(b"\x89PNG\x00\x01")
    verb = "moves" if name == "move_path" else "deletes"
    extra = {"destination": "elsewhere.txt"} if name == "move_path" else {}
    for path in ("folder", "image.bin"):
        output, _ = await tool(runtime, name, {"path": path, **extra}, path)
        assert f"{name} {verb} one regular UTF-8 text file within the file write limit" in output
        assert "use run_command for folders, binary or larger files." in output
    output, _ = await tool(runtime, name, {"path": "missing.txt", **extra}, "missing")
    assert "path does not exist." in output
    assert (project / "folder" / "inside.txt").exists() and (project / "image.bin").exists()
    if name == "move_path":
        output, _ = await tool(runtime, name, {"path": "note.txt", "destination": "./note.txt"}, "same")
        assert "path and destination are the same file." in output
        output, _ = await tool(runtime, name, {"path": "note.txt", "destination": "folder"}, "into-folder")
        assert "destination is a folder; give the full new file path." in output
    assert manager.checkpoints.list(session["id"]) == []


async def test_a_larger_file_than_the_write_limit_is_refused(runtime):
    manager, session, project, tools, _ = runtime
    (project / "large.txt").write_text("x" * 90_000)
    await read(runtime, "large.txt")
    output, _ = await tool(runtime, "delete_path", {"path": "large.txt"})
    assert "within the file write limit" in output and (project / "large.txt").exists()


@pytest.mark.parametrize("name", ["move_path", "delete_path"])
async def test_symbolic_links_are_refused_and_their_targets_stay_intact(runtime, name):
    manager, session, project, tools, _ = runtime
    os.symlink(project / "note.txt", project / "link.txt")
    await read(runtime, "link.txt")
    extra = {"destination": "moved.txt"} if name == "move_path" else {}
    output, event = await tool(runtime, name, {"path": "link.txt", **extra})
    assert event["state"] == "error"
    assert "path is a symbolic link; move_path and delete_path work on regular files only." in output
    assert (project / "link.txt").is_symlink() and (project / "note.txt").read_text() == NOTE
    if name == "move_path":
        os.symlink(project / "other.txt", project / "dangling.txt")
        output, _ = await tool(runtime, name, {"path": "note.txt", "destination": "dangling.txt", "overwrite": True}, "dest")
        assert "destination is a symbolic link" in output
        assert (project / "dangling.txt").is_symlink() and not (project / "other.txt").exists()
    assert manager.checkpoints.list(session["id"]) == []


async def test_an_external_destination_asks_for_folder_access(runtime, tmp_path):
    manager, session, project, tools, cards = runtime
    outside = tmp_path / "outside"
    outside.mkdir()

    async def decline_access(session, event):
        cards.append((event["name"], event.get("preview", "")))
        return event["name"] != "access_directory"
    manager.approve = decline_access
    output, event = await tool(runtime, "move_path", {"path": "note.txt", "destination": str(outside / "note.txt")})
    assert [name for name, _ in cards] == ["access_directory"]
    assert str(outside) in cards[0][1]
    assert event["state"] == "rejected" and "folder access" in output
    assert (project / "note.txt").exists() and not (outside / "note.txt").exists()


async def test_credential_files_are_excluded_at_both_ends(runtime):
    manager, session, project, tools, _ = runtime
    (project / ".env").write_text("TOKEN=x\n")
    output, _ = await tool(runtime, "move_path", {"path": "note.txt", "destination": ".env", "overwrite": True})
    assert "Credential files are excluded from file tools." in output
    output, _ = await tool(runtime, "delete_path", {"path": ".env"}, "delete")
    assert "Credential files are excluded from file tools." in output
    assert (project / ".env").read_text() == "TOKEN=x\n" and (project / "note.txt").exists()


async def test_activity_log_and_transcript_name_moves_and_deletes(runtime):
    manager, session, project, tools, _ = runtime
    await tool(runtime, "move_path", {"path": "note.txt", "destination": "moved.txt"}, "move")
    await read(runtime, "moved.txt")
    await tool(runtime, "delete_path", {"path": "moved.txt"}, "delete")
    log, _ = activity_log(session)
    assert f"**Moved** (`move_path`):\n- `{project / 'note.txt'}`" in log
    assert f"**Deleted** (`delete_path`):\n- `{project / 'moved.txt'}`" in log
    text = transcript(session)
    assert "- Moved `note.txt` to `moved.txt`" in text and "- Deleted `moved.txt`" in text
