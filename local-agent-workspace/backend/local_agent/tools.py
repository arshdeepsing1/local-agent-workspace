import asyncio
import difflib
import fnmatch
import json
import os
import stat
import sys
import time
from pathlib import Path

import regex as safe_regex

from .jobs import run_process

IGNORED = {".git", "node_modules", ".venv", ".codex_venv", "__pycache__", ".local", ".pytest_cache", "dist"}
SECRET_NAMES = {".env", "env_vars.txt", ".netrc", ".npmrc", "credentials", "credentials.json", "id_rsa", "id_ed25519"}
LIMIT = 80_000
QUERY_OUTPUT_LIMIT = 8_000
READ_SCAN_LIMIT = 8_000_000
READ_LINE_LIMIT = 128_000
SEARCH_FILE_LIMIT = 2_000_000
SEARCH_TOTAL_LIMIT = 16_000_000
SEARCH_FILE_SCAN_LIMIT = 5_000
SEARCH_DIR_SCAN_LIMIT = 2_000
QUERY_TIMEOUT = 3.0
REGEX_TIMEOUT = 0.05


def _integer(value, name, minimum, maximum=None):
    if type(value) is not int or value < minimum or maximum is not None and value > maximum:
        limit = f" through {maximum}" if maximum is not None else " or greater"
        raise ValueError(f"{name} must be an integer from {minimum}{limit}.")


def _output_size(value):
    # Match the model-facing serializer, including JSON escaping of non-ASCII text.
    return len(json.dumps(value, indent=2).encode("utf-8"))


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise ValueError("File query time limit reached; narrow the path, glob, or line range.")


def _excerpt(line, limit=300):
    encoded = line.encode("utf-8")
    if len(encoded) <= limit:
        return line, False
    return encoded[:limit].decode("utf-8", errors="ignore") + " … [line truncated]", True


def file_error(error: Exception) -> str:
    if isinstance(error, PermissionError) and sys.platform == "darwin":
        return (f"macOS denied access to {error.filename or 'this path'}. "
                "This is an operating-system permission, separate from the chat permission mode. "
                "In System Settings → Privacy & Security → Files and Folders, enable access to this folder "
                "for the application that launches the local server (for example, Terminal or Codex), "
                "then restart the server. Choosing Bypass permissions in chat cannot override macOS privacy settings.")
    return str(error)


