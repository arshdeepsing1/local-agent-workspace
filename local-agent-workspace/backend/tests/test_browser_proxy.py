"""The filtering proxy of the browser tool (browser_proxy.py).

Chromium sends every request to a local proxy. The proxy resolves each name itself, checks every address with the
app's address policy and connects to the exact address it checked, so a name that answers differently a second time
(DNS rebinding) cannot reach a private service. Nothing here touches the internet: the "public" and "private"
addresses are test addresses that a dial hook maps to local servers, and resolver hooks play the DNS.

Most tests talk to the proxy with raw sockets. The tests that need Chromium skip with the reason when it cannot start."""
import asyncio
import base64
import contextlib
import hashlib
import logging
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_browser_engine import browser_processes, require_browser

from local_agent import browser_proxy
from local_agent.browser import LAUNCH_FLAGS, BrowserSession
from local_agent.browser_proxy import (FilteringProxy, Policy, Upstream, check_upstream_setting, redact_url,
                                       resolve_upstream)
from local_agent.browser_tool import BrowserManager

PUBLIC, PRIVATE = "93.184.216.34", "10.1.2.3"
SECRET = "s3cr3t-pass-word"


@pytest.fixture(autouse=True)
def no_machine_socks_setting(monkeypatch):
    """Tests never read the SOCKS setting of the computer that runs them."""
    monkeypatch.setattr(browser_proxy, "macos_socks_proxy", lambda: None)


class Server:
    """A threaded loopback HTTP server that stands for a site. It records every request it gets."""

    def __init__(self, name, pages=None, ignore_close=False):
        self.name, self.requests, self.pages, self.headers = name, [], pages or {}, []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            if ignore_close:  # an origin that keeps the socket although the request says Connection: close
                protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.requests.append((self.command, self.path, self.headers.get("Host")))
                owner.headers.append({key.lower(): value for key, value in self.headers.items()})
                page = owner.pages.get(self.path.split("?")[0])
                code, extra = 200, ()
                if isinstance(page, tuple):  # (status, [(header, value)], body)
                    code, extra, page = page
                body = (page or f"SERVER-{owner.name} {self.path}").encode()
                self.send_response(code)
                for name, value in extra:
                    self.send_header(name, value)
                self.send_header("Content-Type", "text/html; charset=utf-8" if page else "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                if ignore_close:
                    self.close_connection = False

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.02), daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Chat:
    """One registered browser session: its credentials, the loopback keys it may reach and what the proxy told it."""

    def __init__(self, proxy, *allowed, host_map=None):
        self.allowed, self.refusals = set(allowed), []
        self.policy = Policy(allows=lambda key: key in self.allowed, host_map=host_map or {},
                             refused=lambda host, text: self.refusals.append((host, text)))
        self.username, self.password = proxy.register(self.policy)

    def header(self, password=None, username=None):
        token = f"{username or self.username}:{password or self.password}".encode()
        return b"Proxy-Authorization: Basic " + base64.b64encode(token) + b"\r\n"


class Rig:
    def __init__(self):
        self.public, self.private, self.loop, self.other = (Server("public"), Server("private"), Server("loop"),
                                                            Server("other"))
        self.answers, self.asked, self.dialed = {}, [], []
        self.maps = {(PUBLIC, 80): self.public.port, (PUBLIC, 443): self.public.port,
                     (PRIVATE, 80): self.private.port, (PRIVATE, 443): self.private.port}
        self.proxy = FilteringProxy(resolver=self.resolve, dial=self.dial)

    async def resolve(self, name):
        self.asked.append(name)
        answer = self.answers[name]
        return list(answer(self.asked.count(name)) if callable(answer) else answer)

    async def dial(self, address, port):
        self.dialed.append((address, port))
        return await asyncio.open_connection("127.0.0.1", self.maps.get((address, port), port))

    def chat(self, *allowed, **options):
        return Chat(self.proxy, *allowed, **options)

    async def talk(self, data, *, timeout=5):
        """Send bytes to the proxy and read until it closes the connection."""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        writer.write(data)
        await writer.drain()
        out = bytearray()
        try:
            while chunk := await asyncio.wait_for(reader.read(65536), timeout):
                out += chunk
        finally:
            writer.close()
        return bytes(out)

    async def connect(self, target, chat, inner=b"GET /probe HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n", header=None):
        auth = header if header is not None else (chat.header() if chat else b"")
        head = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n".encode() + auth + b"\r\n"
        return await self.talk(head + inner)

    async def get(self, target, chat, path="/probe", header=None):
        auth = header if header is not None else (chat.header() if chat else b"")
        head = f"GET http://{target}{path} HTTP/1.1\r\nHost: {target}\r\n".encode() + auth
        return await self.talk(head + b"Connection: close\r\n\r\n")

    def stop(self):
        for server in (self.public, self.private, self.loop, self.other):
            server.stop()


def status(data):
    return int(data.split(b" ", 2)[1]) if data.startswith(b"HTTP/") else 0


@pytest.fixture
async def rig():
    rig = Rig()
    await rig.proxy.start()
    yield rig
    await rig.proxy.stop()
    rig.stop()


def nothing_reached(rig):
    assert rig.private.requests == [] and rig.loop.requests == [] and rig.other.requests == []


# A name that answers differently a second time --------------------------------------------------------------------

async def test_a_name_that_answers_public_and_then_private_never_reaches_the_private_server(rig):
    rig.answers["rebind.test"] = lambda call: [PUBLIC] if call == 1 else [PRIVATE]
    chat = rig.chat()
    first = await rig.connect("rebind.test:80", chat)
    assert status(first) == 200 and b"SERVER-public" in first
    second = await rig.connect("rebind.test:80", chat)
    assert status(second) == 403 and b"private address" in second
    assert rig.private.requests == [] and (PRIVATE, 80) not in rig.dialed
    third = await rig.get("rebind.test:80", chat)
    assert status(third) == 403 and rig.private.requests == []


async def test_the_proxy_resolves_once_and_connects_to_the_address_it_checked(rig):
    rig.answers["pin.test"] = lambda call: [PUBLIC] if call == 1 else [PRIVATE]
    answer = await rig.connect("pin.test:80", rig.chat())
    assert status(answer) == 200 and b"SERVER-public" in answer
    assert rig.asked == ["pin.test"] and rig.dialed == [(PUBLIC, 80)]
    assert rig.private.requests == []


@pytest.mark.parametrize("answers", [[PUBLIC, PRIVATE], [PRIVATE, PUBLIC], ["100.64.0.5"], ["169.254.169.254"],
                                     ["fd00::1"], ["fe80::1"], ["0.0.0.0"], ["224.0.0.1"], ["::"], ["::ffff:10.1.2.3"]])
async def test_one_private_address_among_the_answers_refuses_the_name(rig, answers):
    rig.answers["mixed.test"] = answers
    answer = await rig.connect("mixed.test:80", rig.chat())
    assert status(answer) == 403
    assert rig.dialed == [] and rig.private.requests == []


async def test_a_refusal_is_recorded_for_the_browser_tool_to_report(rig):
    rig.answers["rebind.test"] = [PRIVATE]
    chat = rig.chat()
    await rig.connect("rebind.test:80", chat)
    assert chat.refusals == [("rebind.test", "blocked by the address check: rebind.test resolved to a private address")]


async def test_a_name_that_does_not_resolve_is_a_bad_gateway_and_not_a_refusal(rig):
    rig.answers["gone.test"] = []
    chat = rig.chat()
    assert status(await rig.connect("gone.test:80", chat)) == 502
    assert chat.refusals == []


# Loopback ---------------------------------------------------------------------------------------------------------

FORMS = ["127.0.0.1:{p}", "localhost:{p}", "[::1]:{p}", "[::ffff:127.0.0.1]:{p}", "[::ffff:7f00:1]:{p}", "2130706433:{p}",
         "0x7f000001:{p}", "017700000001:{p}", "0177.0.0.1:{p}", "127.1:{p}", "127.0.0.2:{p}", "evil.test:{p}",
         "evil6.test:{p}", "mapped.test:{p}", "Evil.Test.:{p}"]
ANSWERS = {"evil.test": ["127.0.0.1"], "evil6.test": ["::1"], "mapped.test": ["::ffff:127.0.0.1"]}


@pytest.mark.parametrize("method", ["connect", "get"])
@pytest.mark.parametrize("form", FORMS)
async def test_a_page_cannot_reach_a_loopback_port_that_is_not_allowed(rig, form, method):
    rig.answers.update(ANSWERS)
    rig.answers["evil.test."] = ["127.0.0.1"]
    target = form.format(p=rig.loop.port)
    answer = await (rig.connect(target, rig.chat()) if method == "connect" else rig.get(target, rig.chat()))
    assert status(answer) in (400, 403), answer
    nothing_reached(rig)


@pytest.mark.parametrize("form", [form for form in FORMS if form != "127.0.0.1:{p}"])
async def test_allowing_one_loopback_key_allows_no_other_way_to_the_same_port(rig, form):
    rig.answers.update(ANSWERS)
    chat = rig.chat(f"127.0.0.1:{rig.loop.port}")
    answer = await rig.connect(form.format(p=rig.loop.port), chat)
    assert status(answer) in (400, 403), answer
    nothing_reached(rig)


