import asyncio
import base64
import json
import os
import signal
import socket
import sys
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
    async with extension_manager.turn() as connection:
        assert await connection.discover() == []
        [entry] = connection.status
    assert entry["status"] == "failed" and "could not connect" in entry["error"] and "/does/not/exist" in entry["error"]


async def test_initialize_timeout_reaps_unresponsive_server(extension_manager, local_server, tmp_path, monkeypatch):
    server = tmp_path / "unresponsive.py"
    server.write_text("import os,time,sys; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)")
    local_server["args"] = [str(server), str(tmp_path / "server.pid")]
    extension_manager.update_config({"servers": [local_server]})
    monkeypatch.setattr("local_agent.mcp_client.CONNECT_TIMEOUT", .5)
    async with extension_manager.turn() as connection:
        assert await connection.discover() == []
        assert connection.status[0]["status"] == "failed"
        assert "did not answer within" in connection.status[0]["error"]
        # Closed at once, on this task, before the turn goes on.
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


async def test_a_broken_server_is_skipped_and_the_others_work(extension_manager, local_server, tmp_path):
    bad = {**local_server, "id": "broken", "name": "Broken", "command": "/does/not/exist"}
    extension_manager.update_config({"servers": [bad, local_server]})
    async with extension_manager.turn() as connection:
        definitions = await connection.discover()
        names = {item["function"]["name"] for item in definitions}
        assert names == {"mcp__local__echo", "mcp__local__fail", "mcp__local__slow"} == connection.tool_names
        assert [item["status"] for item in connection.status] == ["failed", "connected"]
        assert not any(name.startswith("mcp__broken__") for name in connection.registry)
        assert (await connection.call("mcp__local__echo", {"text": "works"}))["output"] == "works env=None"
        with pytest.raises(ValueError, match="not available"):
            await connection.call("mcp__broken__echo", {"text": "never"})
    assert not process_exists(int((tmp_path / "server.pid").read_text()))
    assert connection.tool_names == set()
    with pytest.raises(ValueError, match="no longer available"):
        await connection.call("mcp__local__echo", {"text": "must not run"})
    with pytest.raises(ValueError, match="start a new turn"):
        await connection.discover()


async def test_a_server_failing_mid_listing_leaves_none_of_its_tools(extension_manager, local_server, tmp_path, monkeypatch):
    from local_agent import mcp_client
    original = mcp_client.validate_schema
    seen = []

    def failing(schema):
        seen.append(schema)
        if len(seen) == 2:  # The server's second tool.
            raise ValueError("Unsupported schema")
        return original(schema)
    monkeypatch.setattr(mcp_client, "validate_schema", failing)
    extension_manager.update_config({"servers": [local_server]})
    async with extension_manager.turn() as connection:
        assert await connection.discover() == []
        assert connection.registry == {} and connection.status[0]["status"] == "failed"
        assert "Unsupported schema" in connection.status[0]["error"]
        assert not process_exists(int((tmp_path / "server.pid").read_text()))


async def test_cancellation_while_the_second_server_connects_reaps_both(extension_manager, local_server, tmp_path):
    slow = tmp_path / "unresponsive.py"
    slow.write_text("import os,time,sys; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)")
    second = {**local_server, "id": "second", "args": [str(slow), str(tmp_path / "second.pid")]}
    extension_manager.update_config({"servers": [local_server, second]})

    async def turn():
        async with extension_manager.turn() as connection:
            await connection.discover()

    task = asyncio.create_task(turn())
    async with asyncio.timeout(10):
        while not (tmp_path / "second.pid").exists() or not (tmp_path / "second.pid").read_text():
            await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert not process_exists(int((tmp_path / "server.pid").read_text()))
    assert not process_exists(int((tmp_path / "second.pid").read_text()))