class WorkspaceTools:
    def __init__(self, workspace: str, credential_file: str = "", allowed_directories=(), unrestricted=False):
        self.root = Path(workspace).expanduser().resolve()
        self.credential_file = Path(credential_file).expanduser().resolve() if credential_file else None
        self.allowed_directories = [Path(p).expanduser().resolve() for p in allowed_directories]
        self.unrestricted = unrestricted

    def resolve(self, value: str = ".") -> Path:
        requested = self.root / Path(value).expanduser()
        path = requested.resolve()
        credential = path == self.credential_file
        if self.credential_file and not credential:
            try:
                credential = path.samefile(self.credential_file)
            except FileNotFoundError:
                pass  # New files and missing credential files have no shared identity.
        if credential:
            raise ValueError("Credential files are excluded from file tools.")
        for candidate in (requested, path):
            relative = candidate.relative_to(self.root) if candidate.is_relative_to(self.root) else candidate
            parts = [part.casefold() for part in relative.parts]
            if any(p in SECRET_NAMES or p.startswith(".env.") and p != ".env.example" for p in parts):
                raise ValueError("Credential files are excluded from file tools.")
            if any(p in IGNORED for p in parts):
                raise ValueError("This internal or generated directory is excluded.")
        return path

    def permitted(self, path: Path) -> bool:
        return self.unrestricted or any(path.is_relative_to(root) for root in [self.root, *self.allowed_directories])

    def path(self, value: str = ".") -> Path:
        path = self.resolve(value)
        if not self.permitted(path):
            raise ValueError("This folder needs access approval in this conversation. Ask the agent to inspect its absolute path.")
        return path

    def display_path(self, path: Path) -> str:
        return str(path.relative_to(self.root)) if path.is_relative_to(self.root) else str(path)

    def list_files(self, path: str = ".", depth: int = 1, glob: str = "*"):
        base = self.path(path)
        if not base.is_dir():
            raise ValueError("Not a directory.")
        result = []

        def visit(folder, level):
            for child in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if len(result) >= 300:
                    break
                try:
                    safe = self.path(str(child))
                except ValueError:
                    continue
                entry = {"path": self.display_path(child), "name": child.name,
                         "directory": safe.is_dir()}
                if fnmatch.fnmatch(child.name.lower(), glob.lower()):
                    result.append(entry)
                if level > 1 and safe.is_dir() and not child.is_symlink():
                    visit(child, level - 1)
        visit(base, min(max(depth, 1), 3))
        return result

    def list_files_page(self, path=".", depth=1, glob="*", offset=0):
        _integer(offset, "offset", 0, 300)
        entries = self.list_files(path, depth, glob)
        result = {"entries": [], "next_offset": None, "truncated": False,
                  "listing_limit_reached": len(entries) == 300}
        for entry in entries[offset:]:
            candidate = {**result, "entries": [*result["entries"], entry],
                         "next_offset": None, "truncated": False}
            if _output_size(candidate) > QUERY_OUTPUT_LIMIT:
                if not result["entries"]:
                    raise ValueError("A file entry exceeds the 8 KB listing limit; choose a shorter path.")
                result.update(next_offset=offset + len(result["entries"]), truncated=True)
                break
            result["entries"].append(entry)
        return result

    def read_file(self, path: str):
        target = self.path(path)
        if target.stat().st_size > LIMIT:
            raise ValueError("File is too large for the text editor (80 KB limit).")
        text = target.read_text(encoding="utf-8")
        if "\x00" in text:
            raise ValueError("Binary files cannot be opened in the text editor.")
        return text

    def read_file_range(self, path: str, start_line: int = 1, max_lines: int = 200):
        _integer(start_line, "start_line", 1)
        _integer(max_lines, "max_lines", 1, 1000)
        target = self.path(path)
        if not stat.S_ISREG(target.stat().st_mode):
            raise ValueError("Only regular UTF-8 files can be read.")
        result = {"path": self.display_path(target), "start_line": start_line,
                  "end_line": start_line - 1, "content": "", "next_line": None, "truncated": False}
        if _output_size(result) > QUERY_OUTPUT_LIMIT:
            raise ValueError("Path or line offset exceeds the 8 KB ranged output limit.")
        deadline = time.monotonic() + QUERY_TIMEOUT
        scanned = 0
        lines = []
        with target.open("rb") as source:
            number = 0
            while True:
                _check_deadline(deadline)
                raw = source.readline(READ_LINE_LIMIT + 1)
                if not raw:
                    return result
                scanned += len(raw)
                number += 1
                if scanned > READ_SCAN_LIMIT:
                    raise ValueError("Line range requires scanning more than 8 MB; choose a smaller start_line.")
                if len(raw) > READ_LINE_LIMIT:
                    raise ValueError(f"Line {number} is too long for ranged reading (128 KB limit).")
                if b"\x00" in raw:
                    raise ValueError("Binary files cannot be read as text.")
                line = raw.decode("utf-8").rstrip("\r\n")
                if number < start_line:
                    continue
                content = "\n".join([*lines, f"{number}: {line}"])
                candidate = {**result, "end_line": number, "content": content,
                             "next_line": max(1000, number + 1), "truncated": False}
                if _output_size(candidate) > QUERY_OUTPUT_LIMIT:
                    if not lines:
                        raise ValueError(f"Line {number} is too large for the 8 KB ranged output limit.")
                    result.update(next_line=number, truncated=True)
                    return result
                lines.append(f"{number}: {line}")
                result.update(end_line=number, content=content)
                if len(lines) >= max_lines:
                    if source.read(1):
                        result.update(next_line=number + 1, truncated=True)
                    return result

    def change(self, name: str, arguments: dict, apply=False):
        target = self.path(arguments["path"])
        old = self.read_file(arguments["path"]) if target.exists() else ""
        if name == "edit_file":
            needle = arguments["old_text"]
            if not needle or old.count(needle) != 1:
                raise ValueError("old_text must match exactly once; include more surrounding context.")
            new = old.replace(needle, arguments["new_text"], 1)
        else:
            new = arguments["content"]
        if len(new.encode()) > LIMIT:
            raise ValueError("File content exceeds 80 KB.")
        diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                           fromfile=arguments["path"], tofile=arguments["path"]))
        if apply:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new)
        return diff or "No changes."

    def search_files(self, query: str, glob: str = "*", path: str = ".", *,
                     regex: bool = False, case_sensitive: bool = False, context_lines: int = 0,
                     offset: int = 0, max_results: int = 50):
        if not isinstance(query, str) or not query:
            raise ValueError("Search text cannot be empty.")
        if len(query) > 1000:
            raise ValueError("Search text exceeds the 1,000 character limit.")
        if type(regex) is not bool or type(case_sensitive) is not bool:
            raise ValueError("regex and case_sensitive must be booleans.")
        _integer(context_lines, "context_lines", 0, 5)
        _integer(offset, "offset", 0, 10_000)
        _integer(max_results, "max_results", 1, 100)
        pattern = None
        if regex:
            try:
                pattern = safe_regex.compile(query, safe_regex.VERSION1 | (0 if case_sensitive else safe_regex.IGNORECASE))
            except (safe_regex.error, RecursionError) as error:
                raise ValueError(f"Invalid regular expression: {error}") from error
        needle = query if case_sensitive else query.casefold()
        def unreadable(error):
            raise error

        search_root = self.path(path)
        if search_root.is_dir():
            search_base = search_root
            entries = os.walk(search_root, followlinks=False, onerror=unreadable)
        elif search_root.is_file():
            search_base = search_root.parent
            entries = ((str(search_base), [], [search_root.name]),)
        else:
            raise ValueError("Search path must be a regular file or directory.")
        result = {"matches": [], "next_offset": None, "truncated": False, "skipped_files": 0}
        deadline = time.monotonic() + QUERY_TIMEOUT
        scanned_files = scanned_dirs = scanned_bytes = found = 0

        for base, dirs, files in entries:
            _check_deadline(deadline)
            scanned_dirs += 1
            if scanned_dirs > SEARCH_DIR_SCAN_LIMIT:
                raise ValueError("Search directory scan limit reached; narrow the path.")
            safe_dirs = []
            for dirname in sorted(dirs):
                _check_deadline(deadline)
                child = Path(base) / dirname
                try:
                    self.path(str(child))
                    if not child.is_symlink():
                        safe_dirs.append(dirname)
                except (OSError, ValueError):
                    continue
            dirs[:] = safe_dirs
            for filename in sorted(files):
                _check_deadline(deadline)
                scanned_files += 1
                if scanned_files > SEARCH_FILE_SCAN_LIMIT:
                    raise ValueError("Search file scan limit reached; narrow the path.")
                child = Path(base) / filename
                if not fnmatch.fnmatch(str(child.relative_to(search_base)), glob):
                    continue
                try:
                    target = self.path(str(child))
                    info = target.stat()
                    if not stat.S_ISREG(info.st_mode) or info.st_size > SEARCH_FILE_LIMIT:
                        result["skipped_files"] += 1
                        continue
                    with target.open("rb") as source:
                        raw = source.read(SEARCH_FILE_LIMIT + 1)
                except (OSError, ValueError):
                    result["skipped_files"] += 1
                    continue
                scanned_bytes += len(raw)
                if scanned_bytes > SEARCH_TOTAL_LIMIT:
                    raise ValueError("Search byte scan limit reached (16 MB); narrow the path or glob.")
                if len(raw) > SEARCH_FILE_LIMIT or b"\x00" in raw:
                    result["skipped_files"] += 1
                    continue
                try:
                    content = raw.decode("utf-8")
                except UnicodeError:
                    result["skipped_files"] += 1
                    continue
                lines = content.splitlines()
                for index, line in enumerate(lines):
                    _check_deadline(deadline)
                    try:
                        matches = (pattern.search(line, timeout=max(0.001, min(REGEX_TIMEOUT, deadline - time.monotonic())), concurrent=True)
                                   if pattern else needle in (line if case_sensitive else line.casefold()))
                    except TimeoutError as error:
                        raise ValueError("Regular expression time limit reached; simplify the pattern or narrow the search.") from error
                    if not matches:
                        continue
                    found += 1
                    if found <= offset:
                        continue
                    text, clipped = _excerpt(line)
                    entry = {"path": self.display_path(child), "line": index + 1, "text": text,
                             "before": [], "after": [], "text_truncated": clipped}
                    for key, positions in (("before", range(max(0, index - context_lines), index)),
                                           ("after", range(index + 1, min(len(lines), index + context_lines + 1)))):
                        for position in positions:
                            text, clipped = _excerpt(lines[position])
                            entry[key].append({"line": position + 1, "text": text})
                            entry["text_truncated"] |= clipped
                    candidate = {**result, "matches": [*result["matches"], entry],
                                 "next_offset": 10_000, "skipped_files": SEARCH_FILE_SCAN_LIMIT}
                    if len(result["matches"]) >= max_results or _output_size(candidate) > QUERY_OUTPUT_LIMIT:
                        if not result["matches"]:
                            raise ValueError("A search match exceeds the 8 KB output limit; reduce context_lines.")
                        next_offset = offset + len(result["matches"])
                        if next_offset > 10_000:
                            raise ValueError("Search pagination limit reached; narrow the path, glob, or query.")
                        result.update(next_offset=next_offset, truncated=True)
                        return result
                    result["matches"].append(entry)
                    result["truncated"] |= entry["text_truncated"]
        return result

    async def run_command(self, command: str, timeout_seconds=60, max_output_bytes=LIMIT, on_output=None):
        return await run_process(command, self.root, timeout_seconds, max_output_bytes, on_output)

    async def execute(self, name: str, arguments: dict):
        if name == "run_command":
            return await self.run_command(arguments["command"], arguments.get("timeout_seconds", 60),
                                          arguments.get("max_output_bytes", LIMIT))
        if name in ("write_file", "edit_file"):
            return self.change(name, arguments, apply=True)
        if name == "list_files":
            return self.list_files_page(arguments.get("path", "."), arguments.get("depth", 1),
                                        arguments.get("glob", "*"), arguments.get("offset", 0))
        if name == "read_file":
            return await asyncio.to_thread(self.read_file_range, arguments["path"],
                                           arguments.get("start_line", 1), arguments.get("max_lines", 200))
        if name == "search_files":
            return await asyncio.to_thread(self.search_files, arguments["query"], arguments.get("glob", "*"), arguments.get("path", "."),
                                           **{key: arguments[key] for key in ("regex", "case_sensitive", "context_lines", "offset", "max_results") if key in arguments})
        raise ValueError(f"Unknown tool: {name}")


