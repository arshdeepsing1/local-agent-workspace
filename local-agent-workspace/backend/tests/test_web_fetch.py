"""web_fetch's SSRF guard, fetch, HTML conversion and paging, and web_search's providers (web_fetch.py).

Offline only: live fetches go to a local http.server that the tests reach through an injected resolver,
classifier and port list; searches use httpx.MockTransport."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpcore
import httpx
import pytest

from local_agent import web_fetch
from local_agent.redaction import redact_secrets
from local_agent.web_fetch import (GuardedBackend, GuardedTransport, Refused, check_url, classify, content_kind,
                                   domain_of, html_to_text, normalize_domain, page_result)

REFUSED = [
    "http://127.0.0.1/", "http://127.0.0.2:80/", "http://0x7f000001/", "http://2130706433/", "http://017700000001/",
    "http://127.1/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[::ffff:7f00:1]/", "http://[::127.0.0.1]/",
    "http://169.254.169.254/latest/meta-data/", "http://metadata.google.internal/computeMetadata/v1/",
    "http://10.0.0.1/", "http://10.255.255.255/", "http://172.16.0.1/", "http://172.31.255.254/", "http://192.168.1.1/",
    "http://100.64.0.1/", "http://198.18.0.1/", "http://[fd00::1]/", "http://[fc00::1]/", "http://[fe80::1]/",
    "http://[fe80::1%25eth0]/", "http://[fec0::1]/", "http://0.0.0.0/", "http://[::]/", "http://224.0.0.1/",
    "http://255.255.255.255/", "http://localhost/", "http://LOCALHOST./", "http://a.localhost/", "http://printer.local/",
    "http://db.corp/", "http://intranet/", "http://[2002:7f00:1::]/", "http://[2002:a00:1::]/", "http://[64:ff9b::7f00:1]/",
    "http://[64:ff9b::a00:1]/", "http://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/", "file:///etc/passwd", "ftp://example.com/",
    "gopher://example.com/", "javascript:alert(1)", "http://user:secret@example.com/", "http://user@example.com/",
    "http://example.com:8080/", "https://example.com:22/", "http://example.com:notaport/", "http://999.1.1.1/",
    "http://example.123/", "https://example.com/?api_key=abcdefgh12345678", "https://example.com/?token=abcdef1234567890",
    "https://example.com/ghp_abcdefghijklmnopqrstuvwxyz0123", "http://exa mple.com/", "", "http://",
]


@pytest.mark.parametrize("url", REFUSED)
def test_the_ssrf_table_refuses_every_address_outside_the_public_internet(url):
    with pytest.raises(Refused):
        check_url(url)


@pytest.mark.parametrize("url, host", [
    ("https://docs.python.org/3/", "docs.python.org"), ("http://93.184.215.14/", "93.184.215.14"),
    ("https://[2606:2800:21f:cb07:6820:80da:af6b:8b2c]/", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"),
    ("HTTPS://Docs.Example.COM./a?b=1#frag", "docs.example.com"), ("https://bücher.example/x", "xn--bcher-kva.example"),
    ("http://example.com:80/", "example.com"), ("https://example.com:443/", "example.com"),
])
def test_public_addresses_and_names_pass_and_are_normalized(url, host):
    target = check_url(url)
    assert target.host == host
    assert "#" not in target.url and ":80/" not in target.url and ":443/" not in target.url
    assert domain_of(url) == host


def test_classify_names_the_reason_and_sees_embedded_ipv4():
    assert classify("93.184.215.14") is None
    assert classify("2606:2800:21f:cb07:6820:80da:af6b:8b2c") is None
    assert "loopback" in classify("::ffff:127.0.0.1")
    assert "metadata" in classify("169.254.169.254")
    assert classify("2002:5db8:d70e::1") is not None  # 6to4 is not a global address, whatever it embeds
    assert classify("not an address") == "is not an IP address"


def test_a_secret_in_the_url_is_refused():
    with pytest.raises(Refused, match="one of your secrets"):
        check_url("https://example.com/search?q=synthetic-workspace-token-1", secrets=["synthetic-workspace-token-1"])
    with pytest.raises(Refused, match="one of your secrets"):
        check_url("https://example.com/search?q=synthetic%2Dworkspace%2Dtoken%2D1", secrets=["synthetic-workspace-token-1"])
    assert check_url("https://example.com/search?q=python+tokens").host == "example.com"


def test_domain_rules_normalize_like_urls():
    assert normalize_domain("Docs.Python.org") == "docs.python.org"
    assert normalize_domain("https://Docs.Python.org/3/") == "docs.python.org"
    with pytest.raises(ValueError, match="Enter a domain"):
        normalize_domain("not a domain")
    assert domain_of("ftp://") is None and domain_of(None) is None


# The backend ---------------------------------------------------------------------------------------------------


class NoNetwork:
    async def connect_tcp(self, *args, **kwargs):
        raise AssertionError("A refused name must never be connected to.")

    async def sleep(self, seconds):
        pass


async def test_a_name_with_one_public_and_one_private_address_is_refused_before_connecting():
    async def resolver(host, port):
        return ["93.184.215.14", "10.0.0.5"]
    backend = GuardedBackend(resolver=resolver, inner=NoNetwork())
    with pytest.raises(httpcore.ConnectError, match="10.0.0.5"):
        await backend.connect_tcp("mixed.example", 443)
    assert "is a private address" in backend.refusal


async def test_every_connection_is_resolved_and_checked_again():
    answers = iter([["93.184.215.14"], ["127.0.0.1"]])
    connected = []

    class Recorder(NoNetwork):
        async def connect_tcp(self, host, port, **kwargs):
            connected.append(host)
            return object()

    async def resolver(host, port):
        return next(answers)
    backend = GuardedBackend(resolver=resolver, inner=Recorder())
    await backend.connect_tcp("rebind.example", 443)
    with pytest.raises(httpcore.ConnectError, match="loopback"):
        await backend.connect_tcp("rebind.example", 443)
    assert connected == ["93.184.215.14"]  # the checked address, never the name


# Live fetches from a local server ----------------------------------------------------------------------------


class Site:
    """A local http.server. routes: path -> (status, headers, body) or a callable(handler) -> that tuple."""

    def __init__(self):
        self.routes, self.requests = {}, []
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                site.requests.append({"path": self.path, "headers": dict(self.headers)})
                route = site.routes.get(self.path.split("?")[0], (404, {"Content-Type": "text/plain"}, b"missing"))
                status, headers, body = route(self) if callable(route) else route
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def url(self, path, host="site.example"):
        return f"http://{host}:{self.port}{path}"


@pytest.fixture
def site():
    server = Site()
    yield server
    server.server.shutdown()
    server.server.server_close()


def backend_for(site, names=None):
    """A backend that resolves names (default: every name to 127.0.0.1) and treats only 127.0.0.1 as public."""
    async def resolver(host, port):
        value = (names or {}).get(host, ["127.0.0.1"])
        return value() if callable(value) else value
    return GuardedBackend(resolver=resolver, classifier=lambda ip: None if ip == "127.0.0.1" else classify(ip))


async def get(site, path, backend=None, **options):
    backend = backend or backend_for(site)
    return await web_fetch.fetch(site.url(path), backend=backend, ports=(site.port,), **options)


async def test_an_html_page_becomes_text_with_its_title(site):
    site.routes["/doc"] = (200, {"Content-Type": "text/html; charset=utf-8"},
                           "<title>Guide</title><h2>Install</h2><p>Run <code>pip</code>.</p>"
                           "<script>steal()</script>".encode())
    page = await get(site, "/doc")
    assert page["status"] == 200 and page["title"] == "Guide" and page["content_type"] == "text/html"
    assert page["text"] == "## Install\n\nRun `pip`." and page["cut"] is False
    assert site.requests[0]["headers"]["User-Agent"] == web_fetch.USER_AGENT


async def test_same_host_redirects_are_followed_without_cookies(site):
    site.routes["/a"] = (302, {"Location": "/b", "Set-Cookie": "session=abc; Path=/"}, b"")
    site.routes["/b"] = (200, {"Content-Type": "text/plain"}, b"arrived")
    page = await get(site, "/a")
    assert page["text"] == "arrived" and page["final_url"].endswith("/b") and page["url"].endswith("/a")
    assert "Cookie" not in site.requests[1]["headers"]


async def test_a_redirect_to_another_host_is_returned_for_a_new_call(site):
    site.routes["/a"] = (301, {"Location": f"http://other.example:{site.port}/x"}, b"")
    page = await get(site, "/a")
    assert page["redirect"] == f"http://other.example:{site.port}/x" and page["status"] == 301
    assert "own approval" in page["message"] and len(site.requests) == 1


@pytest.mark.parametrize("location", ["http://10.0.0.1/", "http://localhost/", "file:///etc/passwd",
                                      "http://169.254.169.254/latest/"])
async def test_a_redirect_to_a_private_address_is_refused(site, location):
    site.routes["/a"] = (302, {"Location": location}, b"")
    with pytest.raises(Refused, match="redirects to an address that is not fetched"):
        await get(site, "/a")


async def test_a_name_resolving_to_a_private_address_is_refused_at_connect(site):
    with pytest.raises(Refused, match="resolves to 10.0.0.7"):
        await get(site, "/a", backend=backend_for(site, {"site.example": ["127.0.0.1", "10.0.0.7"]}))
    assert site.requests == []


async def test_a_rebinding_name_is_refused_when_it_connects_again(site):
    answers = iter([["127.0.0.1"], ["10.0.0.9"]])
    site.routes["/start"] = (302, {"Location": "/next", "Connection": "close"}, b"")
    site.routes["/next"] = (200, {"Content-Type": "text/plain"}, b"private data")
    with pytest.raises(Refused, match="resolves to 10.0.0.9"):
        await get(site, "/start", backend=backend_for(site, {"site.example": lambda: next(answers)}))
    assert [request["path"] for request in site.requests] == ["/start"]


async def test_more_than_five_redirects_stop(site):
    for index in range(7):
        site.routes[f"/{index}"] = (302, {"Location": f"/{index + 1}"}, b"")
    with pytest.raises(Refused, match="more than 5 times"):
        await get(site, "/0")
    assert len(site.requests) == 6


async def test_the_body_is_read_up_to_the_cap(site):
    site.routes["/big"] = (200, {"Content-Type": "text/plain"}, b"x" * 3_000_000)
    page = await get(site, "/big")
    assert page["cut"] is True and len(page["text"]) == web_fetch.MAX_BYTES


@pytest.mark.parametrize("content_type, body, expected", [
    ("application/json", b'{"a": [1, 2]}', '{\n  "a": [\n    1,\n    2\n  ]\n}'),
    ("text/plain; charset=latin-1", "café".encode("latin-1"), "café"),
    ("application/problem+json", b'{"title": "x"}', '{\n  "title": "x"\n}'),
    ("text/markdown", b"# Title", "# Title"),
])
async def test_json_and_text_are_kept_readable(site, content_type, body, expected):
    site.routes["/x"] = (200, {"Content-Type": content_type}, body)
    assert (await get(site, "/x"))["text"] == expected


@pytest.mark.parametrize("content_type", ["image/png", "application/pdf", "application/octet-stream"])
async def test_other_content_types_are_refused(site, content_type):
    site.routes["/x"] = (200, {"Content-Type": content_type}, b"\x89PNG")
    with pytest.raises(Refused, match="does not read"):
        await get(site, "/x")


async def test_redact_applies_to_the_text_and_the_title(site):
    site.routes["/x"] = (200, {"Content-Type": "text/html"},
                         b"<title>token=abcdefgh12345678</title><p>password: hunter2hunter2</p>")
    page = await get(site, "/x", redact=redact_secrets)
    assert "abcdefgh12345678" not in json.dumps(page) and "hunter2hunter2" not in page["text"]


async def test_a_connection_failure_names_the_host(site):
    async def resolver(host, port):
        return ["127.0.0.1"]
    port = site.port
    site.server.shutdown()
    site.server.server_close()
    backend = GuardedBackend(resolver=resolver, classifier=lambda ip: None)
    with pytest.raises(ValueError, match="Could not connect to site.example"):
        await web_fetch.fetch(f"http://site.example:{port}/", backend=backend, ports=(port,))


def test_the_default_transport_uses_the_guarded_backend():
    backend = GuardedBackend()
    transport = GuardedTransport(backend)
    assert transport._pool._network_backend is backend


# Conversion and paging ---------------------------------------------------------------------------------------


def test_html_conversion_keeps_structure_and_drops_scripts():
    title, text = html_to_text(
        "<html><head><title> Docs  page </title><style>p{}</style></head><body><nav><a href='/home'>Home</a></nav>"
        "<h1>Main &amp; title</h1><p>Hello   <b>world</b>, see <a href='guide.html'>the guide</a>.</p>"
        "<ul><li>One</li><li>Two</li></ul><pre>def f():\n    return 1</pre>"
        "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
        "<svg><text>drawn</text></svg><script>alert(1)</script><a href='javascript:x()'>Run</a></body></html>",
        "https://example.com/docs/")
    assert title == "Docs page"
    assert text == ("[Home](https://example.com/home)\n\n# Main & title\n\nHello world, see "
                    "[the guide](https://example.com/docs/guide.html).\n\n- One\n- Two\n\n```\ndef f():\n    return 1\n"
                    "```\n\nA | B\n1 | 2\n\nRun")


def test_a_page_without_a_closed_head_still_has_its_body():
    title, text = html_to_text("<html><head><title>T</title><body><p>Body text</p>", "https://example.com/")
    assert (title, text) == ("T", "Body text")


def test_content_kinds():
    assert content_kind("text/html; charset=utf-8") == "html"
    assert content_kind("application/vnd.api+json") == "json"
    assert content_kind("image/svg+xml") == "text"
    assert content_kind("image/png") is None


def test_a_long_page_keeps_its_rest_for_get_tool_output():
    kept = []

    def keep(text, note):
        kept.append((text, note))
        return "out-123"
    fetched = {"url": "https://example.com/", "final_url": "https://example.com/", "status": 200,
               "content_type": "text/html", "title": "T", "text": "word " * 5_000, "cut": False}
    result = page_result(fetched, keep)
    assert len(json.dumps(result, indent=2).encode()) <= web_fetch.PAGE_BYTES
    assert result["result_id"] == "out-123" and result["total_chars"] == 25_000
    assert result["next_offset"] == len(result["output"]) > 1000
    assert "Untrusted web content from example.com" in result["note"] and "Untrusted" in kept[0][1]
    assert kept[0][0] == fetched["text"]
    short = page_result({**fetched, "text": "short"}, keep)
    assert short["output"] == "short" and "result_id" not in short and len(kept) == 1
    cut = page_result(fetched)
    assert cut["truncated"] is True and "result_id" not in cut


# web_search ----------------------------------------------------------------------------------------------------


def provider(handler):
    seen = []

    def respond(request):
        seen.append(request)
        return handler(request)
    return httpx.MockTransport(respond), seen


async def test_brave_sends_the_key_in_its_header_and_returns_plain_snippets():
    transport, seen = provider(lambda request: httpx.Response(200, json={"web": {"results": [
        {"title": "Python <strong>docs</strong>", "url": "https://docs.python.org/", "description": "The &amp; <b>docs</b>"},
        {"title": "Bad", "url": "javascript:alert(1)"}, {"title": "Two", "url": "https://example.com/2"}]}}))
    setup = web_fetch.search_setup({"web_search_provider": "brave", "env_file": "/srv/other/env"},
                                   lambda: {"BRAVE_API_KEY": "brave-synthetic-key"})
    result = await web_fetch.search(setup, "python docs", 1, transport=transport)
    assert result["results"] == [{"title": "Python docs", "url": "https://docs.python.org/", "snippet": "The & docs"}]
    assert result["provider"] == "Brave Search" and "Untrusted" in result["note"]
    request = seen[0]
    assert request.url.host == "api.search.brave.com" and request.url.params["q"] == "python docs"
    assert request.headers["X-Subscription-Token"] == "brave-synthetic-key"
    assert request.url.params["count"] == "1"


async def test_tavily_posts_the_query_with_a_bearer_key():
    transport, seen = provider(lambda request: httpx.Response(200, json={"results": [
        {"title": "A", "url": "https://example.com/a", "content": "x" * 400}]}))
    setup = web_fetch.search_setup({"web_search_provider": "tavily", "web_search_key": "MY_TAVILY"},
                                   lambda: {"MY_TAVILY": "tvly-synthetic"})
    result = await web_fetch.search(setup, "q", transport=transport)
    assert json.loads(seen[0].content) == {"query": "q", "max_results": 5, "search_depth": "basic"}
    assert seen[0].headers["Authorization"] == "Bearer tvly-synthetic"
    assert len(result["results"][0]["snippet"]) == web_fetch.SNIPPET_CHARS


async def test_searxng_uses_the_configured_address_even_on_this_computer():
    transport, seen = provider(lambda request: httpx.Response(200, json={"results": [
        {"title": "A", "url": "https://example.com/a", "content": "about a"}]}))
    setup = web_fetch.search_setup({"web_search_provider": "searxng", "web_search_url": "http://127.0.0.1:8888/"})
    result = await web_fetch.search(setup, "a", transport=transport)
    assert str(seen[0].url) == "http://127.0.0.1:8888/search?q=a&format=json"
    assert result["results"][0]["snippet"] == "about a"


@pytest.mark.parametrize("status, message", [(401, "Check the API key"), (429, "rate limit"), (500, "HTTP 500")])
async def test_provider_errors_name_the_status_never_the_key(status, message):
    transport, _ = provider(lambda request: httpx.Response(status, text="brave-synthetic-key"))
    setup = web_fetch.search_setup({"web_search_provider": "brave"}, lambda: {"BRAVE_API_KEY": "brave-synthetic-key"})
    with pytest.raises(ValueError, match=message) as raised:
        await web_fetch.search(setup, "q", transport=transport)
    assert "brave-synthetic-key" not in str(raised.value)


def test_search_setup_says_what_is_missing():
    assert web_fetch.search_setup({"web_search_provider": ""}) is None
    missing = web_fetch.search_setup({"web_search_provider": "brave", "env_file": "/srv/other/env"}, lambda: {})
    assert missing["problem"].startswith("Add BRAVE_API_KEY=… to your credential file")
    assert "key" not in missing
    assert "SearXNG address" in web_fetch.search_setup({"web_search_provider": "searxng"})["problem"]


def test_queries_with_secrets_are_not_sent():
    assert web_fetch.check_query("  python   asyncio  ") == "python asyncio"
    with pytest.raises(ValueError, match="one of your secrets"):
        web_fetch.check_query("find synthetic-token-12345", secrets=["synthetic-token-12345"])
    with pytest.raises(ValueError, match="looks like it contains a secret"):
        web_fetch.check_query("api_key=abcdefgh12345678 docs")
    with pytest.raises(ValueError, match="longer than 400"):
        web_fetch.check_query("x" * 401)
    with pytest.raises(ValueError, match="without a user name"):
        web_fetch.check_search_url("http://user:pw@127.0.0.1:8888")
