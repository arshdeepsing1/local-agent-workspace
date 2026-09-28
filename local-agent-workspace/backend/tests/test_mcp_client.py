import asyncio
import json
import os
import signal
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_agent.extensions import ExtensionManager
from local_agent.mcp_client import bounded_result, model_tool_name


@pytest.fixture
def extension_manager(tmp_path):
    settings = SimpleNamespace(state_dir=tmp_path, redact=lambda text: text)
    return ExtensionManager(settings)


@pytest.fixture
def local_server(tmp_path):
    server = tmp_path / "mcp_server.py"
    server.write_text('''import asyncio, json, os, sys
from pathlib import Path
from mcp.server import MCPServer
Path(sys.argv[1]).write_text(str(os.getpid()))
server = MCPServer("Local test", log_level="ERROR")
@server.tool()
def echo(text: str) -> str:
    return text + " env=" + str(os.environ.get("DBRICKS_TOKEN"))
@server.tool()
def fail() -> str:
    raise ValueError("Intentional test failure")
@server.tool()
async def slow() -> str:
    Path(sys.argv[2]).write_text("started")
    await asyncio.sleep(30)
    return "finished"
server.run()
''')
    return {"id": "local", "name": "Local test", "transport": "stdio", "command": sys.executable,
            "args": [str(server), str(tmp_path / "server.pid"), str(tmp_path / "call-started")], "enabled": True}


