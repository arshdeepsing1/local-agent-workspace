import asyncio
import errno
import os
import shutil
import signal

import pytest


@pytest.fixture(autouse=True)
def isolated_defaults(monkeypatch):
    """No default code folder or Python environment from the developer's environment, and a
    model without a built-in profile (131,072 and 8,192). Tests of the default model delenv
    LOCAL_AGENT_MODEL."""
    monkeypatch.setenv("LOCAL_AGENT_CODING_DIR", "")
    monkeypatch.setenv("LOCAL_AGENT_PYTHON_VENV", "")
    monkeypatch.setenv("LOCAL_AGENT_MODEL", "test-endpoint")


@pytest.fixture(scope="session")
def empty_home(tmp_path_factory):
    return tmp_path_factory.mktemp("home")


@pytest.fixture(autouse=True)
def isolated_user_folders(monkeypatch, empty_home):
    """The user's own skill and command folders (~/.agents, ~/.claude) are read from an empty folder, never
    the developer's home. Tests of those folders set extensions.USER_HOME to their own."""
    from local_agent import extensions
    monkeypatch.setattr(extensions, "USER_HOME", empty_home)


@pytest.fixture(autouse=True)
def empty_user_home(tmp_path_factory, monkeypatch):
    """The user's instruction files (~/.agents/AGENTS.md, ~/.claude/CLAUDE.md) and output styles come from an
    empty folder, not the developer's home. Tests of those files write into instructions.user_home()."""
    from local_agent import instructions
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(instructions, "user_home", lambda: home)


@pytest.fixture(autouse=True)
def no_ripgrep(monkeypatch):
    """Search without ripgrep by default, so results do not depend on whether it is
    installed. Tests that need it use real_ripgrep."""
    from local_agent import search
    monkeypatch.setattr(search, "ripgrep", lambda: None)


@pytest.fixture(autouse=True)
def mac_shell_prompt(monkeypatch):
    """A system prompt that names zsh, as on a Mac, whatever shell this machine has, so that a
    test near a token threshold gives the same result on every runner. Commands still run with
    this machine's shell; tests of the prompt's own choice patch agents.shell_note back."""
    from local_agent import agents, jobs
    note = " ".join((jobs.SHELL_NOTES["/bin/zsh"], jobs.POLLING_NOTE))
    monkeypatch.setattr(agents, "shell_note", lambda: note)


@pytest.fixture
def real_ripgrep(monkeypatch):
    """Search with the installed rg; the test is skipped when rg is not installed."""
    from local_agent import search
    rg = shutil.which("rg")
    if not rg:
        pytest.skip("ripgrep (rg) is not installed")
    monkeypatch.setattr(search, "ripgrep", lambda: rg)
    return rg


@pytest.fixture(params=["as-is", "eperm"])
def killpg_answer(request, monkeypatch):
    """Run a test twice: with os.killpg as it is, and as macOS answers when a group's
    processes have all exited but are not yet reaped (EPERM instead of ESRCH).

    The EPERM variant still sends the signal, so a test can also check that nothing
    the command started survives.
    """
    if request.param == "eperm":
        killpg = os.killpg

        def not_permitted(pid, sig):
            try:
                killpg(pid, sig)
            except ProcessLookupError:
                pass
            raise PermissionError(errno.EPERM, "Operation not permitted")
        monkeypatch.setattr(os, "killpg", not_permitted)
    return request.param


@pytest.fixture
async def unkillable_group(monkeypatch):
    """Make os.killpg answer EPERM without signalling, as for a command whose running
    processes all belong to another user (for example through sudo), and shorten
    the grace periods after SIGTERM and SIGKILL. The groups are killed for real afterwards."""
    from local_agent import jobs
    killpg = os.killpg
    groups = set()

    def not_permitted(pid, sig):
        groups.add(pid)
        raise PermissionError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(os, "killpg", not_permitted)
    monkeypatch.setattr(jobs, "KILL_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(jobs, "TERM_GRACE_SECONDS", 0.2)
    yield groups
    for pid in groups:
        try:
            killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    await asyncio.sleep(0.2)  # Let the event loop reap them before it closes.


# --- Saved chats written by the code before the upgrade (fixtures/pre_upgrade/README.md) ---------------------------

from dataclasses import dataclass  # noqa: E402  (this block only adds helpers; the imports above stay as they were)
from pathlib import Path  # noqa: E402

PRE_UPGRADE_FOLDER = Path(__file__).resolve().parent / "fixtures" / "pre_upgrade"
PRE_UPGRADE_FILES = {"parent": "parent.jsonl", "child_a": "child_a.jsonl", "child_b": "child_b.jsonl",
                     "compacted": "compacted.jsonl"}


@dataclass
class PreUpgrade:
    """The current Store over a state folder holding the four fixture chats, and their IDs."""
    store: object
    state: Path
    parent: str
    child_a: str
    child_b: str
    compacted: str

    @property
    def ids(self):
        return [self.parent, self.child_a, self.child_b, self.compacted]

    def file(self, session_id):
        return self.store.conversations / f"{session_id}.jsonl"


@pytest.fixture
def pre_upgrade_store(tmp_path):
    """A fresh state folder laid out as the current Store expects (<state>/conversations/<id>.jsonl) with the
    chats the code of commit e59f328 saved (parent with two ended subagent children, and a compacted chat),
    and a current Store opened on it. The conversation IDs are fixed strings, the workspace is /project."""
    import json
    from local_agent.store import Store
    state = tmp_path / "pre-upgrade-state"
    ids = {}
    (state / "conversations").mkdir(parents=True, mode=0o700)
    for name, file in PRE_UPGRADE_FILES.items():
        source = PRE_UPGRADE_FOLDER / file
        ids[name] = json.loads(source.read_text(encoding="utf-8").splitlines()[0])["data"]["id"]
        target = state / "conversations" / f"{ids[name]}.jsonl"
        shutil.copyfile(source, target)
        target.chmod(0o600)
    store = Store(state / "sessions.db")
    yield PreUpgrade(store=store, state=state, **ids)
    store.db.close()


# --- A loopback page server for the browser tests (fixtures/browser_pages) ----------------------------------------

import html as _html  # noqa: E402  (this block only adds the browser_pages fixture; nothing above changed)
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from urllib.parse import parse_qs, urlsplit  # noqa: E402

BROWSER_PAGES = Path(__file__).resolve().parent / "fixtures" / "browser_pages"
PIXEL = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63fcffff3f"
                      "0005fe02fea7d6a2f30000000049454e44ae426082")


