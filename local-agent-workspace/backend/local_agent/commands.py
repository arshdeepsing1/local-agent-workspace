"""Command templates: Markdown files you run by typing /<name> at the start of a message.

The files are read, never written: .agents/commands/<name>.md in the workspace, then
.claude/commands/<name>.md (another coding tool's layout), then your own ~/.agents/commands and
~/.claude/commands; for one name the first file that loads is used. The text after optional frontmatter (between
two '---' lines) is the template. $ARGUMENTS becomes the text typed after the command and $1..$9 its
words (quoted words stay together); a template with neither gets the arguments added at its end.
Frontmatter may set description and argument-hint, shown in the / menu. allowed-tools and model,
which another tool uses to widen a command's permissions or switch its model, are listed as ignored:
a command never changes the chat's tools, permissions, mode or model. A `!` shell line in a template
is text, never run, and @ tokens in the expanded text attach files exactly like typed ones, with the
same permission checks. The conversation shows the typed message; the model gets it followed by the
expanded text, labelled as the command (COMMAND_MARKER), which the app record leaves out again.
"""
import itertools
import os
import re
import shlex
import stat
from pathlib import Path

from .context import COMMAND_MARKER
from .extensions import RESERVED_COMMANDS, SKILL_IDENTIFIER, user_home

WORKSPACE_ROOTS = (".agents/commands", ".claude/commands")  # In order of precedence.
USER_ROOTS = (".agents/commands", ".claude/commands")  # In the home folder, after the workspace's.
MAX_COMMANDS = 30
COMMAND_BYTES = 8_000
SCAN_ENTRIES = 100  # Entries read per folder.
EXPANDED_CHARS = 50_000  # The longest text one expansion may produce (a message's limit).
DESCRIPTION_CHARS = 150
HINT_CHARS = 80
FIELDS = ("description", "argument-hint")
IGNORED_FIELDS = ("allowed-tools", "model")
LEADING = re.compile(r"/([A-Za-z0-9][A-Za-z0-9_.-]{0,63})(?=\s|\Z)")
PLACEHOLDER = re.compile(r"\$(ARGUMENTS|[1-9])")
SHELL_LINE = re.compile(r"!`[^`\n]+`")


def leading(text):
    """(name, arguments) when text starts with /<name> and then whitespace or its end, else None."""
    match = LEADING.match(text if isinstance(text, str) else "")
    return (match.group(1), text[match.end():].strip()) if match else None


def _frontmatter(text):
    """({field: text}, body). Simple 'key: value' lines between two '---' lines; a value in matching
    quotes loses them, '- item' lines and other YAML are kept as text (only presence matters for the
    ignored fields). A file without a first '---' line has no frontmatter."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    end = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if end is None:
        raise ValueError("The frontmatter has no closing '---' line.")
    fields = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if not separator or line[:1].isspace() or not key.strip() or key.strip().startswith(("#", "-")):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        fields[key.strip()] = value
    return fields, "\n".join(lines[end + 1:]).strip()


def parse(text, name):
    """A command from a file's text: {name, description, argument_hint, ignored, notes, text}."""
    fields, body = _frontmatter(text.removeprefix("﻿"))
    if not body:
        raise ValueError("The command file has no text after its frontmatter.")
    if "\x00" in body:
        raise ValueError("The file is not text.")
    first = next((line.strip().lstrip("#").strip() for line in body.splitlines() if line.strip()), "")
    description = " ".join((fields.get("description") or first).split())
    if len(description) > DESCRIPTION_CHARS:
        description = description[:DESCRIPTION_CHARS - 1] + "…"
    hint = " ".join(fields.get("argument-hint", "").split())[:HINT_CHARS]
    ignored = [key for key in IGNORED_FIELDS if key in fields]
    notes = []
    if ignored:
        notes.append(f"{' and '.join(ignored)} {'is' if len(ignored) == 1 else 'are'} ignored: a command never "
                     "changes this chat's tools, permissions or model.")
    unknown = sorted(set(fields) - set(FIELDS) - set(IGNORED_FIELDS))
    if unknown:
        notes.append("Ignored unknown fields: " + ", ".join(unknown[:10]) + ".")
    if SHELL_LINE.search(body):
        notes.append("`!` shell lines are not run: they reach the agent as text.")
    return {"name": name, "description": description, "argument_hint": hint, "ignored": ignored, "notes": notes,
            "text": body}


