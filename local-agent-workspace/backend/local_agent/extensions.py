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
from .jobs import NOT_STOPPED, CommandTimeout, run_process, with_note
from .mcp_client import MCPConnection, bounded_error, bounded_result

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
MAX_SERVERS = 4
MAX_HOOKS = 16
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
HOOK_SHOWN_BYTES = 3_800  # the part of it a hook card and a warning show
SHORTENED = " […shortened by the app]"


def _clip_hook_text(value, limit):
    value = value.strip()
    return value if len(value) <= limit else value[:limit - len(SHORTENED)].rstrip() + SHORTENED


def parse_hook_output(event, text, truncated=False):
    """The structured part of a hook's stdout (exit code 0), or None for plain text.

    Only when the whole trimmed output is one JSON object is it read; anything else
    (text, a list, malformed or cut-off JSON) is plain text, as before. Returns
    {decision?, reason?, context?, arguments?, prompt?, report?, ignored: [names]}: fields
    this event does not use are listed in ignored. A known field with a wrong type or value
    raises ValueError: the hook then counts as failed (a before-tool hook blocks its tool)."""
    stripped = text.strip() if isinstance(text, str) else ""
    if not (stripped.startswith("{") and stripped.endswith("}")) or truncated:
        return None
    try:
        value = json.loads(stripped)
    except (ValueError, RecursionError):
        return None
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


def _text(value, label, limit, optional=False):
    if not isinstance(value, str) or (not optional and not value.strip()) or len(value) > limit or "\x00" in value:
        raise ValueError(f"{label} must be text of at most {limit} characters.")
    return value


def validate_config(config):
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
                if set(entry) - {"id", "name", "transport", "command", "args", "url", "enabled"}:
                    raise ValueError("MCP server configuration contains unsupported fields; custom environment/auth fields are not supported.")
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
                elif item["transport"] == "http":
                    url = _text(entry.get("url"), "Server URL", 2048)
                    parsed = urlsplit(url)
                    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                        raise ValueError("MCP HTTP servers need an HTTP(S) URL without embedded credentials or fragments.")
                    if entry.get("command") or entry.get("args"):
                        raise ValueError("HTTP servers cannot include a local command or arguments.")
                    item["url"] = url
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
        if self.path.exists():
            if self.path.stat().st_size > CONFIG_LIMIT:
                raise ValueError("Extension configuration exceeds 32 KB.")
            self.config = validate_config(json.loads(self.path.read_text()))

    def public_config(self):
        return copy.deepcopy(self.config)

    def _tokens(self):
        try:
            credentials = getattr(self.settings, "credentials", None)
            return [credentials()[1]] if credentials else []
        except (ValueError, OSError):
            return []

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
        candidate = validate_config(config)
        descriptor, filename = tempfile.mkstemp(prefix="extensions-", suffix=".tmp", dir=self.settings.state_dir)
        try:
            with os.fdopen(descriptor, "w") as target:
                json.dump(candidate, target, indent=2)
            Path(filename).replace(self.path)
        finally:
            Path(filename).unlink(missing_ok=True)
        self.config = candidate
        return self.public_config()

    @asynccontextmanager
    async def turn(self):
        tokens = self._tokens()
        async with MCPConnection(self.config["servers"], lambda text: self._redact(text, tokens)) as connection:
            yield connection

    async def discover(self):
        async with self.turn() as connection:
            return await connection.discover()

    async def test(self):
        """Explicit diagnostics isolate each server, without enabling partial chat discovery."""
        tokens = self._tokens()
        redact = lambda text: self._redact(text, tokens)
        diagnostics = {"tools": [], "servers": []}
        for server in self.public_config()["servers"]:
            item = {"id": server["id"], "name": redact(server["name"]), "status": "disabled", "tools": []}
            diagnostics["servers"].append(item)
            if not server["enabled"]:
                continue
            try:
                # Enter and close on this task even when initialization fails.
                async with MCPConnection([server], redact) as connection:
                    definitions = await connection.discover()
                    names = [redact(tool["function"]["name"]) for tool in definitions]
                item.update(status="connected", tools=names)
                diagnostics["tools"].extend(names)
            except Exception as error:
                item.update(status="failed", error=bounded_error(redact(str(error))))
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

        Returns {exit_code, output (redacted, at most HOOK_SHOWN_BYTES as a card shows it),
        truncated, timed_out?, structured?}: structured (parse_hook_output) is set when the
        hook exited 0 and its whole stdout, redacted, is one JSON object; a ValueError for
        an invalid one is returned as structured_error."""
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
                result = await run_process(command, workspace, selected["timeout_seconds"], HOOK_STDOUT_BYTES, env=env)
            except CommandTimeout as error:
                result = {**error.result, "timed_out": True}
            # Room for the note that a command did not end, added after redaction.
            room = len(NOT_STOPPED) + 1 if result.get("stopped") is False else 0
            redacted = self._redact(result["output"], tokens, partial=result["truncated"] or result.get("timed_out", False))
            output = bounded_result(redacted, limit=HOOK_SHOWN_BYTES - room)
            extra = {}
            if result.get("exit_code") == 0 and not result.get("timed_out"):
                # Read from the redacted output: a secret never reaches the card or the model.
                try:
                    structured = parse_hook_output(selected["event"], redacted, truncated=result["truncated"])
                except ValueError as exc:
                    extra["structured_error"] = str(exc)
                else:
                    if structured is not None:
                        extra["structured"] = structured
                if result["truncated"] and redacted.lstrip().startswith("{"):
                    extra["structured_error"] = (f"Hook output is longer than {HOOK_STDOUT_BYTES // 1024} KB, so it "
                                                 "was not read as JSON.")
            return {**result, "output": with_note(output["output"], result),
                    "truncated": result["truncated"] or output["truncated"], **extra}
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
