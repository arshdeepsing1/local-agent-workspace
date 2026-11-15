"""A local filtering proxy for the browser tool (browser.py).

Chromium resolves a host name itself when it connects. A name that answers with a public address to the app's check and
then with a private address to Chromium (DNS rebinding) could reach a private service. So Chromium sends every request
to this proxy instead. The proxy resolves the name, checks every address with the app's address policy and connects to
the exact address it checked. Chromium never resolves a name of its own.

Public surface
    FilteringProxy(resolver=None, dial=None, upstream=None, bypass=None, ...)
        await start() -> port           listens on 127.0.0.1 at a random port, and is safe to repeat
        await stop()                    closes every connection and task, and is safe to repeat
        register(policy) -> (user, pw)  one set of random credentials for each browser session
        unregister(user)
        upstream, bypass                the company proxy (an Upstream or None) and its bypass check
        log                             the last requests as (kind, host, port, outcome), never a secret
    Policy(allows, host_map, refused)   what one session may reach: allows(key) for loopback host keys, the host_map
                                        testing hook, and refused(host, text) for the report to the browser tool
    Upstream(host, port, username, password, kind)   kind is "http" (a company proxy) or "socks5"
    resolve_upstream(setting) -> ProxyChoice(mode, upstream, problem)
    check_upstream_setting(value), redact_url(value), restore_password(new, stored)

Sign-in
    Chromium sends proxy credentials only after a 407 answer. The browser session makes one request to PRIME_URL before
    it attaches its own network hooks (a hook on responses would show the 407 to the page and never let Chromium
    answer it). The proxy answers that URL itself. Chromium then keeps the credentials for every later request.

What the proxy accepts
    CONNECT host:port (https, wss) and absolute-URI plain requests (http, ws). Anything else gets a 4xx. A request with
    no valid credentials gets 407 and is never forwarded. The secrets are compared in constant time.
    A plain request gets one connection of its own. The proxy copies the body by its declared length (Content-Length or
    chunked, any other framing is refused), marks the answer Connection: close, and forwards nothing else the client
    sends. So every request is checked, and the session's Proxy-Authorization and Proxy-Connection headers never reach a
    server or the upstream. After a 101 for a websocket the two sides are relayed as they are, to the checked host.

The address check (the same policy as browser.parse_address and web_fetch.classify)
    A private, link-local, CGNAT, unique-local, metadata, multicast or unspecified address is always refused. Loopback is
    reached only through a host key (host and port) that the session allows. A name that is not a loopback name and
    resolves to any loopback or private address is refused. A numeric host in an unusual form (decimal, octal, hex,
    IPv4-mapped) is refused or classified by its real address. The proxy resolves a name once for each connection and
    connects to an address it checked. It never connects to its own port or to the upstream's port.

Upstream (a company proxy such as Zscaler in local proxy mode)
    The proxy still resolves and checks locally first, so a private target is refused before anything leaves. Then it
    sends CONNECT by host name to the upstream and relays the bytes (TLS stays end to end, the proxy never decrypts), or
    sends a plain request as an absolute URI. A host on the bypass list connects directly, but only after it passed the
    same check. Loopback targets never go to the upstream. The upstream's own address is dialled by the proxy itself and
    is never a page target. Credentials exist only in an explicit "http://user:password@host:port" setting and appear
    nowhere as text (<redacted>).

What remains
    With an upstream the upstream resolves the name again, and a name that rebinds between the proxy's lookup and the
    upstream's can reach what the upstream can reach. The proxy cannot pin that address without breaking the upstream's
    own policy by host name. Without an upstream the lookup and the connection are one step in this process.

SOCKS5 upstream (socks5://[user:password@]host:port, or a system SOCKS proxy when "auto" finds no HTTP or HTTPS proxy)
    The same order holds: resolve and check locally, refuse a private target before anything leaves, then open a SOCKS5
    connection to the upstream (RFC 1928, with user name and password from RFC 1929) and send CONNECT by host name
    (address type domain, or the address itself for a numeric host). A plain http request is tunnelled to the checked
    host and port and sent in origin form, with the one request for each connection rule of the direct path. TLS stays
    end to end. SOCKS4 is not supported: "auto" reports it in the status line.
"""
import asyncio
import base64
import binascii
import contextlib
import hmac
import ipaddress
import logging
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

from .web_fetch import Refused, classify, normalize_host

log = logging.getLogger("local_agent.browser_proxy")

MAX_HEAD = 16_384  # bytes of a request head
MAX_HEADERS = 100
HEAD_TIMEOUT = 15  # seconds to send a whole request head
CONNECT_TIMEOUT = 10  # seconds to open one connection
RESOLVE_TIMEOUT = 5
IDLE_TIMEOUT = 300  # seconds without a byte in either direction
GRACE = 2  # seconds the other direction may finish after the client closed
MAX_PER_SESSION = 128  # open connections of one browser session
MAX_CONNECTIONS = 512  # open connections of the whole proxy, signed in or not
CHUNK = 65_536
MAX_ATTEMPTS = 4  # addresses tried for one name
LOG_SIZE = 200
SETTING_MAX = 300
REDACTED = "<redacted>"
SOCKS_REPLIES = {1: "general failure", 2: "not allowed by its rules", 3: "network unreachable", 4: "host unreachable",
                 5: "connection refused", 6: "TTL expired", 7: "command not supported", 8: "address type not supported"}
