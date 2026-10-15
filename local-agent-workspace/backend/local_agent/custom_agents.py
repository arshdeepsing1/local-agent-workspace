"""Subagent types for delegate_task: the built-in ones (explore, general, editor) with their
Settings → Subagents defaults, and custom agents defined in Markdown files of the workspace.

A custom agent file is project content, like AGENTS.md, and is only read: .agents/agents/<name>.md
first, then .claude/agents/<name>.md (the same name in both: the .agents file is used). Its
frontmatter (between two '---' lines) names the agent, tells the model when to choose it and sets
its limits; the body is the agent's instructions, which the subagent's first prompt carries (the
parent's system prompt only gets the short agent index). Nothing in a file widens what the chat
allows: its tools are intersected with its profile and the chat's subagent ceiling, the chat's
turned-off tools, rules and permission mode apply as to any subagent, and subagents never delegate.

The parser is small and strict (no YAML library): `key: value` lines, `key: [a, b]`, and `key:`
followed by `- item` lines; quoted values may use "..." (JSON escapes) or '...'. Lines starting
with # are comments. A file that does not parse is listed with the reason instead.
"""
import itertools
import json
import os
import re
import stat
from pathlib import Path

from .config import ENDPOINT_NAME
from .feature_tools import FEATURE_TOOLS
from .model_profiles import profile_for
from .reasoning import EFFORT_LABELS, EFFORT_LEVELS
from .redaction import redact_secrets
from .tool_profiles import FILE_TOOLS, PROFILES, READ_TOOLS, validate_profile
from .tools import TOOL_DEFINITIONS

AGENT_ROOTS = (".agents/agents", ".claude/agents")  # In order of precedence.
# The built-in types (planning.AGENT_TYPES) and what they are for, as Agent tools lists them.
BUILT_IN = {
    "explore": ("read_only", "Investigation with read-only tools: find, search, outline and read files."),
    "general": ("inherit", "The chat's tools (within the subagent tool ceiling)."),
    "editor": ("file_editor", "Reads and edits files, without commands."),
}
PROFILE_TYPES = {"read_only": "explore", "file_editor": "editor", "inherit": "general"}
PROFILE_LABELS = {"read_only": "read-only", "file_editor": "file editor", "inherit": "chat's tools"}
NAME = re.compile(r"[a-z][a-z0-9-]{0,39}\Z")
SKILL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")  # extensions.SKILL_IDENTIFIER
MCP_TOOL = re.compile(r"mcp__[A-Za-z0-9_-]{1,64}__[A-Za-z0-9_.-]{1,128}\Z")  # rules.MCP_NAME
KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}\Z")
FIELDS = ("name", "description", "tools", "profile", "model", "effort", "skills", "max_steps")
MAX_AGENTS = 30
FILE_BYTES = 16_384  # 16 KB per agent file.
BODY_CHARS = 8_000
DESCRIPTION_CHARS = 300
SCAN_ENTRIES = 100  # Entries read per agent folder.
MAX_SKILLS = 3  # AgentManager.skill_text: at most three skills per conversation.
MAX_TOOLS = 64
MAX_STEPS = 24  # planning.MAX_STEPS
INDEX_ENTRIES = 30
INDEX_BYTES = 2_000
INDEX_DESCRIPTION = 150
# Claude Code's agent files (.claude/agents) name its own tools; these are this app's equivalents.
TOOL_ALIASES = {"Read": "read_file", "Write": "write_file", "Edit": "edit_file", "MultiEdit": "edit_file",
                "Glob": "find_files", "Grep": "search_files", "LS": "list_files", "Bash": "run_command",
                "NotebookRead": "notebook_read", "NotebookEdit": "notebook_edit"}
# Claude Code's model values: not Databricks endpoints, so the chat's model is used.
MODEL_ALIASES = ("sonnet", "opus", "haiku")
INHERIT_VALUES = ("", "inherit", "default-model")
INDEX_HEADER = ("Custom subagents: set delegate_task agent_type to one of these names when its description fits "
                "(each brings its own instructions and tools, within this chat's limits):\n")


def known_tools():
    return {item["function"]["name"] for item in (*TOOL_DEFINITIONS, *FEATURE_TOOLS)}


def display_redactor(settings):
    """Redaction for agent file text the app shows or sends: the Databricks token and common secret shapes."""
    return lambda text: settings.redact(redact_secrets(text))


