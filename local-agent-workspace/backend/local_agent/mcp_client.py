import asyncio
import base64
import binascii
import copy
import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import AsyncExitStack, suppress

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
# A server that failed is not started again in any chat for this long (the Test button still tries it).
RETRY_SECONDS = 60
STDERR_TAIL_CHARS = 2_000  # the end of a stdio server's stderr shown after a failure
INSTRUCTIONS_CHARS = 1_000  # a server's own instructions, shown in Agent tools only; never sent to the model
# A result longer than its first page is kept (result_store.py) up to this many characters.
FULL_RESULT_CHARS = 1_000_000
STRUCTURED_CHARS = 64_000  # structured content after the text, as JSON
# Images a result may carry to its card (never to the model): count, decoded size and types.
MAX_IMAGES = 2
MAX_IMAGE_BYTES = 512 * 1024
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
# Room in the first page for the fields that point at the rest (result_id, next_offset, ...).
PAGE_FIELDS_RESERVE = 240
# Tool annotations kept for the card: hints from the server, shown, never used for permissions.
ANNOTATION_FIELDS = {"title": "title", "read_only_hint": "read_only", "destructive_hint": "destructive",
                     "idempotent_hint": "idempotent", "open_world_hint": "open_world"}


def model_tool_name(server_id, tool_name):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", tool_name)
    prefix = f"mcp__{server_id}__"
    if safe != tool_name or len(prefix + safe) > 64:
        suffix = "_" + hashlib.sha256(tool_name.encode()).hexdigest()[:10]
        safe = safe[:64 - len(prefix) - len(suffix)] + suffix
    return prefix + safe


def server_switch(server_id):
    """The entry in a chat's turned-off tools that turns a whole server off (no process starts)."""
    return f"mcp__{server_id}__*"


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


def _size(count):
    return f"{count / 1024:,.0f} KB" if count >= 1024 else f"{count:,} bytes"


def _decoded_size(data):
    return len(data) * 3 // 4 - data[-2:].count("=") if isinstance(data, str) else 0


def _structured_text(structured, text):
    """structured content as JSON for after the text, or "" when the text already says it (a tool
    returning text also sends it as {"result": text})."""
    values = [structured]
    if isinstance(structured, dict) and len(structured) == 1:
        values.append(next(iter(structured.values())))
    for value in values:
        shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if shown.strip() and shown.strip() in text:
            return ""
        with suppress(ValueError, TypeError):
            if text.strip() and json.loads(text) == value:
                return ""
    shown = json.dumps(structured, ensure_ascii=False)
    if len(shown) > STRUCTURED_CHARS:
        shown = shown[:STRUCTURED_CHARS] + " […structured content shortened by the app]"
    return shown


def flatten_content(result):
    """(text, images) for a CallToolResult: text stays text; an embedded text resource is labelled and
    kept, a binary one and audio are described; a link is one line; an image is described for the model
    and kept for the card (at most MAX_IMAGES of MAX_IMAGE_BYTES, IMAGE_TYPES only); structured
    content follows as JSON. images: [{mime_type, data (base64), bytes}]."""
    parts, images = [], []
    for content in result.content:
        kind = getattr(content, "type", "")
        if kind == "text":
            parts.append(content.text)
        elif kind == "resource":
            resource = content.resource
            mime = f" ({resource.mime_type})" if resource.mime_type else ""
            if isinstance(getattr(resource, "text", None), str):
                parts.append(f"Resource {resource.uri}{mime}:\n{resource.text}")
            else:
                parts.append(f"[Resource {resource.uri}{mime}: {_size(_decoded_size(getattr(resource, 'blob', '')))} of "
                             "binary data, not shown.]")
        elif kind == "resource_link":
            description = f" — {content.description}" if content.description else ""
            parts.append(f"Link: {content.name} {content.uri}{description}")
        elif kind == "image":
            size = _decoded_size(content.data)
            kept = False
            if content.mime_type in IMAGE_TYPES and size <= MAX_IMAGE_BYTES and len(images) < MAX_IMAGES:
                try:
                    base64.b64decode(content.data, validate=True)
                    images.append({"mime_type": content.mime_type, "data": content.data, "bytes": size})
                    kept = True
                except (binascii.Error, ValueError):
                    pass
            parts.append(f"[{content.mime_type}, {_size(size)}; "
                         + ("shown in the chat, not sent to the model]" if kept else "not shown or sent to the model]"))
        elif kind == "audio":
            parts.append(f"[{content.mime_type} audio, {_size(_decoded_size(content.data))}; not sent to the model]")
        else:
            parts.append(f"[MCP {kind or 'unknown'} content omitted.]")
    text = "\n".join(parts)
    if result.structured_content is not None:
        structured = _structured_text(result.structured_content, text)
        if structured:
            text = (text + "\nStructured content (JSON):\n" if text else "") + structured
    return text, images


