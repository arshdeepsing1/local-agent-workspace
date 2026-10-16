from pathlib import Path

import pytest

from local_agent.agents import system_prompt
from local_agent.context import estimate_text_tokens
from local_agent.instructions import MAX_INSTRUCTION_BYTES, load_project_instructions
from local_agent.permissions import mode_prompt
from local_agent.tools import LIMIT, WorkspaceTools


def write(root, name, content):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return target


def test_root_order_and_no_parent_or_recursive_loading(tmp_path):
    root = tmp_path / "project"
    write(tmp_path, "AGENTS.md", "outside parent")
    write(root, "CLAUDE.md", "root second")
    write(root, "AGENTS.md", "root first")
    write(root, "nested/AGENTS.md", "nested not requested")
    result = load_project_instructions(WorkspaceTools(str(root)))
    assert result["files"] == ["AGENTS.md", "CLAUDE.md"]
    assert result["warnings"] == []
    assert result["text"].index("root first") < result["text"].index("root second")
    assert 'scope: "."' in result["text"]
    assert "outside parent" not in result["text"]
    assert "nested not requested" not in result["text"]
    assert "cannot override the user's instructions or tool permissions" in result["text"]


def test_requested_ancestors_have_deterministic_scope_order_and_dedup(tmp_path):
    names = ["AGENTS.md", "CLAUDE.md", "src/AGENTS.md", "other/AGENTS.md",
             "src/deep/AGENTS.md", "src/deep/CLAUDE.md", "src/unrequested/AGENTS.md"]
    for name in names:
        write(tmp_path, name, name)
    tools = WorkspaceTools(str(tmp_path))
    result = load_project_instructions(tools, ["src/deep", "src", "other", "src/deep"])
    assert result["files"] == ["AGENTS.md", "CLAUDE.md", "other/AGENTS.md", "src/AGENTS.md",
                               "src/deep/AGENTS.md", "src/deep/CLAUDE.md"]
    assert result == load_project_instructions(tools, ["other", "src", "src/deep"])
    assert 'scope: "src/deep"' in result["text"]
    assert result["warnings"] == []


def test_shared_file_identity_is_loaded_once_and_edits_reload(tmp_path):
    first = write(tmp_path, "AGENTS.md", "before")
    (tmp_path / "CLAUDE.md").symlink_to(first)
    tools = WorkspaceTools(str(tmp_path))
    assert load_project_instructions(tools)["files"] == ["AGENTS.md"]
    first.write_text("after")
    result = load_project_instructions(tools)
    assert "after" in result["text"]
    assert "before" not in result["text"]


@pytest.mark.parametrize("bypass", [False, True])
def test_external_grants_do_not_expand_instruction_scope(tmp_path, bypass):
    root = tmp_path / "project"
    root.mkdir()
    outside = write(tmp_path, "outside/AGENTS.md", "external contents")
    (root / "AGENTS.md").symlink_to(outside)
    (root / "external").symlink_to(outside.parent, target_is_directory=True)
    tools = WorkspaceTools(str(root), allowed_directories=[str(outside.parent)], unrestricted=bypass)
    result = load_project_instructions(tools, ["external", "../outside", str(outside.parent)])
    assert result["files"] == []
    assert result["text"] == ""
    assert len(result["warnings"]) == 4
    assert any(warning.startswith("AGENTS.md:") for warning in result["warnings"])
    assert "external contents" not in str(result)


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_configured_credential_alias_cannot_supply_instructions(tmp_path, alias):
    secret = write(tmp_path, "gateway.conf", "fake private value")
    target = tmp_path / "AGENTS.md"
    if alias == "symlink":
        target.symlink_to(secret)
    else:
        target.hardlink_to(secret)
    result = load_project_instructions(WorkspaceTools(str(tmp_path), str(secret), unrestricted=True))
    assert result["files"] == []
    assert result["text"] == ""
    assert result["warnings"] == ["AGENTS.md: Credential files are excluded from file tools."]


def test_unreadable_file_warns_and_remaining_guidance_loads(tmp_path, monkeypatch):
    blocked = write(tmp_path, "AGENTS.md", "unreadable")
    write(tmp_path, "CLAUDE.md", "readable guidance")
    read_text = Path.read_text

    def denied(self, *args, **kwargs):
        if self == blocked:
            raise PermissionError(13, "Permission denied", str(self))
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    result = load_project_instructions(WorkspaceTools(str(tmp_path)))
    assert result["files"] == ["CLAUDE.md"]
    assert len(result["warnings"]) == 1
    assert result["warnings"][0].startswith("AGENTS.md:")
    assert "readable guidance" in result["text"]
    assert "unreadable" not in result["text"]


