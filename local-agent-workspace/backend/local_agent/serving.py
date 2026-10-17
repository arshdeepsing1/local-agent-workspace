"""Model serving requests: the one place their URLs, headers, timeouts and HTTP client are built
(and the HTTP client of the app's other outside requests, open_external_client).

Every model call (agent and subagent requests, compaction, titles, /btw side questions) and the
endpoint listing go through a ServingClient. The client is httpx.AsyncClient, looked up when a
request starts, so tests that replace it with a MockTransport keep working. A ReplayClient serves
scripted or recorded responses (ReplayTransport) through the same seam, offline: an unmatched
request fails instead of reaching the network.
"""
import json
import re
from pathlib import Path
from urllib.parse import quote

import httpx

from .request_limits import LONG_REQUEST_TIMEOUT, STREAM_READ_TIMEOUT
from .telemetry import request_tag_headers

# A request that is not streamed (/btw, each compaction request): Databricks allows up to 597 seconds.
LONG_TIMEOUT = httpx.Timeout(LONG_REQUEST_TIMEOUT, connect=20)
# Compact now's client; each of its requests sets LONG_TIMEOUT itself.
COMPACTION_CLIENT_TIMEOUT = httpx.Timeout(120, connect=20)
LISTING_TIMEOUT = 20  # The connection check (GET /api/2.0/serving-endpoints).
INVOCATIONS_PATH = re.compile(r"/serving-endpoints/([^/]+)/invocations")


def stream_timeout(read=STREAM_READ_TIMEOUT):
    """A streamed agent request's timeouts: a prefill of a large context with adaptive thinking can stay
    silent for minutes, so read is long (agents passes its own STREAM_READ_TIMEOUT)."""
    return httpx.Timeout(connect=20, read=read, write=120, pool=20)


def invocations_url(host, model):
    """The chat-completions URL of one serving endpoint (the name is quoted: it is never a path)."""
    return host + "/serving-endpoints/" + quote(model, safe="") + "/invocations"


def endpoints_url(host):
    """The workspace's serving endpoint list."""
    return host + "/api/2.0/serving-endpoints"


def endpoint_url(host, name):
    """One serving endpoint's description (task and state)."""
    return endpoints_url(host) + "/" + quote(name, safe="")


def request_headers(token, session=None, call=None, json_body=False):
    """Authorization, the request tags naming the ledger record (with call) and, for a body, its type."""
    headers = {"Authorization": f"Bearer {token}"}
    if call is not None:
        headers.update(request_tag_headers(session, call))
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def call_headers(headers, session, call):
    """headers (request_headers(token)) for one ledger record's request with a JSON body."""
    return {**headers, **request_tag_headers(session, call), "Content-Type": "application/json"}


def open_client(timeout, transport=None):
    """An httpx.AsyncClient that never follows redirects (a redirect could carry the token elsewhere).
    httpx.AsyncClient is looked up now, not at import, and transport is passed only when set: test
    helpers that replace it pass their own transport."""
    extra = {"transport": transport} if transport is not None else {}
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False, **extra)


def open_external_client(timeout, transport=None, headers=None):
    """An httpx.AsyncClient for the app's requests to other services (web_fetch.py: a web page, a search
    provider): no redirects followed (web_fetch follows them itself, after its checks), and no proxy,
    certificate or other setting from the environment (trust_env off)."""
    extra = {"transport": transport} if transport is not None else {}
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False, headers=headers, **extra)


class ServingClient:
    """Model serving requests for one Settings object: its credentials and the HTTP client (the URLs and
    headers come from the functions above). transport (an httpx.AsyncBaseTransport) answers every request
    instead of the network when set."""

    def __init__(self, settings, transport=None):
        self.settings = settings
        self.transport = transport

    def credentials(self):
        """(host, token) from the credential file; ValueError when it is not usable."""
        return self.settings.credentials()

    def client(self, timeout):
        """The HTTP client for one request or one turn (open_client, with this client's transport)."""
        return open_client(timeout, self.transport)


class UnmatchedRequest(AssertionError):
    """A request no replay fixture answers. It is raised (never sent) and recorded in unmatched."""


