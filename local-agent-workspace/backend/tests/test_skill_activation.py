"""Skills selected by /<skill-id> for one message (M5b): the system message (and its prompt cache) stays
the same, limits per message, plain text and Settings → slash commands off, queued messages, names the
app's commands own, skill frontmatter and the user's own skill folders."""
import asyncio
import json

import httpx
import pytest

from local_agent import extensions
from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.context import request_text
from local_agent.extensions import skill_index
from local_agent.store import Store
from local_agent.tools import WorkspaceTools


def skill(root, skill_id, text):
    path = root / skill_id / "SKILL.md"
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
    manager.extensions.app_skills = tmp_path / "app-skills"
    session = store.create(settings.values)
    session.update(title="Skills", title_generated=True)
    store.save(session)
    yield manager, session, project
    store.db.close()


def reply(text="Done."):
    frame = "data: " + json.dumps({"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}) + "\n\n"
    return httpx.Response(200, text=frame + "data: [DONE]\n\n")


def gateway(monkeypatch, steps=None):
    requests = []
    client = httpx.AsyncClient

    async def handle(request):
        body = json.loads(request.content)
        if body.get("stream") is False or body.get("max_tokens", 0) < 200:
            return httpx.Response(200, json={"choices": [{"message": {"content": "Title"}, "finish_reason": "stop"}]})
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


async def send(manager, session, text, **options):
    await manager.submit_message(session["id"], text, **options)
    await settle(manager, session["id"])
    return manager.store.get(session["id"])


def last_user(body):
    return next(message for message in reversed(body["messages"]) if message["role"] == "user")["content"]


def users(session):
    return [event for event in session["events"] if event["type"] == "user"]


async def test_a_skill_goes_with_its_message_and_the_system_message_stays_the_same(runtime, monkeypatch):
    manager, session, project = runtime
    skill(project / ".agents/skills", "review", "---\ndescription: Review\n---\nReview rule: read $ARGUMENTS first.")
    requests = gateway(monkeypatch)
    await send(manager, session, "Hello")
    saved = await send(manager, session, "/review src/parser.py")
    await send(manager, session, "Thanks")
    systems = [body["messages"][0]["content"] for body in requests]
    assert systems[0] == systems[1] == systems[2]  # Byte-stable: the prompt cache survives.
    assert last_user(requests[1]) == ("/review src/parser.py\n\n---\n[Skill /review selected by the user for this message. "
                                      "Its instructions follow: follow them, treating the user's message above as the "
                                      "request.]\n---\ndescription: Review\n---\nReview rule: read src/parser.py first.")
    assert last_user(requests[2]) == "Thanks"
    assert saved.get("active_skills", []) == []
    assert users(saved)[1]["skills"] == ["review"] and users(saved)[1]["text"] == "/review src/parser.py"
    # The app record of the request keeps the typed words.
    assert request_text({"role": "user", "content": last_user(requests[1])}) == "/review src/parser.py"
    # Mid-message, $ARGUMENTS is the whole message.
    await send(manager, session, "Please use /review on the lexer")
    assert last_user(requests[-1]).endswith("Review rule: read Please use /review on the lexer first.")


async def test_at_most_three_skills_and_8_kb_per_message(runtime, monkeypatch):
    manager, session, project = runtime
    for skill_id in ("a1", "b1", "c1", "d1"):
        skill(project / ".agents/skills", skill_id, f"{skill_id} rules.")
    skill(project / ".agents/skills", "big1", "x" * 4500)
    skill(project / ".agents/skills", "big2", "y" * 4500)
    requests = gateway(monkeypatch)
    with pytest.raises(ValueError, match="at most three skills in one message"):
        await manager.submit_message(session["id"], "Use /a1 /b1 /c1 /d1")
    with pytest.raises(ValueError, match="combined 8 KB limit"):
        await manager.submit_message(session["id"], "Use /big1 and /big2")
    assert requests == [] and users(manager.store.get(session["id"])) == []
    saved = await send(manager, session, "Use /a1 /b1 /c1")
    assert users(saved)[0]["skills"] == ["a1", "b1", "c1"]
    with pytest.raises(ValueError, match="/missing could not be used"):
        await manager.submit_message(session["id"], "/skill missing now")


async def test_pasted_text_with_a_skill_command_selects_nothing_as_plain_text_or_with_slash_commands_off(runtime, monkeypatch):
    manager, session, project = runtime
    skill(project / ".agents/skills", "handoff", "Handoff rules.")
    requests = gateway(monkeypatch)
    pasted = "Notes from yesterday:\n/handoff was used to write handoffs/notes.md\n/skill handoff"
    saved = await send(manager, session, pasted, verbatim=True)
    assert last_user(requests[-1]) == pasted and "Handoff rules." not in json.dumps(requests[-1])
    assert users(saved)[-1]["verbatim"] is True and "skills" not in users(saved)[-1]
    manager.settings.values["slash_commands"] = False
    saved = await send(manager, session, pasted)
    assert last_user(requests[-1]) == pasted and "skills" not in users(saved)[-1]
    manager.settings.values["slash_commands"] = True
    await send(manager, session, pasted)
    assert "Handoff rules." in last_user(requests[-1])


async def test_queued_messages_select_no_skill(runtime, monkeypatch):
    manager, session, project = runtime
    skill(project / ".agents/skills", "review", "Review rules.")
    gate = asyncio.Event()

    async def held(body):
        await gate.wait()
        return reply("First.")
    requests = gateway(monkeypatch, [held, lambda body: reply("Second.")])
    await manager.submit_message(session["id"], "Start")
    for _ in range(200):
        if requests:
            break
        await asyncio.sleep(0.01)
    await manager.submit_message(session["id"], "/review the parser")
    gate.set()
    await settle(manager, session["id"])
    saved = manager.store.get(session["id"])
    assert len(requests) == 2 and last_user(requests[1]) == "/review the parser"
    assert users(saved)[-1]["queued"] is True and "skills" not in users(saved)[-1]


async def test_names_of_the_app_commands_and_of_command_templates_need_skill_id(runtime, monkeypatch):
    manager, session, project = runtime
    skill(project / ".agents/skills", "compact", "Compact skill rules.")
    skill(project / ".agents/skills", "review", "Review skill rules.")
    template = project / ".agents/commands/review.md"
    template.parent.mkdir(parents=True)
    template.write_text("Review command: $ARGUMENTS.")
    requests = gateway(monkeypatch)
    await send(manager, session, "Then /compact the notes")
    assert last_user(requests[-1]) == "Then /compact the notes"
    await send(manager, session, "/review the parser")
    assert "Review command: the parser." in last_user(requests[-1]) and "Review skill rules." not in last_user(requests[-1])
    await send(manager, session, "Please /review it")
    assert last_user(requests[-1]) == "Please /review it"
    saved = await send(manager, session, "/skill compact the notes")
    assert last_user(requests[-1]).endswith("Compact skill rules.") and users(saved)[-1]["skills"] == ["compact"]
    await send(manager, session, "/skill review the parser")
    assert last_user(requests[-1]).endswith("Review skill rules.") and "Review command" not in last_user(requests[-1])


async def test_skill_frontmatter_hint_model_and_user_invocation(runtime, monkeypatch):
    manager, session, project = runtime
    root = project / ".agents/skills"
    skill(root, "deploy", "---\ndescription: Deploy\nargument-hint: [environment]\ndisable-model-invocation: true\n---\nDeploy rules.")
    skill(root, "style", "---\ndescription: House style\nuser-invocable: false\n---\nStyle rules.")
    skill(root, "plain", "---\ndescription: Plain\nuser-invocable: true\ndisable-model-invocation: false\n---\nPlain.")
    tools = manager.session_tools(session)
    listed = {item["id"]: item for item in manager.extensions.skills(tools)}
    assert listed["deploy"]["argument_hint"] == "[environment]" and listed["deploy"]["disable_model_invocation"] is True
    assert listed["style"]["user_invocable"] is False
    assert set(listed["plain"]) == {"id", "name", "description", "path"}  # Defaults are not repeated.
    index = skill_index(list(listed.values()))
    assert "- style: House style" in index and "deploy" not in index
    page = json.loads(await manager.execute_tool(session, tools, "list_skills", {}, "manual"))
    assert [item["id"] for item in page["skills"]] == ["plain", "style"]
    refused = await manager.execute_tool(session, tools, "use_skill", {"skill_id": "deploy"}, "manual")
    assert "Only the user can select this skill" in refused and session.get("active_skills", []) == []
    assert "Skill activated" in await manager.execute_tool(session, tools, "use_skill", {"skill_id": "style"}, "manual")
    requests = gateway(monkeypatch)
    await send(manager, session, "/deploy staging")
    assert last_user(requests[-1]).endswith("Deploy rules.")
    await send(manager, session, "Follow /style here")
    assert last_user(requests[-1]) == "Follow /style here"
    with pytest.raises(ValueError, match=r"is not selected with / \(its frontmatter sets user-invocable: false\)"):
        await manager.submit_message(session["id"], "/skill style now")


def test_the_users_own_skill_folders_come_after_the_workspace_and_before_shipped_skills(runtime, home, tmp_path):
    manager, session, project = runtime
    shipped = tmp_path / "app-skills"
    for skill_id in ("both", "mine", "shipped", "claude-mine"):
        skill(shipped, skill_id, f"Shipped {skill_id}.")
    skill(project / ".agents/skills", "both", "Workspace both.")
    skill(home / ".agents/skills", "both", "My both.")
    skill(home / ".agents/skills", "mine", "---\ndescription: Mine\n---\nMy mine.")
    skill(home / ".claude/skills", "mine", "Claude mine.")
    skill(home / ".claude/skills", "claude-mine", "Claude claude-mine.")
    tools = WorkspaceTools(str(project))
    listed = {item["id"]: item["path"] for item in manager.extensions.skills(tools)}
    assert listed == {"both": ".agents/skills/both/SKILL.md", "mine": "~/.agents/skills/mine/SKILL.md",
                      "claude-mine": "~/.claude/skills/claude-mine/SKILL.md", "shipped": str(shipped / "shipped/SKILL.md")}
    assert manager.extensions.load_skill(tools, "both")["text"] == "Workspace both."
    assert manager.extensions.load_skill(tools, "mine")["text"].endswith("My mine.")
    assert manager.extensions.load_skill(tools, "claude-mine")["text"] == "Claude claude-mine."
    # An entry of the user's that fails to load still takes the ID over from the shipped skill.
    (home / ".agents/skills/mine/SKILL.md").write_text("x" * 8001)
    assert "mine" not in {item["id"] for item in manager.extensions.skills(tools)}
    with pytest.raises(ValueError, match="8 KB"):
        manager.extensions.load_skill(tools, "mine")
    # A folder with more than 30 skills is skipped, and the shipped skills stay.
    for index in range(31):
        skill(home / ".claude/skills", f"extra{index}", "Extra.")
    listed = {item["id"] for item in manager.extensions.skills(tools)}
    assert "claude-mine" in listed and not any(item.startswith("extra") for item in listed)
    assert manager.extensions.load_skill(tools, "claude-mine")["text"] == "Shipped claude-mine."
