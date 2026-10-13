"""The default code folder (LOCAL_AGENT_CODING_DIR) and the Python virtual environment
(LOCAL_AGENT_PYTHON_VENV): checks, Settings and .env precedence, the standing folder grant,
and the environment every app-run command gets."""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from local_agent import agents as agents_module
from local_agent import tools as tools_module
from local_agent import user_paths, workspace_folders
from local_agent.agents import AgentManager
from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.store import Store
from local_agent.tools import EXCLUDED_ROOT, WorkspaceTools


@pytest.fixture
def layout(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    state = tmp_path / "data" / "state"
    state.mkdir(parents=True)
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("local_agent.config.APP_ROOT", app)
    return SimpleNamespace(tmp=tmp_path, home=home, project=project, state=state, app=app)


def make_venv(folder):
    (folder / "bin").mkdir(parents=True)
    for name, body in (("python", 'echo "venv-python $VIRTUAL_ENV"'), ("pytest", "echo venv-pytest")):
        program = folder / "bin" / name
        program.write_text("#!/bin/sh\n" + body + "\n")
        program.chmod(0o755)
    (folder / "pyvenv.cfg").write_text("home = /usr/bin\nversion_info = 3.12.4.final.0\n")
    return folder


def settings_for(layout, **values):
    settings = Settings(layout.state)
    settings.values.update(workspace=str(layout.project), env_file="", **values)
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    return settings


@pytest.fixture
async def runtime(layout):
    """Build an AgentManager on demand: make(**settings values) -> (manager, session, tools)."""
    created = []

    def make(**values):
        settings = settings_for(layout, **values)
        store = Store(layout.tmp / f"tests-{len(created)}.sqlite3")
        manager = AgentManager(store, settings)
        session = store.create(settings.values)
        session.update(title="Env", title_generated=True, permission_mode="acceptEdits")
        store.save(session)
        tools = manager.session_tools(session)
        tools.instruction_signature = ("", [])
        tools.skill_signature = ""
        created.append((manager, store))
        return manager, session, tools
    yield make
    for manager, store in created:
        await manager.jobs.shutdown()
        store.db.close()


async def pending_card(manager):
    for _ in range(300):
        if manager.pending:
            return next(iter(manager.pending))
        await asyncio.sleep(0.01)
    raise AssertionError("Expected an approval card")


def tool_events(session, name):
    return [event for event in session["events"] if event.get("type") == "tool" and event.get("name") == name]


async def run_command(manager, session, tools, command, call_id="c"):
    result = await manager.execute_tool(session, tools, "run_command", {"command": command}, call_id)
    try:
        return json.loads(result)["output"]
    except (ValueError, KeyError):
        return result


# 16 check_coding_dir


def check(layout, value, env_file=""):
    return user_paths.check_coding_dir(value, state_dir=layout.state, env_file=env_file, app_root=layout.app,
                                       home=layout.home.resolve())


@pytest.mark.parametrize("value, reason", [
    ("/", "filesystem root is too broad"),
    ("/usr/local/code", "system folder"),
    ("{home}", "home folder (or a folder containing it) is too broad"),
    ("{tmp}", "home folder (or a folder containing it) is too broad"),
    ("{state}", "app's state folder or inside it"),
    ("{state}/x", "app's state folder or inside it"),
    ("{tmp}/data", "contains the app's state folder"),
    ("{app}/sub", "inside the app's own folder"),
    ("{home}/p/credentials/x", "credential location"),
    ("{home}/p/.git", "internal or generated folder"),
    ("{home}/p/.venv", "internal or generated folder"),
    ("{home}/.ssh/x", "hidden configuration folder"),
    ("{home}/.config/x", "hidden configuration folder"),
    ("{home}/.aws", "hidden configuration folder"),
    ("{home}/keys", "contains your Databricks credential file"),
    ("{home}/afile", "This is not a folder."),
    ("{home}/missing-parent/child", "Its parent folder does not exist"),
    ("relative/path", "Enter an absolute path"),
    ("~nosuchuser-xyz/x", "is not a known user here"),
    pytest.param("/" + "a" * 4096, "at most 4,096 characters", id="too-long"),
    ("{home}/link", "app's state folder or inside it"),
])
def test_refused_default_code_folders(layout, value, reason):
    (layout.home / "keys").mkdir()
    (layout.home / "keys" / "db.txt").write_text("DBRICKS_TOKEN=x\n")
    (layout.home / "afile").write_text("x")
    (layout.home / "link").symlink_to(layout.state)
    value = value.format(home=layout.home, tmp=layout.tmp, state=layout.state, app=layout.app)
    with pytest.raises(ValueError, match=reason.replace("(", r"\(").replace(")", r"\)")):
        check(layout, value, env_file=str(layout.home / "keys" / "db.txt"))


def alias_of(monkeypatch, alias, folder):
    """Make alias another name for folder, as a bind mount or a case variant on a case-insensitive disk is."""
    real = tools_module._identity
    monkeypatch.setattr(tools_module, "_identity", lambda path: real(folder) if Path(path) == alias else real(path))


def test_a_hidden_home_folder_under_another_name_is_refused(layout, monkeypatch):
    alias = layout.tmp / "alias"
    (alias / ".ssh").mkdir(parents=True)
    (layout.home / ".ssh").mkdir()
    (alias / "code").mkdir()
    alias_of(monkeypatch, alias, layout.home.resolve())
    with pytest.raises(ValueError, match="hidden configuration folder"):
        check(layout, str(alias / ".ssh"))
    assert check(layout, str(alias / "code")) == ((alias / "code").resolve(), False)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to read-only folders")
def test_a_missing_folder_under_a_read_only_parent_is_refused(layout):
    parent = layout.home / "locked"
    parent.mkdir()
    parent.chmod(0o555)
    try:
        with pytest.raises(ValueError, match="parent folder is not writable"):
            check(layout, str(parent / "child"))
    finally:
        parent.chmod(0o755)


def test_accepted_default_code_folders(layout):
    (layout.home / "code").mkdir()
    assert check(layout, str(layout.home / "code")) == ((layout.home / "code").resolve(), False)
    assert check(layout, "~/new") == ((layout.home / "new").resolve(), True)
    assert user_paths._within is workspace_folders._within


# 17, 18 Settings and .env


def test_an_env_folder_appears_in_settings_and_a_settings_value_wins(layout, monkeypatch):
    (layout.home / "code").mkdir()
    (layout.home / "other").mkdir()
    monkeypatch.setenv("LOCAL_AGENT_CODING_DIR", str(layout.home / "code"))
    settings = settings_for(layout)
    assert settings.values["coding_dir"] == str(layout.home / "code")
    status = settings.public()["coding_dir_status"]
    assert status == {"path": str((layout.home / "code").resolve()), "state": "ready", "source": "env",
                      "message": f"Ready: every conversation can use {(layout.home / 'code').resolve()}."}
    public = settings.update({"coding_dir": "~/other"})
    assert public["coding_dir"] == str((layout.home / "other").resolve())
    assert public["coding_dir_status"]["source"] == "settings"
    assert Settings(layout.state).values["coding_dir"] == str((layout.home / "other").resolve())


def test_an_invalid_env_folder_never_blocks_other_saves(layout, monkeypatch):
    monkeypatch.setenv("LOCAL_AGENT_CODING_DIR", str(layout.state / "x"))
    settings = settings_for(layout)
    public = settings.update({"user_instructions": "Be brief."})
    assert public["coding_dir_status"]["state"] == "invalid"
    assert public["coding_dir_status"]["path"] == str(layout.state / "x")
    assert public["coding_dir_status"]["message"].startswith("Not used: This is the app's state folder")
    with pytest.raises(ValueError, match="^Default code folder: This is a system folder"):
        settings.update({"coding_dir": "/usr/share/code"})
    assert settings.update({"coding_dir": ""})["coding_dir_status"]["state"] == "off"


def put_settings(client, headers, **values):
    current = client.get("/api/settings", headers=headers).json()
    body = {key: current[key] for key in ("workspace", "model", "env_file")}
    return client.put("/api/settings", json={**body, **values}, headers=headers)


@pytest.mark.parametrize("key, name, label", [("coding_dir", "LOCAL_AGENT_CODING_DIR", "Default code folder"),
                                              ("python_venv", "LOCAL_AGENT_PYTHON_VENV", "Python environment")])
def test_a_path_through_a_symbolic_link_loop_is_reported_as_not_used(layout, monkeypatch, key, name, label):
    loop = layout.home / "loop"
    loop.symlink_to(loop)
    monkeypatch.setenv(name, str(loop / "x"))
    settings = settings_for(layout)
    with TestClient(create_app(settings)) as client:
        boot = client.get("/api/bootstrap")
        assert boot.status_code == 200
        status = boot.json()["settings"][f"{key}_status"]
        assert status["state"] == "invalid"
        assert status["message"] == "Not used: This path runs through a symbolic link loop."
        assert client.app.state.manager.standing_directories() == []
        assert client.app.state.manager.python_venv() is None
        headers = {"X-Local-Token": boot.json()["token"]}
        assert put_settings(client, headers, max_agent_steps=10).status_code == 200
        (layout.home / "self").symlink_to("self")
        refused = put_settings(client, headers, **{key: str(layout.home / "self")})
        assert refused.status_code == 400
        assert refused.json()["detail"] == f"{label}: This path runs through a symbolic link loop."
    assert check(layout, str(layout.home / "code"), env_file=str(loop / "db.txt"))[0] == layout.home / "code"


def test_the_settings_route_saves_both_paths(layout):
    coding, venv = layout.home / "code", make_venv(layout.tmp / "venvs" / "v")
    coding.mkdir()
    settings = settings_for(layout)
    with TestClient(create_app(settings)) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        saved = put_settings(client, headers, coding_dir=" ~/code ", python_venv=str(venv))
        assert saved.status_code == 200
        public = saved.json()
        assert public["coding_dir"] == str(coding.resolve()) and public["coding_dir_status"]["state"] == "ready"
        assert public["python_venv"] == str(venv) and public["python_venv_status"]["state"] == "ready"
        stored = json.loads(settings.path.read_text())
        assert stored["coding_dir"] == str(coding.resolve()) and stored["python_venv"] == str(venv)
        refused = put_settings(client, headers, coding_dir=str(layout.state / "x"))
        assert refused.status_code == 400 and refused.json()["detail"].startswith("Default code folder: ")
        assert put_settings(client, headers, coding_dir=5).status_code == 422
        assert put_settings(client, headers, python_venv=["/srv/v"]).status_code == 422
    assert json.loads(settings.path.read_text())["coding_dir"] == str(coding.resolve())


def test_saving_the_checked_form_of_an_env_value_keeps_following_env(layout, monkeypatch):
    (layout.home / "code").mkdir()
    monkeypatch.setenv("LOCAL_AGENT_CODING_DIR", "~/code")
    settings = settings_for(layout)
    settings.update({"coding_dir": str((layout.home / "code").resolve())})
    assert "coding_dir" not in json.loads(settings.path.read_text())
    assert settings.public()["coding_dir_status"]["source"] == "env"


def test_the_coding_dir_check_uses_the_env_file_saved_in_the_same_request(layout):
    keys = layout.home / "keys"
    keys.mkdir()
    (keys / "db.txt").write_text("DBRICKS_TOKEN=x\n")
    settings = settings_for(layout)
    with pytest.raises(ValueError, match="contains your Databricks credential file"):
        settings.update({"env_file": str(keys / "db.txt"), "coding_dir": str(keys)})
    settings.update({"env_file": str(keys / "db.txt")})
    assert settings.update({"env_file": str(layout.tmp / "db.txt"), "coding_dir": str(keys)})["coding_dir"] == str(keys)


@pytest.mark.parametrize("key, name", [("coding_dir", "LOCAL_AGENT_CODING_DIR"),
                                       ("python_venv", "LOCAL_AGENT_PYTHON_VENV")])
def test_values_from_env_keep_following_env_until_changed_in_settings(layout, monkeypatch, key, name):
    folders = {}
    for label in "abcd":
        folder = layout.home / label
        folders[label] = str(make_venv(folder) if key == "python_venv" else folder.mkdir() or folder)
    monkeypatch.setenv(name, folders["a"])
    settings = settings_for(layout)
    settings.update({"user_instructions": "x"})
    assert key not in json.loads(settings.path.read_text())
    monkeypatch.setenv(name, folders["b"])
    settings = settings_for(layout)
    assert settings.values[key] == folders["b"]
    assert settings.public()[f"{key}_status"]["source"] == "env"
    settings.update({key: folders["c"]})
    monkeypatch.setenv(name, folders["d"])
    assert Settings(layout.state).values[key] == folders["c"]
    monkeypatch.setenv(name, folders["b"])
    Settings(layout.state).update({key: ""})
    reloaded = Settings(layout.state)
    assert reloaded.values[key] == "" and reloaded.public()[f"{key}_status"]["state"] == "off"


async def test_tilde_values_from_env_become_absolute_paths(layout, monkeypatch, runtime):
    make_venv(layout.home / "venvs" / "v")
    monkeypatch.setenv("LOCAL_AGENT_CODING_DIR", "~/code")
    monkeypatch.setenv("LOCAL_AGENT_PYTHON_VENV", "~/venvs/v")
    manager, session, tools = runtime()
    coding, venv = str((layout.home / "code").resolve()), str(layout.home / "venvs" / "v")
    public = manager.settings.public()
    assert public["coding_dir_status"]["path"] == coding and public["coding_dir_status"]["state"] == "missing"
    assert public["python_venv_status"]["path"] == venv
    assert manager.standing_directories() == [coding]
    assert manager.jobs.command_environment()["VIRTUAL_ENV"] == venv
    seen = []
    prepare = agents_module.prepare_command

    async def recording(argv, **options):
        result = await prepare(argv, **options)
        seen.append((options["forbidden_roots"], result[0]))
        return result
    monkeypatch.setattr(agents_module, "prepare_command", recording)
    manager.rules.add("project", session["workspace"], {"behavior": "allow", "tool": "run_command", "prefix": ["pytest"],
                                                                      "flags": ["-q"]})
    session["permission_mode"] = "manual"
    assert (await run_command(manager, session, tools, "pytest -q")).strip() == "venv-pytest"
    roots, argv = seen[-1]
    assert Path(coding) in roots and argv[0] == os.path.join(venv, "bin", "pytest")
    (layout.home / "code").mkdir()
    status = manager.settings.public()["coding_dir_status"]
    assert status["state"] == "ready" and status["path"] == coding
    assert manager.standing_directories() == [coding]


# 20-23, 26 The standing folder grant


async def test_accept_edits_writes_into_a_missing_default_folder_without_an_access_card(layout, runtime):
    coding = layout.home / "code"
    manager, session, tools = runtime(coding_dir=str(coding))
    # No card may appear: a folder-access card would wait for an answer.
    output = await asyncio.wait_for(manager.execute_tool(
        session, tools, "write_file", {"path": str(coding / "hello" / "hello.py"), "content": "print(1)\n"}, "w"), 5)
    assert (coding / "hello" / "hello.py").read_text() == "print(1)\n", output
    assert not tool_events(session, "access_directory")
    changes = manager.changes.list(session["id"], manager.review_tools(session))
    assert "hello.py" in json.dumps(changes)
    assert manager.settings.public()["coding_dir_status"]["state"] == "ready"


async def test_manual_mode_still_asks_before_writing_there(layout, runtime):
    coding = layout.home / "code"
    coding.mkdir()
    manager, session, tools = runtime(coding_dir=str(coding))
    session["permission_mode"] = "manual"
    task = asyncio.create_task(manager.execute_tool(session, tools, "write_file",
                                                    {"path": str(coding / "a.py"), "content": "x\n"}, "w"))
    key = await pending_card(manager)
    card = next(event for event in session["events"] if event["id"] == key[1])
    assert card["name"] == "write_file" and not tool_events(session, "access_directory")
    manager.decide(*key, True)
    await task
    assert (coding / "a.py").read_text() == "x\n"


async def test_relative_paths_still_mean_the_project(layout, runtime):
    coding = layout.home / "code"
    coding.mkdir()
    manager, session, tools = runtime(coding_dir=str(coding))
    await manager.execute_tool(session, tools, "write_file", {"path": "notes.md", "content": "n\n"}, "w")
    assert (layout.project / "notes.md").exists() and not (coding / "notes.md").exists()


async def test_subagents_get_the_standing_folder(layout, runtime):
    coding = layout.home / "code"
    coding.mkdir()
    manager, session, _ = runtime(coding_dir=str(coding))
    child = manager.store.create(manager.settings.values)
    child.update(is_subagent=True, parent_session_id=session["id"], allowed_directories=[])
    assert coding.resolve() in manager.session_tools(child).allowed_directories


async def test_change_review_keeps_the_standing_folder_next_to_earlier_folders(layout, runtime, monkeypatch):
    coding, earlier = layout.home / "code", layout.home / "earlier"
    coding.mkdir()
    earlier.mkdir()
    manager, session, _ = runtime(coding_dir=str(coding))
    monkeypatch.setattr(manager.changes, "outside_folders", lambda session_id, tools: [str(earlier)])
    allowed = manager.review_tools(session).allowed_directories
    assert coding.resolve() in allowed and earlier.resolve() in allowed


def system_text(manager, session, tools):
    return manager.context_inputs(session, tools, "manual")[0]["content"]


async def test_the_system_prompt_names_the_default_folder(layout, runtime):
    coding = layout.home / "code"
    manager, session, tools = runtime(coding_dir=str(coding))
    text = system_text(manager, session, tools)
    assert f"Default folder for new code: {coding.resolve()}." in text and "standalone" in text
    assert "It does not exist yet" in text
    coding.mkdir()
    assert "It does not exist yet" not in system_text(manager, session, tools)
    for value in ("", str(layout.state / "x"), str(layout.project)):
        manager.settings.values["coding_dir"] = value
        assert "Default folder for new code" not in system_text(manager, session, tools)
    session["workspace"] = str(layout.project)
    manager.settings.values["coding_dir"] = str(layout.home / "work")
    (layout.home / "work" / "inner").mkdir(parents=True)
    session["workspace"] = str(layout.home / "work" / "inner")
    assert "Default folder for new code" in system_text(manager, session, manager.session_tools(session))


# 24 Files routes


def test_files_routes_reach_the_default_folder(layout):
    coding = layout.home / "code"
    coding.mkdir()
    (coding / "a.txt").write_text("hello\n")
    (layout.state / "x").mkdir()
    settings = settings_for(layout, coding_dir=str(coding))
    with TestClient(create_app(settings)) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        listing = client.get("/api/files", params={"path": str(coding)}, headers=headers)
        assert listing.status_code == 200 and "a.txt" in listing.text
        assert client.get("/api/file", params={"path": str(coding / "a.txt")}, headers=headers).json()["content"] == "hello\n"
        settings.values["coding_dir"] = str(layout.state / "x")
        assert client.get("/api/files", params={"path": str(layout.state / "x")}, headers=headers).status_code == 400
    with pytest.raises(ValueError, match=EXCLUDED_ROOT):
        WorkspaceTools(str(layout.project), "", [layout.state / "x"], excluded_roots=(layout.state,)).resolve(
            str(layout.state / "x"))


# 25 Read-only Git never runs a git from the default folder


async def test_read_only_git_refuses_a_git_inside_the_default_folder(layout, runtime, monkeypatch):
    coding = layout.home / "code"
    marker = layout.tmp / "marker"
    (coding / "bin").mkdir(parents=True)
    (coding / "bin" / "git").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (coding / "bin" / "git").chmod(0o755)
    monkeypatch.setenv("PATH", str(coding / "bin") + os.pathsep + os.environ.get("PATH", ""))
    manager, session, _ = runtime(coding_dir=str(coding))
    event = await manager.event(session, "tool", name="run_command", input={"command": "git status"}, state="running",
                                output="")
    with pytest.raises(ValueError, match="inside the project or a folder granted"):
        await manager.command_tool(session, event, {"command": "git status"}, argv=["git", "status"], read_only=True)
    assert not marker.exists()


# 27, 28 venv_environment and server_bins


def test_venv_environment_layers_the_environment_without_changing_its_input():
    env = {"PATH": os.pathsep.join(["/old/venv/bin", "/usr/bin", "", "/app/bin", "/v/bin", "/bin"]),
           "VIRTUAL_ENV": "/old/venv", "PYTHONHOME": "/py", "HOME": "/h"}
    original = dict(env)
    result = user_paths.venv_environment(env, "/v", drop=["/app/bin"])
    assert env == original
    assert result["PATH"] == os.pathsep.join(["/v/bin", "/usr/bin", "", "/bin"])
    assert result["VIRTUAL_ENV"] == "/v" and "PYTHONHOME" not in result and result["HOME"] == "/h"
    assert user_paths.venv_environment({}, "/v")["PATH"] == "/v/bin" + os.pathsep + os.defpath
    assert user_paths.venv_environment({"PATH": ""}, "/v")["PATH"] == "/v/bin"
    assert user_paths.venv_environment({"PATH": "/v/bin"}, "/v")["PATH"] == "/v/bin"


def test_server_bins(monkeypatch):
    monkeypatch.setattr(sys, "prefix", "/srv/app/.venv")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    monkeypatch.setattr(sys, "executable", "/srv/app/.venv/bin/python3")
    assert user_paths.server_bins() == ["/srv/app/.venv/bin"]
    monkeypatch.setattr(sys, "executable", "/srv/other/python3")
    assert user_paths.server_bins() == ["/srv/other", "/srv/app/.venv/bin"]
    monkeypatch.setattr(sys, "base_prefix", "/srv/app/.venv")
    assert user_paths.server_bins() == []


async def test_the_app_servers_own_programs_are_hidden_from_commands_with_a_venv(layout, runtime, monkeypatch):
    app_bin = layout.tmp / "appbin"
    app_bin.mkdir()
    (app_bin / "app-only-tool").write_text("#!/bin/sh\necho app\n")
    (app_bin / "app-only-tool").chmod(0o755)
    monkeypatch.setenv("PATH", str(app_bin) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(user_paths, "server_bins", lambda: [str(app_bin)])
    venv = make_venv(layout.tmp / "venvs" / "v")
    manager, session, tools = runtime(python_venv=str(venv), permission_mode="bypassPermissions")
    session["permission_mode"] = "bypassPermissions"
    command = "command -v app-only-tool || echo none"
    assert (await run_command(manager, session, tools, command)).strip() == "none"
    manager.settings.values["python_venv"] = ""
    assert (await run_command(manager, session, tools, command, "c2")).strip() == str(app_bin / "app-only-tool")


# 29 check_python_venv


def check_venv(layout, value):
    return user_paths.check_python_venv(value, state_dir=layout.state, app_root=layout.app)


def test_a_virtual_environment_is_accepted_with_its_version(layout):
    venv = make_venv(layout.tmp / "venvs" / "v")
    assert check_venv(layout, str(venv)) == {"path": str(venv), "python": str(venv / "bin" / "python"),
                                             "version": "3.12.4"}
    (venv / "pyvenv.cfg").write_text("version = 3.11.9\nversion_info = 3.12.4.final.0\n")
    assert check_venv(layout, str(venv))["version"] == "3.11.9"
    link = layout.tmp / "link"
    link.symlink_to(venv)
    assert check_venv(layout, str(link))["path"] == str(link)  # Not resolved: the name is how it is found.
    conda = make_venv(layout.tmp / "conda")
    (conda / "pyvenv.cfg").unlink()
    (conda / "conda-meta").mkdir()
    assert check_venv(layout, str(conda))["version"] is None


@pytest.mark.parametrize("case, reason", [
    ("no-python", "No bin/python here"),
    ("not-executable", "No bin/python here"),
    ("missing", "This folder does not exist."),
    ("root", "filesystem root is not a virtual environment"),
    ("state", "app's state folder or inside it"),
    ("app", "app's own Python environment"),
    ("server", "Python environment the app server runs in"),
    ("no-cfg", "No pyvenv.cfg"),
    ("relative", "Enter an absolute path"),
])
def test_refused_virtual_environments(layout, monkeypatch, case, reason):
    venv = make_venv(layout.tmp / "venvs" / "v")
    value = str(venv)
    if case == "no-python":
        (venv / "bin" / "python").unlink()
    elif case == "not-executable":
        (venv / "bin" / "python").chmod(0o644)
    elif case == "missing":
        value = str(layout.tmp / "nowhere")
    elif case == "root":
        value = "/"
    elif case == "state":
        value = str(make_venv(layout.state / "v"))
    elif case == "app":
        value = str(make_venv(layout.app / ".venv"))
    elif case == "server":
        monkeypatch.setattr(sys, "prefix", str(venv))
        monkeypatch.setattr(sys, "base_prefix", "/usr")
    elif case == "no-cfg":
        (venv / "pyvenv.cfg").unlink()
    elif case == "relative":
        value = "venvs/v"
    with pytest.raises(ValueError, match=reason):
        check_venv(layout, value)


def test_a_venv_path_with_the_path_separator_is_refused(layout):
    # PATH would split it into other entries, and commands would not find its bin/python.
    venv = make_venv(layout.tmp / f"proj{os.pathsep}v2" / ".venv")
    with pytest.raises(ValueError, match=f"contains '{os.pathsep}', which cannot appear in a PATH entry"):
        check_venv(layout, str(venv))


async def test_a_venv_path_with_the_path_separator_is_invalid_and_not_used(layout, runtime):
    venv = make_venv(layout.tmp / f"proj{os.pathsep}v2" / ".venv")
    manager, _, _ = runtime(python_venv=str(venv))
    status = manager.settings.public()["python_venv_status"]
    assert status["state"] == "invalid" and "cannot appear in a PATH entry" in status["message"]
    assert manager.python_venv() is None
    environment = manager.jobs.command_environment()
    assert environment.get("VIRTUAL_ENV") != str(venv) and str(venv) not in environment.get("PATH", "")


# 30-33, 36, 37 Every app-run command uses the venv


def test_terminal_jobs_run_with_the_venv(layout):
    venv = make_venv(layout.tmp / "venvs" / "v")

    def output(settings):
        with TestClient(create_app(settings)) as client:
            headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
            job = client.post("/api/jobs", json={"command": "python"}, headers=headers).json()
            for _ in range(200):
                job = client.get(f"/api/jobs/{job['id']}", headers=headers).json()
                if job["state"] != "running":
                    return job["output"]
                time.sleep(0.02)
        raise AssertionError("The job did not finish")
    assert f"venv-python {venv}" in output(settings_for(layout, python_venv=str(venv)))
    assert "venv-python" not in output(settings_for(layout, python_venv=""))


async def test_agent_shell_rule_argv_and_verify_use_the_venv(layout, runtime, monkeypatch):
    venv = make_venv(layout.tmp / "venvs" / "v")
    manager, session, tools = runtime(python_venv=str(venv))
    session["permission_mode"] = "bypassPermissions"
    assert (await run_command(manager, session, tools, "python")).strip() == f"venv-python {venv}"
    session["permission_mode"] = "manual"
    manager.rules.add("project", session["workspace"], {"behavior": "allow", "tool": "run_command", "prefix": ["pytest"],
                                                                      "flags": ["-q"]})
    assert (await run_command(manager, session, tools, "pytest -q", "c2")).strip() == "venv-pytest"
    event = tool_events(session, "run_command")[-1]
    assert event["without_shell"] is True and event["allowed_by_rule"] is True
    # Verify runs the project's command as run_command.
    manager.projects.update(str(layout.project.resolve()), {"verify_command": "python"})
    session["permission_mode"] = "bypassPermissions"
    frames = iter([{"tool_calls": [{"index": 0, "id": "w", "function": {
        "name": "write_file", "arguments": json.dumps({"path": "a.txt", "content": "a\n"})}}]}, {"content": "Done."}])
    client = httpx.AsyncClient

    async def handle(request):
        delta = next(frames, {"content": "Done."})
        finish = "tool_calls" if "tool_calls" in delta else "stop"
        return httpx.Response(200, text="data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]})
                              + "\n\ndata: [DONE]\n\n")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handle)))
    await manager.run(session, "Write a")
    verify = [event for event in session["events"] if event.get("verify")]
    assert verify and f"venv-python {venv}" in verify[-1]["output"]


