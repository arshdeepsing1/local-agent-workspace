"""Readable Markdown transcript of a saved conversation.

A conversation file (<state dir>/conversations/<id>.jsonl) repeats every reply
in its exact model history and keeps whole tool results, so it is several times
larger than what happened in it, and some of its lines are too long for
read_file. The transcript keeps the messages, replies, notices and errors in
order, with one entry per tool call: its main input and, for commands and
failures, a shortened result. Another session can read a long project history
from it. Standard library only, so offline scripts can use it without the web app.
"""
import json
import re
from datetime import datetime

from .redaction import redact_secrets


EXCERPT_CHARS = 600
COMMAND_CHARS = 2000
DETAIL_CHARS = 300


def _json(text):
    try:
        value = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _when(timestamp):
    try:
        return datetime.fromtimestamp(float(timestamp)).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown time"


def _clip(text, limit):
    text = str(text).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + f" … [{len(text) - limit:,} more characters]"


def _fenced(text):
    fence = "`" * max(3, max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    return f"{fence}\n{text}\n{fence}"


def _code(text):
    """Inline code that survives backticks in paths and queries."""
    text = " ".join(str(text).split())
    fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _indent(text):
    return "\n".join("  " + line if line else line for line in text.splitlines())


def _demote_headings(text):
    """Put a reply's own headings below the transcript's turn headings. Lines
    inside fenced code, such as shell comments, stay as they are."""
    lines, fence = [], None
    for line in text.split("\n"):
        stripped = line.lstrip(" ")
        indented = len(line) - len(stripped) >= 4
        marker = None if indented else re.match(r"`{3,}|~{3,}", stripped)
        if fence is None and marker:
            fence = marker.group()
        elif fence is not None:
            # Only a bare run of the same character, at least as long, closes it.
            closing = marker and marker.group()[0] == fence[0] and len(marker.group()) >= len(fence)
            if closing and not stripped[marker.end():].strip():
                fence = None
        elif not indented and re.match(r"#{1,6}(?:[ \t]|$)", stripped):
            level = len(stripped) - len(stripped.lstrip("#"))
            line = "#" * min(6, level + 2) + stripped[level:]
        lines.append(line)
    return "\n".join(lines)


def _tool(event, excerpt_chars):
    name = str(event.get("name") or "tool")
    arguments = event.get("input") if isinstance(event.get("input"), dict) else {}
    state = str(event.get("state") or "unknown")
    output = event.get("output") if isinstance(event.get("output"), str) else ""
    result = _json(output)
    path = arguments.get("path", ".")
    if name == "run_command" and state == "completed":
        status = str(result.get("state") or event.get("job_state") or state).replace("_", " ")
        if type(result.get("exit_code")) is int:
            status += f", exit {result['exit_code']}"
        lines = [f"- Command ({status}):", _indent(_fenced(_clip(arguments.get("command", ""), COMMAND_CHARS)))]
        text = result["output"] if isinstance(result.get("output"), str) else ("" if result else output)
        if text.strip():
            lines.append(_indent("Output:\n" + _fenced(_clip(text, excerpt_chars))))
        return "\n".join(lines)
    if name == "run_command":
        detail = "Command " + _code(_clip(arguments.get("command", ""), DETAIL_CHARS))
    elif name == "read_file":
        detail = f"Read {_code(path)}"
        start, end = result.get("start_line"), result.get("end_line")
        if type(start) is int and type(end) is int and end >= start:
            detail += f" lines {start}–{end}"
    elif name in ("write_file", "edit_file"):
        detail = ("Wrote " if name == "write_file" else "Edited ") + _code(path)
    elif name == "list_files":
        detail = f"Listed {_code(path)}" + (f" matching {_code(arguments['glob'])}" if arguments.get("glob") else "")
    elif name == "search_files":
        detail = f"Searched {_code(path)} for {_code(arguments.get('query', ''))}"
    elif name == "access_directory":
        detail = f"Folder access for {_code(path)}"
    elif name == "insert_activity_log":
        detail = f"Inserted the activity log into {_code(path)}"
    elif name == "use_skill":
        detail = f"Selected skill {_code(arguments.get('skill_id', ''))}"
    elif name == "delegate_task":
        detail = "Delegated: " + _clip(arguments.get("task", ""), DETAIL_CHARS)
    else:
        detail = f"{_code(name)} {_clip(json.dumps(arguments, ensure_ascii=False), DETAIL_CHARS)}"
    line = f"- {detail}"
    if state != "completed":
        line += f" ({state})"
        first = output.strip().splitlines()[:1]
        if state in ("error", "rejected", "cancelled") and first:
            line += ": " + _clip(first[0], DETAIL_CHARS)
    return line


def transcript(session, *, excerpt_chars=EXCERPT_CHARS, redact=lambda text: text):
    """Markdown for one conversation. Common secret shapes and anything redact
    replaces (the app's configured credentials) are replaced with [REDACTED]."""
    events = [event for event in session.get("events", []) if isinstance(event, dict)]
    users = sum(event.get("type") == "user" for event in events)
    lines = [f"# Transcript: {session.get('title') or 'Untitled conversation'}", "",
             f"- Conversation `{session.get('id', 'unknown')}`, model `{session.get('model', 'unknown')}`, "
             f"{users} user {'message' if users == 1 else 'messages'}",
             f"- Workspace: `{session.get('workspace', 'unknown')}`",
             f"- Started {_when(session.get('created'))}; last updated {_when(session.get('updated'))}",
             "- Written by the app from the saved conversation file. Messages and replies are complete, with their "
             "headings moved two levels down; each tool call shows its main input, and command output is "
             f"shortened to {excerpt_chars:,} characters.", ""]
    tools, turn = [], 0
    for event in events:
        kind = event.get("type")
        if kind == "tool":
            tools.append(_tool(event, excerpt_chars))
            continue
        if tools:
            lines += [*tools, ""]
            tools = []
        text = str(event.get("text") or "").strip()
        if kind == "user":
            turn += 1
            delegated = (event.get("origin") or {}).get("kind") == "delegated"
            lines += ["---", "", f"## Turn {turn} · {_when(event.get('created'))}", "",
                      f"**User{' (delegated by the parent conversation)' if delegated else ''}:**", "",
                      _demote_headings(text) or "(empty message)", ""]
        elif kind == "assistant" and text:
            lines += ["**Assistant:**", "", _demote_headings(text), ""]
        elif kind in ("notice", "error") and text:
            lines += [f"> **{kind.title()}:** " + text.replace("\n", "\n> "), ""]
    lines += [*tools, ""] if tools else []
    return redact(redact_secrets("\n".join(lines).rstrip() + "\n"))