async def test_stderr_tail_and_server_info_are_shown_redacted(extension_manager, tmp_path):
    extension_manager.settings.redact = lambda text: text.replace("secret-token", "[REDACTED]")
    server = tmp_path / "crash.py"
    server.write_text("import sys\nsys.stderr.write('x' * 5000 + '\\nstarting\\nfatal: bad config secret-token\\n')\nsys.exit(3)\n")
    crash = {"id": "crash", "name": "Crash", "transport": "stdio", "command": sys.executable, "args": [str(server)],
             "enabled": True}
    extension_manager.update_config({"servers": [crash]})
    async with extension_manager.turn() as connection:
        await connection.discover()
        [entry] = connection.status
    assert entry["status"] == "failed"
    assert entry["stderr_tail"].endswith("fatal: bad config [REDACTED]") and "secret-token" not in json.dumps(entry)
    assert len(entry["stderr_tail"]) <= 2000


async def test_status_reports_server_info_and_turned_off_servers(extension_manager, local_server, tmp_path):
    other = {**local_server, "id": "other", "args": [local_server["args"][0], str(tmp_path / "other.pid"), "unused"]}
    disabled = {**local_server, "id": "unused", "enabled": False}
    extension_manager.update_config({"servers": [local_server, other, disabled]})
    async with extension_manager.turn(off=["other"]) as connection:
        await connection.discover()
        status = {item["id"]: item for item in connection.status}
    assert status["local"]["status"] == "connected" and status["local"]["server_info"]["name"] == "Local test"
    assert status["local"]["tools"] == ["mcp__local__echo", "mcp__local__fail", "mcp__local__slow"]
    assert status["other"]["status"] == "off" and status["unused"]["status"] == "disabled"
    assert not (tmp_path / "other.pid").exists()  # A server the chat turned off never starts.


async def test_a_failed_server_waits_before_it_is_tried_again(extension_manager, local_server, tmp_path, monkeypatch):
    marker = tmp_path / "starts.txt"
    script = tmp_path / "count.py"
    script.write_text(f"from pathlib import Path\nwith open({str(marker)!r}, 'a') as f: f.write('x')\nraise SystemExit(1)\n")
    broken = {**local_server, "id": "broken", "args": [str(script)]}
    extension_manager.update_config({"servers": [broken]})
    for _ in range(2):
        async with extension_manager.turn() as connection:
            await connection.discover()
    assert marker.read_text() == "x"  # Not started again within RETRY_SECONDS.
    assert connection.status[0]["status"] == "failed" and 50 < connection.status[0]["retry_seconds"] <= 60
    extension_manager.failures["broken"]["at"] -= 61
    async with extension_manager.turn() as connection:
        await connection.discover()
    assert marker.read_text() == "xx"
    # The Test button tries it at once, also while a chat would wait.
    assert (await extension_manager.test())["servers"][0]["status"] == "failed"
    assert marker.read_text() == "xxx" and "broken" in extension_manager.failures
    # A saved change is tried again at once.
    extension_manager.update_config({"servers": [local_server]})
    assert extension_manager.failures == {}


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