@pytest.mark.parametrize("method", ["connect", "get"])
async def test_an_allowed_loopback_server_is_reached(rig, method):
    chat = rig.chat(f"127.0.0.1:{rig.loop.port}")
    target = f"127.0.0.1:{rig.loop.port}"
    answer = await (rig.connect(target, chat) if method == "connect" else rig.get(target, chat))
    assert status(answer) == 200 and b"SERVER-loop /probe" in answer
    assert rig.other.requests == []


async def test_localhost_is_its_own_key_and_never_asks_the_dns(rig):
    chat = rig.chat(f"localhost:{rig.loop.port}")
    answer = await rig.connect(f"localhost:{rig.loop.port}", chat)
    assert status(answer) == 200 and b"SERVER-loop" in answer
    assert rig.asked == []
    later = rig.chat()
    assert status(await rig.connect(f"localhost:{rig.loop.port}", later)) == 403


async def test_the_allowed_keys_are_read_for_each_request(rig):
    chat = rig.chat()
    target = f"127.0.0.1:{rig.loop.port}"
    assert status(await rig.get(target, chat)) == 403
    chat.allowed.add(target)
    assert status(await rig.get(target, chat)) == 200
    chat.allowed.clear()
    assert status(await rig.get(target, chat)) == 403


async def test_a_page_cannot_reach_the_proxy_itself(rig):
    chat = rig.chat(f"127.0.0.1:{rig.proxy.port}")
    assert status(await rig.connect(f"127.0.0.1:{rig.proxy.port}", chat)) == 403


@pytest.mark.parametrize("target", ["10.1.2.3:80", "192.168.1.1:80", "169.254.169.254:80", "100.64.0.1:80", "[fd00::1]:80",
                                    "[fe80::1]:80", "0.0.0.0:80", "224.0.0.1:80", "intranet:80", "wiki.corp:80",
                                    "printer.local:80", "service.internal:80"])
async def test_private_targets_are_refused_even_when_a_loopback_key_is_allowed(rig, target):
    answer = await rig.connect(target, rig.chat(f"127.0.0.1:{rig.loop.port}"))
    assert status(answer) in (400, 403)
    assert rig.dialed == []


@pytest.mark.parametrize("method", ["connect", "get"])
async def test_one_chats_credentials_cannot_use_another_chats_loopback_key(rig, method):
    first, second = rig.chat(f"127.0.0.1:{rig.loop.port}"), rig.chat(f"127.0.0.1:{rig.other.port}")
    pick = rig.connect if method == "connect" else rig.get
    assert status(await pick(f"127.0.0.1:{rig.other.port}", first)) == 403
    assert status(await pick(f"127.0.0.1:{rig.loop.port}", second)) == 403
    assert rig.other.requests == [] and rig.loop.requests == []
    assert status(await pick(f"127.0.0.1:{rig.loop.port}", first)) == 200
    assert status(await pick(f"127.0.0.1:{rig.other.port}", second)) == 200


# Credentials ------------------------------------------------------------------------------------------------------

async def test_missing_or_wrong_credentials_get_407_and_nothing_is_forwarded(rig):
    chat = rig.chat(f"127.0.0.1:{rig.loop.port}")
    target = f"127.0.0.1:{rig.loop.port}"
    for how in ("connect", "get"):
        pick = rig.connect if how == "connect" else rig.get
        missing = await pick(target, None)
        assert status(missing) == 407 and b"Proxy-Authenticate: Basic" in missing
        for bad in (chat.header(password="wrong"), chat.header(username="nobody"), chat.header(password=chat.password + "x"),
                    chat.header(password=chat.password[:-1])):
            assert status(await pick(target, None, header=bad)) == 407
    assert rig.loop.requests == [] and rig.dialed == []


async def test_a_malformed_credentials_header_gets_407(rig):
    for header in (b"Proxy-Authorization: Bearer abc\r\n", b"Proxy-Authorization: Basic !!!\r\n",
                   b"Proxy-Authorization: Basic " + base64.b64encode(b"nocolon") + b"\r\n"):
        data = f"CONNECT 127.0.0.1:{rig.loop.port} HTTP/1.1\r\n".encode() + header + b"\r\n"
        assert status(await rig.talk(data)) == 407
    assert rig.loop.requests == []


async def test_each_session_has_its_own_random_credentials(rig):
    chats = [rig.chat() for _ in range(3)]
    assert len({chat.username for chat in chats}) == 3 and len({chat.password for chat in chats}) == 3
    assert all(len(chat.password) >= 32 and chat.password != chat.username for chat in chats)
    rig.proxy.unregister(chats[0].username)
    assert status(await rig.connect(f"127.0.0.1:{rig.loop.port}", chats[0])) == 407


async def test_the_sign_in_address_is_answered_by_the_proxy_and_only_with_credentials(rig):
    chat = rig.chat()
    answer = await rig.get(browser_proxy.PRIME_HOST, chat, "/")
    assert status(answer) == 200 and rig.asked == [] and rig.dialed == []
    assert status(await rig.get(browser_proxy.PRIME_HOST, None, "/")) == 407


async def test_secrets_are_compared_in_constant_time(rig, monkeypatch):
    seen = []
    real = browser_proxy.hmac.compare_digest
    monkeypatch.setattr(browser_proxy.hmac, "compare_digest", lambda a, b: seen.append((a, b)) or real(a, b))
    chat = rig.chat()
    await rig.connect("127.0.0.1:1", chat)
    await rig.connect("127.0.0.1:1", None)
    assert seen and all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in seen)


# What the proxy accepts -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("request_line", [
    "GET /probe HTTP/1.1", "POST /probe HTTP/1.1", "GET ftp://files.test/a HTTP/1.1", "GET file:///etc/hosts HTTP/1.1",
    "GET https://secure.test/ HTTP/1.1", "CONNECT secure.test HTTP/1.1", "CONNECT secure.test:0 HTTP/1.1",
    "CONNECT secure.test:99999 HTTP/1.1", "CONNECT http://secure.test:80 HTTP/1.1", "GET http://user:pw@secure.test/ HTTP/1.1",
    "BREW http://secure.test/ HTTP/1.1", "GET http://secure.test/ HTTP/9.9", "nonsense", "GET http:// HTTP/1.1"])
async def test_anything_but_connect_and_absolute_http_requests_is_rejected(rig, request_line):
    rig.answers["secure.test"] = [PUBLIC]
    chat = rig.chat()
    answer = await rig.talk(f"{request_line}\r\n".encode() + chat.header() + b"\r\n")
    assert 400 <= status(answer) < 500, answer
    assert rig.dialed == []


async def test_a_plain_request_is_forwarded_with_its_own_host_and_no_proxy_headers(rig):
    rig.answers["shop.test"] = [PUBLIC]
    chat = rig.chat()
    head = ("GET http://shop.test/page?q=1 HTTP/1.1\r\nHost: evil.internal\r\n".encode() + chat.header()
            + b"Proxy-Connection: keep-alive\r\nConnection: keep-alive\r\n\r\n")
    answer = await rig.talk(head)
    assert status(answer) == 200 and b"SERVER-public /page?q=1" in answer
    assert rig.public.requests == [("GET", "/page?q=1", "shop.test")]


async def test_a_websocket_upgrade_request_is_forwarded_as_an_upgrade(rig):
    rig.answers["shop.test"] = [PUBLIC]
    seen = []

    async def serve(reader, writer):
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    rig.maps[(PUBLIC, 80)] = server.sockets[0].getsockname()[1]
    chat = rig.chat()
    head = (b"GET http://shop.test/socket HTTP/1.1\r\nHost: shop.test\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Key: abc\r\n" + chat.header() + b"\r\n")
    answer = await rig.talk(head)
    server.close()
    assert status(answer) == 101
    assert b"upgrade: websocket" in seen[0].lower() and b"connection: upgrade" in seen[0].lower()
    assert b"proxy-authorization" not in seen[0].lower()


# An upstream proxy (a company proxy such as Zscaler in local proxy mode) -------------------------------------------

class FakeUpstream:
    """A proxy on localhost that records what it receives, answers CONNECT with an established tunnel, and ends the
    tunnel with a canned reply (it terminates what it receives, like a company proxy would for its own policy)."""

    def __init__(self, keep_alive=False):
        self.heads, self.server, self.keep_alive = [], None, keep_alive

    async def start(self):
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def serve(self, reader, writer):
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                self.heads.append(head)
                if head.startswith(b"CONNECT"):
                    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    await writer.drain()
                    await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\n"
                             + (b"" if self.keep_alive else b"Connection: close\r\n") + b"\r\nUPSTREAM")
                await writer.drain()
                if not self.keep_alive:
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()


@pytest.fixture
async def upstream(rig):
    fake = await FakeUpstream().start()
    rig.proxy.upstream = Upstream("127.0.0.1", fake.port)
    yield fake
    await fake.stop()


async def test_with_an_upstream_a_public_name_goes_to_it_by_host_name(rig, upstream):
    rig.answers["shop.test"] = [PUBLIC]
    answer = await rig.connect("shop.test:443", rig.chat())
    assert status(answer) == 200 and b"UPSTREAM" in answer
    assert upstream.heads[0].startswith(b"CONNECT shop.test:443 HTTP/1.1\r\n")
    assert b"proxy-authorization" not in upstream.heads[0].lower() and rig.dialed == [("127.0.0.1", upstream.port)]
    assert rig.public.requests == []


