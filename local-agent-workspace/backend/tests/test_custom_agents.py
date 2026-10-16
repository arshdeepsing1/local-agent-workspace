"""Custom agents and per-agent model and effort: agent files (.agents/agents, .claude/agents), their
parser and limits, the agent index in the system message, delegate_task with a custom agent (its
model, effort, tools, skills, instructions and steps), Settings → Subagents defaults for the built-in
types, budgets on another endpoint, the approval card, and the subagents index."""
import asyncio
import json
import types
import os

import httpx
import pytest

from local_agent import custom_agents, planning
from local_agent.prompt_cache import unmarked
from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import DEFAULT_SUBAGENT_DEFAULTS, Settings, subagent_defaults
from local_agent.custom_agents import (AGENT_ROOTS, FILE_BYTES, INDEX_BYTES, MAX_AGENTS, agent_index, discover,
                                       model_label, parse_agent, resolve)
from local_agent.portability import export_bundle, fresh_copies
from local_agent.store import Store
from local_agent.tool_profiles import tool_allowed

REAL_CLIENT = httpx.AsyncClient
OPUS = "databricks-claude-opus-5-5"
GPT = "databricks-gpt-oss-120b"
SONNET = "databricks-claude-sonnet-4-5"
AUDITOR = """---
name: security-auditor
description: Reviews code for injection, secrets handling and unsafe file access.
tools: [read_file, search_files, run_command, Grep, web_fetch]
profile: inherit
model: databricks-gpt-oss-120b
effort: high
skills:
  - review
  - missing
max_steps: 7
---
Check every changed file for hard-coded secrets.
Report each finding with path:line.
"""


def write_agent(project, name, text, root=AGENT_ROOTS[0]):
    folder = project / root
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(text)
    return folder / f"{name}.md"


def agent_text(description="Finds things.", **fields):
    lines = [f"description: {description}", *(f"{key}: {value}" for key, value in fields.items())]
    return "---\n" + "\n".join(lines) + "\n---\nInstructions.\n"


# Parsing

def test_a_full_agent_file_parses_with_its_notes():
    agent = parse_agent(AUDITOR, "security-auditor")
    assert {key: agent[key] for key in ("name", "profile", "model", "effort", "skills", "max_steps")} == {
        "name": "security-auditor", "profile": "inherit", "model": GPT, "effort": "high",
        "skills": ["review", "missing"], "max_steps": 7}
    assert agent["description"] == "Reviews code for injection, secrets handling and unsafe file access."
    # Unknown names are left out with a note; Claude Code names map to this app's tools.
    assert agent["tools"] == ["read_file", "search_files", "run_command"]
    assert agent["instructions"] == "Check every changed file for hard-coded secrets.\nReport each finding with path:line."
    assert "Claude Code tool names used as this app's: Grep → search_files." in agent["notes"]
    assert "Ignored unknown tools: web_fetch." in agent["notes"]


def test_defaults_are_the_file_name_read_only_and_the_chats_model_and_effort():
    agent = parse_agent("---\ndescription: 'Maps a module, it''s quick'\n# A comment\n\n---\n", "mapper")
    assert agent == {"name": "mapper", "description": "Maps a module, it's quick", "profile": "read_only", "tools": None,
                     "model": None, "effort": None, "skills": [], "max_steps": None, "instructions": "", "notes": []}
    claude = parse_agent('---\nname: reviewer\ndescription: "Reviews \\"diffs\\""\ntools: Read, Grep, Glob\n'
                         "model: sonnet\ncolor: blue\n---\nBody", "other")
    assert claude["tools"] == ["read_file", "search_files", "find_files"] and claude["model"] is None
    assert claude["description"] == 'Reviews "diffs"'
    assert any("sonnet is not a Databricks endpoint" in note for note in claude["notes"])
    assert "Ignored unknown fields: color." in claude["notes"]
    assert parse_agent(agent_text(model="inherit", effort="inherit"), "x")["model"] is None
    assert parse_agent("﻿" + agent_text("Windows file.").replace("\n", "\r\n"), "x")["description"] == "Windows file."


@pytest.mark.parametrize("text,reason", [
    ("No frontmatter here.", "must start with a '---' line"),
    ("---\ndescription: x\n", "no closing '---'"),
    ("---\nname: mapper\n---\nBody", "description is required"),
    ("---\ndescription:\n---\n", "description is required"),
    ("---\ndescription: " + "x" * 301 + "\n---\n", "at most 300"),
    (agent_text(name="Security_Auditor"), "name must start with a lowercase letter"),
    (agent_text(name="explore"), "built-in agent type"),
    (agent_text(name="a" * 41), "at most 40"),
    ("---\ndescription: x\n  nested: value\n---\n", "Line 3: expected 'key: value'"),
    ("---\ndescription: x\ndescription: y\n---\n", "Line 3: description is given twice"),
    ("---\ndescription: >\n  folded\n---\n", "multi-line values"),
    ("---\n- item\ndescription: x\n---\n", "must follow a 'key:' line"),
    (agent_text(tools="[read_file"), "must close on the same line"),
    (agent_text(tools="[]"), "tools is empty"),
    (agent_text(tools="[web_fetch, Task]"), "names no tool"),
    (agent_text(tools="[run_command]"), "set profile: file_editor or inherit"),
    (agent_text(profile="admin"), "profile must be read_only"),
    (agent_text(model="bad model!"), "model must be a Databricks serving endpoint name"),
    (agent_text(model="[a, b]"), "model must be a Databricks serving endpoint name"),
    (agent_text(effort="turbo"), "effort must be one of default, low"),
    (agent_text(skills="[a, b, c, d]"), "at most 3 different skill IDs"),
    (agent_text(skills="[../x]"), "skill IDs"),
    (agent_text(max_steps=25), "max_steps must be a whole number from 1 to 24"),
    (agent_text(max_steps=0), "1 to 24"),
    (agent_text(max_steps="'ten'"), "1 to 24"),
    ("---\ndescription: x\n---\n" + "y" * 8001, "at most 8,000"),
    # A lone surrogate escape decodes but cannot be written as UTF-8 (the agents route, the system message).
    ('---\ndescription: "Maps \\ud800 modules"\n---\n', "Line 2: a double-quoted value is not valid text"),
    ('---\ndescription: x\ntools: ["read_file", "\\udfff"]\n---\n', "Line 3: a double-quoted value is not valid text"),
])
def test_bad_files_are_refused_with_a_clear_reason(text, reason):
    with pytest.raises(ValueError, match=reason.replace("(", r"\(").replace("[", r"\[")):
        parse_agent(text, "agent")


