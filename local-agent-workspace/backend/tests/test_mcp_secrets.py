"""MCP secret references (env and headers), workspace credentials, and their redaction."""
import asyncio
import json
import os
import signal
import socket
import sys
from types import SimpleNamespace

import pytest

from local_agent.extensions import ExtensionManager, validate_config, workspace_match

SECRET = "synthetic-secret-value-0123"


@pytest.fixture
def credential_file(tmp_path):
    path = tmp_path / "credentials.env"
    path.write_text(f"DBRICKS_URL=https://workspace.example.com\nDBRICKS_TOKEN=synthetic-workspace-token\n"
                    f"GH_TOKEN={SECRET}\nAPI_KEY='another-synthetic-key-456'\n")
    return path


@pytest.fixture
def manager(tmp_path, credential_file):
    state = tmp_path / "state"
    state.mkdir()
    settings = SimpleNamespace(state_dir=state, redact=lambda text: text.replace("synthetic-workspace-token", "[REDACTED]"),
                               values={"env_file": str(credential_file)},
                               credentials=lambda: ("https://workspace.example.com", "synthetic-workspace-token"))
    return ExtensionManager(settings, app_skills=tmp_path / "none")


def stdio(env=None, **values):
    return {"id": "local", "name": "Local", "transport": "stdio", "command": "server", "enabled": True,
            **({"env": env} if env is not None else {}), **values}


def http(url="https://workspace.example.com/api/2.0/mcp/sql", **values):
    return {"id": "remote", "name": "Remote", "transport": "http", "url": url, "enabled": True, **values}


def test_references_and_plain_values_validate():
    saved = validate_config({"servers": [stdio({"GITHUB_PERSONAL_ACCESS_TOKEN": {"secret": "GH_TOKEN"}, "LOG_LEVEL": "debug"}),
                                         http(headers={"Authorization": {"secret": "GH_TOKEN", "prefix": "Bearer "},
                                                       "X-Client": "local"})]})
    assert saved["servers"][0]["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": {"secret": "GH_TOKEN"}, "LOG_LEVEL": "debug"}
    assert saved["servers"][1]["headers"]["Authorization"] == {"secret": "GH_TOKEN", "prefix": "Bearer "}
    assert validate_config({"servers": [http(workspace_credentials=True)]})["servers"][0]["workspace_credentials"] is True


@pytest.mark.parametrize("server,message", [
    (stdio({"SECRET": "value"}), "looks like a secret"),
    (stdio({"GITHUB_TOKEN": "plain"}), 'use {"secret": "GITHUB_TOKEN"}'),
    (stdio({"OPTIONS": "--token=ghp" + "_abcdefghijklmnopqrstuvwxyz"}), "looks like a secret"),
    (stdio({"PATH": "/srv/other"}), "cannot be set"),
    (stdio({"LD_PRELOAD": "/srv/other/x.so"}), "cannot be set"),
    (stdio({"PYTHONSTARTUP": "x"}), "cannot be set"),
    (stdio({"DATABRICKS_HOST": "x"}), "cannot be set"),
    (stdio({"lower": "x"}), "uppercase"),
    (stdio({"TOKEN": {"secret": "DBRICKS_TOKEN"}}), "workspace_credentials"),
    (stdio({"TOKEN": {"secret": "bad name"}}), "credential-file key"),
    (stdio({f"V{n}": "x" for n in range(17)}), "at most 16"),
    (stdio({"V": "x" * 4097}), "at most 4,096"),
    (stdio(headers={"A": "b"}), "take env"),
    (http(headers={"Host": "example.com"}), "set by the app"),
    (http(headers={"X-Note": "line\r\nInjected: yes"}), "one line"),
    (http(headers={"X-A": "1", "x-a": "2"}), "duplicate"),
    (http(headers={"Authorization": "Bearer abcdefghijklmnopqrstuvwxyz"}), "looks like a secret"),
    (http(env={"A": "b"}), "take headers"),
    (http("http://mcp.example.com/mcp", headers={"X-Api-Key": {"secret": "API_KEY"}}), "need an https URL"),
    (http(workspace_credentials=True, headers={"authorization": {"secret": "GH_TOKEN"}}), "remove yours"),
    (http("http://workspace.example.com/mcp", workspace_credentials=True), "https"),
    (http(workspace_credentials="yes"), "true or false"),
    # A prefix is saved in plain text: a scheme word and one space only.
    (http(headers={"Authorization": {"secret": "EMPTY", "prefix": "ghp" + "_abcdefghijklmnopqrstuvwxyz12"}}), "scheme word"),
    (http(headers={"Cookie": {"secret": "CSRF", "prefix": "session=s3cr3tvalue1; csrf="}}), "scheme word"),
    (http(headers={"Authorization": {"secret": "T", "prefix": "Bearer"}}), "scheme word"),
    (http(headers={"Authorization": {"secret": "T", "prefix": "abcdef0123456789 "}}), "scheme word"),
])
def test_unsafe_values_are_refused(server, message):
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace("{", r"\{")):
        validate_config({"servers": [server]})