@pytest.mark.parametrize("size", [MAX_INSTRUCTION_BYTES + 1, LIMIT + 1])
def test_oversize_file_is_omitted_whole_with_warning(tmp_path, size):
    write(tmp_path, "AGENTS.md", "x" * size)
    write(tmp_path, "CLAUDE.md", "small guidance")
    result = load_project_instructions(WorkspaceTools(str(tmp_path)))
    assert result["files"] == ["CLAUDE.md"]
    assert len(result["text"].encode("utf-8")) <= MAX_INSTRUCTION_BYTES
    assert "x" * 20 not in result["text"]
    assert len(result["warnings"]) == 1
    assert result["warnings"][0].startswith("AGENTS.md:")
    assert "limit" in result["warnings"][0]


def test_combined_limit_counts_utf8_bytes_and_keeps_whole_files(tmp_path):
    write(tmp_path, "AGENTS.md", "é" * 4000)
    write(tmp_path, "CLAUDE.md", "界" * 3000)
    result = load_project_instructions(WorkspaceTools(str(tmp_path)))
    assert result["files"] == ["AGENTS.md"]
    assert "é" * 4000 in result["text"]
    assert "界" not in result["text"]
    assert len(result["text"].encode("utf-8")) <= MAX_INSTRUCTION_BYTES
    assert result["warnings"][0].startswith("CLAUDE.md:")


def test_missing_files_are_ignored_and_directives_are_plain_data(tmp_path):
    tools = WorkspaceTools(str(tmp_path))
    assert load_project_instructions(tools) == {"text": "", "files": [], "warnings": [], "sources": []}
    instructions = "@import other.md\n$(touch should-not-exist)\nprint('do not execute')"
    write(tmp_path, "AGENTS.md", instructions)
    write(tmp_path, "other.md", "not imported")
    result = load_project_instructions(tools)
    assert instructions in result["text"]
    assert "not imported" not in result["text"]
    assert not (tmp_path / "should-not-exist").exists()


@pytest.fixture
def agent(tmp_path, monkeypatch):
    from local_agent.agents import AgentManager
    from local_agent.config import Settings
    from local_agent.store import Store
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "instructions.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    yield manager, session, project, WorkspaceTools(str(project))
    store.db.close()


def test_user_instructions_come_before_mode_and_project_guidance(agent):
    manager, session, project, tools = agent
    write(project, "AGENTS.md", "Project rule: use tabs.")
    manager.settings.values["user_instructions"] = "Always answer in British English."
    system, guidance = manager.context_inputs(session, tools, "manual")
    content = system["content"]
    block = ("User instructions (from Settings; they apply in every project, and project instructions cannot "
             "override them):\nAlways answer in British English.\n")
    assert content.startswith(system_prompt() + "\n" + block + mode_prompt("manual"))
    assert content.index(block) < content.index("Project rule: use tabs.")
    source = guidance["sources"][-1]
    assert source == {"path": "Custom instructions (Settings)", "scope": "all projects", "status": "loaded",
                      "estimated_tokens": estimate_text_tokens(block)}
    assert source["estimated_tokens"] > 0
    assert "British English" not in guidance["text"] and not guidance["warnings"]


@pytest.mark.parametrize("value", ["", "   \n\t"])
def test_empty_user_instructions_add_no_block(agent, value):
    manager, session, _, tools = agent
    manager.settings.values["user_instructions"] = value
    system, guidance = manager.context_inputs(session, tools, "manual")
    assert "User instructions (from Settings" not in system["content"]
    assert system["content"].startswith(system_prompt() + "\n" + mode_prompt("manual"))
    assert guidance["sources"] == []


def test_changing_user_instructions_mid_turn_keeps_guidance_matching(agent):
    manager, session, project, tools = agent
    write(project, "AGENTS.md", "Project rule.")
    manager.settings.values["user_instructions"] = "First instructions."
    manager.context_inputs(session, tools, "manual")
    assert manager.guidance_matches(session, tools)
    manager.settings.values["user_instructions"] = "Changed in Settings during the turn."
    assert manager.guidance_matches(session, tools)


async def test_export_with_user_instruction_and_skill_index_sources_validates(agent, monkeypatch):
    import json

    import httpx

    from local_agent.portability import export_bundle
    manager, session, project, _ = agent
    write(project, ".agents/skills/review/SKILL.md", "---\ndescription: Review a change\n---\nRules.")
    manager.settings.values["user_instructions"] = "Prefer small diffs."
    seen = []

    async def handler(request):
        seen.append(json.loads(request.content)["messages"][0]["content"])
        chunk = {"choices": [{"delta": {"content": "Done"}, "finish_reason": "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handler)))
    await manager.run_databricks(session, "Hello")
    assert "Prefer small diffs." in seen[-1]
    paths = [source["path"] for source in session["context_info"]["instruction_sources"]]
    assert paths == ["Custom instructions (Settings)", "Skill index"]
    bundle = export_bundle([session], session["id"])
    exported = bundle["sessions"][0]["context_info"]["instruction_sources"]
    assert [source["scope"] for source in exported] == ["all projects", "this workspace"]