def test_tools_outside_the_profile_are_noted_and_delegate_task_never_applies():
    agent = parse_agent(agent_text(tools="[read_file, write_file, delegate_task, mcp__docs__search]"), "a")
    assert agent["tools"] == ["read_file"]
    assert "delegate_task is ignored: subagents cannot delegate." in agent["notes"]
    assert "Not in the read_only profile, so unavailable: write_file, mcp__docs__search." in agent["notes"]
    inherit = parse_agent(agent_text(profile="inherit", tools="[read_file, mcp__docs__search]"), "a")
    assert inherit["tools"] == ["read_file", "mcp__docs__search"]


# Discovery

def test_discovery_reads_both_folders_and_the_agents_folder_wins(tmp_path):
    write_agent(tmp_path, "mapper", agent_text("From .agents."))
    write_agent(tmp_path, "mapper", agent_text("From .claude."), AGENT_ROOTS[1])
    write_agent(tmp_path, "tester", agent_text("Runs tests.", profile="inherit"), AGENT_ROOTS[1])
    (tmp_path / AGENT_ROOTS[0] / "notes.txt").write_text("not an agent")
    (tmp_path / AGENT_ROOTS[0] / "other.md").write_text(agent_text("Same name.", name="mapper"))
    found = discover(tmp_path)
    assert [(agent["name"], agent["path"], agent["description"]) for agent in found["agents"]] == [
        ("mapper", ".agents/agents/mapper.md", "From .agents."), ("tester", ".claude/agents/tester.md", "Runs tests.")]
    assert "Used instead of .claude/agents/mapper.md, which has the same name." in found["agents"][0]["notes"]
    assert found["errors"] == [{"path": ".agents/agents/other.md", "error": ".agents/agents/mapper.md already defines mapper."}]


def test_discovery_reports_oversized_symlinked_and_broken_files_and_stops_at_30(tmp_path):
    folder = write_agent(tmp_path, "big", "---\ndescription: x\n---\n" + "y" * FILE_BYTES).parent
    (tmp_path / "outside.md").write_text(agent_text())
    os.symlink(tmp_path / "outside.md", folder / "link.md")
    (folder / "binary.md").write_bytes(b"---\ndescription: x\n---\n\x00")
    (folder / "latin.md").write_bytes("---\ndescription: café\n---\n".encode("latin-1"))
    for index in range(MAX_AGENTS + 2):
        write_agent(tmp_path, f"agent-{index:02d}", agent_text(f"Agent {index}."))
    found = discover(tmp_path)
    errors = {item["path"]: item["error"] for item in found["errors"]}
    assert "agent files are at most 16 KB" in errors[".agents/agents/big.md"]
    assert "Symbolic links are not read" in errors[".agents/agents/link.md"]
    assert errors[".agents/agents/binary.md"] == "The file is not text."
    assert errors[".agents/agents/latin.md"] == "The file is not UTF-8 text."
    assert len(found["agents"]) == MAX_AGENTS
    assert errors[".agents/agents/agent-31.md"] == "At most 30 custom agents are read; this one is ignored."


def test_an_agent_folder_outside_the_workspace_is_not_read(tmp_path):
    project, elsewhere = tmp_path / "project", tmp_path / "elsewhere"
    project.mkdir()
    write_agent(elsewhere, "mapper", agent_text())
    (project / ".agents").mkdir()
    os.symlink(elsewhere / AGENT_ROOTS[0], project / AGENT_ROOTS[0])
    assert discover(project) == {"agents": [], "errors": [
        {"path": ".agents/agents", "error": "Agent files must be in a folder inside this workspace."}]}
    assert discover(tmp_path / "missing") == {"agents": [], "errors": []}


NON_UTF8_ERROR = {".agents/agents/caf\\udce9.md": "The file name is not valid UTF-8 text; rename the file."}


def non_utf8_agent(project):
    """Write the agent file cafe as caf\\xe9.md, a name with a byte that is not UTF-8; False where the
    file system refuses such names (macOS)."""
    folder = project / AGENT_ROOTS[0]
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with open(os.fsencode(folder) + b"/caf\xe9.md", "wb") as handle:
            handle.write(agent_text("A cafe agent.", name="cafe").encode())
    except OSError:
        return False
    return True


def test_a_file_name_that_is_not_utf8_is_reported_without_parsing(tmp_path):
    if not non_utf8_agent(tmp_path):
        pytest.skip("this file system refuses file names that are not UTF-8")
    write_agent(tmp_path, "mapper", agent_text("Maps."))
    found = discover(tmp_path)
    assert [agent["name"] for agent in found["agents"]] == ["mapper"]
    assert {item["path"]: item["error"] for item in found["errors"]} == NON_UTF8_ERROR
    json.dumps(found, ensure_ascii=False).encode("utf-8")  # Safe to send and save.


def test_secrets_in_agent_files_are_redacted(tmp_path):
    token = "dapi" + "0123456789abcdef" * 2  # A token shape, built here so the source holds none.
    write_agent(tmp_path, "leaky", f"---\ndescription: Uses token {token}\n---\npassword = hunter2-secret\n")
    agent = discover(tmp_path, custom_agents.redact_secrets)["agents"][0]
    assert token not in agent["description"] and "hunter2" not in agent["instructions"]
    assert "[REDACTED]" in agent["description"]


# The agent index in the system message

def test_the_index_stays_within_2_kb_and_counts_the_rest():
    agents = [{"name": f"agent-{index:02d}", "description": "x" * 300} for index in range(MAX_AGENTS)]
    text = agent_index(agents)
    assert len(text.encode("utf-8")) <= INDEX_BYTES
    assert text.startswith("Custom subagents: set delegate_task agent_type")
    kept = sum(line.startswith("- agent-") for line in text.splitlines())
    assert 0 < kept < MAX_AGENTS and text.endswith(f"- … and {MAX_AGENTS - kept} more (an unknown agent_type is "
                                                   "refused with every name).")
    assert agent_index([]) == ""
    assert agent_index([{"name": "mapper", "description": "Maps."}]).endswith("\n- mapper: Maps.")


