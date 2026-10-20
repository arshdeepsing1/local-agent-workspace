"""M5c: the user's instruction files, AGENTS.local.md / CLAUDE.local.md, bounded @imports and path-scoped
project rules (instructions.py), and how they reach the system message and the write deferral."""
import json
import os
from unittest.mock import AsyncMock

import pytest

from local_agent import instructions
from local_agent.agents import system_prompt
from local_agent.instructions import (MAX_IMPORTS_PER_FILE, MAX_INSTRUCTION_PATHS, MAX_USER_FILE_BYTES,
                                      load_project_instructions, load_user_instructions, note_instruction_path,
                                      user_tools)
from local_agent.permissions import mode_prompt
from local_agent.tools import WorkspaceTools


def write(root, name, content):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return target


def loaded(result):
    return [source["path"] for source in result["sources"] if source["status"] != "omitted"]


# Precedence: shared then local in each folder, the rules for every file after the root, matched rules last

def test_local_files_follow_the_shared_file_of_their_folder(tmp_path):
    for name in ("CLAUDE.local.md", "AGENTS.local.md", "CLAUDE.md", "AGENTS.md", "src/AGENTS.local.md", "src/AGENTS.md"):
        write(tmp_path, name, f"text of {name}")
    result = load_project_instructions(WorkspaceTools(str(tmp_path)), ["src"])
    assert result["files"] == ["AGENTS.md", "AGENTS.local.md", "CLAUDE.md", "CLAUDE.local.md", "src/AGENTS.md",
                               "src/AGENTS.local.md"]
    text = result["text"]
    assert text.index("text of AGENTS.md") < text.index("text of AGENTS.local.md") < text.index("text of CLAUDE.md")
    assert '[Project instructions: "src/AGENTS.local.md"; scope: "src"]' in text


def test_rules_load_after_the_root_and_path_rules_after_deeper_scopes(tmp_path):
    write(tmp_path, "AGENTS.md", "root guidance")
    write(tmp_path, "src/AGENTS.md", "src guidance")
    write(tmp_path, ".agents/rules/style.md", "Always use tabs.")
    write(tmp_path, ".agents/rules/python.md", '---\ndescription: Python files\npaths: ["**/*.py"]\n---\nUse type hints.')
    tools = WorkspaceTools(str(tmp_path))
    result = load_project_instructions(tools, ["src"])
    assert loaded(result) == ["AGENTS.md", ".agents/rules/style.md", "src/AGENTS.md"]
    assert "Use type hints" not in result["text"]
    result = load_project_instructions(tools, ["src"], ["src/main.py"])
    assert loaded(result) == ["AGENTS.md", ".agents/rules/style.md", "src/AGENTS.md", ".agents/rules/python.md"]
    assert '[Project rule: ".agents/rules/python.md"; applies to files matching: ["**/*.py"]]\nUse type hints.' in result["text"]
    assert "description: Python files" not in result["text"]  # The frontmatter is not guidance.
    assert result["sources"][-1]["scope"] == "files matching **/*.py"
    assert '[Project rule: ".agents/rules/style.md"; scope: "."]' in result["text"]


def test_rule_paths_take_a_list_or_commas_and_claude_rules_are_read_after_agents_rules(tmp_path):
    write(tmp_path, ".agents/rules/docs.md", "---\npaths:\n  - docs/**\n  - '*.rst'\n---\nDocs rule.")
    write(tmp_path, ".claude/rules/docs.md", "Shadowed by the .agents rule of the same name.")
    write(tmp_path, ".claude/rules/web.md", "---\npaths: web/*.js, web/*.css\n---\nWeb rule.")
    tools = WorkspaceTools(str(tmp_path))
    result = load_project_instructions(tools, [], ["web/app.css", "notes/readme.rst"])
    assert loaded(result) == [".agents/rules/docs.md", ".claude/rules/web.md"]
    assert "Shadowed" not in result["text"]
    assert load_project_instructions(tools, [], ["web/sub/app.js"])["files"] == []


@pytest.mark.parametrize("field", ["src/**/*.{ts,tsx}, docs/*.md", "[src/**/*.{ts,tsx}, docs/*.md]"])
def test_a_comma_inside_glob_braces_stays_in_its_glob(tmp_path, field):
    write(tmp_path, ".agents/rules/ts.md", f"---\npaths: {field}\n---\nTypeScript rule.")
    tools = WorkspaceTools(str(tmp_path))
    for path in ("src/app.ts", "src/ui/view.tsx", "docs/guide.md"):
        result = load_project_instructions(tools, [], [path])
        assert loaded(result) == [".agents/rules/ts.md"], path
        assert ('[Project rule: ".agents/rules/ts.md"; applies to files matching: ["src/**/*.{ts,tsx}", "docs/*.md"]]'
                in result["text"])
    assert load_project_instructions(tools, [], ["src/app.js"])["files"] == []