def test_header_prefixes_that_are_scheme_words_validate():
    for prefix in ("Bearer ", "bearer ", "Key ", "Klaviyo-API-Key "):
        saved = validate_config({"servers": [http(headers={"Authorization": {"secret": "T", "prefix": prefix}})]})
        assert saved["servers"][0]["headers"]["Authorization"] == {"secret": "T", "prefix": prefix}
    saved = validate_config({"servers": [http(headers={"Authorization": {"secret": "T", "prefix": ""}})]})
    assert saved["servers"][0]["headers"]["Authorization"] == {"secret": "T"}


def test_a_new_server_id_cannot_contain_a_double_underscore(manager, tmp_path):
    with pytest.raises(ValueError, match="cannot contain __"):
        manager.update_config({"servers": [stdio(id="ab__x")]})
    # One saved before the rule keeps loading and saving.
    manager.path.write_text(json.dumps({"servers": [stdio(id="ab__x")], "hooks": []}))
    reloaded = ExtensionManager(manager.settings, app_skills=tmp_path / "none")
    assert reloaded.update_config({"servers": [stdio(id="ab__x", name="Renamed")]})["servers"][0]["name"] == "Renamed"


def test_a_plain_value_holding_the_workspace_token_is_refused(manager):
    with pytest.raises(ValueError, match="looks like a secret"):
        manager.update_config({"servers": [stdio({"NOTE": "x-synthetic-workspace-token"})]})


@pytest.mark.parametrize("url,expected", [
    ("https://workspace.example.com/api/2.0/mcp/genie/space", True),
    ("https://WORKSPACE.example.com:443/api/2.0/mcp/sql", True),
    ("https://workspace.example.com:8443/api/2.0/mcp/sql", False),
    ("http://workspace.example.com/api/2.0/mcp/sql", False),
    ("https://workspace.example.com.attacker.example/mcp", False),
    ("https://other.example.com/mcp", False),
    ("https://workspace.example.com:bad/mcp", False),
])
def test_workspace_credentials_match_only_the_workspace_host_and_port(url, expected):
    assert workspace_match(url, "https://workspace.example.com") is expected


def test_resolve_reads_references_at_connect_and_workspace_credentials_on_the_host(manager, credential_file):
    env, headers = manager.resolve(stdio({"GITHUB_TOKEN": {"secret": "GH_TOKEN"}, "MODE": "fast"}))
    assert env == {"GITHUB_TOKEN": SECRET, "MODE": "fast"} and headers == {}
    _, headers = manager.resolve(http(headers={"X-Api-Key": {"secret": "API_KEY", "prefix": "Key "}},
                                      workspace_credentials=True))
    assert headers == {"X-Api-Key": "Key another-synthetic-key-456", "Authorization": "Bearer synthetic-workspace-token"}
    with pytest.raises(ValueError, match="only for https URLs on your Databricks workspace") as refused:
        manager.resolve(http("https://other.example.com/mcp", workspace_credentials=True))
    assert "synthetic-workspace-token" not in str(refused.value)
    # Read again at every connect: a changed credential file applies to the next turn.
    credential_file.write_text(credential_file.read_text().replace(SECRET, "changed-synthetic-value-789"))
    assert manager.resolve(stdio({"GITHUB_TOKEN": {"secret": "GH_TOKEN"}}))[0] == {"GITHUB_TOKEN": "changed-synthetic-value-789"}