def test_resolve_names_every_available_agent_for_an_unknown_one(tmp_path):
    write_agent(tmp_path, "mapper", agent_text())
    write_agent(tmp_path, "broken", "---\nname: broken\n---\n")
    catalog = discover(tmp_path)
    with pytest.raises(ValueError, match=r"Unknown agent_type 'planner'\. Available agent types: explore, general, "
                                         r"editor, mapper\."):
        resolve("planner", None, catalog, DEFAULT_SUBAGENT_DEFAULTS)
    with pytest.raises(ValueError, match=r"\(.agents/agents/broken.md cannot be used: description is required"):
        resolve("broken", None, catalog, DEFAULT_SUBAGENT_DEFAULTS)
    explore = resolve("explore", None, catalog, DEFAULT_SUBAGENT_DEFAULTS)
    assert (explore["requested"], explore["model"], explore["effort"]) == ("read_only", None, "low")
    general = resolve(None, None, catalog, DEFAULT_SUBAGENT_DEFAULTS)
    assert (general["name"], general["requested"], general["effort"]) == ("general", None, None)
    mapper = resolve("mapper", "inherit", catalog, DEFAULT_SUBAGENT_DEFAULTS)
    assert (mapper["custom"], mapper["requested"], mapper["path"]) == (True, "read_only", ".agents/agents/mapper.md")


def test_model_labels_follow_the_endpoint_profiles_or_the_name():
    assert model_label(OPUS) == "Claude Opus 5.5" and model_label(GPT) == "GPT OSS 120B"
    assert model_label(SONNET) == "Claude Sonnet 4.5"
    assert custom_agents.MAX_STEPS == planning.MAX_STEPS


# Settings → Subagents

def test_subagent_defaults_are_checked_and_a_hand_written_file_falls_back():
    assert subagent_defaults(None) == DEFAULT_SUBAGENT_DEFAULTS
    assert DEFAULT_SUBAGENT_DEFAULTS["explore"] == {"model": "", "effort": "low"}
    assert subagent_defaults({"general": {"model": f" {SONNET} ", "effort": "max"}})["general"] == {
        "model": SONNET, "effort": "max"}
    for bad, reason in (({"planner": {}}, "only explore, general and editor"),
                        ({"explore": {"model": "a b"}}, "endpoint name"), ({"explore": {"effort": "turbo"}}, "effort"),
                        ({"explore": {"color": "x"}}, "only model and effort"), ({"explore": []}, "only model")):
        with pytest.raises(ValueError, match=reason):
            subagent_defaults(bad)
    assert subagent_defaults({"explore": {"effort": "turbo"}, "general": 3, "x": 1}, strict=False) == DEFAULT_SUBAGENT_DEFAULTS


def use_endpoints(manager, *models):
    """Chats of the user on these endpoints: an agent file may name only endpoints the user already uses."""
    for model in models:
        chat = manager.store.create({**manager.settings.values, "model": model})
        manager.store.save(chat)


@pytest.fixture
async def api(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.env = {}
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            client.headers["X-Local-Token"] = (await client.get("/api/bootstrap")).json()["token"]
            yield client, app.state.manager, settings, project


async def test_settings_save_the_defaults_partially_and_read_them_back(api):
    client, _, settings, _ = api
    body = {key: settings.values[key] for key in ("workspace", "model", "env_file")}
    assert (await client.get("/api/settings")).json()["subagent_defaults"] == DEFAULT_SUBAGENT_DEFAULTS
    saved = await client.put("/api/settings", json={**body, "subagent_defaults": {"explore": {"model": SONNET}}})
    assert saved.status_code == 200, saved.text
    # Types and fields left out keep their values.
    assert saved.json()["subagent_defaults"]["explore"] == {"model": SONNET, "effort": "low"}
    assert saved.json()["subagent_defaults"]["general"] == {"model": "", "effort": ""}
    assert json.loads(settings.path.read_text())["subagent_defaults"]["explore"]["model"] == SONNET
    assert Settings(settings.state_dir).values["subagent_defaults"]["explore"]["model"] == SONNET
    refused = await client.put("/api/settings", json={**body, "subagent_defaults": {"editor": {"effort": "turbo"}}})
    assert refused.status_code == 400 and "editor subagent effort" in refused.json()["detail"]
    assert (await client.put("/api/settings", json={**body, "subagent_defaults": {"editor": {"effort": 3}}})).status_code == 422
    assert settings.values["subagent_defaults"]["editor"] == {"model": "", "effort": ""}


async def test_the_agents_route_lists_built_in_and_custom_agents_and_bad_files(api):
    client, manager, settings, project = api
    use_endpoints(manager, GPT)
    write_agent(project, "security-auditor", AUDITOR)
    write_agent(project, "bad", "---\nname: Bad\ndescription: x\n---\n", AGENT_ROOTS[1])
    listing = (await client.get("/api/agents")).json()
    assert [(item["name"], item["profile"], item["effort"]) for item in listing["built_in"]] == [
        ("explore", "read_only", "low"), ("general", "inherit", ""), ("editor", "file_editor", "")]
    custom = listing["custom"][0]
    assert {key: custom[key] for key in ("name", "path", "profile", "model", "effort", "tools", "skills", "max_steps",
                                         "built_in")} == {
        "name": "security-auditor", "path": ".agents/agents/security-auditor.md", "profile": "inherit", "model": GPT,
        "effort": "high", "tools": ["read_file", "search_files", "run_command"], "skills": ["review", "missing"],
        "max_steps": 7, "built_in": False}
    assert "instructions" not in custom and custom["instructions_chars"] > 0
    assert listing["errors"] == [{"path": ".claude/agents/bad.md", "error": listing["errors"][0]["error"]}]
    assert listing["errors"][0]["error"].startswith("name must start with a lowercase letter")
    assert listing["folders"] == [".agents/agents", ".claude/agents"] and 0 < listing["index_bytes"] <= INDEX_BYTES


async def test_the_agents_route_lists_files_with_bad_text_or_names_as_errors(api):
    client, _, _, project = api
    write_agent(project, "mapper", '---\ndescription: "Maps \\ud800 modules"\n---\nBody\n')
    write_agent(project, "tester", agent_text("Runs tests."))
    named = non_utf8_agent(project)
    response = await client.get("/api/agents")
    assert response.status_code == 200, response.text
    listing = response.json()
    assert [item["name"] for item in listing["custom"]] == ["tester"]
    assert {item["path"]: item["error"] for item in listing["errors"]} == {
        **(NON_UTF8_ERROR if named else {}),
        ".agents/agents/mapper.md": "Line 2: a double-quoted value is not valid text (a lone \\u escape)."}


# Delegation

@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("base\n")
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(project), env_file="", model="test-model")
    settings.credentials = lambda: ("https://gateway.example", "synthetic-token")
    store = Store(settings.state_dir / "agents.sqlite3")
    manager = AgentManager(store, settings)
    parent = store.create(settings.values)
    parent.update(title="Parent", title_generated=True, permission_mode="bypassPermissions")
    store.save(parent)
    yield manager, parent, project
    for session_id in list(manager.tasks):
        await manager.stop(session_id)
    await manager.jobs.shutdown()
    store.db.close()


