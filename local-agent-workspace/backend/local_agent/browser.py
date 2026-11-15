"""The browser engine: a real browser (Playwright, asyncio) that the agent drives, with every safety rule in code.

browser_tool.py wraps this engine for the agent loop (agents.py run_browser): its BrowserManager keeps the sessions, and
the permission layer decides with the facts preview() gives. The engine itself has no permission logic; it enforces the
rules below. Playwright is imported only when a session starts, so the app runs without the package; browser_status()
says what to install.

Public surface
    browser_status(channel=None)        {"ready": True} or {"ready": False, "problem": "<what to do>"}
    BrowserSession(profile_dir, ...)    one browser with a persistent profile
        state                           "closed", "starting", "ready", "busy" or "failed"
        last_activity, idle_seconds()   for the idle close, which the owner does
        await start() / close()         also usable as "async with BrowserSession(...) as session"
        await act(action, submit_ok=None, expect=None, **arguments)
                                        -> BrowserResult; ValueError for an unknown action or a bad argument.
                                        submit_ok(host key) -> bool says whether a submit to that host is approved
                                        (see "The submit rule"); expect is what an approval card showed (see below).
        await preview(action, **args)   -> BrowserResult with host, tab, label, submits and refused, nothing performed
    BrowserResult                       text, more, refused, host, tab, screenshot, screenshot_size, submits, label
    host_key(url), parse_address(url)   the address policy as pure functions (AddressRefused)
    paginate(head, body)                the 8 KB first page and the rest
    credential_reason(attrs)            why a field counts as a credential field, or None

Refusal reasons (BrowserResult.refused): host_not_allowed, private_address, unsupported_address, credential_field,
bot_check, download, dialog, file_chooser, permission_prompt, stale_ref, timeout, not_open, tab_limit (the cap of 8
tabs), submit_needs_approval and page_changed. Every text result starts with web_fetch.UNTRUSTED and ends with
END_MARKER. Every line between them that comes from a page is data: redacted, and with anything that imitates the
banner, an end marker, a refusal text of the app, a Page:/URL: line or tool-result markup bracketed as "[quoted: ...]".
Text the page wrote (a dialog message, a download name, a permission name) is one short line of at most 200 characters
and sits below the app's own Page: and URL: lines, never above them.

Where each rule is enforced
    launch options     accept_downloads=False, service_workers="block", permissions=[] and
                       --deny-permission-prompts, no DNS prefetch, no prerender, no QUIC, WebRTC limited to the proxy,
                       no integrated sign-in to any server, --disable-shared-workers (LAUNCH_FLAGS).
    filtering proxy    every request, loopback included, goes to a local proxy (browser_proxy.py) with this session's own
                       credentials. The proxy resolves each name, checks every address with parse_address and classify,
                       and connects to the address it checked, so Chromium never resolves a name of its own. A refusal
                       is reported as "blocked by the address check". Below the rules that follow, not instead of them.
    init script        runs before any page script in every frame: permission requests are denied, SharedWorker is a
                       stub that throws (not writable, not configurable), fetchLater and Worklet.addModule (audio and
                       paint worklets) are stubs that refuse, and a <script type=speculationrules> is removed the
                       moment it is inserted.
    context.route      every request: top-level documents load only from hosts that allowed(key) accepts;
                       a request is classified by the frame that makes it, so a public frame inside a loopback page
                       may not request loopback or private addresses; a loopback page may request its own origin and
                       other allowed loopback keys, never private ranges; non-http(s) schemes (file:, ftp:) are
                       refused. A blocked top-level navigation is
                       answered with 204, so the page stays where it was. A request of a new popup waits until the
                       tab's redirect hook is attached, and is aborted when that does not happen in time.
    context.route_web_socket   the same address rules for ws: and wss:
    CDP Fetch (response stage) Playwright's route does not see redirect hops, so each page also has a CDP
                       session that stops a redirect to a host or address the rules refuse, before it is followed.
                       The same hook fails a response of the speculation rules type, so a Speculation-Rules header
                       cannot start a prefetch.
    event listeners    dialogs are dismissed (a beforeunload dialog is accepted while the engine itself leaves the
                       page), downloads cancelled, file choosers left empty, permission requests (best effort, by an
                       init script) denied. Each is reported as a refusal. After 20 dialogs in one action the tab is
                       closed.
    in the page        type and select go through one synchronous script that focuses the field, checks it and sets its
                       value, so a page cannot change the field between the check and the input.

The submit rule (request layer)
    While an interactive action (click, type, select, scroll, back, forward) runs on a page that is not on loopback,
    the route stops every request that sends data to a host that is not loopback: a method other than GET, HEAD or
    OPTIONS (a form post, a fetch or XHR with a body, sendBeacon). The request is aborted (a navigation is answered with
    204, so the page stays), the action returns refused="submit_needs_approval" with the target host key, and the owner
    runs the action again with submit_ok(host key) true once the user approved a submit on that host. GET navigations
    stay under the host rules. Data a page sends without any action of the agent (a timer, an analytics beacon while the
    page is just open or loads) is not held: the agent did not make that submission. On loopback nothing is held.

What an approval covers (expect)
    The owner may pass expect={"tab", "host", "ref", "label", "submits"}, the facts the approval card showed. Under the
    session lock, right before it acts, the engine checks that the active tab, its host key, the label of the ref and the
    submit fact are the same. If one differs it does nothing and returns refused="page_changed" with a text that says what
    changed. A ref is unique in the session and belongs to the tab where it was read: a ref of a closed tab is stale and
    never resolves in another tab, and an action never switches tabs by itself.

Limits that remain
    DNS: the filtering proxy resolves, checks and pins each connection (see browser_proxy.py), so a name that answers
    differently to the browser than to the app (DNS rebinding) cannot reach a private address from this machine. The
    app's own lookup (CLASS_TTL, and a new one on every top-level navigation) stays: it gives the early, readable
    refusal, and with a company proxy upstream the upstream resolves the name once more, where only its own policy
    stands between a rebinding name and the company network.
    Request sources the route cannot see: speculation-rules prefetch and prerender (switched off as above, except a
    page that Chromium prefetches through rules this module cannot remove), link prefetch of the browser itself, and the
    browser's own preconnect. Worklets, fetchLater and shared workers are switched off; dedicated workers, EventSource,
    sendBeacon, keepalive fetches, module imports and link preloads go through the route (tests/test_browser_engine.py).
    Redirects: the CDP hook covers every tab this module has attached to. A popup's first request waits for its hook.
    Service workers: registration is faked by Playwright's block option, so none exists and none can route.
    WebSockets, WebRTC and prefetch: ws: and wss: are routed; WebRTC is limited by a launch flag and DNS prefetch
    and prerender are switched off, but other transports are not inspected.
    The outline leaves out text a person cannot see (off the page, clipped, scaled to nothing, transparent, nearly the
    colour of its background, under 4 px, in a closed <details>). Text covered by another element is not detected: the
    test costs a hit test per text node. alt and aria-label text stay, labelled as such.
    Pages can tamper with their own prototypes, so the outline is what the page lets a script see.

host_map is a testing hook only: {"shop.example.test": ("93.184.216.34", "127.0.0.1:8123")} makes the policy
classify the name as the public address and makes the filtering proxy connect to the local port
(after it checked the public address). It never applies unless a caller passes it.
"""
import asyncio
import base64
import contextlib
import ipaddress
import json
import os
import re
import struct
import sys
import time
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .browser_proxy import PRIME_URL, FilteringProxy, Policy
from .result_budget import TOOL_RESULT_HARD_LIMIT
from .web_fetch import (KEPT_CHARS, LOCAL_NAMES, LOCAL_SUFFIXES, MAX_URL_CHARS, PAGE_BYTES, PORTS, UNTRUSTED,
                        Refused, classify, normalize_host)

INSTALL_EXTRA = "pip install -e './backend[browser]'"
INSTALL_BROWSER = "python -m playwright install chromium"
MAX_TABS = 8
BUFFER = 200  # console messages and requests kept per tab
MAX_CONTROLS = 1_000
FIND_LIMIT = 20
MIN_TIMEOUT, MAX_TIMEOUT, DEFAULT_TIMEOUT = 5, 120, 30
MAX_SIDE = 1_568  # the longest side of a screenshot, in pixels
MAX_IMAGE_BYTES = 1_500_000
PUBLIC_TEST_ADDRESS = "93.184.216.34"
RESOLVE_TIMEOUT = 5
REDIRECTS = (301, 302, 303, 307, 308)
REASONS = ("host_not_allowed", "private_address", "unsupported_address", "credential_field", "bot_check", "download",
           "dialog", "file_chooser", "permission_prompt", "stale_ref", "timeout", "not_open", "tab_limit",
           "submit_needs_approval", "page_changed")
MAX_DIALOGS = 20  # dialogs in one action before the tab is closed
MAX_FULL_PAGE_VIEWPORTS = 8  # a full-page screenshot is cut at this many viewport heights
TEXT_CAP = 200  # characters of any text a page wrote that the app shows (a dialog message, a download name, a title)
CLASS_TTL = 30  # seconds a host name's classification is kept
READY_TIMEOUT = 5  # seconds a request of a new tab waits for the tab's redirect hook
NOWHERE = "no-host.invalid"  # a host no server has: the allow lists below match nothing
LAUNCH_FLAGS = ["--deny-permission-prompts", "--blink-settings=dnsPrefetchingEnabled=false",
                "--disable-features=Prerender2,NoStatePrefetch,PrefetchProxy",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-shared-workers",
                # Nothing leaves the browser except through the filtering proxy: no QUIC (UDP), and no integrated
                # Windows or Kerberos sign-in to any server (the allow lists match no host).
                "--disable-quic", f"--auth-server-allowlist={NOWHERE}", f"--auth-negotiate-delegate-allowlist={NOWHERE}"]
INTERACTIVE = ("click", "type", "select", "scroll", "back", "forward")  # the actions the submit rule watches
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
END_MARKER = "[End of untrusted web content]"
# Actions that do not touch a page's controls: they still work when a bot check is on the page.
BOT_CHECK_OK = ("open", "read", "find", "screenshot", "tabs", "close", "back", "forward", "console", "network")

ACTIONS = {
    "open": {"url": str, "new_tab": bool}, "read": {"tab": str}, "find": {"query": str}, "click": {"ref": str},
    "type": {"ref": str, "text": str, "submit": bool}, "select": {"ref": str, "value": str},
    "scroll": {"direction": str, "ref": str}, "back": {}, "forward": {}, "tabs": {"tab": str},
    "screenshot": {"full_page": bool, "ref": str}, "console": {"errors_only": bool}, "network": {"errors_only": bool},
    "close": {"tab": str},
}
REQUIRED = {"open": ("url",), "find": ("query",), "click": ("ref",), "type": ("ref", "text"),
            "select": ("ref", "value")}
DIRECTIONS = ("up", "down", "top", "bottom")

# Credential fields (research R7f). An autocomplete value is a credential when it is one of CREDENTIAL_AUTOCOMPLETE or
# starts with "cc-". The name, id, placeholder, aria-label, label and title of a field are normalised (accents removed,
# camelCase split, lower case) and matched two ways: CREDENTIAL_SUBSTRINGS against the text with the separators removed
# ("user_password" and "userPassword" both give "userpassword"), and CREDENTIAL_TOKENS against each whole word, for the
# short words that would match inside ordinary ones ("pin" but not "pinterest" or "spinning"). The page script uses the
# same two patterns, so the preview and the check right before typing agree.
CREDENTIAL_AUTOCOMPLETE = ("current-password", "new-password", "one-time-code")
CREDENTIAL_SUBSTRINGS = (r"password|passwd|passwort|kennwort|motdepasse|contrasena|wachtwoord|passcode|passphrase|passport|"
                         r"cvv\d?|cvc\d?|ccnum|ccexp|cccsc|cardnum|creditcard|expir|nationalid|taxid|apikey|securityanswer|"
                         r"securityquestion|securitycode|verificationcode|onetimecode|socialsecurity|routingnumber|"
                         r"accountnumber|secretanswer|secretkey|accesstoken|authtoken")
CREDENTIAL_TOKENS = r"pass|pwd\d?|pin|pincode|otp|mfa|2fa|totp|ssn|cvv\d?|cvc\d?|csc|token|secret|senha|iban"
CREDENTIAL_SUB_RE = re.compile(CREDENTIAL_SUBSTRINGS)
CREDENTIAL_TOKEN_RE = re.compile(r"(?:" + CREDENTIAL_TOKENS + r")\Z")
CAMEL = re.compile(r"([a-z0-9])([A-Z])")
WORD = re.compile(r"[a-z0-9]+")
CREDENTIAL_KEYS = ("name", "id", "placeholder", "aria_label", "label", "title")

# Text of a page that imitates the app's own framing. Matched after NFKC folding and without format characters.
BANNER_FAKES = re.compile(
    r"(untrusted[\s_-]+(?:web[\s_-]+)?content|(?:begin|end|start)[\s_-]+(?:of[\s_-]+)?(?:the[\s_-]+)?"
    r"(?:untrusted[\s_-]+)?(?:untrusted|web|page|external|tool|browser|app)[\s_-]+(?:content|output|result|results))",
    re.IGNORECASE)
MARKUP_FAKES = re.compile(
    r"(</?\s*(?:tool_result|tool_use|tool_call|function_results?|function_calls?|system|assistant|user|human|developer|"
    r"untrusted)\b[^>]*>|\[\s*/?\s*(?:system|inst|tool)[^\]]*\]|\[\s*end\s+of\b[^\]]*\])", re.IGNORECASE)
APP_LINES = re.compile(r"\s*(?:not\s+(?:typed|opened|clicked|selected|captured|available|found|run)|stopped|earlier|"
                       r"page|url|tab|scroll)\s*:", re.IGNORECASE)
APP_REFUSALS = re.compile(r"\s*(?:not\s+(?:typed|opened|clicked|selected|captured|available|found|run)|stopped|earlier)\s*:",
                          re.IGNORECASE)