class PageServer:
    """One threaded http.server on a loopback port, serving fixtures/browser_pages. `requests` lists every request
    as (method, path, host), where host is the Host header without its port; count() filters them. HTML files fill
    {{PORT}}, {{OTHER_PORT}} (the port of the other server), {{LONG}}, {{QUERY_LIST}} and {{ECHO}}. Also served:
    /redirect?to=, /file.bin (an attachment), /secret.txt (with CORS), /pixel.png, /ping and /ws (both count only),
    /cdn-cgi/... (a stand-in for a challenge frame) and slow.html, which waits 20 seconds."""

    def __init__(self, owner):
        self.owner = owner
        self.requests = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body=b"", content_type="text/html; charset=utf-8", headers=()):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower() if "]" not in (
                    self.headers.get("Host") or "") else self.headers.get("Host", "")
                server.requests.append((self.command, parts.path, host))
                path = parts.path
                if path == "/redirect":
                    return self.reply(302, headers=[("Location", (query.get("to") or ["/"])[0])])
                if path == "/file.bin":
                    return self.reply(200, b"binary", "application/octet-stream",
                                      [("Content-Disposition", 'attachment; filename="report.bin"')])
                if path == "/secret.txt":
                    return self.reply(200, b"SECRET-VALUE", "text/plain", [("Access-Control-Allow-Origin", "*")])
                if path == "/pixel.png":
                    return self.reply(200, PIXEL, "image/png", [("Access-Control-Allow-Origin", "*")])
                if path in ("/ping", "/favicon.ico", "/ws"):
                    return self.reply(204 if path == "/ping" else 404)
                if path.startswith("/cdn-cgi/"):
                    return self.reply(200, b"<html><body>challenge</body></html>")
                if path == "/slow.html":
                    server.owner.stop_event.wait(20)
                name = path.lstrip("/") or "index.html"
                target = (BROWSER_PAGES / name).resolve()
                if BROWSER_PAGES not in target.parents or not target.is_file():
                    return self.reply(404, b"<html><body>Not found</body></html>")
                body = target.read_text(encoding="utf-8") if target.suffix == ".html" else None
                if body is None:
                    kind = "application/javascript" if target.suffix == ".js" else "application/octet-stream"
                    return self.reply(200, target.read_bytes(), kind)
                items = "".join(f"<li>{_html.escape(key)}={_html.escape(value[0])}</li>" for key, value in query.items())
                long_text = "".join(f"<p>Paragraph {number}: the quick brown fox jumps over the lazy dog, again and "
                                    f"again, so that this page is much longer than one first page.</p>\n"
                                    for number in range(1, 301))
                for key, value in (("{{PORT}}", str(server.port)), ("{{OTHER_PORT}}", str(server.owner.other_port(server))),
                                   ("{{LONG}}", long_text), ("{{QUERY_LIST}}", items),
                                   ("{{ECHO}}", _html.escape((query.get("text") or [""])[0]))):
                    body = body.replace(key, value)
                self.reply(200, body.encode("utf-8"))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path="/", host="127.0.0.1"):
        return f"http://{host}:{self.port}/{path.lstrip('/')}"

    def count(self, path=None, host=None):
        """How many requests arrived, optionally for a path (a prefix) and a Host header (without its port)."""
        return sum(1 for _, seen_path, seen_host in self.requests
                   if (path is None or seen_path.startswith(path)) and (host is None or seen_host == host))

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class BrowserPages:
    """The browser_pages fixture: the main PageServer (.url(path), .port, .count()), and start_second() for a
    second server on another loopback port (.second)."""

    def __init__(self):
        self.stop_event = threading.Event()
        self.main = PageServer(self)
        self.second = None

    @property
    def port(self):
        return self.main.port

    @property
    def requests(self):
        return self.main.requests

    def url(self, path="/", host="127.0.0.1"):
        return self.main.url(path, host)

    def count(self, path=None, host=None):
        return self.main.count(path, host)

    def start_second(self):
        if self.second is None:
            self.second = PageServer(self)
        return self.second

    def other_port(self, server):
        other = self.second if server is self.main else self.main
        return other.port if other is not None else 0

    def stop(self):
        self.stop_event.set()
        for server in (self.main, self.second):
            if server is not None:
                server.stop()


@pytest.fixture
def browser_pages():
    pages = BrowserPages()
    yield pages
    pages.stop()