@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
async def test_read_only_git_never_sees_the_venv(layout, runtime, monkeypatch):
    venv = make_venv(layout.tmp / "venvs" / "v")
    marker = layout.tmp / "marker"
    (venv / "bin" / "git").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (venv / "bin" / "git").chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=layout.project, check=True, capture_output=True)
    manager, session, _ = runtime(python_venv=str(venv))
    seen = []
    prepare = agents_module.prepare_command

    async def recording(argv, **options):
        result = await prepare(argv, **options)
        seen.append(result[0])
        return result
    monkeypatch.setattr(agents_module, "prepare_command", recording)
    event = await manager.event(session, "tool", name="run_command", input={"command": "git status"}, state="running",
                                output="")
    await manager.command_tool(session, event, {"command": "git status"}, argv=["git", "status"], read_only=True)
    assert not marker.exists() and seen[-1][0] != str(venv / "bin" / "git")


async def test_hooks_get_the_venv_but_not_named_variables(layout, runtime, monkeypatch):
    venv = make_venv(layout.tmp / "venvs" / "v")
    monkeypatch.setenv("MY_NAMED", "named-value")
    manager, session, tools = runtime(python_venv=str(venv), command_env=["MY_NAMED"])
    session["permission_mode"] = "bypassPermissions"
    manager.extensions.update_config({"hooks": [{"id": "check", "enabled": True, "event": "before_tool",
                                                 "command": "python; echo NAMED=$MY_NAMED"}]})
    await manager.execute_tool(session, tools, "write_file", {"path": "a.txt", "content": "a\n"}, "w")
    hook = tool_events(session, "hook_before_tool")[-1]
    assert f"venv-python {venv}" in hook["output"]
    assert "NAMED=\n" in hook["output"] + "\n" and "named-value" not in hook["output"]
    # Commands still get the named variable.
    assert "named-value" in await run_command(manager, session, tools, "echo $MY_NAMED")