def definition(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}}


STRING = {"type": "string"}
TOOL_DEFINITIONS = [
    definition("list_files", "List local files using an absolute, home-relative or workspace-relative path. The app requests access for external folders. Optional glob filters names (e.g. *.csv). Returns entries and next_offset in pages of at most 8 KB; follow next_offset with the same path, depth and glob. If listing_limit_reached is true, narrow the folder or glob to find entries beyond the first 300. Excludes secrets and generated folders.",
               {"path": STRING, "depth": {"type": "integer"}, "glob": STRING,
                "offset": {"type": "integer", "minimum": 0, "maximum": 300}}, []),
    definition("read_file", "Read numbered lines from a regular UTF-8 file. Defaults to 200 lines; output is at most 8 KB. Follow next_line for more. Scanning is limited to 8 MB and individual lines to 128 KB. External paths trigger folder access approval.",
               {"path": STRING, "start_line": {"type": "integer", "minimum": 1},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 1000}}, ["path"]),
    definition("search_files", "Search one UTF-8 file or files under a directory path (defaults to workspace) for literal text or a bounded regular expression. Choose the narrowest path and glob; use regex=false unless regular-expression syntax is needed, and escape literal metacharacters when regex=true. Case insensitive by default. Returns matching lines, optional context, skipped_files, and next_offset for pagination; output is at most 8 KB. Skips binary, unreadable, excluded, and over-2-MB files. Scan and regex time limits return explicit errors. External paths trigger folder access approval.",
               {"query": STRING, "glob": STRING, "path": STRING, "regex": {"type": "boolean"},
                "case_sensitive": {"type": "boolean"}, "context_lines": {"type": "integer", "minimum": 0, "maximum": 5},
                "offset": {"type": "integer", "minimum": 0, "maximum": 10000},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 100}}, ["query"]),
    definition("write_file", "Create or replace a file subject to the selected permission mode; read existing files first.",
               {"path": STRING, "content": STRING}, ["path", "content"]),
    definition("edit_file", "Replace one exact text occurrence in a file, subject to the selected permission mode.",
               {"path": STRING, "old_text": STRING, "new_text": STRING}, ["path", "old_text", "new_text"]),
    definition("run_command", "Run a noninteractive workspace command under the selected permission mode. Streams output; returns a job ID and bounded first output page. Default foreground Stop cancels the job. Set background=true to continue beyond this turn; use get_job_output/list_jobs/stop_job. Do not daemonize or use shell '&'.",
               {"command": STRING, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600, "default": 60},
                "max_output_bytes": {"type": "integer", "minimum": 1024, "maximum": 1000000, "default": 80000},
                "background": {"type": "boolean", "default": False}}, ["command"]),
    definition("list_jobs", "List a page of command jobs belonging to this conversation, including state and exit code. Follow next_offset for older jobs.",
               {"offset": {"type": "integer", "minimum": 0, "maximum": 104, "default": 0}}, []),
    definition("get_job_output", "Read a bounded output page for a job in this conversation. Follow next_offset; poll running jobs only when useful. Offsets count characters in retained output; truncated means later process output was discarded.",
               {"job_id": STRING, "offset": {"type": "integer", "minimum": 0, "maximum": 1000000, "default": 0},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 8000, "default": 4000}}, ["job_id"]),
    definition("stop_job", "Stop a managed job belonging to this conversation and retrieve its final status. Does not undo completed side effects.",
               {"job_id": STRING}, ["job_id"]),
]