@pytest.mark.parametrize("field", ["paths: []", "paths:"])
def test_an_empty_paths_field_omits_the_rule_instead_of_applying_everywhere(tmp_path, field):
    write(tmp_path, ".agents/rules/none.md", f"---\n{field}\n---\nEmpty paths rule.")
    write(tmp_path, ".agents/rules/plain.md", "---\ndescription: no paths\n---\nPlain rule.")
    result = load_project_instructions(WorkspaceTools(str(tmp_path)), [], ["src/app.py"])
    assert result["files"] == [".agents/rules/plain.md"] and "Empty paths rule" not in result["text"]
    assert result["warnings"] == ['.agents/rules/none.md: paths is empty: list the globs the rule applies to, such as '
                                  '["src/**/*.py"], or leave paths out for a rule that applies everywhere.']


def test_the_imports_of_a_path_scoped_rule_are_headed_with_its_globs(tmp_path):
    write(tmp_path, ".agents/rules/python.md", '---\npaths: ["src/**/*.py"]\n---\nPython rule. See @../../docs/py.md')
    write(tmp_path, "docs/py.md", "No print in library code. @more.md")
    write(tmp_path, "docs/more.md", "More Python notes.")
    result = load_project_instructions(WorkspaceTools(str(tmp_path)), [], ["src/app.py"])
    text = result["text"]
    assert '[Imported by ".agents/rules/python.md": "docs/py.md"; applies to files matching: ["src/**/*.py"]]' in text
    assert '[Imported by "docs/py.md": "docs/more.md"; applies to files matching: ["src/**/*.py"]]' in text
    assert 'scope: "."' not in text
    assert {source["path"]: source["scope"] for source in result["sources"]} == {
        ".agents/rules/python.md": "files matching src/**/*.py", "docs/py.md": "files matching src/**/*.py",
        "docs/more.md": "files matching src/**/*.py"}


def test_a_broken_or_oversize_rule_is_omitted_with_a_reason(tmp_path):
    write(tmp_path, ".agents/rules/broken.md", "---\npaths: [unclosed\n---\nBroken.")
    write(tmp_path, ".agents/rules/empty.md", "---\npaths: [1, 2]\n---\nNot globs.")
    write(tmp_path, ".agents/rules/large.md", "x" * 8001)
    write(tmp_path, ".agents/rules/fine.md", "Fine rule.")
    write(tmp_path, ".agents/rules/notes.txt", "Not a rule.")
    result = load_project_instructions(WorkspaceTools(str(tmp_path)))
    assert result["files"] == [".agents/rules/fine.md"]
    reasons = {source["path"]: source.get("reason", "") for source in result["sources"]}
    assert "must close on the same line" in reasons[".agents/rules/broken.md"]
    assert "glob patterns" in reasons[".agents/rules/empty.md"]
    assert reasons[".agents/rules/large.md"] == "A rule file is at most 8,000 bytes."
    assert "Not a rule" not in result["text"]


def test_a_rules_folder_outside_the_workspace_is_refused(tmp_path):
    outside = write(tmp_path / "outside", "rules/escape.md", "outside rule")
    root = tmp_path / "project"
    (root / ".agents").mkdir(parents=True)
    (root / ".agents/rules").symlink_to(outside.parent, target_is_directory=True)
    result = load_project_instructions(WorkspaceTools(str(root)))
    assert result["files"] == [] and "outside rule" not in result["text"]
    assert result["warnings"][0].startswith(".agents/rules: Instructions and their imports must remain inside")


def test_instruction_paths_are_bounded_and_unique():
    session = {}
    for number in range(MAX_INSTRUCTION_PATHS + 5):
        note_instruction_path(session, f"src/file{number}.py")
    note_instruction_path(session, f"src/file{MAX_INSTRUCTION_PATHS + 4}.py")
    paths = session["instruction_paths"]
    assert len(paths) == MAX_INSTRUCTION_PATHS and paths[0] == "src/file5.py" and len(set(paths)) == len(paths)