async def test_pythonhome_is_dropped_only_with_a_venv(layout, runtime, monkeypatch):
    venv = make_venv(layout.tmp / "venvs" / "v")
    monkeypatch.setenv("PYTHONHOME", "/srv/py")
    manager, _, _ = runtime(python_venv=str(venv), command_env=["PYTHONHOME"])
    assert "PYTHONHOME" not in manager.jobs.command_environment()
    manager.settings.values["python_venv"] = ""
    assert manager.jobs.command_environment()["PYTHONHOME"] == "/srv/py"


# 34 Agent edits inside the venv always ask


async def test_edits_inside_the_venv_are_protected(layout, runtime):
    venv = make_venv(layout.project / "venv")
    manager, session, tools = runtime(python_venv=str(venv))
    move = ("move_path", {"path": "venv/bin/pytest", "destination": "venv/bin/make"})
    edit = ("edit_file", {"path": "venv/bin/python", "old_text": "echo", "new_text": "echo x"})
    for name, arguments in (move, edit):
        verdict = manager.permission(session, tools, name, arguments)
        assert verdict.decision == "ask" and "Python environment commands run with" in verdict.protected
    move_in = ("move_path", {"path": "tool.sh", "destination": "venv/bin/make"})
    assert manager.permission(session, tools, *move_in).decision == "ask"
    session["permission_mode"] = "dontAsk"
    assert manager.permission(session, tools, *move).decision == "deny"
    session["permission_mode"] = "bypassPermissions"
    assert manager.permission(session, tools, *move).decision == "allow"
    session["permission_mode"] = "acceptEdits"
    assert manager.permission(session, tools, "write_file", {"path": "src/a.py", "content": "x"}).decision == "allow"
    task = asyncio.create_task(manager.execute_tool(session, tools, *move, "m"))
    key = await pending_card(manager)
    card = next(event for event in session["events"] if event["id"] == key[1])
    assert card["protected"].startswith("the Python environment") and card["rememberable"] == []
    manager.decide(*key, False)
    await task
    assert (venv / "bin" / "pytest").exists() and not (venv / "bin" / "make").exists()


