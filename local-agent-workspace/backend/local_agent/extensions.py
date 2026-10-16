import copy
import itertools
import json
import os
import re
import shlex
import stat
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from . import config
from .jobs import CREDENTIAL_ENV, DENIED_ENV, DENIED_ENV_PREFIXES, NOT_STOPPED, CommandTimeout, run_process, with_note
from .mcp_client import MCPConnection, bounded_error, bounded_result
from .redaction import redact_secrets

CONFIG_LIMIT = 32_000
SKILL_LIMIT = 8_000
MAX_SKILLS = 30
IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,19}\Z")
SKILL_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
TOOL_SELECTOR = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
# Workspace skill folders in order of precedence; the app only reads .claude/skills.
WORKSPACE_SKILL_ROOTS = (".agents/skills", ".claude/skills")
SKILL_INDEX_ENTRIES = 30
SKILL_INDEX_BYTES = 2_000
SKILL_INDEX_DESCRIPTION = 150
MAX_SERVERS = 12  # configured
MAX_ENABLED_SERVERS = 4
MAX_HOOKS = 16
# Environment variables (stdio) and HTTP headers of one server: names, values given as text or as
# a reference {"secret": KEY} to an assignment in the credential file, resolved at each connect.
MAX_SERVER_VALUES = 16
SERVER_VALUE_CHARS = 4_096
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}\Z")
SECRET_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
HEADER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}\Z")
# Never set for a server: what changes how its program or shell starts, and the app's own credentials.
DENIED_SERVER_ENV = DENIED_ENV | CREDENTIAL_ENV | {"PATH", "PYTHONPATH", "PYTHONHOME"}
DENIED_SERVER_ENV_PREFIXES = (*DENIED_ENV_PREFIXES, "PYTHON", "DBRICKS_", "DATABRICKS_", "LOCAL_AGENT_")
# Set by the HTTP transport itself.
DENIED_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "upgrade", "te", "mcp-session-id",
                  "mcp-protocol-version"}
# Parts of a name that say its value is a secret: such a value must be a reference.
SECRET_WORDS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "APIKEY", "API_KEY", "CREDENTIAL", "COOKIE", "AUTHORIZATION",
                "OAUTH")
SECRET_PARTS = {"KEY", "PAT", "PASS", "AUTH", "BEARER"}
LOOPBACK = ("localhost", "127.0.0.1", "::1")
SECRET_HINT = 'use {{"secret": "{name}"}} and add {name}=… to your credential file'
# Hook events: the three tool events (their tools field selects tools) and the lifecycle events.
TOOL_HOOK_EVENTS = ("before_tool", "after_tool", "tool_failure")
HOOK_EVENTS = (*TOOL_HOOK_EVENTS, "user_prompt", "session_start", "agent_stop", "subagent_start", "subagent_stop")
HOOK_EVENT_LIST = ", ".join(HOOK_EVENTS[:-1]) + " or " + HOOK_EVENTS[-1]
# Structured hook output: what one JSON object on stdout may set, per event. decision values are
# listed; other fields are text, except before_tool arguments (an object). Unknown fields are ignored.
HOOK_OUTPUT_FIELDS = {
    "before_tool": {"decision": ("allow", "deny", "ask"), "reason": str, "arguments": dict, "context": str},
    "after_tool": {"context": str},
    "tool_failure": {"context": str},
    "user_prompt": {"decision": ("allow", "block"), "reason": str, "prompt": str, "context": str},
    "session_start": {"context": str},
    "agent_stop": {"decision": ("allow", "block"), "reason": str},
    "subagent_start": {"context": str},
    "subagent_stop": {"decision": ("allow", "block"), "reason": str, "report": str},
}
HOOK_CONTEXT_CHARS = 2_000  # context and reason are shortened to this many characters
HOOK_REPORT_CHARS = 6_000
HOOK_STDOUT_BYTES = 16_384  # stdout kept from a hook; more is shown as text, never read as JSON
HOOK_SHOWN_BYTES = 3_800  # the part of stdout and stderr a hook card and a warning show
STDERR_LABEL = "[stderr]\n"  # stderr follows stdout under this line on a card; it is never read as JSON
SHORTENED = " […shortened by the app]"


def _clip_hook_text(value, limit):
    value = value.strip()
    return value if len(value) <= limit else value[:limit - len(SHORTENED)].rstrip() + SHORTENED