# @imports: relative to the importing file, outside code, three levels deep, cycle-safe, confined

def test_imports_load_after_their_file_with_the_importing_scope(tmp_path):
    write(tmp_path, "AGENTS.md", "See @docs/style.md for style.\n")
    write(tmp_path, "docs/style.md", "Style: imports @tone.md too.")
    write(tmp_path, "docs/tone.md", "Tone: plain.")
    result = load_project_instructions(WorkspaceTools(str(tmp_path)))
    assert result["files"] == ["AGENTS.md", "docs/style.md", "docs/tone.md"]
    assert '[Imported by "AGENTS.md": "docs/style.md"; scope: "."]\nStyle:' in result["text"]
    assert '[Imported by "docs/style.md": "docs/tone.md"; scope: "."]' in result["text"]
    assert "See @docs/style.md for style." in result["text"]  # The line stays as written.
    assert result["warnings"] == []


def test_imports_nest_three_levels_and_cycles_load_each_file_once(tmp_path):
    write(tmp_path, "AGENTS.md", "@one.md")
    write(tmp_path, "one.md", "one @two.md @AGENTS.md")
    write(tmp_path, "two.md", "two @three.md @one.md")
    write(tmp_path, "three.md", "three @four.md")
    write(tmp_path, "four.md", "four")
    result = load_project_instructions(WorkspaceTools(str(tmp_path)))
    assert result["files"] == ["AGENTS.md", "one.md", "two.md", "three.md"]
    assert result["text"].count("[Imported by") == 3 and "four" not in result["text"].split("three @four.md")[1]
    assert result["warnings"] == ["three.md → @four.md: Imports nest at most 3 levels deep."]


def test_a_file_imports_at_most_ten_files(tmp_path):
    count = MAX_IMPORTS_PER_FILE + 2
    write(tmp_path, "AGENTS.md", " ".join(f"@part{n}.md" for n in range(count)))
    for n in range(count):
        write(tmp_path, f"part{n}.md", f"part {n}")
    result = load_project_instructions(WorkspaceTools(str(tmp_path)))
    assert result["files"] == ["AGENTS.md", *[f"part{n}.md" for n in range(MAX_IMPORTS_PER_FILE)]]
    assert result["warnings"] == [f"AGENTS.md → @part{MAX_IMPORTS_PER_FILE}.md: A file imports at most 10 files; "
                                  "the later imports are not loaded."]


def test_imports_in_code_mentions_and_missing_files(tmp_path):
    write(tmp_path, "AGENTS.md", "Code: `@docs/span.md`\n```\n@docs/fenced.md\n```\nAsk @alice, use @pytest.fixture, "
                                 "install @types/node, mail a@docs.md, see @missing/file.md.")
    for name in ("docs/span.md", "docs/fenced.md", "alice", "docs.md"):
        write(tmp_path, name, f"must not load {name}")
    result = load_project_instructions(WorkspaceTools(str(tmp_path)))
    assert "must not load docs" not in result["text"]
    # @alice names a file here, so it is an import; prose such as @pytest.fixture stays text without a warning.
    assert result["files"] == ["AGENTS.md", "alice"]
    assert result["warnings"] == ["AGENTS.md → @missing/file.md: Import not found; the line stays as text."]


@pytest.mark.parametrize("kind", ["parent", "symlink", "absolute", "home", "credential", "env"])
def test_imports_cannot_leave_the_workspace_or_read_secrets(tmp_path, kind):
    root = tmp_path / "project"
    outside = write(tmp_path, "outside/secret-notes.md", "outside contents")
    credential = write(tmp_path, "gateway.conf", "fake credential value")
    write(root, ".env", "FAKE=value")
    token = {"parent": "@../outside/secret-notes.md", "symlink": "@linked/secret-notes.md", "absolute": f"@{outside}",
             "home": "@~/secret-notes.md", "credential": "@conf/gateway.md", "env": "@.env"}[kind]
    (root / "linked").symlink_to(outside.parent, target_is_directory=True)
    (root / "conf").mkdir()
    (root / "conf/gateway.md").symlink_to(credential)
    write(root, "AGENTS.md", f"Read {token} first.")
    result = load_project_instructions(WorkspaceTools(str(root), str(credential), unrestricted=True,
                                                      allowed_directories=[str(outside.parent)]))
    assert result["files"] == ["AGENTS.md"]
    assert "outside contents" not in result["text"] and "fake credential value" not in result["text"]
    assert "FAKE=value" not in result["text"]
    assert len(result["warnings"]) == 1 and result["warnings"][0].startswith(f"AGENTS.md → {token}:")