# 35a A venv without pip (uv venv, --without-pip): a pip on PATH belongs to another Python


@pytest.mark.parametrize("site", ["lib/python3.12/site-packages", "lib/pypy3.10/site-packages",
                                  "lib64/python3.12/site-packages", "site-packages"])
async def test_the_system_prompt_names_a_venv_without_pip(layout, runtime, site):
    venv = make_venv(layout.tmp / "venvs" / "v")
    (venv / site).mkdir(parents=True)  # uv venv and --without-pip create site-packages without pip.
    manager, session, tools = runtime(python_venv=str(venv))
    text = system_text(manager, session, tools)
    assert f"commands run with the virtual environment {venv} active" in text
    assert "Install packages with `python -m pip`" not in text and "python, pip and installed tools" not in text
    assert "This environment has no pip" in text and "uv pip install" in text
    assert "Files inside it can be changed only with the user's approval." in text
    status = manager.settings.public()["python_venv_status"]
    assert status["pip"] is False and "No pip is installed in it." in status["message"]
    # Checked again on every use: once pip is installed, the usual line comes back.
    package = venv / site / "pip"
    package.mkdir()
    (package / "__init__.py").write_text("")
    text = system_text(manager, session, tools)
    assert "python, pip and installed tools resolve there" in text and "Install packages with `python -m pip`" in text
    assert "has no pip" not in text
    assert manager.settings.public()["python_venv_status"]["pip"] is True


