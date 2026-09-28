import asyncio
import os
import signal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from local_agent.api import create_app
from local_agent.config import Settings
from local_agent.tools import WorkspaceTools


CREDENTIAL_KEYS = ("DBRICKS_URL", "DBRICKS_TOKEN", "DATABRICKS_HOST", "DATABRICKS_TOKEN")


@pytest.fixture
def app_root(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    monkeypatch.setattr("local_agent.config.APP_ROOT", root)
    for key in CREDENTIAL_KEYS:
        monkeypatch.delenv(key, raising=False)
    return root


@pytest.mark.parametrize("filename, alias", [
    (".env", ".ENV"),
    ("env_vars.txt", "ENV_VARS.TXT"),
    (".env.production", ".ENV.PRODUCTION"),
    (".local/state.json", ".LOCAL/state.json"),
    (".git/config", ".GIT/config"),
])
def test_excluded_names_cannot_be_read_with_different_casing(tmp_path, filename, alias):
    target = tmp_path / filename
    target.parent.mkdir(exist_ok=True)
    target.write_text("fake private contents")
    if not (tmp_path / alias).exists():
        pytest.skip("The filesystem is case-sensitive")
    tools = WorkspaceTools(str(tmp_path), unrestricted=True)
    with pytest.raises(ValueError, match="excluded"):
        tools.read_file(alias)
    with pytest.raises(ValueError, match="excluded"):
        tools.change("write_file", {"path": alias, "content": "replacement"}, apply=True)
    assert target.read_text() == "fake private contents"


@pytest.mark.parametrize("alias_kind", ["case", "hardlink"])
def test_configured_credential_identity_is_excluded_from_api(app_root, alias_kind):
    credential = app_root / "gateway.conf"
    credential.write_text("DBRICKS_URL=https://fake.example\nDBRICKS_TOKEN=fake-proof-only\n")
    alias = app_root / ("GATEWAY.CONF" if alias_kind == "case" else "copy.conf")
    if alias_kind == "hardlink":
        alias.hardlink_to(credential)
    elif not alias.exists():
        pytest.skip("The filesystem is case-sensitive")
    settings = Settings(app_root / "state")
    settings.values.update(workspace=str(app_root), env_file=str(credential))
    with TestClient(create_app(settings)) as client:
        headers = {"X-Local-Token": client.get("/api/bootstrap").json()["token"]}
        response = client.get("/api/file", params={"path": alias.name}, headers=headers)
        assert response.status_code == 400
        assert "excluded" in response.json()["detail"]
        assert "fake-proof-only" not in response.text


@pytest.mark.parametrize("name", [".env", ".env.production", "env_vars.txt"])
def test_excluded_symlink_name_is_checked_before_resolution(tmp_path, name):
    (tmp_path / "ordinary.txt").write_text("fake secret marker")
    (tmp_path / name).symlink_to(tmp_path / "ordinary.txt")
    tools = WorkspaceTools(str(tmp_path))
    with pytest.raises(ValueError, match="Credential files are excluded"):
        tools.read_file(name)
    assert name not in [entry["name"] for entry in tools.list_files()]
    assert tools.search_files("fake secret marker", glob=name)["matches"] == []


def test_lexical_and_resolved_directory_exclusions_remain_enforced(tmp_path):
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    (ordinary / "note.txt").write_text("example")
    (tmp_path / ".local").symlink_to(ordinary, target_is_directory=True)
    secret = tmp_path / ".env"
    secret.write_text("fake secret")
    (tmp_path / "readable.txt").symlink_to(secret)
    tools = WorkspaceTools(str(tmp_path), unrestricted=True)
    for path in (".local/note.txt", "readable.txt"):
        with pytest.raises(ValueError, match="excluded"):
            tools.read_file(path)
    (tmp_path / ".env.example").write_text("placeholder")
    assert tools.read_file(".env.example") == "placeholder"
    tools.change("write_file", {"path": "new/note.txt", "content": "new"}, apply=True)
    assert tools.read_file("new/note.txt") == "new"


async def test_cancel_kills_descendants_after_shell_has_exited(tmp_path, monkeypatch):
    spawned = []
    spawn = asyncio.create_subprocess_exec

    async def capture_process(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr("local_agent.tools.asyncio.create_subprocess_exec", capture_process)
    task = asyncio.create_task(WorkspaceTools(str(tmp_path)).run_command(
        "/bin/sleep 30 & echo $! > child.pid"))
    try:
        async with asyncio.timeout(3):
            while not spawned or spawned[0].returncode is None or not (tmp_path / "child.pid").exists():
                await asyncio.sleep(.01)
        child = int((tmp_path / "child.pid").read_text())
        os.kill(child, 0)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with asyncio.timeout(3):
            while True:
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(.01)
    finally:
        for process in spawned:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if spawned:
            await spawned[0].stdout.read()


def test_unreadable_credentials_allow_bootstrap_and_settings_recovery(app_root, monkeypatch):
    credential = app_root / "gateway.conf"
    credential.write_text("DBRICKS_URL=https://fake.example\nDBRICKS_TOKEN=fake-proof-only\n")
    settings = Settings(app_root / "state")
    settings.values.update(workspace=str(app_root), env_file=str(credential))
    read_text = Path.read_text

    def denied(self, *args, **kwargs):
        if self == credential:
            raise PermissionError(1, "Operation not permitted", str(self))
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    monkeypatch.setattr("local_agent.tools.sys.platform", "darwin")
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.get("/api/bootstrap")
        assert response.status_code == 200
        assert response.json()["settings"]["configured"] is False
        headers = {"X-Local-Token": response.json()["token"]}
        diagnostic = client.get("/api/connection", headers=headers)
        assert diagnostic.status_code == 400
        assert "macOS denied access" in diagnostic.json()["detail"]
        assert "fake-proof-only" not in diagnostic.text
        recovered = client.put("/api/settings", headers=headers, json={**settings.values, "env_file": ""})
        assert recovered.status_code == 200
        assert client.get("/api/bootstrap").status_code == 200


@pytest.mark.parametrize("file_keys", [("DBRICKS_URL", "DBRICKS_TOKEN"), ("DATABRICKS_HOST", "DATABRICKS_TOKEN")])
@pytest.mark.parametrize("override_keys", [("DBRICKS_URL", "DBRICKS_TOKEN"), ("DATABRICKS_HOST", "DATABRICKS_TOKEN")])
@pytest.mark.parametrize("sources", [("external", "environment"), ("external", "dotenv"), ("dotenv", "environment")])
def test_credential_aliases_respect_source_precedence(app_root, monkeypatch, file_keys, override_keys, sources):
    lower = f"{file_keys[0]}=https://lower.example\n{file_keys[1]}=fake-lower-token\n"
    higher = f"{override_keys[0]}=https://higher.example\n{override_keys[1]}=fake-higher-token\n"
    external = app_root / "gateway.conf"
    if sources[0] == "external":
        external.write_text(lower)
    else:
        (app_root / ".env").write_text(lower)
    if sources[1] == "environment":
        monkeypatch.setenv(override_keys[0], "https://higher.example")
        monkeypatch.setenv(override_keys[1], "fake-higher-token")
    else:
        (app_root / ".env").write_text(higher)
    settings = Settings(app_root / "state")
    settings.values["env_file"] = str(external) if external.exists() else ""
    assert settings.credentials() == ("https://higher.example", "fake-higher-token")