def frame(delta, finish=None):
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"


def reply(text):
    return httpx.Response(200, text=frame({"content": text}, "stop") + "data: [DONE]\n\n")


def calls_reply(*calls):
    parts = [{"index": index, "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
             for index, (name, arguments, call_id) in enumerate(calls)]
    return httpx.Response(200, text=frame({"content": "", "tool_calls": parts}, "tool_calls") + "data: [DONE]\n\n")


class Gateway:
    """The parent's scripted responses in order; a child's request gets reply(child_text). Each
    request's endpoint (from the URL) and body are kept."""

    def __init__(self, monkeypatch, parent, child_text="Report: nothing found."):
        self.parent, self.child_text = parent, child_text
        self.requests = {"parent": [], "child": []}
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(self)))

    async def __call__(self, request):
        body = json.loads(request.content)
        if body.get("stream") is not True:  # A title request.
            return httpx.Response(200, json={"choices": [{"message": {"content": "A title"}, "finish_reason": "stop"}]})
        endpoint = request.url.path.split("/")[2]
        # Without cache markers: a child's first request marks its one user message.
        first = next((m["content"] for m in unmarked(body)["messages"] if m["role"] == "user"), "")
        if isinstance(first, str) and first.startswith("Delegated task:\n"):
            self.requests["child"].append((endpoint, body))
            return reply(self.child_text)
        self.requests["parent"].append((endpoint, body))
        return self.parent[min(len(self.requests["parent"]), len(self.parent)) - 1]()


async def run_turn(manager, parent, prompt="Delegate the work"):
    manager.start(parent["id"], prompt)
    await asyncio.wait_for(manager.tasks[parent["id"]], 10)
    return manager.store.get(parent["id"])


def card(session, call_id):
    return next(event for event in session["events"] if event.get("call_id") == call_id)


def offered(body):
    return [tool["function"]["name"] for tool in body.get("tools", [])]


async def test_a_custom_agent_gets_its_model_effort_tools_skills_instructions_and_steps(runtime, monkeypatch):
    manager, parent, project = runtime
    use_endpoints(manager, GPT)
    write_agent(project, "security-auditor", AUDITOR)
    skill = project / ".agents/skills/review/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: Review\ndescription: Review carefully\n---\nLook at every hunk.\n")
    parent["subagent_tool_profile"] = "read_only"  # The chat's ceiling narrows the agent's inherit profile.
    manager.store.save(parent)
    gateway = Gateway(monkeypatch, [lambda: calls_reply(("delegate_task", {"task": "Audit the repo",
                                                                           "agent_type": "security-auditor"}, "a")),
                                    lambda: reply("Parent done.")])
    saved = await run_turn(manager, parent)
    (endpoint, body), = gateway.requests["child"]
    assert endpoint == GPT and body["reasoning_effort"] == "high"
    # The file's tools within the read_only ceiling: run_command is not offered.
    assert offered(body) == ["read_file", "search_files"]
    system, first = body["messages"][0]["content"], body["messages"][1]["content"]
    assert "Workspace skill: review\n---\nname: Review" in system and "Custom subagents:" not in system
    assert first.startswith("Delegated task:\nAudit the repo\n\nCustom agent: security-auditor (from "
                            ".agents/agents/security-auditor.md). Follow its instructions")
    assert "Check every changed file for hard-coded secrets." in first
    assert "Note: Skill missing was not loaded" in first
    assert first.endswith("Your tools are read-only (find, search, outline, read): you cannot change files or run "
                          f"commands. Your tools: read_file, search_files. {planning.REPORT}")
    child = manager.store.get(card(saved, "a")["child_session_id"])
    assert {key: child[key] for key in ("agent_type", "tool_profile", "model", "reasoning_effort", "agent_effort",
                                        "agent_model", "agent_file", "agent_tools", "active_skills", "max_steps")} == {
        "agent_type": "security-auditor", "tool_profile": "read_only", "model": GPT, "reasoning_effort": "high",
        "agent_effort": "high", "agent_model": GPT, "agent_file": ".agents/agents/security-auditor.md",
        "agent_tools": ["read_file", "search_files"], "active_skills": ["review"], "max_steps": 7}
    progress = card(saved, "a")["delegation"]
    assert (progress["agent_type"], progress["model"], progress["effort"]) == ("security-auditor", GPT, "high")
    entry = saved["subagents"][0]
    assert (entry["agent_type"], entry["model"], entry["effort"], entry["status"]) == (
        "security-auditor", GPT, "high", "completed")
    result = json.loads(card(saved, "a")["output"])
    assert (result["agent_type"], result["tool_profile"], result["status"]) == ("security-auditor", "read_only", "completed")
    # The parent's system message lists the agent; its tool definitions are the same as without agents.
    parent_system = gateway.requests["parent"][0][1]["messages"][0]["content"]
    assert ("Custom subagents: set delegate_task agent_type to one of these names when its description fits"
            in parent_system)
    assert "- security-auditor: Reviews code for injection, secrets handling and unsafe file access." in parent_system


def write_skill(project, skill_id):
    path = project / ".agents/skills" / skill_id / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {skill_id}\ndescription: Skill {skill_id}\n---\nDo {skill_id}.\n")


async def test_skill_words_in_an_agents_instructions_load_no_skills(runtime, monkeypatch):
    manager, parent, project = runtime
    for skill_id in ("a1", "b1", "c1", "d1"):
        write_skill(project, skill_id)
    # A fourth skill would fail the child (at most three); a skill the file does not list must not load.
    write_agent(project, "mapper", "---\ndescription: Maps.\nskills: [a1, b1, c1]\n---\nWhen done, follow /d1.\n")
    write_agent(project, "lister", "---\ndescription: Lists.\n---\nUse /d1 here.\n")
    gateway = Gateway(monkeypatch, [lambda: calls_reply(
        ("delegate_task", {"task": "Map", "agent_type": "mapper"}, "a"),
        ("delegate_task", {"task": "List with /a1", "agent_type": "lister"}, "b"),
        ("delegate_task", {"task": "Look, then use /a1", "agent_type": "explore"}, "c")), lambda: reply("Parent done.")])
    saved = await run_turn(manager, parent)
    assert [json.loads(card(saved, call)["output"])["status"] for call in "abc"] == ["completed"] * 3
    assert len(gateway.requests["child"]) == 3
    skills = {call: manager.store.get(card(saved, call)["child_session_id"]).get("active_skills") for call in "abc"}
    # A custom agent gets its file's skills only; a built-in type's task can still name one, for its first message.
    assert skills == {"a": ["a1", "b1", "c1"], "b": None, "c": None}
    first = {request[1]["messages"][1]["content"].split("\n")[1]: request[1]["messages"][1]["content"]
             for request in gateway.requests["child"]}
    # The parent agent wrote the task: the skill is labelled as named by it, never as the user's choice.
    assert "Do a1." in first["Look, then use /a1"] and "[Skill /a1 named by the parent agent" in first["Look, then use /a1"]
    assert "selected by the user" not in first["Look, then use /a1"]
    assert not any("Do d1." in text or "[Skill /" in text for key, text in first.items() if key != "Look, then use /a1")