async def test_schema_budget_skips_the_server_and_reaps_it(extension_manager, local_server, tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.mcp_client.MAX_DEFINITION_BYTES", 20)
    extension_manager.update_config({"servers": [local_server]})
    async with extension_manager.turn() as connection:
        assert await connection.discover() == []
        assert connection.status[0]["status"] == "skipped" and "enable fewer servers or tools" in connection.status[0]["error"]
        assert not process_exists(int((tmp_path / "server.pid").read_text()))
    assert extension_manager.failures == {}  # A skip is not a failure: no waiting before the next turn.


async def test_the_aggregate_cap_keeps_the_earlier_servers_tools(extension_manager, local_server, tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.mcp_client.MAX_TOOLS", 4)
    second = {**local_server, "id": "second", "args": [local_server["args"][0], str(tmp_path / "second.pid"), "unused"]}
    extension_manager.update_config({"servers": [local_server, second]})
    async with extension_manager.turn() as connection:
        definitions = await connection.discover()
        assert [item["status"] for item in connection.status] == ["connected", "skipped"]
        assert {item["function"]["name"] for item in definitions} == {"mcp__local__echo", "mcp__local__fail", "mcp__local__slow"}
        assert not process_exists(int((tmp_path / "second.pid").read_text()))


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


@pytest.fixture
def rich_server(tmp_path):
    server = tmp_path / "rich_server.py"
    server.write_text('''import base64
from mcp.server import MCPServer
from mcp.types import (AudioContent, BlobResourceContents, EmbeddedResource, ImageContent, ResourceLink, TextContent,
                       TextResourceContents, ToolAnnotations)
server = MCPServer("Rich", log_level="ERROR")
PNG = base64.b64encode(b"0" * 2008).decode()
@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False, title="Show content"))
def rich() -> list:
    return [TextContent(type="text", text="Summary"),
            ImageContent(type="image", data=PNG, mime_type="image/png"),
            ImageContent(type="image", data=PNG, mime_type="image/svg+xml"),
            EmbeddedResource(type="resource", resource=TextResourceContents(uri="file:///notes.txt", mime_type="text/plain",
                                                                            text="Inside the resource")),
            EmbeddedResource(type="resource", resource=BlobResourceContents(uri="file:///data.bin", blob=PNG)),
            ResourceLink(type="resource_link", uri="https://example.com/doc", name="doc", description="The manual"),
            AudioContent(type="audio", data=PNG, mime_type="audio/wav")]
@server.tool(annotations=ToolAnnotations(destructive_hint=True))
def long() -> str:
    return "x" * 30000
server.run()
''')
    return {"id": "rich", "name": "Rich", "transport": "stdio", "command": sys.executable, "args": [str(server)],
            "enabled": True}


async def test_rich_content_is_flattened_images_described_and_annotations_kept(extension_manager, rich_server):
    from local_agent.result_store import ResultStore
    store = ResultStore()
    extension_manager.update_config({"servers": [rich_server]})
    async with extension_manager.turn() as connection:
        await connection.discover()
        assert connection.annotations == {"mcp__rich__rich": {"title": "Show content", "read_only": True, "open_world": False},
                                          "mcp__rich__long": {"destructive": True}}
        connection.results = store.chat("chat")
        result = await connection.call("mcp__rich__rich", {})
        long = await connection.call("mcp__rich__long", {})
    lines = result["output"].split("\n")
    assert lines[0] == "Summary"
    assert lines[1] == "[image/png, 2 KB; shown in the chat, not sent to the model]"
    assert lines[2] == "[image/svg+xml, 2 KB; not shown or sent to the model]"
    assert lines[3:5] == ["Resource file:///notes.txt (text/plain):", "Inside the resource"]
    assert lines[5] == "[Resource file:///data.bin: 2 KB of binary data, not shown.]"
    assert lines[6] == "Link: doc https://example.com/doc — The manual"
    assert lines[7] == "[audio/wav audio, 2 KB; not sent to the model]"
    [image] = result["images"]
    assert image["mime_type"] == "image/png" and image["bytes"] == 2008
    assert base64.b64decode(store.image("chat", image["id"])["data"]) == b"0" * 2008
    assert "result_id" not in result  # It fits its first page.
    assert long["truncated"] is True and long["total_chars"] == 30000 and long["next_offset"] == len(long["output"])
    assert len(json.dumps(long, indent=2).encode()) <= 8000
    assert store.page("chat", long["result_id"], long["next_offset"])["output"] == "x" * 4000


def test_structured_content_follows_the_text_unless_it_repeats_it():
    from local_agent.mcp_client import flatten_content
    result = SimpleNamespace(content=[SimpleNamespace(type="text", text="x hi")], structured_content={"result": "x hi"})
    assert flatten_content(result) == ("x hi", [])
    result = SimpleNamespace(content=[SimpleNamespace(type="text", text="Found 2")], structured_content={"items": [1, 2]})
    assert flatten_content(result)[0] == 'Found 2\nStructured content (JSON):\n{"items": [1, 2]}'
    assert flatten_content(SimpleNamespace(content=[], structured_content={"a": 1}))[0] == '{"a": 1}'