def process_exists(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def test_actual_stdio_server_discovery_and_invocation(extension_manager, local_server, tmp_path, monkeypatch):
    monkeypatch.setenv("DBRICKS_TOKEN", "must-not-inherit")
    extension_manager.update_config({"servers": [local_server]})
    async with extension_manager.turn() as connection:
        definitions = await connection.discover()
        echo = next(item for item in definitions if item["function"]["name"] == "mcp__local__echo")
        assert echo["function"]["parameters"]["properties"]["text"]["type"] == "string"
        result = await connection.call("mcp__local__echo", {"text": "hello"})
        assert result == {"output": "hello env=None", "is_error": False, "truncated": False}
        with pytest.raises(ValueError, match="not available"):
            await connection.call("mcp__other__echo", {})
        pid = int((tmp_path / "server.pid").read_text())
        assert process_exists(pid)
    assert not process_exists(pid)


async def test_actual_stdio_cancellation_closes_sdk_connection(extension_manager, local_server, tmp_path):
    extension_manager.update_config({"servers": [local_server]})

    async def turn():
        async with extension_manager.turn() as connection:
            await connection.discover()
            await connection.call("mcp__local__slow", {})

    task = asyncio.create_task(turn())
    async with asyncio.timeout(5):
        while not (tmp_path / "call-started").exists():
            await asyncio.sleep(.01)
    pid = int((tmp_path / "server.pid").read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert not process_exists(pid)


async def test_tool_timeout_preserves_cleanup_and_reports_uncertain_side_effects(extension_manager, local_server, tmp_path, monkeypatch):
    extension_manager.update_config({"servers": [local_server]})
    async with extension_manager.turn() as connection:
        await connection.discover()
        monkeypatch.setattr("local_agent.mcp_client.CALL_TIMEOUT", .05)
        with pytest.raises(ValueError, match="side effects may have completed"):
            await connection.call("mcp__local__slow", {})
    assert not process_exists(int((tmp_path / "server.pid").read_text()))


async def test_caller_error_is_not_hidden_by_sdk_task_group(extension_manager, local_server, tmp_path):
    extension_manager.update_config({"servers": [local_server]})
    with pytest.raises(ValueError, match="original error"):
        async with extension_manager.turn() as connection:
            await connection.discover()
            raise ValueError("original error")
    assert not process_exists(int((tmp_path / "server.pid").read_text()))


async def test_disabled_servers_are_never_started(extension_manager, local_server, tmp_path):
    local_server["enabled"] = False
    extension_manager.update_config({"servers": [local_server]})
    assert await extension_manager.discover() == []
    assert not (tmp_path / "server.pid").exists()


async def test_connection_failure_is_actionable(extension_manager, local_server):
    local_server["command"] = "/does/not/exist"
    extension_manager.update_config({"servers": [local_server]})
    with pytest.raises(ValueError, match="MCP server local"):
        await extension_manager.discover()


async def test_initialize_timeout_reaps_unresponsive_server(extension_manager, local_server, tmp_path, monkeypatch):
    server = tmp_path / "unresponsive.py"
    server.write_text("import os,time,sys; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)")
    local_server["args"] = [str(server), str(tmp_path / "server.pid")]
    extension_manager.update_config({"servers": [local_server]})
    monkeypatch.setattr("local_agent.mcp_client.CONNECT_TIMEOUT", .1)
    with pytest.raises(ValueError, match="could not connect"):
        await extension_manager.discover()
    assert not process_exists(int((tmp_path / "server.pid").read_text()))


async def test_tool_result_error_is_retained(extension_manager, local_server):
    extension_manager.update_config({"servers": [local_server]})
    async with extension_manager.turn() as connection:
        await connection.discover()
        result = await connection.call("mcp__local__fail", {})
        assert result["is_error"] is True


async def test_invalid_tool_arguments_are_rejected_locally(extension_manager, local_server):
    extension_manager.update_config({"servers": [local_server]})
    async with extension_manager.turn() as connection:
        await connection.discover()
        with pytest.raises(ValueError, match="required constraint"):
            await connection.call("mcp__local__echo", {})
        assert (await connection.call("mcp__local__echo", {"text": "valid"}))["is_error"] is False


async def test_diagnostics_check_every_server_and_redact_failures(extension_manager, local_server, tmp_path):
    extension_manager.settings.redact = lambda text: text.replace("secret-token", "[REDACTED]")
    bad = {**local_server, "id": "broken", "command": "/missing/secret-token"}
    disabled = {**bad, "id": "disabled", "enabled": False}
    last = {**bad, "id": "last"}
    extension_manager.update_config({"servers": [bad, local_server, disabled, last]})
    report = await extension_manager.test()
    assert [item["status"] for item in report["servers"]] == ["failed", "connected", "disabled", "failed"]
    assert report["servers"][1]["tools"] == report["tools"]
    assert "mcp__local__echo" in report["tools"]
    assert report["servers"][2]["tools"] == [] and "error" not in report["servers"][2]
    assert "secret-token" not in json.dumps(report)
    assert "[REDACTED]" in json.dumps(report)
    assert not process_exists(int((tmp_path / "server.pid").read_text()))


async def test_failed_or_closed_discovery_never_reuses_partial_tools(extension_manager, local_server, tmp_path):
    bad = {**local_server, "id": "broken", "command": "/does/not/exist"}
    extension_manager.update_config({"servers": [local_server, bad]})
    async with extension_manager.turn() as connection:
        with pytest.raises(ValueError, match="MCP server broken"):
            await connection.discover()
        assert connection.registry == {} and connection.definitions == [] and connection.tool_names == set()
        with pytest.raises(ValueError, match="no longer available"):
            await connection.call("mcp__local__echo", {"text": "must not run"})
        with pytest.raises(ValueError, match="start a new turn"):
            await connection.discover()
    assert not process_exists(int((tmp_path / "server.pid").read_text()))
    assert connection.tool_names == set()
    with pytest.raises(ValueError, match="no longer available"):
        await connection.call("mcp__local__echo", {"text": "must not run"})


def test_model_names_are_bounded_and_unambiguous_after_sanitizing():
    assert model_tool_name("local", "echo") == "mcp__local__echo"
    names = [model_tool_name("a" * 20, value) for value in ["name.with.dots", "name_with_dots", "a" * 200, "🧪" * 200]]
    assert len(set(names)) == 4
    assert all(len(name) <= 64 for name in names)
    assert all(set(name) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-") for name in names)


def test_unicode_result_budget_includes_json_escaping():
    result = bounded_result("🌍" * 5000)
    assert result["truncated"] is True
    assert len(json.dumps(result, indent=2).encode()) <= 8000
    assert set(result["output"]) == {"🌍"}


async def test_schema_budget_failure_reaps_server(extension_manager, local_server, tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.mcp_client.MAX_DEFINITION_BYTES", 20)
    extension_manager.update_config({"servers": [local_server]})
    with pytest.raises(Exception, match="MCP|TaskGroup"):
        await extension_manager.discover()
    assert not process_exists(int((tmp_path / "server.pid").read_text()))


async def test_actual_streamable_http_server(extension_manager, tmp_path):
    server = tmp_path / "http_server.py"
    server.write_text('''import sys
from mcp.server import MCPServer
server = MCPServer("HTTP test", log_level="ERROR")
@server.tool()
def echo(text: str) -> str:
    return "http " + text
server.run(transport="streamable-http", port=int(sys.argv[1]))
''')
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = await asyncio.create_subprocess_exec(sys.executable, str(server), str(port),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        async with asyncio.timeout(5):
            while True:
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(.01)
        extension_manager.update_config({"servers": [{"id": "remote", "transport": "http", "enabled": True,
            "url": f"http://127.0.0.1:{port}/mcp"}]})
        async with extension_manager.turn() as connection:
            tools = await connection.discover()
            assert tools[0]["function"]["name"] == "mcp__remote__echo"
            result = await connection.call("mcp__remote__echo", {"text": "hello"})
            assert result["output"] == "http hello"
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
