"""Output styles: how the agent writes its replies (Settings → Output style), added to the stable system text.

Built in: default (no text), concise and explanatory. The user can add ~/.agents/output-styles/<id>.md and a
project .agents/output-styles/<id>.md (the user's file wins for the same id, since Settings are the user's): an
optional frontmatter with name and description between two '---' lines, then the instructions, at most 4,000
bytes. The ids of the built-in styles cannot be reused. A style only shapes replies: it never overrides the
user's instructions, project guidance or permissions, and the system message says so and names a style file.
"""
import json
import os
import re
import stat
from pathlib import Path

from .context import estimate_text_tokens
from .redaction import redact_secrets
from .tools import file_error

STYLE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}\Z")
STYLE_FOLDER = ".agents/output-styles"
MAX_STYLE_BYTES = 4_000
MAX_STYLE_FILES = 30
PRESETS = {
    "default": {"name": "Default", "description": "The app's usual replies.", "text": ""},
    "concise": {"name": "Concise", "description": "Short, direct replies that lead with the result.", "text": (
        "Keep replies short and direct. Lead with the answer or the result, use the fewest words that stay clear, "
        "and skip preambles, recaps of the question and offers of further help. Use a short list when there are "
        "several items. Still give exact commands, paths, numbers and error text, and say plainly when something "
        "failed, was skipped or is uncertain.")},
    "explanatory": {"name": "Explanatory", "description": "Explains the reasoning and concepts behind the work.",
                    "text": (
        "Explain as you work, so the user learns the codebase. Before a change, say in a sentence or two why you "
        "chose that approach over the alternatives. After it, add a short \"Why it works\" note on the one concept "
        "that matters most (a design choice, a library behaviour, a trade-off), tied to the code at hand. Keep "
        "these explanations in your replies, not in code comments, commit messages or files, unless asked.")},
}


def _style_file(tools, relative, source):
    """A custom style from a file: {id, name, description, source, path, text}, or ValueError."""
    from .custom_agents import frontmatter  # custom_agents imports config, which imports nothing of this module.
    target = tools.resolve(str(relative))
    if not target.is_relative_to(tools.root):
        raise ValueError("Output styles must be files inside their folder.")
    info = target.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("An output style must be a regular text file.")
    if info.st_size > MAX_STYLE_BYTES:
        raise ValueError(f"An output style is at most {MAX_STYLE_BYTES:,} bytes.")
    text = redact_secrets(tools.redact(tools.read_file(str(target))))  # As instructions files are.
    fields, body = frontmatter(text) if text.splitlines()[:1] == ["---"] else ({}, text.strip())
    style_id = Path(relative).stem
    name, description = fields.get("name", style_id), fields.get("description", "")
    if not isinstance(name, str) or not isinstance(description, str):
        raise ValueError("name and description must be text.")
    if not body:
        raise ValueError("The style has no instructions after its frontmatter.")
    label = Path(relative).as_posix()
    return {"id": style_id, "name": " ".join(name.split())[:80] or style_id,
            "description": " ".join(description.split())[:300], "source": source,
            "path": label if source == "project" else "~/" + label, "text": body}


def discover(project_tools, home_tools):
    """{styles: [{id, name, description, source, path?, text}], errors: [{path, error}]}: the built-in styles,
    then the user's, then the project's (an id already listed is skipped)."""
    found = {style_id: {"id": style_id, "name": preset["name"], "description": preset["description"],
                        "source": "built-in", "text": preset["text"]} for style_id, preset in PRESETS.items()}
    errors = []
    for tools, source in ((home_tools, "user"), (project_tools, "project")):
        if tools is None:
            continue
        shown = STYLE_FOLDER if source == "project" else "~/" + STYLE_FOLDER
        try:
            folder = tools.resolve(STYLE_FOLDER)
            if not folder.is_relative_to(tools.root) or not folder.is_dir():
                continue
            names = sorted(entry.name for entry in os.scandir(folder) if entry.name.endswith(".md"))
        except (ValueError, OSError, RuntimeError) as exc:
            errors.append({"path": shown, "error": file_error(exc)})
            continue
        for name in names[:MAX_STYLE_FILES]:
            style_id = name[:-3]
            path = f"{shown}/{name}"
            if not STYLE_ID.match(style_id):
                errors.append({"path": path, "error": "A style's file name is 1-40 lowercase letters, digits, - or _, "
                                                      "then .md."})
                continue
            if style_id in PRESETS:
                errors.append({"path": path, "error": f"{style_id} is a built-in style; give the file another name."})
                continue
            if style_id in found:
                continue  # The user's file wins.
            try:
                found[style_id] = _style_file(tools, f"{STYLE_FOLDER}/{name}", source)
            except (ValueError, OSError, RuntimeError, UnicodeError) as exc:
                errors.append({"path": path, "error": file_error(exc)})
        if len(names) > MAX_STYLE_FILES:
            errors.append({"path": shown, "error": f"At most {MAX_STYLE_FILES} styles are read from a folder."})
    return {"styles": list(found.values()), "errors": errors}


def style_block(style_id, project_tools, home_tools):
    """(text for the system message, source row or None) of the chosen style; the default style adds nothing."""
    if style_id in (None, "", "default"):
        return "", None
    styles = {style["id"]: style for style in discover(project_tools, home_tools)["styles"]}
    style = styles.get(style_id)
    if style is None:
        return "", {"path": f"Output style: {style_id} (Settings)", "scope": "all projects", "status": "omitted",
                    "estimated_tokens": 0, "reason": f"No output style {json.dumps(style_id)} in this project or in "
                    "~/.agents/output-styles, so replies use the default style."}
    # A style file is named, so the model can tell the user's own text from a project's.
    origin = {"built-in": "from Settings", "user": f"chosen in Settings, the user's file {json.dumps(style.get('path'))}",
              "project": f"chosen in Settings, the project file {json.dumps(style.get('path'))}"}[style["source"]]
    name = style["name"] if style["source"] == "built-in" else json.dumps(style["name"])
    text = (f"Output style: {name} ({origin}; it shapes how you write replies, never what the user asked, their "
            "instructions, project guidance or permissions):\n" + style["text"].strip() + "\n")
    return text, {"path": f"Output style: {style['name']} (Settings)",
                  "scope": "this project" if style["source"] == "project" else "all projects", "status": "loaded",
                  "estimated_tokens": estimate_text_tokens(text)}