def test_a_reference_holding_the_workspace_token_is_never_sent(manager, credential_file):
    credential_file.write_text(credential_file.read_text() + "ALIAS=synthetic-workspace-token\n")
    for server in (stdio({"TOKEN": {"secret": "ALIAS"}}), http("https://other.example.com/mcp", headers={
            "Authorization": {"secret": "ALIAS", "prefix": "Bearer "}})):
        with pytest.raises(ValueError, match="holds your Databricks token") as refused:
            manager.resolve(server)
        assert "synthetic-workspace-token" not in str(refused.value)


def test_a_missing_key_names_the_key_never_a_value(manager):
    with pytest.raises(ValueError, match="Add GITHUB_PAT=… to your credential file") as missing:
        manager.resolve(stdio({"GITHUB_TOKEN": {"secret": "GITHUB_PAT"}}))
    assert SECRET not in str(missing.value)


def test_no_value_is_saved_or_shown_and_redaction_covers_resolved_values(manager):
    manager.update_config({"servers": [stdio({"GITHUB_TOKEN": {"secret": "GH_TOKEN"}}),
                                       {**http(headers={"X-Api-Key": {"secret": "API_KEY"}}), "enabled": False}]})
    for text in (manager.path.read_text(), json.dumps(manager.public_config())):
        assert SECRET not in text and "another-synthetic-key-456" not in text and "GH_TOKEN" in text
    # Only enabled servers' values are redacted (a disabled server's are never read for a connect).
    assert manager.secret_values() == [SECRET]
    assert manager._redact(f"echo {SECRET} synthetic-workspace-token", manager._tokens()) == "echo [REDACTED] [REDACTED]"


@pytest.fixture
def echo_server(tmp_path):
    server = tmp_path / "env_server.py"
    server.write_text('''import os
from mcp.server import MCPServer
server = MCPServer("Env test", log_level="ERROR")
@server.tool()
def show(name: str) -> str:
    return name + "=" + str(os.environ.get(name))
server.run()
''')
    return server


async def test_a_stdio_server_gets_the_secret_and_its_echo_is_redacted(manager, echo_server):
    manager.update_config({"servers": [stdio({"GITHUB_TOKEN": {"secret": "GH_TOKEN"}, "MODE": "fast"},
                                             command=sys.executable, args=[str(echo_server)])]})
    async with manager.turn() as connection:
        await connection.discover()
        shown = await connection.call("mcp__local__show", {"name": "GITHUB_TOKEN"})
        mode = await connection.call("mcp__local__show", {"name": "MODE"})
        workspace = await connection.call("mcp__local__show", {"name": "DBRICKS_TOKEN"})
    assert shown["output"] == "GITHUB_TOKEN=[REDACTED]" and mode["output"] == "MODE=fast"
    assert workspace["output"] == "DBRICKS_TOKEN=None"


async def test_a_secret_a_server_echoes_in_its_tool_definitions_never_reaches_the_model(manager, tmp_path):
    server = tmp_path / "leaky_server.py"
    server.write_text('''import os, sys
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
server = MCPServer("Leaky", log_level="ERROR")
key = os.environ["GITHUB_TOKEN"]
def show(query: str) -> str:
    return "ok"
server.tool(description="Uses key " + key, title="Search with " + key,
            annotations=ToolAnnotations(title="Search " + key, readOnlyHint=True))(show)
if sys.argv[1:] == ["name"]:
    server.tool(name="leak_" + key)(show)
server.run()
''')
    leaky = stdio({"GITHUB_TOKEN": {"secret": "GH_TOKEN"}}, command=sys.executable, args=[str(server)])
    manager.update_config({"servers": [leaky]})
    async with manager.turn() as connection:
        definitions = await connection.discover()
        assert SECRET not in json.dumps(definitions) and SECRET not in json.dumps(connection.annotations)
        assert definitions[0]["function"]["description"].endswith("Uses key [REDACTED]")
        assert connection.annotations["mcp__local__show"] == {"title": "Search [REDACTED]", "read_only": True}
        # The schema as the server sent it still validates the arguments.
        assert (await connection.call("mcp__local__show", {"query": "x"}))["output"] == "ok"
    # A tool name that holds a secret fails that server: a name cannot be redacted and still be called.
    manager.update_config({"servers": [{**leaky, "args": [str(server), "name"]}]})
    async with manager.turn() as connection:
        assert await connection.discover() == []
        assert connection.status[0]["status"] == "failed" and "holds a secret" in connection.status[0]["error"]
        assert SECRET not in json.dumps(connection.status)