def _redact_tree(value, redact):
    """value with redact applied to every string (keys too) inside objects and lists."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {redact(key): _redact_tree(item, redact) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_tree(item, redact) for item in value]
    return value


def _escaped_secrets(text, redact):
    """text (stdout that may be one JSON object) re-encoded with its decoded strings redacted, when a
    JSON escape spelled a secret that redacting the raw text missed; else None."""
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        value = json.loads(stripped)
        cleaned = _redact_tree(value, redact)
    except (ValueError, RecursionError):
        return None
    return json.dumps(cleaned, ensure_ascii=False) if cleaned != value else None


def parse_hook_output(event, text, truncated=False, redact=None):
    """The structured part of a hook's stdout (exit code 0), or None for plain text.

    Only when the whole trimmed output is one JSON object is it read; anything else
    (text, a list, cut-off JSON) is plain text, as before. Returns {decision?, reason?,
    context?, arguments?, prompt?, report?, ignored: [names]}: fields this event does not
    use are listed in ignored. Output that looks like one object ({...}) but is not valid
    JSON, or a known field with a wrong type or value, raises ValueError: the hook then
    counts as failed (a before-tool hook blocks its tool). redact applies to every decoded
    string first: a JSON escape can spell a secret that the raw text does not contain."""
    stripped = text.strip() if isinstance(text, str) else ""
    if not (stripped.startswith("{") and stripped.endswith("}")) or truncated:
        return None
    try:
        value = json.loads(stripped)
        if redact is not None:
            value = _redact_tree(value, redact)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Hook output looks like a JSON object but is not valid JSON ({exc.msg} at character "
                         f"{exc.pos}).") from None
    except (ValueError, RecursionError) as exc:
        detail = "nested too deeply" if isinstance(exc, RecursionError) else "it could not be decoded"
        raise ValueError(f"Hook output looks like a JSON object but is not valid JSON ({detail}).") from None
    if not isinstance(value, dict):
        return None
    fields = HOOK_OUTPUT_FIELDS.get(event, {})
    parsed = {"ignored": sorted(str(key)[:40] for key in value if key not in fields)[:10]}
    for key, kind in fields.items():
        if key not in value or value[key] is None:
            continue
        item = value[key]
        if isinstance(kind, tuple):
            if item not in kind:
                raise ValueError(f"Hook output has an unsupported decision {json.dumps(item)[:40]} for {event}; "
                                 f"use {', '.join(kind)}.")
            parsed["decision"] = item
        elif kind is dict:
            if not isinstance(item, dict):
                raise ValueError("Hook output arguments must be a JSON object.")
            parsed["arguments"] = item
        else:
            if not isinstance(item, str):
                raise ValueError(f"Hook output {key} must be text.")
            if key in ("reason", "context"):
                item = _clip_hook_text(item, HOOK_CONTEXT_CHARS)
            elif key == "report":
                item = _clip_hook_text(item, HOOK_REPORT_CHARS)
            if key == "prompt" and not item.strip():
                raise ValueError("Hook output prompt must not be empty.")
            if item.strip():
                parsed[key] = item
    if parsed.get("decision") == "allow":
        parsed.pop("decision")  # No objection: never skips a card the mode or rules require.
    if parsed.get("decision") in ("deny", "block", "ask") and not parsed.get("reason"):
        parsed["reason"] = "The hook gave no reason."
    return parsed


def shown_hook_output(stdout, stderr, limit):
    """(text, shortened): stdout, then stderr under STDERR_LABEL, within limit bytes as bounded_result
    counts them; when both are long, stderr keeps about half, so a failing hook's error still shows."""
    if not stderr.strip():
        result = bounded_result(stdout, limit=limit)
        return result["output"], result["truncated"]
    tail = ("\n" if stdout and not stdout.endswith("\n") else "") + STDERR_LABEL + stderr
    first = bounded_result(stdout, limit=max(limit // 2, limit - len(json.dumps(tail).encode())))
    second = bounded_result(tail, limit=limit - len(json.dumps(first["output"]).encode()))
    return first["output"] + second["output"], first["truncated"] or second["truncated"]


def _text(value, label, limit, optional=False):
    if not isinstance(value, str) or (not optional and not value.strip()) or len(value) > limit or "\x00" in value:
        raise ValueError(f"{label} must be text of at most {limit} characters.")
    return value


def looks_secret(name):
    upper = name.upper()
    return any(word in upper for word in SECRET_WORDS) or bool(set(re.split(r"[_-]", upper)) & SECRET_PARTS)


def secret_reference_name(name):
    """A credential-file key named after an environment variable or a header (X-Api-Key: X_API_KEY)."""
    key = re.sub(r"[^A-Z0-9_]", "_", name.upper()).strip("_") or "SECRET"
    return ("_" + key if key[0].isdigit() else key)[:64]


def _server_values(entry, field, server_id, tokens=()):
    """A server's env or headers: {name: text | {secret: KEY} (headers: and an optional prefix)}."""
    values = entry.get(field, {})
    label = "Environment variable" if field == "env" else "Header"
    if not isinstance(values, dict) or len(values) > MAX_SERVER_VALUES:
        raise ValueError(f"MCP server {server_id}: {field} must be an object with at most {MAX_SERVER_VALUES} entries.")
    result, seen = {}, set()
    for name, value in values.items():
        if field == "env":
            if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
                raise ValueError(f"MCP server {server_id}: environment variable names are uppercase letters, digits "
                                 "and _ (at most 64), starting with a letter or _.")
            if name in DENIED_SERVER_ENV or name.startswith(DENIED_SERVER_ENV_PREFIXES):
                raise ValueError(f"MCP server {server_id}: {name} cannot be set for a server (it changes how programs "
                                 "start, or it is the app's own credential).")
        else:
            if not isinstance(name, str) or not HEADER_NAME.fullmatch(name):
                raise ValueError(f"MCP server {server_id}: header names are letters, digits and - (at most 64).")
            if name.lower() in DENIED_HEADERS:
                raise ValueError(f"MCP server {server_id}: the {name} header is set by the app.")
            if name.lower() in seen:
                raise ValueError(f"MCP server {server_id}: duplicate header {name}.")
            seen.add(name.lower())
        if isinstance(value, dict):
            allowed = {"secret", "prefix"} if field == "headers" else {"secret"}
            key = value.get("secret")
            if set(value) - allowed or not isinstance(key, str) or not SECRET_NAME.fullmatch(key):
                raise ValueError(f"MCP server {server_id}: {label.lower()} {name} must be text or "
                                 '{"secret": NAME}' + (' with an optional "prefix" such as "Bearer "' if field == "headers"
                                                       else "") + ", where NAME is a credential-file key.")
            if key.upper().startswith(("DBRICKS_", "DATABRICKS_", "LOCAL_AGENT_")):
                raise ValueError(f"MCP server {server_id}: {key} is the app's own credential; for your Databricks "
                                 'workspace\'s managed MCP servers use "workspace_credentials": true instead.')
            item = {"secret": key}
            if "prefix" in value:
                prefix = value["prefix"]
                if (not isinstance(prefix, str) or len(prefix) > 32
                        or any(ord(char) < 32 or ord(char) == 127 for char in prefix)):
                    raise ValueError(f"MCP server {server_id}: a header prefix is text of at most 32 characters.")
                if prefix:
                    item["prefix"] = prefix
            result[name] = item
            continue
        if not isinstance(value, str) or len(value) > SERVER_VALUE_CHARS or "\x00" in value or (
                field == "headers" and any(char in value for char in "\r\n")):
            raise ValueError(f"MCP server {server_id}: {label.lower()} {name} must be text of at most "
                             f"{SERVER_VALUE_CHARS:,} characters" + (" on one line." if field == "headers" else "."))
        if looks_secret(name) or redact_secrets(value) != value or any(token and token in value for token in tokens):
            reference = secret_reference_name(name)
            raise ValueError(f"MCP server {server_id}: {label.lower()} {name} looks like a secret, which is not kept in "
                             f"this configuration; {SECRET_HINT.format(name=reference)}.")
        result[name] = value
    return result


def references(server):
    """The credential-file keys a server's env and headers refer to, in order."""
    keys = []
    for field in ("env", "headers"):
        for value in (server.get(field) or {}).values():
            if isinstance(value, dict) and value["secret"] not in keys:
                keys.append(value["secret"])
    return keys


def workspace_match(url, workspace):
    """Whether url is https on the Databricks workspace URL's host and port (lowercased host, default 443)."""
    try:
        target, home = urlsplit(url), urlsplit(workspace)
        return (target.scheme == "https" and home.scheme == "https" and bool(target.hostname)
                and target.hostname.lower() == (home.hostname or "").lower()
                and (target.port or 443) == (home.port or 443))
    except ValueError:  # A port that is not a number.
        return False


def validate_config(config, tokens=()):
    """The checked configuration. tokens: secrets a plain env or header value must not contain."""
    if not isinstance(config, dict) or set(config) - {"servers", "hooks"}:
        raise ValueError("Extension configuration contains unsupported fields.")
    result = {"servers": [], "hooks": []}
    for key, maximum in (("servers", MAX_SERVERS), ("hooks", MAX_HOOKS)):
        entries = config.get(key, [])
        if not isinstance(entries, list) or len(entries) > maximum:
            raise ValueError(f"Configure at most {maximum} {key}.")
        identifiers = set()
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not IDENTIFIER.fullmatch(entry["id"]):
                raise ValueError("Extension IDs must start with a lowercase letter and contain at most 20 lowercase letters, digits, _ or -.")
            if entry["id"] in identifiers:
                raise ValueError(f"Duplicate {key} ID: {entry['id']}.")
            identifiers.add(entry["id"])
            if type(entry.get("enabled", False)) is not bool:
                raise ValueError("enabled must be true or false.")
            item = {"id": entry["id"], "enabled": entry.get("enabled", False)}
            if key == "servers":
                if set(entry) - {"id", "name", "transport", "command", "args", "url", "enabled", "env", "headers",
                                 "workspace_credentials"}:
                    raise ValueError("MCP server configuration contains unsupported fields.")
                item["name"] = _text(entry.get("name", entry["id"]), "Server name", 80)
                item["transport"] = entry.get("transport")
                if item["transport"] == "stdio":
                    item["command"] = _text(entry.get("command"), "Server command", 4096)
                    args = entry.get("args", [])
                    if not isinstance(args, list) or len(args) > 64:
                        raise ValueError("MCP server arguments must be a list of at most 64 strings.")
                    item["args"] = [_text(arg, "Server argument", 4096, optional=True) for arg in args]
                    if entry.get("url"):
                        raise ValueError("stdio servers cannot include an HTTP URL.")
                    if "headers" in entry or "workspace_credentials" in entry:
                        raise ValueError("stdio servers take env, not headers or workspace_credentials.")
                    if entry.get("env"):
                        item["env"] = _server_values(entry, "env", entry["id"], tokens)
                elif item["transport"] == "http":
                    url = _text(entry.get("url"), "Server URL", 2048)
                    parsed = urlsplit(url)
                    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                        raise ValueError("MCP HTTP servers need an HTTP(S) URL without embedded credentials or fragments.")
                    if entry.get("command") or entry.get("args"):
                        raise ValueError("HTTP servers cannot include a local command or arguments.")
                    if "env" in entry:
                        raise ValueError("HTTP servers take headers, not env.")
                    item["url"] = url
                    if entry.get("headers"):
                        item["headers"] = _server_values(entry, "headers", entry["id"], tokens)
                        if parsed.scheme == "http" and parsed.hostname not in LOOPBACK and references(item):
                            raise ValueError("Secret headers need an https URL (plain http only to a server on this "
                                             "computer: localhost or 127.0.0.1).")
                    workspace = entry.get("workspace_credentials", False)
                    if type(workspace) is not bool:
                        raise ValueError("workspace_credentials must be true or false.")
                    if workspace:
                        if parsed.scheme != "https":
                            raise ValueError("workspace_credentials needs an https URL on your Databricks workspace.")
                        if any(name.lower() == "authorization" for name in item.get("headers", {})):
                            raise ValueError("workspace_credentials sends the Authorization header; remove yours.")
                        item["workspace_credentials"] = True
                else:
                    raise ValueError("MCP transport must be stdio or http.")
            else:
                if set(entry) - {"id", "event", "command", "timeout_seconds", "enabled", "tools"}:
                    raise ValueError("Hook configuration contains unsupported fields.")
                if entry.get("event") not in HOOK_EVENTS:
                    raise ValueError(f"Hook event must be {HOOK_EVENT_LIST}.")
                timeout = entry.get("timeout_seconds", 10)
                if type(timeout) is not int or not 1 <= timeout <= 30:
                    raise ValueError("Hook timeout must be an integer from 1 to 30 seconds.")
                item.update(event=entry["event"], command=_text(entry.get("command"), "Hook command", 4096), timeout_seconds=timeout)
                if "tools" in entry:
                    selectors = entry["tools"]
                    if (not isinstance(selectors, list) or len(selectors) > 64
                            or any(not isinstance(name, str) or not TOOL_SELECTOR.fullmatch(name) for name in selectors)
                            or len(set(selectors)) != len(selectors)):
                        raise ValueError("Hook tools must be at most 64 unique exact tool names (no wildcards or regular expressions).")
                    item["tools"] = list(selectors)
            result[key].append(item)
    if sum(server["enabled"] for server in result["servers"]) > MAX_ENABLED_SERVERS:
        raise ValueError(f"Enable at most {MAX_ENABLED_SERVERS} MCP servers at a time (up to {MAX_SERVERS} can be "
                         "configured).")
    if len(json.dumps(result, indent=2).encode()) > CONFIG_LIMIT:
        raise ValueError("Extension configuration exceeds 32 KB.")
    return result


class ExtensionManager:
    def __init__(self, settings, app_skills=None):
        self.settings = settings
        # Skills shipped with the app (skills/<id>/SKILL.md) are offered in every
        # workspace; a workspace skill with the same ID replaces the shipped one.
        self.app_skills = Path(app_skills) if app_skills is not None else config.APP_ROOT / "skills"
        self.path = settings.state_dir / "extensions.json"
        self.config = {"servers": [], "hooks": []}
        # In memory: servers that failed to start, retried after mcp_client.RETRY_SECONDS (shared by every chat).
        self.failures = {}
        if self.path.exists():
            if self.path.stat().st_size > CONFIG_LIMIT:
                raise ValueError("Extension configuration exceeds 32 KB.")
            self.config = validate_config(json.loads(self.path.read_text()))

    def public_config(self):
        """The configuration as saved: secret references only, never their values."""
        return copy.deepcopy(self.config)

    def _credential_values(self):
        """The credential file's assignments (read as data), or {} without one."""
        values = getattr(self.settings, "values", None) or {}
        path = values.get("env_file") if isinstance(values, dict) else ""
        if not path:
            return {}
        try:
            return config.read_env(Path(path).expanduser())
        except (OSError, UnicodeError, ValueError):
            return {}

    def secret_values(self):
        """The resolved values of the enabled servers' secret references (8 characters or more), to redact."""
        keys = [key for server in self.config["servers"] if server["enabled"] for key in references(server)]
        if not keys:
            return []
        found = self._credential_values()
        return [found[key] for key in keys if len(found.get(key) or "") >= 8]

    def _tokens(self):
        tokens = []
        try:
            credentials = getattr(self.settings, "credentials", None)
            tokens = [credentials()[1]] if credentials else []
        except (ValueError, OSError):
            pass
        return tokens + self.secret_values()

    def resolve(self, server):
        """(env, headers) for one connect of server: its references read from the credential file now, and
        the workspace token for workspace_credentials on the workspace's own host. ValueError (that
        server alone fails) names a missing key, never a value."""
        found = self._credential_values() if references(server) else {}
        resolved = {}
        for field in ("env", "headers"):
            resolved[field] = {}
            for name, value in (server.get(field) or {}).items():
                if isinstance(value, dict):
                    if not found.get(value["secret"]):
                        where = "your credential file" if (getattr(self.settings, "values", None) or {}).get("env_file") \
                            else "your credential file (set LOCAL_AGENT_ENV_FILE)"
                        raise ValueError(f"Add {value['secret']}=… to {where}: the {name} "
                                         f"{'environment variable' if field == 'env' else 'header'} refers to it.")
                    value = value.get("prefix", "") + found[value["secret"]]
                if field == "headers" and any(char in value for char in "\r\n\x00"):
                    raise ValueError(f"The value for the {name} header must be on one line.")
                resolved[field][name] = value
        try:
            credentials = getattr(self.settings, "credentials", None)
            host, token = credentials() if credentials else ("", "")
        except (ValueError, OSError) as error:
            if server.get("workspace_credentials"):
                raise ValueError(f"workspace_credentials needs your Databricks credentials: {error}") from None
            host, token = "", ""
        # The workspace token goes only to the workspace host, and only through workspace_credentials.
        if token and any(token in value for field in resolved.values() for value in field.values()):
            raise ValueError("A secret reference of this server holds your Databricks token, which is sent only with "
                             'workspace_credentials to your workspace; use "workspace_credentials": true instead.')
        if server.get("workspace_credentials"):
            if not token:
                raise ValueError("workspace_credentials needs your Databricks credentials in your credential file.")
            if not workspace_match(server["url"], host):
                raise ValueError(f"workspace_credentials works only for https URLs on your Databricks workspace "
                                 f"({urlsplit(host).hostname}, the same port); this server's URL is elsewhere.")
            resolved["headers"]["Authorization"] = f"Bearer {token}"
        return resolved["env"], resolved["headers"]

    def _redact(self, text, tokens, partial=False):
        for token in tokens:
            if token:
                text = text.replace(token, "[REDACTED]")
        if partial:
            held = max((size for token in tokens for size in range(1, min(len(token), len(text) + 1))
                        if text.endswith(token[:size])), default=0)
            text = text[:-held] if held else text
        return self.settings.redact(text)

    def update_config(self, config):
        try:
            credentials = getattr(self.settings, "credentials", None)
            tokens = [credentials()[1]] if credentials else []
        except (ValueError, OSError):
            tokens = []
        candidate = validate_config(config, tokens)
        descriptor, filename = tempfile.mkstemp(prefix="extensions-", suffix=".tmp", dir=self.settings.state_dir)
        try:
            with os.fdopen(descriptor, "w") as target:
                json.dump(candidate, target, indent=2)
            Path(filename).replace(self.path)
        finally:
            Path(filename).unlink(missing_ok=True)
        self.config = candidate
        self.failures.clear()  # A changed server is tried again at once.
        return self.public_config()

    @asynccontextmanager
    async def turn(self, off=()):
        """This turn's connection. off: IDs of servers the chat turned off, never started."""
        tokens = self._tokens()
        async with MCPConnection(self.config["servers"], lambda text: self._redact(text, tokens), off=off,
                                 failures=self.failures, resolve=self.resolve,
                                 state_dir=self.settings.state_dir) as connection:
            yield connection

    async def discover(self):
        async with self.turn() as connection:
            return await connection.discover()

    async def test(self):
        """Explicit diagnostics: every enabled server is connected now (a recent failure does not wait),
        and the result also decides when chats try a failed server again."""
        tokens = self._tokens()
        redact = lambda text: self._redact(text, tokens)
        diagnostics = {"tools": [], "servers": []}
        for server in self.public_config()["servers"]:
            self.failures.pop(server["id"], None)
            try:
                # Enter and close on this task even when initialization fails.
                async with MCPConnection([server], redact, failures=self.failures, resolve=self.resolve,
                                         state_dir=self.settings.state_dir) as connection:
                    await connection.discover()
                    item = connection.status[0]
                    if connection.annotations:
                        item["tool_annotations"] = connection.annotations
            except Exception as error:
                item = {"id": server["id"], "name": redact(server["name"]), "status": "failed", "tools": [],
                        "error": bounded_error(redact(str(error)))}
            item["tools"] = [redact(name) for name in item["tools"]]
            diagnostics["servers"].append(item)
            diagnostics["tools"].extend(item["tools"])
        return diagnostics

    async def call(self, name, arguments):
        async with self.turn() as connection:
            return await connection.call(name, arguments)

    def _skill(self, tools, skill_id, root=None, roots=WORKSPACE_SKILL_ROOTS):
        if not isinstance(skill_id, str) or not SKILL_IDENTIFIER.fullmatch(skill_id):
            raise ValueError("Choose a skill ID from this workspace's skill list.")
        root = root or next((folder for folder in roots
                             if os.path.lexists(tools.root / folder / skill_id / "SKILL.md")), WORKSPACE_SKILL_ROOTS[0])
        target = tools.path(f"{root}/{skill_id}/SKILL.md")
        if not target.is_relative_to(tools.root) or not stat.S_ISREG(target.stat().st_mode):
            raise ValueError("Skills must be regular files inside this workspace.")
        return self._read_skill(skill_id, target, str(target.relative_to(tools.root)))

    def _app_skill(self, skill_id):
        folder = self.app_skills / skill_id
        target = folder / "SKILL.md"
        if folder.is_symlink() or target.is_symlink() or not stat.S_ISREG(target.stat().st_mode):
            raise ValueError("Shipped skills must be regular files in the app's skills folder.")
        return self._read_skill(skill_id, target, str(target))

    def _read_skill(self, skill_id, target, path):
        with target.open("rb") as source:
            raw = source.read(SKILL_LIMIT + 1)
        if len(raw) > SKILL_LIMIT or b"\x00" in raw:
            raise ValueError("Skills must be UTF-8 text of at most 8 KB.")
        text = raw.decode("utf-8")
        name, description = skill_id, ""
        lines = text.splitlines()
        if lines and lines[0] == "---":
            for line in lines[1:]:
                if line == "---":
                    break
                key, separator, value = line.partition(":")
                if separator and key in {"name", "description"}:
                    value = value.strip().strip("\"'")
                    if key == "name":
                        name = value[:80]
                    else:
                        description = value[:300]
        return {"id": skill_id, "name": self.settings.redact(name), "description": self.settings.redact(description),
                "path": path, "text": self.settings.redact(text)}

    @staticmethod
    def _in_workspace(tools, skill_id, roots=WORKSPACE_SKILL_ROOTS):
        # Any workspace entry for this ID takes it over, even an invalid one, so a
        # shipped skill never silently replaces a workspace skill that fails to load.
        return any(os.path.lexists(tools.root / root / skill_id / "SKILL.md") for root in roots)

    def _discover(self, folder, load):
        children = list(itertools.islice(folder.iterdir(), 101))
        if len(children) > 100:
            raise ValueError("Skill discovery scans at most 100 entries; reduce the skills directory.")
        skills = []
        for child in sorted(children):
            if not child.is_dir() or child.is_symlink() or not SKILL_IDENTIFIER.fullmatch(child.name):
                continue
            if not (child / "SKILL.md").exists():
                continue
            try:
                skill = load(child.name)
            except (OSError, ValueError, UnicodeError):
                continue
            skills.append({key: value for key, value in skill.items() if key != "text"})
            if len(skills) > MAX_SKILLS:
                raise ValueError("At most 30 workspace skills can be discovered.")
        return skills

    def _claude_skills(self, tools):
        """Skills in .claude/skills, or None when that folder is missing or unusable
        (outside the workspace, not a folder, too many entries or skills, unreadable).
        Problems in this other tool's folder skip it, as for the shipped skills; only
        a usable folder takes skill IDs over from the shipped skills."""
        try:
            folder = tools.path(WORKSPACE_SKILL_ROOTS[1])
            if not folder.is_relative_to(tools.root) or not folder.is_dir():
                return None
            return self._discover(folder, lambda skill_id: self._skill(tools, skill_id, WORKSPACE_SKILL_ROOTS[1]))
        except (OSError, ValueError, RuntimeError):
            return None

    @staticmethod
    def _usable_roots(claude):
        return WORKSPACE_SKILL_ROOTS if claude is not None else WORKSPACE_SKILL_ROOTS[:1]

    def skills(self, tools):
        folder = tools.path(".agents/skills")
        skills = []
        if folder.exists():
            if not folder.is_relative_to(tools.root) or not folder.is_dir():
                raise ValueError("Skills must be in this workspace's .agents/skills directory.")
            skills = self._discover(folder, lambda skill_id: self._skill(tools, skill_id, WORKSPACE_SKILL_ROOTS[0]))
        claude = self._claude_skills(tools)
        roots = self._usable_roots(claude)
        try:
            shipped = self._discover(self.app_skills, self._app_skill) if self.app_skills.is_dir() else []
        except (OSError, ValueError):
            shipped = []
        return (skills + [skill for skill in claude or [] if not self._in_workspace(tools, skill["id"], WORKSPACE_SKILL_ROOTS[:1])]
                + [skill for skill in shipped if not self._in_workspace(tools, skill["id"], roots)])

    def load_skill(self, tools, skill_id):
        if not isinstance(skill_id, str) or not SKILL_IDENTIFIER.fullmatch(skill_id):
            return self._skill(tools, skill_id)
        roots = self._usable_roots(self._claude_skills(tools))
        if not self._in_workspace(tools, skill_id, roots) and os.path.lexists(self.app_skills / skill_id / "SKILL.md"):
            return self._app_skill(skill_id)
        return self._skill(tools, skill_id, roots=roots)

    async def run_hook(self, hook, payload, workspace, env=None):
        """Run a hook with payload on stdin. env, when given, is the hook's whole environment.

        Returns {exit_code, output (redacted stdout, then stderr under STDERR_LABEL, at most
        HOOK_SHOWN_BYTES as a card shows it), truncated, timed_out?, structured?}: structured
        (parse_hook_output) is set when the hook exited 0 and its whole stdout is one JSON
        object, with every decoded text redacted; a ValueError for an invalid one is returned
        as structured_error. stderr never takes part: a warning on it cannot spoil the JSON."""
        selected = next((entry for entry in self.config["hooks"] if entry["id"] == hook.get("id") and entry["enabled"]), None)
        if selected is None or selected != hook:
            raise ValueError("Hook is disabled or changed; refresh its configuration before running it.")
        tokens = self._tokens()
        data = json.dumps(payload)
        if len(data.encode()) > 128_000:
            raise ValueError("Hook input exceeds 128 KB.")
        descriptor, filename = tempfile.mkstemp(prefix="hook-input-", suffix=".json", dir=self.settings.state_dir)
        try:
            with os.fdopen(descriptor, "w") as target:
                target.write(data)
            command = f"(\n{selected['command']}\n) < {shlex.quote(filename)}"
            try:
                result = await run_process(command, workspace, selected["timeout_seconds"], HOOK_STDOUT_BYTES, env=env,
                                           separate_stderr=True)
            except CommandTimeout as error:
                result = {**error.result, "timed_out": True}
            # Room for the note that a command did not end, added after redaction.
            room = len(NOT_STOPPED) + 1 if result.get("stopped") is False else 0
            ended = result.get("timed_out", False)
            redacted = self._redact(result["output"], tokens, partial=result["truncated"] or ended)
            errors = self._redact(result.pop("stderr", ""), tokens, partial=result.pop("stderr_truncated", False) or ended)
            redact = lambda text: self._redact(text, tokens)
            # Shown re-encoded only when a JSON escape spelled a secret: the card never shows it either.
            cleaned = None if result["truncated"] else _escaped_secrets(redacted, redact)
            shown, shortened = shown_hook_output(cleaned or redacted, errors, HOOK_SHOWN_BYTES - room)
            extra = {}
            if result.get("exit_code") == 0 and not result.get("timed_out"):
                # Read from the redacted stdout, each decoded text redacted again (an escaped secret):
                # a secret never reaches the card or the model.
                try:
                    structured = parse_hook_output(selected["event"], redacted, truncated=result["truncated"],
                                                   redact=redact)
                except ValueError as exc:
                    extra["structured_error"] = str(exc)
                else:
                    if structured is not None:
                        extra["structured"] = structured
                if result["truncated"] and redacted.lstrip().startswith("{"):
                    extra["structured_error"] = (f"Hook output is longer than {HOOK_STDOUT_BYTES // 1024} KB, so it "
                                                 "was not read as JSON.")
            return {**result, "output": with_note(shown, result), "truncated": result["truncated"] or shortened, **extra}
        finally:
            Path(filename).unlink(missing_ok=True)


def skill_index(skills) -> str:
    """A short list of skill IDs and descriptions for the system message, at most
    SKILL_INDEX_ENTRIES lines and SKILL_INDEX_BYTES bytes; '' without skills."""
    if not skills:
        return ""
    header = "Skills you can load with use_skill when one fits the task (list_skills shows details):\n"
    lines = []
    for skill in skills:
        description = " ".join(str(skill.get("description") or "").split())
        if len(description) > SKILL_INDEX_DESCRIPTION:
            description = description[:SKILL_INDEX_DESCRIPTION - 1] + "…"
        lines.append(f"- {skill['id']}: {description}\n" if description else f"- {skill['id']}\n")

    def more(count):
        return f"- … and {count} more (call list_skills).\n" if count else ""
    kept = []
    for index, line in enumerate(lines):
        candidate = header + "".join(kept) + line + more(len(lines) - index - 1)
        if len(kept) >= SKILL_INDEX_ENTRIES or len(candidate.encode("utf-8")) > SKILL_INDEX_BYTES:
            break
        kept.append(line)
    return (header + "".join(kept) + more(len(lines) - len(kept))).rstrip("\n")