def model_label(model):
    """A short name for an endpoint ("Claude Sonnet 4.5"): its profile's label, else from its name
    (the frontend's modelLabel)."""
    profile = profile_for(model)
    if profile is not None:
        return profile["label"]
    text = re.sub(r"^(databricks-|system\.ai\.)", "", model)
    text = re.sub(r"\b(\d+)-(\d+)\b", r"\1.\2", text).replace("-", " ")
    text = re.sub(r"\b(gpt|oss)\b", lambda match: match.group(1).upper(), text, flags=re.IGNORECASE)
    text = re.sub(r"\b(\d+)b\b", r"\1B", text)
    text = re.sub(r"\b(claude|opus|sonnet|haiku|llama|gemini|gemma)\b", lambda match: match.group(1).capitalize(), text)
    return text[:1].upper() + text[1:]


# Parsing

def _scalar(value, number):
    if value[:1] == '"':
        try:
            decoded = json.loads(value)
        except ValueError:
            raise ValueError(f"Line {number}: a double-quoted value must end with \" (JSON escapes).") from None
        if not isinstance(decoded, str):
            raise ValueError(f"Line {number}: a double-quoted value must be text.")
        try:
            decoded.encode("utf-8")  # A lone surrogate escape ("\ud800") breaks every later JSON or UTF-8 write.
        except UnicodeEncodeError:
            raise ValueError(f"Line {number}: a double-quoted value is not valid text (a lone \\u escape).") from None
        return decoded
    if value[:1] == "'":
        if len(value) < 2 or not value.endswith("'"):
            raise ValueError(f"Line {number}: a single-quoted value must end with '.")
        return value[1:-1].replace("''", "'")
    if re.fullmatch(r"-?\d{1,9}", value):
        return int(value)
    return value


def _items(text, number):
    """The items of an inline [a, b] list."""
    items, current, quote = [], "", None
    for char in text:
        if quote:
            current += char
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
            current += char
        elif char == ",":
            items.append(current.strip())
            current = ""
        else:
            current += char
    if quote:
        raise ValueError(f"Line {number}: a quoted list item is not closed.")
    items.append(current.strip())
    if any(not item for item in items):
        raise ValueError(f"Line {number}: a [list] has an empty item.")
    return [_scalar(item, number) for item in items]


