"""outline_file: the structure of one text file with line numbers, without reading it all.

Markdown headings (with the line where each section ends), Python classes and functions
(from ast, with end lines) and, for other languages, declarations found by a line scan.
Line numbers count LF-separated lines, as read_file does (tools.split_lines). Every
pattern runs through the `regex` module with a timeout and has bounded quantifiers, and
the whole outline has a 3-second deadline, so a pathological file cannot hang the app.
Paths go through WorkspaceTools.path(): the file tools' exclusions and folder access apply.
"""
import ast
import json
import stat
import time
from pathlib import Path

import regex as safe_regex

from . import tools as tool_rules
from .tools import (ENCODING_NOTE, MAX_LARGE_PAGE_BYTES, MIN_PAGE_BYTES, QUERY_OUTPUT_LIMIT, _integer, _output_size,
                    split_lines)

OUTLINE_SCAN_LIMIT = tool_rules.READ_SCAN_LIMIT  # 8 MB
PYTHON_AST_LIMIT = 1_000_000  # ast cannot be interrupted; about a second at this size.
MAX_OUTLINE_ENTRIES = 20_000
OUTLINE_OFFSET_LIMIT = 20_000
ENTRY_TEXT_CHARS = 160
SCAN_LINE_CHARS = 400  # Longer lines are never declaration entries.
HEADING_LINE_CHARS = 1_000
CHECK_EVERY = 256
TIMEOUT_MESSAGE = "Outlining this file took longer than 3 seconds; use search_files to find sections."
LIMIT_NOTE = "More than 20,000 entries; use max_level or search_files."
NO_ENTRIES_NOTE = "No headings or declarations found; use search_files or read_file."
NO_RULES_NOTE = "outline_file has no rules for this file type; use search_files or read_file with start_line."
BARE_CR_NOTE = ("This file contains carriage returns that are not line endings; outline from a line scan so line "
                "numbers match read_file.")
LARGE_PYTHON_NOTE = "Large Python file: outline from a line scan."