async def test_with_an_upstream_a_plain_request_goes_as_an_absolute_uri(rig, upstream):
    rig.answers["shop.test"] = [PUBLIC]
    answer = await rig.get("shop.test:80", rig.chat(), "/a?b=c")
    assert b"UPSTREAM" in answer
    assert upstream.heads[0].startswith(b"GET http://shop.test/a?b=c HTTP/1.1\r\n")
    assert b"host: shop.test" in upstream.heads[0].lower()
    assert b"proxy-authorization" not in upstream.heads[0].lower()


@pytest.mark.parametrize("name, answers", [("intranet.test", [PRIVATE]), ("mixed.test", [PUBLIC, PRIVATE]),
                                           ("loop.test", ["127.0.0.1"]), ("v6.test", ["::1"])])
@pytest.mark.parametrize("method", ["connect", "get"])
async def test_with_an_upstream_a_private_target_is_refused_before_the_upstream_sees_anything(rig, upstream, name, answers,
                                                                                               method):
    rig.answers[name] = answers
    chat = rig.chat(f"127.0.0.1:{rig.loop.port}")
    answer = await (rig.connect(f"{name}:443", chat) if method == "connect" else rig.get(f"{name}:80", chat))
    assert status(answer) == 403 and upstream.heads == []
    for literal in ("10.1.2.3:443", "[fd00::1]:443"):
        assert status(await rig.connect(literal, chat)) in (400, 403)
    assert upstream.heads == []


async def test_a_loopback_server_that_is_allowed_is_never_sent_to_the_upstream(rig, upstream):
    chat = rig.chat(f"127.0.0.1:{rig.loop.port}")
    answer = await rig.get(f"127.0.0.1:{rig.loop.port}", chat)
    assert b"SERVER-loop" in answer and upstream.heads == []


async def test_a_page_cannot_reach_the_upstream_port_even_when_that_key_is_allowed(rig, upstream):
    chat = rig.chat(f"127.0.0.1:{upstream.port}", f"localhost:{upstream.port}")
    for target in (f"127.0.0.1:{upstream.port}", f"localhost:{upstream.port}"):
        assert status(await rig.connect(target, chat)) == 403
        assert status(await rig.get(target, chat)) == 403
    assert upstream.heads == []


async def test_a_bypass_entry_goes_direct_for_a_public_host_and_never_skips_the_check(rig, upstream):
    rig.proxy.bypass = lambda host: True
    rig.answers["direct.test"] = [PUBLIC]
    rig.answers["sneaky.test"] = [PRIVATE]
    chat = rig.chat()
    answer = await rig.connect("direct.test:80", chat)
    assert status(answer) == 200 and b"SERVER-public" in answer and upstream.heads == []
    assert status(await rig.connect("sneaky.test:80", chat)) == 403
    assert status(await rig.connect("10.1.2.3:80", chat)) in (400, 403)
    assert rig.private.requests == [] and upstream.heads == []


async def test_an_upstream_that_is_down_is_a_bad_gateway(rig):
    rig.proxy.upstream = Upstream("127.0.0.1", 1)
    rig.answers["shop.test"] = [PUBLIC]
    assert status(await rig.connect("shop.test:443", rig.chat())) == 502


async def test_upstream_credentials_are_sent_to_the_upstream_and_never_logged_or_shown(rig, upstream, caplog):
    caplog.set_level(logging.DEBUG)
    rig.proxy.upstream = Upstream("127.0.0.1", upstream.port, "agent", SECRET)
    rig.answers["shop.test"] = [PUBLIC]
    chat = rig.chat()
    answer = await rig.connect("shop.test:443", chat)
    assert b"UPSTREAM" in answer
    expected = base64.b64encode(f"agent:{SECRET}".encode())
    assert b"Proxy-Authorization: Basic " + expected in upstream.heads[0]
    assert SECRET not in caplog.text and SECRET not in repr(list(rig.proxy.log)) and SECRET not in answer.decode()
    assert SECRET not in repr(rig.proxy.upstream) and SECRET not in str(rig.proxy.upstream)
    assert "<redacted>" in repr(rig.proxy.upstream)


# A SOCKS5 upstream (RFC 1928 and RFC 1929) --------------------------------------------------------------------------

class FakeSocks:
    """A SOCKS5 server on localhost. It records the greeting, the sign-in and the CONNECT request (address type, host and
    port), then plays the target: it reads one request head and answers it with a canned reply."""

    def __init__(self, users=None, reply=0):
        self.users, self.reply = users, reply
        self.greetings, self.logins, self.requests, self.heads = [], [], [], []

    async def start(self):
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def serve(self, reader, writer):
        try:
            version, count = await reader.readexactly(2)
            methods = list(await reader.readexactly(count))
            self.greetings.append((version, methods))
            wanted = 2 if self.users is not None else 0
            if wanted not in methods:
                writer.write(b"\x05\xff")
                return
            writer.write(bytes([5, wanted]))
            if wanted == 2:
                await reader.readexactly(1)
                user = await reader.readexactly((await reader.readexactly(1))[0])
                password = await reader.readexactly((await reader.readexactly(1))[0])
                self.logins.append((user.decode(), password.decode()))
                ok = self.users.get(user.decode()) == password.decode()
                writer.write(bytes([1, 0 if ok else 1]))
                if not ok:
                    return
            version, command, _, kind = await reader.readexactly(4)
            if kind == 3:
                host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
            else:
                host = (await reader.readexactly(4 if kind == 1 else 16)).hex()
            port = int.from_bytes(await reader.readexactly(2), "big")
            self.requests.append((command, kind, host, port))
            writer.write(bytes([5, self.reply, 0, 1, 0, 0, 0, 0, 0, 0]))
            await writer.drain()
            if self.reply:
                return
            self.heads.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\nConnection: close\r\n\r\nSOCKSUP")
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()


@pytest.fixture
async def socks(rig):
    fake = await FakeSocks().start()
    rig.proxy.upstream = Upstream("127.0.0.1", fake.port, kind="socks5")
    yield fake
    await fake.stop()


async def test_with_a_socks_upstream_a_public_name_goes_to_it_by_host_name(rig, socks):
    rig.answers["shop.test"] = [PUBLIC]
    answer = await rig.connect("shop.test:443", rig.chat())
    assert status(answer) == 200 and b"SOCKSUP" in answer
    assert socks.greetings == [(5, [0])] and socks.requests == [(1, 3, "shop.test", 443)]
    assert rig.dialed == [("127.0.0.1", socks.port)] and rig.public.requests == []


async def test_with_a_socks_upstream_a_plain_request_is_tunnelled_and_sent_in_origin_form(rig, socks):
    rig.answers["shop.test"] = [PUBLIC]
    answer = await rig.get("shop.test:8080", rig.chat(), "/a?b=c")
    assert b"SOCKSUP" in answer and socks.requests == [(1, 3, "shop.test", 8080)]
    head = socks.heads[0].lower()
    assert socks.heads[0].startswith(b"GET /a?b=c HTTP/1.1\r\n") and b"host: shop.test:8080" in head
    assert b"connection: close" in head and b"proxy-authorization" not in head and b"proxy-connection" not in head


async def test_with_a_socks_upstream_a_second_request_on_the_connection_is_never_forwarded(rig, socks):
    rig.answers["shop.test"] = [PUBLIC]
    await rig.talk(b"GET http://shop.test/one HTTP/1.1\r\nHost: shop.test\r\n" + rig.chat().header()
                   + b"\r\nGET http://evil.test/two HTTP/1.1\r\nHost: evil.test\r\n\r\n")
    assert len(socks.requests) == 1 and len(socks.heads) == 1 and b"/two" not in socks.heads[0]


async def test_a_socks_upstream_gets_an_address_literal_as_an_address(rig, socks):
    await rig.connect("93.184.216.34:443", rig.chat())
    assert socks.requests == [(1, 1, "5db8d822", 443)]


@pytest.mark.parametrize("name, answers", [("intranet.test", [PRIVATE]), ("mixed.test", [PUBLIC, PRIVATE]),
                                           ("loop.test", ["127.0.0.1"]), ("v6.test", ["::1"])])
@pytest.mark.parametrize("method", ["connect", "get"])
async def test_with_a_socks_upstream_a_private_target_is_refused_before_the_socks_server_sees_anything(
        rig, socks, name, answers, method):
    rig.answers[name] = answers
    chat = rig.chat(f"127.0.0.1:{rig.loop.port}")
    answer = await (rig.connect(f"{name}:443", chat) if method == "connect" else rig.get(f"{name}:80", chat))
    assert status(answer) == 403
    for literal in ("10.1.2.3:443", "[fd00::1]:443"):
        assert status(await rig.connect(literal, chat)) in (400, 403)
    assert socks.greetings == [] and socks.requests == [] and rig.dialed == []


async def test_a_loopback_server_that_is_allowed_is_never_sent_to_the_socks_upstream(rig, socks):
    chat = rig.chat(f"127.0.0.1:{rig.loop.port}")
    answer = await rig.get(f"127.0.0.1:{rig.loop.port}", chat)
    assert b"SERVER-loop" in answer and socks.greetings == []