# The user's files: ~/.agents/AGENTS.md (on by default) and ~/.claude/CLAUDE.md (off by default)

def test_the_user_files_follow_their_settings_and_have_their_own_budget(tmp_path):
    home = instructions.user_home()
    write(home, ".agents/AGENTS.md", "Personal: prefer small commits. See @~/notes/tone.md")
    write(home, "notes/tone.md", "Tone: plain words.")
    write(home, ".claude/CLAUDE.md", "y" * (MAX_USER_FILE_BYTES + 3000))
    tools = user_tools()
    result = load_user_instructions({"user_agents_file": True, "user_claude_file": False}, tools)
    assert [source["path"] for source in result["sources"]] == ["~/.agents/AGENTS.md", "~/notes/tone.md"]
    assert result["text"].startswith('[User instructions file: "~/.agents/AGENTS.md"]\nPersonal: prefer small commits.')
    assert "Tone: plain words." in result["text"]
    assert {source["scope"] for source in result["sources"]} == {"all projects"}
    both = load_user_instructions({"user_agents_file": True, "user_claude_file": True}, tools)
    claude = both["sources"][-1]
    assert claude["path"] == "~/.claude/CLAUDE.md" and claude["status"] == "trimmed"
    assert len(both["text"].encode("utf-8")) < 2 * MAX_USER_FILE_BYTES
    assert both["warnings"] == ["~/.claude/CLAUDE.md: " + claude["reason"]]
    assert load_user_instructions({}, tools) == {"text": "", "warnings": [], "sources": []}


def test_loaded_files_and_imports_have_the_token_and_secret_shapes_redacted(agent):
    manager, session, project, _ = agent
    home = instructions.user_home()
    write(project, "AGENTS.md", "See @notes/dev.txt for the gateway.")
    write(project, "notes/dev.txt", "Gateway key test-token; GITHUB_TOKEN=abc123")
    write(project, ".agents/rules/keys.md", "Rotate test-token monthly.")
    write(home, ".agents/AGENTS.md", "My setup: @~/notes/gateway.cfg")
    write(home, "notes/gateway.cfg", "[DEFAULT]\ntoken = test-token\npassword: hunter22\n")
    tools = manager.session_tools(session)
    system, guidance = manager.context_inputs(session, tools, "manual")
    content = system["content"]
    assert "test-token" not in content and "abc123" not in content and "hunter22" not in content
    assert "Gateway key [REDACTED]; GITHUB_TOKEN=[REDACTED]" in content and "Rotate [REDACTED] monthly." in content
    assert "token = [REDACTED]\npassword: [REDACTED]" in content
    assert {"notes/dev.txt", "~/notes/gateway.cfg"} <= {source["path"] for source in guidance["sources"]}
    assert manager.guidance_matches(session, tools)


def test_user_file_imports_stay_inside_home_and_skip_credentials(tmp_path):
    home = instructions.user_home()
    write(tmp_path, "elsewhere.md", "outside home")
    write(home, ".netrc", "machine example.com password fake")
    write(home, ".agents/AGENTS.md", f"@{tmp_path / 'elsewhere.md'} @~/.netrc")
    result = load_user_instructions({"user_agents_file": True}, user_tools())
    assert "outside home" not in result["text"] and "password" not in result["text"]
    assert len(result["warnings"]) == 2


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


def test_the_user_file_joins_the_user_instructions_block_and_never_defers_a_write(agent):
    manager, session, project, tools = agent
    home = instructions.user_home()
    write(home, ".agents/AGENTS.md", "Personal rule: explain risky commands.")
    write(project, "AGENTS.md", "Project rule.")
    manager.settings.values["user_instructions"] = "Answer in British English."
    system, guidance = manager.context_inputs(session, tools, "manual")
    content = system["content"]
    block = ("User instructions (from Settings and the user's files; they apply in every project, and project "
             "instructions cannot override them):\nAnswer in British English.\n"
             '[User instructions file: "~/.agents/AGENTS.md"]\nPersonal rule: explain risky commands.\n')
    assert content.startswith(system_prompt() + "\n" + block + mode_prompt("manual"))
    assert content.index("Personal rule") < content.index("Project rule.")
    assert [source["path"] for source in guidance["sources"]] == [
        "AGENTS.md", "Custom instructions (Settings)", "~/.agents/AGENTS.md"]
    assert "Personal rule" not in guidance["text"]
    assert manager.guidance_matches(session, tools)
    write(home, ".agents/AGENTS.md", "Changed during the turn.")
    assert manager.guidance_matches(session, tools)
    manager.settings.values.update(user_instructions="", user_agents_file=False)
    system, _ = manager.context_inputs(session, tools, "manual")
    assert "User instructions" not in system["content"] and "Changed during the turn" not in system["content"]