def annotations_of(tool):
    """The tool's annotations the card shows ({title?, read_only?, ...}), or None."""
    source = getattr(tool, "annotations", None)
    found = {}
    for field, key in ANNOTATION_FIELDS.items():
        value = getattr(source, field, None) if source is not None else None
        if field == "title" and value is None:
            value = getattr(tool, "title", None)
        if isinstance(value, bool) or (field == "title" and isinstance(value, str) and value.strip()):
            found[key] = value if isinstance(value, bool) else value.strip()[:120]
    return found or None


def _signature(server):
    return json.dumps(server, sort_keys=True)


class SkipServer(ValueError):
    """A server that works but would exceed the aggregate tool limits: skipped, not failed."""


class MCPConnection:
    """A turn-scoped registry; enter, discover, call and exit on the same task.

    Each server connects on its own exit stack: a server that fails is closed at once and
    contributes nothing, and the others still work. status lists every configured server:
    {id, name, status: connected | failed | skipped | disabled | off, tools, error?,
    stderr_tail?, server_info?, instructions?, retry_seconds?}. off: the chat turned the
    server off (off holds its ID), so it never starts. failures (shared by the turns of
    every chat): {server ID: {at, signature, entry}} of servers that failed, retried only
    after RETRY_SECONDS. resolve(server) returns (env, headers) for a connect or raises
    ValueError (a missing secret). results, when set by the caller, keeps long results
    and images (result_store.ChatResults)."""

    def __init__(self, servers, redact, *, off=(), failures=None, resolve=None, state_dir=None, clock=time.monotonic):
        self.servers = copy.deepcopy(servers)
        self.redact = redact
        self.off = set(off)
        self.failures = failures if failures is not None else {}
        self.resolve = resolve
        self.state_dir = state_dir
        self.clock = clock
        self.stack = AsyncExitStack()
        self.registry = {}
        self.definitions = []
        self.tool_names = set()
        self.status = []
        self.results = None
        self.errlogs = {}
        self._discovered = False
        self._unavailable = False

    @property
    def annotations(self):
        return {name: tool["annotations"] for name, tool in self.registry.items() if tool.get("annotations")}

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
            self.errlogs.clear()
            self._unavailable = True
        return False

    def _tail(self, errlog):
        """The redacted end of a stdio server's stderr, or ""."""
        if errlog is None or errlog.closed:
            return ""
        try:
            descriptor = errlog.fileno()
            size = os.fstat(descriptor).st_size
            # pread leaves the offset the server writes at alone. Read more than is shown: a token
            # cut at the start of the window is redacted whole first.
            start = max(0, size - 2 * STDERR_TAIL_CHARS)
            text = os.pread(descriptor, size - start, start).decode("utf-8", errors="replace")
        except (OSError, ValueError):
            return ""
        text = self.redact(text)
        if len(text) > STDERR_TAIL_CHARS:
            text = text[-STDERR_TAIL_CHARS:]
            text = text.split("\n", 1)[1] if "\n" in text[:200] else text
        return text.strip()

    async def discover(self):
        if self._unavailable:
            raise ValueError("MCP discovery failed or this turn has closed; start a new turn before using its tools.")
        if self._discovered:
            return copy.deepcopy(self.definitions)
        self.status = []
        for server in self.servers:
            entry = {"id": server["id"], "name": self.redact(server["name"]), "status": "disabled", "tools": []}
            self.status.append(entry)
            if not server["enabled"]:
                continue
            if server["id"] in self.off:
                entry["status"] = "off"
                continue
            failed = self.failures.get(server["id"])
            if failed and failed["signature"] == _signature(server):
                waited = self.clock() - failed["at"]
                if waited < RETRY_SECONDS:
                    entry.update({key: value for key, value in failed["entry"].items() if key not in ("id", "name")},
                                 retry_seconds=max(1, round(RETRY_SECONDS - waited)))
                    continue
            await self._connect(server, entry)
        self._discovered = True
        self.tool_names = set(self.registry)
        return copy.deepcopy(self.definitions)

    async def _connect(self, server, entry):
        """Connect one server on its own stack and merge its tools, or record why it did not."""
        stack = AsyncExitStack()
        errlog = None
        tools = []
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                env, headers = self.resolve(server) if self.resolve else ({}, {})
                if server["transport"] == "stdio":
                    base = {key: value for key, value in os.environ.items() if key in ENVIRONMENT_KEYS}
                    parameters = StdioServerParameters(command=server["command"], args=server["args"], env={**base, **env})
                    # stderr needs a real file descriptor; the file is already unlinked.
                    errlog = tempfile.TemporaryFile(dir=self.state_dir)
                    streams = await stack.enter_async_context(stdio_client(parameters, errlog=errlog))
                else:
                    client = await stack.enter_async_context(httpx2.AsyncClient(
                        timeout=httpx2.Timeout(CONNECT_TIMEOUT, read=CALL_TIMEOUT), trust_env=False,
                        follow_redirects=False, headers=headers))
                    streams = await stack.enter_async_context(streamable_http_client(server["url"], http_client=client))
                session = await stack.enter_async_context(ClientSession(*streams, read_timeout_seconds=CALL_TIMEOUT))
                initialized = await session.initialize()
                cursor = None
                seen_cursors = set()
                names = set()
                while True:
                    page = await session.list_tools(params=PaginatedRequestParams(cursor=cursor) if cursor else None)
                    for tool in page.tools:
                        name = model_tool_name(server["id"], tool.name)
                        if name in self.registry or name in names:
                            raise ValueError("MCP tools have duplicate or colliding names.")
                        names.add(name)
                        validate_schema(tool.input_schema)
                        tools.append((name, tool, {"type": "function", "function": {"name": name,
                            "description": f"External MCP tool from {server['name']}: {tool.description or tool.name}",
                            "parameters": tool.input_schema}}))
                    cursor = page.next_cursor
                    if not cursor:
                        break
                    if cursor in seen_cursors:
                        raise ValueError("MCP server repeated its tool-list cursor.")
                    seen_cursors.add(cursor)
            candidate = [*self.definitions, *(definition for _, _, definition in tools)]
            if len(candidate) > MAX_TOOLS or len(json.dumps(candidate).encode()) > MAX_DEFINITION_BYTES:
                raise SkipServer(f"with the servers before it, its {len(tools)} tools would exceed {MAX_TOOLS} MCP "
                                 f"tools or {MAX_DEFINITION_BYTES // 1000} KB of tool definitions; enable fewer servers "
                                 "or tools.")
        except asyncio.CancelledError:
            with suppress(Exception):
                await stack.aclose()
            if errlog is not None:
                errlog.close()
            raise
        except Exception as error:
            with suppress(Exception):
                await stack.aclose()  # On this task, before the next server starts.
            tail = self._tail(errlog)
            if errlog is not None:
                errlog.close()
            skipped = isinstance(error, SkipServer)
            detail = str(error) if skipped else (f"could not connect or list tools: {error}" if str(error)
                                                 else f"could not connect or list tools ({type(error).__name__}).")
            if isinstance(error, TimeoutError):
                detail = f"did not answer within {CONNECT_TIMEOUT} seconds."
            entry.update(status="skipped" if skipped else "failed", error=bounded_error(self.redact(detail))[:1000],
                         **({"stderr_tail": tail} if tail else {}))
            if not skipped:
                self.failures[server["id"]] = {"at": self.clock(), "signature": _signature(server),
                                               "entry": {key: value for key, value in entry.items()}}
            return
        # Closed in reverse order at turn end, on this task.
        self.stack.push_async_exit(stack.__aexit__)
        if errlog is not None:
            self.stack.callback(errlog.close)
            self.errlogs[server["id"]] = errlog
        self.failures.pop(server["id"], None)
        for name, tool, definition in tools:
            self.registry[name] = {"server_id": server["id"], "tool_name": tool.name, "session": session,
                                   "schema": copy.deepcopy(tool.input_schema), "annotations": annotations_of(tool)}
            self.definitions.append(definition)
        info = initialized.server_info
        entry.update(status="connected", tools=[name for name, _, _ in tools],
                     server_info={key: self.redact(value)[:120] for key, value in
                                  (("name", info.name), ("title", info.title), ("version", info.version)) if value})
        if initialized.instructions:
            entry["instructions"] = self.redact(initialized.instructions)[:INSTRUCTIONS_CHARS]

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
            tail = self._tail(self.errlogs.get(tool["server_id"]))
            raise ValueError(bounded_error(self.redact(f"MCP tool failed: {error}")
                                           + (f"\nServer stderr (end):\n{tail}" if tail else ""))) from error
        text, images = flatten_content(result)
        # Redacted before any cut: a cut through a secret leaves a part the exact match no longer finds.
        text = self.redact(text)
        page = bounded_result(text, result.is_error)
        if page["truncated"] and self.results is not None:
            kept = text[:FULL_RESULT_CHARS]
            page = bounded_result(text, result.is_error, limit=MAX_RESULT_BYTES - PAGE_FIELDS_RESERVE)
            page.update(result_id=self.results.add(kept), next_offset=len(page["output"]), total_chars=len(kept))
            if len(text) > len(kept):
                page["rest_not_kept"] = True
        if images and self.results is not None:
            page["images"] = [{"id": self.results.add_image(image["mime_type"], image["data"]),
                               "mime_type": image["mime_type"], "bytes": image["bytes"]} for image in images]
        return page