FORMATS = {
    "markdown": (".md", ".markdown", ".mdx", ".mdown", ".mkd", ".txt"),
    "python": (".py", ".pyi"),
    "javascript": (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts"),
    "go": (".go",),
    "rust": (".rs",),
    "jvm": (".java", ".kt", ".kts", ".scala", ".cs", ".swift", ".dart"),
    "c": (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh"),
    "ruby": (".rb",),
    "php": (".php",),
    "shell": (".sh", ".bash", ".zsh"),
    "sql": (".sql",),
    "config": (".toml", ".ini", ".cfg", ".conf"),
    "yaml": (".yml", ".yaml"),
    "make": (".mk",),
    "notebook": (".ipynb",),
}
SUFFIX_FORMATS = {suffix: name for name, suffixes in FORMATS.items() for suffix in suffixes}
MAKE_NAMES = ("makefile", "gnumakefile")


def outline_format(path):
    """The outline rules for a file name: a FORMATS key, or 'text'."""
    name = Path(path).name.lower()
    if name in MAKE_NAMES:
        return "make"
    return SUFFIX_FORMATS.get(Path(name).suffix, "text")


def _compile(pattern, flags=0):
    return safe_regex.compile(pattern, flags | safe_regex.VERSION0)


ATX = _compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
SETEXT_ONE = _compile(r"^ {0,3}=+[ \t]*$")
SETEXT_TWO = _compile(r"^ {0,3}-+[ \t]*$")
FENCE = _compile(r"^ {0,3}(`{3,}|~{3,})")
PARAGRAPH_EXCLUDED = _compile(r"^[ \t]*(?:[>\-*+|]|[0-9]{1,9}\.)")
PYTHON_SCAN = _compile(r"^(\s{0,200})(async\s{1,10}def|def|class)\s{1,10}[A-Za-z_]")
JS_METHOD_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "with", "else"}
C_FUNCTION_KEYWORDS = {"if", "for", "while", "switch", "return", "else", "do", "case", "typedef"}
# (pattern, check) per family; check(line, match) filters matches further (None: every match counts).
FAMILIES = {
    "javascript": [
        (_compile(r"^\s{0,40}(?:export\s{1,5}(?:default\s{1,5})?)?(?:declare\s{1,5})?(?:abstract\s{1,5})?"
                  r"class\s{1,5}[A-Za-z_$]"), None),
        (_compile(r"^\s{0,40}(?:export\s{1,5}(?:default\s{1,5})?)?(?:async\s{1,5})?function\b"), None),
        (_compile(r"^\s{0,40}(?:export\s{1,5})?(?:declare\s{1,5})?(?:interface|type|enum)\s{1,5}[A-Za-z_$]"), None),
        (_compile(r"^\s{0,40}(?:export\s{1,5})?(?:const|let|var)\s{1,5}[A-Za-z_$][\w$]{0,100}\s{0,5}"
                  r"(?::[^=]{0,200})?=\s{0,5}(?:async\s{1,5})?(?:function\b|\([^)]{0,300}\)\s{0,5}"
                  r"(?::[^=]{0,200})?=>|[A-Za-z_$][\w$]{0,100}\s{0,5}=>)"), None),
        (_compile(r"^\s{1,40}(?:(?:static|async|get|set|public|private|protected|readonly|override)\s{1,5}){0,4}"
                  r"\*?([A-Za-z_$][\w$]{0,100})\s{0,5}\([^)]{0,300}\)\s{0,5}(?::[^{]{0,200})?\{\s{0,5}$"),
         lambda line, match: match.group(1) not in JS_METHOD_KEYWORDS),
    ],
    "go": [
        (_compile(r"^func\s{1,5}(?:\([^)]{0,200}\)\s{0,5})?[A-Za-z_]"), None),
        (_compile(r"^type\s{1,5}[A-Za-z_]\w{0,100}\s{1,5}(?:struct|interface)\b"), None),
    ],
    "rust": [
        (_compile(r"^\s{0,40}(?:pub(?:\([^)]{0,40}\))?\s{1,5})?(?:async\s{1,5})?(?:unsafe\s{1,5})?(?:const\s{1,5})?"
                  r"(?:extern\s{1,5}\"[^\"]{0,20}\"\s{1,5})?(?:fn|struct|enum|trait|impl|mod|union)\b"), None),
    ],
    "jvm": [
        (_compile(r"^\s{0,40}(?:[\w@]{1,40}\s{1,5}){0,6}(?:class|interface|enum|record|object|struct|protocol|"
                  r"extension|trait)\s{1,5}[A-Za-z_]"), None),
        (_compile(r"^\s{0,40}(?:[\w@]{1,40}\s{1,5}){0,6}(?:fun|func|def)\s{1,5}"), None),
        (_compile(r"^\s{0,40}(?:(?:public|private|protected|internal|static|final|abstract|synchronized|native|"
                  r"default|override|async|virtual|sealed)\s{1,5}){1,6}[^;(){}=]{1,200}\("),
         lambda line, match: not line.rstrip().endswith(";")),
    ],
    "c": [
        (_compile(r"^\s{0,40}(?:template\s{0,5}<[^>]{0,200}>\s{0,5})?(?:class|struct|namespace|union|"
                  r"enum(?:\s{1,5}class)?)\s{1,5}[A-Za-z_][^;]{0,300}$"), None),
        (_compile(r"^[A-Za-z_~][^;(){}=#]{0,200}\("),
         lambda line, match: (not line.rstrip().endswith(";")
                              and line.split(None, 1)[0].split("(", 1)[0] not in C_FUNCTION_KEYWORDS)),
    ],
    "ruby": [(_compile(r"^\s{0,40}(?:class|module|def)\s{1,5}"), None)],
    "php": [(_compile(r"^\s{0,40}(?:(?:abstract|final|public|private|protected|static)\s{1,5}){0,4}"
                      r"(?:class|interface|trait|function)\s{1,5}&?[A-Za-z_]"), None)],
    "shell": [
        (_compile(r"^\s{0,40}(?:function\s{1,5})?[A-Za-z_][\w.-]{0,100}\s{0,5}\(\)\s{0,5}\{?$"), None),
        (_compile(r"^\s{0,40}function\s{1,5}[A-Za-z_]"), None),
    ],
    "sql": [(_compile(r"^\s{0,40}create\s{1,5}(?:or\s{1,5}replace\s{1,5})?(?:temp(?:orary)?\s{1,5})?(?:table|view|"
                      r"materialized\s{1,5}view|function|procedure|index|schema|trigger)\b", safe_regex.IGNORECASE),
             None)],
    "config": [(_compile(r"^\s{0,40}\[\[?\s{0,5}[^\]]{1,200}\]\]?\s{0,5}(?:[#;].{0,300})?$"), None)],
    "yaml": [(_compile(r"^[A-Za-z_][\w.-]{0,100}\s{0,5}:"), None)],
    "make": [(_compile(r"^[A-Za-z0-9_/%-][A-Za-z0-9_./%-]{0,200}\s{0,5}:(?!=)"), None)],
}


class _Full(Exception):
    """MAX_OUTLINE_ENTRIES entries were collected."""


class _Scan:
    """The deadline and the entry list of one outline."""

    def __init__(self):
        self.deadline = time.monotonic() + tool_rules.QUERY_TIMEOUT
        self.entries = []  # (line, end_line or None, level, text)
        self.full = False

    def tick(self, index):
        if index % CHECK_EVERY == 0 and time.monotonic() > self.deadline:
            raise ValueError(TIMEOUT_MESSAGE)

    def timeout(self):
        return max(0.001, self.deadline - time.monotonic())

    def match(self, pattern, line):
        try:
            return pattern.match(line, timeout=self.timeout())
        except TimeoutError:
            raise ValueError(TIMEOUT_MESSAGE) from None

    def add(self, line, end, level, text):
        if len(self.entries) >= MAX_OUTLINE_ENTRIES:
            self.full = True
            raise _Full
        self.entries.append((line, end, level, text))


def _columns(text):
    """Leading indentation in columns (a tab counts 4)."""
    width = 0
    for character in text:
        if character == " ":
            width += 1
        elif character == "\t":
            width += 4
        else:
            break
    return width


def _markdown(lines, scan):
    start = 0
    if lines and lines[0].rstrip() == "---":  # YAML front matter
        start = next((index + 1 for index in range(1, len(lines)) if lines[index].rstrip() == "---"), 0)
    fence, skip = None, -1
    headings = []  # [line, level, text]; end lines are filled below
    for index in range(start, len(lines)):
        scan.tick(index)
        line = lines[index]
        if index <= skip:
            continue
        stripped = line.lstrip(" ")
        indented = len(line) - len(stripped) >= 4
        marker = (scan.match(FENCE, line) if not indented and len(line) <= HEADING_LINE_CHARS
                  and stripped[:1] in ("`", "~") else None)
        if fence is not None:
            # Only a bare run of the same character, at least as long, closes a fence.
            if (marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence)
                    and not line[marker.end():].strip()):
                fence = None
            continue
        if marker:
            fence = marker.group(1)
            continue
        if indented or len(line) > HEADING_LINE_CHARS or not line.strip():
            continue
        atx = scan.match(ATX, line) if stripped.startswith("#") else None
        if atx:
            text = (atx.group(2) or "").strip()
            if text:
                headings.append([index + 1, len(atx.group(1)), text])
            continue
        if (index + 1 < len(lines) and len(lines[index + 1]) <= HEADING_LINE_CHARS
                and lines[index + 1].lstrip(" ")[:1] in ("=", "-") and not scan.match(PARAGRAPH_EXCLUDED, line)):
            following = lines[index + 1]
            level = 1 if scan.match(SETEXT_ONE, following) else 2 if scan.match(SETEXT_TWO, following) else None
            if level is not None:
                headings.append([index + 1, level, line.strip()])
                skip = index + 1
    # A section ends before the next heading of the same or a higher level.
    ends, open_sections = {}, []
    for position, (line, level, _) in enumerate(headings):
        while open_sections and headings[open_sections[-1]][1] >= level:
            ends[open_sections.pop()] = line - 1
        open_sections.append(position)
    for position, (line, level, text) in enumerate(headings):
        scan.add(line, ends.get(position, len(lines)), level, text)


