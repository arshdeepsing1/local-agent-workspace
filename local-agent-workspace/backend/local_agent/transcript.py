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
PLAN_CHARS = 20000  # submit_plan's own limit: a plan is shown in full.
PLAN_MODES = {"acceptEdits": "Accept edits", "manual": "Manual"}
QUESTION_OUTCOMES = {"answered": "answered", "skipped": "skipped", "expired": "not answered in time"}
PLAN_OUTCOMES = {"keep_planning": "kept planning", "expired": "not reviewed in time",
                 "superseded": "not reviewed: the mode was changed from the menu"}


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


def _failure(state, output):
    first = output.strip().splitlines()[:1]
    return ": " + _clip(first[0], DETAIL_CHARS) if state in ("error", "rejected", "cancelled") and first else ""


def _questions(event, arguments, state, output):
    """ask_user: each question with the user's answer, or how the questions ended."""
    outcome = event.get("question_outcome")
    label = QUESTION_OUTCOMES.get(outcome, state)
    if outcome == "skipped" and event.get("skipped_reason") == "dont_ask":
        label += " in Don't ask mode"
    answers = [item for item in event.get("answers") or [] if isinstance(item, dict)] if outcome == "answered" else []
    asked = answers or [item for item in arguments.get("questions") or [] if isinstance(item, dict)]
    lines = [f"- Asked the user ({label})" + (_failure(state, output) or ":")]
    for item in asked:
        lines.append(_indent("- " + " ".join(str(item.get("question") or "").split())))
        if answers:
            chosen = [str(choice) for choice in item.get("selected") or []]
            if item.get("other"):
                chosen.append("Other: " + str(item["other"]))
            lines.append(_indent(_indent("Answer: " + " ".join("; ".join(chosen).split()))))
    return "\n".join(lines)


def _plan(event, arguments, state, output):
    """submit_plan: the plan in full, the review outcome and the user's feedback."""
    outcome = event.get("plan_outcome")
    if outcome == "approved":
        label = f"approved; mode: {PLAN_MODES.get(event.get('plan_mode'), event.get('plan_mode'))}"
    else:
        label = PLAN_OUTCOMES.get(outcome, state)
    title = " ".join(str(arguments.get("title") or "").split())
    lines = [f"- Plan{' ' + json.dumps(title, ensure_ascii=False) if title else ''} ({label})" + (_failure(state, output) or ":")]
    if str(arguments.get("plan") or "").strip():
        lines.append(_indent(_fenced(_clip(arguments["plan"], PLAN_CHARS))))
    if isinstance(event.get("feedback"), str) and event["feedback"].strip():
        lines.append(_indent("Feedback: " + event["feedback"].strip()))
    return "\n".join(lines)


def _tool(event, excerpt_chars):
    name = str(event.get("name") or "tool")
    arguments = event.get("input") if isinstance(event.get("input"), dict) else {}
    state = str(event.get("state") or "unknown")
    output = event.get("output") if isinstance(event.get("output"), str) else ""
    result = _json(output)
    path = arguments.get("path", ".")
    if name == "ask_user":
        return _questions(event, arguments, state, output)
    if name == "submit_plan":
        return _plan(event, arguments, state, output)
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
    elif name == "move_path":
        detail = f"Moved {_code(path)} to {_code(arguments.get('destination', ''))}"
    elif name == "delete_path":
        detail = f"Deleted {_code(path)}"
    elif name in ("notebook_edit", "notebook_read"):
        detail = ("Edited notebook " if name == "notebook_edit" else "Read notebook ") + _code(path)
    elif name == "list_files":
        detail = f"Listed {_code(path)}" + (f" matching {_code(arguments['glob'])}" if arguments.get("glob") else "")
    elif name == "search_files":
        detail = f"Searched {_code(path)} for {_code(arguments.get('query', ''))}"
    elif name == "find_files":
        detail = f"Found files matching {_code(arguments.get('pattern', ''))} in {_code(path)}"
    elif name == "wait_job":
        detail = f"Waited for job {_code(str(arguments.get('job_id', ''))[:8])}"
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
        line += f" ({state})" + _failure(state, output)
    return line


def transcript_file_stem(session, *, redact=lambda text: text):
    """<date>-<title>-<id>, the name the transcript script and the app's export give a transcript.
    The title is redacted like the transcript itself, so a secret becomes "-redacted-"."""
    try:
        day = datetime.fromtimestamp(float(session.get("created"))).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        day = "undated"
    title = redact(redact_secrets(str(session.get("title") or "")))
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].strip("-") or "chat"
    return f"{day}-{slug}-{str(session.get('id', ''))[:8]}".rstrip("-")


def transcript_file_name(session, *, redact=lambda text: text):
    return transcript_file_stem(session, redact=redact) + ".md"


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
             "headings moved two levels down; each tool call shows its main input, questions show the user's "
             "answers, plans are shown in full with how they were reviewed, and command output is "
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
            lines += ["_Attached when sent: " + "; ".join((f"{_code('@' + str(item.get('path')))} lines {item.get('start_line')}–{item.get('end_line')}" + (", shortened" if item.get("truncated") else "")) if item.get("kind") == "file" else f"output of {_code('! ' + str(item.get('command', '')))} (exit {item.get('exit_code')})" for item in event["attachments"] if isinstance(item, dict)) + "_", ""] if isinstance(event.get("attachments"), list) and event["attachments"] else []
        elif kind == "assistant" and text:
            lines += ["**Assistant:**", "", _demote_headings(text), ""]
        elif kind in ("notice", "error") and text:
            lines += [f"> **{kind.title()}:** " + text.replace("\n", "\n> "), ""]
    lines += [*tools, ""] if tools else []
    return redact(redact_secrets("\n".join(lines).rstrip() + "\n"))