async def test_a_skill_only_the_user_selects_never_reaches_a_subagent(runtime, monkeypatch):
    """disable-model-invocation: a delegated task that names the skill, or an agent file that lists it,
    does not select it (the model chose both), and other skills still load."""
    manager, parent, project = runtime
    write_skill(project, "a1")
    path = project / ".agents/skills/deploy/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: deploy\ndescription: Deploys\ndisable-model-invocation: true\n---\nRun the deploy script now.\n")
    write_agent(project, "shipper", "---\ndescription: Ships.\nskills: [deploy, a1]\n---\nShip it.\n")
    gateway = Gateway(monkeypatch, [lambda: calls_reply(
        ("delegate_task", {"task": "Please follow /deploy and /a1", "agent_type": "general"}, "a"),
        ("delegate_task", {"task": "Ship", "agent_type": "shipper"}, "b")), lambda: reply("Parent done.")])
    saved = await run_turn(manager, parent)
    assert [json.loads(card(saved, call)["output"])["status"] for call in "ab"] == ["completed"] * 2
    first = {request[1]["messages"][1]["content"].split("\n")[1]: request[1]["messages"][1]["content"]
             for request in gateway.requests["child"]}
    named = first["Please follow /deploy and /a1"]
    assert "Run the deploy script now." not in named and "[Skill /deploy" not in named and "Do a1." in named
    child = manager.store.get(card(saved, "a")["child_session_id"])
    assert next(event for event in child["events"] if event["type"] == "user").get("skills") == ["a1"]
    shipper = manager.store.get(card(saved, "b")["child_session_id"])
    assert shipper.get("active_skills") == ["a1"]
    assert "Skill deploy was not loaded: only the user selects it (disable-model-invocation)." in first["Ship"]
    assert not any("Run the deploy script now." in json.dumps(request[1]) for request in gateway.requests["child"])


async def test_a_custom_agent_tool_outside_its_list_is_refused(runtime):
    manager, parent, project = runtime
    child = manager.store.create({"workspace": str(project), "model": "test-model"})
    child.update(is_subagent=True, tool_profile="inherit", agent_type="security-auditor", agent_tools=["read_file"])
    assert tool_allowed(child, "read_file") and not tool_allowed(child, "write_file")
    tools = manager.session_tools(child)
    output = await manager.execute_tool(child, tools, "write_file", {"path": "x.txt", "content": "x"}, "w")
    assert output == "The security-auditor agent's tools list does not include write_file. Use only the available tools."
    assert not (project / "x.txt").exists()


async def test_no_tool_of_the_agent_fits_the_ceiling_and_unknown_names_are_refused_before_a_child(runtime):
    manager, parent, project = runtime
    write_agent(project, "runner", agent_text("Runs things.", profile="inherit", tools="[run_command]"))
    parent["subagent_tool_profile"] = "file_editor"
    manager.store.save(parent)
    with pytest.raises(ValueError, match=r"None of the runner agent's tools \(run_command\) is allowed under this "
                                         r"chat's subagent tool ceiling \(file_editor\)"):
        await manager.delegates.delegate(parent, "Run it", agent_type="runner")
    with pytest.raises(ValueError, match="None of the runner agent's tools"):  # Before a Manual-mode card.
        manager.delegates.card_preview(parent, {"task": "Run it", "agent_type": "runner"})
    with pytest.raises(ValueError, match="Available agent types: explore, general, editor, runner"):
        await manager.delegates.delegate(parent, "Run it", agent_type="tester")
    assert len(manager.store.list()) == 1


async def test_agent_tools_the_chat_turned_off_or_denies_are_left_out(runtime, monkeypatch):
    manager, parent, project = runtime
    write_agent(project, "runner", agent_text("Runs things.", profile="inherit", tools="[run_command, outline_file]"))
    write_agent(project, "reader", agent_text("Reads.", tools="[read_file, search_files, outline_file]"))
    # outline_file is covered by read_file: turning read_file off turns it off too.
    parent.update(disabled_tools=["read_file"], allow_rules=[{"behavior": "deny", "tool": "run_command"}])
    manager.store.save(parent)
    with pytest.raises(ValueError, match=r"None of the runner agent's tools \(run_command, outline_file\) is available "
                                         r"in this chat: each is turned off or blocked by a deny rule"):
        await manager.delegates.delegate(parent, "Run it", agent_type="runner")
    assert "Tools: search_files" in manager.delegates.card_preview(parent, {"task": "Read", "agent_type": "reader"}).splitlines()
    gateway = Gateway(monkeypatch, [lambda: calls_reply(("delegate_task", {"task": "Run", "agent_type": "runner"}, "a"),
                                                        ("delegate_task", {"task": "Read", "agent_type": "reader"}, "b")),
                                    lambda: reply("Parent done.")])
    saved = await run_turn(manager, parent)
    assert card(saved, "a")["state"] == "error" and "child_session_id" not in card(saved, "a")
    (_, body), = gateway.requests["child"]  # No paid requests without a tool.
    assert offered(body) == ["search_files"]
    assert "Your tools: search_files." in body["messages"][1]["content"]
    assert manager.store.get(card(saved, "b")["child_session_id"])["agent_tools"] == ["search_files"]


async def test_a_child_that_cannot_be_saved_leaves_no_conversation_and_frees_its_slot(runtime, monkeypatch):
    manager, parent, project = runtime
    save = manager.store.save

    def failing(session, **options):
        if session.get("is_subagent"):
            raise OSError(28, "No space left on device")
        return save(session, **options)
    monkeypatch.setattr(manager.store, "save", failing)
    with pytest.raises(OSError, match="No space left"):
        await manager.delegates.delegate(parent, "Look", agent_type="explore")
    assert [session["id"] for session in manager.store.list()] == [parent["id"]]
    assert manager.delegates.running() == 0 and manager.delegates.active == {}


