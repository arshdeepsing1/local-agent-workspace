"""The shipped /workflow skill: a prompt only the user starts, which splits a task into independent parts and
runs them as one parallel batch of delegate_task calls."""
import asyncio
import json
import re
from pathlib import Path

import pytest

from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.extensions import SKILL_INDEX_DESCRIPTION, SKILL_LIMIT, ExtensionManager, skill_index
from local_agent.feature_tools import FEATURE_TOOLS
from local_agent.store import Store
from local_agent.tools import WorkspaceTools

SKILLS = Path(__file__).resolve().parents[2] / "skills"
PATH = SKILLS / "workflow" / "SKILL.md"
FORBIDDEN = [r"/users/", r"/home/", r"[\w.+-]+@[\w-]+\.[\w.]+"]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    store = Store(settings.state_dir / "workflow.sqlite3")
    manager = AgentManager(store, settings)
    manager.extensions.app_skills = SKILLS
    session = store.create(settings.values)
    yield manager, session, project
    store.db.close()


def frontmatter():
    match = re.match(r"---\n(.*?)\n---\n", PATH.read_text(encoding="utf-8"), re.DOTALL)
    assert match, "frontmatter missing"
    return match.group(1)


def test_the_file_exists_and_fits_the_app_limit():
    assert PATH.is_file()
    assert PATH.stat().st_size <= SKILL_LIMIT - 400  # The app adds a short wrapper around an active skill.
    assert PATH.stat().st_size <= 4000  # Short enough to read in full on every use.


def test_the_frontmatter_names_the_skill_and_only_the_user_starts_it():
    front = frontmatter()
    assert re.search(r"^name:\s*workflow\s*$", front, re.MULTILINE)
    description = re.search(r"^description:\s*(.+)$", front, re.MULTILINE).group(1).strip().strip("\"'")
    assert description.startswith("Use when") and len(description) <= SKILL_INDEX_DESCRIPTION
    assert re.search(r"^disable-model-invocation:\s*true\s*$", front, re.MULTILINE)


@pytest.mark.parametrize("needle", ["delegate_task", "one response", "context", "independent"])
def test_the_body_contains_the_contract_string(needle):
    assert needle in PATH.read_text(encoding="utf-8")


def test_the_body_uses_real_argument_names_and_values():
    text = PATH.read_text(encoding="utf-8")
    properties = next(item["function"] for item in FEATURE_TOOLS
                      if item["function"]["name"] == "delegate_task")["parameters"]["properties"]
    for name in ("task", "context", "agent_type", "tool_profile", "max_steps"):
        assert name in properties and name in text
    for value in ("explore", "editor", "read_only", "file_editor"):
        assert value in text
    assert "background" in text  # Said only to keep it off unless the user asked.


def test_the_body_follows_the_project_style():
    text = PATH.read_text(encoding="utf-8")
    assert "—" not in text and "–" not in text and ";" not in text
    lowered = text.lower()
    for pattern in FORBIDDEN:
        assert not re.search(pattern, lowered), pattern


def test_the_app_loads_it_from_the_shipped_folder_with_its_flag(runtime):
    manager, session, project = runtime
    tools = WorkspaceTools(str(project))
    ids = [skill["id"] for skill in manager.extensions.skills(tools)]
    assert "workflow" in ids
    loaded = manager.extensions.load_skill(tools, "workflow")
    assert loaded["disable_model_invocation"] is True
    assert "delegate_task" in loaded["text"]


def test_the_model_never_sees_it_in_the_skill_index(runtime, tmp_path):
    manager, session, project = runtime
    skills = manager.extensions.skills(WorkspaceTools(str(project)))
    index = skill_index(skills)
    assert "workflow" not in index and "- verify:" in index


@pytest.mark.parametrize("command, selected", [
    ("/workflow", ["workflow"]), ("/workflow fix the three failing modules", ["workflow"]),
    ("workflow without a slash", []), ("/workflows", []),
])
def test_a_message_starting_with_the_command_selects_it(runtime, command, selected):
    manager, session, project = runtime
    assert manager.requested_skills(WorkspaceTools(str(project)), command, session) == selected


def test_the_extension_manager_alone_lists_it(tmp_path):
    from types import SimpleNamespace
    manager = ExtensionManager(SimpleNamespace(state_dir=tmp_path, redact=lambda text: text), app_skills=SKILLS)
    assert "workflow" in [skill["id"] for skill in manager.skills(WorkspaceTools(str(tmp_path)))]


def test_the_parts_are_not_a_fixed_number_and_the_limit_is_the_apps_to_enforce():
    """The per-turn limit is a setting from 1 to 24 that the model is never told: the skill names no count of its
    own, and says what happens at the limit."""
    text = PATH.read_text(encoding="utf-8")
    assert not re.search(r"\b\d+ to \d+ (independent )?parts", text)
    assert "as few as the task needs" in text
    assert "refuses" in text and "limit set in Settings" in text and "continue with the subagents you have" in text.lower()


async def test_a_turn_that_starts_with_the_command_sends_the_skill_text_to_the_model_and_other_turns_do_not(
        tmp_path, monkeypatch):
    from test_parallel_subagents import Gateway, make_runtime, reply
    async for manager, parent, project in make_runtime(tmp_path, monkeypatch):
        manager.extensions.app_skills = SKILLS
        gateway = Gateway(monkeypatch, [lambda: reply("Done.")], {})
        for prompt in ("/workflow split this", "split this"):
            manager.start(parent["id"], prompt)
            await asyncio.wait_for(manager.tasks[parent["id"]], 10)
        first, second = gateway.requests["parent"]
        with_command, without = first["messages"][-1]["content"], second["messages"][-1]["content"]
        assert with_command.startswith("/workflow split this\n\n---\n[Skill /workflow")
        assert "You are the orchestrator" in with_command and "delegate_task" in with_command
        assert without == "split this" and "orchestrator" not in without
        assert "orchestrator" not in json.dumps(second["messages"][-1])
