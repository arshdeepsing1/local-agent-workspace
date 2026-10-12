"""Exact-text edits and whole-file writes that keep a file's line endings.

Everything here is linear in the file size: edits replace raw spans found in the
LF view of the file, and whole-file writes keep the endings of the unchanged first
and last lines. Previews diff only the changed middle of the file.
"""
import bisect
import difflib
import re
from typing import NamedTuple

LINES = re.compile(r"[^\n]*\n|[^\n]+")
HUNK = re.compile(r"^@@ -(\d+)((?:,\d+)?) \+(\d+)((?:,\d+)?) @@")
CONTEXT = 3
NOT_FOUND = ("old_text was not found (after the earlier edits in this call). Read the file again and copy the "
             "text exactly; read_file line numbers are not part of the text.")


class Plan(NamedTuple):
    text: str  # The new file text, with the endings that will be written.
    prefix: str  # Put before the diff in the tool result ("" for a single plain edit or a write).


def split_lines(text):
    """Lines with their endings, split at "\\n" only (never at U+2028, form feeds or bare CR)."""
    return LINES.findall(text)


def majority_ending(text):
    """The file's most common line ending; CRLF wins a tie."""
    crlf = text.count("\r\n")
    return "\r\n" if crlf and crlf >= text.count("\n") - crlf else "\n"


def _plural(count, word):
    return f"{count} {word}" + ("" if count == 1 else "s")


def _edits(arguments):
    single = "old_text" in arguments or "new_text" in arguments
    listed = "edits" in arguments
    if single and listed or listed and "replace_all" in arguments:
        raise ValueError("Use either old_text and new_text, or edits, not both.")
    if listed:
        edits = arguments["edits"]
        if not isinstance(edits, list) or not 1 <= len(edits) <= 50 or not all(isinstance(item, dict) for item in edits):
            raise ValueError("edits must be a list of 1 to 50 {old_text, new_text} objects.")
        return edits, True
    if not single or "old_text" not in arguments or "new_text" not in arguments:
        raise ValueError("Give old_text and new_text, or edits.")
    return [arguments], False


def _replace_one(raw, old, new, replace_all, crlf):
    """Apply one replacement to the raw text; returns (text, replacements)."""
    if crlf:
        # Match in the LF view; unchanged regions keep their exact bytes.
        old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
        view = raw.replace("\r\n", "\n")
        # View positions of the "\n" of each CRLF pair, to map view offsets back to raw ones.
        pairs = [match.start() - index for index, match in enumerate(re.finditer("\r\n", raw))]
        new = new.replace("\n", majority_ending(raw))
    else:
        view, pairs = raw, []
    count = view.count(old)
    if count == 0:
        raise ValueError(NOT_FOUND)
    if count > 1 and not replace_all:
        raise ValueError(f"old_text matches {count} times; include more surrounding context so it matches once, "
                         "or set replace_all=true.")
    raw_offset = (lambda index: index + bisect.bisect_left(pairs, index)) if pairs else (lambda index: index)
    parts, position, start = [], 0, view.find(old)
    while start != -1:
        begin, end = raw_offset(start), raw_offset(start + len(old))
        parts += [raw[position:begin], new]
        position = end
        start = view.find(old, start + len(old)) if replace_all else -1
    parts.append(raw[position:])
    return "".join(parts), count if replace_all else 1


def apply_edits(before_text, arguments):
    """edit_file: replacements in order, each on the text the earlier ones produced."""
    edits, listed = _edits(arguments)
    crlf = "\r\n" in before_text
    text, replacements = before_text, 0
    for index, edit in enumerate(edits):
        label = f"edits[{index}]: " if listed else ""
        old, new, replace_all = edit.get("old_text"), edit.get("new_text"), edit.get("replace_all", False)
        try:
            if not isinstance(old, str) or not isinstance(new, str) or type(replace_all) is not bool:
                raise ValueError("old_text and new_text must be strings and replace_all a boolean.")
            if not old:
                raise ValueError("old_text cannot be empty.")
            text, count = _replace_one(text, old, new, replace_all, crlf)
        except ValueError as error:
            raise ValueError(label + str(error)) from None
        replacements += count
    plain = not listed and not arguments.get("replace_all")
    prefix = "" if plain else f"Applied {_plural(len(edits), 'edit')} ({_plural(replacements, 'replacement')}).\n\n"
    return text, prefix


def keep_endings(before_text, content):
    """write_file: give LF content the endings of the file it replaces."""
    if before_text is None or "\r\n" not in before_text or "\r" in content:
        return content
    if before_text.count("\n") == before_text.count("\r\n"):
        return content.replace("\n", "\r\n")
    # Mixed endings: unchanged first and last lines keep theirs; other lines get the majority.
    old, new = split_lines(before_text), split_lines(content)
    view = lambda line: line[:-2] + "\n" if line.endswith("\r\n") else line
    limit = min(len(old), len(new))
    prefix = 0
    while prefix < limit and view(old[prefix]) == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and view(old[-1 - suffix]) == new[-1 - suffix]:
        suffix += 1
    ending = majority_ending(before_text)
    middle = [line[:-1] + ending if line.endswith("\n") else line for line in new[prefix:len(new) - suffix]]
    return "".join([*old[:prefix], *middle, *old[len(old) - suffix:]])


def plan_change(name, arguments, before_text, limit):
    """The new text of a write_file or edit_file call. Raises ValueError without side effects."""
    if name == "edit_file":
        if before_text is None:
            raise ValueError("edit_file needs an existing file; use write_file to create it.")
        text, prefix = apply_edits(before_text, arguments)
    elif name == "write_file":
        content = arguments.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be a string.")
        text, prefix = keep_endings(before_text, content), ""
    else:
        raise ValueError("Only write_file and edit_file change file text.")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        size = None
    if size is None or size > limit or "\x00" in text:
        raise ValueError(f"File content must be UTF-8 text without NUL bytes and at most {limit // 1000} KB.")
    return Plan(text, prefix)


def _no_newline(lines):
    for line in lines:
        yield line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"


def preview_diff(display_path, before_text, after_text):
    """A unified diff of the LF views, computed on the changed middle only."""
    before, after = before_text or "", after_text or ""
    old_view, new_view = before.replace("\r\n", "\n"), after.replace("\r\n", "\n")
    if old_view == new_view:
        return "No changes." if before == after else "Only line endings change."
    old, new = split_lines(old_view), split_lines(new_view)
    limit = min(len(old), len(new))
    prefix = 0
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and old[-1 - suffix] == new[-1 - suffix]:
        suffix += 1
    # Keep a few unchanged lines on each side as context; shift hunk headers back.
    start = max(0, prefix - CONTEXT)
    old_part = old[start:len(old) - max(0, suffix - CONTEXT)]
    new_part = new[start:len(new) - max(0, suffix - CONTEXT)]

    def shift(line):
        match = HUNK.match(line)
        if not match or not start:
            return line
        return (f"@@ -{int(match[1]) + start}{match[2]} +{int(match[3]) + start}{match[4]} @@"
                + line[match.end():])

    lines = difflib.unified_diff(old_part, new_part, fromfile=display_path, tofile=display_path, n=CONTEXT)
    return "".join(_no_newline(shift(line) for line in lines))