async def test_an_unknown_agent_name_is_a_tool_error_listing_the_agents(runtime, monkeypatch):
    manager, parent, project = runtime
    write_agent(project, "mapper", agent_text())
    Gateway(monkeypatch, [lambda: calls_reply(("delegate_task", {"task": "Map", "agent_type": "planner"}, "a")),
                          lambda: reply("Parent done.")])
    saved = await run_turn(manager, parent)
    event = card(saved, "a")
    assert event["state"] == "error" and "child_session_id" not in event
    assert "Unknown agent_type 'planner'. Available agent types: explore, general, editor, mapper." in event["output"]


async def test_the_manual_mode_card_names_the_agent_its_tools_and_its_model(runtime):
    manager, parent, project = runtime
    use_endpoints(manager, SONNET)
    write_agent(project, "security-auditor", AUDITOR.replace("model: databricks-gpt-oss-120b", f"model: {SONNET}")
                .replace("effort: high", "effort: low"))
    parent["permission_mode"] = "manual"
    tools = manager.session_tools(parent)
    started = []

    async def worker(child, prompt):
        started.append(child)
        await manager.event(child, "assistant", text="Done.")
    manager.run_databricks = worker
    task = asyncio.create_task(manager.execute_tool(parent, tools, "delegate_task", {
        "task": "Audit", "agent_type": "security-auditor"}, "d"))
    for _ in range(300):
        if manager.pending:
            break
        await asyncio.sleep(0.01)
    session_id, event_id = next(iter(manager.pending))
    event = next(item for item in parent["events"] if item["id"] == event_id)
    assert event["preview"] == (
        "Start a subagent (security-auditor · chat's tools · model Claude Sonnet 4.5 · effort Fast; separate paid model "
        "requests, at most 7 steps)\n\nTask: Audit\nAgent file: .agents/agents/security-auditor.md\n"
        "Tools: read_file, search_files, run_command\nSkills: review, missing\nEndpoint: databricks-claude-sonnet-4-5")
    assert started == []
    manager.decide(session_id, event_id, True)
    result = json.loads(await asyncio.wait_for(task, 10))
    assert result["status"] == "completed" and started[0]["model"] == SONNET and started[0]["reasoning_effort"] == "low"


async def test_an_approved_card_starts_the_agent_it_showed_after_its_file_changes(runtime):
    manager, parent, project = runtime
    use_endpoints(manager, SONNET, GPT)
    write_agent(project, "mapper", f"---\ndescription: Maps.\nmodel: {SONNET}\neffort: low\ntools: [read_file]\n---\n"
                "Map the old way.\n")
    parent["permission_mode"] = "manual"
    tools = manager.session_tools(parent)
    started = []

    async def worker(child, prompt):
        started.append((child, prompt))
        await manager.event(child, "assistant", text="Done.")
    manager.run_databricks = worker
    task = asyncio.create_task(manager.execute_tool(parent, tools, "delegate_task", {"task": "Map", "agent_type": "mapper"},
                                                    "d"))
    for _ in range(300):
        if manager.pending:
            break
        await asyncio.sleep(0.01)
    session_id, event_id = next(iter(manager.pending))
    preview = next(item for item in parent["events"] if item["id"] == event_id)["preview"]
    assert preview.startswith("Start a subagent (mapper · read-only · model Claude Sonnet 4.5 · effort Fast;")
    # The file changes while the card waits (an editor, a git pull): what was approved starts.
    write_agent(project, "mapper", agent_text("Maps.", model=GPT, effort="max", profile="inherit") + "Map the new way.\n")
    manager.decide(session_id, event_id, True)
    assert json.loads(await asyncio.wait_for(task, 10))["status"] == "completed"
    (child, prompt), = started
    assert (child["model"], child["reasoning_effort"], child["tool_profile"], child["agent_tools"]) == (
        SONNET, "low", "read_only", ["read_file"])
    assert "Map the old way." in prompt and "new way" not in prompt


async def test_built_in_cards_name_their_profile_and_settings_effort(runtime):
    manager, parent, _ = runtime
    preview = manager.delegates.card_preview(parent, {"task": "Look", "agent_type": "explore", "max_steps": 4})
    assert preview == ("Start a subagent (explore · read-only · effort Fast; separate paid model requests, at most 4 "
                       "steps)\n\nTask: Look")
    assert manager.delegates.card_preview(parent, {"task": "Look"}).startswith("Start a subagent (general; separate")


async def test_settings_defaults_set_a_built_in_types_model_and_effort(runtime, monkeypatch):
    manager, parent, _ = runtime
    parent["reasoning_effort"] = "medium"
    manager.store.save(parent)
    manager.settings.values["subagent_defaults"] = subagent_defaults({
        "explore": {"model": GPT, "effort": ""}, "editor": {"model": "", "effort": "max"}})
    gateway = Gateway(monkeypatch, [lambda: calls_reply(
        ("delegate_task", {"task": "Look", "agent_type": "explore"}, "a"),
        ("delegate_task", {"task": "Fix", "agent_type": "editor"}, "b"),
        ("delegate_task", {"task": "Other"}, "c")), lambda: reply("Parent done.")])
    saved = await run_turn(manager, parent)
    children = {call: manager.store.get(card(saved, call)["child_session_id"]) for call in "abc"}
    assert (children["a"]["model"], children["a"]["reasoning_effort"]) == (GPT, "medium")
    assert "agent_effort" not in children["a"] and children["a"]["agent_model"] == GPT
    assert (children["b"]["model"], children["b"]["reasoning_effort"], children["b"]["agent_effort"]) == (
        "test-model", "max", "max")
    assert (children["c"]["model"], children["c"]["reasoning_effort"]) == ("test-model", "medium")
    endpoints = sorted(endpoint for endpoint, _ in gateway.requests["child"])
    assert endpoints == sorted([GPT, "test-model", "test-model"])
    assert [entry["model"] for entry in saved["subagents"]] == [GPT, "test-model", "test-model"]
    assert [entry["effort"] for entry in saved["subagents"]] == ["medium", "max", "medium"]


