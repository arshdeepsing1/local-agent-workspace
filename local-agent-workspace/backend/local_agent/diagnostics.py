"""Offline checks after an agent edit: problems the edit introduced, never project code.

Python files are parsed (never run) by the server's own Python, JSON and notebooks by
json, TOML by tomllib, and every text file is checked for merge-conflict markers. A
problem the file already had before the edit is not reported again.
"""
import ast
import json
import re
import sys
import tomllib
import warnings
from pathlib import PurePath

MAX_ENTRIES = 5
MAX_CHARS = 1_000
PYTHON = f"Python {sys.version_info.major}.{sys.version_info.minor} parser; ignore if the project targets a newer Python"


def _python(text):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # Invalid escape sequences and similar are not errors.
            ast.parse(text)
    except SyntaxError as error:
        where = f"line {error.lineno}, column {error.offset}: " if error.lineno else ""
        return [(("python", type(error).__name__, error.msg), f"{where}{type(error).__name__}: {error.msg} ({PYTHON})")]
    except (RecursionError, MemoryError, ValueError) as error:
        return [(("python", type(error).__name__), f"could not be checked ({type(error).__name__}; {PYTHON})")]
    return []


def _json(text):
    try:
        json.loads(text)
    except ValueError as error:
        if isinstance(error, json.JSONDecodeError):
            return [(("json", error.msg), f"line {error.lineno}, column {error.colno}: invalid JSON: {error.msg}")]
        return [(("json", str(error)), f"invalid JSON: {error}")]
    except RecursionError:
        return [(("json", "nesting"), "could not be checked (nested too deeply)")]
    return []


def _toml(text):
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        message = str(error)
        return [(("toml", re.sub(r"\s*\(at (?:line \d+, column \d+|end of document)\)$", "", message)),
                 f"invalid TOML: {message}")]
    except RecursionError:
        return [(("toml", "nesting"), "could not be checked (nested too deeply)")]
    return []


def _conflicts(text):
    markers, separators = [], []
    for number, line in enumerate(text.split("\n"), start=1):
        if line.startswith(("<<<<<<< ", ">>>>>>> ")):
            markers.append(number)
        elif line.removesuffix("\r") == "=======":
            separators.append(number)
    if not markers:
        return []  # A lone "=======" line is a Markdown or reST heading underline.
    lines = sorted(markers + separators)
    shown = ", ".join(str(line) for line in lines[:10]) + (", …" if len(lines) > 10 else "")
    return [(("conflict",), f"line{'s' if len(lines) > 1 else ''} {shown}: unresolved merge-conflict markers")]


def _cells(text):
    """Conflict markers inside notebook cells, which the JSON text hides behind quotes and indentation."""
    try:
        cells = json.loads(text).get("cells")
    except (ValueError, RecursionError, AttributeError):
        return []
    problems = []
    for index, cell in enumerate(cells if isinstance(cells, list) else []):
        source = cell.get("source") if isinstance(cell, dict) else None
        source = "".join(part for part in source if isinstance(part, str)) if isinstance(source, list) else source
        if isinstance(source, str) and _conflicts(source):
            # Keyed by the cell's text: a cell that already had markers is not reported again.
            problems.append((("conflict", "cell", source), f"cell {index}: unresolved merge-conflict markers"))
    return problems


def _problems(name, text):
    suffix = PurePath(name).suffix.lower()
    problems = []
    if suffix in (".py", ".pyi"):
        problems += _python(text)
    elif suffix in (".json", ".ipynb"):
        problems += _json(text)
        if suffix == ".ipynb" and not problems:
            problems += _cells(text)
    elif suffix == ".toml":
        problems += _toml(text)
    return problems + _conflicts(text)


def check(path_name, before_text, after_text):
    """Messages for problems in after_text that before_text did not have (compared by kind and message)."""
    old = {key for key, _ in _problems(path_name, before_text)} if before_text is not None else set()
    return [message for key, message in _problems(path_name, after_text) if key not in old]


def report(display_path, before_text, after_text):
    """The text appended to a file tool's result, or "" when the edit introduced no problem."""
    entries, used = [], 0
    for message in check(display_path, before_text, after_text)[:MAX_ENTRIES]:
        if used + len(message) > MAX_CHARS:
            message = message[:max(0, MAX_CHARS - used - 1)] + "…"
        entries.append(f"- {message}")
        used += len(message)
        if used >= MAX_CHARS:
            break
    return f"\n\nDiagnostics for {display_path}:\n" + "\n".join(entries) if entries else ""