REASONS = {400: "Bad Request", 403: "Forbidden", 405: "Method Not Allowed", 407: "Proxy Authentication Required", 408: "Request Timeout",
           431: "Request Header Fields Too Large", 502: "Bad Gateway", 503: "Service Unavailable"}
HOP_BY_HOP = {"proxy-authorization", "proxy-connection", "proxy-authenticate", "connection", "keep-alive", "te", "host",
              "trailer", "upgrade"}
TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
AUTHORITY = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[^\s:/\[\]@?#]+):([0-9]{1,5})\Z")
PAC_CACHE_SECONDS = 15
PRIME_HOST = "proxy-sign-in.invalid"  # answered by the proxy itself, for the sign-in below
PRIME_URL = f"http://{PRIME_HOST}/"
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")


class Refusal(Exception):
    """A request the proxy answers itself. text is what the page and the browser tool read."""

    def __init__(self, status, text, *, host=None, record=False):
        super().__init__(text)
        self.status, self.text, self.host, self.record = status, text, host, record


class _Idle(Exception):
    pass


# The upstream setting ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Upstream:
    """The company proxy (kind "http") or a SOCKS5 proxy (kind "socks5"). repr and str never show the password."""
    host: str
    port: int
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    kind: str = "http"

    def __repr__(self):
        return f"Upstream({'socks5 ' if self.kind == 'socks5' else ''}{self.host}:{self.port}" + (f", user={self.username}, password={REDACTED}" if self.username else "") + ")"

    def __str__(self):
        return f"{self.host}:{self.port}"

    def authorization(self):
        """The Proxy-Authorization header line (bytes) for the upstream, or b""."""
        if not self.username or self.kind != "http":
            return b""
        token = base64.b64encode(f"{self.username}:{self.password or ''}".encode())
        return b"Proxy-Authorization: Basic " + token + b"\r\n"


@dataclass(frozen=True)
class ProxyChoice:
    """What the setting came to: mode is the text for the status line, upstream the proxy to use (or None), problem a
    sentence for the user when the app cannot do what the system asks."""
    mode: str
    upstream: Upstream | None = None
    problem: str | None = None


def _hostport(upstream):
    host = upstream.host
    return f"[{host}]:{upstream.port}" if ":" in host else f"{host}:{upstream.port}"


SOCKS_SCHEMES = ("socks5", "socks5h")
SOCKS4_SCHEMES = ("socks4", "socks4a")
SOCKS4_MODE = "SOCKS4 proxy found: not supported, enter a SOCKS5 or http:// proxy address"
SOCKS4_PROBLEM = ("This computer uses a SOCKS4 proxy, which the app does not support. "
                  "Enter a SOCKS5 address such as socks5://127.0.0.1:1080, or an http:// proxy address, in Settings → Browsing.")


def parse_proxy_url(value, *, credentials=True):
    """The Upstream of "http://[user:password@]host:port" or "socks5://[user:password@]host:port". ValueError with a
    sentence for the user otherwise."""
    if not isinstance(value, str) or not value.strip() or len(value) > SETTING_MAX:
        raise ValueError("Enter auto, none or an address such as http://proxy.example:8080 or socks5://proxy.example:1080 "
                         f"(at most {SETTING_MAX} characters).")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("The proxy address has a port that is not a number.") from None
    scheme = parts.scheme.lower()
    if scheme in SOCKS4_SCHEMES:
        raise ValueError("SOCKS4 is not supported. Use a socks5:// or an http:// proxy address.")
    if scheme != "http" and scheme not in SOCKS_SCHEMES:
        raise ValueError("The proxy address must start with http:// or socks5:// (the proxy itself is reached without TLS).")
    kind = "http" if scheme == "http" else "socks5"
    if parts.path not in ("", "/") or parts.query or parts.fragment or any(c in value for c in " \t\r\n"):
        raise ValueError("The proxy address is only host and port, such as http://proxy.example:8080.")
    if not parts.hostname or port is None or not 1 <= port <= 65535:
        raise ValueError("The proxy address needs a host and a port from 1 to 65535, such as http://proxy.example:8080.")
    try:
        host, _ = normalize_host(parts.hostname)
    except Refused:
        raise ValueError("The proxy address has a host that is not valid.") from None
    username = unquote(parts.username) if parts.username is not None else None
    password = unquote(parts.password) if parts.password is not None else None
    if (username is None) != (password is None) or (username is not None and not username):
        raise ValueError(f"A proxy address with credentials needs both a user name and a password: {scheme}://user:password@host:port.")
    if kind == "socks5" and username is not None and credentials:
        if not password or len(username.encode()) > 255 or len(password.encode()) > 255:
            raise ValueError("A SOCKS5 user name and password each take 1 to 255 bytes.")
    if not credentials:
        username = password = None
    return Upstream(host, port, username, password, kind)


def check_upstream_setting(value):
    """The checked setting: "auto", "none" or an http or socks5 proxy address without a trailing slash. ValueError
    otherwise."""
    if value in ("auto", "none"):
        return value
    parse_proxy_url(value)
    return value.rstrip("/")


def redact_url(value):
    """The setting with the password of a proxy address replaced by <redacted>."""
    if not isinstance(value, str):
        return value
    found = re.match(r"([A-Za-z][A-Za-z0-9+.-]*://)([^/?#]*)(.*)\Z", value, re.DOTALL)
    if not found or "@" not in found.group(2):
        return value
    userinfo, _, hostpart = found.group(2).rpartition("@")  # the password may hold a raw @
    user, colon, _ = userinfo.partition(":")
    return f"{found.group(1)}{user}:{REDACTED}@{hostpart}{found.group(3)}" if colon else value


def restore_password(value, stored):
    """The setting a user saved after seeing redact_url's text: a <redacted> password becomes the stored one. ValueError
    when there is no stored password for the same user, host and port."""
    if not isinstance(value, str) or f":{REDACTED}@" not in value:
        return value
    try:
        new, old = parse_proxy_url(value.replace(REDACTED, "x")), parse_proxy_url(stored)
    except (ValueError, TypeError):
        raise ValueError("The proxy password is not saved. Type the password again.") from None
    if old.password is None or (new.username, new.host, new.port) != (old.username, old.host, old.port):
        raise ValueError("The proxy password is not saved for this user and address. Type the password again.")
    return value.replace(REDACTED, quote(old.password, safe=""), 1)


_pac = [0.0, False]


def pac_in_use():
    """True when the system uses a PAC file (an automatic proxy script), which this app cannot evaluate."""
    if time.monotonic() - _pac[0] < PAC_CACHE_SECONDS:
        return _pac[1]
    found = False
    try:
        if sys.platform == "darwin":
            done = subprocess.run(["/usr/sbin/scutil", "--proxy"], capture_output=True, text=True, timeout=3)
            found = bool(re.search(r"ProxyAutoConfigEnable\s*:\s*1\b", done.stdout))
        elif sys.platform.startswith("win"):
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
                found = bool(winreg.QueryValueEx(key, "AutoConfigURL")[0])
    except (OSError, subprocess.SubprocessError, ImportError):
        found = False
    _pac[0], _pac[1] = time.monotonic(), found
    return found


_socks = [0.0, None]


def parse_macos_socks(text):
    """"host:port" from the text of `scutil --proxy` when the SOCKS proxy is on and its host and port are valid, else None."""
    def find(name):
        found = re.search(rf"^\s*{name}\s*:\s*(\S+)\s*$", text, re.MULTILINE)
        return found.group(1) if found else None
    if find("SOCKSEnable") != "1":
        return None
    host, port = find("SOCKSProxy"), find("SOCKSPort")
    try:
        host, _ = normalize_host(host or "")
        number = int(port or "")
    except (Refused, ValueError):
        return None
    if not 1 <= number <= 65535:
        return None
    return f"[{host}]:{number}" if ":" in host else f"{host}:{number}"


def macos_socks_proxy():
    """The SOCKS proxy of System Settings on macOS as "host:port" (read from `scutil --proxy`, like the PAC check), or None
    on any other system and when it is off."""
    if time.monotonic() - _socks[0] < PAC_CACHE_SECONDS:
        return _socks[1]
    found = None
    try:
        if sys.platform == "darwin":
            done = subprocess.run(["/usr/sbin/scutil", "--proxy"], capture_output=True, text=True, timeout=3)
            found = parse_macos_socks(done.stdout)
    except (OSError, subprocess.SubprocessError):
        found = None
    _socks[0], _socks[1] = time.monotonic(), found
    return found


def _system_socks(found):
    """The ProxyChoice for a system SOCKS proxy (the getproxies "socks" entry, then the macOS setting), or None. A SOCKS4
    proxy is named as not supported. System credentials are never used."""
    value = found.get("socks") or macos_socks_proxy()
    if not value:
        return None
    text = value if "://" in value else f"socks5://{value}"
    try:
        if urlsplit(text).scheme.lower() in SOCKS4_SCHEMES:
            return ProxyChoice(SOCKS4_MODE, None, SOCKS4_PROBLEM)
        upstream = parse_proxy_url(text, credentials=False)
    except ValueError:
        return None
    return ProxyChoice(f"system SOCKS proxy {upstream}", upstream)


PAC_MODE = "PAC file found: enter the proxy address"
PAC_PROBLEM = ("This computer finds its proxy through a PAC file, which the app cannot read. "
               "Enter the proxy address in Settings → Browsing, such as http://127.0.0.1:9000.")


def resolve_upstream(setting):
    """The ProxyChoice for a setting ("auto", "none" or an address). "auto" reads the system's HTTPS or HTTP proxy
    (urllib.request.getproxies, which reads System Settings on macOS), and a system SOCKS proxy only when it finds none of
    those. No system proxy means direct: a VPN or Zscaler in tunnel mode captures the traffic below this app. Credentials
    are used only from an explicit address."""
    if setting == "none":
        return ProxyChoice("direct")
    if setting != "auto":
        upstream = parse_proxy_url(setting)
        return ProxyChoice(f"configured {'SOCKS ' if upstream.kind == 'socks5' else ''}proxy {upstream}", upstream)
    try:
        found = urllib.request.getproxies()
    except Exception:  # a broken system setting must not stop browsing
        found = {}
    for key in ("https", "http"):
        value = found.get(key)
        if not value:
            continue
        try:
            upstream = parse_proxy_url(value if "://" in value else f"http://{value}", credentials=False)
        except ValueError:
            continue
        return ProxyChoice(f"system proxy {upstream}", upstream)
    if (socks := _system_socks(found)) is not None:
        return socks
    if pac_in_use():
        return ProxyChoice(PAC_MODE, None, PAC_PROBLEM)
    return ProxyChoice("direct")


def default_bypass(host):
    """True when the system's no_proxy list names this host (urllib.request.proxy_bypass)."""
    try:
        return bool(urllib.request.proxy_bypass(host))
    except Exception:
        return False


# The proxy --------------------------------------------------------------------------------------------------------

@dataclass
class Policy:
    """What one browser session may reach. allows(key): the session allows this loopback host key (host:port).
    host_map: a testing hook, {name: (public address, "127.0.0.1:port")}. refused(host, text): told of each refusal."""
    allows: object = lambda key: False  # noqa: E731
    host_map: dict = field(default_factory=dict)
    refused: object = None


@dataclass
class _Entry:
    policy: Policy
    secret: bytes
    active: int = 0
    tasks: set = field(default_factory=set)  # the connection tasks of this session, cancelled by unregister


@dataclass
class _Route:
    addresses: list
    direct: bool  # loopback and testing-hook targets never go to the upstream
    dial_port: int | None = None  # the testing hook's local port
    host: str = ""  # the checked, normalised host (an IPv6 address in brackets): what is sent on


class FilteringProxy:
    """See the module docstring. The attributes below may change while it runs: upstream, bypass and the limits."""

    def __init__(self, *, resolver=None, dial=None, upstream=None, bypass=None, max_head=MAX_HEAD, head_timeout=HEAD_TIMEOUT,
                 connect_timeout=CONNECT_TIMEOUT, resolve_timeout=RESOLVE_TIMEOUT, idle_timeout=IDLE_TIMEOUT,
                 max_per_session=MAX_PER_SESSION, max_connections=MAX_CONNECTIONS):
        self.resolver, self._dial, self.upstream, self.bypass = resolver, dial or self._open, upstream, bypass or default_bypass
        self.max_head, self.head_timeout, self.connect_timeout = max_head, head_timeout, connect_timeout
        self.resolve_timeout, self.idle_timeout, self.max_per_session = resolve_timeout, idle_timeout, max_per_session
        self.max_connections = max_connections
        self.port = None
        self.open_connections = 0
        self.log = deque(maxlen=LOG_SIZE)
        self._server = None
        self._sessions = {}
        self._tasks = set()
        self._dummy = secrets.token_bytes(32)
        self._upstream_ips = (None, 0.0, frozenset())

    # life cycle

    async def start(self):
        if self._server is None:
            server = await asyncio.start_server(self._serve, "127.0.0.1", 0, limit=self.max_head)
            self._server, self.port = server, server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self):
        server, self._server, self.port = self._server, None, None
        if server is not None:
            server.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            with contextlib.suppress(Exception, asyncio.TimeoutError):
                await asyncio.wait_for(server.wait_closed(), 5)

    def register(self, policy):
        """(username, password): random credentials that map to this policy. Chromium sends them with each request."""
        username, password = f"chat-{secrets.token_hex(6)}", secrets.token_urlsafe(32)
        self._sessions[username] = _Entry(policy, password.encode())
        return username, password

    def unregister(self, username):
        """Forget a session and close its open connections (the browser may outlive its context's close)."""
        entry = self._sessions.pop(username, None)
        for task in list(entry.tasks) if entry else ():
            task.cancel()

    # one connection

    async def _serve(self, reader, writer):
        if self.open_connections >= self.max_connections:
            await self._close(writer)
            return
        task = asyncio.current_task()
        self._tasks.add(task)
        self.open_connections += 1
        try:
            await self._handle(reader, writer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # one broken connection never stops the proxy
            log.debug("A proxy connection ended with %s.", type(exc).__name__)
        finally:
            self._tasks.discard(task)
            self.open_connections -= 1
            await self._close(writer)

    @staticmethod
    async def _close(writer):
        with contextlib.suppress(Exception):
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 2)

    async def _reply(self, writer, status, text, *extra):
        body = text.encode("utf-8", "replace")
        head = (f"HTTP/1.1 {status} {REASONS.get(status, 'Error')}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n" + "".join(f"{line}\r\n" for line in extra) + "\r\n")
        with contextlib.suppress(Exception):
            writer.write(head.encode("latin-1") + body)
            await asyncio.wait_for(writer.drain(), 5)

    async def _handle(self, reader, writer):
        entry = None
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.head_timeout)
            except TimeoutError:
                raise Refusal(408, "The request head did not arrive in time.") from None
            except (asyncio.LimitOverrunError, ValueError):
                raise Refusal(431, "The request head is too large.") from None
            except asyncio.IncompleteReadError:
                return
            method, target, headers = self._parse(head)
            entry = self._authenticate(headers)
            if entry is None:
                self.log.append(("request", "", 0, "auth"))
                raise Refusal(407, "The proxy needs the credentials of its browser session.")
            if entry.active >= self.max_per_session:
                raise Refusal(503, "This browser session has too many open connections.")
            entry.active += 1
            entry.tasks.add(asyncio.current_task())
            try:
                await self._route(entry, reader, writer, method, target, headers)
            finally:
                entry.active -= 1
                entry.tasks.discard(asyncio.current_task())
        except Refusal as refusal:
            if refusal.record and entry is not None:
                self._tell(entry, refusal.host or "", refusal.text)
            if refusal.status != 407:
                self.log.append(("request", refusal.host or "", 0, f"refused {refusal.status}"))
            extra = ('Proxy-Authenticate: Basic realm="Local Agent browser"',) if refusal.status == 407 else ()
            await self._reply(writer, refusal.status, refusal.text, *extra)

    def _tell(self, entry, host, text):
        if entry.policy.refused is not None:
            with contextlib.suppress(Exception):
                entry.policy.refused(host, text)

    # reading the request

    def _parse(self, head):
        text = head[:-4].decode("latin-1")
        bare = text.replace("\r\n", "")
        if "\r" in bare or "\n" in bare or "\x00" in bare:
            raise Refusal(400, "The request head has a bare CR, LF or NUL.")
        lines = text.split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not TOKEN.match(parts[0]) or parts[2] not in ("HTTP/1.0", "HTTP/1.1"):
            raise Refusal(400, "The request line is not valid.")
        if len(lines) - 1 > MAX_HEADERS:
            raise Refusal(431, "The request has too many headers.")
        headers = []
        for line in lines[1:]:
            name, colon, value = line.partition(":")
            if not colon or not TOKEN.match(name):
                raise Refusal(400, "A header is not valid.")
            headers.append((name, value.strip()))
        return parts[0], parts[1], headers

    def _authenticate(self, headers):
        """The session entry whose credentials the request carries, or None. Compared in constant time."""
        value = next((value for name, value in headers if name.lower() == "proxy-authorization"), "")
        scheme, _, token = value.partition(" ")
        user = password = b""
        if scheme.lower() == "basic":
            try:
                user, _, password = base64.b64decode(token.strip(), validate=True).partition(b":")
            except (binascii.Error, ValueError):
                user = b""
        entry = self._sessions.get(user.decode("utf-8", "replace"))
        same = hmac.compare_digest(password, entry.secret if entry else self._dummy)
        return entry if entry is not None and same else None

    # what to do with a request

    async def _route(self, entry, reader, writer, method, target, headers):
        if method.upper() == "CONNECT":
            match = AUTHORITY.match(target)
            if not match or not 1 <= int(match.group(2)) <= 65535:
                raise Refusal(400, "CONNECT needs host:port.")
            host, port = match.group(1), int(match.group(2))
            route = await self._authorize(entry, host, port)
            await self._tunnel(entry, reader, writer, port, route)
            return
        parts = urlsplit(target)
        scheme = parts.scheme.lower()
        if method == "GET" and parts.hostname == PRIME_HOST and scheme == "http":
            self.log.append(("http", PRIME_HOST, 80, "sign-in"))
            await self._reply(writer, 200, "ok")
            return
        if scheme not in ("http", "ws") or not parts.netloc or "@" in parts.netloc:
            raise Refusal(400, "Send CONNECT host:port, or an absolute http:// URL.")
        if method not in METHODS:
            raise Refusal(405, "This method is not forwarded.")
        try:
            port = parts.port or 80
            host = parts.hostname
        except ValueError:
            raise Refusal(400, "The URL's port is not a number.") from None
        if not host or not 1 <= port <= 65535:
            raise Refusal(400, "The URL has no host.")
        host = f"[{host}]" if ":" in host else host
        kind, length = self._framing(headers)  # unclear framing is refused before anything connects
        route = await self._authorize(entry, host, port)
        await self._forward(reader, writer, method, parts, port, headers, route, kind, length)

    async def _authorize(self, entry, host_text, port):
        """The _Route a request may take, or a Refusal. Resolves a name once and checks every address."""
        from .browser import AddressRefused, parse_address  # browser.py imports this module
        try:
            address = parse_address(f"http://{host_text}:{port}/", top_level=False)
        except AddressRefused as exc:
            status = 403 if exc.reason == "private_address" else 400
            raise Refusal(status, f"blocked by the address check: {exc}", host=host_text.lower(), record=True) from None
        host = address.host
        policy = entry.policy
        if host in policy.host_map:  # a testing hook: a name that is public to the check and local to the connection
            public, local = policy.host_map[host]
            if reason := classify(public):
                raise Refusal(403, f"blocked by the address check: {host} {reason}.", host=host, record=True)
            return _Route([public], True, int(str(local).rpartition(":")[2]), _bracket(host))
        addresses = [address.ip] if address.ip else (["127.0.0.1", "::1"] if address.loopback else None)
        if addresses is None:
            addresses = await self._lookup(host)
        if await self._is_own(host, port, addresses):
            raise Refusal(403, f"blocked by the address check: {address.key} is a port of this app.", host=host, record=True)
        if address.loopback:
            try:
                allowed = bool(policy.allows(address.key))
            except Exception:
                allowed = False
            if not allowed:
                raise Refusal(403, f"blocked by the address check: {address.key} is not an allowed local address.",
                              host=host, record=True)
            return _Route(addresses, True, None, _bracket(host))
        if not address.ip:
            for found in addresses:
                if classify(found):
                    raise Refusal(403, f"blocked by the address check: {host} resolved to a private address",
                                  host=host, record=True)
        return _Route(addresses, False, None, _bracket(host))

    async def _lookup(self, host):
        try:
            found = await asyncio.wait_for(self._resolve(host), self.resolve_timeout)
        except (OSError, TimeoutError, ValueError, UnicodeError):
            raise Refusal(502, f"{host} could not be resolved.") from None
        found = list(dict.fromkeys(str(item) for item in found))
        if not found:
            raise Refusal(502, f"{host} did not resolve to any address.")
        return found

    async def _resolve(self, host):
        if self.resolver is not None:
            found = self.resolver(host)
            return list(await found if asyncio.iscoroutine(found) or isinstance(found, asyncio.Future) else found)
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
        return [info[4][0] for info in infos]

    async def _is_own(self, host, port, addresses):
        """True for this proxy's own port on loopback and for the upstream's address and port."""
        if port == self.port and any(_loopback(found) for found in addresses):
            return True
        upstream = self.upstream
        if upstream is None or port != upstream.port:
            return False
        if host == upstream.host:
            return True
        return bool(set(map(_canonical, addresses)) & await self._upstream_addresses(upstream))

    async def _upstream_addresses(self, upstream):
        known, at, found = self._upstream_ips
        if known == upstream and time.monotonic() - at < 60:
            return found
        try:
            addresses = {_canonical(upstream.host)} if _is_ip(upstream.host) else set(
                map(_canonical, await asyncio.wait_for(self._resolve(upstream.host), self.resolve_timeout)))
        except (OSError, TimeoutError, ValueError):
            addresses = set()
        if upstream.host in ("localhost",):
            addresses |= {"127.0.0.1", "::1"}
        self._upstream_ips = (upstream, time.monotonic(), frozenset(addresses))
        return self._upstream_ips[2]

    # connecting

    @staticmethod
    async def _open(address, port):
        return await asyncio.open_connection(address, port, limit=CHUNK)

    async def _connect(self, route, host, port):
        """(reader, writer) to the first checked address that answers."""
        last = None
        for address in route.addresses[:MAX_ATTEMPTS]:
            try:
                return await asyncio.wait_for(self._dial("127.0.0.1" if route.dial_port else address, route.dial_port or port),
                                              self.connect_timeout)
            except (OSError, TimeoutError) as exc:
                last = exc
        raise Refusal(502, f"Could not connect to {host}:{port} ({type(last).__name__}).")

    def _via_upstream(self, route):
        upstream = self.upstream
        if upstream is None or route.direct:
            return None
        try:
            bypassed = bool(self.bypass(route.host.strip("[]")))
        except Exception:
            bypassed = False
        return None if bypassed else upstream

    async def _upstream_connection(self, upstream):
        try:
            return await asyncio.wait_for(self._dial(upstream.host, upstream.port), self.connect_timeout)
        except (OSError, TimeoutError):
            raise Refusal(502, "The upstream proxy did not answer.") from None

    async def _socks_connection(self, upstream, host, port):
        """(reader, writer) of a SOCKS5 tunnel through the upstream to host:port, asked for by name."""
        reader, writer = await self._upstream_connection(upstream)
        done = False
        try:
            await asyncio.wait_for(self._socks_handshake(reader, writer, upstream, host.strip("[]"), port), self.connect_timeout)
            done = True
            return reader, writer
        except (OSError, TimeoutError, asyncio.IncompleteReadError, ValueError):
            raise Refusal(502, "The SOCKS proxy did not answer as a SOCKS5 proxy.") from None
        finally:
            if not done:
                await self._close(writer)

    @staticmethod
    async def _socks_handshake(reader, writer, upstream, host, port):
        """RFC 1928 and RFC 1929. A Refusal for what the SOCKS proxy refuses, ValueError for what it says wrong."""
        signs_in = upstream.username is not None
        methods = b"\x00\x02" if signs_in else b"\x00"
        writer.write(b"\x05" + bytes([len(methods)]) + methods)
        version, method = await reader.readexactly(2)
        if version != 5:
            raise ValueError("not SOCKS5")
        if method == 0xFF or method not in (0, 2) or (method == 2 and not signs_in):
            raise Refusal(502, "The SOCKS proxy needs a sign-in that is not set. Check Settings → Browsing." if not signs_in
                          else "The SOCKS proxy accepts no sign-in method that the app offers. Check Settings → Browsing.")
        if method == 2:
            user, password = upstream.username.encode(), (upstream.password or "").encode()
            writer.write(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
            version, code = await reader.readexactly(2)
            if version != 1:
                raise ValueError("not RFC 1929")
            if code != 0:
                raise Refusal(502, "The SOCKS proxy refused the user name or password. Check Settings → Browsing.")
        if _is_ip(host):
            ip = ipaddress.ip_address(host)
            target = bytes([1 if ip.version == 4 else 4]) + ip.packed
        else:
            name = host.encode("ascii")
            if not 1 <= len(name) <= 255:
                raise Refusal(400, "The host name is too long for SOCKS5.")
            target = b"\x03" + bytes([len(name)]) + name
        writer.write(b"\x05\x01\x00" + target + port.to_bytes(2, "big"))
        version, code, _, kind = await reader.readexactly(4)
        if version != 5:
            raise ValueError("not SOCKS5")
        if code != 0:
            raise Refusal(502, f"The SOCKS proxy could not connect to {host}:{port} (code {code}, "
                               f"{SOCKS_REPLIES.get(code, 'unknown reason')}).")
        size = {1: 4, 4: 16}.get(kind)
        if size is None and kind != 3:
            raise ValueError("unknown address type")
        await reader.readexactly((await reader.readexactly(1))[0] if kind == 3 else size)
        await reader.readexactly(2)

    async def _tunnel(self, entry, reader, writer, port, route):
        host = route.host
        upstream = self._via_upstream(route)
        if upstream is None:
            remote_reader, remote_writer = await self._connect(route, host, port)
            outcome = "allowed"
        elif upstream.kind == "socks5":
            remote_reader, remote_writer = await self._socks_connection(upstream, host, port)
            outcome = "upstream"
        else:
            remote_reader, remote_writer = await self._upstream_connection(upstream)
            outcome = "upstream"
            try:
                remote_writer.write(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n".encode("latin-1")
                                    + upstream.authorization() + b"\r\n")
                await asyncio.wait_for(remote_writer.drain(), self.connect_timeout)
                answer = await asyncio.wait_for(remote_reader.readuntil(b"\r\n\r\n"), self.connect_timeout)
            except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
                await self._close(remote_writer)
                raise Refusal(502, "The upstream proxy did not accept the connection.") from None
            code = answer.split(b" ", 2)[1:2]
            if not code or not code[0].startswith(b"2"):
                await self._close(remote_writer)
                raise Refusal(502, f"The upstream proxy refused the connection ({(code or [b'?'])[0][:3].decode('latin-1')}).")
        self.log.append(("connect", host.strip("[]"), port, outcome))
        try:
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await asyncio.wait_for(writer.drain(), self.connect_timeout)
            await self._relay(reader, writer, remote_reader, remote_writer)
        finally:
            await self._close(remote_writer)

    @staticmethod
    def _framing(headers):
        """("none", 0), ("length", n) or ("chunked", 0) for a request body. Any other framing is refused: the proxy must
        know where the body ends, because nothing after it is a request it has checked."""
        lengths = [value for name, value in headers if name.lower() == "content-length"]
        codings = [value.strip().lower() for name, value in headers if name.lower() == "transfer-encoding"]
        if codings:
            if lengths or codings != ["chunked"]:
                raise Refusal(400, "The request body framing is not supported.")
            return "chunked", 0
        if not lengths:
            return "none", 0
        if len(set(lengths)) != 1 or not re.fullmatch(r"[0-9]{1,15}", lengths[0]):
            raise Refusal(400, "The request has a bad Content-Length.")
        return "length", int(lengths[0])

    async def _forward(self, reader, writer, method, parts, port, headers, route, kind, length):
        """One plain request for each connection: the head is rewritten, the body is copied by its declared length, and
        nothing the client sends after that is forwarded. The answer is marked Connection: close."""
        host = route.host
        upgrade = any(name.lower() == "upgrade" and "websocket" in value.lower() for name, value in headers) and any(
            name.lower() == "connection" and "upgrade" in value.lower() for name, value in headers)
        if parts.scheme.lower() == "ws" and not upgrade:
            raise Refusal(400, "A ws:// request must be an upgrade.")
        authority = host if port == 80 else f"{host}:{port}"
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        upstream = self._via_upstream(route)
        proxied = upstream if upstream is not None and upstream.kind == "http" else None  # a SOCKS tunnel gets origin form
        lines = [f"{method} " + (f"http://{authority}{path}" if proxied else path) + " HTTP/1.1", f"Host: {authority}"]
        lines += [f"{name}: {value}" for name, value in headers if name.lower() not in HOP_BY_HOP]
        if upgrade:
            lines += ["Upgrade: websocket", "Connection: Upgrade"]
        else:
            lines.append("Connection: close")
        if upstream is None:
            remote_reader, remote_writer = await self._connect(route, host, port)
        elif proxied is None:
            remote_reader, remote_writer = await self._socks_connection(upstream, host, port)
        else:
            remote_reader, remote_writer = await self._upstream_connection(upstream)
        self.log.append(("http", host.strip("[]"), port, "upstream" if upstream else "allowed"))
        try:
            head = ("\r\n".join(lines) + "\r\n").encode("latin-1", "replace") + (proxied.authorization() if proxied else b"")
            remote_writer.write(head + b"\r\n")
            await asyncio.wait_for(remote_writer.drain(), self.connect_timeout)
            state = [time.monotonic()]
            answer = asyncio.ensure_future(self._answer(remote_reader, writer, method, upgrade, proxied is not None, state))
            body = None if upgrade or kind == "none" else asyncio.ensure_future(
                self._send_body(reader, remote_writer, kind, length, state))
            try:
                pending = {task for task in (answer, body) if task is not None}
                while not answer.done():
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    if body in done and (body.cancelled() or body.exception() is not None):
                        break  # the client's body broke off: the exchange is over
                if answer.done():
                    switched = answer.result()
                else:
                    switched = False
            finally:
                for task in (answer, body):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(*(task for task in (answer, body) if task is not None), return_exceptions=True)
            if switched:  # 101: from here on both sides speak the upgraded protocol, to the host that was checked
                await self._relay(reader, writer, remote_reader, remote_writer)
        finally:
            await self._close(remote_writer)

    async def _send_body(self, reader, writer, kind, length, state):
        """Copy exactly one request body from the client to the server, and no more."""
        async def take(coro):
            return await asyncio.wait_for(coro, self.idle_timeout)

        async def copy(count):
            while count:
                data = await take(reader.read(min(CHUNK, count)))
                if not data:
                    raise ConnectionError("The client ended inside its body.")
                writer.write(data)
                await asyncio.wait_for(writer.drain(), self.idle_timeout)
                state[0] = time.monotonic()
                count -= len(data)
        if kind == "length":
            await copy(length)
            return
        while True:  # chunked
            line = await take(reader.readuntil(b"\r\n"))
            writer.write(line)
            try:
                size = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise ConnectionError("A chunk size is not valid.") from None
            if size == 0:
                while (trailer := await take(reader.readuntil(b"\r\n"))) != b"\r\n":
                    writer.write(trailer)
                writer.write(trailer)
                await asyncio.wait_for(writer.drain(), self.idle_timeout)
                return
            await copy(size + 2)  # the data and its CRLF

    async def _answer(self, remote_reader, writer, method, upgrade, from_upstream, state):
        """Send the server's answer to the client with Connection: close. True after a 101 for an upgrade request."""
        while True:
            try:
                head = await asyncio.wait_for(remote_reader.readuntil(b"\r\n\r\n"), self.idle_timeout)
                status = int(head.split(b" ", 2)[1])
            except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError, IndexError):
                raise Refusal(502, "The server did not answer with a valid response.") from None
            if status == 407 and from_upstream:
                raise Refusal(502, "The upstream proxy needs credentials that are not set. Check Settings → Browsing.")
            if status == 101:
                if not upgrade:
                    raise Refusal(502, "The server switched protocols without being asked.")
                writer.write(head)
                await asyncio.wait_for(writer.drain(), self.idle_timeout)
                return True
            if status < 200:  # 100 Continue and the like
                writer.write(head)
                await asyncio.wait_for(writer.drain(), self.idle_timeout)
                continue
            break
        kept, length, chunked = [], None, False
        lines = head[:-4].split(b"\r\n")
        for line in lines[1:]:
            name = line.split(b":", 1)[0].strip().lower()
            if name in (b"connection", b"keep-alive", b"proxy-connection", b"proxy-authenticate"):
                continue
            if name == b"content-length":
                with contextlib.suppress(ValueError):
                    length = int(line.split(b":", 1)[1])
            chunked = chunked or name == b"transfer-encoding"
            kept.append(line)
        writer.write(b"\r\n".join([lines[0], *kept, b"Connection: close"]) + b"\r\n\r\n")
        await asyncio.wait_for(writer.drain(), self.idle_timeout)
        if method == "HEAD" or status in (204, 304):
            return False
        if length is None or chunked:
            await self._pipe(remote_reader, writer, state)  # until the server closes, as Connection: close says
            return False
        while length > 0:  # a server that ignored Connection: close is not waited for after its body
            data = await asyncio.wait_for(remote_reader.read(min(CHUNK, length)), self.idle_timeout)
            if not data:
                break
            writer.write(data)
            await asyncio.wait_for(writer.drain(), self.idle_timeout)
            state[0] = time.monotonic()
            length -= len(data)
        return False

    # relaying

    async def _relay(self, client_reader, client_writer, server_reader, server_writer):
        """Copy both ways until one side ends. The server ending ends everything. The client ending lets the server
        finish for a moment. Both sides close, and no task outlives the call."""
        state = [time.monotonic()]
        up = asyncio.ensure_future(self._pipe(client_reader, server_writer, state))
        down = asyncio.ensure_future(self._pipe(server_reader, client_writer, state))
        try:
            await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            if down.done():
                return
            await asyncio.wait({down}, timeout=GRACE)
        finally:
            for task in (up, down):
                task.cancel()
            await asyncio.gather(up, down, return_exceptions=True)

    async def _pipe(self, reader, writer, state):
        """Copy one direction in chunks. It ends at the end of the stream, an error or the idle time."""
        try:
            while True:
                try:
                    data = await asyncio.wait_for(reader.read(CHUNK), self.idle_timeout)
                except TimeoutError:
                    if time.monotonic() - state[0] < self.idle_timeout:
                        continue
                    return
                if not data:
                    with contextlib.suppress(Exception):
                        if writer.can_write_eof():
                            writer.write_eof()
                    return
                state[0] = time.monotonic()
                writer.write(data)
                await asyncio.wait_for(writer.drain(), self.idle_timeout)
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            return


def _bracket(host):
    return f"[{host}]" if ":" in host else host


def _is_ip(text):
    try:
        ipaddress.ip_address(text.strip("[]"))
        return True
    except ValueError:
        return False


def _canonical(text):
    try:
        ip = ipaddress.ip_address(str(text).strip("[]").split("%")[0])
    except ValueError:
        return str(text).lower()
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ip)


def _loopback(text):
    try:
        ip = ipaddress.ip_address(_canonical(text))
    except ValueError:
        return False
    return ip.is_loopback