def _python_ast(text, lines, scan):
    tree = ast.parse(text)
    found, visited = [], [0]

    def walk(nodes, level):
        for node in nodes:
            visited[0] += 1
            scan.tick(visited[0])
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([decorator.lineno for decorator in node.decorator_list] + [node.lineno])
                label = lines[node.lineno - 1].strip() if 1 <= node.lineno <= len(lines) else node.name
                found.append((start, node.end_lineno, level, label))
                if len(found) > MAX_OUTLINE_ENTRIES:
                    raise _Full
                if level < 6:
                    walk(node.body, level + 1)
            else:
                # if/else, for, while, with, try/except/else/finally and match cases add no level.
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.stmt, ast.excepthandler, ast.match_case)):
                        walk([child], level)
    try:
        walk(tree.body, 1)
    except _Full:
        pass
    found.sort(key=lambda item: item[0])
    for entry in found:
        scan.add(*entry)


def _line_scan(lines, scan, patterns):
    stack = []
    for index, whole in enumerate(lines):
        scan.tick(index)
        if len(whole) > SCAN_LINE_CHARS or not whole.strip():
            continue
        # A bare CR starts a new line for an interpreter but not for read_file: each part is
        # scanned, and reported on read_file's line.
        for line in whole.split("\r"):
            for pattern, check in patterns:
                found = scan.match(pattern, line)
                if found and (check is None or check(line, found)):
                    indent = _columns(line)
                    while stack and stack[-1] >= indent:
                        stack.pop()
                    level = min(6, len(stack) + 1)
                    stack.append(indent)
                    scan.add(index + 1, None, level, line.strip())
                    break


def _clip(text):
    return text if len(text) <= ENTRY_TEXT_CHARS else text[:ENTRY_TEXT_CHARS - 1] + "…"