class ReplayTransport(httpx.AsyncBaseTransport):
    """Answers requests from fixtures, in order, offline. Each fixture is a dict:

    - which requests it answers: model (that endpoint's invocations path) or path (a regular
      expression the URL path must match in full; default: any invocations path), method (default
      POST, or GET for an endpoints path) and match (a callable given the parsed JSON body);
    - its answer: status (default 200) and headers, then chunks (an SSE stream: each a dict sent as
      JSON or a string sent as is, then "data: [DONE]" unless done is False), json, or text; or
      error: "connect", "timeout" or "read" (that httpx error is raised before any status); break:
      "read" ends a streamed answer with a read error after its chunks instead of [DONE];
    - times: how many requests it answers (default 1; None answers every matching request).

    requests records each request ({method, path, model, headers, body}: body is the parsed JSON or
    None) and unmatched the requests no fixture answered.
    """

    def __init__(self, fixtures=()):
        self.fixtures = [dict(fixture) for fixture in fixtures]
        self.used = [0] * len(self.fixtures)
        self.requests = []
        self.unmatched = []

    @classmethod
    def from_fixture(cls, path):
        """Fixtures from a JSON file: one fixture, a list, or {"fixtures": [...]}. Other keys (the request
        body that was sent, notes) are kept and ignored."""
        data = json.loads(Path(path).read_text())
        if isinstance(data, dict) and isinstance(data.get("fixtures"), list):
            data = data["fixtures"]
        return cls(data if isinstance(data, list) else [data])

    def add(self, *fixtures):
        """Append fixtures (answered after the earlier ones)."""
        for fixture in fixtures:
            self.fixtures.append(dict(fixture))
            self.used.append(0)
        return self

    def bodies(self, model=None):
        """The parsed bodies of the model requests sent, to one endpoint when model is given."""
        return [item["body"] for item in self.requests
                if item["model"] is not None and (model is None or item["model"] == model)]

    def remaining(self):
        """Fixtures with a fixed count that have not answered all their requests yet."""
        return [fixture for fixture, used in zip(self.fixtures, self.used)
                if fixture.get("times", 1) is not None and used < fixture.get("times", 1)]

    def _matches(self, fixture, request, body):
        path = request.url.path
        if "model" in fixture:
            pattern = re.escape("/serving-endpoints/" + fixture["model"] + "/invocations")
        else:
            pattern = fixture.get("path", INVOCATIONS_PATH.pattern)
        method = fixture.get("method", "GET" if pattern.startswith("/api/2.0/") else "POST")
        if request.method != method or not re.fullmatch(pattern, path):
            return False
        check = fixture.get("match")
        return check is None or bool(check(body))

    async def handle_async_request(self, request):
        content = await request.aread()
        try:
            body = json.loads(content) if content else None
        except (UnicodeDecodeError, ValueError):
            body = None
        found = INVOCATIONS_PATH.fullmatch(request.url.path)
        record = {"method": request.method, "path": request.url.path, "model": found and found.group(1),
                  "headers": dict(request.headers), "body": body}
        self.requests.append(record)
        for index, fixture in enumerate(self.fixtures):
            times = fixture.get("times", 1)
            if (times is None or self.used[index] < times) and self._matches(fixture, request, body):
                self.used[index] += 1
                return self._answer(fixture, request)
        self.unmatched.append(record)
        raise UnmatchedRequest(f"No replay fixture answers {request.method} {request.url.path}.")

    @staticmethod
    def _answer(fixture, request):
        error = fixture.get("error")
        if error is not None:
            kind = {"connect": httpx.ConnectError, "timeout": httpx.ConnectTimeout, "read": httpx.ReadError}[error]
            raise kind(f"Replayed {error} failure.", request=request)
        status, headers = fixture.get("status", 200), dict(fixture.get("headers") or {})
        if "chunks" in fixture:
            lines = [chunk if isinstance(chunk, str) else json.dumps(chunk) for chunk in fixture["chunks"]]
            if fixture.get("done", True) and not fixture.get("break"):
                lines.append("[DONE]")
            data = "".join(f"data: {line}\n\n" for line in lines).encode("utf-8")
            headers.setdefault("content-type", "text/event-stream")
            if fixture.get("break") == "read":
                return httpx.Response(status, headers=headers, stream=_BrokenStream(data), request=request)
            return httpx.Response(status, headers=headers, content=data, request=request)
        if "json" in fixture:
            return httpx.Response(status, headers=headers, json=fixture["json"], request=request)
        return httpx.Response(status, headers=headers, text=fixture.get("text", ""), request=request)


class _BrokenStream(httpx.AsyncByteStream):
    """A streamed answer whose connection fails after its first bytes."""

    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data
        raise httpx.ReadError("Replayed read failure after the stream began.")


class ReplayClient(ServingClient):
    """A ServingClient answered by a ReplayTransport, with synthetic credentials: no credential file and
    no network. transport is the ReplayTransport, so a test can add fixtures and read what was sent."""

    HOST = "https://serving.example"
    TOKEN = "replay-token"

    def __init__(self, settings, fixtures=(), host=HOST, token=TOKEN):
        transport = fixtures if isinstance(fixtures, ReplayTransport) else ReplayTransport(fixtures)
        super().__init__(settings, transport)
        self.host, self.token = host, token

    def credentials(self):
        return self.host, self.token


def sse_chunks(text="Done.", finish="stop", usage=None, tool_calls=None):
    """The chunks of a short streamed reply, for fixtures: text, then optional tool calls, then the finish
    reason (with usage when given)."""
    chunks = [{"choices": [{"delta": {"content": text}, "finish_reason": None}]}] if text else []
    for index, call in enumerate(tool_calls or ()):
        chunks.append({"choices": [{"delta": {"tool_calls": [{
            "index": index, "id": call["id"], "type": "function",
            "function": {"name": call["name"], "arguments": json.dumps(call.get("arguments", {}))}}]},
            "finish_reason": None}]})
    last = {"choices": [{"delta": {}, "finish_reason": finish}]}
    if usage is not None:
        last["usage"] = usage
    chunks.append(last)
    return chunks