async def test_a_page_cannot_reach_the_socks_port_even_when_that_key_is_allowed(rig, socks):
    chat = rig.chat(f"127.0.0.1:{socks.port}", f"localhost:{socks.port}")
    for target in (f"127.0.0.1:{socks.port}", f"localhost:{socks.port}"):
        assert status(await rig.connect(target, chat)) == 403
        assert status(await rig.get(target, chat)) == 403
    assert socks.greetings == []


async def test_a_bypass_entry_goes_direct_past_the_socks_upstream_and_never_skips_the_check(rig, socks):
    rig.proxy.bypass = lambda host: True
    rig.answers["direct.test"], rig.answers["sneaky.test"] = [PUBLIC], [PRIVATE]
    chat = rig.chat()
    answer = await rig.connect("direct.test:80", chat)
    assert status(answer) == 200 and b"SERVER-public" in answer and socks.greetings == []
    assert status(await rig.connect("sneaky.test:80", chat)) == 403 and rig.private.requests == []


async def test_a_socks_upstream_that_signs_in_gets_the_user_name_and_password_and_never_shows_them(rig, caplog):
    caplog.set_level(logging.DEBUG)
    fake = await FakeSocks(users={"agent": SECRET}).start()
    try:
        rig.proxy.upstream = Upstream("127.0.0.1", fake.port, "agent", SECRET, kind="socks5")
        rig.answers["shop.test"] = [PUBLIC]
        answer = await rig.connect("shop.test:443", rig.chat())
        assert b"SOCKSUP" in answer and fake.logins == [("agent", SECRET)] and fake.greetings == [(5, [0, 2])]
        assert fake.requests == [(1, 3, "shop.test", 443)]
        assert SECRET not in caplog.text and SECRET not in repr(list(rig.proxy.log)) and SECRET not in answer.decode()
        assert SECRET not in repr(rig.proxy.upstream) and SECRET not in str(rig.proxy.upstream)
        assert "<redacted>" in repr(rig.proxy.upstream)
    finally:
        await fake.stop()


async def test_a_socks_upstream_that_refuses_the_password_is_a_bad_gateway_and_nothing_is_sent_on(rig, caplog):
    caplog.set_level(logging.DEBUG)
    fake = await FakeSocks(users={"agent": SECRET}).start()
    try:
        rig.proxy.upstream = Upstream("127.0.0.1", fake.port, "agent", "wrong-" + SECRET, kind="socks5")
        rig.answers["shop.test"] = [PUBLIC]
        answer = await rig.connect("shop.test:443", rig.chat())
        assert status(answer) == 502 and b"user name or password" in answer
        assert fake.requests == [] and fake.heads == []
        assert SECRET not in answer.decode() and SECRET not in caplog.text
    finally:
        await fake.stop()


async def test_a_socks_upstream_that_needs_a_sign_in_nobody_set_is_a_bad_gateway(rig):
    fake = await FakeSocks(users={"agent": SECRET}).start()
    try:
        rig.proxy.upstream = Upstream("127.0.0.1", fake.port, kind="socks5")
        rig.answers["shop.test"] = [PUBLIC]
        answer = await rig.connect("shop.test:443", rig.chat())
        assert status(answer) == 502 and b"sign-in" in answer and fake.requests == []
    finally:
        await fake.stop()


@pytest.mark.parametrize("code", [1, 2, 4, 5, 7])
async def test_a_socks_upstream_that_cannot_connect_is_a_bad_gateway_with_its_reason(rig, code):
    fake = await FakeSocks(reply=code).start()
    try:
        rig.proxy.upstream = Upstream("127.0.0.1", fake.port, kind="socks5")
        rig.answers["shop.test"] = [PUBLIC]
        answer = await rig.connect("shop.test:443", rig.chat())
        assert status(answer) == 502 and f"code {code}".encode() in answer and fake.heads == []
    finally:
        await fake.stop()


async def test_a_socks_upstream_that_is_down_is_a_bad_gateway(rig):
    rig.proxy.upstream = Upstream("127.0.0.1", 1, kind="socks5")
    rig.answers["shop.test"] = [PUBLIC]
    assert status(await rig.connect("shop.test:443", rig.chat())) == 502


async def test_a_server_that_does_not_speak_socks_is_a_bad_gateway(rig):
    async def talk(reader, writer):
        writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(talk, "127.0.0.1", 0)
    try:
        rig.proxy.upstream = Upstream("127.0.0.1", server.sockets[0].getsockname()[1], kind="socks5")
        rig.answers["shop.test"] = [PUBLIC]
        assert status(await rig.connect("shop.test:443", rig.chat())) == 502
    finally:
        server.close()
        await server.wait_closed()


# Choosing the upstream --------------------------------------------------------------------------------------------

def test_none_never_asks_the_system(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: pytest.fail("the system was asked"))
    choice = resolve_upstream("none")
    assert choice.mode == "direct" and choice.upstream is None and choice.problem is None


def test_auto_reads_the_system_proxy(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies",
                        lambda: {"http": "http://127.0.0.1:9000", "https": "http://127.0.0.1:9000"})
    monkeypatch.setattr(browser_proxy, "pac_in_use", lambda: False)
    choice = resolve_upstream("auto")
    assert choice.mode == "system proxy 127.0.0.1:9000"
    assert (choice.upstream.host, choice.upstream.port, choice.upstream.username) == ("127.0.0.1", 9000, None)


def test_auto_takes_the_https_entry_and_accepts_an_address_without_a_scheme(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "web.test:3128", "https": "secure.test:8443"})
    monkeypatch.setattr(browser_proxy, "pac_in_use", lambda: False)
    assert resolve_upstream("auto").mode == "system proxy secure.test:8443"


def test_auto_without_a_system_proxy_is_direct(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(browser_proxy, "pac_in_use", lambda: False)
    choice = resolve_upstream("auto")
    assert choice.mode == "direct" and choice.upstream is None and choice.problem is None


def test_auto_with_a_pac_file_only_says_to_enter_the_address(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(browser_proxy, "pac_in_use", lambda: True)
    choice = resolve_upstream("auto")
    assert choice.mode == "PAC file found: enter the proxy address" and choice.upstream is None
    assert "Settings → Browsing" in choice.problem and "proxy address" in choice.problem


def test_a_system_proxy_wins_over_a_pac_file_and_system_credentials_are_not_used(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": f"http://someone:{SECRET}@proxy.test:8080"})
    monkeypatch.setattr(browser_proxy, "pac_in_use", lambda: True)
    choice = resolve_upstream("auto")
    assert choice.mode == "system proxy proxy.test:8080" and choice.problem is None
    assert choice.upstream.username is None and choice.upstream.password is None
    assert SECRET not in choice.mode


def test_an_explicit_address_is_used_and_its_credentials_stay_out_of_every_text(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: pytest.fail("the system was asked"))
    choice = resolve_upstream(f"http://agent:{SECRET}@proxy.test:8080")
    assert choice.mode == "configured proxy proxy.test:8080"
    assert (choice.upstream.username, choice.upstream.password) == ("agent", SECRET)
    for text in (choice.mode, repr(choice), str(choice.upstream), repr(choice.upstream), choice.problem or ""):
        assert SECRET not in text
    assert redact_url(f"http://agent:{SECRET}@proxy.test:8080") == "http://agent:<redacted>@proxy.test:8080"
    assert redact_url("http://proxy.test:8080") == "http://proxy.test:8080"
    assert redact_url("auto") == "auto"


def test_a_configured_socks_address_is_used_and_its_password_stays_out_of_every_text(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: pytest.fail("the system was asked"))
    choice = resolve_upstream(f"socks5://agent:{SECRET}@proxy.test:1080")
    assert choice.mode == "configured SOCKS proxy proxy.test:1080" and choice.upstream.kind == "socks5"
    assert (choice.upstream.username, choice.upstream.password) == ("agent", SECRET)
    for text in (choice.mode, repr(choice), str(choice.upstream), repr(choice.upstream), choice.problem or ""):
        assert SECRET not in text
    assert redact_url(f"socks5://agent:{SECRET}@proxy.test:1080") == "socks5://agent:<redacted>@proxy.test:1080"
    assert resolve_upstream("socks5://proxy.test:1080").upstream.username is None


def test_auto_uses_a_system_socks_proxy_only_when_no_http_proxy_is_set(monkeypatch):
    monkeypatch.setattr(browser_proxy, "pac_in_use", lambda: False)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"socks": "socks5://127.0.0.1:1080"})
    choice = resolve_upstream("auto")
    assert choice.mode == "system SOCKS proxy 127.0.0.1:1080" and choice.upstream.kind == "socks5"
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"socks": "127.0.0.1:1080", "http": "http://web.test:3128"})
    choice = resolve_upstream("auto")
    assert choice.mode == "system proxy web.test:3128" and choice.upstream.kind == "http"
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"socks": "socks5h://127.0.0.1:1080"})
    assert resolve_upstream("auto").mode == "system SOCKS proxy 127.0.0.1:1080"