async def test_a_child_on_a_smaller_endpoint_gets_that_endpoints_budgets(runtime, monkeypatch):
    manager, parent, project = runtime
    use_endpoints(manager, GPT)
    # Settings budgets customised for Claude Opus 5.5, beyond GPT OSS 120B's defaults and maxima.
    manager.settings.values.update(model=OPUS, context_window=1_000_000, max_output_tokens=32_000)
    parent["model"] = OPUS
    manager.store.save(parent)
    write_agent(project, "mapper", agent_text("Maps.", model=GPT))
    gateway = Gateway(monkeypatch, [lambda: calls_reply(("delegate_task", {"task": "Map", "agent_type": "mapper"}, "a")),
                                    lambda: reply("Parent done.")])
    saved = await run_turn(manager, parent)
    (endpoint, body), = gateway.requests["child"]
    assert endpoint == GPT and body["max_tokens"] == 8_192
    assert gateway.requests["parent"][0][1]["max_tokens"] == 32_000
    child = manager.store.get(card(saved, "a")["child_session_id"])
    assert manager.request_limits(child) == (131_072, 8_192)
    assert manager.request_limits(parent) == (1_000_000, 32_000)
    # Lower Settings budgets stay: the endpoint's defaults are a ceiling, not a floor.
    manager.settings.values.update(context_window=100_000, max_output_tokens=4_096)
    assert manager.request_limits(child) == (100_000, 4_096)
    # The cap applies only while the child runs on its agent's endpoint.
    child["model"] = OPUS
    assert manager.request_limits(child) == (100_000, 4_096)
    manager.settings.values.update(context_window=1_000_000, max_output_tokens=32_000)
    assert manager.request_limits(child) == (1_000_000, 32_000)


async def test_a_child_on_the_settings_endpoint_keeps_the_budgets_set_for_it(runtime, monkeypatch):
    manager, parent, project = runtime
    manager.settings.values.update(model=OPUS, context_window=1_000_000, max_output_tokens=32_000)
    parent["model"] = GPT  # This chat runs on a smaller endpoint; the agent names the Settings one.
    manager.store.save(parent)
    write_agent(project, "mapper", agent_text("Maps.", model=OPUS))
    gateway = Gateway(monkeypatch, [lambda: calls_reply(("delegate_task", {"task": "Map", "agent_type": "mapper"}, "a")),
                                    lambda: reply("Parent done.")])
    saved = await run_turn(manager, parent)
    (endpoint, body), = gateway.requests["child"]
    assert endpoint == OPUS and body["max_tokens"] == 32_000
    child = manager.store.get(card(saved, "a")["child_session_id"])
    assert child["agent_model"] == OPUS
    assert manager.request_limits(child) == manager.request_limits({**parent, "model": OPUS}) == (1_000_000, 32_000)


async def test_a_chat_effort_change_does_not_reach_a_child_with_its_agents_effort(runtime):
    manager, parent, _ = runtime
    release = asyncio.Event()

    async def worker(child, prompt):
        await release.wait()
        await manager.event(child, "assistant", text="Done.")
    manager.run_databricks = worker
    manager.live[parent["id"]] = parent
    explore = asyncio.create_task(manager.delegates.delegate(parent, "Look", agent_type="explore"))
    general = asyncio.create_task(manager.delegates.delegate(parent, "Other"))
    for _ in range(300):
        if len(manager.active_children(parent)) == 2:
            break
        await asyncio.sleep(0.01)
    await manager.change_effort(parent, "xhigh")
    efforts = {child["agent_type"]: child.get("reasoning_effort") for child in manager.active_children(parent)}
    assert efforts == {"explore": "low", "general": "xhigh"}
    release.set()
    await asyncio.gather(explore, general)


async def test_a_queued_subagent_starts_with_the_chats_effort_of_that_time(runtime):
    manager, parent, _ = runtime
    parent["reasoning_effort"] = "medium"
    manager.store.save(parent)
    manager.live[parent["id"]] = parent
    release, efforts = asyncio.Event(), {}

    async def worker(child, prompt):
        efforts[prompt.split("\n")[1]] = child.get("reasoning_effort")
        await release.wait()
        await manager.event(child, "assistant", text="Done.")
    manager.run_databricks = worker
    kinds = [None, None, None, None, "explore"]  # Three run; the last two wait for a slot.
    cards = [await manager.event(parent, "tool", name="delegate_task", call_id=f"c{index}", input={"task": f"Task {index}"},
                                 output="", state="running") for index in range(len(kinds))]
    tasks = [asyncio.create_task(manager.delegates.delegate(parent, f"Task {index}", parent_event=cards[index],
                                                            agent_type=kind)) for index, kind in enumerate(kinds)]
    for _ in range(300):
        if len(manager.active_children(parent)) == 3 and all(item.get("delegation") for item in cards):
            break
        await asyncio.sleep(0.01)
    # A queued card shows the agent's own effort (explore: Settings → Subagents), not the chat's of now.
    assert [(item["delegation"]["status"], item["delegation"].get("effort")) for item in cards[3:]] == [
        ("queued", None), ("queued", "low")]
    await manager.change_effort(parent, "xhigh")
    release.set()
    await asyncio.gather(*tasks)
    assert efforts == {"Task 0": "medium", "Task 1": "medium", "Task 2": "medium", "Task 3": "xhigh", "Task 4": "low"}
    assert [item["delegation"]["effort"] for item in cards] == ["medium", "medium", "medium", "xhigh", "low"]


async def test_tools_turned_off_while_an_agent_waits_for_a_slot_are_left_out(runtime):
    manager, parent, project = runtime
    write_agent(project, "runner", agent_text("Runs.", profile="inherit", tools="[run_command, read_file]"))
    write_agent(project, "lister", agent_text("Lists.", tools="[list_files]"))
    manager.live[parent["id"]] = parent
    release, prompts = asyncio.Event(), {}

    async def worker(child, prompt):
        prompts[child.get("agent_type")] = (child.get("agent_tools"), prompt)
        await release.wait()
        await manager.event(child, "assistant", text="Done.")
    manager.run_databricks = worker
    running = [asyncio.create_task(manager.delegates.delegate(parent, f"Task {index}")) for index in range(3)]
    for _ in range(300):
        if len(manager.active_children(parent)) == 3:
            break
        await asyncio.sleep(0.01)
    queued = [asyncio.create_task(manager.delegates.delegate(parent, "Wait", agent_type=kind)) for kind in ("runner", "lister")]
    await asyncio.sleep(0.05)
    await manager.set_disabled_tools(parent, ["run_command", "list_files"])
    release.set()
    await asyncio.gather(*running)
    runner, lister = await asyncio.gather(*queued, return_exceptions=True)
    assert runner["status"] == "completed" and prompts["runner"][0] == ["read_file"]
    assert "Your tools: read_file." in prompts["runner"][1]
    assert isinstance(lister, ValueError) and "None of the lister agent's tools (list_files) is available" in str(lister)
    assert "lister" not in prompts


