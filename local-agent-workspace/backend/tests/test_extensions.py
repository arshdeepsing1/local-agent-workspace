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
    return ExtensionManager(SimpleNamespace(state_dir=state, redact=lambda text: text.replace("secret-token", "[REDACTED]")))


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


def test_skills_discover_only_workspace_agents_and_load_bounded_text(manager, tmp_path):
    tools = WorkspaceTools(str(tmp_path))
    folder = tmp_path / ".agents/skills/review"
    folder.mkdir(parents=True)
    text = "---\nname: Code review\ndescription: 'Review a proposed change'\n---\nRead the diff.\n"
    (folder / "SKILL.md").write_text(text)
    other = tmp_path / ".claude/skills/ignored"
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