def _entry(line, end, level, text, redact=None):
    indent = "  " * (level - 1)
    text = redact(text) if redact else text  # Before the cut, which could split the configured token.
    return f"{line}-{end} {indent}{_clip(text)}" if end is not None else f"{line} {indent}{_clip(text)}"


def outline_file(tools, path, *, offset=0, max_level=6, page_bytes=QUERY_OUTPUT_LIMIT):
    _integer(offset, "offset", 0, OUTLINE_OFFSET_LIMIT)
    _integer(max_level, "max_level", 1, 6)
    target = tools.path(path)
    info = target.stat()
    if stat.S_ISDIR(info.st_mode):
        raise ValueError("Only regular files can be outlined, and this is a folder; use project_overview or "
                         "find_files for folders.")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Only regular text files can be outlined.")
    kind = outline_format(target)
    if kind == "notebook":
        raise ValueError("Only regular text files can be outlined; use notebook_read for Jupyter notebooks.")
    too_large = ("This file exceeds the 8 MB outline and search limit; use read_file with tail_lines for its end, or "
                 "run_command with grep -n (if available) to find lines.")
    if info.st_size > OUTLINE_SCAN_LIMIT:
        raise ValueError(too_large)
    with target.open("rb") as source:
        raw = source.read(OUTLINE_SCAN_LIMIT + 1)
    if len(raw) > OUTLINE_SCAN_LIMIT:
        raise ValueError(too_large)
    if b"\x00" in raw:
        raise ValueError("Binary files cannot be outlined.")
    notes = []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        notes.append(ENCODING_NOTE)
    # A UTF-8 byte order mark (Windows editors) would hide a heading or definition on line 1,
    # and ast.parse rejects it in a str; line numbers do not change.
    text = text.removeprefix("\ufeff")
    lines = split_lines(text)
    scan = _Scan()
    try:
        if kind == "markdown":
            _markdown(lines, scan)
        elif kind == "python":
            fallback = None
            if len(raw) > PYTHON_AST_LIMIT:
                fallback = LARGE_PYTHON_NOTE
            elif safe_regex.search(r"\r(?!\n)", text, timeout=scan.timeout()) is not None:
                fallback = BARE_CR_NOTE
            else:
                try:
                    _python_ast(text, lines, scan)
                except (SyntaxError, ValueError, RecursionError, MemoryError) as error:
                    if isinstance(error, ValueError) and str(error) == TIMEOUT_MESSAGE:
                        raise
                    number = getattr(error, "lineno", None)
                    fallback = (f"Python could not parse this file{f' (line {number})' if number else ''}; this "
                                "outline comes from a line scan.")
                    scan.entries = []
            if fallback is not None:
                notes.append(fallback)
                _line_scan(lines, scan, [(PYTHON_SCAN, None)])
        elif kind in FAMILIES:
            _line_scan(lines, scan, FAMILIES[kind])
    except _Full:
        pass
    except TimeoutError:
        raise ValueError(TIMEOUT_MESSAGE) from None
    if scan.full:
        notes.append(LIMIT_NOTE)
    selected = [entry for entry in scan.entries if entry[2] <= max_level]
    if not scan.entries:
        notes.append(NO_RULES_NOTE if kind == "text" else NO_ENTRIES_NOTE)
    total = len(selected)
    if offset > total:
        raise ValueError(f"offset is beyond the {total:,} outline entries.")
    result = {"path": tools.display_path(target), "format": kind, "total_lines": len(lines), "file_bytes": len(raw),
              "entries": [], "total_entries": total, "next_offset": None, "truncated": False}
    if max_level < 6:
        result["max_level"] = max_level
    if notes:
        result["note"] = " ".join(notes)
    if scan.full:
        result["outline_limit_reached"] = True
    redact = getattr(tools, "redact", None)
    return _page(result, [_entry(*entry, redact) for entry in selected[offset:]], offset, page_bytes)


def _page(result, entries, offset, page_bytes):
    """result with as many entries as fit the page, and next_offset when more remain."""
    limit = min(MAX_LARGE_PAGE_BYTES, max(MIN_PAGE_BYTES, page_bytes)) if type(page_bytes) is int else QUERY_OUTPUT_LIMIT
    widest = {**result, "next_offset": OUTLINE_OFFSET_LIMIT, "truncated": True}
    # In indent=2 JSON, n strings in a list add their JSON text, 6 bytes each and 2 more.
    size = _output_size(widest) + 2
    kept = []
    for entry in entries:
        cost = len(json.dumps(entry)) + 6
        if size + cost > limit:
            break
        kept.append(entry)
        size += cost
    while kept and _output_size({**widest, "entries": kept}) > limit:
        kept.pop()  # Never expected: the arithmetic above is exact.
    result["entries"] = kept
    if len(kept) < len(entries):
        result.update(next_offset=offset + len(kept), truncated=True)
    return result