async def test_a_venv_layout_without_site_packages_keeps_the_usual_pip_line(layout, runtime):
    venv = make_venv(layout.tmp / "venvs" / "v")  # No site-packages folder the app recognises.
    manager, session, tools = runtime(python_venv=str(venv))
    text = system_text(manager, session, tools)
    assert "Install packages with `python -m pip`" in text and "has no pip" not in text
    status = manager.settings.public()["python_venv_status"]
    assert status["pip"] is None and "No pip" not in status["message"]


# 35 An invalid venv is not used


async def test_an_invalid_venv_is_reported_and_not_used(layout, runtime, monkeypatch):
    venv = make_venv(layout.tmp / "venvs" / "v")
    manager, session, tools = runtime(python_venv=str(venv))
    session["permission_mode"] = "bypassPermissions"
    text = system_text(manager, session, tools)
    assert f"commands run with the virtual environment {venv} active" in text and "(Python 3.12.4)" in text
    assert f"venv-python {venv}" in await run_command(manager, session, tools, "python", "c0")
    (venv / "bin" / "python").unlink()
    assert "venv-python" not in await run_command(manager, session, tools, "python || true")
    assert manager.python_venv() is None
    assert manager.jobs.command_environment().get("VIRTUAL_ENV") != str(venv)
    assert manager.settings.public()["python_venv_status"]["state"] == "invalid"
    assert "is not usable (No bin/python here" in system_text(manager, session, tools)
    session["tool_profile"] = "read_only"
    assert "\nPython:" not in system_text(manager, session, tools)
    session["tool_profile"] = "inherit"
    manager.settings.values["python_venv"] = ""
    monkeypatch.setattr(user_paths, "server_bins", lambda: ["/srv/app/.venv/bin"])
    assert "app server's own environment" in system_text(manager, session, tools)
    monkeypatch.setattr(user_paths, "server_bins", lambda: [])
    assert "\nPython:" not in system_text(manager, session, tools)
