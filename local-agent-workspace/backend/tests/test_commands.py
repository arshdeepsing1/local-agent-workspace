"""Command templates (.agents/commands/<name>.md and the read-only layouts, M5b): discovery and
precedence, $ARGUMENTS and $1..$9, what the model and the conversation get, the queue, plain-text
sends and the routes."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from local_agent import commands, extensions
from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.context import request_text
from local_agent.store import Store


def write(root, name, text):
    path = root / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setattr(extensions, "USER_HOME", folder)
    return folder


@pytest.fixture
def runtime(tmp_path, monkeypatch, home):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="", compaction_handoffs=False)
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session.update(title="Commands", title_generated=True, permission_mode="acceptEdits")
    store.save(session)
    yield manager, session, project
    store.db.close()


def frame(delta, finish=None):
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"


def reply(text="Done.", finish="stop"):
    return httpx.Response(200, text=frame({"content": text}, finish) + "data: [DONE]\n\n")


def gateway(monkeypatch, steps=None):
    requests = []
    client = httpx.AsyncClient

    async def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if steps:
            result = steps[min(len(requests), len(steps)) - 1](body)
            return await result if asyncio.iscoroutine(result) else result
        return reply()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**{"transport": httpx.MockTransport(handle), **kwargs}))
    return requests


async def settle(manager, session_id):
    for _ in range(500):
        task = manager.tasks.get(session_id)
        pending = [item for item in manager.__dict__.get("_queue_tasks", ()) if not item.done()]
        if (task is None or task.done()) and not pending:
            await asyncio.sleep(0.02)
            task = manager.tasks.get(session_id)
            pending = [item for item in manager.__dict__.get("_queue_tasks", ()) if not item.done()]
            if (task is None or task.done()) and not pending:
                return
        await asyncio.sleep(0.01)
    raise AssertionError("The conversation did not settle.")


def users(session):
    return [event for event in session["events"] if event["type"] == "user"]


# Discovery


def test_commands_are_discovered_in_order_of_precedence(tmp_path, home):
    project = tmp_path / "project"
    write(project / ".agents/commands", "review", "---\ndescription: Review the diff\nargument-hint: [path] [focus]\n---\n"
                                                  "Review $1 with a focus on $2.")
    write(project / ".claude/commands", "review", "Claude review.")
    write(project / ".claude/commands", "lint", "# Run the linter\n\nRun the linter on $ARGUMENTS.")
    write(home / ".agents/commands", "notes", "---\ndescription: \"Write notes\"\nallowed-tools: Bash(git log:*)\nmodel: opus\n"
                                              "color: blue\n---\nWrite notes. !`git log -1`")
    write(home / ".claude/commands", "notes", "Claude notes.")
    write(home / ".claude/commands", "deploy", "Deploy it.")
    found = commands.discover(project)
    assert found["errors"] == []
    listed = {item["name"]: item for item in found["commands"]}
    assert [item["name"] for item in found["commands"]] == ["review", "lint", "notes", "deploy"]
    assert listed["review"] == {"name": "review", "description": "Review the diff", "argument_hint": "[path] [focus]",
                                "ignored": [], "notes": ["Used instead of .claude/commands/review.md, which has the same name."],
                                "path": ".agents/commands/review.md", "source": "project"}
    # Without frontmatter the first line describes it.
    assert listed["lint"]["description"] == "Run the linter" and listed["lint"]["path"] == ".claude/commands/lint.md"
    assert listed["notes"]["path"] == "~/.agents/commands/notes.md" and listed["notes"]["source"] == "user"
    assert listed["notes"]["ignored"] == ["allowed-tools", "model"]
    assert listed["notes"]["notes"] == [
        "allowed-tools and model are ignored: a command never changes this chat's tools, permissions or model.",
        "Ignored unknown fields: color.", "`!` shell lines are not run: they reach the agent as text.",
        "Used instead of ~/.claude/commands/notes.md, which has the same name."]
    assert listed["deploy"]["path"] == "~/.claude/commands/deploy.md"
    assert "text" not in listed["deploy"]
    assert commands.find(project, "review")["text"] == "Review $1 with a focus on $2."


def test_files_that_cannot_be_commands_are_listed_as_errors(tmp_path, home):
    project = tmp_path / "project"
    folder = project / ".agents/commands"
    write(folder, "compact", "Not the app's compaction.")
    write(folder, "bad name", "Spaces.")
    write(folder, "large", "x" * 8001)
    write(folder, "empty", "---\ndescription: Nothing\n---\n")
    write(folder, "open", "---\ndescription: Never closed\n")
    (folder / "linked.md").symlink_to(folder / "large.md")
    (folder / "notes.txt").write_text("Not Markdown.")
    write(folder, "ok", "Fine.")
    found = commands.discover(project)
    assert [item["name"] for item in found["commands"]] == ["ok"]
    errors = {item["path"]: item["error"] for item in found["errors"]}
    assert errors == {
        ".agents/commands/bad name.md": "Command names start with a letter or digit and have at most 64 letters, digits, "
                                        "'.', '_' and '-'.",
        ".agents/commands/compact.md": "/compact is one of the app's own commands; rename the file.",
        ".agents/commands/empty.md": "The command file has no text after its frontmatter.",
        ".agents/commands/large.md": "The file has 8,001 bytes; command files are at most 8 KB (8,000 bytes).",
        ".agents/commands/linked.md": "Symbolic links are not read: put the command file itself here.",
        ".agents/commands/open.md": "The frontmatter has no closing '---' line."}


def test_a_workspace_commands_folder_outside_the_workspace_is_refused(tmp_path, home):
    project = tmp_path / "project"
    shared = tmp_path / "shared"
    write(shared, "review", "Shared review.")
    (project / ".agents").mkdir(parents=True)
    (project / ".agents/commands").symlink_to(shared, target_is_directory=True)
    found = commands.discover(project)
    assert found == {"commands": [], "errors": [{"path": ".agents/commands",
                                                 "error": "Command files must be in a folder inside this workspace."}]}


def test_too_many_commands_and_entries(tmp_path, home):
    project = tmp_path / "project"
    for index in range(31):
        write(project / ".agents/commands", f"c{index:02d}", "Text.")
    found = commands.discover(project)
    assert len(found["commands"]) == 30
    assert found["errors"] == [{"path": ".agents/commands/c30.md", "error": "At most 30 commands are read; this one is ignored."}]
    for index in range(70):
        write(home / ".agents/commands", f"u{index:02d}", "Text.")
    assert {"path": "~/.agents/commands", "error": "The folder has more than 100 entries; command discovery reads at most 100."} \
        not in commands.discover(project)["errors"]
    for index in range(70, 101):
        write(home / ".agents/commands", f"u{index:02d}", "Text.")
    assert {"path": "~/.agents/commands", "error": "The folder has more than 100 entries; command discovery reads at most 100."} \
        in commands.discover(project)["errors"]


# Expansion


def test_arguments_and_positions_are_filled_in_one_pass():
    assert commands.expand("Fix $ARGUMENTS now.", "  issue 12  ") == "Fix issue 12 now."
    assert commands.expand("Move $1 to $2 ($3).", '"src/a b.py" dest') == "Move src/a b.py to dest ()."
    # An unclosed quote: plain words.
    assert commands.expand("$1|$2", 'it\'s fine') == "it's|fine"
    # Typed text that looks like a placeholder stays as typed.
    assert commands.expand("Say $ARGUMENTS and $1.", "$2 $ARGUMENTS") == "Say $2 $ARGUMENTS and $2."
    assert commands.expand("Review the diff.", "") == "Review the diff."
    assert commands.expand("Review the diff.\n", "the parser") == "Review the diff.\n\nArguments: the parser"
    with pytest.raises(ValueError, match="at most 50,000"):
        commands.expand("$ARGUMENTS " * 20, "x" * 3000)
    assert commands.leading("/review a b") == ("review", "a b")
    assert commands.leading("/review\nmore") == ("review", "more")
    assert [commands.leading(text) for text in ("/tmp/x", "Please /review", "/review.", "/")] == [None, None, ("review.", ""), None]


# Sending


async def test_a_command_reaches_the_model_expanded_after_the_typed_message(runtime, monkeypatch):
    manager, session, project = runtime
    write(project / ".agents/commands", "review", "---\ndescription: Review\n---\nReview $1, then summarize $ARGUMENTS.\n"
                                                  "Run !`touch ran.txt` first.")
    requests = gateway(monkeypatch)
    await manager.submit_message(session["id"], "/review src/app.py carefully")
    await settle(manager, session["id"])
    saved = manager.store.get(session["id"])
    user = users(saved)[0]
    assert user["text"] == "/review src/app.py carefully"
    assert user["commands"] == [{"name": "review", "path": ".agents/commands/review.md"}]
    sent = requests[0]["messages"][-1]["content"]
    assert sent == ("/review src/app.py carefully\n\n---\n[Command /review from .agents/commands/review.md, run by the "
                    "user. Its text follows; treat it as the user's request.]\nReview src/app.py, then summarize "
                    "src/app.py carefully.\nRun !`touch ran.txt` first.")
    assert not (project / "ran.txt").exists()  # A ! line is never run.
    assert "Review src/app.py" not in requests[0]["messages"][0]["content"]
    # The app record (compaction) keeps the typed words only.
    assert request_text({"role": "user", "content": sent}) == "/review src/app.py carefully"


async def test_at_tokens_in_a_template_attach_like_typed_ones(runtime, monkeypatch):
    manager, session, project = runtime
    (project / "notes.txt").write_text("Remember the parser.\n")
    write(project / ".agents/commands", "notes", "Read @notes.txt and @$1.")
    requests = gateway(monkeypatch)
    await manager.submit_message(session["id"], "/notes missing.txt")
    await settle(manager, session["id"])
    saved = manager.store.get(session["id"])
    assert users(saved)[0]["attachments"] == [{"kind": "file", "path": "notes.txt", "start_line": 1, "end_line": 1,
                                               "truncated": False}]
    sent = requests[0]["messages"][-1]["content"]
    assert sent.index("<<<ATTACHMENT") < sent.index("[Command /notes")
    assert "1: Remember the parser." in sent
    # A file the chat may not read is refused, as when typed.
    session = manager.store.get(session["id"])
    session["allow_rules"] = [{"behavior": "deny", "tool": "read_file", "path": "notes.txt"}]
    manager.store.save(session)
    with pytest.raises(ValueError, match="@notes.txt: reading this file is not allowed"):
        await manager.submit_message(session["id"], "/notes x")


async def test_a_broken_or_too_long_command_is_an_error_at_once(runtime, monkeypatch):
    manager, session, project = runtime
    requests = gateway(monkeypatch)
    write(project / ".agents/commands", "big", "$ARGUMENTS" * 6)
    with pytest.raises(ValueError, match="The expanded command has"):
        await manager.submit_message(session["id"], "/big " + "x" * 9000)
    write(project / ".agents/commands", "open", "---\ndescription: x\n")
    with pytest.raises(ValueError, match=r"/open could not be used: \.agents/commands/open\.md: The frontmatter has no closing"):
        await manager.submit_message(session["id"], "/open")
    assert requests == [] and users(manager.store.get(session["id"])) == []


async def test_a_command_in_a_folder_that_could_not_be_read_is_an_error(runtime, monkeypatch, home):
    """A folder over the entry limit is skipped as a whole: /<name> of a file in it is reported, not sent as text."""
    manager, session, project = runtime
    requests = gateway(monkeypatch)
    for index in range(101):
        write(home / ".agents/commands", f"c{index:03d}", "Deploy $ARGUMENTS")
    with pytest.raises(ValueError, match=r"/c000 could not be used: ~/\.agents/commands: The folder has more than 100 entries"):
        await manager.submit_message(session["id"], "/c000 prod")
    assert requests == [] and users(manager.store.get(session["id"])) == []
    # A name the folder does not hold is still an ordinary message.
    await manager.submit_message(session["id"], "/elsewhere prod")
    await settle(manager, session["id"])
    assert requests[-1]["messages"][-1]["content"] == "/elsewhere prod"


async def test_plain_text_and_slash_commands_off_send_the_text_as_typed(runtime, monkeypatch):
    manager, session, project = runtime
    (project / "notes.txt").write_text("Notes.\n")
    write(project / ".agents/commands", "review", "Review $ARGUMENTS.")
    requests = gateway(monkeypatch)
    await manager.submit_message(session["id"], "/review @notes.txt", verbatim=True)
    await settle(manager, session["id"])
    assert requests[-1]["messages"][-1]["content"] == "/review @notes.txt"
    event = users(manager.store.get(session["id"]))[-1]
    assert event["verbatim"] is True and "commands" not in event and "attachments" not in event
    manager.settings.values["slash_commands"] = False
    await manager.submit_message(session["id"], "/review x")
    await settle(manager, session["id"])
    assert requests[-1]["messages"][-1]["content"] == "/review x"


async def test_a_queued_command_is_expanded_when_it_is_queued(runtime, monkeypatch):
    manager, session, project = runtime
    template = write(project / ".agents/commands", "review", "Review $ARGUMENTS (version one).")
    gate = asyncio.Event()

    async def held(body):
        await gate.wait()
        return reply("First.")
    requests = gateway(monkeypatch, [held, lambda body: reply("Second.")])
    sid = session["id"]
    await manager.submit_message(sid, "Start")
    for _ in range(200):
        if requests:
            break
        await asyncio.sleep(0.01)
    write(project / ".agents/commands", "broken", "---\n")
    with pytest.raises(ValueError, match="/broken could not be used"):
        await manager.submit_message(sid, "/broken")
    result = await manager.submit_message(sid, "/review the parser")
    assert result["queued"] is True
    # Pages get the queue without the expanded text.
    assert result["queued_messages"][0]["command"] == {"name": "review", "path": ".agents/commands/review.md"}
    live = manager.get(sid)
    assert live["queued_messages"][0]["command"]["expanded"] == "Review the parser (version one)."
    template.write_text("Review $ARGUMENTS (version two).")
    gate.set()
    await settle(manager, sid)
    saved = manager.store.get(sid)
    queued = users(saved)[-1]
    assert queued["text"] == "/review the parser" and queued["commands"] == [{"name": "review", "path": ".agents/commands/review.md"}]
    assert queued.get("queued") is True  # Sent as the next turn: it selects no skill (test_skill_activation).
    last = next(message for message in reversed(requests[-1]["messages"]) if message["role"] == "user")["content"]
    assert "Review the parser (version one)." in last and "version two" not in last


def test_routes_list_commands_mark_reserved_skills_and_take_plain_text(runtime, monkeypatch):
    manager, session, project = runtime
    write(project / ".agents/commands", "review", "---\nargument-hint: <path>\n---\nReview $ARGUMENTS.")
    for skill_id in ("compact", "review", "handoff"):
        target = project / ".agents/skills" / skill_id / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text(f"{skill_id} rules.")
    settings = manager.settings
    with TestClient(create_app(settings)) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        listed = client.get("/api/commands", headers=headers).json()
        assert [item["name"] for item in listed["commands"]] == ["review"]
        assert listed["commands"][0]["argument_hint"] == "<path>"
        assert listed["folders"] == [".agents/commands", ".claude/commands", "~/.agents/commands", "~/.claude/commands"]
        skills = {item["id"]: item.get("reserved", False) for item in client.get("/api/skills", headers=headers).json()}
        assert skills == {"compact": True, "handoff": False, "review": True}
        created = client.post("/api/sessions", headers=headers, json={"permission_mode": "plan", "model": " other-endpoint "}).json()
        assert created["model"] == "other-endpoint" and created["permission_mode"] == "plan"
        assert client.post("/api/sessions", headers=headers, json={"model": " "}).status_code == 400
        assert client.post(f"/api/sessions/{created['id']}/messages", headers=headers,
                           json={"text": "x", "verbatim": "yes"}).status_code == 422