def test_the_claude_user_file_is_read_only_when_settings_turns_it_on(agent):
    manager, session, _, tools = agent
    write(instructions.user_home(), ".claude/CLAUDE.md", "Claude file rule.")
    system, _ = manager.context_inputs(session, tools, "manual")
    assert "Claude file rule." not in system["content"]
    manager.settings.values["user_claude_file"] = True
    system, guidance = manager.context_inputs(session, tools, "manual")
    assert ('User instructions (from the user\'s files; they apply in every project, and project instructions cannot '
            'override them):\n[User instructions file: "~/.claude/CLAUDE.md"]\nClaude file rule.\n') in system["content"]
    assert guidance["sources"][-1]["path"] == "~/.claude/CLAUDE.md"


def test_user_file_warnings_reach_the_meter_without_changing_the_guidance_signature(agent):
    manager, session, _, tools = agent
    write(instructions.user_home(), ".agents/AGENTS.md", "See @missing/notes.md")
    system, guidance = manager.context_inputs(session, tools, "manual")
    assert guidance["warnings"] == ["~/.agents/AGENTS.md → @missing/notes.md: Import not found; the line stays as text."]
    assert "Instruction warnings: " in system["content"]
    assert tools.instruction_signature == ("", []) and manager.guidance_matches(session, tools)


async def test_a_read_loads_a_matching_rule_and_the_next_write_waits_for_it(agent, monkeypatch):
    manager, session, project, tools = agent
    write(project, ".agents/rules/python.md", '---\npaths: ["src/**/*.py"]\n---\nRun mypy after Python edits.')
    write(project, "src/app.py", "print('hi')\n")
    write(project, "README.md", "readme")
    monkeypatch.setattr(manager, "approve", AsyncMock(return_value=True))
    session["permission_mode"] = "acceptEdits"
    system, _ = manager.context_inputs(session, tools, "acceptEdits")
    assert "Run mypy" not in system["content"]
    await manager.execute_tool(session, tools, "read_file", {"path": "README.md"}, "readme")
    assert manager.guidance_matches(session, tools)
    await manager.execute_tool(session, tools, "read_file", {"path": "src/app.py"}, "read")
    assert session["instruction_paths"] == ["README.md", "src/app.py"]
    result = await manager.execute_tool(session, tools, "edit_file",
                                        {"path": "src/app.py", "old_text": "hi", "new_text": "hello"}, "edit")
    assert "Project instructions were discovered or changed" in result
    assert (project / "src/app.py").read_text() == "print('hi')\n"
    system, guidance = manager.context_inputs(session, tools, "acceptEdits")
    assert "Run mypy after Python edits." in system["content"]
    assert guidance["sources"][-1]["scope"] == "files matching src/**/*.py"
    await manager.execute_tool(session, tools, "edit_file", {"path": "src/app.py", "old_text": "hi", "new_text": "hello"},
                               "retry")
    assert (project / "src/app.py").read_text() == "print('hello')\n"


def test_instruction_paths_stay_out_of_the_pages_exports_and_rewind_backups(agent):
    from local_agent.agents import public_session
    from local_agent.portability import export_bundle
    from local_agent.portability_api import backup_copy
    manager, session, _, _ = agent
    session["instruction_paths"] = ["src/app.py"]
    manager.store.save(session)
    assert "instruction_paths" not in public_session(session)
    assert manager.store.get(session["id"])["instruction_paths"] == ["src/app.py"]  # Kept beside the conversation.
    assert "instruction_paths" not in export_bundle([session], session["id"])["sessions"][0]
    assert "instruction_paths" not in backup_copy(session)
    path = manager.store._path(session["id"])  # A field record of the private conversation file.
    fields = {line["name"]: line["data"] for line in map(json.loads, path.read_text().splitlines()) if line["record"] == "field"}
    assert fields["instruction_paths"] == ["src/app.py"] and "instruction_paths" not in json.loads(path.read_text().splitlines()[0])["data"]
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