# Address and host policy ----------------------------------------------------------------------------------------

class AddressRefused(ValueError):
    """An address the browser does not open. reason is a refusal reason (private_address, unsupported_address)."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Address:
    url: str  # as given, stripped
    scheme: str
    host: str  # normalised: lower case, IDNA ASCII, no trailing dot, no brackets
    port: int
    key: str  # the host key (see host_key)
    loopback: bool
    ip: str | None  # the address, when the host is an IP literal


def _is_loopback_ip(ip):
    return (ip.version == 4 and ip in ipaddress.ip_network("127.0.0.0/8")) or ip == ipaddress.ip_address("::1")


def parse_address(url, *, top_level=True):
    """The Address of a URL, before any lookup. AddressRefused (with a reason) when the browser never opens it:
    a scheme other than http and https, user info, a numeric host written in an unusual form, a name of a private
    network, an address outside the public internet that is not loopback, and (top level only) a port other than
    80 and 443 on a host that is not loopback. top_level=False is for subresources, which may use other ports."""
    if not isinstance(url, str) or not url.strip():
        raise AddressRefused("unsupported_address", "Give an http or https URL.")
    url = url.strip()
    if len(url) > MAX_URL_CHARS:
        raise AddressRefused("unsupported_address", f"The URL is longer than {MAX_URL_CHARS:,} characters.")
    if any(ord(character) < 33 or ord(character) == 127 for character in url):
        raise AddressRefused("unsupported_address", "The URL contains spaces or control characters.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise AddressRefused("unsupported_address", "The URL is not valid (its port is not a number).") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise AddressRefused("unsupported_address", "Only http and https pages are opened"
                             + (f", not {scheme[:20]}:." if scheme else "."))
    if top_level and "@" in parts.netloc:
        raise AddressRefused("unsupported_address", "A URL with a user name or password is not opened.")
    port = port if port is not None else 443 if scheme == "https" else 80
    try:
        host, ip = normalize_host(parts.hostname)
    except Refused as exc:
        raise AddressRefused("unsupported_address", str(exc)) from None
    if ip is not None:
        loopback = _is_loopback_ip(ip)
        if not loopback and (reason := classify(ip)):
            raise AddressRefused("private_address", f"{host} {reason}; only public addresses and this computer's "
                                 "loopback addresses are opened.")
    else:
        loopback = host == "localhost" or host.endswith(".localhost")
        if not loopback and (host in LOCAL_NAMES or host.endswith(LOCAL_SUFFIXES) or "." not in host):
            raise AddressRefused("private_address", f"{host} names a private network.")
    if top_level and not loopback and port not in PORTS:
        raise AddressRefused("unsupported_address", f"Only the standard web ports ({PORTS[0]} and {PORTS[1]}) are "
                             f"opened on a public host, not port {port}.")
    shown = f"[{host}]" if ip is not None and ip.version == 6 else host
    return Address(url, scheme, host, port, f"{shown}:{port}" if loopback else shown, loopback,
                   str(ip) if ip is not None else None)


def host_key(url):
    """The key a permission rule uses for a URL's host: lower case, IDNA, no trailing dot. A loopback host
    (localhost, *.localhost, 127.0.0.0/8, [::1]) keeps its port, because each local server is its own grant
    (localhost:3000); any other host is the host alone. AddressRefused for an address that is never opened."""
    return parse_address(url).key


def verdict(target, source, *, top_level, allowed, same_origin=False, engine=False):
    """Why a request is not made (a refusal reason), or None. Pure: the session gathers the facts.
    target: "public", "loopback", "private" or "unresolved", the class of the address requested.
    source: "public", "loopback" or "blank", the class of the page that asks.
    top_level: the request loads a tab's top-level document. allowed: the user allowed the target's host key.
    same_origin: the target is the asking page's own origin. engine: the app itself started this navigation."""
    if target == "private":
        return "private_address"
    if target == "unresolved":
        return "unsupported_address"
    if top_level:
        if target == "loopback" and not engine and source != "loopback":
            return "private_address"  # a public page may not send the browser to this computer
        return None if allowed else "host_not_allowed"
    if target == "loopback":
        return None if source == "loopback" and (same_origin or allowed) else "private_address"
    return None