def test_auto_reads_the_macos_socks_setting_and_never_uses_its_credentials(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(browser_proxy, "pac_in_use", lambda: True)
    monkeypatch.setattr(browser_proxy, "macos_socks_proxy", lambda: "127.0.0.1:1081")
    choice = resolve_upstream("auto")
    assert choice.mode == "system SOCKS proxy 127.0.0.1:1081" and choice.upstream.kind == "socks5"
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": "http://web.test:3128"})
    assert resolve_upstream("auto").mode == "system proxy web.test:3128"
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"socks": f"socks5://someone:{SECRET}@127.0.0.1:1080"})
    choice = resolve_upstream("auto")
    assert choice.upstream.username is None and SECRET not in repr(choice)


def test_the_macos_proxy_listing_gives_the_socks_host_and_port_only_when_it_is_on():
    on = "<dictionary> {\n  HTTPEnable : 0\n  SOCKSEnable : 1\n  SOCKSPort : 1080\n  SOCKSProxy : 127.0.0.1\n}\n"
    assert browser_proxy.parse_macos_socks(on) == "127.0.0.1:1080"
    assert browser_proxy.parse_macos_socks(on.replace("SOCKSEnable : 1", "SOCKSEnable : 0")) is None
    assert browser_proxy.parse_macos_socks("<dictionary> {\n  HTTPEnable : 0\n}\n") is None
    assert browser_proxy.parse_macos_socks("SOCKSEnable : 1\nSOCKSProxy : bad host\nSOCKSPort : 1080\n") is None


@pytest.mark.parametrize("found", ["socks4://127.0.0.1:1080", "socks4a://127.0.0.1:1080"])
def test_a_system_socks4_proxy_is_reported_as_not_supported(monkeypatch, found):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"socks": found})
    monkeypatch.setattr(browser_proxy, "pac_in_use", lambda: False)
    choice = resolve_upstream("auto")
    assert choice.upstream is None and "SOCKS4" in choice.mode and "not supported" in choice.mode
    assert "SOCKS5" in choice.problem
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"socks": found, "http": "http://web.test:3128"})
    assert resolve_upstream("auto").mode == "system proxy web.test:3128"


@pytest.mark.parametrize("value", ["auto", "none", "http://proxy.test:8080", "http://u:p@proxy.test:8080",
                                   "http://127.0.0.1:9000", "http://[::1]:9000", "http://proxy.test:8080/", "socks5://proxy.test:1080",
                                   "socks5://u:p@proxy.test:1080", "socks5://[::1]:1080"])
def test_valid_settings_pass(value):
    assert check_upstream_setting(value) == value.rstrip("/")


@pytest.mark.parametrize("value", ["", "AUTO ", "ftp://proxy.test:21", "https://proxy.test:443", "socks4://proxy.test:1080",
                                   "http://", "proxy.test:8080", "http://proxy.test", "http://proxy.test:0", "http://proxy.test:99999",
                                   "http://proxy.test:8080/path", "http://proxy.test:8080?x=1", "http://u@proxy.test:8080",
                                   "http://bad host:80", 7, None, "x" * 400, "socks5://proxy.test", "socks5://u@proxy.test:1080",
                                   "socks5://u:@proxy.test:1080", "socks5://" + "u" * 256 + ":p@proxy.test:1080",
                                   "socks5://u:" + "p" * 256 + "@proxy.test:1080", "socks5://proxy.test:1080/x"])
def test_other_settings_are_refused(value):
    with pytest.raises(ValueError):
        check_upstream_setting(value)


# Settings and the status line ---------------------------------------------------------------------------------------

def make_settings(tmp_path, monkeypatch):
    from local_agent.config import Settings
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    return Settings(tmp_path / "state")


def test_the_setting_defaults_to_auto_and_is_checked(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    assert settings.values["browser_upstream_proxy"] == "auto"
    settings.values.update(workspace=str(tmp_path), env_file="")
    with pytest.raises(ValueError, match="proxy"):
        settings.update({"browser_upstream_proxy": "socks4://proxy.test:1080"})
    settings.update({"browser_upstream_proxy": "none"})
    assert settings.values["browser_upstream_proxy"] == "none"


def test_a_hand_edited_bad_value_falls_back_to_auto(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "settings.json").write_text(json.dumps({"browser_upstream_proxy": "ftp://nope:1"}))
    from local_agent.config import Settings
    assert Settings(tmp_path / "state").values["browser_upstream_proxy"] == "auto"


def test_the_public_settings_redact_the_password_and_a_save_keeps_the_stored_one(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.update({"browser_upstream_proxy": f"http://agent:{SECRET}@proxy.test:8080"})
    assert settings.values["browser_upstream_proxy"] == f"http://agent:{SECRET}@proxy.test:8080"
    shown = settings.public()["browser_upstream_proxy"]
    assert shown == "http://agent:<redacted>@proxy.test:8080" and SECRET not in repr(settings.public())
    settings.update({"browser_upstream_proxy": shown})  # what the settings dialog sends back
    assert settings.values["browser_upstream_proxy"] == f"http://agent:{SECRET}@proxy.test:8080"
    with pytest.raises(ValueError, match="password"):
        settings.update({"browser_upstream_proxy": "http://agent:<redacted>@other.test:8080"})
    settings.update({"browser_upstream_proxy": "http://agent:new-secret@proxy.test:8080"})
    assert settings.values["browser_upstream_proxy"].endswith("new-secret@proxy.test:8080")


def test_the_browser_status_names_the_connection_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": "http://127.0.0.1:9000"})
    monkeypatch.setattr(browser_proxy, "pac_in_use", lambda: False)
    settings = make_settings(tmp_path, monkeypatch)
    settings.values["browser_tools"] = False
    assert settings.browser_status() == {"ready": False, "problem": "Browsing is off. Turn it on in Settings → Browsing."}
    settings.values["browser_tools"] = True
    assert settings.browser_status()["proxy"] == "system proxy 127.0.0.1:9000"
    settings.values["browser_upstream_proxy"] = "none"
    assert settings.browser_status()["proxy"] == "direct"
    settings.values["browser_upstream_proxy"] = f"http://agent:{SECRET}@proxy.test:8080"
    shown = settings.browser_status()
    assert shown["proxy"] == "configured proxy proxy.test:8080" and SECRET not in repr(shown)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(browser_proxy, "pac_in_use", lambda: True)
    settings.values["browser_upstream_proxy"] = "auto"
    pac = settings.browser_status()
    assert pac["proxy"] == "PAC file found: enter the proxy address" and "Settings → Browsing" in pac["proxy_problem"]


def test_the_browser_status_names_a_socks_proxy_and_reports_socks4_as_not_supported(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_proxy, "pac_in_use", lambda: False)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"socks": "socks5://127.0.0.1:1080"})
    settings = make_settings(tmp_path, monkeypatch)
    settings.values["browser_tools"] = True
    assert settings.browser_status()["proxy"] == "system SOCKS proxy 127.0.0.1:1080"
    settings.values["browser_upstream_proxy"] = f"socks5://agent:{SECRET}@proxy.test:1080"
    shown = settings.browser_status()
    assert shown["proxy"] == "configured SOCKS proxy proxy.test:1080" and SECRET not in repr(shown)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"socks": "socks4://127.0.0.1:1080"})
    settings.values["browser_upstream_proxy"] = "auto"
    old = settings.browser_status()
    assert "SOCKS4" in old["proxy"] and "not supported" in old["proxy"] and "SOCKS5" in old["proxy_problem"]


