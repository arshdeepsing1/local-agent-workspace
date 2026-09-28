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

from .jobs import CommandTimeout, run_process
from .mcp_client import MCPConnection, bounded_error, bounded_result

CONFIG_LIMIT = 32_000
SKILL_LIMIT = 8_000
MAX_SKILLS = 30
IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,19}\Z")
SKILL_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
TOOL_SELECTOR = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def _text(value, label, limit, optional=False):
    if not isinstance(value, str) or (not optional and not value.strip()) or len(value) > limit or "\x00" in value:
        raise ValueError(f"{label} must be text of at most {limit} characters.")
    return value


def validate_config(config):
    if not isinstance(config, dict) or set(config) - {"servers", "hooks"}:
        raise ValueError("Extension configuration contains unsupported fields.")
    result = {"servers": [], "hooks": []}
    for key, maximum in (("servers", 4), ("hooks", 8)):
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
                if entry.get("event") not in {"before_tool", "after_tool", "tool_failure"}:
                    raise ValueError("Hook event must be before_tool, after_tool or tool_failure.")
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
    def __init__(self, settings):
        self.settings = settings
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

    def _skill(self, tools, skill_id):
        if not isinstance(skill_id, str) or not SKILL_IDENTIFIER.fullmatch(skill_id):
            raise ValueError("Choose a skill ID from this workspace's skill list.")
        target = tools.path(f".agents/skills/{skill_id}/SKILL.md")
        if not target.is_relative_to(tools.root) or not stat.S_ISREG(target.stat().st_mode):
            raise ValueError("Skills must be regular files inside this workspace.")
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
                "path": str(target.relative_to(tools.root)), "text": self.settings.redact(text)}

    def skills(self, tools):
        folder = tools.path(".agents/skills")
        if not folder.exists():
            return []
        if not folder.is_relative_to(tools.root) or not folder.is_dir():
            raise ValueError("Skills must be in this workspace's .agents/skills directory.")
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
                skill = self._skill(tools, child.name)
            except (OSError, ValueError, UnicodeError):
                continue
            skills.append({key: value for key, value in skill.items() if key != "text"})
            if len(skills) > MAX_SKILLS:
                raise ValueError("At most 30 workspace skills can be discovered.")
        return skills

    def load_skill(self, tools, skill_id):
        return self._skill(tools, skill_id)

    async def run_hook(self, hook, payload, workspace):
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
                result = await run_process(command, workspace, selected["timeout_seconds"], 4096)
            except CommandTimeout as error:
                result = {**error.result, "timed_out": True}
            output = bounded_result(self._redact(result["output"], tokens, partial=result["truncated"] or result.get("timed_out", False)), limit=3800)
            return {**result, "output": output["output"], "truncated": result["truncated"] or output["truncated"]}
        finally:
            Path(filename).unlink(missing_ok=True)
