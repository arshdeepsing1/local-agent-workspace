import asyncio
import json
import shlex
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_agent.extensions import ExtensionManager, validate_config
from local_agent.tools import WorkspaceTools


@pytest.fixture
def manager(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    return ExtensionManager(SimpleNamespace(state_dir=state, redact=lambda text: text.replace("secret-token", "[REDACTED]")),
                            app_skills=tmp_path / "no-shipped-skills")


def hook(command, **values):
    return {"id": "check", "event": "before_tool", "command": command, "timeout_seconds": 3, "enabled": True, **values}


def test_config_roundtrip_atomic_permissions_and_defensive_copies(manager):
    assert manager.public_config() == {"servers": [], "hooks": []}
    source = {"servers": [{"id": "test", "name": "Test", "transport": "stdio", "command": "program", "enabled": False}],
              "hooks": [hook("true")]}
    saved = manager.update_config(source)
    source["hooks"][0]["command"] = "changed input"
    saved["hooks"][0]["command"] = "changed output"
    assert manager.public_config()["hooks"][0]["command"] == "true"
    assert stat.S_IMODE(manager.path.stat().st_mode) == 0o600
    assert ExtensionManager(manager.settings).public_config() == manager.public_config()
    before = manager.path.read_text()
    with pytest.raises(ValueError):
        manager.update_config({"hooks": [hook("bad", timeout_seconds=31)]})
    assert manager.path.read_text() == before
    assert not list(manager.path.parent.glob("extensions-*.tmp"))


@pytest.mark.parametrize("config", [
    {"extra": True}, {"servers": [{}]}, {"servers": [{"id": "BAD"}]},
    {"hooks": [hook("true", event="other")]}, {"hooks": [hook("true", timeout_seconds=True)]},
    {"hooks": [hook("true", enabled="yes")]}, {"hooks": [hook("true"), hook("false")]},
    {"servers": [{"id": "test", "transport": "http", "url": "file:///tmp/server"}]},
    {"servers": [{"id": "test", "transport": "http", "url": "https://user:secret@example.com"}]},
    {"servers": [{"id": "test", "transport": "stdio", "command": "x", "env": {"SECRET": "value"}}]},
])
def test_config_validation(config):
    with pytest.raises(ValueError):
        validate_config(config)


def test_http_config_and_server_caps():
    server = {"id": "remote", "transport": "http", "url": "https://example.com/mcp", "enabled": True}
    assert validate_config({"servers": [server]})["servers"][0]["url"] == server["url"]
    with pytest.raises(ValueError, match="at most 4"):
        validate_config({"servers": [{**server, "id": f"remote{n}"} for n in range(5)]})


def test_hook_selectors_and_failure_phase_roundtrip(manager):
    manager.update_config({"hooks": [hook("true", event="tool_failure", tools=["write_file", "mcp__demo__echo"])]})
    assert manager.public_config()["hooks"][0]["tools"] == ["write_file", "mcp__demo__echo"]
    assert ExtensionManager(manager.settings).public_config() == manager.public_config()
    assert "tools" not in validate_config({"hooks": [hook("true")]})["hooks"][0]
    assert validate_config({"hooks": [hook("true", tools=[])]})["hooks"][0]["tools"] == []


@pytest.mark.parametrize("selectors", ["read_file", ["*"], ["read_.*"], ["read_file", "read_file"], [None], ["x" * 65], [f"tool{n}" for n in range(65)]])
def test_hook_selectors_require_unique_exact_tool_names(selectors):
    with pytest.raises(ValueError, match="exact tool names"):
        validate_config({"hooks": [hook("true", tools=selectors)]})


def test_skills_discover_workspace_agents_and_load_bounded_text(manager, tmp_path):
    tools = WorkspaceTools(str(tmp_path))
    folder = tmp_path / ".agents/skills/review"
    folder.mkdir(parents=True)
    text = "---\nname: Code review\ndescription: 'Review a proposed change'\n---\nRead the diff.\n"
    (folder / "SKILL.md").write_text(text)
    other = tmp_path / ".codex/skills/ignored"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("Not part of this skill catalog.")
    assert manager.skills(tools) == [{"id": "review", "name": "Code review", "description": "Review a proposed change",
                                     "path": ".agents/skills/review/SKILL.md"}]
    assert manager.load_skill(tools, "review")["text"] == text
    with pytest.raises(ValueError):
        manager.load_skill(tools, "../other")
    (folder / "SKILL.md").write_text("x" * 8001)
    with pytest.raises(ValueError, match="8 KB"):
        manager.load_skill(tools, "review")


def test_skills_reject_external_and_secret_symlink_targets(manager, tmp_path):
    workspace = tmp_path / "workspace"
    folder = workspace / ".agents/skills/linked"
    folder.mkdir(parents=True)
    external = tmp_path / "external.md"
    external.write_text("External instructions")
    target = folder / "SKILL.md"
    target.symlink_to(external)
    tools = WorkspaceTools(str(workspace), allowed_directories=[str(tmp_path)])
    assert manager.skills(tools) == []
    with pytest.raises(ValueError, match="inside this workspace"):
        manager.load_skill(tools, "linked")
    target.unlink()
    secret = workspace / ".env"
    secret.write_text("secret-token")
    target.symlink_to(secret)
    with pytest.raises(ValueError, match="Credential"):
        manager.load_skill(tools, "linked")


def test_shipped_skills_fill_in_ids_the_workspace_does_not_define(manager, tmp_path):
    shipped = tmp_path / "app/skills"
    manager.app_skills = shipped
    for skill_id, text in (("handoff", "---\ndescription: Write a handoff\n---\nShipped rules."), ("review", "Shipped review.")):
        (shipped / skill_id).mkdir(parents=True)
        (shipped / skill_id / "SKILL.md").write_text(text)
    workspace = tmp_path / "workspace"
    (workspace / ".agents/skills/review").mkdir(parents=True)
    (workspace / ".agents/skills/review/SKILL.md").write_text("Workspace review.")
    tools = WorkspaceTools(str(workspace))
    assert manager.skills(tools) == [
        {"id": "review", "name": "review", "description": "", "path": ".agents/skills/review/SKILL.md"},
        {"id": "handoff", "name": "handoff", "description": "Write a handoff", "path": str(shipped / "handoff/SKILL.md")}]
    assert manager.load_skill(tools, "handoff")["text"].endswith("Shipped rules.")
    assert manager.load_skill(tools, "review")["text"] == "Workspace review."
    # A workspace copy replaces the shipped skill, even when that copy is invalid.
    (workspace / ".agents/skills/handoff").mkdir()
    (workspace / ".agents/skills/handoff/SKILL.md").write_text("x" * 8001)
    assert [skill["id"] for skill in manager.skills(tools)] == ["review"]
    with pytest.raises(ValueError, match="8 KB"):
        manager.load_skill(tools, "handoff")
    with pytest.raises(ValueError):
        manager.load_skill(tools, "../handoff")


def test_shipped_skills_reject_symlinks_and_oversized_files(manager, tmp_path):
    shipped = tmp_path / "app/skills"
    manager.app_skills = shipped
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("Outside instructions")
    (shipped / "file-link").mkdir(parents=True)
    (shipped / "file-link/SKILL.md").symlink_to(outside / "SKILL.md")
    (shipped / "folder-link").symlink_to(outside)
    (shipped / "large").mkdir()
    (shipped / "large/SKILL.md").write_text("x" * 8001)
    tools = WorkspaceTools(str(tmp_path / "workspace"))
    assert manager.skills(tools) == []
    for skill_id in ("file-link", "folder-link"):
        with pytest.raises(ValueError, match="regular files in the app's skills folder"):
            manager.load_skill(tools, skill_id)


def test_the_app_ships_the_handoff_skill_to_every_workspace(tmp_path):
    manager = ExtensionManager(SimpleNamespace(state_dir=tmp_path, redact=lambda text: text))
    tools = WorkspaceTools(str(tmp_path))
    handoff = next(skill for skill in manager.skills(tools) if skill["id"] == "handoff")
    assert handoff["path"] == str(Path(__file__).resolve().parents[2] / "skills/handoff/SKILL.md")
    assert "insert_activity_log" in manager.load_skill(tools, "handoff")["text"]


def test_shipped_skill_descriptions_fit_the_skill_index(tmp_path):
    # The index clips descriptions; a shipped skill keeps its "Use when" trigger whole.
    from local_agent.extensions import SKILL_INDEX_DESCRIPTION, skill_index
    manager = ExtensionManager(SimpleNamespace(state_dir=tmp_path, redact=lambda text: text))
    skills = manager.skills(WorkspaceTools(str(tmp_path)))
    assert skills and all(len(skill["description"]) <= SKILL_INDEX_DESCRIPTION for skill in skills)
    assert "- handoff: Use when the user asks for a handoff" in skill_index(skills)
    assert "…" not in skill_index(skills)


async def test_hook_receives_json_stdin_strips_credentials_and_redacts_output(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("DBRICKS_TOKEN", "never-pass-this")
    source = "import json,os,sys; value=json.load(sys.stdin); print(value['name'],os.environ.get('DBRICKS_TOKEN'),'secret-token')"
    configured = hook(shlex.quote(sys.executable) + " -c " + shlex.quote(source))
    manager.update_config({"hooks": [configured]})
    result = await manager.run_hook(configured, {"name": "tool-name"}, tmp_path)
    assert result["exit_code"] == 0
    assert result["output"] == "tool-name None [REDACTED]\n"
    assert not list(manager.path.parent.glob("hook-input-*"))


async def test_hook_timeout_retains_output_and_cancellation_removes_input(manager, tmp_path):
    configured = hook("printf partial; sleep 10; touch must-not-exist", timeout_seconds=1)
    manager.update_config({"hooks": [configured]})
    result = await manager.run_hook(configured, {}, tmp_path)
    assert result["timed_out"] is True
    assert result["output"] == "partial"
    configured = hook("sleep 10; touch must-not-exist")
    manager.update_config({"hooks": [configured]})
    task = asyncio.create_task(manager.run_hook(configured, {}, tmp_path))
    await asyncio.sleep(.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(manager.path.parent.glob("hook-input-*"))
    assert not (tmp_path / "must-not-exist").exists()


async def test_a_long_hook_output_still_says_when_the_hook_did_not_end(manager, tmp_path, unkillable_group):
    from local_agent.jobs import NOT_STOPPED
    configured = hook("printf '%5000s' | tr ' ' x; sleep 30", timeout_seconds=1)
    manager.update_config({"hooks": [configured]})
    result = await asyncio.wait_for(manager.run_hook(configured, {}, tmp_path), 10)
    assert result["timed_out"] is True and result["stopped"] is False and result["truncated"] is True
    assert result["output"].startswith("xxx") and result["output"].endswith("\n" + NOT_STOPPED)
    assert len(json.dumps(result, indent=2).encode()) <= 4000


async def test_hook_rejects_disabled_or_changed_configuration(manager, tmp_path):
    configured = hook("true")
    manager.update_config({"hooks": [{**configured, "enabled": False}]})
    with pytest.raises(ValueError, match="disabled or changed"):
        await manager.run_hook(configured, {}, tmp_path)
    manager.update_config({"hooks": [hook("false")]})
    with pytest.raises(ValueError, match="disabled or changed"):
        await manager.run_hook(configured, {}, tmp_path)


async def test_hook_truncated_secret_prefix_and_unicode_output_are_bounded(manager, tmp_path):
    token = "complete-sensitive-value"
    manager.settings.credentials = lambda: ("https://fake.example", token)
    source = "import os; os.write(1,b'x'*4088+b'complete-sensitive-value')"
    configured = hook(shlex.quote(sys.executable) + " -c " + shlex.quote(source))
    manager.update_config({"hooks": [configured]})
    result = await manager.run_hook(configured, {}, tmp_path)
    assert "complete" not in result["output"]
    assert result["truncated"] is True
    assert len(json.dumps(result, indent=2).encode()) <= 4000


def test_skill_metadata_is_redacted(manager, tmp_path):
    folder = tmp_path / ".agents/skills/check"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: secret-token\ndescription: secret-token\n---\nBody")
    skill = manager.skills(WorkspaceTools(str(tmp_path)))[0]
    assert skill["name"] == skill["description"] == "[REDACTED]"


def skill_file(root, skill_id, text):
    target = root / skill_id / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target


def test_claude_skills_are_discovered_and_loaded_read_only(manager, tmp_path):
    workspace = tmp_path / "workspace"
    skill_file(workspace / ".claude/skills", "lint", "---\ndescription: Run the linter\n---\nLint rules.")
    tools = WorkspaceTools(str(workspace))
    before = sorted(path.name for path in (workspace / ".claude/skills/lint").iterdir())
    assert manager.skills(tools) == [{"id": "lint", "name": "lint", "description": "Run the linter",
                                      "path": ".claude/skills/lint/SKILL.md"}]
    assert manager.load_skill(tools, "lint")["text"].endswith("Lint rules.")
    assert sorted(path.name for path in (workspace / ".claude/skills/lint").iterdir()) == before


def test_agents_skills_win_over_claude_skills_which_win_over_shipped(manager, tmp_path):
    shipped = tmp_path / "app/skills"
    manager.app_skills = shipped
    workspace = tmp_path / "workspace"
    for skill_id in ("both", "claude-shipped", "shipped-only"):
        skill_file(shipped, skill_id, f"Shipped {skill_id}.")
    skill_file(workspace / ".agents/skills", "both", "Agents both.")
    skill_file(workspace / ".claude/skills", "both", "Claude both.")
    skill_file(workspace / ".claude/skills", "claude-shipped", "Claude claude-shipped.")
    tools = WorkspaceTools(str(workspace))
    listed = {skill["id"]: skill["path"] for skill in manager.skills(tools)}
    assert listed == {"both": ".agents/skills/both/SKILL.md", "claude-shipped": ".claude/skills/claude-shipped/SKILL.md",
                      "shipped-only": str(shipped / "shipped-only/SKILL.md")}
    assert manager.load_skill(tools, "both")["text"] == "Agents both."
    assert manager.load_skill(tools, "claude-shipped")["text"] == "Claude claude-shipped."
    assert manager.load_skill(tools, "shipped-only")["text"] == "Shipped shipped-only."
    # An invalid .claude copy still takes the ID over from the shipped skill.
    (workspace / ".claude/skills/claude-shipped/SKILL.md").write_text("x" * 8001)
    assert "claude-shipped" not in {skill["id"] for skill in manager.skills(tools)}
    with pytest.raises(ValueError, match="8 KB"):
        manager.load_skill(tools, "claude-shipped")


@pytest.mark.parametrize("problem", ["entries", "skills", "file"])
def test_a_broken_claude_skills_folder_is_skipped(manager, tmp_path, problem):
    workspace = tmp_path / "workspace"
    skill_file(workspace / ".agents/skills", "review", "Review.")
    claude = workspace / ".claude/skills"
    if problem == "entries":
        for index in range(101):
            (claude / f"entry{index}").mkdir(parents=True)
    elif problem == "skills":
        for index in range(31):
            skill_file(claude, f"skill{index}", "Skill.")
    else:
        claude.parent.mkdir(parents=True)
        claude.write_text("not a folder")
    tools = WorkspaceTools(str(workspace))
    assert [skill["id"] for skill in manager.skills(tools)] == ["review"]


@pytest.mark.parametrize("problem", ["outside", "skills", "file"])
def test_a_skipped_claude_skills_folder_does_not_take_over_shipped_skills(manager, tmp_path, problem):
    # A .claude/skills folder that is skipped must not hide or replace the shipped skill
    # of the same ID, in the list or in load_skill.
    shipped = tmp_path / "app/skills"
    manager.app_skills = shipped
    skill_file(shipped, "handoff", "---\ndescription: Shipped handoff\n---\nShipped text.")
    workspace = tmp_path / "workspace"
    claude = workspace / ".claude/skills"
    if problem == "outside":
        shared = tmp_path / "home/.claude/skills"
        skill_file(shared, "handoff", "Shared handoff.")
        claude.parent.mkdir(parents=True)
        claude.symlink_to(shared, target_is_directory=True)
    elif problem == "skills":
        for index in range(30):
            skill_file(claude, f"skill{index}", "Skill.")
        skill_file(claude, "handoff", "Claude handoff.")
    else:
        skill_file(claude, "handoff", "Claude handoff.")
        for index in range(100):
            (claude / f"entry{index}").mkdir()
    for unrestricted in (False, True):
        tools = WorkspaceTools(str(workspace), unrestricted=unrestricted)
        assert [skill["path"] for skill in manager.skills(tools)] == [str(shipped / "handoff/SKILL.md")]
        assert manager.load_skill(tools, "handoff")["text"].endswith("Shipped text.")


def test_a_broken_agents_skills_folder_still_reports_its_error(manager, tmp_path):
    workspace = tmp_path / "workspace"
    for index in range(101):
        (workspace / f".agents/skills/entry{index}").mkdir(parents=True)
    with pytest.raises(ValueError, match="at most 100 entries"):
        manager.skills(WorkspaceTools(str(workspace)))


def test_skill_index_is_bounded_and_collapses_descriptions():
    from local_agent.extensions import skill_index
    assert skill_index([]) == ""
    text = skill_index([{"id": "review", "description": "Review\n  a change\twith care"}, {"id": "plain", "description": ""}])
    assert text == ("Skills you can load with use_skill when one fits the task (list_skills shows details):\n"
                    "- review: Review a change with care\n- plain")
    long = skill_index([{"id": "long", "description": "word " * 100}])
    assert len(long.splitlines()[1]) == len("- long: ") + 150 and long.endswith("…")
    many = [{"id": f"skill{index:02d}", "description": "A short description."} for index in range(45)]
    text = skill_index(many)
    lines = text.splitlines()
    assert len(lines) == 1 + 30 + 1 and lines[-1] == "- … and 15 more (call list_skills)."
    wide = [{"id": f"skill{index:02d}", "description": "é" * 200} for index in range(30)]
    text = skill_index(wide)
    assert len(text.encode("utf-8")) <= 2000
    lines = text.splitlines()
    kept = len(lines) - 2
    assert 0 < kept < 30 and lines[-1] == f"- … and {30 - kept} more (call list_skills)."


async def test_the_skill_index_reaches_the_system_message(tmp_path, monkeypatch):
    import httpx

    from local_agent.agents import AgentManager
    from local_agent.config import Settings
    from local_agent.store import Store
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    skill_file(project / ".agents/skills", "review", "---\ndescription: Review   a\tchange\n---\nRules.")
    skill_file(project / ".claude/skills", "lint", "---\ndescription: Run the linter\n---\nLint rules.")
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "index.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    seen = []

    async def handler(request):
        seen.append(json.loads(request.content)["messages"][0]["content"])
        chunk = {"choices": [{"delta": {"content": "Done"}, "finish_reason": "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handler)))
    try:
        await manager.run_databricks(session, "Hello")
    finally:
        store.db.close()
    assert ("Skills you can load with use_skill when one fits the task (list_skills shows details):\n"
            "- review: Review a change\n- lint: Run the linter\n") in seen[-1]
    assert "When a listed skill fits the task, load it with use_skill before acting." in seen[-1]
    assert "Lint rules." not in seen[-1]
    source = session["context_info"]["instruction_sources"][-1]
    assert source["path"] == "Skill index" and source["scope"] == "this workspace" and source["estimated_tokens"] > 0