def test_a_socks_password_is_redacted_in_the_public_settings_and_a_save_keeps_the_stored_one(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.update({"browser_upstream_proxy": f"socks5://agent:{SECRET}@proxy.test:1080"})
    shown = settings.public()["browser_upstream_proxy"]
    assert shown == "socks5://agent:<redacted>@proxy.test:1080" and SECRET not in repr(settings.public())
    settings.update({"browser_upstream_proxy": shown})
    assert settings.values["browser_upstream_proxy"] == f"socks5://agent:{SECRET}@proxy.test:1080"
    with pytest.raises(ValueError, match="password"):
        settings.update({"browser_upstream_proxy": "socks5://agent:<redacted>@other.test:1080"})
    with pytest.raises(ValueError, match="SOCKS4"):
        settings.update({"browser_upstream_proxy": "socks4://proxy.test:1080"})


def test_the_api_accepts_the_setting_and_never_returns_the_password(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from local_agent.api import create_app
    settings = make_settings(tmp_path, monkeypatch)
    settings.values.update(workspace=str(tmp_path), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "fake-token")
    with TestClient(create_app(settings)) as client:
        bootstrap = client.get("/api/bootstrap").json()
        assert bootstrap["settings"]["browser_upstream_proxy"] == "auto"
        headers = {"X-Local-Token": bootstrap["token"]}
        current = {key: bootstrap["settings"][key] for key in ("workspace", "model", "env_file")}
        done = client.put("/api/settings", headers=headers,
                          json={**current, "browser_upstream_proxy": f"http://agent:{SECRET}@proxy.test:8080"})
        assert done.status_code == 200 and SECRET not in done.text
        assert done.json()["browser_upstream_proxy"] == "http://agent:<redacted>@proxy.test:8080"
        assert SECRET not in client.get("/api/bootstrap").text
        bad = client.put("/api/settings", headers=headers, json={**current, "browser_upstream_proxy": "ftp://x:1"})
        assert bad.status_code in (400, 422)
        wrong = client.put("/api/settings", headers=headers, json={**current, "browser_upstream_proxy": 5})
        assert wrong.status_code == 422


# Limits and clean shutdown ----------------------------------------------------------------------------------------

def quiet(proxy):
    assert proxy.open_connections == 0 and not proxy._tasks, (proxy.open_connections, proxy._tasks)


async def settle(proxy, seconds=2.0):
    for _ in range(int(seconds / 0.02)):
        if proxy.open_connections == 0 and not proxy._tasks:
            return
        await asyncio.sleep(0.02)


async def test_oversize_headers_are_refused_and_nothing_stays_open():
    proxy = FilteringProxy(max_head=2_048)
    await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(b"GET http://shop.test/ HTTP/1.1\r\n" + b"X-Fill: " + b"a" * 5_000 + b"\r\n\r\n")
        data = await asyncio.wait_for(reader.read(65536), 5)
        assert status(data) == 431
        assert await asyncio.wait_for(reader.read(65536), 5) == b""
        writer.close()
        await settle(proxy)
        quiet(proxy)
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(b"GET http://shop.test/ HTTP/1.1\r\n" + b"X-Fill: a\r\n" * 300 + b"\r\n")  # many small headers
        assert status(await asyncio.wait_for(reader.read(65536), 5)) == 431
        writer.close()
    finally:
        await proxy.stop()
    quiet(proxy)


async def test_a_slow_client_is_closed_and_leaves_no_task():
    proxy = FilteringProxy(head_timeout=0.3)
    await proxy.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(b"CONNECT shop.test:443 HT")  # the rest never comes
        data = await asyncio.wait_for(reader.read(65536), 3)
        assert status(data) == 408 or data == b""
        assert await asyncio.wait_for(reader.read(65536), 3) == b""
        writer.close()
        await settle(proxy)
        quiet(proxy)
    finally:
        await proxy.stop()


async def test_an_idle_tunnel_is_closed_and_leaves_no_task(rig):
    rig.proxy.idle_timeout = 0.3
    chat = rig.chat(f"127.0.0.1:{rig.loop.port}")
    reader, writer = await asyncio.open_connection("127.0.0.1", rig.proxy.port)
    writer.write(f"CONNECT 127.0.0.1:{rig.loop.port} HTTP/1.1\r\n".encode() + chat.header() + b"\r\n")
    assert status(await asyncio.wait_for(reader.read(65536), 3)) == 200
    assert await asyncio.wait_for(reader.read(65536), 3) == b""  # closed by the proxy after the idle time
    writer.close()
    await settle(rig.proxy)
    quiet(rig.proxy)


async def test_the_number_of_connections_of_one_session_is_capped(rig):
    rig.proxy.max_per_session = 2
    chat, other = rig.chat(f"127.0.0.1:{rig.loop.port}"), rig.chat(f"127.0.0.1:{rig.loop.port}")
    open_tunnels = []
    for _ in range(2):
        reader, writer = await asyncio.open_connection("127.0.0.1", rig.proxy.port)
        writer.write(f"CONNECT 127.0.0.1:{rig.loop.port} HTTP/1.1\r\n".encode() + chat.header() + b"\r\n")
        assert status(await asyncio.wait_for(reader.read(65536), 3)) == 200
        open_tunnels.append((reader, writer))
    assert status(await rig.connect(f"127.0.0.1:{rig.loop.port}", chat)) == 503
    assert status(await rig.connect(f"127.0.0.1:{rig.loop.port}", other)) == 200  # another chat is not held back
    open_tunnels.pop()[1].close()
    await asyncio.sleep(0.2)
    assert status(await rig.connect(f"127.0.0.1:{rig.loop.port}", chat)) == 200
    for _, writer in open_tunnels:
        writer.close()
    await settle(rig.proxy)
    quiet(rig.proxy)


async def test_stop_closes_open_tunnels_and_leaves_no_task(rig):
    chat = rig.chat(f"127.0.0.1:{rig.loop.port}")
    before = len(asyncio.all_tasks())
    tunnels = []
    for _ in range(3):
        reader, writer = await asyncio.open_connection("127.0.0.1", rig.proxy.port)
        writer.write(f"CONNECT 127.0.0.1:{rig.loop.port} HTTP/1.1\r\n".encode() + chat.header() + b"\r\n")
        assert status(await asyncio.wait_for(reader.read(65536), 3)) == 200
        tunnels.append((reader, writer))
    port = rig.proxy.port
    await rig.proxy.stop()
    for reader, writer in tunnels:
        assert await asyncio.wait_for(reader.read(65536), 3) == b""
        writer.close()
    quiet(rig.proxy)
    assert len(asyncio.all_tasks()) <= before
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
    assert rig.proxy.port is None
    await rig.proxy.stop()  # safe to repeat


async def test_the_client_hanging_up_closes_the_server_side(rig):
    chat = rig.chat(f"127.0.0.1:{rig.loop.port}")
    reader, writer = await asyncio.open_connection("127.0.0.1", rig.proxy.port)
    writer.write(f"CONNECT 127.0.0.1:{rig.loop.port} HTTP/1.1\r\n".encode() + chat.header() + b"\r\n")
    assert status(await asyncio.wait_for(reader.read(65536), 3)) == 200
    writer.close()
    await settle(rig.proxy)
    quiet(rig.proxy)


async def test_a_proxy_can_start_again_after_it_stopped():
    proxy = FilteringProxy()
    first = await proxy.start()
    assert await proxy.start() == first  # starting twice is one start
    await proxy.stop()
    second = await proxy.start()
    assert second == proxy.port
    await proxy.stop()


# Chromium ---------------------------------------------------------------------------------------------------------

def test_the_launch_flags_close_the_other_ways_out_of_the_proxy():
    flags = " ".join(LAUNCH_FLAGS)
    assert "--disable-quic" in LAUNCH_FLAGS
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in LAUNCH_FLAGS
    assert "--blink-settings=dnsPrefetchingEnabled=false" in LAUNCH_FLAGS
    assert any(flag.startswith("--auth-server-allowlist=") and flag != "--auth-server-allowlist=" for flag in LAUNCH_FLAGS)
    assert any(flag.startswith("--auth-negotiate-delegate-allowlist=") for flag in LAUNCH_FLAGS)
    assert not any(word in flags for word in ("ignore-certificate", "ssl-key-log", "allow-insecure", "no-proxy-server",
                                              "proxy-bypass-list=<local>"))


async def test_chromium_goes_through_the_proxy_with_its_own_credentials_and_the_flags_are_set(tmp_path, rig):
    require_browser()
    rig.loop.pages["/"] = "<html><title>dev</title><body>dev server</body></html>"
    key = f"127.0.0.1:{rig.loop.port}"
    session = BrowserSession(tmp_path / "profile", headless=True, allowed=lambda found: found == key, proxy=rig.proxy)
    async with session:
        result = await session.act("open", url=f"http://{key}/")
        assert "dev server" in result.text and result.refused is None
        line = " ".join(browser_processes(str(tmp_path / "profile")))
        assert f"--proxy-server=http://127.0.0.1:{rig.proxy.port}" in line
        for flag in ("--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "<-loopback>",
                     "--auth-server-allowlist="):
            assert flag in line, flag
        assert all(entry.secret.decode() not in line for entry in rig.proxy._sessions.values())  # not in `ps`
        assert any(entry[1:3] == ("127.0.0.1", rig.loop.port) and entry[3] == "allowed" for entry in rig.proxy.log), \
            list(rig.proxy.log)
    assert rig.loop.requests and all(path == "/" or path == "/favicon.ico" for _, path, _ in rig.loop.requests)


WEBRTC_JS = """async () => {
  const pc = new RTCPeerConnection({iceServers: [{urls: 'stun:127.0.0.1:9'}]});
  pc.createDataChannel('x');
  const found = [];
  pc.onicecandidate = event => { if (event.candidate) found.push(event.candidate.candidate); };
  await pc.setLocalDescription(await pc.createOffer());
  await new Promise(done => setTimeout(done, 1500));
  return found;
}"""


async def test_webrtc_finds_no_network_address_to_send_over_udp(tmp_path, rig):
    require_browser()
    key = f"127.0.0.1:{rig.loop.port}"
    rig.loop.pages["/"] = "<html><title>rtc</title><body>rtc</body></html>"
    session = BrowserSession(tmp_path / "profile", headless=True, allowed=lambda found: found == key, proxy=rig.proxy)
    async with session:
        await session.act("open", url=f"http://{key}/")
        page = next(iter(session._tabs.values())).page
        assert await page.evaluate(WEBRTC_JS) == []


async def test_chromium_cannot_use_another_ways_out_with_the_proxy_on(tmp_path, rig):
    """A page that fetches a name and an address that is refused, and one with a DNS prefetch and a preconnect link,
    reaches the proxy only: nothing is dialled except through its checks."""
    require_browser()
    key = f"127.0.0.1:{rig.loop.port}"
    rig.answers["cdn.test"] = [PRIVATE]
    rig.loop.pages["/"] = ("<html><title>links</title><head><link rel=dns-prefetch href='//cdn.test'>"
                           "<link rel=preconnect href='http://cdn.test'><link rel=prefetch href='http://cdn.test/a.js'></head>"
                           "<body><img src='http://cdn.test/x.png'>links page</body></html>")
    session = BrowserSession(tmp_path / "profile", headless=True, allowed=lambda found: found == key, proxy=rig.proxy,
                             resolver=lambda host: [PUBLIC])
    async with session:
        result = await session.act("open", url=f"http://{key}/")
        assert "links page" in result.text
        await asyncio.sleep(0.5)
    assert "cdn.test" in rig.asked  # the browser handed the name to the proxy and did not resolve it
    assert rig.private.requests == [] and (PRIVATE, 80) not in rig.dialed


async def test_a_negotiate_or_ntlm_challenge_is_never_answered_with_the_users_sign_in(tmp_path, rig):
    """No positive control is possible here (no Kerberos ticket, no Windows), so this guards the flag's effect only."""
    require_browser()
    key = f"127.0.0.1:{rig.loop.port}"
    rig.loop.pages["/"] = (401, [("WWW-Authenticate", "Negotiate"), ("WWW-Authenticate", "NTLM")], "sign in")
    session = BrowserSession(tmp_path / "profile", headless=True, allowed=lambda found: found == key, proxy=rig.proxy)
    async with session:
        await session.act("open", url=f"http://{key}/")
        await asyncio.sleep(0.5)
    assert rig.loop.headers and not any("authorization" in headers for headers in rig.loop.headers)


WS_PAGE = """<html><title>dev</title><body><p id=o>waiting</p><script>
let api = 'no-api', ws = 'no-ws';
const show = () => document.getElementById('o').textContent = 'api=' + api + ' ws=' + ws;
fetch('/api').then(r => r.text()).then(t => { api = t; show(); });
const socket = new WebSocket('ws://127.0.0.1:{{WS}}/live');
socket.onmessage = event => { ws = event.data; show(); };
</script></body></html>"""


async def start_websocket_server():
    """A WebSocket server that sends one text frame, "hello-ws", to every client (no extra package)."""
    async def serve(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            key = next(line.split(":", 1)[1].strip() for line in head.split("\r\n") if line.lower().startswith("sec-websocket-key"))
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
            writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n" + b"\x81\x08hello-ws")
            await writer.drain()
            await reader.read()
        except (asyncio.IncompleteReadError, ConnectionError, StopIteration):
            pass
        finally:
            writer.close()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_an_allowed_loopback_dev_server_works_in_chromium_over_http_and_a_websocket(tmp_path, rig):
    require_browser()
    server, ws_port = await start_websocket_server()
    rig.loop.pages["/"] = WS_PAGE.replace("{{WS}}", str(ws_port))
    rig.loop.pages["/api"] = "api-ok"
    keys = {f"127.0.0.1:{rig.loop.port}", f"127.0.0.1:{ws_port}"}
    session = BrowserSession(tmp_path / "profile", headless=True, allowed=lambda found: found in keys, proxy=rig.proxy)
    try:
        async with session:
            await session.act("open", url=f"http://127.0.0.1:{rig.loop.port}/")
            text = ""
            for _ in range(50):
                text = (await session.act("read")).text
                if "api=api-ok ws=hello-ws" in text:
                    break
                await asyncio.sleep(0.1)
            assert "api=api-ok ws=hello-ws" in text, text
        ports = {entry[2] for entry in rig.proxy.log if entry[3] == "allowed"}
        assert {rig.loop.port, ws_port} <= ports  # both went through the proxy
    finally:
        server.close()


async def test_a_name_that_turns_private_for_the_browser_is_blocked_and_reported_by_the_tool(tmp_path, rig):
    require_browser()
    rig.answers["rebind.test"] = lambda call: [PUBLIC] if call == 1 else [PRIVATE]
    rig.public.pages["/"] = "<html><title>public</title><body>PUBLIC-SITE</body></html>"
    rig.private.pages["/"] = "<html><title>private</title><body>PRIVATE-SERVICE</body></html>"
    # The app's own check always sees the public answer, like the first lookup of a rebinding name.
    session = BrowserSession(tmp_path / "profile", headless=True, allowed=lambda key: True, proxy=rig.proxy,
                             resolver=lambda host: [PUBLIC])
    async with session:
        first = await session.act("open", url="http://rebind.test/")
        assert "PUBLIC-SITE" in first.text
        second = await session.act("open", url="http://rebind.test/", new_tab=True)
        assert "PRIVATE-SERVICE" not in second.text
        assert "blocked by the address check: rebind.test resolved to a private address" in second.text
        assert second.refused == "private_address"
    assert rig.private.requests == []


async def test_the_session_names_pass_through_a_host_map_without_the_browser_resolving_them(tmp_path, browser_pages):
    require_browser()
    session = BrowserSession(tmp_path / "profile", headless=True, allowed=lambda key: True,
                             host_map={"shop.test": (PUBLIC, f"127.0.0.1:{browser_pages.port}")})
    async with session:
        result = await session.act("open", url="http://shop.test/index.html")
        assert result.refused is None and "shop.test" in result.text


async def test_the_manager_starts_the_proxy_lazily_and_close_all_stops_it(tmp_path, browser_pages):
    require_browser()
    manager = BrowserManager(tmp_path / "state", headless=True)
    assert manager.proxy.port is None  # nothing listens before the first session
    key = f"127.0.0.1:{browser_pages.port}"
    outcome = await manager.run("chat-a", {"action": "open", "url": browser_pages.url("index.html")},
                                allowed=lambda found: found == key, redact=lambda text: text,
                                store_result=lambda text, note: "r1", store_image=lambda *args: "u1")
    assert outcome.kind == "result", outcome.text
    port = manager.proxy.port
    assert port
    await manager.close_all()
    assert manager.proxy.port is None
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
    await manager.close_all()


async def test_no_chromium_is_left_after_close_all(tmp_path, browser_pages):
    require_browser()
    manager = BrowserManager(tmp_path / "state", headless=True)
    key = f"127.0.0.1:{browser_pages.port}"
    await manager.run("chat-b", {"action": "open", "url": browser_pages.url("index.html")}, allowed=lambda found: found == key,
                      redact=lambda text: text, store_result=lambda text, note: "r1", store_image=lambda *args: "u1")
    assert browser_processes(str(tmp_path / "state"))
    await manager.close_all()
    for _ in range(50):
        if not browser_processes(str(tmp_path / "state")):
            break
        await asyncio.sleep(0.1)
    assert browser_processes(str(tmp_path / "state")) == []


async def test_the_manager_hands_the_resolved_upstream_to_the_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": "http://127.0.0.1:9000"})
    monkeypatch.setattr(browser_proxy, "pac_in_use", lambda: False)
    manager = BrowserManager(tmp_path / "state", headless=True, upstream_setting="auto")
    assert (await manager.apply_upstream()).mode == "system proxy 127.0.0.1:9000"
    assert (manager.proxy.upstream.host, manager.proxy.upstream.port) == ("127.0.0.1", 9000)
    manager.upstream_setting = "none"
    assert (await manager.apply_upstream()).mode == "direct" and manager.proxy.upstream is None
    with contextlib.suppress(Exception):
        await manager.close_all()


# One request for each connection (review fix 1, 4) -----------------------------------------------------------------

def two_requests(second, auth):
    first = b"GET http://shop.test/a HTTP/1.1\r\nHost: shop.test\r\n" + auth + b"Proxy-Connection: keep-alive\r\n\r\n"
    return first + b"GET " + second + b" HTTP/1.1\r\nHost: x\r\n" + auth + b"\r\n"


@pytest.mark.parametrize("second", [b"http://10.1.2.3/secret", b"http://evil.test/a", b"http://shop.test/b"])
async def test_a_second_request_on_a_kept_alive_connection_is_never_forwarded(second):
    rig = Rig()
    rig.public.stop()
    rig.public = Server("public", ignore_close=True)
    rig.maps[(PUBLIC, 80)] = rig.public.port
    await rig.proxy.start()
    try:
        rig.answers.update({"shop.test": [PUBLIC], "evil.test": [PRIVATE]})
        chat = rig.chat()
        answer = await rig.talk(two_requests(second, chat.header()))
        assert answer.count(b"HTTP/1.") == 1 and b"SERVER-public /a" in answer and b"close" in answer.lower()
        assert rig.public.requests == [("GET", "/a", "shop.test")] and rig.private.requests == []
        assert all("proxy-authorization" not in h and "proxy-connection" not in h for h in rig.public.headers)
    finally:
        await rig.proxy.stop()
        rig.stop()


async def test_with_an_upstream_a_second_request_is_never_forwarded_and_only_the_upstreams_credentials_are_sent(rig):
    fake = await FakeUpstream(keep_alive=True).start()
    rig.proxy.upstream = Upstream("127.0.0.1", fake.port, "agent", SECRET)
    try:
        rig.answers.update({"shop.test": [PUBLIC]})
        chat = rig.chat()
        answer = await rig.talk(two_requests(b"http://10.1.2.3/secret", chat.header()))
        assert answer.count(b"HTTP/1.") == 1 and b"UPSTREAM" in answer
        assert len(fake.heads) == 1 and b"10.1.2.3" not in fake.heads[0]
        assert chat.password.encode() not in fake.heads[0] and b"Proxy-Authorization: Basic " + base64.b64encode(
            f"agent:{SECRET}".encode()) in fake.heads[0]
        assert fake.heads[0].lower().count(b"proxy-authorization") == 1 and b"proxy-connection" not in fake.heads[0].lower()
    finally:
        await fake.stop()


async def test_an_upstream_407_is_not_shown_to_the_browser_as_its_own_proxy_challenge(rig):
    async def serve(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="x"\r\n'
                     b"Content-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    rig.proxy.upstream = Upstream("127.0.0.1", server.sockets[0].getsockname()[1])
    rig.answers["shop.test"] = [PUBLIC]
    answer = await rig.get("shop.test:80", rig.chat())
    server.close()
    assert status(answer) == 502 and b"roxy-authenticate" not in answer


async def test_the_request_body_is_forwarded_by_its_declared_length_and_nothing_after_it(rig):
    seen = []

    async def serve(reader, writer):
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        seen.append(await reader.read(65536))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    rig.maps[(PUBLIC, 80)] = server.sockets[0].getsockname()[1]
    rig.answers["shop.test"] = [PUBLIC]
    chat = rig.chat()
    head = b"POST http://shop.test/p HTTP/1.1\r\nHost: shop.test\r\nContent-Length: 5\r\n" + chat.header() + b"\r\n"
    answer = await rig.talk(head + b"12345" + b"GET http://10.1.2.3/secret HTTP/1.1\r\n\r\n")
    server.close()
    assert b"ok" in answer and seen[1] == b"12345"


async def test_a_chunked_request_body_is_forwarded_up_to_its_end(rig):
    seen = []

    async def serve(reader, writer):
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        seen.append(await reader.read(65536))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    rig.maps[(PUBLIC, 80)] = server.sockets[0].getsockname()[1]
    rig.answers["shop.test"] = [PUBLIC]
    chat = rig.chat()
    head = b"POST http://shop.test/p HTTP/1.1\r\nHost: shop.test\r\nTransfer-Encoding: chunked\r\n" + chat.header() + b"\r\n"
    body = b"3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n"
    answer = await rig.talk(head + body + b"GET http://10.1.2.3/secret HTTP/1.1\r\n\r\n")
    server.close()
    assert b"ok" in answer and seen[1] == body


@pytest.mark.parametrize("framing", [b"Content-Length: 3\r\nTransfer-Encoding: chunked\r\n", b"Content-Length: 3\r\nContent-Length: 4\r\n",
                                     b"Content-Length: -1\r\n", b"Content-Length: x\r\n", b"Transfer-Encoding: gzip, chunked\r\n"])
async def test_a_request_with_unclear_framing_is_refused(rig, framing):
    rig.answers["shop.test"] = [PUBLIC]
    chat = rig.chat()
    answer = await rig.talk(b"POST http://shop.test/p HTTP/1.1\r\nHost: shop.test\r\n" + framing + chat.header() + b"\r\nabc")
    assert status(answer) == 400 and rig.public.requests == []


async def test_a_websocket_stays_open_both_ways_after_the_101_and_never_gets_the_credentials(rig):
    rig.answers["shop.test"] = [PUBLIC]
    seen = []

    async def serve(reader, writer):
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        await writer.drain()
        seen.append(await reader.readexactly(5))
        writer.write(b"world")
        await writer.drain()
        await reader.read()
        writer.close()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    rig.maps[(PUBLIC, 80)] = server.sockets[0].getsockname()[1]
    chat = rig.chat()
    reader, writer = await asyncio.open_connection("127.0.0.1", rig.proxy.port)
    writer.write(b"GET http://shop.test/s HTTP/1.1\r\nHost: shop.test\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                 + chat.header() + b"Proxy-Connection: keep-alive\r\n\r\n")
    assert status(await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)) == 101
    writer.write(b"hello")
    assert await asyncio.wait_for(reader.readexactly(5), 3) == b"world"
    writer.close()
    server.close()
    assert seen[1] == b"hello" and b"proxy-" not in seen[0].lower()


async def test_chromium_fetching_one_plain_http_host_twice_never_sends_the_proxy_headers_to_it(tmp_path):
    require_browser()
    rig = Rig()
    rig.public.stop()
    rig.public = Server("public", ignore_close=True,
                        pages={"/": "<html><title>shop</title><body>SHOP</body></html>", "/a": "A", "/b": "B"})
    rig.maps[(PUBLIC, 80)] = rig.public.port
    rig.answers["shop.test"] = [PUBLIC]
    await rig.proxy.start()
    try:
        session = BrowserSession(tmp_path / "profile", headless=True, allowed=lambda key: True, proxy=rig.proxy,
                                 resolver=lambda host: [PUBLIC])
        async with session:
            for path in ("/", "/a", "/b", "/a"):
                result = await session.act("open", url=f"http://shop.test{path}")
                assert result.refused is None, result.text
        assert len(rig.public.headers) >= 4
        assert all("proxy-authorization" not in h and "proxy-connection" not in h for h in rig.public.headers)
        assert rig.private.requests == []
    finally:
        await rig.proxy.stop()
        rig.stop()


# Smaller fixes of the review ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("line", [b"X-A: a\rb\r\n", b"X-A: a\nInjected: yes\r\n", b"X-A: a\x00b\r\n"])
async def test_a_head_with_a_bare_cr_lf_or_nul_is_refused(rig, line):
    rig.answers["shop.test"] = [PUBLIC]
    chat = rig.chat()
    answer = await rig.talk(b"GET http://shop.test/ HTTP/1.1\r\nHost: shop.test\r\n" + line + chat.header() + b"\r\n")
    assert status(answer) == 400 and rig.public.requests == []


async def test_the_upstream_gets_the_checked_normalised_host(rig, upstream):
    rig.answers["shop.test"] = [PUBLIC]
    await rig.connect("Shop.Test.:443", rig.chat())
    await rig.connect("[2606:4700::1111]:443", rig.chat())
    await rig.get("Shop.Test.:80", rig.chat(), "/x")
    heads = [head.split(b"\r\n")[0] for head in upstream.heads]
    assert heads == [b"CONNECT shop.test:443 HTTP/1.1", b"CONNECT [2606:4700::1111]:443 HTTP/1.1",
                     b"GET http://shop.test/x HTTP/1.1"]


async def test_unregistering_a_session_closes_its_open_tunnels(rig):
    chat = rig.chat(f"127.0.0.1:{rig.loop.port}")
    reader, writer = await asyncio.open_connection("127.0.0.1", rig.proxy.port)
    writer.write(f"CONNECT 127.0.0.1:{rig.loop.port} HTTP/1.1\r\n".encode() + chat.header() + b"\r\n")
    assert status(await asyncio.wait_for(reader.read(65536), 3)) == 200
    rig.proxy.unregister(chat.username)
    assert await asyncio.wait_for(reader.read(65536), 3) == b""
    writer.close()
    await settle(rig.proxy)
    quiet(rig.proxy)


async def test_connections_without_credentials_are_capped_for_the_whole_proxy():
    proxy = FilteringProxy(max_connections=3)
    await proxy.start()
    held = []
    try:
        for _ in range(3):
            held.append(await asyncio.open_connection("127.0.0.1", proxy.port))
        await asyncio.sleep(0.1)
        assert proxy.open_connections == 3
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        assert await asyncio.wait_for(reader.read(65536), 3) == b""  # closed at once
        assert proxy.open_connections == 3
        writer.close()
        held.pop()[1].close()
        await settle_to(proxy, 2)
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(b"nonsense\r\n\r\n")
        assert status(await asyncio.wait_for(reader.read(65536), 3)) == 400  # served again
        writer.close()
    finally:
        for _, writer in held:
            writer.close()
        await proxy.stop()


async def settle_to(proxy, count):
    for _ in range(100):
        if proxy.open_connections <= count:
            return
        await asyncio.sleep(0.02)


def test_a_password_with_an_at_sign_is_redacted_whole():
    assert redact_url("http://user:p@ss@host.test:8080") == "http://user:<redacted>@host.test:8080"
    assert redact_url("http://user:a:b@host.test:8080/") == "http://user:<redacted>@host.test:8080/"
    assert redact_url("http://user:p%40ss@host.test:8080") == "http://user:<redacted>@host.test:8080"


@pytest.mark.parametrize("password", ["a:b", "p@ss", "p%ss", "a/b", "x y"])
def test_a_password_survives_the_round_trip_through_the_dialog(password):
    from urllib.parse import quote
    stored = f"http://agent:{quote(password, safe='')}@proxy.test:8080"
    shown = redact_url(stored)
    assert password not in shown
    restored = browser_proxy.restore_password(shown, stored)
    assert browser_proxy.parse_proxy_url(restored).password == password


async def test_the_upstream_is_resolved_off_the_event_loop(tmp_path, monkeypatch):
    import threading
    where = []
    real = browser_proxy.resolve_upstream
    monkeypatch.setattr("local_agent.browser_tool.resolve_upstream",
                        lambda setting: where.append(threading.get_ident()) or real(setting))
    manager = BrowserManager(tmp_path / "state", headless=True, upstream_setting="none")
    choice = await manager.apply_upstream()
    assert choice.mode == "direct" and where and where[0] != threading.get_ident()