def _words(value):
    """(tokens, compact) of a field attribute: accents removed, camelCase split, lower case, letters and digits only."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    tokens = WORD.findall(CAMEL.sub(r"\1 \2", text).lower())
    return tokens, "".join(tokens)


def page_changes(now, expect, ref=None, line=lambda text, cap=80: str(text)[:cap]):
    """What differs between what an approval card showed (expect: tab, host, label, submits) and a preview of the action
    now (a BrowserResult): a list of phrases, empty when nothing does."""
    found = []
    if "tab" in expect and now.tab != expect["tab"]:
        found.append(f"the active tab was {expect['tab']} and is now {now.tab or 'none'}")
    if "host" in expect and now.host != expect["host"]:
        found.append(f"the page was on {expect['host'] or 'no host'} and is now on {now.host or 'no host'}")
    if "label" in expect and now.label != expect["label"]:
        found.append(f"the label of {ref or 'the control'} was {line(expect['label'], 80)} and is "
                     + (f"now {line(now.label, 80)}" if now.label else "gone"))
    if "submits" in expect and bool(now.submits) != bool(expect["submits"]):
        found.append("the action " + ("now submits a form" if now.submits else "no longer submits a form"))
    return found


def page_changed_text(changes):
    return "The page changed while waiting for approval: " + "; ".join(changes) + ". Nothing was done. Read the page again."


def credential_reason(attrs):
    """Why a field counts as a credential field (a short phrase), or None. attrs: type, autocomplete, name, id,
    placeholder, aria_label, label, title (strings or missing). The page script in TYPE_JS applies the same rules."""
    if str(attrs.get("type") or "").lower() == "password":
        return "a password field"
    for token in str(attrs.get("autocomplete") or "").lower().split():
        if token in CREDENTIAL_AUTOCOMPLETE or token.startswith("cc-"):
            return f"autocomplete={token}"
    for key in CREDENTIAL_KEYS:
        tokens, compact = _words(attrs.get(key))
        if found := CREDENTIAL_SUB_RE.search(compact):
            return f"a field named {found.group(0)}"
        for token in tokens:
            if CREDENTIAL_TOKEN_RE.match(token):
                return f"a field named {token}"
    return None


# Text results ---------------------------------------------------------------------------------------------------

def _fold(text):
    """NFKC, without format characters (zero-width and soft hyphens, bidi marks): the text as a reader would see it."""
    return "".join(char for char in unicodedata.normalize("NFKC", text) if unicodedata.category(char) != "Cf")


def _quote_fragments(line):
    line = BANNER_FAKES.sub(lambda match: f"[quoted: {match.group(0)}]", line)
    return MARKUP_FAKES.sub(lambda match: f"[quoted: {match.group(0)}]", line)


def neutralise(text, *, header=False):
    """Page text with anything that imitates the app's own framing bracketed as quoted text, so a page cannot forge it:
    the untrusted-content banner, an end-of-content marker (also the app's own END_MARKER), tool-result markup, and a
    line that starts like one of the app's refusals ("Not typed: ...", "Stopped: ..."). header=True also quotes a line
    that starts like the app's own Page:, URL:, Tab: or Scroll: line. Matching is done on the NFKC form without
    format characters, so zero-width characters and full-width letters do not get past it; a line that only matches
    in that form is shown in that form."""
    lines = []
    for line in str(text).split("\n"):
        folded = _fold(line)
        probe = folded if folded != line else line
        hit = BANNER_FAKES.search(probe) or MARKUP_FAKES.search(probe)
        starts = (APP_LINES if header else APP_REFUSALS).match(probe)
        if hit or starts:
            line = _quote_fragments(probe)
            if starts:
                line = f"[quoted: {line}]"
        lines.append(line)
    return "\n".join(lines)


def inline(text, cap=TEXT_CAP):
    """One line of at most cap characters: control characters, line and paragraph breaks and runs of spaces become one
    space. For the text a page wrote that the app shows (a dialog message, a download name, a title)."""
    text = re.sub(r"[\x00-\x1f\x7f-\x9f  ]+", " ", str(text))
    text = " ".join(text.split())
    return text if len(text) <= cap else text[:cap - 1] + "…"


def banner(host=None):
    return UNTRUSTED.format(host=host or "the web")


def paginate(head, body, page_bytes=PAGE_BYTES, kept_chars=KEPT_CHARS):
    """(first page, rest): head and the start of body fit page_bytes (UTF-8); the rest, up to kept_chars, is
    returned for the paging tool, or None when everything fits. No result passes the 128 KiB result cap."""
    page_bytes = min(page_bytes, TOOL_RESULT_HARD_LIMIT)
    if len((head + body).encode("utf-8")) <= page_bytes:
        return head + body, None
    reserve = len("\n[The text continues: 9,999,999 more characters (the rest was not kept). Read them with "
                  "get_tool_output.]".encode("utf-8"))
    room = max(0, page_bytes - len(head.encode("utf-8")) - reserve)
    cut = len(body.encode("utf-8")[:room].decode("utf-8", errors="ignore"))
    newline = body.rfind("\n", 0, cut)
    if newline > cut // 2:
        cut = newline
    first, rest = body[:cut], body[cut:].lstrip("\n")
    kept = rest[:kept_chars]
    note = (f"\n[The text continues: {len(kept):,} more characters" + (" (the rest was not kept)" if len(rest) > len(kept)
            else "") + ". Read them with get_tool_output.]")
    return head + first + note, kept


def image_size(data):
    """(width, height) of a PNG or JPEG, or None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":
        position = 2
        while position + 9 < len(data):
            if data[position] != 0xFF:
                position += 1
                continue
            marker = data[position + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                position += 2
                continue
            length = struct.unpack(">H", data[position + 2:position + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", data[position + 5:position + 9])
                return width, height
            position += 2 + length
    return None


# Status ---------------------------------------------------------------------------------------------------------

def _browsers_folder():
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH") not in (None, "", "0"):
        return Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _expected_revision():
    """The Chromium revision this Playwright expects (from its browsers.json), or None."""
    import importlib.util
    import json
    spec = importlib.util.find_spec("playwright")
    for folder in (spec.submodule_search_locations or []) if spec else []:
        try:
            data = json.loads((Path(folder) / "driver" / "package" / "browsers.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for entry in data.get("browsers", []):
            if entry.get("name") == "chromium" and entry.get("revision"):
                return str(entry["revision"])
    return None


def _chrome_installed():
    candidates = ("/Applications/Google Chrome.app", "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
                  "/opt/google/chrome/chrome", "C:/Program Files/Google/Chrome/Application/chrome.exe")
    return any(Path(path).exists() for path in candidates)


def browser_status(channel=None):
    """{"ready": True}, or {"ready": False, "problem": "<what to do>"}. Looks for the package and a browser
    binary without launching anything. channel="chrome" asks for the installed Chrome instead of Chromium."""
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return {"ready": False, "problem": f"Browsing needs the Playwright package. Install it with: {INSTALL_EXTRA}"}
    if channel == "chrome":
        if _chrome_installed():
            return {"ready": True}
    else:
        folder = _browsers_folder()
        revision = _expected_revision()
        found = (folder / f"chromium-{revision}") if revision else None
        if found is not None and found.is_dir() and any(found.iterdir()):
            return {"ready": True}
        if found is None and folder.is_dir() and any(folder.glob("chromium-*")):
            return {"ready": True}
    return {"ready": False, "problem": f"Browsing needs a browser. Install one with: {INSTALL_BROWSER}"}


def auto_headless():
    """False (a visible window) when a display exists: always on macOS and Windows, on Linux when DISPLAY or
    WAYLAND_DISPLAY is set."""
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class BrowserUnavailable(RuntimeError):
    """The browser cannot start; the message says what to install."""


def _playwright():
    try:
        from playwright import async_api
    except ImportError:
        raise BrowserUnavailable(browser_status()["problem"]) from None
    return async_api


# The page scripts -----------------------------------------------------------------------------------------------

CRED_JS = ("const CRED_SUB = new RegExp(" + json.dumps(CREDENTIAL_SUBSTRINGS) + ");\nconst CRED_TOK = new RegExp('^(?:' + "
           + json.dumps(CREDENTIAL_TOKENS) + " + ')$');\nconst CRED_AC = " + json.dumps(list(CREDENTIAL_AUTOCOMPLETE)) + ";\n")

COMMON_JS = CRED_JS + r"""
const SKIP = new Set(['script','style','noscript','template','head','svg','canvas','object','embed','audio','video',
  'map','datalist','option','optgroup','title','meta','link']);
const san = (s) => String(s == null ? '' : s).replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, ' ');
const collapse = (s) => s.replace(/\s+/g, ' ').trim();
const clean = (s) => collapse(san(s));
const clip = (s, n) => s.length > n ? s.slice(0, n - 1) + '…' : s;
const q = (s) => '"' + clip(clean(s), 120).replace(/"/g, "'") + '"';
const word = (s) => /^[A-Za-z0-9_.:\/-]+$/.test(s) ? s : q(s);
function credWords(v) {
  const s = String(v == null ? '' : v).normalize('NFKD').replace(/[̀-ͯ]/g, '').replace(/([a-z0-9])([A-Z])/g, '$1 $2').toLowerCase();
  const tokens = s.match(/[a-z0-9]+/g) || [];
  return {tokens, compact: tokens.join('')};
}
function credentialReason(a) {
  if (String(a.type || '').toLowerCase() === 'password') return 'a password field';
  for (const t of String(a.autocomplete || '').toLowerCase().split(/\s+/)) {
    if (t && (CRED_AC.includes(t) || t.startsWith('cc-'))) return 'autocomplete=' + t;
  }
  for (const k of ['name', 'id', 'placeholder', 'aria_label', 'label', 'title']) {
    const w = credWords(a[k]);
    const m = CRED_SUB.exec(w.compact);
    if (m) return 'a field named ' + m[0];
    for (const t of w.tokens) if (CRED_TOK.test(t)) return 'a field named ' + t;
  }
  return null;
}
// Colours: [r, g, b, a] from a computed rgb() or rgba(); null when the browser gave another form.
function parseColor(s) {
  const m = /^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+%?))?\s*\)$/.exec(s || '');
  if (!m) return null;
  let a = m[4] === undefined ? 1 : (m[4].endsWith('%') ? parseFloat(m[4]) / 100 : parseFloat(m[4]));
  return [+m[1], +m[2], +m[3], a];
}
const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
const parentOf = (el) => el.parentElement || (el.parentNode && el.parentNode.host) || null;
const OPACITY = new WeakMap(), BG = new WeakMap();
function effOpacity(el) {
  if (!el) return 1;
  if (OPACITY.has(el)) return OPACITY.get(el);
  const v = parseFloat(getComputedStyle(el).opacity);
  const o = (isNaN(v) ? 1 : v) * effOpacity(parentOf(el));
  OPACITY.set(el, o);
  return o;
}
// The background a reader sees behind an element: the nearest opaque ancestor colour, blended; null when an image is behind.
function bgOf(el) {
  if (!el) return [255, 255, 255];
  if (BG.has(el)) return BG.get(el);
  const cs = getComputedStyle(el);
  let r;
  if (cs.backgroundImage && cs.backgroundImage !== 'none') r = null;
  else {
    const c = parseColor(cs.backgroundColor), parent = bgOf(parentOf(el));
    if (!c || c[3] === 0) r = parent;
    else if (!parent) r = c[3] >= 0.99 ? c.slice(0, 3) : null;
    else r = [0, 1, 2].map((k) => c[k] * c[3] + parent[k] * (1 - c[3]));
  }
  BG.set(el, r);
  return r;
}
function scaledAway(t) {
  const m = /^matrix(3d)?\(([^)]*)\)$/.exec(t || '');
  if (!m) return false;
  const v = m[2].split(',').map(parseFloat);
  const [a, b, c, d] = m[1] ? [v[0], v[1], v[4], v[5]] : [v[0], v[1], v[2], v[3]];
  return Math.hypot(a, b) < 0.05 || Math.hypot(c, d) < 0.05;
}
function clippedAway(cs, el) {
  if ((cs.position === 'absolute' || cs.position === 'fixed') && cs.clip && cs.clip.startsWith('rect(')) {
    const v = cs.clip.slice(5, -1).split(',').map(parseFloat);
    if (v.length === 4 && (v[1] - v[3] <= 1 || v[2] - v[0] <= 1)) return true;
  }
  const cp = cs.clipPath || '';
  if (/^circle\(\s*0(px|%)?[\s,)]/.test(cp) || cp === 'inset(100%)') return true;
  const inset = /^inset\(([^)]*)\)/.exec(cp);
  if (inset) {
    const v = inset[1].split(/\s+/).map((x) => x.endsWith('%') ? parseFloat(x) : NaN);
    if (v.length && v.every((x) => !isNaN(x))) {
      const [t, r, b, l] = v.length === 1 ? [v[0], v[0], v[0], v[0]] : v.length === 2 ? [v[0], v[1], v[0], v[1]] : v.length === 3 ? [v[0], v[1], v[2], v[1]] : v;
      if (t + b >= 100 || l + r >= 100) return true;
    }
  }
  return false;
}
function hidden(el) {
  if (el.hidden || el.getAttribute('aria-hidden') === 'true' || el.hasAttribute('inert')) return true;
  const cs = getComputedStyle(el);
  if (cs.display === 'none') return true;
  if (cs.display === 'contents') return false;
  if (cs.contentVisibility === 'hidden' || parseFloat(cs.fontSize) === 0) return true;
  if (effOpacity(el) < 0.1) return true;
  if (cs.transform !== 'none' && scaledAway(cs.transform)) return true;
  if (clippedAway(cs, el)) return true;
  const rects = el.getClientRects();
  if (rects.length === 0) return true;
  const clipsContent = (cs.overflowX !== 'visible' || cs.overflowY !== 'visible');
  if (clipsContent && (rects[0].width <= 1 || rects[0].height <= 1)) return true;
  return false;
}
// Text a person cannot read: too small, transparent, the colour of its background, or outside the page.
function unreadable(node) {
  const p = node.parentElement;
  if (!p) return false;
  const cs = getComputedStyle(p);
  if (cs.visibility !== 'visible') return true;
  if (parseFloat(cs.fontSize) < 4) return true;
  const c = parseColor(cs.color);
  if (c && c[3] < 0.1) return true;
  if (c) {
    const bg = bgOf(p);
    if (bg) {
      const blended = [0, 1, 2].map((k) => c[k] * c[3] + bg[k] * (1 - c[3]));
      const a = lum(blended) + 0.05, b = lum(bg) + 0.05;
      if ((Math.max(a, b) / Math.min(a, b)) < 1.2) return true;
    }
  }
  if (collapse(node.nodeValue || '') === '') return false;
  const r = document.createRange();
  r.selectNodeContents(node);
  const box = r.getBoundingClientRect();
  if (box.width > 0 || box.height > 0) {
    if (box.right + scrollX <= 0 || box.bottom + scrollY <= 0) return true;
  }
  return false;
}
function textOf(el) {
  let out = '';
  for (const n of el.childNodes) {
    if (n.nodeType === 3) out += n.nodeValue;
    else if (n.nodeType === 1) {
      const t = n.localName;
      if (SKIP.has(t) || hidden(n)) continue;
      if (t === 'img') { out += ' ' + (n.getAttribute('alt') || '') + ' '; continue; }
      out += ' ' + textOf(n) + ' ';
    }
  }
  return clean(out);
}
function labelOf(el) {
  const parts = [];
  const al = el.getAttribute('aria-label'); if (al) parts.push(al);
  const lb = el.getAttribute('aria-labelledby');
  if (lb) for (const id of lb.split(/\s+/)) { const e = document.getElementById(id); if (e) parts.push(textOf(e)); }
  if (el.labels) for (const l of el.labels) parts.push(textOf(l));
  return clean(parts.join(' '));
}
function roleOf(el) {
  const t = el.localName, r = (el.getAttribute('role') || '').toLowerCase();
  if (t === 'a') return el.hasAttribute('href') ? 'link' : null;
  if (t === 'button' || t === 'summary') return 'button';
  if (t === 'input') {
    const type = (el.type || 'text').toLowerCase();
    if (type === 'hidden') return null;
    if (['submit', 'button', 'reset', 'image'].includes(type)) return 'button';
    if (type === 'checkbox') return 'checkbox';
    if (type === 'radio') return 'radio';
    if (type === 'file') return 'file';
    return 'input';
  }
  if (t === 'textarea') return 'textarea';
  if (t === 'select') return 'select';
  if (el.isContentEditable && !(el.parentElement && el.parentElement.isContentEditable)) return 'editable';
  if (['button', 'link', 'checkbox', 'radio', 'tab', 'menuitem', 'switch', 'option', 'textbox', 'combobox',
       'searchbox', 'slider'].includes(r)) return r;
  return null;
}
function nameOf(el, role) {
  if (role === 'button' && el.localName === 'input') return clean(el.value || el.getAttribute('alt') || el.type);
  const own = clean(el.getAttribute('aria-label') || '');
  if (own) return own;
  if (['link', 'button', 'tab', 'menuitem', 'switch', 'option', 'checkbox', 'radio'].includes(role) && el.localName !== 'input') {
    const t = textOf(el); if (t) return t;
  }
  return labelOf(el) || clean(el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || '');
}
// "button aria-label=..." when the name comes from the aria-label attribute (text a reader of the page does not see).
function titled(role, el) {
  const own = el.localName === 'input' ? '' : clean(el.getAttribute('aria-label') || '');
  return own ? role + ' aria-label=' + q(own) : role + ' ' + q(nameOf(el, role));
}
function fieldAttrs(el) {
  return {type: (el.type || '').toLowerCase(), autocomplete: el.getAttribute('autocomplete') || '', name: el.getAttribute('name') || '',
    id: el.id || '', placeholder: el.getAttribute('placeholder') || '', aria_label: el.getAttribute('aria-label') || '',
    label: labelOf(el), title: el.getAttribute('title') || ''};
}
function describe(el, role) {
  const t = el.localName;
  const flags = (el.disabled ? ' (disabled)' : '') + (el.readOnly ? ' (read-only)' : '');
  const attr = (k, v) => v ? ' ' + k + '=' + q(v) : '';
  const secret = ['input', 'textarea', 'select', 'editable'].includes(role) && !!credentialReason(fieldAttrs(el));
  const filled = (v) => v ? ' (filled)' : ' (empty)';
  if (role === 'link') return titled('link', el) + ' → ' + clip(san(el.href || el.getAttribute('href') || ''), 300);
  if (role === 'input') {
    const type = (el.type || 'text').toLowerCase();
    let s = 'input type=' + type + (el.name ? ' name=' + word(el.name) : '') + attr('label', labelOf(el)) + attr('placeholder', el.getAttribute('placeholder'));
    if (type === 'password' || secret) s += filled(el.value);
    else if (el.value) s += ' value=' + q(el.value);
    return s + flags;
  }
  if (role === 'textarea') return 'textarea' + (el.name ? ' name=' + word(el.name) : '') + attr('label', labelOf(el)) + attr('placeholder', el.getAttribute('placeholder')) + (secret ? filled(el.value) : (el.value ? ' value=' + q(clip(el.value, 200)) : '')) + flags;
  if (role === 'checkbox' || role === 'radio') {
    if (t === 'input') return role + (el.name ? ' name=' + word(el.name) : '') + attr('label', nameOf(el, role)) + (el.value && el.value !== 'on' ? ' value=' + word(el.value) : '') + (el.checked ? ' checked' : ' unchecked') + flags;
    return titled(role, el) + (el.getAttribute('aria-checked') === 'true' ? ' checked' : ' unchecked');
  }
  if (role === 'select') {
    const opts = Array.from(el.options).slice(0, 30).map((o) => clip(clean(o.label || o.text), 60) + (o.value !== (o.label || o.text) ? ' [' + clip(clean(o.value), 40) + ']' : ''));
    const chosen = Array.from(el.selectedOptions).map((o) => clean(o.label || o.text)).join(', ');
    return 'select' + (el.name ? ' name=' + word(el.name) : '') + attr('label', labelOf(el)) + (secret ? filled(chosen) : ' selected=' + q(chosen)) + ' options: ' + opts.join(' | ') + (el.options.length > 30 ? ' | …' : '') + flags;
  }
  if (role === 'file') return 'file-input' + (el.name ? ' name=' + word(el.name) : '') + attr('label', labelOf(el)) + flags;
  if (role === 'editable') return 'editable' + attr('label', labelOf(el)) + (secret ? filled(textOf(el)) : ' text=' + q(clip(textOf(el), 200)));
  return titled(role, el) + flags;
}
// A control's identity for an approval: its role and name now.
function identOf(el) {
  const role = roleOf(el);
  return role ? role + ' ' + q(nameOf(el, role)) : el.localName;
}
function botCheck() {
  if ((document.title || '').toLowerCase().startsWith('just a moment')) return true;
  for (const f of document.querySelectorAll('iframe')) {
    let h = '', p = '';
    try { const u = new URL(f.src, location.href); h = u.hostname.toLowerCase(); p = u.pathname; } catch (e) { continue; }
    if (h === 'challenges.cloudflare.com' || h === 'hcaptcha.com' || h.endsWith('.hcaptcha.com') || h === 'recaptcha.net' ||
        h.endsWith('.recaptcha.net') || ((h === 'www.google.com' || h === 'google.com') && p.startsWith('/recaptcha'))) return true;
  }
  return !!document.querySelector('[class*="captcha" i], [id*="captcha" i]');
}
"""

# Whether a click on the element follows a link or submits a form into another tab (target other than the same one).
NEW_TAB_TARGET_JS = r"""(e) => {
  const link = e.closest('a[href], area[href]');
  const form = e.form || e.closest('form');
  const target = (link && link.target) || (e.formTarget || '') || (!link && form ? form.target : '') || '';
  return target !== '' && !['_self', '_parent', '_top'].includes(target.toLowerCase());
}"""

OUTLINE_JS = "(opts) => {" + COMMON_JS + r"""
const MAXC = opts.maxChars, MAXCTL = opts.maxControls, VIEW = !!opts.viewport, REG = opts.registry;
if (REG.next < opts.floor) REG.next = opts.floor;
const BLOCK = new Set(['address','article','aside','blockquote','body','dd','details','dialog','div','dl','dt','fieldset',
  'figcaption','figure','footer','form','header','hr','main','nav','ol','p','pre','section','table','tr','ul','li','br',
  'h1','h2','h3','h4','h5','h6','thead','tbody','tfoot','caption','menu','legend']);
const lines = [], controls = [], elements = [];
let buf = [], size = 0, lead = '', truncated = false;
function flush() {
  const t = collapse(buf.join('')); buf = [];
  if (!t) return;
  if (size > MAXC) { truncated = true; lead = ''; return; }
  lines.push(lead + t); size += lead.length + t.length + 1; lead = '';
}
function inView(node) {
  const r = document.createRange(); r.selectNodeContents(node);
  const b = r.getBoundingClientRect();
  return b.bottom > 0 && b.top < innerHeight && b.right > 0 && b.left < innerWidth;
}
function walk(node) {
  if (size > MAXC) { truncated = true; return; }
  if (node.nodeType === 3) {
    if (unreadable(node)) return;
    if (VIEW && !inView(node)) return;
    buf.push(san(node.nodeValue)); return;
  }
  if (node.nodeType !== 1) return;
  const el = node, tag = el.localName;
  if (SKIP.has(tag) || hidden(el)) return;
  if (tag === 'iframe' || tag === 'frame') {
    flush(); let h = ''; try { h = new URL(el.src, location.href).host; } catch (e) {}
    lines.push('[frame' + (h ? ': ' + clip(san(h), 100) : '') + ']'); return;
  }
  if (tag === 'img') { const alt = clean(el.getAttribute('alt') || ''); if (alt) buf.push(' [img alt=' + q(alt) + '] '); return; }
  const role = roleOf(el);
  if (role) {
    if (VIEW && !inView(el)) return;
    if (controls.length >= MAXCTL) { truncated = true; return; }
    flush();
    const index = controls.length;
    let id = REG.ids.get(el);
    if (!id) { id = REG.next++; REG.ids.set(el, id); }
    controls.push({id, role, name: nameOf(el, role), text: clip(textOf(el), 200), line: describe(el, role)});
    elements.push(el);
    lines.push('\ue000' + index + '\ue001'); size += 40;
    return;
  }
  const cs = getComputedStyle(el);
  const block = BLOCK.has(tag) || ['block', 'flex', 'grid', 'list-item', 'table', 'table-row', 'flow-root'].includes(cs.display);
  if (/^h[1-6]$/.test(tag)) { flush(); lead = '#'.repeat(+tag[1]) + ' '; }
  else if (tag === 'li') { flush(); lead = '- '; }
  else if (block) flush();
  if ((tag === 'td' || tag === 'th') && el.previousElementSibling) buf.push(' | ');
  if (tag === 'details' && !el.open) {
    for (const c of el.children) if (c.localName === 'summary') walk(c);  // a closed details shows only its summary
  } else if (tag === 'slot') {
    const assigned = el.assignedNodes({flatten: true});
    for (const c of (assigned.length ? assigned : el.childNodes)) walk(c);
  } else {
    if (el.shadowRoot) for (const c of el.shadowRoot.childNodes) walk(c);
    else for (const c of el.childNodes) walk(c);
  }
  if (block) flush();
}
walk(document.body || document.documentElement);
flush();
return {data: {lines, controls, truncated, bot: botCheck(), next: REG.next,
  scroll: {y: Math.round(scrollY), height: document.documentElement.scrollHeight, view: innerHeight}}, elements};
}"""

BOT_JS = "() => {" + COMMON_JS + "return botCheck();}"

# Sets a field's text in one synchronous step: the field gets focus first (a page's focus handler may change it), then
# it is checked, then its value is set, so nothing the page runs can come between the check and the input.
TYPE_JS = "(el, a) => {" + COMMON_JS + r"""
if (!el.isConnected) return {error: 'stale'};
const t = el.localName;
if (!(t === 'input' || t === 'textarea' || el.isContentEditable)) return {error: 'not a text field'};
try { el.focus({preventScroll: true}); } catch (e) {}
if (!a.loopback) {
  const why = credentialReason(fieldAttrs(el));
  if (why) return {refused: why};
}
if (t === 'input' || t === 'textarea') {
  const type = (el.type || 'text').toLowerCase();
  if (['checkbox','radio','file','button','submit','reset','image','range','color','hidden'].includes(type)) return {error: 'not a text field'};
  if (el.disabled || el.readOnly) return {error: 'the field is disabled or read-only'};
  const proto = t === 'input' ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, a.text);
  el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: a.text}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
} else {
  document.execCommand('selectAll'); document.execCommand('insertText', false, a.text);
}
return {ok: true, line: describe(el, roleOf(el) || t)};
}"""

# Chooses an option in one synchronous step, with the same credential check as TYPE_JS.
SELECT_JS = "(el, a) => {" + COMMON_JS + r"""
if (!el.isConnected) return {error: 'stale'};
if (el.localName !== 'select') return {error: 'the element is not a select control'};
if (!a.loopback) {
  const why = credentialReason(fieldAttrs(el));
  if (why) return {refused: why};
}
if (el.disabled) return {error: 'the control is disabled'};
const options = Array.from(el.options);
const hit = options.find((x) => x.value === a.value) || options.find((x) => (x.label || x.text).trim() === a.value);
if (!hit) return {missing: true, line: describe(el, 'select')};
if (hit.disabled) return {error: 'that option is disabled'};
el.selectedIndex = hit.index;
el.dispatchEvent(new Event('input', {bubbles: true}));
el.dispatchEvent(new Event('change', {bubbles: true}));
return {ok: true, line: describe(el, 'select')};
}"""

IDENT_JS = "(el) => {" + COMMON_JS + "return {label: identOf(el), attrs: fieldAttrs(el)};}"

SUBMIT_JS = "(el) => {" + COMMON_JS + r"""
const t = el.localName, type = (el.type || '').toLowerCase(), role = (el.getAttribute('role') || '').toLowerCase();
if (el.hasAttribute('formaction') || el.hasAttribute('formmethod')) return true;
if (t === 'input' && (type === 'submit' || type === 'image')) return true;
if (t === 'button' && (type === 'submit' || type === '') && el.form) return true;
if ((t === 'button' || role === 'button') && el.closest('form')) return true;
return false;
}"""

GUARD_JS = r"""
(() => {
  const fix = (target, name, value) => {
    try { Object.defineProperty(target, name, {value, writable: false, configurable: false, enumerable: false}); } catch (e) {}
  };
  const refuse = (what) => function () { throw new DOMException(what + ' is turned off in this browser.', 'NotSupportedError'); };
  const reject = (what) => function () { return Promise.reject(new DOMException(what + ' is turned off in this browser.', 'NotSupportedError')); };
  // Requests from these never reach the route that checks every address, so they are off.
  fix(window, 'SharedWorker', function SharedWorker() { throw new DOMException('Shared workers are turned off in this browser.', 'NotSupportedError'); });
  if ('fetchLater' in window) fix(window, 'fetchLater', refuse('fetchLater'));
  for (const name of ['Worklet', 'AudioWorklet']) {
    try { if (window[name] && window[name].prototype) fix(window[name].prototype, 'addModule', reject('Worklets are')); } catch (e) {}
  }
  // A speculation rules script can start a prefetch or prerender that the route does not see: remove it as it appears.
  const rules = (n) => n && n.nodeType === 1 && n.localName === 'script' && /^\s*speculationrules\s*$/i.test(n.getAttribute('type') || '');
  const sweep = (n) => { if (rules(n)) { n.remove(); } else if (n && n.querySelectorAll) { n.querySelectorAll('script').forEach((s) => { if (rules(s)) s.remove(); }); } };
  try {
    new MutationObserver((records) => { for (const r of records) for (const n of r.addedNodes) sweep(n); })
      .observe(document, {childList: true, subtree: true});
  } catch (e) {}
})();
"""

PERMISSION_JS = r"""
(() => {
  const ask = (what) => { try { window.__laPermissionAsked(what); } catch (e) {} };
  const denied = (what) => { ask(what); return Promise.reject(new DOMException('Permission denied', 'NotAllowedError')); };
  const each = (fn) => { try { fn(); } catch (e) {} };
  each(() => {
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition = (ok, fail) => { ask('geolocation'); if (fail) setTimeout(() => fail({code: 1, message: 'Permission denied'}), 0); };
    navigator.geolocation.watchPosition = (ok, fail) => { ask('geolocation'); if (fail) setTimeout(() => fail({code: 1, message: 'Permission denied'}), 0); return 0; };
  });
  each(() => { if (window.Notification) Notification.requestPermission = () => { ask('notifications'); return Promise.resolve('denied'); }; });
  each(() => {
    if (!navigator.mediaDevices) return;
    navigator.mediaDevices.getUserMedia = () => denied('camera or microphone');
    navigator.mediaDevices.getDisplayMedia = () => denied('screen capture');
  });
  each(() => { if (navigator.clipboard) { navigator.clipboard.readText = () => denied('clipboard'); navigator.clipboard.read = () => denied('clipboard'); } });
})();
"""


# Session --------------------------------------------------------------------------------------------------------

@dataclass
class BrowserResult:
    text: str  # what goes to the model; starts with the untrusted-content banner
    more: str | None = None  # the rest of a long text, for the paging tool
    refused: str | None = None  # one of REASONS
    host: str | None = None  # the host key the action touched or wants
    tab: str | None = None
    screenshot: bytes | None = None
    screenshot_size: tuple | None = None
    screenshot_mime: str | None = None
    submits: bool = False  # a form submission on a host that is not loopback
    label: str | None = None  # what the ref names now (role and name), for an approval card: see act(expect=...)


@dataclass
class Note:
    reason: str
    host: str | None
    tab: str | None
    text: str
    at: float
    untrusted: bool = False  # the text carries something a page wrote, so it goes below the app's own lines


@dataclass
class Guard:
    """The submit rule for one interactive action: loopback is the acting page's class, submit_ok(host key) says
    whether a submit to that host is approved, blocked is the first host key that was stopped."""
    loopback: bool
    submit_ok: object
    blocked: str | None = None


@dataclass(eq=False)
class Tab:
    id: str
    page: object
    popup: bool = False
    refs: dict = field(default_factory=dict)
    console: deque = field(default_factory=lambda: deque(maxlen=BUFFER))
    network: deque = field(default_factory=lambda: deque(maxlen=BUFFER))
    entries: OrderedDict = field(default_factory=OrderedDict)
    cdp: object = None
    main_frame_id: str | None = None  # the CDP id of the tab's main frame (stable across navigations)
    registry: object = None  # a handle to {ids: WeakMap, next}: the refs of the elements in this document
    engine_pending: bool = False
    closed: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    opener_host: str | None = None  # the host key of the page that opened this one (for about:blank and blob: pages)
    created: float = field(default_factory=time.monotonic)
    requested: bool = False  # the route has seen a request of this tab
    blocked: bool = False  # the route stopped a request of this tab
    dialogs: int = 0  # dialogs in the current action
    dialog_closed: bool = False  # the tab was closed for its dialogs
    claimed: bool = False  # the app leads this popup somewhere itself (see _first_request)


@dataclass
class Draft:
    """An action's result before the notes and the banner are added."""
    status: list = field(default_factory=list)  # lines the app wrote
    body: list = field(default_factory=list)  # lines that come from a page (already redacted and neutralised)
    refused: str | None = None
    host: str | None = None
    tab: str | None = None
    screenshot: bytes | None = None
    screenshot_size: tuple | None = None
    screenshot_mime: str | None = None
    submits: bool = False
    note_ok: bool = True  # include notes that arose during the action
    label: str | None = None


def _validate(action, arguments):
    if action not in ACTIONS:
        raise ValueError(f"Unknown browser action {action!r}. Actions: {', '.join(ACTIONS)}.")
    allowed = ACTIONS[action]
    args = {key: value for key, value in arguments.items() if value is not None}
    for key, value in args.items():
        if key not in allowed:
            raise ValueError(f"Unknown argument {key!r} for the {action} action. "
                             f"It takes: {', '.join(allowed) or 'no arguments'}.")
        if not isinstance(value, allowed[key]) or (allowed[key] is str and not isinstance(value, str)):
            raise ValueError(f"The {key!r} argument of {action} must be {'true or false' if allowed[key] is bool else 'text'}.")
    for key in REQUIRED.get(action, ()):
        if key not in args:
            raise ValueError(f"The {action} action needs the {key!r} argument.")
    if action == "scroll":
        if ("direction" in args) == ("ref" in args):
            raise ValueError("The scroll action needs exactly one of 'direction' and 'ref'.")
        if "direction" in args and args["direction"] not in DIRECTIONS:
            raise ValueError(f"The 'direction' argument must be one of: {', '.join(DIRECTIONS)}.")
    if action == "screenshot" and "ref" in args and args.get("full_page"):
        raise ValueError("The screenshot action takes 'full_page' or 'ref', not both.")
    return args


def _short_error(exc):
    return next((line.strip() for line in str(exc).splitlines() if line.strip()), type(exc).__name__)[:300]


class BrowserSession:
    """One browser for one chat. See the module docstring for the surface. All arguments are injected:
    profile_dir (a persistent profile folder), headless (None: a window when a display exists), channel (None for
    Playwright's Chromium, "chrome" for the installed Chrome), action_timeout (seconds, 5 to 120), allowed (the
    host-permission check, called with a host key), resolver (host -> list of IP strings, sync or async; the
    default asks the system's DNS), redact (applied to all text), the three limits, and host_map (a testing hook,
    see the module docstring)."""

    def __init__(self, profile_dir, *, headless=None, channel=None, action_timeout=DEFAULT_TIMEOUT,
                 allowed=lambda key: False, resolver=None, redact=lambda text: text, page_bytes=PAGE_BYTES,
                 kept_chars=KEPT_CHARS, result_cap=TOOL_RESULT_HARD_LIMIT, host_map=None, proxy=None):
        if not MIN_TIMEOUT <= action_timeout <= MAX_TIMEOUT:
            raise ValueError(f"action_timeout must be from {MIN_TIMEOUT} to {MAX_TIMEOUT} seconds.")
        self.profile_dir = Path(profile_dir)
        self.headless = auto_headless() if headless is None else headless
        self.channel = channel
        self.action_timeout = action_timeout
        self.allowed = allowed
        self.resolver = resolver
        self.redact = redact
        self.page_bytes = min(page_bytes, result_cap)
        self.kept_chars = kept_chars
        self.host_map = dict(host_map or {})
        self.proxy = proxy  # a FilteringProxy shared with other sessions; None: this session makes and stops its own
        self._own_proxy = None
        self._credentials = None  # the user name this session registered at the proxy
        self._granted = set()  # loopback host keys the user allowed in this session: the proxy's allowed set
        self.state = "closed"
        self.last_activity = time.monotonic()
        self._lock = asyncio.Lock()
        self._pw = self._context = None
        self._errors = ()  # Playwright's Error, once it is imported
        self._timeout_error = TimeoutError
        self._tabs = {}
        self._by_page = {}
        self._active = None
        self._counter = 0
        self._creating = False
        self._notes = []
        self._classes = {}
        self._popup_source = {}  # page -> "public" or "loopback": the page that opened a popup
        self._tasks = set()
        self._guard = None  # the Guard of the interactive action that runs now
        self._orphans = 0  # popups of a refused first request that the app was not told about yet
        self._ref_next = 1  # refs are numbered across the whole session, so a ref belongs to one tab for good

    # life cycle

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    def idle_seconds(self):
        return time.monotonic() - self.last_activity

    async def start(self):
        async with self._lock:
            if self.state in ("ready", "busy"):
                return
            await self._start()

    async def _start(self):
        self.state = "starting"
        try:
            api = _playwright()
            self._errors = (api.Error,)
            self._timeout_error = api.TimeoutError
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self._pw = await api.async_playwright().start()
            options = {"headless": self.headless, "accept_downloads": False, "service_workers": "block",
                       "permissions": [], "args": list(LAUNCH_FLAGS), "viewport": {"width": 1280, "height": 800},
                       "proxy": await self._proxy_options()}
            if self.channel:
                options["channel"] = self.channel
            self._context = await self._pw.chromium.launch_persistent_context(str(self.profile_dir), **options)
            context = self._context
            await self._sign_in_to_proxy(context)
            await context.expose_binding("__laPermissionAsked", self._on_permission)
            await context.add_init_script(GUARD_JS + PERMISSION_JS)
            await context.route("**/*", self._route)
            await context.route_web_socket(re.compile(".*"), self._ws_route)
            context.on("page", self._on_new_page)
            context.on("close", lambda _: self._on_context_closed())
            self._tabs.clear()
            self._by_page.clear()
            for page in list(context.pages):
                await self._register_page(page, popup=False)
            self._active = next(iter(self._tabs), None)
            self.state = "ready"
        except BaseException as exc:
            await self._teardown()
            self.state = "failed"
            if isinstance(exc, Exception) and not isinstance(exc, BrowserUnavailable):
                message = _short_error(exc)
                if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
                    raise BrowserUnavailable(f"Browsing needs a browser. Install one with: {INSTALL_BROWSER}") from None
                raise BrowserUnavailable(f"The browser did not start: {message}") from None
            raise

    async def _proxy_options(self):
        """Playwright's proxy option: every request goes to the filtering proxy with this session's own credentials.
        A persistent context is the whole browser, so the proxy is set at launch, which is for this chat only.
        "<-loopback>" sends loopback through the proxy too (Chromium bypasses it by default)."""
        if self.proxy is None:
            self._own_proxy = FilteringProxy(resolver=self.resolver)
        proxy = self.proxy or self._own_proxy
        port = await proxy.start()
        hook = {f"127.0.0.1:{local.rpartition(':')[2]}" for _, local in self.host_map.values()}  # host_map's targets
        policy = Policy(allows=lambda key: key in hook or key in self._granted or bool(self.allowed(key)),
                        host_map=self.host_map, refused=self._proxy_refused)
        username, password = proxy.register(policy)
        self._credentials = (proxy, username)
        return {"server": f"http://127.0.0.1:{port}", "username": username, "password": password, "bypass": "<-loopback>"}

    async def _sign_in_to_proxy(self, context):
        """Chromium answers the proxy's 407 and then keeps the credentials. Do that once now, before this module's own
        response hook exists: that hook would show the 407 to the page instead of letting Chromium answer it."""
        page = context.pages[0] if context.pages else await context.new_page()
        script = f"() => fetch({PRIME_URL!r}, {{mode: 'no-cors', cache: 'no-store'}}).then(() => true, () => false)"
        if not await page.evaluate(script):
            raise BrowserUnavailable("The browser could not sign in to its local proxy.")

    def _proxy_refused(self, host, text):
        """The proxy refused a connection by the address check: the result of the action says so."""
        self._note("private_address", host, None, f"A request to {self._safe(host)[:100]} was {self._safe(text)[:200]}.")

    def _allow(self, key):
        """allowed(key), and a loopback key the user allowed stays allowed for the proxy while the session lives, so a
        page that was opened with a one-time approval can still load its own files and sockets."""
        found = bool(self.allowed(key))
        if found:
            self._granted.add(key)
        return found

    async def close(self):
        """Close every tab and the browser. Safe to call at any time, also while an action runs."""
        self.state = "closed"
        await self._teardown()

    async def _teardown(self):
        context, pw = self._context, self._pw
        self._context = self._pw = None
        self._tabs.clear()
        self._by_page.clear()
        self._active = None
        for task in list(self._tasks):
            task.cancel()
        if context is not None:
            with contextlib.suppress(Exception, asyncio.TimeoutError):
                await asyncio.wait_for(context.close(), 10)
        if pw is not None:
            with contextlib.suppress(Exception, asyncio.TimeoutError):
                await asyncio.wait_for(pw.stop(), 10)
        credentials, own, self._credentials, self._own_proxy = self._credentials, self._own_proxy, None, None
        if credentials is not None:
            credentials[0].unregister(credentials[1])
        if own is not None:
            await own.stop()

    def _on_context_closed(self):
        if self.state not in ("closed", "starting"):
            self.state = "failed"

    def _spawn(self, coroutine):
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task):
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()  # retrieved, so the loop does not log it

    # tabs and events

    def _on_new_page(self, page):
        popup = not self._creating
        if popup:
            opener = self._tabs.get(self._active)
            self._popup_source[page] = self._source_class(opener.page.url) if opener else "blank"
        self._spawn(self._register_page(page, popup=popup))

    async def _register_page(self, page, *, popup):
        if page in self._by_page or self._context is None:
            return
        if len(self._tabs) >= MAX_TABS:
            self._note("tab_limit", None, None, f"A page tried to open a ninth tab; at most {MAX_TABS} tabs are kept, "
                       "so it was closed.")
            with contextlib.suppress(Exception):
                await page.close()
            return
        self._counter += 1
        tab = Tab(f"t{self._counter}", page, popup=popup)
        self._tabs[tab.id] = tab
        self._by_page[page] = tab
        if popup:
            self._note("popup", None, tab.id, f"A page opened a new tab {tab.id}.", counts=False)
            with contextlib.suppress(Exception):
                opener_page = await page.opener()  # the page that really opened it, not the one that is active
                opener = self._by_page.get(opener_page)
                if opener is not None:
                    self._popup_source[page] = self._source_class(opener.page.url)
                    tab.opener_host = self._host_of(opener)
            if tab.opener_host is None:
                opener = self._tabs.get(self._active)
                tab.opener_host = self._host_of(opener) if opener is not None and opener is not tab else None
        page.on("console", lambda message: self._on_console(tab, message))
        page.on("pageerror", lambda error: tab.console.append(("pageerror", self._safe(str(getattr(error, "message", error))), "")))
        page.on("request", lambda request: self._entry(tab, request))
        page.on("response", lambda response: self._on_response(tab, response))
        page.on("requestfailed", lambda request: self._on_request_failed(tab, request))
        page.on("dialog", lambda dialog: self._spawn(self._on_dialog(tab, dialog)))
        page.on("download", lambda download: self._spawn(self._on_download(tab, download)))
        page.on("filechooser", lambda chooser: self._on_file_chooser(tab))
        page.on("framenavigated", lambda frame: self._on_navigated(tab, frame))
        page.on("close", lambda _: self._on_page_closed(tab))
        try:
            tab.cdp = await self._context.new_cdp_session(page)
            tab.cdp.on("Fetch.requestPaused", lambda event: self._spawn(self._on_paused(tab, event)))
            tree = await tab.cdp.send("Page.getFrameTree")
            tab.main_frame_id = tree["frameTree"]["frame"]["id"]
            await tab.cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]})
        except Exception:
            tab.cdp = None
            if not tab.closed:
                self._note("unsupported_address", None, tab.id, "The redirect guard could not start for a tab, so it "
                           "was closed.")
                with contextlib.suppress(Exception):
                    await page.close()
        if self._active is None:
            self._active = tab.id
        tab.ready.set()

    def _on_page_closed(self, tab):
        tab.closed = True
        self._tabs.pop(tab.id, None)
        self._by_page.pop(tab.page, None)
        self._popup_source.pop(tab.page, None)
        if self._active == tab.id:
            self._active = next(reversed(self._tabs), None)

    def _on_navigated(self, tab, frame):
        if frame.parent_frame is None:
            tab.refs.clear()
            tab.registry = None

    def _note(self, reason, host, tab, text, *, counts=True, untrusted=False):
        self._notes.append(Note(reason if counts else "info:" + reason, host, tab, text, time.monotonic(), untrusted))
        del self._notes[:-50]

    def _safe(self, text):
        return neutralise(self.redact(text if isinstance(text, str) else str(text)))

    def _line(self, text, cap=TEXT_CAP):
        """Text a page wrote, as one short line of data: redacted, on one line, cut, and with anything that imitates the
        app's own lines quoted."""
        return neutralise(inline(self.redact(text if isinstance(text, str) else str(text)), cap), header=True)

    def _entry(self, tab, request):
        entry = tab.entries.get(request)
        if entry is None:
            entry = {"method": request.method, "url": request.url, "status": None, "failure": None,
                     "type": request.resource_type, "blocked": None}
            tab.entries[request] = entry
            tab.network.append(entry)
            while len(tab.entries) > 2 * BUFFER:
                tab.entries.popitem(last=False)
        return entry

    def _on_response(self, tab, response):
        self._entry(tab, response.request)["status"] = response.status

    def _on_request_failed(self, tab, request):
        self._entry(tab, request)["failure"] = request.failure or "failed"

    def _on_console(self, tab, message):
        try:
            location = message.location or {}
            where = f"{location.get('url', '')}:{location.get('lineNumber', '')}" if location.get("url") else ""
            tab.console.append((message.type, self._safe(message.text), self._safe(where)))
        except Exception:
            pass

    async def _on_dialog(self, tab, dialog):
        tab.dialogs += 1
        if tab.dialogs > MAX_DIALOGS:
            with contextlib.suppress(Exception):
                await dialog.dismiss()
            if not tab.closed and not tab.dialog_closed:
                tab.dialog_closed = True
                self._note("dialog", None, tab.id, "The page kept opening dialogs, so the tab was closed.")
                with contextlib.suppress(Exception):
                    await tab.page.close()
            return
        if dialog.type == "beforeunload" and tab.engine_pending:
            # The app itself leaves the page (open, back, forward): a page cannot hold the tab.
            self._note("dialog", None, tab.id, "A beforeunload dialog of the page was accepted, so the tab could leave "
                       "the page.")
            with contextlib.suppress(Exception):
                await dialog.accept()
            return
        if tab.dialogs <= 5:  # a page that opens many dialogs gets a few lines, then the close above
            self._note("dialog", None, tab.id, f"A {self._line(dialog.type, 30)} dialog was dismissed: "
                       f"{self._line(dialog.message)}", untrusted=True)
        with contextlib.suppress(Exception):
            await dialog.dismiss()

    async def _on_download(self, tab, download):
        name = self._line(download.suggested_filename, 120)
        try:
            host = urlsplit(download.url).hostname or ""
        except ValueError:
            host = ""
        self._note("download", None, tab.id, f"A download was cancelled: file {name!r} from {self._line(host, 120)}. A "
                   "download needs the user's approval.", untrusted=True)
        with contextlib.suppress(Exception):
            await download.cancel()

    def _on_file_chooser(self, tab):
        self._note("file_chooser", None, tab.id, "A page asked for a file. No file was chosen: choosing one needs the "
                   "user.")

    def _on_permission(self, source, what):
        tab = self._by_page.get(source.get("page")) if isinstance(source, dict) else None
        what = what[:40] if isinstance(what, str) else "something"
        self._note("permission_prompt", None, tab.id if tab else None,
                   f"A page asked for permission ({self._line(what, 40)}). It was denied.", untrusted=True)

    # the address decision

    async def _resolve(self, host):
        if host in self.host_map:
            return [self.host_map[host][0]]
        if self.resolver is not None:
            found = self.resolver(host)
            return list(await found if asyncio.iscoroutine(found) or isinstance(found, asyncio.Future) else found)
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=2)
        return list(dict.fromkeys(str(info[4][0]) for info in infos))

    async def _classify(self, address):
        """("public" | "loopback" | "private" | "unresolved", detail) for a parsed Address. A name is resolved again when
        its answer is CLASS_TTL seconds old and on every top-level navigation (_decide, _do_open drop it first), and
        every address it resolves to must be public (a name that resolves to this computer or to a private network is
        refused: DNS rebinding, see the module docstring for what remains)."""
        if address.loopback:
            return "loopback", ""
        if address.ip is not None:
            return "public", ""
        cached = self._classes.get(address.host)
        if isinstance(cached, asyncio.Future):
            return await cached
        if cached is not None and time.monotonic() - cached[0] < CLASS_TTL:
            return cached[1]
        future = asyncio.get_running_loop().create_future()
        self._classes[address.host] = future
        try:
            addresses = await asyncio.wait_for(self._resolve(address.host), RESOLVE_TIMEOUT)
            if not addresses:
                result = ("unresolved", f"{address.host} did not resolve to any address.")
            else:
                result = ("public", "")
                for item in addresses:
                    if reason := classify(item):
                        result = ("private", f"{address.host} resolves to {item}, which {reason}.")
                        break
        except (OSError, TimeoutError, ValueError):
            result = ("unresolved", f"{address.host} could not be resolved.")
        except BaseException:
            self._classes.pop(address.host, None)
            future.cancel()
            raise
        self._classes[address.host] = (time.monotonic(), result)
        future.set_result(result)
        return result

    def _source_class(self, page_url):
        try:
            address = parse_address(page_url, top_level=False)
        except AddressRefused:
            return "blank"
        return "loopback" if address.loopback else "public"

    def _frame_source(self, request, tab):
        """(class, url) of the page that makes a request: "public", "loopback" or "blank", and the address it has. The
        class comes from the requesting frame, not from the tab, so a public frame inside a loopback page asks as
        public. A frame's own document is requested by its parent; a frame with no address of its own (about:blank,
        srcdoc, blob) asks as its nearest ancestor that has one, and a popup as the page that opened it."""
        frame = None
        with contextlib.suppress(Exception):  # the frame is not there yet: the first request of a new popup
            frame = request.frame
            if request.is_navigation_request() and frame.parent_frame is not None:
                frame = frame.parent_frame
        while frame is not None:
            url = frame.url
            source = self._source_class(url)
            if source != "blank":
                return source, url
            frame = frame.parent_frame
        if tab is not None:
            return self._popup_source.get(tab.page, "blank"), tab.page.url
        opener = self._tabs.get(self._active)  # the first request of a popup that is not registered yet
        return (self._source_class(opener.page.url) if opener else "blank"), ""

    async def _decide(self, tab, request):
        """None to let a request through, else (reason, host key, top_level) to block it."""
        url = request.url
        scheme = urlsplit(url).scheme.lower()
        if scheme in ("data", "blob", "about"):
            return None
        top_level = False
        if request.is_navigation_request():
            try:
                top_level = request.frame.parent_frame is None
            except Exception:  # the frame is not there yet: the first request of a new popup
                top_level = True
        try:
            address = parse_address(url, top_level=top_level)
        except AddressRefused as exc:
            return exc.reason, None, top_level
        allowed = False
        if top_level or address.loopback:
            allowed = bool(self._allow(address.key))
            if top_level and not allowed:
                return "host_not_allowed", address.key, top_level  # before any lookup
        if top_level:
            self._classes.pop(address.host, None)  # every top-level navigation looks the name up again
        target, _ = await self._classify(address)
        source, source_url = self._frame_source(request, tab)
        same = False
        if address.loopback:
            with contextlib.suppress(AddressRefused):
                same = parse_address(source_url, top_level=False).key == address.key
        engine = False
        if top_level and tab is not None:
            engine, tab.engine_pending = tab.engine_pending, False
        reason = verdict(target, source, top_level=top_level, allowed=allowed, same_origin=same, engine=engine)
        return (reason, address.key, top_level) if reason else None

    def _held(self, request):
        """The host key of a request that sends data to a host that is not loopback while an interactive action runs and
        no submit is approved for that host (the submit rule, see the module docstring); else None."""
        guard = self._guard
        if guard is None or guard.loopback or request.method.upper() in SAFE_METHODS:
            return None
        try:
            address = parse_address(request.url, top_level=False)
        except AddressRefused:
            return None  # the address rules refuse it anyway
        if address.loopback:
            return None
        try:
            approved = bool(guard.submit_ok(address.key)) if guard.submit_ok is not None else False
        except Exception:
            approved = False
        if approved:
            return None
        guard.blocked = guard.blocked or address.key
        return address.key

    async def _route(self, route):
        request = route.request
        tab = blocked = None
        unhooked = True
        try:
            try:
                page = request.frame.page
                tab = self._by_page.get(page)
                unhooked = tab is None or not tab.ready.is_set()  # decided now: a hook made later sees nothing of this
                for _ in range(50):  # a new popup is registered a moment after its first request
                    if tab is not None or page.is_closed():
                        break
                    await asyncio.sleep(0.02)
                    tab = self._by_page.get(page)
            except Exception:
                tab, unhooked = None, True
            try:
                top = request.is_navigation_request() and request.frame.parent_frame is None
            except Exception:  # the frame is not there yet: the first request of a new popup
                top = request.is_navigation_request()
            if tab is not None:
                tab.requested = True
                if unhooked and not top:
                    # The tab's redirect hook is not attached yet: wait for it, or stop the request, so a subresource is
                    # never redirected to a place the rules refuse without anyone looking.
                    try:
                        await asyncio.wait_for(tab.ready.wait(), READY_TIMEOUT)
                    except TimeoutError:
                        blocked = ("unsupported_address", None, False)
            if blocked is None:
                blocked = await self._decide(tab, request)
            if blocked is None and (key := self._held(request)) is not None:
                blocked = ("submit_needs_approval", key, top)
        except asyncio.CancelledError:
            raise
        except Exception:
            blocked = ("unsupported_address", None, False)
        with contextlib.suppress(Exception):
            if blocked is None:
                if unhooked and top:
                    await self._first_request(route, request, tab)  # no redirect hook can see this request: see there
                    return
                await route.continue_()
                return
            reason, key, top_level = blocked
            if tab is not None:
                self._entry(tab, request)["blocked"] = reason.replace("_", " ")
                tab.blocked = True
            self._note(reason, key, tab.id if tab else None, self._block_text(reason, key, request.url, top_level))
            if top_level:
                await route.fulfill(status=204)  # the page stays where it was
            else:
                await route.abort("blockedbyclient")

    def _mapped(self, url):
        """(url, Host header) a request is made with outside the browser: a name of host_map (a testing hook) is not in
        the system's DNS, so the request goes to its local port."""
        parts = urlsplit(url)
        entry = self.host_map.get(parts.hostname or "")
        if entry is None:
            return url, None
        return parts._replace(netloc=entry[1]).geturl(), parts.netloc

    async def _first_request(self, route, request, tab):
        """The first document request of a popup. It is made before this module can attach its redirect hook to the new
        tab, and a hook attached later does not see a request that is already in flight, so a redirect of this request
        would be followed unseen. The app makes the request itself without following redirects: an ordinary answer is
        handed to the page; a redirect is judged first, and when it is allowed the tab is led to its target by a new
        navigation, which the hook sees. A refused redirect leaves the popup blank (the caller closes it).
        tab is None when the browser has not told the app about the popup yet (a request of a new popup has no frame)."""
        url, host = self._mapped(request.url)
        try:
            response = await route.fetch(url=url, headers={**request.headers, "host": host} if host else None,
                                         max_redirects=0)
        except Exception:
            await route.abort("blockedbyclient")
            return
        location = response.headers.get("location")
        if response.status not in REDIRECTS or not location:
            await route.fulfill(response=response)
            return
        target = urljoin(request.url, location)
        refusal = await self._redirect_verdict(request.url, target, True)
        await route.fulfill(status=204)  # the popup stays blank
        if refusal:
            reason, key = refusal
            if tab is not None:
                tab.blocked = True
            else:
                self._orphans += 1
            self._note(reason, key, tab.id if tab else None, self._block_text(reason, key, target, True)
                       .replace("A navigation", "A redirect"))
        else:
            self._spawn(self._follow(tab, target, request.headers.get("referer")))

    async def _follow(self, tab, url, referer):
        """Lead a popup to the target of an allowed redirect, once its hook is attached."""
        with contextlib.suppress(Exception, TimeoutError):
            for _ in range(100):  # a popup the app has not been told about yet is the oldest one still blank
                tab = tab or next((item for item in self._tabs.values() if item.popup and not item.claimed
                                   and item.page.url in ("", "about:blank")), None)
                if tab is not None:
                    break
                await asyncio.sleep(0.02)
            if tab is None:
                return
            tab.claimed = True
            await asyncio.wait_for(tab.ready.wait(), READY_TIMEOUT)
            await tab.page.goto(url, wait_until="commit", referer=referer, timeout=self.action_timeout * 1000)

    async def _redirect_verdict(self, from_url, target_url, top_level):
        """(reason, host key) when a redirect from from_url to target_url is refused, else None."""
        if urlsplit(target_url).scheme.lower() in ("data", "blob", "about"):
            return None
        try:
            address = parse_address(target_url, top_level=top_level)
        except AddressRefused as exc:
            return exc.reason, None
        allowed = bool(self._allow(address.key)) if (top_level or address.loopback) else False
        if top_level and not allowed:
            return "host_not_allowed", address.key
        if top_level:
            self._classes.pop(address.host, None)
        target, _ = await self._classify(address)
        reason = verdict(target, self._source_class(from_url), top_level=top_level, allowed=allowed)
        return (reason, address.key) if reason else None

    def _block_text(self, reason, key, url, top_level):
        where = "navigation" if top_level else "request"
        if reason == "host_not_allowed":
            return f"A {where} to {key} was stopped: that host is not allowed yet. The user must allow it first."
        if reason == "submit_needs_approval":
            return (f"A {where} that sends data to {key} was stopped: the page tried to submit a form or send data. "
                    f"A submit on {key} needs the user's approval.")
        return f"A {where} to {self._safe(url)[:200]} was blocked ({reason.replace('_', ' ')})."

    async def _ws_route(self, ws):
        try:
            address = parse_address(ws.url.replace("ws", "http", 1), top_level=False)
            target, _ = await self._classify(address)
            others = [tab for tab in self._tabs.values() if self._source_class(tab.page.url) == "public"]
            reason = verdict(target, "public" if others else "loopback", top_level=False,
                             allowed=bool(self._allow(address.key)) if address.loopback else True)
        except AddressRefused as exc:
            reason = exc.reason
        except asyncio.CancelledError:
            raise
        except Exception:
            reason = "unsupported_address"
        with contextlib.suppress(Exception):
            if reason is None:
                await ws.connect_to_server()
            else:
                self._note(reason, None, None, f"A WebSocket to {self._safe(ws.url)[:200]} was blocked "
                           f"({reason.replace('_', ' ')}).")
                await ws.close(code=1008, reason="blocked")

    async def _on_paused(self, tab, event):
        """Response stage: stop a redirect to an address or host the rules refuse, before it is followed."""
        cdp, request_id = tab.cdp, event["requestId"]
        try:
            code = event.get("responseStatusCode")
            headers = {item["name"].lower(): item["value"] for item in event.get("responseHeaders") or []}
            if "speculationrules" in headers.get("content-type", "").lower():
                # A rules file (the Speculation-Rules header of a page points at one) would start prefetches that the
                # route cannot see: it never reaches the page.
                await cdp.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"})
                return
            location = headers.get("location")
            if code in REDIRECTS and location:
                target_url = urljoin(event["request"]["url"], location)
                top_level = False
                if event.get("resourceType") == "Document":  # the main frame's id is kept from when the tab opened
                    top_level = tab.main_frame_id is None or tab.main_frame_id == event.get("frameId")
                blocked = await self._redirect_verdict(event["request"]["url"], target_url, top_level)
                if blocked:
                    reason, key = blocked
                    tab.blocked = True
                    self._note(reason, key, tab.id, self._block_text(reason, key, target_url, top_level)
                               .replace("A navigation", "A redirect").replace("A request", "A redirect"))
                    if top_level:  # failing a navigation would show an error page; stopping it keeps the page
                        await cdp.send("Page.stopLoading")
                    else:
                        await cdp.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"})
                    return
            await cdp.send("Fetch.continueRequest", {"requestId": request_id})
        except asyncio.CancelledError:
            raise
        except Exception:
            with contextlib.suppress(Exception):  # fail closed
                await cdp.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"})

    # actions

    def _tab(self, tab_id=None):
        tab = self._tabs.get(tab_id or self._active)
        return tab

    def _host_of(self, tab):
        """The host key of a tab's page. A tab with no address of its own (about:blank, blob:) has its opener's."""
        if tab is None:
            return None
        try:
            return parse_address(tab.page.url, top_level=False).key
        except AddressRefused:
            return tab.opener_host

    def _text(self, draft, started=None):
        """Compose the final BrowserResult: banner, the app's lines, notes, then page lines, then the end marker; paged.
        A note that carries text a page wrote goes into the page lines, below the app's own Page:, URL: and Tab: lines."""
        status = list(draft.status)
        body = list(draft.body)
        refused = draft.refused
        if draft.note_ok:
            lines = {False: ([], []), True: ([], [])}  # untrusted -> (this action's notes, earlier notes)
            urgent = None
            for note in self._notes:
                current = note.reason.startswith("info:") or started is None or note.at >= started
                lines[note.untrusted][0 if current else 1].append(note.text if current else "Earlier: " + note.text)
                if current and not note.reason.startswith("info:"):
                    if note.reason == "submit_needs_approval" and urgent is None:
                        urgent = note
                    if refused is None:
                        refused, draft.host = note.reason, note.host or draft.host
            if urgent is not None:  # the owner must ask the user, whatever else happened
                refused, draft.host = urgent.reason, urgent.host or draft.host
            self._notes.clear()
            status.extend(lines[False][0] + lines[False][1])
            page_notes = lines[True][0] + lines[True][1]
            if page_notes:
                at = 0
                while at < len(body) and at < 4 and body[at].startswith(("Page: ", "URL: ", "Tab: ", "Scroll: ")):
                    at += 1
                body[at:at] = page_notes
        head = banner(draft.host) + "\n" + "".join(line + "\n" for line in status)
        if body and status:
            head += "\n"
        text, more = paginate(head, "\n".join(body), self.page_bytes - len(END_MARKER) - 1, self.kept_chars)
        return BrowserResult(text.rstrip("\n") + "\n" + END_MARKER, more, refused, draft.host, draft.tab, draft.screenshot,
                             draft.screenshot_size, draft.screenshot_mime, draft.submits, draft.label)

    def _refusal(self, reason, line, *, host=None, tab=None):
        return self._text(Draft(status=[line], refused=reason, host=host, tab=tab, note_ok=False))

    async def act(self, action, *, submit_ok=None, expect=None, **arguments):
        """Run one action and return its BrowserResult. ValueError for an unknown action or a bad argument.
        One action at a time. Cancelling the task (Stop) stops the page operation, leaves the session usable,
        and propagates. submit_ok and expect: see the module docstring ("The submit rule", "What an approval covers")."""
        args = _validate(action, arguments)
        async with self._lock:
            started = time.monotonic()
            self.last_activity = started
            try:
                if self.state not in ("ready", "busy"):
                    if action == "close":
                        return self._refusal(None, "No browser session is open.")
                    if action != "open":
                        return self._refusal("not_open", "Not available: no browser is open. Use the open action first.")
                    refusal, _ = self._open_refusal(args["url"])
                    if refusal is not None:  # no browser is started for an address that is refused
                        return self._text(refusal)
                    try:
                        await self._start()
                    except BrowserUnavailable as exc:
                        return self._refusal("not_open", f"Not opened: browsing is not available. {exc}")
                self.state = "busy"
                draft = None
                for tab in self._tabs.values():
                    tab.dialogs = 0
                try:
                    draft = await asyncio.wait_for(self._run(action, args, submit_ok, expect), self.action_timeout)
                except (TimeoutError, self._timeout_error):
                    draft = await self._timed_out(action, args)
                except asyncio.CancelledError:
                    await self._after_cancel()
                    raise
                except self._errors as exc:
                    draft = await self._failed(action, exc)
                await self._settle_popups(started)
                await self._sweep_popups(started)
                return self._text(draft, started)
            finally:
                if self.state == "busy":
                    self.state = "ready"
                self.last_activity = time.monotonic()

    async def _settle_popups(self, started):
        """Give a popup that opened during the action a moment to make its first request, so a refusal of it is reported
        in this action's result."""
        fresh = [tab for tab in self._tabs.values() if tab.popup and tab.created >= started and not tab.closed]
        begun = time.monotonic()
        while fresh and time.monotonic() - begun < 1.6:
            waiting = [tab for tab in fresh if not tab.closed and not tab.blocked and tab.page.url in ("", "about:blank")
                       and (tab.requested or time.monotonic() - begun < 0.35)]
            if not waiting:
                return
            await asyncio.sleep(0.05)

    async def _sweep_popups(self, started):
        """Close a popup tab that never loaded anything because its first page was refused."""
        if not any(note.at >= started and note.reason in ("host_not_allowed", "private_address", "unsupported_address")
                   for note in self._notes):
            return
        orphans, self._orphans = self._orphans, 0
        for tab in sorted(self._tabs.values(), key=lambda item: item.created):
            if tab.popup and tab.page.url in ("", "about:blank") and (tab.blocked or orphans > 0 and not tab.requested):
                orphans -= 0 if tab.blocked else 1
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(tab.page.close(), 2)

    async def _failed(self, action, exc):
        message = _short_error(exc)
        if self.state == "failed":
            await self._teardown()
            return Draft(status=["The browser stopped. Use the open action to start a new session."],
                         refused="not_open", note_ok=False)
        return Draft(status=[f"The {action} action failed: {self._safe(message)}"], tab=self._active)

    async def _after_cancel(self):
        tab = self._tab()
        if tab is not None:
            with contextlib.suppress(Exception, TimeoutError):
                await asyncio.wait_for(self._stop_loading(tab), 1)

    async def _stop_loading(self, tab):
        if tab.cdp is not None:
            await tab.cdp.send("Page.stopLoading")
        else:
            await tab.page.evaluate("window.stop()")

    async def _timed_out(self, action, args):
        tab = self._tab()
        draft = Draft(status=[f"Stopped: the {action} action took longer than {self.action_timeout:g} seconds. "
                              "What the page has so far is shown below, if anything."],
                      refused="timeout", tab=tab.id if tab else None, host=self._host_of(tab))
        if tab is None:
            return draft
        with contextlib.suppress(Exception, TimeoutError):
            await asyncio.wait_for(self._stop_loading(tab), 0.5)
        if action in ("open", "read", "click", "scroll", "back", "forward", "type", "select"):
            with contextlib.suppress(Exception, TimeoutError):
                await asyncio.wait_for(self._add_outline(draft, tab), 1.0)
        return draft

    async def _run(self, action, args, submit_ok=None, expect=None):
        handler = getattr(self, f"_do_{action}")
        tab = self._tab(args.get("tab")) if action in ("read", "close", "tabs") else self._tab()
        if action in ("read", "close", "tabs") and "tab" in args and tab is None:
            return Draft(status=[f"Not found: there is no tab {self._safe(args['tab'])}. Use the tabs action to list them."])
        if action not in BOT_CHECK_OK and tab is not None and await self._has_bot_check(tab):
            return Draft(status=["Stopped: bot check. Ask the user to complete it in the browser window."],
                         refused="bot_check", tab=tab.id, host=self._host_of(tab), note_ok=False)
        if tab is None and action not in ("open", "tabs", "close"):
            return Draft(status=["Not available: no page is open. Use the open action first."], refused="not_open")
        if expect is not None and (changes := await self._changes(action, args, expect)):
            return Draft(status=[page_changed_text(changes)], refused="page_changed", tab=tab.id if tab else None,
                         host=self._host_of(tab), note_ok=False)
        if action in INTERACTIVE and tab is not None:
            self._guard = Guard(self._source_class(tab.page.url) == "loopback", submit_ok)
            try:
                draft = await handler(tab, args)
                if action in ("type", "select", "scroll"):
                    await asyncio.sleep(0.25)  # a script the action ran may send its request a moment later
                return draft
            finally:
                self._guard = None
        return await handler(tab, args)

    async def _changes(self, action, args, expect):
        """What differs between what an approval card showed (expect) and the page now: a list of phrases, empty when
        nothing does. Checked inside the action's lock, right before it acts."""
        return page_changes(await self.preview(action, **args), expect, args.get("ref"), self._line)

    async def _has_bot_check(self, tab):
        try:
            return bool(await tab.page.evaluate(BOT_JS))
        except Exception:
            return False

    async def _settle(self, page):
        """Wait for the page to finish loading (briefly) and for scripts that run at once."""
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=3_000)
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("load", timeout=3_000)
        await asyncio.sleep(0.15)

    async def _outline(self, tab, *, viewport=False, fresh=False):
        """(lines, data): the outline lines with refs, and the script's other facts. Sets the tab's refs. An
        element keeps its ref while its document stays (a click on the same page does not renumber); fresh=True
        (the read action) starts new numbers, so every earlier ref is stale."""
        page = tab.page
        if fresh or tab.registry is None:
            tab.registry = await page.evaluate_handle("(next) => ({ids: new WeakMap(), next})", self._ref_next)
        handle = await page.evaluate_handle(OUTLINE_JS, {"maxChars": self.kept_chars, "maxControls": MAX_CONTROLS,
                                                         "viewport": viewport, "registry": tab.registry,
                                                         "floor": self._ref_next})
        try:
            data = await handle.evaluate("w => w.data")
            elements = await (await handle.get_property("elements")).get_properties()
        finally:
            with contextlib.suppress(Exception):
                await handle.dispose()
        old = [element for element, _ in tab.refs.values()]
        tab.refs = {}
        for index, control in enumerate(data["controls"]):
            element = elements.get(str(index))
            if element is not None and element.as_element() is not None:
                tab.refs[f"r{control['id']}"] = (element.as_element(), control)
        self._ref_next = max(self._ref_next, data["next"])  # a ref is never reused in another tab
        if old:
            await asyncio.gather(*(item.dispose() for item in old), return_exceptions=True)

        def expand(line):
            return re.sub("\ue000(\\d+)\ue001", lambda match: f"[r{data['controls'][int(match.group(1))]['id']}] "
                          + data["controls"][int(match.group(1))]["line"], line)
        return [neutralise(self.redact(expand(line)), header=True) for line in data["lines"]], data

    async def _add_outline(self, draft, tab, *, viewport=False, fresh=False):
        title = await tab.page.title()
        lines, data = await self._outline(tab, viewport=viewport, fresh=fresh)
        draft.body = [f"Page: {self._line(title)}", f"URL: {self._safe(tab.page.url)}", f"Tab: {tab.id}"]
        if viewport:
            scroll = data["scroll"]
            draft.body.append(f"Scroll: {scroll['y']} of {max(0, scroll['height'] - scroll['view'])} px")
        draft.body.extend(lines or ["(The page shows no text.)"])
        if data["truncated"]:
            draft.body.append("[The outline was cut: the page is longer than the limit.]")
        draft.tab = tab.id
        draft.host = draft.host or self._host_of(tab)
        if data["bot"]:
            draft.refused = draft.refused or "bot_check"
            draft.status.append("Stopped: bot check. Ask the user to complete it in the browser window.")
        return data

    def _open_refusal(self, url):
        """(Draft, None) when the address is refused or not allowed, before any lookup; else (None, Address)."""
        try:
            address = parse_address(url)
        except AddressRefused as exc:
            label = exc.reason.replace("_", " ")
            return Draft(status=[f"Not opened: {label}. {self._safe(str(exc))}"], refused=exc.reason,
                         note_ok=False), None
        if not self.allowed(address.key):
            return Draft(status=[f"Not opened: {address.key} is not allowed yet. The user must allow it first."],
                         refused="host_not_allowed", host=address.key, note_ok=False), None
        return None, address

    async def _do_open(self, tab, args):
        refusal, address = self._open_refusal(args["url"])
        if refusal is not None:
            return refusal
        self._classes.pop(address.host, None)  # a top-level navigation looks the name up again
        target, detail = await self._classify(address)
        if reason := verdict(target, "blank", top_level=True, allowed=True, engine=True):
            label = reason.replace("_", " ")
            return Draft(status=[f"Not opened: {label}. {self._safe(detail)}"], refused=reason, host=address.key,
                         note_ok=False)
        if args.get("new_tab") or tab is None:
            if len(self._tabs) >= MAX_TABS:
                return Draft(status=[f"Not opened: {MAX_TABS} tabs are already open. Close one first."],
                             refused="tab_limit", host=address.key, note_ok=False)
            tab = await self._new_tab()
        self._active = tab.id
        tab.engine_pending = True
        timeout = self.action_timeout * 1000
        failure = None
        try:
            await tab.page.goto(address.url, wait_until="domcontentloaded", timeout=timeout)
        except self._errors as exc:
            if isinstance(exc, self._timeout_error):
                raise
            failure = exc
        finally:
            tab.engine_pending = False
        if failure is None:
            await self._settle(tab.page)
        draft = Draft(host=address.key)
        if failure is not None and not any(note.reason in ("host_not_allowed", "download", "private_address")
                                           for note in self._notes):
            message = _short_error(failure)
            if "Download is starting" not in message:
                draft.status.append(f"Not opened: {self._safe(message)}")
        await self._add_outline(draft, tab)
        return draft

    async def _new_tab(self):
        self._creating = True
        try:
            page = await self._context.new_page()
        finally:
            self._creating = False
        for _ in range(100):
            tab = self._by_page.get(page)
            if tab is not None:
                await asyncio.wait_for(tab.ready.wait(), 5)
                return tab
            await asyncio.sleep(0.02)
        raise self._errors[0]("The new tab could not be registered.")

    async def _do_read(self, tab, args):
        draft = Draft()
        await self._add_outline(draft, tab, fresh=True)
        if draft.refused == "bot_check":
            draft.refused = None  # reading is allowed; the note says why other actions stop
        self._active = tab.id
        return draft

    def _ref(self, tab, ref):
        """(element handle, meta) for a ref, or None when it is not valid (stale)."""
        return tab.refs.get(ref)

    def _owner(self, tab, ref):
        """The other tab that holds a ref, if any: a ref belongs to the tab where it was read."""
        return next((other for other in self._tabs.values() if other is not tab and ref in other.refs), None)

    def _stale(self, tab, ref, verb):
        owner = self._owner(tab, ref)
        why = (f"the reference {self._safe(ref)} belongs to tab {owner.id}, not to the active tab {tab.id}. Use the tabs "
               f"action to switch to {owner.id}, or read this page again" if owner is not None else
               f"the reference {self._safe(ref)} is stale. Read the page again to get new references")
        return Draft(status=[f"Not {verb}: {why}."], refused="stale_ref", tab=tab.id, host=self._host_of(tab), note_ok=False)

    async def _do_find(self, tab, args):
        query = args["query"].strip().lower()
        draft = Draft()
        await self._add_outline(draft, tab)
        found = []
        for ref, (_, meta) in tab.refs.items():
            if query and query in f"{meta['role']} {meta['name']} {meta['text']}".lower():
                found.append(f"[{ref}] {self._safe(meta['line'])}")
            if len(found) >= FIND_LIMIT:
                break
        draft.body = [f"Found {len(found)} control(s) matching {self._safe(args['query'])!r}"
                      + (f" (the first {FIND_LIMIT})" if len(found) >= FIND_LIMIT else "") + ".", *found]
        draft.refused = None if draft.refused == "bot_check" else draft.refused
        return draft

    async def _do_click(self, tab, args):
        entry = self._ref(tab, args["ref"])
        if entry is None:
            return self._stale(tab, args["ref"], "clicked")
        element = entry[0]
        before = tab.page.url
        pages_before = set(self._tabs)
        new_tab_target = False
        with contextlib.suppress(Exception):
            new_tab_target = await asyncio.wait_for(element.evaluate(NEW_TAB_TARGET_JS), 1)
        try:
            await element.click(timeout=self.action_timeout * 1000 - 500, no_wait_after=True)
        except self._errors as exc:
            if isinstance(exc, self._timeout_error):
                raise
            if re.search(r"not attached|detached|destroyed", str(exc), re.IGNORECASE):
                return self._stale(tab, args["ref"], "clicked")
            return Draft(status=[f"Not clicked: {self._safe(_short_error(exc))}"], tab=tab.id)
        await self._settle(tab.page)
        # A link or form that targets a new tab: its tab can register after the settle on a busy machine. Wait for
        # it briefly, so this result names it. A tab that comes later still gets its note.
        waited = time.monotonic()
        while new_tab_target and set(self._tabs) == pages_before and time.monotonic() - waited < 1.0:
            await asyncio.sleep(0.05)
        draft = Draft(host=self._host_of(tab))
        after = tab.page.url
        draft.status.append(f"Clicked {args['ref']}: " + (f"the page is now {self._safe(after)}" if after != before
                                                           else "the same page."))
        for new in set(self._tabs) - pages_before:
            draft.status.append(f"A new tab {new} opened: {self._safe(self._tabs[new].page.url)}")
        await self._add_outline(draft, tab)
        return draft

    async def _do_type(self, tab, args):
        entry = self._ref(tab, args["ref"])
        if entry is None:
            return self._stale(tab, args["ref"], "typed")
        element = entry[0]
        loopback = self._source_class(tab.page.url) == "loopback"
        try:
            result = await element.evaluate(TYPE_JS, {"loopback": loopback, "text": args["text"]})
        except self._errors as exc:
            if isinstance(exc, self._timeout_error):
                raise
            if not tab.refs:  # the typing made the page navigate
                return await self._navigated(tab, f"Typed into {args['ref']}")
            return self._stale(tab, args["ref"], "typed")
        if result.get("error") == "stale":
            return self._stale(tab, args["ref"], "typed")
        if result.get("refused"):
            return Draft(status=["Not typed: credential field. Ask the user to fill it in the browser window.",
                                 f"The field is {self._safe(result['refused'])}."], refused="credential_field",
                         tab=tab.id, host=self._host_of(tab), note_ok=False)
        if result.get("error"):
            return Draft(status=[f"Not typed: {self._safe(result['error'])}."], tab=tab.id, host=self._host_of(tab))
        draft = Draft(tab=tab.id, host=self._host_of(tab))
        draft.status.append(f"Typed into {args['ref']}. The field is now: {self._safe(result['line'])}")
        if args.get("submit"):
            before = tab.page.url
            await element.press("Enter", timeout=self.action_timeout * 1000 - 500)
            await self._settle(tab.page)
            draft.status.append("Pressed Enter: " + (f"the page is now {self._safe(tab.page.url)}"
                                                      if tab.page.url != before else "the same page."))
            await self._add_outline(draft, tab)
        return draft

    async def _navigated(self, tab, what):
        """The result of an action that made the page navigate before its script could answer."""
        await self._settle(tab.page)
        draft = Draft(tab=tab.id, host=self._host_of(tab))
        draft.status.append(f"{what}: the page is now {self._safe(tab.page.url)}")
        await self._add_outline(draft, tab)
        return draft

    async def _do_select(self, tab, args):
        entry = self._ref(tab, args["ref"])
        if entry is None:
            return self._stale(tab, args["ref"], "selected")
        element = entry[0]
        loopback = self._source_class(tab.page.url) == "loopback"
        try:
            result = await element.evaluate(SELECT_JS, {"loopback": loopback, "value": args["value"]})
        except self._errors as exc:
            if isinstance(exc, self._timeout_error):
                raise
            if not tab.refs:  # the choice made the page navigate
                return await self._navigated(tab, f"Selected in {args['ref']}")
            return self._stale(tab, args["ref"], "selected")
        if result.get("error") == "stale":
            return self._stale(tab, args["ref"], "selected")
        if result.get("refused"):
            return Draft(status=["Not selected: credential field. Ask the user to fill it in the browser window.",
                                 f"The field is {self._safe(result['refused'])}."], refused="credential_field",
                         tab=tab.id, host=self._host_of(tab), note_ok=False)
        if result.get("error"):
            return Draft(status=[f"Not selected: {self._safe(result['error'])}."], tab=tab.id)
        if result.get("missing"):
            return Draft(status=[f"Not selected: the control has no option {self._safe(args['value'])!r}. "
                                 f"It is now: {self._safe(result['line'])}"], tab=tab.id)
        await asyncio.sleep(0.15)
        return Draft(status=[f"Selected in {args['ref']}: {self._safe(result['line'])}"], tab=tab.id, host=self._host_of(tab))

    async def _do_scroll(self, tab, args):
        page = tab.page
        if "ref" in args:
            entry = self._ref(tab, args["ref"])
            if entry is None:
                return self._stale(tab, args["ref"], "scrolled")
            try:
                await entry[0].scroll_into_view_if_needed(timeout=self.action_timeout * 1000 - 500)
            except self._errors as exc:
                if isinstance(exc, self._timeout_error):
                    raise
                return self._stale(tab, args["ref"], "scrolled")
        else:
            script = {"up": "window.scrollBy({top: -innerHeight * 0.8, behavior: 'instant'})",
                      "down": "window.scrollBy({top: innerHeight * 0.8, behavior: 'instant'})",
                      "top": "window.scrollTo({top: 0, behavior: 'instant'})",
                      "bottom": "window.scrollTo({top: document.documentElement.scrollHeight, behavior: 'instant'})"}
            await page.evaluate(script[args["direction"]])
        await asyncio.sleep(0.15)
        draft = Draft(host=self._host_of(tab))
        await self._add_outline(draft, tab, viewport=True)
        return draft

    async def _history(self, tab, going_back):
        before = tab.page.url
        tab.engine_pending = True
        try:
            method = tab.page.go_back if going_back else tab.page.go_forward
            response = await method(wait_until="domcontentloaded", timeout=self.action_timeout * 1000 - 500)
        except self._errors as exc:
            if isinstance(exc, self._timeout_error):
                raise
            response = None
        finally:
            tab.engine_pending = False
        if going_back and tab.page.url == "about:blank" and before != "about:blank":
            response = None  # the tab's start page is not a page to go back to
            tab.engine_pending = True
            with contextlib.suppress(*self._errors):
                await tab.page.go_forward(wait_until="domcontentloaded", timeout=self.action_timeout * 1000 - 500)
            tab.engine_pending = False
        await self._settle(tab.page)
        draft = Draft(host=self._host_of(tab), tab=tab.id)
        if response is None and tab.page.url == before:
            draft.status.append(f"There is no {'earlier' if going_back else 'later'} page in this tab's history.")
        title = await tab.page.title()
        draft.body = [f"Page: {self._line(title)}", f"URL: {self._safe(tab.page.url)}", f"Tab: {tab.id}"]
        if tab.page.url != before and await self._has_bot_check(tab):
            draft.status.append("Stopped: bot check. Ask the user to complete it in the browser window.")
            draft.refused = "bot_check"
        return draft

    async def _do_back(self, tab, args):
        return await self._history(tab, True)

    async def _do_forward(self, tab, args):
        return await self._history(tab, False)

    def _tab_lines(self):
        lines = []
        for tab in list(self._tabs.values()):
            mark = "*" if tab.id == self._active else " "
            lines.append(f"{mark} {tab.id}  {self._safe(tab.page.url)}")
        return lines or ["(no tabs are open)"]

    async def _do_tabs(self, tab, args):
        if "tab" in args:
            self._active = tab.id
            with contextlib.suppress(Exception):
                await tab.page.bring_to_front()
        titles = []
        for item in list(self._tabs.values()):
            with contextlib.suppress(Exception):
                titles.append(f"{'*' if item.id == self._active else ' '} {item.id}  "
                              f"{self._line(await item.page.title())!r}  {self._safe(item.page.url)}")
        draft = Draft(body=titles or ["(no tabs are open)"], tab=self._active, host=self._host_of(tab))
        if "tab" in args:
            draft.status.append(f"Switched to tab {tab.id}.")
        draft.body.insert(0, "Tabs (* marks the active one):")
        return draft

    async def _do_close(self, tab, args):
        if "tab" in args:
            tab.engine_pending = True  # the app closes it: a beforeunload dialog does not keep the tab
            await tab.page.close()
            for _ in range(20):
                if tab.closed:
                    break
                await asyncio.sleep(0.02)
            return Draft(status=[f"Closed tab {tab.id}."], body=["Tabs (* marks the active one):", *self._tab_lines()],
                         note_ok=False)
        self.state = "closed"
        await self._teardown()
        return Draft(status=["The browser session is closed."], note_ok=False)

    async def _do_console(self, tab, args):
        rows = [row for row in tab.console if not args.get("errors_only") or row[0] in ("error", "pageerror")]
        lines = [f"[{kind}] {text}" + (f" ({where})" if where else "") for kind, text, where in rows]
        return Draft(body=[f"Console of tab {tab.id}" + (" (errors only)" if args.get("errors_only") else "")
                           + f": {len(lines)} message(s).", *lines], tab=tab.id, host=self._host_of(tab))

    async def _do_network(self, tab, args):
        lines = []
        for entry in tab.network:
            failed = bool(entry["failure"]) or bool(entry["blocked"]) or (entry["status"] or 0) >= 400
            if args.get("errors_only") and not failed:
                continue
            state = ("BLOCKED (" + entry["blocked"] + ")" if entry["blocked"] else
                     "FAILED " + entry["failure"] if entry["failure"] else str(entry["status"] or "pending"))
            lines.append(f"{entry['method']} {self._safe(entry['url'])[:300]} -> {state} [{entry['type']}]")
        return Draft(body=[f"Network of tab {tab.id}" + (" (failures only)" if args.get("errors_only") else "")
                           + f": {len(lines)} request(s).", *lines], tab=tab.id, host=self._host_of(tab))

    async def _do_screenshot(self, tab, args):
        page = tab.page
        element = None
        if "ref" in args:
            entry = self._ref(tab, args["ref"])
            if entry is None:
                return self._stale(tab, args["ref"], "captured")
            element = entry[0]
        with contextlib.suppress(Exception):
            await page.bring_to_front()
        try:
            data, mime, what, notes = await self._capture(tab, element, bool(args.get("full_page")))
        except self._errors as exc:
            if isinstance(exc, self._timeout_error):
                raise
            return Draft(status=[f"Not captured: {self._safe(_short_error(exc))}"], tab=tab.id)
        size = image_size(data) or (0, 0)
        return Draft(status=[f"Screenshot of {what}: {size[0]}x{size[1]} pixels, {mime.split('/')[1].upper()}, "
                             f"{len(data) // 1024} KB." + "".join(" " + note for note in notes)], tab=tab.id,
                     host=self._host_of(tab), screenshot=data, screenshot_size=size, screenshot_mime=mime)

    async def _capture(self, tab, element, full_page):
        """(image bytes, mime, what it shows, notes). A full-page capture is cut at MAX_FULL_PAGE_VIEWPORTS viewport
        heights, and a capture scaled down to MAX_SIDE says "reduced from WxH"; both are said in the notes."""
        page = tab.page
        notes = []
        cut = False
        if element is not None:
            await element.scroll_into_view_if_needed(timeout=5_000)
            box = await element.bounding_box()
            if not box:
                raise self._errors[0]("The element is not visible.")
            width, height, what = box["width"], box["height"], "the element"
        elif full_page:
            width, height = await page.evaluate("[document.documentElement.scrollWidth, document.documentElement.scrollHeight]")
            what = "the full page"
            view = page.viewport_size or {"width": 1280, "height": 800}
            limit = MAX_FULL_PAGE_VIEWPORTS * view["height"]
            if height > limit:
                what, cut = f"the full page, cut to its first {limit:,} of {height:,} px", True
                height = limit
        else:
            view = page.viewport_size or {"width": 1280, "height": 800}
            width, height, what = view["width"], view["height"], "the viewport"
        scale = min(1.0, MAX_SIDE / max(width, height, 1))
        data = mime = None
        used = scale
        for fmt, quality, factor in (("png", None, 1.0), ("jpeg", 85, 1.0), ("jpeg", 70, 0.8), ("jpeg", 50, 0.6)):
            used = scale * factor
            data = await self._shoot(tab, element, full_page, fmt, quality, used, width, height, cut)
            mime = f"image/{fmt}"
            if len(data) <= MAX_IMAGE_BYTES:
                break
        if used < 0.999:
            notes.append(f"(reduced from {round(width)}x{round(height)})")
        return data, mime, what, notes

    async def _shoot(self, tab, element, full_page, fmt, quality, scale, width, height, cut=False):
        page = tab.page
        if scale >= 1.0 and not cut:
            options = {"type": fmt, "timeout": self.action_timeout * 1000 - 500}
            if quality:
                options["quality"] = quality
            if element is not None:
                return await element.screenshot(**options)
            return await page.screenshot(full_page=full_page, **options)
        metrics = await tab.cdp.send("Page.getLayoutMetrics")
        view = metrics["cssVisualViewport"]
        if element is not None:
            box = await element.bounding_box()
            clip = {"x": view["pageX"] + box["x"], "y": view["pageY"] + box["y"], "width": box["width"],
                    "height": box["height"], "scale": scale}
        elif full_page:
            clip = {"x": 0, "y": 0, "width": width, "height": height, "scale": scale}
        else:
            clip = {"x": view["pageX"], "y": view["pageY"], "width": view["clientWidth"],
                    "height": view["clientHeight"], "scale": scale}
        params = {"format": fmt, "clip": clip, "captureBeyondViewport": bool(full_page or element is not None)}
        if quality:
            params["quality"] = quality
        shot = await tab.cdp.send("Page.captureScreenshot", params)
        return base64.b64decode(shot["data"])

    # preview

    async def preview(self, action, **arguments):
        """What an action would touch, without doing it: host, tab, submits, label and refused. text is empty unless the
        action would be refused. label is what the ref names now (role and name), so the owner can show it on a card and
        pass it back as act(expect=...). The owner calls this to decide about an approval card, then calls act."""
        args = _validate(action, arguments)
        tab = self._tab(args.get("tab")) if action in ("read", "close", "tabs") else self._tab()
        result = BrowserResult("", tab=tab.id if tab else None)
        if action == "open":
            try:
                address = parse_address(args["url"])
            except AddressRefused as exc:
                result.refused, result.text = exc.reason, f"Not opened: {exc.reason.replace('_', ' ')}. {exc}"
                return result
            result.host = address.key
            if not self.allowed(address.key):
                result.refused, result.text = "host_not_allowed", f"{address.key} is not allowed yet."
            return result
        if self.state not in ("ready", "busy") or tab is None:
            if action != "close":
                result.refused, result.text = "not_open", "No page is open."
            return result
        result.host = self._host_of(tab)
        loopback = self._source_class(tab.page.url) == "loopback"
        if action in ("click", "type", "select", "scroll", "screenshot") and "ref" in args:
            entry = self._ref(tab, args["ref"])
            if entry is None:
                owner = self._owner(tab, args["ref"])
                result.refused = "stale_ref"
                result.text = (f"The reference belongs to tab {owner.id}, not to the active tab. Use the tabs action." if owner
                               else "The reference is stale. Read the page again.")
                return result
            element = entry[0]
            try:
                found = await element.evaluate(IDENT_JS)
                result.label = found["label"]
                if action == "click" and not loopback:
                    result.submits = bool(await element.evaluate(SUBMIT_JS))
                if action == "type":
                    result.submits = bool(args.get("submit")) and not loopback
                if action in ("type", "select") and not loopback:
                    if why := credential_reason(found["attrs"]):
                        verb = "typed" if action == "type" else "selected"
                        result.refused, result.text = "credential_field", f"Not {verb}: credential field ({why})."
            except self._errors:
                result.refused, result.text = "stale_ref", "The reference is stale. Read the page again."
        return result