def frontmatter(text):
    """({field: value}, body) of an agent file. Values are text, whole numbers or lists of them."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("The file must start with a '---' line: frontmatter with at least a description, "
                         "closed by another '---' line, then the instructions.")
    end = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if end is None:
        raise ValueError("The frontmatter has no closing '---' line.")
    fields, listing = {}, None  # listing: the key whose "- item" lines follow
    for number, line in enumerate(lines[1:end], start=2):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "-" or stripped.startswith("- "):
            if listing is None:
                raise ValueError(f"Line {number}: a '- item' line must follow a 'key:' line.")
            item = stripped[1:].strip()
            if not item:
                raise ValueError(f"Line {number}: a list item is empty.")
            fields[listing].append(_scalar(item, number))
            continue
        key, separator, value = line.partition(":")
        if line[:1].isspace() or not separator or not KEY.fullmatch(key.strip()):
            raise ValueError(f"Line {number}: expected 'key: value' (nested values are not supported).")
        key, value = key.strip(), value.strip()
        if key in fields:
            raise ValueError(f"Line {number}: {key} is given twice.")
        listing = None
        if not value:
            fields[key], listing = [], key
        elif value[:1] == "[":
            if not value.endswith("]"):
                raise ValueError(f"Line {number}: a [list] must close on the same line.")
            inner = value[1:-1].strip()
            fields[key] = _items(inner, number) if inner else []
        elif value[:1] in "|>":
            raise ValueError(f"Line {number}: multi-line values ({value[:1]}) are not supported; put {key} on one line.")
        else:
            fields[key] = _scalar(value, number)
    return fields, "\n".join(lines[end + 1:]).strip()


def profile_tools(profile):
    """The tool names a profile permits, or None for every tool (inherit)."""
    return None if profile == "inherit" else READ_TOOLS if profile == "read_only" else FILE_TOOLS


def _tools(value, profile, notes):
    names = value.split(",") if isinstance(value, str) else value
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        raise ValueError("tools must be a list of tool names, such as [read_file, search_files].")
    names = [name.strip() for name in names if name.strip()]
    if not names:
        raise ValueError("tools is empty: list the tools the agent may use, or leave tools out for every tool of "
                         "its profile.")
    if len(names) > MAX_TOOLS:
        raise ValueError(f"tools lists more than {MAX_TOOLS} names.")
    known, permitted = known_tools(), profile_tools(profile)
    chosen, mapped, unknown, outside = [], [], [], []
    for name in names:
        if name in TOOL_ALIASES:
            mapped.append(f"{name} → {TOOL_ALIASES[name]}")
            name = TOOL_ALIASES[name]
        if name == "delegate_task":
            notes.append("delegate_task is ignored: subagents cannot delegate.")
            continue
        if name not in known and not MCP_TOOL.fullmatch(name):
            unknown.append(name[:64])
            continue
        if permitted is not None and name not in permitted:
            outside.append(name)
            continue
        if name not in chosen:
            chosen.append(name)
    if mapped:
        notes.append("Claude Code tool names used as this app's: " + ", ".join(mapped) + ".")
    if unknown:
        notes.append("Ignored unknown tools: " + ", ".join(unknown) + ".")
    if outside:
        notes.append(f"Not in the {profile} profile, so unavailable: " + ", ".join(outside) + ".")
    if not chosen:
        raise ValueError(f"tools names no tool this agent can use with the {profile} profile"
                         + (" (set profile: file_editor or inherit for more tools)" if outside else "") + ".")
    return chosen


def parse_agent(text, stem):
    """An agent definition from a file's text (stem: the file name without .md): {name, description,
    profile, tools (None: every tool of the profile), model, effort, skills, max_steps, instructions,
    notes}. ValueError says what is wrong."""
    fields, body = frontmatter(text.removeprefix("﻿"))  # Editors on Windows may start a file with a BOM.
    notes = []
    unknown = sorted(set(fields) - set(FIELDS))
    if unknown:
        notes.append("Ignored unknown fields: " + ", ".join(unknown) + ".")
    name = fields.get("name", stem)
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("name must start with a lowercase letter and have at most 40 lowercase letters, digits "
                         "and '-'" + ("" if "name" in fields else " (without a name field the file name is used)") + ".")
    if name in BUILT_IN:
        raise ValueError(f"{name} is a built-in agent type; give this agent another name.")
    description = fields.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description is required: one line (at most 300 characters) that tells the model when to "
                         "use this agent.")
    description = " ".join(description.split())
    if len(description) > DESCRIPTION_CHARS:
        raise ValueError(f"description has {len(description):,} characters; at most {DESCRIPTION_CHARS}.")
    profile = fields.get("profile", "read_only")
    if profile not in PROFILES:
        raise ValueError("profile must be read_only (the default), file_editor or inherit.")
    tools = _tools(fields["tools"], profile, notes) if "tools" in fields else None
    model = fields.get("model")
    if isinstance(model, str) and model.strip().lower() in INHERIT_VALUES:
        model = None
    elif isinstance(model, str) and model.strip().lower() in MODEL_ALIASES:
        notes.append(f"model {model.strip()} is not a Databricks endpoint name; the chat's model is used.")
        model = None
    elif model is not None:
        if not isinstance(model, str) or not ENDPOINT_NAME.match(model.strip()):
            raise ValueError("model must be a Databricks serving endpoint name (letters, digits, '.', '_' and '-'), "
                             "or left out for the chat's model.")
        model = model.strip()
    effort = fields.get("effort")
    if isinstance(effort, str) and effort.strip().lower() in INHERIT_VALUES:
        effort = None
    elif effort is not None:
        effort = effort.strip().lower() if isinstance(effort, str) else effort
        if effort not in EFFORT_LEVELS:
            raise ValueError("effort must be one of " + ", ".join(EFFORT_LEVELS) + ", or left out for the chat's effort.")
    skills = fields.get("skills", [])
    skills = [item.strip() for item in skills.split(",") if item.strip()] if isinstance(skills, str) else skills
    if (not isinstance(skills, list) or any(not isinstance(item, str) or not SKILL_ID.fullmatch(item) for item in skills)
            or len(set(skills)) != len(skills) or len(skills) > MAX_SKILLS):
        raise ValueError(f"skills must list at most {MAX_SKILLS} different skill IDs.")
    steps = fields.get("max_steps")
    if steps is not None and (type(steps) is not int or not 1 <= steps <= MAX_STEPS):
        raise ValueError(f"max_steps must be a whole number from 1 to {MAX_STEPS}.")
    if len(body) > BODY_CHARS:
        raise ValueError(f"The instructions have {len(body):,} characters; at most {BODY_CHARS:,}.")
    if "\x00" in body:
        raise ValueError("The file is not text.")
    return {"name": name, "description": description, "profile": profile, "tools": tools, "model": model,
            "effort": effort, "skills": list(skills), "max_steps": steps, "instructions": body, "notes": notes}


def _read(entry):
    info = entry.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("Symbolic links are not read: put the agent file itself here.")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Not a regular file.")
    if info.st_size > FILE_BYTES:
        raise ValueError(f"The file has {info.st_size:,} bytes; agent files are at most 16 KB ({FILE_BYTES:,} bytes).")
    descriptor = os.open(entry, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        raw = source.read(FILE_BYTES + 1)
    if len(raw) > FILE_BYTES:
        raise ValueError(f"The file has more than {FILE_BYTES:,} bytes; agent files are at most 16 KB.")
    if b"\x00" in raw:
        raise ValueError("The file is not text.")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("The file is not UTF-8 text.") from None


def _redacted(agent, redact):
    return {**agent, "description": redact(agent["description"]), "instructions": redact(agent["instructions"]),
            "notes": [redact(note) for note in agent["notes"]]}


def discover(workspace, redact=lambda text: text):
    """{agents, errors} of a workspace: agents in precedence order (at most MAX_AGENTS), each with its
    workspace-relative path; errors [{path, error}] for files and folders that could not be used."""
    root = Path(workspace).expanduser().resolve()
    agents, errors, names = [], [], {}
    for folder in AGENT_ROOTS:
        base = root / folder
        if not os.path.lexists(base):
            continue
        try:
            resolved = base.resolve()
            if not resolved.is_relative_to(root) or not resolved.is_dir():
                errors.append({"path": folder, "error": "Agent files must be in a folder inside this workspace."})
                continue
            entries = list(itertools.islice(resolved.iterdir(), SCAN_ENTRIES + 1))
        except (OSError, RuntimeError) as exc:
            errors.append({"path": folder, "error": redact(str(exc)) or type(exc).__name__})
            continue
        if len(entries) > SCAN_ENTRIES:
            errors.append({"path": folder, "error": f"The folder has more than {SCAN_ENTRIES} entries; "
                           f"agent discovery reads at most {SCAN_ENTRIES}."})
            continue
        for entry in sorted(entries, key=lambda item: item.name):
            if not entry.name.endswith(".md") or entry.name.startswith("."):
                continue
            try:
                entry.name.encode("utf-8")
            except UnicodeEncodeError:  # Bytes that are not UTF-8 (surrogate escapes): no path to show, send or save.
                errors.append({"path": f"{folder}/{ascii(entry.name)[1:-1]}",
                               "error": "The file name is not valid UTF-8 text; rename the file."})
                continue
            path = f"{folder}/{entry.name}"
            try:
                agent = _redacted(parse_agent(_read(entry), entry.name[:-3]), redact)
            except (OSError, ValueError) as exc:
                errors.append({"path": path, "error": redact(str(exc))[:500] or type(exc).__name__})
                continue
            first = names.get(agent["name"])
            if first is not None:
                if first["root"] == folder:
                    errors.append({"path": path, "error": f"{first['path']} already defines {agent['name']}."})
                else:
                    first["notes"].append(f"Used instead of {path}, which has the same name.")
                continue
            if len(agents) >= MAX_AGENTS:
                errors.append({"path": path, "error": f"At most {MAX_AGENTS} custom agents are read; this one is ignored."})
                continue
            agent.update(path=path, root=folder)
            names[agent["name"]] = agent
            agents.append(agent)
    return {"agents": agents, "errors": errors}


def agent_index(agents):
    """The custom agents for the system message: name and description, at most INDEX_ENTRIES lines and
    INDEX_BYTES bytes; '' without agents. It changes only when the files do, so prompt caching keeps
    the prefix before it."""
    if not agents:
        return ""
    lines = []
    for agent in agents:
        description = agent["description"]
        if len(description) > INDEX_DESCRIPTION:
            description = description[:INDEX_DESCRIPTION - 1] + "…"
        lines.append(f"- {agent['name']}: {description}\n")

    def more(count):
        return f"- … and {count} more (an unknown agent_type is refused with every name).\n" if count else ""
    kept = []
    for index, line in enumerate(lines):
        candidate = INDEX_HEADER + "".join(kept) + line + more(len(lines) - index - 1)
        if len(kept) >= INDEX_ENTRIES or len(candidate.encode("utf-8")) > INDEX_BYTES:
            break
        kept.append(line)
    return (INDEX_HEADER + "".join(kept) + more(len(lines) - len(kept))).rstrip("\n")


def built_in_agents(defaults):
    """The built-in types with their Settings → Subagents defaults ("" model or effort: the chat's)."""
    return [{"name": name, "built_in": True, "profile": profile, "description": description,
             "model": (defaults.get(name) or {}).get("model", ""), "effort": (defaults.get(name) or {}).get("effort", "")}
            for name, (profile, description) in BUILT_IN.items()]


def used_endpoints(settings, sessions):
    """The endpoints the user already uses: the Settings model, the Settings → Subagents models and the
    models of their own chats (not subagent chats, whose model an agent file may have chosen)."""
    names = {settings.values.get("model")}
    defaults = settings.values.get("subagent_defaults")
    if isinstance(defaults, dict):
        names |= {entry.get("model") for entry in defaults.values() if isinstance(entry, dict)}
    names |= {session.get("model") for session in sessions if isinstance(session, dict) and not session.get("is_subagent")}
    return {name.strip() for name in names if isinstance(name, str) and name.strip()}


def limit_model(agent, used):
    """An agent file names its endpoint only from those the user already uses (used_endpoints): a
    project's file must not send the subagent's requests, and the workspace content they carry, to
    another endpoint the token can reach. Any other model runs on the chat's model, with a note."""
    if not agent.get("model") or agent["model"] in used:
        return agent
    note = (f"Its model {agent['model']} is not an endpoint you use (the Settings model, Settings → Subagents or "
            "one of your chats), so it runs on the chat's model.")
    return {**agent, "model": None, "unused_model": agent["model"], "notes": [*agent.get("notes", []), note]}


def public_agent(agent):
    """A custom agent for Agent tools: its definition without the instructions text."""
    return {**{key: value for key, value in agent.items() if key not in ("instructions", "root")},
            "built_in": False, "instructions_chars": len(agent["instructions"])}


def narrower(*profiles):
    asked = [validate_profile(value) for value in profiles if value is not None]
    return min(asked, key=PROFILES.index) if asked else None


def resolve(agent_type, tool_profile, catalog, defaults):
    """The agent a delegate_task call asks for: {name, custom, requested (the tool profile it asks
    for, None for the chat's ceiling), model, effort (None: the chat's), tools, skills, instructions,
    max_steps, path}. An unknown agent_type is a ValueError that lists the available ones."""
    if agent_type is not None and not isinstance(agent_type, str):
        raise ValueError("agent_type must be explore, general, editor, or a custom agent's name.")
    if agent_type is None or agent_type in BUILT_IN:
        kind = agent_type or PROFILE_TYPES.get(tool_profile, "general")
        entry = defaults.get(kind) if isinstance(defaults.get(kind), dict) else {}
        return {"name": kind, "custom": False, "requested": narrower(BUILT_IN[agent_type][0] if agent_type else None,
                                                                         tool_profile),
                "model": entry.get("model") or None, "effort": entry.get("effort") or None, "tools": None,
                "skills": [], "instructions": "", "max_steps": None, "path": None}
    agent = next((item for item in catalog["agents"] if item["name"] == agent_type), None)
    if agent is None:
        names = [*BUILT_IN, *(item["name"] for item in catalog["agents"])]
        broken = next((item for item in catalog["errors"] if Path(item["path"]).stem == agent_type), None)
        raise ValueError(f"Unknown agent_type {agent_type[:64]!r}"
                         + (f" ({broken['path']} cannot be used: {broken['error']})" if broken else "")
                         + ". Available agent types: " + ", ".join(names) + ".")
    return {"name": agent["name"], "custom": True, "requested": narrower(agent["profile"], tool_profile),
            "model": agent["model"], "effort": agent["effort"], "tools": agent["tools"], "skills": agent["skills"],
            "instructions": agent["instructions"], "max_steps": agent["max_steps"], "path": agent["path"],
            "unused_model": agent.get("unused_model")}


def child_tools(tools, profile):
    """An agent's tools list within the child's effective profile (None: no list, every tool of it)."""
    if tools is None:
        return None
    permitted = profile_tools(profile)
    return [name for name in tools if permitted is None or name in permitted]


def effort_label(effort):
    return EFFORT_LABELS.get(effort, effort)
