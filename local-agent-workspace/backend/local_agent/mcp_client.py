import asyncio
import copy
import hashlib
import json
import os
import re
from contextlib import AsyncExitStack

import httpx2
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import PaginatedRequestParams

from .jobs import ENVIRONMENT_KEYS
from .tool_schema import validate_arguments, validate_schema

CONNECT_TIMEOUT = 15
CALL_TIMEOUT = 60
MAX_TOOLS = 32
MAX_DEFINITION_BYTES = 16_000
MAX_RESULT_BYTES = 8_000


def model_tool_name(server_id, tool_name):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", tool_name)
    prefix = f"mcp__{server_id}__"
    if safe != tool_name or len(prefix + safe) > 64:
        suffix = "_" + hashlib.sha256(tool_name.encode()).hexdigest()[:10]
        safe = safe[:64 - len(prefix) - len(suffix)] + suffix
    return prefix + safe


def bounded_result(text, is_error=False, limit=MAX_RESULT_BYTES):
    result = {"output": text, "is_error": bool(is_error), "truncated": False}
    if len(json.dumps(result, indent=2).encode()) <= limit:
        return result
    result["truncated"] = True
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        result["output"] = text[:middle]
        if len(json.dumps(result, indent=2).encode()) <= limit:
            low = middle
        else:
            high = middle - 1
    result["output"] = text[:low]
    return result


def bounded_error(text):
    result = bounded_result(text, is_error=True, limit=MAX_RESULT_BYTES - 100)
    return result["output"] + ("\n[Provider error truncated.]" if result["truncated"] else "")


class MCPConnection:
    """A turn-scoped registry; enter, discover, call and exit on the same task."""

    def __init__(self, servers, redact):
        self.servers = copy.deepcopy(servers)
        self.redact = redact
        self.stack = AsyncExitStack()
        self.registry = {}
        self.definitions = []
        self.tool_names = set()
        self._discovered = False
        self._unavailable = False

    async def __aenter__(self):
        await self.stack.__aenter__()
        return self

    async def __aexit__(self, *error):
        # Preserve a caller's error instead of wrapping it in the transports'
        # nested task-group ExceptionGroups; resource cleanup still runs here,
        # on the same task that opened the SDK contexts.
        try:
            await self.stack.aclose()
        finally:
            self.registry.clear()
            self.definitions.clear()
            self.tool_names.clear()
            self._unavailable = True
        return False

    async def discover(self):
        if self._unavailable:
            raise ValueError("MCP discovery failed or this turn has closed; start a new turn before using its tools.")
        if self._discovered:
            return copy.deepcopy(self.definitions)
        for server in self.servers:
            if not server["enabled"]:
                continue
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    if server["transport"] == "stdio":
                        env = {key: value for key, value in os.environ.items() if key in ENVIRONMENT_KEYS}
                        parameters = StdioServerParameters(command=server["command"], args=server["args"], env=env)
                        stderr = self.stack.enter_context(open(os.devnull, "w"))
                        streams = await self.stack.enter_async_context(stdio_client(parameters, errlog=stderr))
                    else:
                        client = await self.stack.enter_async_context(httpx2.AsyncClient(
                            timeout=httpx2.Timeout(CONNECT_TIMEOUT, read=CALL_TIMEOUT), trust_env=False,
                            follow_redirects=False))
                        streams = await self.stack.enter_async_context(streamable_http_client(server["url"], http_client=client))
                    session = await self.stack.enter_async_context(ClientSession(*streams, read_timeout_seconds=CALL_TIMEOUT))
                    await session.initialize()
                    cursor = None
                    seen_cursors = set()
                    while True:
                        page = await session.list_tools(params=PaginatedRequestParams(cursor=cursor) if cursor else None)
                        for tool in page.tools:
                            name = model_tool_name(server["id"], tool.name)
                            if name in self.registry:
                                raise ValueError("MCP tools have duplicate or colliding names.")
                            definition = {"type": "function", "function": {"name": name,
                                "description": f"External MCP tool from {server['name']}: {tool.description or tool.name}",
                                "parameters": tool.input_schema}}
                            validate_schema(tool.input_schema)
                            candidate = [*self.definitions, definition]
                            if len(candidate) > MAX_TOOLS or len(json.dumps(candidate).encode()) > MAX_DEFINITION_BYTES:
                                raise ValueError("MCP discovery exceeds 32 tools or 16 KB of tool definitions; enable fewer servers/tools.")
                            self.registry[name] = {"server_id": server["id"], "tool_name": tool.name, "session": session,
                                                   "schema": copy.deepcopy(tool.input_schema)}
                            self.definitions.append(definition)
                        cursor = page.next_cursor
                        if not cursor:
                            break
                        if cursor in seen_cursors:
                            raise ValueError("MCP server repeated its tool-list cursor.")
                        seen_cursors.add(cursor)
            except (Exception, asyncio.CancelledError) as error:
                self.registry.clear()
                self.definitions.clear()
                self.tool_names.clear()
                self._unavailable = True
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise ValueError(bounded_error(self.redact(f"MCP server {server['id']} could not connect or list tools: {error}"))) from error
        self._discovered = True
        self.tool_names = set(self.registry)
        return copy.deepcopy(self.definitions)

    async def call(self, name, arguments):
        if self._unavailable:
            raise ValueError("MCP connection is no longer available in this turn.")
        if not self._discovered:
            await self.discover()
        tool = self.registry.get(name)
        if tool is None:
            raise ValueError("MCP tool is not available in this turn's configured servers.")
        validate_arguments(tool["schema"], arguments, limit=32_000)
        try:
            async with asyncio.timeout(CALL_TIMEOUT):
                result = await tool["session"].call_tool(tool["tool_name"], arguments=arguments,
                                                       read_timeout_seconds=CALL_TIMEOUT)
        except TimeoutError as error:
            raise ValueError("MCP tool timed out; its remote side effects may have completed. Check before retrying.") from error
        except Exception as error:
            raise ValueError(bounded_error(self.redact(f"MCP tool failed: {error}"))) from error
        parts = []
        for content in result.content:
            if content.type == "text":
                parts.append(content.text)
            else:
                parts.append(f"[MCP {content.type} content omitted; this client displays text results only.]")
        if result.structured_content is not None and not parts:
            parts.append(json.dumps(result.structured_content, ensure_ascii=False))
        return bounded_result(self.redact("\n".join(parts)), result.is_error)