async def test_a_missing_secret_fails_only_that_server(manager, echo_server):
    good = stdio(command=sys.executable, args=[str(echo_server)])
    missing = {**good, "id": "missing", "env": {"GITHUB_TOKEN": {"secret": "NOT_THERE"}}}
    manager.update_config({"servers": [missing, good]})
    async with manager.turn() as connection:
        definitions = await connection.discover()
        status = {item["id"]: item for item in connection.status}
    assert [item["function"]["name"] for item in definitions] == ["mcp__local__show"]
    assert status["missing"]["status"] == "failed" and "Add NOT_THERE=…" in status["missing"]["error"]


async def test_an_http_server_receives_the_header(manager, tmp_path):
    server = tmp_path / "http_server.py"
    server.write_text('''import sys
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
server = MCPServer("HTTP headers", log_level="ERROR")
@server.tool()
def whoami(ctx: Context) -> str:
    headers = ctx.headers or {}
    return "key=" + str(headers.get("x-api-key")) + " client=" + str(headers.get("x-client"))
server.run(transport="streamable-http", port=int(sys.argv[1]))
''')
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = await asyncio.create_subprocess_exec(sys.executable, str(server), str(port),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        async with asyncio.timeout(10):
            while True:
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(.01)
        manager.update_config({"servers": [http(f"http://127.0.0.1:{port}/mcp", headers={
            "X-Api-Key": {"secret": "API_KEY", "prefix": "Key "}, "X-Client": "local-test"})]})
        async with manager.turn() as connection:
            await connection.discover()
            result = await connection.call("mcp__remote__whoami", {})
        # The server received the resolved value; what comes back is redacted.
        assert result["output"] == "key=Key [REDACTED] client=local-test"
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


async def test_workspace_credentials_for_another_host_fail_that_server_without_a_request(manager, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No connection may be opened for a refused server")
    monkeypatch.setattr("local_agent.mcp_client.httpx2.AsyncClient", forbidden)
    manager.update_config({"servers": [http("https://other.example.com/api/2.0/mcp/sql", workspace_credentials=True)]})
    async with manager.turn() as connection:
        assert await connection.discover() == []
    assert connection.status[0]["status"] == "failed" and "workspace.example.com" in connection.status[0]["error"]
    assert "synthetic-workspace-token" not in json.dumps(connection.status)


async def test_an_echoed_secret_never_reaches_the_chat_wire_ledger_or_export(tmp_path, monkeypatch, credential_file):
    from contextlib import asynccontextmanager

    import httpx

    from local_agent.agents import AgentManager
    from local_agent.config import Settings
    from local_agent.portability import export_bundle
    from local_agent.store import Store
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file=str(credential_file))
    store = Store(settings.state_dir / "secrets.sqlite3")
    manager = AgentManager(store, settings)
    manager.extensions.update_config({"servers": [stdio({"GITHUB_TOKEN": {"secret": "GH_TOKEN"}})]})
    session = store.create(settings.values)
    session["permission_mode"] = "bypassPermissions"

    class Connection:
        definitions = [{"type": "function", "function": {"name": "mcp__local__show", "description": "Show",
                                                          "parameters": {"type": "object", "properties": {}}}}]
        tool_names = {"mcp__local__show"}

        async def discover(self):
            return self.definitions

        async def call(self, name, arguments):
            # A connection that missed its own redaction: the app's still applies.
            return {"output": f"token is {SECRET}", "is_error": False, "truncated": False}

    @asynccontextmanager
    async def turn(**options):
        yield Connection()
    manager.extensions.turn = turn
    sent = []

    async def handler(request):
        sent.append(request.content.decode())
        if len(sent) == 1:
            call = {"index": 0, "id": "c1", "type": "function", "function": {"name": "mcp__local__show", "arguments": "{}"}}
            body = {"choices": [{"delta": {"tool_calls": [call]}, "finish_reason": "tool_calls"}]}
        else:
            body = {"choices": [{"delta": {"content": "Done."}, "finish_reason": "stop"}]}
        return httpx.Response(200, text="data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n")
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handler)))
    await manager.run(session, "Show the token")
    card = next(event for event in session["events"] if event.get("name") == "mcp__local__show")
    assert "token is [REDACTED]" in card["output"]
    saved = store.get(session["id"])
    for text in (json.dumps(saved), json.dumps(export_bundle([saved], saved["id"])), *sent,
                 manager.extensions.path.read_text()):
        assert SECRET not in text
    store.db.close()