async def test_an_agent_max_steps_is_the_default_and_the_call_can_set_its_own(runtime):
    manager, parent, project = runtime
    write_agent(project, "mapper", agent_text(max_steps=3))
    seen = []

    async def worker(child, prompt):
        seen.append(child["max_steps"])
        await manager.event(child, "assistant", text="Done.")
    manager.run_databricks = worker
    await manager.delegates.delegate(parent, "Map", agent_type="mapper")
    await manager.delegates.delegate(parent, "Map", agent_type="mapper", max_steps=12)
    assert seen == [3, 12]


async def test_copies_of_a_custom_agents_subagent_keep_its_tools(runtime):
    manager, parent, project = runtime
    use_endpoints(manager, GPT)
    write_agent(project, "mapper", agent_text(tools="[read_file]", model=GPT, effort="high"))

    async def worker(child, prompt):
        await manager.event(child, "assistant", text="Report.")
    manager.run_databricks = worker
    manager.live[parent["id"]] = parent
    spawning = await manager.event(parent, "tool", name="delegate_task", call_id="c", input={"task": "Map"},
                                   output="", state="running")
    result = await manager.delegates.delegate(parent, "Map", parent_event=spawning, agent_type="mapper")
    await manager.update_event(parent, spawning, state="completed", output=json.dumps(result))
    saved, child = manager.store.get(parent["id"]), manager.store.get(result["child_session_id"])
    copies, root = fresh_copies(export_bundle([saved, child], parent["id"]), str(project))
    copied = next(session for session in copies if session["id"] != root)
    assert (copied["agent_type"], copied["agent_tools"], copied["agent_model"], copied["agent_effort"]) == (
        "mapper", ["read_file"], GPT, "high")
    entry = next(session for session in copies if session["id"] == root)["subagents"][0]
    assert (entry["agent_type"], entry["model"], entry["effort"], entry["task"]) == ("mapper", GPT, "high", "Map")
    # Without the spawning card the task comes from the child's first message, before the agent's block.
    for event in saved["events"]:
        event.pop("child_session_id", None)
    copies, root = fresh_copies(export_bundle([saved, child], parent["id"]), str(project))
    assert next(session for session in copies if session["id"] == root)["subagents"][0]["task"] == "Map"


async def test_the_agent_index_is_left_out_without_agents_and_of_subagents(runtime):
    manager, parent, project = runtime
    tools = manager.session_tools(parent)
    system, guidance = manager.context_inputs(parent, tools, "bypassPermissions")
    assert "Custom subagents" not in system["content"]
    write_agent(project, "mapper", agent_text("Maps modules."))
    system, guidance = manager.context_inputs(parent, tools, "bypassPermissions")
    assert "- mapper: Maps modules." in system["content"]
    assert any(source["path"] == "Agent index" for source in guidance["sources"])
    # Stable between requests (prompt caching), and not offered where delegate_task is not.
    assert manager.context_inputs(parent, tools, "bypassPermissions")[0] == system
    without = manager.context_inputs(parent, tools, "bypassPermissions", available=["read_file"])[0]
    assert "Custom subagents" not in without["content"]
    child = {**parent, "id": "child", "is_subagent": True}
    assert "Custom subagents" not in manager.context_inputs(child, tools, "bypassPermissions")[0]["content"]


# An agent file names only endpoints the user already uses.

def test_the_endpoints_in_use_are_settings_subagent_defaults_and_the_users_chats():
    from local_agent.custom_agents import limit_model, used_endpoints
    settings = types.SimpleNamespace(values={"model": OPUS, "subagent_defaults": {
        "explore": {"model": "", "effort": "low"}, "general": {"model": f" {SONNET} ", "effort": ""}}})
    chats = [{"model": "databricks-gpt-oss-20b"}, {"model": "chosen-by-a-file", "is_subagent": True}, {"model": ""}]
    assert used_endpoints(settings, chats) == {OPUS, SONNET, "databricks-gpt-oss-20b"}
    agent = {"name": "mapper", "model": GPT, "notes": []}
    limited = limit_model(agent, {OPUS})
    assert limited["model"] is None and limited["unused_model"] == GPT
    assert limited["notes"] == [f"Its model {GPT} is not an endpoint you use (the Settings model, Settings → Subagents "
                                "or one of your chats), so it runs on the chat's model."]
    assert limit_model(agent, {GPT}) is agent and limit_model({"model": None}, set()) == {"model": None}


async def test_an_agent_file_on_an_endpoint_you_do_not_use_runs_on_the_chats_model(runtime):
    manager, parent, project = runtime
    write_agent(project, "mapper", agent_text("Maps.", model=GPT, effort="high", tools="[read_file]"))
    parent["permission_mode"] = "manual"
    tools = manager.session_tools(parent)
    started = []

    async def worker(child, prompt):
        started.append(child)
        await manager.event(child, "assistant", text="Done.")
    manager.run_databricks = worker
    task = asyncio.create_task(manager.execute_tool(parent, tools, "delegate_task", {"task": "Map", "agent_type": "mapper"}, "d"))
    for _ in range(300):
        if manager.pending:
            break
        await asyncio.sleep(0.01)
    session_id, event_id = next(iter(manager.pending))
    preview = next(item for item in parent["events"] if item["id"] == event_id)["preview"]
    assert "· model " not in preview.split("\n")[0]  # The card's summary names no endpoint the agent will not use.
    assert f"Endpoint: the agent file names {GPT}, which you do not use; it runs on the chat's model." in preview
    manager.decide(session_id, event_id, True)
    result = json.loads(await asyncio.wait_for(task, 10))
    assert result["status"] == "completed" and started[0]["model"] == parent["model"] != GPT
    assert "agent_model" not in started[0] and started[0]["reasoning_effort"] == "high"  # Its effort still applies.
    # Once the user uses that endpoint (a chat of theirs runs on it), the agent may name it.
    use_endpoints(manager, GPT)
    parent["permission_mode"] = "bypassPermissions"
    result = json.loads(await manager.execute_tool(parent, tools, "delegate_task", {"task": "Map", "agent_type": "mapper"}, "e"))
    assert result["status"] == "completed" and started[1]["model"] == GPT


async def test_the_agents_route_marks_an_endpoint_you_do_not_use(api):
    client, manager, settings, project = api
    write_agent(project, "mapper", agent_text("Maps.", model=GPT))
    agent = (await client.get("/api/agents")).json()["custom"][0]
    assert agent["model"] is None and agent["unused_model"] == GPT
    assert any("is not an endpoint you use" in note for note in agent["notes"])
    use_endpoints(manager, GPT)
    agent = (await client.get("/api/agents")).json()["custom"][0]
    assert agent["model"] == GPT and "unused_model" not in agent