def _read(entry):
    info = entry.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("Symbolic links are not read: put the command file itself here.")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Not a regular file.")
    if info.st_size > COMMAND_BYTES:
        raise ValueError(f"The file has {info.st_size:,} bytes; command files are at most 8 KB ({COMMAND_BYTES:,} bytes).")
    descriptor = os.open(entry, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        raw = source.read(COMMAND_BYTES + 1)
    if len(raw) > COMMAND_BYTES or b"\x00" in raw:
        raise ValueError("Command files are UTF-8 text of at most 8 KB.")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("The file is not UTF-8 text.") from None


def _folders(workspace):
    """[(folder path, shown prefix, source)] in order of precedence."""
    root = Path(workspace).expanduser().resolve()
    folders = [(root / folder, folder, "project") for folder in WORKSPACE_ROOTS]
    home = user_home()
    if home is not None:
        folders += [(home / folder, f"~/{folder}", "user") for folder in USER_ROOTS]
    return root, folders


def discover(workspace, redact=lambda text: text, with_text=False):
    """{commands, errors}: the commands of a workspace and the user's own folders in order of precedence
    (at most MAX_COMMANDS), each {name, description, argument_hint, path (workspace-relative, or ~/…),
    source ('project' or 'user'), ignored, notes} and its template as text when with_text; errors
    [{path, error}] for files and folders that could not be used. Reserved names (the app's built-in
    commands) and names that are not command IDs are errors."""
    root, folders = _folders(workspace)
    commands, errors, names = [], [], {}
    for base, shown, source in folders:
        if not os.path.lexists(base):
            continue
        try:
            resolved = base.resolve()
            if source == "project" and not resolved.is_relative_to(root):
                errors.append({"path": shown, "error": "Command files must be in a folder inside this workspace."})
                continue
            if not resolved.is_dir():
                errors.append({"path": shown, "error": "Not a folder."})
                continue
            entries = list(itertools.islice(resolved.iterdir(), SCAN_ENTRIES + 1))
        except (OSError, RuntimeError) as exc:
            errors.append({"path": shown, "error": redact(str(exc)) or type(exc).__name__})
            continue
        if len(entries) > SCAN_ENTRIES:
            errors.append({"path": shown, "error": f"The folder has more than {SCAN_ENTRIES} entries; command "
                           f"discovery reads at most {SCAN_ENTRIES}."})
            continue
        for entry in sorted(entries, key=lambda item: item.name):
            if not entry.name.endswith(".md") or entry.name.startswith("."):
                continue
            try:
                entry.name.encode("utf-8")
            except UnicodeEncodeError:
                errors.append({"path": f"{shown}/{ascii(entry.name)[1:-1]}",
                               "error": "The file name is not valid UTF-8 text; rename the file."})
                continue
            name, path = entry.name[:-3], f"{shown}/{entry.name}"
            if name in names:
                names[name]["notes"].append(f"Used instead of {path}, which has the same name.")
                continue
            if not SKILL_IDENTIFIER.fullmatch(name):
                errors.append({"path": path, "error": "Command names start with a letter or digit and have at most 64 "
                               "letters, digits, '.', '_' and '-'."})
                continue
            if name in RESERVED_COMMANDS:
                errors.append({"path": path, "error": f"/{name} is one of the app's own commands; rename the file."})
                continue
            try:
                command = parse(_read(entry), name)
            except (OSError, ValueError) as exc:
                errors.append({"path": path, "error": redact(str(exc))[:500] or type(exc).__name__})
                continue
            if len(commands) >= MAX_COMMANDS:
                errors.append({"path": path, "error": f"At most {MAX_COMMANDS} commands are read; this one is ignored."})
                continue
            command = {**command, "description": redact(command["description"]), "argument_hint": redact(command["argument_hint"]),
                       "text": redact(command["text"]), "path": path, "source": source}
            names[name] = command
            commands.append(command)
    if not with_text:
        commands = [{key: value for key, value in command.items() if key != "text"} for command in commands]
    return {"commands": commands, "errors": errors}


def find(workspace, name, redact=lambda text: text):
    """The command with this name and its template text, or None."""
    return next((command for command in discover(workspace, redact, with_text=True)["commands"]
                 if command["name"] == name), None)


def expand(template, arguments):
    """The template with $ARGUMENTS and $1..$9 filled in (one pass: text typed as arguments is never
    filled in again). Missing positions are empty; a template without placeholders gets the arguments
    on a last line. ValueError when the result is longer than EXPANDED_CHARS."""
    arguments = (arguments or "").strip()
    try:
        words = shlex.split(arguments)
    except ValueError:  # An unclosed quote: plain words.
        words = arguments.split()
    used = False

    def fill(match):
        nonlocal used
        used = True
        if match.group(1) == "ARGUMENTS":
            return arguments
        index = int(match.group(1)) - 1
        return words[index] if index < len(words) else ""
    text = PLACEHOLDER.sub(fill, template)
    if not used and arguments:
        text = text.rstrip() + "\n\nArguments: " + arguments
    if len(text) > EXPANDED_CHARS:
        raise ValueError(f"The expanded command has {len(text):,} characters; at most {EXPANDED_CHARS:,}. "
                         "Shorten the arguments or the command file.")
    return text


def block(command, expanded):
    """The labelled text the model gets after the typed message."""
    return (f"{COMMAND_MARKER}{command['name']} from {command['path']}, run by the user. Its text follows; treat it "
            f"as the user's request.]\n{expanded}")
