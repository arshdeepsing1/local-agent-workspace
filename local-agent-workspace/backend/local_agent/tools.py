import asyncio
import fnmatch
import json
import os
import stat
import sys
import time
from pathlib import Path

import regex as safe_regex

from .edits import plan_change, preview_diff
from .jobs import bounded_regex, run_process

IGNORED = {".git", "node_modules", ".venv", ".codex_venv", "__pycache__", ".local", ".pytest_cache", "dist"}
SECRET_NAMES = {".env", "env_vars.txt", ".netrc", ".npmrc", "credentials", "credentials.json", "id_rsa", "id_ed25519"}
LIMIT = 80_000
DEFAULT_WRITE_LIMIT = 80_000
MIN_WRITE_LIMIT = 16_000
MAX_WRITE_LIMIT = 1_000_000
QUERY_OUTPUT_LIMIT = 8_000
READ_SCAN_LIMIT = 8_000_000
READ_LINE_LIMIT = 128_000
SEARCH_FILE_LIMIT = 2_000_000
SEARCH_TOTAL_LIMIT = 64_000_000
SEARCH_FILE_SCAN_LIMIT = 20_000
QUERY_TIMEOUT = 3.0
REGEX_TIMEOUT = 0.05
SEARCH_TIMEOUT = 10.0  # seconds for reading and matching, after the file list is complete
SEARCH_PAGE_LIMIT = 10_000  # the largest search offset
OUTPUT_MODES = ("content", "files_with_matches", "count")
MIN_PAGE_BYTES = 8_000
MAX_PAGE_BYTES = 32_000
MAX_LARGE_PAGE_BYTES = 128_000  # read_file pages from a 262,144-token context budget up
LARGE_PAGE_WINDOW = 262_144
DEFAULT_READ_LINES = 400
OUTLINE_HINT_BYTES = 64_000  # First pages of files at least this large point at outline_file.
MAX_READ_LINES = 2_000
TAIL_FILE_LIMIT = 512_000_000
TAIL_BLOCK = 65_536
COUNT_BLOCK = 1_048_576
INVALID_LINE_NAMES = 20
ENCODING_NOTE = "Some bytes are not valid UTF-8 and are shown as U+FFFD (\ufffd)."
FIND_OFFSET_LIMIT = 200_000
EXCLUDED_ROOT = "The app's settings and conversation folder is excluded from file tools."


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


def write_limit_setting(values):
    """The file write limit in bytes from Settings (write_limit_kb, 16 to 1,000), else the default."""
    kilobytes = values.get("write_limit_kb") if isinstance(values, dict) else None
    if type(kilobytes) is int and MIN_WRITE_LIMIT // 1000 <= kilobytes <= MAX_WRITE_LIMIT // 1000:
        return kilobytes * 1000
    return DEFAULT_WRITE_LIMIT


def error_clause(error: Exception) -> str:
    """file_error without its final period, for a message that continues after it."""
    return file_error(error).rstrip(".")


def file_error(error: Exception) -> str:
    if isinstance(error, PermissionError) and sys.platform == "darwin":
        return (f"macOS denied access to {error.filename or 'this path'}. "
                "This is an operating-system permission, separate from the chat permission mode. "
                "In System Settings → Privacy & Security → Files and Folders, enable access to this folder "
                "for the application that launches the local server (for example, Terminal or Codex), "
                "then restart the server. Choosing Bypass permissions in chat cannot override macOS privacy settings.")
    return str(error)


def split_lines(text):
    """Lines separated by LF only, each without its LF and one trailing CR.

    Bare CR, U+2028/2029, form feeds and the other characters str.splitlines()
    treats as breaks stay inside a line, so line numbers agree with read_file."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()  # A final LF ends the last line; an empty text has no lines.
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def read_page_bytes(context_window):
    """read_file's page size for a context budget: a quarter of it, 8,000 to 32,000 bytes;
    from a 262,144-token budget up, an eighth of it, at most 128,000 bytes."""
    try:
        budget = int(context_window)
    except (TypeError, ValueError, OverflowError):
        return MIN_PAGE_BYTES
    if budget >= LARGE_PAGE_WINDOW:
        return min(MAX_LARGE_PAGE_BYTES, budget // 8)
    return min(MAX_PAGE_BYTES, max(MIN_PAGE_BYTES, budget // 4))


def default_read_lines(page_bytes):
    """read_file's max_lines when the model omits it: 400, or more on large pages (fewer steps
    over a long file means fewer resends of the whole context)."""
    if type(page_bytes) is not int or page_bytes <= MAX_PAGE_BYTES:
        return DEFAULT_READ_LINES
    return min(MAX_READ_LINES, page_bytes // 64)


def excluded(parts):
    """'credential', 'generated' or None for the components of a path."""
    folded = [str(part).casefold() for part in parts]
    if any(p in SECRET_NAMES or p.startswith(".env.") and p != ".env.example" for p in folded):
        return "credential"
    if any(p in IGNORED for p in folded):
        return "generated"
    return None


def _identity(path):
    """(device, inode) of an existing path, or None when it cannot be read."""
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return None
    return info.st_dev, info.st_ino


def parts_below(path, folder):
    """The parts of path below folder (() for folder itself), or None when path is not folder or inside it.
    Matched by name, then by file identity of path and its parents: resolve() only follows symbolic links, so a
    case variant on a case-insensitive disk, a bind mount or a macOS firmlink is another name for the same folder."""
    path, folder = Path(path), Path(folder)
    if path.is_relative_to(folder):
        return path.relative_to(folder).parts
    target = _identity(folder)
    if target is not None:
        for ancestor in (path, *path.parents):
            if _identity(ancestor) == target:
                return path.relative_to(ancestor).parts
    return None


def _decode_line(raw):
    """A line as text, and whether it had bytes that are not valid UTF-8."""
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), True


def _text_cost(text):
    """Bytes a string adds inside a JSON string, as _output_size counts them."""
    return len(json.dumps(text)) - 2


class WorkspaceTools:
    def __init__(self, workspace: str, credential_file: str = "", allowed_directories=(), unrestricted=False,
                 write_limit=DEFAULT_WRITE_LIMIT, *, excluded_roots=(), secrets=()):
        self.root = Path(workspace).expanduser().resolve()
        # Folders file tools never reach (the app's state folder), except a workspace inside one.
        self.excluded_roots = [Path(p).expanduser().resolve() for p in excluded_roots]
        self._identities = {}
        self._excluded = [(root, _identity(root)) for root in self.excluded_roots]
        self._root_inside = [self._within(self.root, root, identity) for root, identity in self._excluded]
        self.credential_file = Path(credential_file).expanduser().resolve() if credential_file else None
        self.allowed_directories = [Path(p).expanduser().resolve() for p in allowed_directories]
        self.unrestricted = unrestricted
        self.write_limit = min(max(write_limit, MIN_WRITE_LIMIT), MAX_WRITE_LIMIT) if type(write_limit) is int else DEFAULT_WRITE_LIMIT
        # The configured token, resolved once: replaced in text a tool cuts (outline entries, search
        # excerpts) before the cut, which would leave a part the later exact match cannot find.
        self.secrets = tuple(secret for secret in secrets if isinstance(secret, str) and secret)

    def redact(self, text):
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

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
            reason = excluded(relative.parts)
            if reason == "credential":
                raise ValueError("Credential files are excluded from file tools.")
            if reason == "generated":
                raise ValueError("This internal or generated directory is excluded.")
        for candidate in (requested, path):
            if self.in_excluded_root(candidate):
                raise ValueError(EXCLUDED_ROOT)
        return path

    def in_excluded_root(self, path) -> bool:
        """Whether path is inside an excluded root (the app's state folder). A workspace
        inside that folder (a worktree) still reaches its own files, and only those."""
        path = Path(path)
        return any(self._within(path, root, identity)
                   and not (inside and self._within(path, self.root, self._folder_identity(self.root)))
                   for (root, identity), inside in zip(self._excluded, self._root_inside))

    def _within(self, path, folder, identity) -> bool:
        """Whether path is folder or inside it, by name or by file identity: resolve() only
        follows symbolic links, and a case variant on a case-insensitive disk, a bind mount
        or a macOS firmlink is another name for the same folder."""
        if path.is_relative_to(folder):
            return True
        return identity is not None and (_identity(path) == identity
                                         or any(self._folder_identity(item) == identity for item in path.parents))

    def _folder_identity(self, folder):
        """_identity of a parent folder, remembered: listings and searches ask about the same
        parents many times."""
        key = str(folder)
        if key not in self._identities:
            if len(self._identities) >= 4096:
                self._identities.clear()
            self._identities[key] = _identity(folder)
        return self._identities[key]

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

    def read_raw(self, path: str):
        """The exact text of a file (no newline translation), or None when it does not exist."""
        target = self.path(path)
        try:
            info = target.stat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Only regular UTF-8 files can be edited.")
        if info.st_size > MAX_WRITE_LIMIT:
            raise ValueError(f"File is too large to edit ({MAX_WRITE_LIMIT // 1000:,} KB limit).")
        with target.open("rb") as source:
            raw = source.read(MAX_WRITE_LIMIT + 1)
        if len(raw) > MAX_WRITE_LIMIT:
            raise ValueError(f"File is too large to edit ({MAX_WRITE_LIMIT // 1000:,} KB limit).")
        if b"\x00" in raw:
            raise ValueError("Binary files cannot be edited as text.")
        return raw.decode("utf-8")

    def read_file(self, path: str):
        target = self.path(path)
        limit = self.write_limit
        if target.stat().st_size > limit:
            raise ValueError(f"File is too large for the text editor ({limit // 1000:,} KB limit).")
        text = target.read_text(encoding="utf-8")
        if "\x00" in text:
            raise ValueError("Binary files cannot be opened in the text editor.")
        return text

    def read_file_range(self, path: str, start_line: int = 1, max_lines: int = 400, *,
                        tail_lines=None, page_bytes=QUERY_OUTPUT_LIMIT, outline_hint=False):
        _integer(start_line, "start_line", 1)
        _integer(max_lines, "max_lines", 1, MAX_READ_LINES)
        if tail_lines is not None:
            _integer(tail_lines, "tail_lines", 1, MAX_READ_LINES)
            if start_line != 1:
                raise ValueError("Use either start_line or tail_lines, not both.")
        limit = (min(MAX_LARGE_PAGE_BYTES, max(MIN_PAGE_BYTES, page_bytes)) if type(page_bytes) is int
                 else QUERY_OUTPUT_LIMIT)
        size_name = f"{limit // 1000} KB"
        target = self.path(path)
        info = target.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Only regular text files can be read.")
        result = {"path": self.display_path(target), "start_line": start_line,
                  "end_line": start_line - 1, "content": "", "next_line": None, "truncated": False}
        if tail_lines is not None:
            result["tail"] = True
        if _output_size(result) > limit:
            raise ValueError(f"Path or line offset exceeds the {size_name} ranged output limit.")
        deadline = time.monotonic() + QUERY_TIMEOUT
        if tail_lines is not None:
            return self._read_tail(target, info.st_size, tail_lines, result, limit, deadline)
        if outline_hint and start_line == 1 and info.st_size >= OUTLINE_HINT_BYTES and info.st_size > limit:
            from .outline import outline_format  # outline imports this module.
            if outline_format(target) not in ("text", "notebook"):
                # Before the scan, so the page is sized with these fields.
                result["file_bytes"] = info.st_size
                result["hint"] = (f"This file is {info.st_size // 1000:,} KB. outline_file lists its sections with "
                                  "line numbers; read the ones you need with start_line and max_lines.")
        scanned = 0
        lines, cost, invalid = [], 0, []

        def finish(**values):
            result.update(values)
            result["content"] = "\n".join(lines)
            if invalid:
                result.update(invalid_utf8_lines=invalid[:INVALID_LINE_NAMES], encoding_note=ENCODING_NOTE)
            return result

        with target.open("rb") as source:
            number = 0
            while True:
                _check_deadline(deadline)
                raw = source.readline(READ_LINE_LIMIT + 1)
                if not raw:
                    return finish()
                scanned += len(raw)
                number += 1
                if scanned > READ_SCAN_LIMIT:
                    raise ValueError("Line range requires scanning more than 8 MB; choose a smaller start_line.")
                if len(raw) > READ_LINE_LIMIT:
                    raise ValueError(f"Line {number} is too long for ranged reading (128 KB limit).")
                if b"\x00" in raw:
                    raise ValueError("Binary files cannot be read as text.")
                if number < start_line:
                    continue
                raw = raw[:-1] if raw.endswith(b"\n") else raw
                line, bad = _decode_line(raw[:-1] if raw.endswith(b"\r") else raw)
                entry = f"{number}: {line}"
                entry_cost = _text_cost(entry) + (2 if lines else 0)  # A joining LF is "\n" in JSON.
                marks = [*invalid, number] if bad else invalid
                metadata = {**result, "end_line": number, "next_line": max(1000, number + 1), "truncated": False,
                            **({"invalid_utf8_lines": marks[:INVALID_LINE_NAMES], "encoding_note": ENCODING_NOTE}
                               if marks else {})}
                if _output_size(metadata) + cost + entry_cost > limit:
                    if not lines:
                        raise ValueError(f"Line {number} is too large for the {size_name} ranged output limit.")
                    return finish(next_line=number, truncated=True)
                lines.append(entry)
                cost += entry_cost
                invalid = marks
                result["end_line"] = number
                if len(lines) >= max_lines:
                    if source.read(1):
                        return finish(next_line=number + 1, truncated=True)
                    return finish()

    def change(self, name: str, arguments: dict, apply=False):
        target = self.path(arguments["path"])
        before = self.read_raw(arguments["path"])
        plan = plan_change(name, arguments, before, self.write_limit)
        diff = preview_diff(self.display_path(target), before, plan.text)
        if apply:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(plan.text.encode("utf-8"))
            return plan.prefix + diff
        return diff

    def search_files(self, query: str, glob: str = "*", path: str = ".", *,
                     regex: bool = False, case_sensitive: bool = False, context_lines: int = 0,
                     offset: int = 0, max_results: int = 50, output_mode: str = "content",
                     include_ignored: bool = False):
        from . import search  # Not at module level: search imports git_tools, which imports this module.
        if not isinstance(query, str) or not query:
            raise ValueError("Search text cannot be empty.")
        if len(query) > 1000:
            raise ValueError("Search text exceeds the 1,000 character limit.")
        if type(regex) is not bool or type(case_sensitive) is not bool or type(include_ignored) is not bool:
            raise ValueError("regex, case_sensitive and include_ignored must be booleans.")
        if output_mode not in OUTPUT_MODES:
            raise ValueError("output_mode must be content, files_with_matches or count.")
        _integer(context_lines, "context_lines", 0, 5)
        _integer(offset, "offset", 0, SEARCH_PAGE_LIMIT)
        _integer(max_results, "max_results", 1, 100)
        pattern = None
        if regex:
            try:
                pattern = bounded_regex(query, safe_regex.VERSION1 | (0 if case_sensitive else safe_regex.IGNORECASE))
            except (safe_regex.error, RecursionError) as error:
                raise ValueError(f"Invalid regular expression: {error}") from error
        elif not case_sensitive:
            # Unicode simple case folding: one character for one character, as rg candidates assume.
            pattern = safe_regex.compile(safe_regex.escape(query), safe_regex.IGNORECASE | safe_regex.VERSION0)

        search_root = self.path(path)
        if not search_root.is_dir() and not search_root.is_file():
            raise ValueError("Search path must be a regular file or directory.")
        entries, note = search.list_universe(self, search_root, include_ignored=include_ignored,
                                             deadline=time.monotonic() + search.ENUMERATION_TIMEOUT)
        entries = [entry for entry in entries if fnmatch.fnmatch(entry.relative, glob)]
        # One file named as the path is searched as far as read_file and outline_file reach.
        file_limit = READ_SCAN_LIMIT if search_root.is_file() else SEARCH_FILE_LIMIT
        if search_root.is_file() and entries and entries[0].size > file_limit:
            raise ValueError(f"This file is {entries[0].size / 1_000_000:.1f} MB, larger than the 8 MB limit for "
                             "searching one file. Use read_file with tail_lines for its end, or run_command with grep -n "
                             "(if available) to find lines.")
        # The same limits with and without rg: they apply to the files that could be read.
        if len(entries) > SEARCH_FILE_SCAN_LIMIT:
            raise ValueError(f"Search file scan limit reached ({SEARCH_FILE_SCAN_LIMIT:,} files); narrow the path or glob.")
        if sum(entry.size for entry in entries if entry.size <= file_limit) > SEARCH_TOTAL_LIMIT:
            raise ValueError(f"Search byte scan limit reached ({SEARCH_TOTAL_LIMIT // 1_000_000} MB); "
                             "narrow the path or glob.")
        deadline = time.monotonic() + SEARCH_TIMEOUT
        candidates = None
        if (search_root.is_dir() and not regex and not any(character in query for character in "\n\r\x00")
                and (case_sensitive or query.isascii())):
            rg = search.ripgrep()
            listed = search.rg_candidates(rg, search_root, query, case_sensitive, deadline) if rg else None
            if listed is not None:
                # rg does not follow symbolic links; Python reads those itself.
                candidates = listed | {search.candidate_key(entry.relative) for entry in entries if entry.symlink}

        key = {"content": "matches", "files_with_matches": "files", "count": "counts"}[output_mode]
        result = {key: [], **({"total_matches": 0} if output_mode == "count" else {}),
                  "next_offset": None, "truncated": False, "skipped_files": 0, **({"note": note} if note else {})}
        largest = {"next_offset": SEARCH_PAGE_LIMIT, "skipped_files": SEARCH_FILE_SCAN_LIMIT,
                   **({"total_matches": 10 ** 12} if output_mode == "count" else {})}

        def remaining():
            return max(0.001, deadline - time.monotonic())

        def full(item):
            """The page as it is when item does not fit; None when it does."""
            candidate = {**result, key: [*result[key], item], **largest}
            if len(result[key]) < max_results and _output_size(candidate) <= QUERY_OUTPUT_LIMIT:
                return None
            if not result[key]:
                raise ValueError("A search match exceeds the 8 KB output limit; reduce context_lines."
                                 if output_mode == "content" else
                                 "A search result exceeds the 8 KB output limit; narrow the path.")
            next_offset = offset + len(result[key])
            if next_offset > SEARCH_PAGE_LIMIT:
                raise ValueError("Search pagination limit reached; narrow the path, glob, or query.")
            result.update(next_offset=next_offset, truncated=True)
            return result

        found = 0
        for item in entries:
            _check_deadline(deadline)
            if item.size > file_limit:
                result["skipped_files"] += 1
                continue
            if candidates is not None and search.candidate_key(item.relative) not in candidates:
                continue
            try:
                with open(item.path, "rb") as source:
                    raw = source.read(file_limit + 1)
            except OSError:
                result["skipped_files"] += 1
                continue
            if len(raw) > file_limit:
                result["skipped_files"] += 1
                continue
            if b"\x00" in raw:
                continue  # Binary: not searched, and not counted.
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue  # Not UTF-8: not searched, and not counted.
            try:
                if pattern is None and query not in content:
                    continue
                if not regex and pattern is not None and not pattern.search(content, timeout=remaining(), concurrent=True):
                    continue
            except TimeoutError as error:
                raise ValueError("File query time limit reached; narrow the path, glob, or line range.") from error
            lines = split_lines(content)
            name = self.display_path(item.path)
            count = 0
            for index, line in enumerate(lines):
                _check_deadline(deadline)
                try:
                    if pattern is None:
                        matches = query in line
                    elif regex:
                        matches = pattern.search(line, timeout=max(0.001, min(REGEX_TIMEOUT, deadline - time.monotonic())),
                                                 concurrent=True)
                    else:
                        matches = pattern.search(line, timeout=remaining(), concurrent=True)
                except TimeoutError as error:
                    raise ValueError("Regular expression time limit reached; simplify the pattern or narrow the search."
                                     if regex else "File query time limit reached; narrow the path, glob, or line range.") from error
                if not matches:
                    continue
                if output_mode != "content":
                    count += 1
                    if output_mode == "files_with_matches":
                        break
                    continue
                found += 1
                if found <= offset:
                    continue
                text, clipped = _excerpt(self.redact(line))
                entry = {"path": name, "line": index + 1, "text": text,
                         "before": [], "after": [], "text_truncated": clipped}
                for side, positions in (("before", range(max(0, index - context_lines), index)),
                                        ("after", range(index + 1, min(len(lines), index + context_lines + 1)))):
                    for position in positions:
                        text, clipped = _excerpt(self.redact(lines[position]))
                        entry[side].append({"line": position + 1, "text": text})
                        entry["text_truncated"] |= clipped
                if full(entry):
                    return result
                result["matches"].append(entry)
                result["truncated"] |= entry["text_truncated"]
            if not count:
                continue
            found += 1
            if found > offset:
                entry = name if output_mode == "files_with_matches" else {"path": name, "count": count}
                if full(entry):
                    return result
                result[key].append(entry)
            if output_mode == "count":
                result["total_matches"] += count
        return result

    def find_files(self, pattern, path=".", *, sort="path", include_ignored=False, offset=0, max_results=200):
        from . import search  # Not at module level: search imports git_tools, which imports this module.
        matcher = search.compile_glob(pattern)
        if sort not in ("path", "modified"):
            raise ValueError("sort must be path or modified.")
        if type(include_ignored) is not bool:
            raise ValueError("include_ignored must be a boolean.")
        _integer(offset, "offset", 0, FIND_OFFSET_LIMIT)
        _integer(max_results, "max_results", 1, 500)
        base = self.path(path)
        if not base.is_dir() and not base.is_file():
            raise ValueError("Path must be a regular file or directory.")
        deadline = time.monotonic() + search.ENUMERATION_TIMEOUT
        entries, note = search.list_universe(self, base, include_ignored=include_ignored, deadline=deadline)

        def spellings(entry):
            """The pattern matches the path below `path`; one with a '/' also matches the
            path as the results show it, or the absolute path."""
            yield entry.relative
            if "/" in pattern:
                yield self.display_path(entry.path)
                if pattern.startswith("/"):
                    yield str(entry.path)
        matched = [entry for entry in entries
                   if any(search.glob_match(matcher, name, deadline) for name in spellings(entry))]
        if sort == "modified":
            stamped = []
            for entry in matched:
                search.check_deadline(deadline)
                try:
                    stamped.append((os.stat(entry.path).st_mtime, entry))
                except OSError:
                    continue  # Removed since it was listed.
            stamped.sort(key=lambda item: (-item[0], item[1].relative))
            items = [{"path": self.display_path(entry.path),
                      "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(modified))}
                     for modified, entry in stamped]
        else:
            items = [self.display_path(entry.path) for entry in matched]
        total = len(items)
        if offset > total:
            raise ValueError(f"offset is beyond the {total:,} matching files.")
        result = {"files": [], "total": total, "next_offset": None, "truncated": False, **({"note": note} if note else {})}
        for item in items[offset:]:
            candidate = {**result, "files": [*result["files"], item], "next_offset": FIND_OFFSET_LIMIT}
            if len(result["files"]) >= max_results or _output_size(candidate) > QUERY_OUTPUT_LIMIT:
                if not result["files"]:
                    raise ValueError("A file path exceeds the 8 KB output limit; choose a shorter path.")
                result.update(next_offset=offset + len(result["files"]), truncated=True)
                break
            result["files"].append(item)
        return result

    def _read_tail(self, target, size, tail_lines, result, limit, deadline):
        """The last tail_lines lines of a regular file, read backwards from its end."""
        size_name = f"{limit // 1000} KB"
        if size > TAIL_FILE_LIMIT:
            raise ValueError("tail_lines works on files up to 512 MB; use run_command with tail for larger files.")
        if size == 0:
            return result
        blocks, breaks, position, trailing = [], 0, size, False
        with target.open("rb") as source:
            while position > 0 and breaks < tail_lines:
                _check_deadline(deadline)
                step = min(TAIL_BLOCK, position, READ_SCAN_LIMIT - (size - position))
                if step <= 0:
                    raise ValueError("The last lines exceed 8 MB; use a smaller tail_lines.")
                position -= step
                source.seek(position)
                block = source.read(step)
                if len(block) != step:
                    raise ValueError("The file changed while it was read; read it again.")
                if not blocks:
                    trailing = block.endswith(b"\n")  # A final LF does not start an empty line.
                    breaks -= trailing
                blocks.append(block)
                breaks += block.count(b"\n")
            data = b"".join(reversed(blocks))
            body = data[:-1] if trailing else data
            start = len(body)
            for _ in range(tail_lines):
                start = body.rfind(b"\n", 0, start)
                if start < 0:
                    break
            start += 1  # -1 (the start of the file) becomes 0.
            number = data.count(b"\n", 0, start) + 1
            source.seek(0)
            counted = 0
            while counted < position:  # Absolute line numbers: count the breaks before the tail.
                _check_deadline(deadline)
                chunk = source.read(min(COUNT_BLOCK, position - counted))
                if not chunk:
                    raise ValueError("The file changed while it was read; read it again.")
                counted += len(chunk)
                number += chunk.count(b"\n")
        decoded = []
        for raw in body[start:].split(b"\n"):
            _check_deadline(deadline)
            raw = raw[:-1] if raw.endswith(b"\r") else raw
            if len(raw) > READ_LINE_LIMIT:
                raise ValueError(f"Line {number} is too long for ranged reading (128 KB limit).")
            if b"\x00" in raw:
                raise ValueError("Binary files cannot be read as text.")
            decoded.append((number, *_decode_line(raw)))
            number += 1
        kept, cost, invalid = [], 0, []
        last = decoded[-1][0]
        for number, line, bad in reversed(decoded):
            entry = f"{number}: {line}"
            entry_cost = _text_cost(entry) + (2 if kept else 0)
            marks = sorted([*invalid, number])[:INVALID_LINE_NAMES] if bad else invalid
            metadata = {**result, "start_line": number, "end_line": last,
                        **({"invalid_utf8_lines": marks, "encoding_note": ENCODING_NOTE} if marks else {})}
            if _output_size(metadata) + cost + entry_cost > limit:
                if not kept:
                    raise ValueError(f"Line {number} is too large for the {size_name} ranged output limit.")
                result["truncated"] = True  # Keeps the last lines that fit.
                break
            kept.append(entry)
            cost += entry_cost
            invalid = marks
            result.update(start_line=number, end_line=last)
        result["content"] = "\n".join(reversed(kept))
        if invalid:
            result.update(invalid_utf8_lines=invalid, encoding_note=ENCODING_NOTE)
        return result

    async def run_command(self, command: str, timeout_seconds=60, max_output_bytes=LIMIT, on_output=None):
        return await run_process(command, self.root, timeout_seconds, max_output_bytes, on_output)

    async def execute(self, name: str, arguments: dict, *, page_bytes=QUERY_OUTPUT_LIMIT, outline_hint=False):
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
                                           arguments.get("start_line", 1),
                                           arguments.get("max_lines", default_read_lines(page_bytes)),
                                           tail_lines=arguments.get("tail_lines"), page_bytes=page_bytes,
                                           outline_hint=outline_hint)
        if name == "outline_file":
            from . import outline  # outline imports this module.
            return await asyncio.to_thread(outline.outline_file, self, arguments["path"],
                                           offset=arguments.get("offset", 0), max_level=arguments.get("max_level", 6),
                                           page_bytes=page_bytes)
        if name == "project_overview":
            from . import overview  # overview imports this module.
            return await asyncio.to_thread(overview.project_overview, self, arguments.get("path", "."),
                                           include_ignored=arguments.get("include_ignored", False))
        if name == "search_files":
            return await asyncio.to_thread(self.search_files, arguments["query"], arguments.get("glob", "*"), arguments.get("path", "."),
                                           **{key: arguments[key] for key in ("regex", "case_sensitive", "context_lines", "offset",
                                                                               "max_results", "output_mode", "include_ignored")
                                              if key in arguments})
        if name == "find_files":
            return await asyncio.to_thread(self.find_files, arguments["pattern"], arguments.get("path", "."),
                                           **{key: arguments[key] for key in ("sort", "include_ignored", "offset", "max_results")
                                              if key in arguments})
        raise ValueError(f"Unknown tool: {name}")


def definition(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}}


STRING = {"type": "string"}
TOOL_DEFINITIONS = [
    definition("list_files", "List local files using an absolute, home-relative or workspace-relative path. The app requests access for external folders. Optional glob filters names (e.g. *.csv). Returns entries and next_offset in pages of at most 8 KB; follow next_offset with the same path, depth and glob. If listing_limit_reached is true, narrow the folder or glob to find entries beyond the first 300. Excludes secrets and generated folders. Use find_files for recursive or ** searches.",
               {"path": STRING, "depth": {"type": "integer"}, "glob": STRING,
                "offset": {"type": "integer", "minimum": 0, "maximum": 300}}, []),
    definition("find_files", "Find files by glob pattern under a path (default: the workspace), at any depth. ** matches any number of folders (src/**/*.test.ts); a pattern without / matches file names anywhere (*.py), and ./*.py only directly in path; a pattern with / matches the path below path or the path as results show it; {a,b} gives alternatives. Case-insensitive. Inside a Git repository that starts within the workspace or granted folder, files ignored by .gitignore are skipped unless include_ignored=true; a note says when .gitignore was not applied. sort=modified lists newest first. Returns files, total and next_offset in pages of at most 8 KB; follow next_offset with the same arguments. Excludes secrets and generated folders. External paths need folder access.",
               {"pattern": STRING, "path": {"type": "string", "default": "."},
                "sort": {"type": "string", "enum": ["path", "modified"], "default": "path"},
                "include_ignored": {"type": "boolean", "default": False},
                "offset": {"type": "integer", "minimum": 0, "maximum": 200000},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200}}, ["pattern"]),
    definition("read_file", "Read numbered lines from a regular text file: from start_line (default 1), or the last tail_lines lines, which suits logs. Pages are up to 8–128 KB depending on the context budget; max_lines defaults to 400, or more (up to 2,000) on large budgets; follow next_line. Bytes that are not valid UTF-8 are shown as � and listed in invalid_utf8_lines. Forward reads scan at most 8 MB from the start; tail_lines works on files up to 512 MB. External paths trigger folder access approval.",
               {"path": STRING, "start_line": {"type": "integer", "minimum": 1},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 2000},
                "tail_lines": {"type": "integer", "minimum": 1, "maximum": 2000}}, ["path"]),
    definition("outline_file", "List the structure of one text file with line numbers so you can read only the parts you need: Markdown headings (with the line where each section ends), Python classes and functions (with end lines), and declarations found by a line scan in JavaScript/TypeScript, Go, Rust, Java/Kotlin/C#/Swift, C/C++, Ruby, PHP, shell, SQL, TOML/INI, YAML and Makefiles (heuristic; may miss some). Entries read \"start-end text\" (or \"start text\"); then call read_file with start_line=start and max_lines=end-start+1. max_level limits heading or nesting depth. Follow next_offset. Files up to 8 MB. Outlining does not count as reading the file before write_file.",
               {"path": STRING, "max_level": {"type": "integer", "minimum": 1, "maximum": 6, "default": 6},
                "offset": {"type": "integer", "minimum": 0, "maximum": 20000, "default": 0}}, ["path"]),
    definition("search_files", "Search one UTF-8 file or files under a directory path (default: the workspace) for literal text or a bounded regular expression. Choose the narrowest path and glob; use regex=false unless regular-expression syntax is needed. Case-insensitive by default. output_mode=files_with_matches lists matching files; count gives matches per file. Inside a Git repository that starts within the workspace or granted folder, .gitignored files are skipped unless include_ignored=true; a note says when .gitignore was not applied. Binary and non-UTF-8 files are not searched; a folder search skips files over 2 MB (skipped_files counts them and unreadable files); give one file as path to search it up to 8 MB. Literal searches use ripgrep, when installed, to find candidate files faster; results are the same either way. Returns next_offset for pagination; output is at most 8 KB. External paths trigger folder access approval.",
               {"query": STRING, "glob": STRING, "path": STRING, "regex": {"type": "boolean"},
                "case_sensitive": {"type": "boolean"}, "context_lines": {"type": "integer", "minimum": 0, "maximum": 5},
                "offset": {"type": "integer", "minimum": 0, "maximum": 10000},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
                "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"], "default": "content"},
                "include_ignored": {"type": "boolean", "default": False}}, ["query"]),
    definition("write_file", "Create or replace a file subject to the selected permission mode; read existing files first.",
               {"path": STRING, "content": STRING}, ["path", "content"]),
    definition("edit_file", "Edit an existing file by exact text replacement. Give old_text and new_text (old_text must match exactly once unless replace_all=true), or edits: a list of such replacements applied in order to the same file, all checked before anything is written, with one approval and one checkpoint. Copy old_text exactly from read_file without the line-number prefixes. Line endings (LF or CRLF) are kept.",
               {"path": STRING, "old_text": STRING, "new_text": STRING, "replace_all": {"type": "boolean"},
                "edits": {"type": "array", "minItems": 1, "maxItems": 50,
                          "items": {"type": "object", "properties": {"old_text": STRING, "new_text": STRING,
                                                                     "replace_all": {"type": "boolean"}},
                                    "required": ["old_text", "new_text"], "additionalProperties": False}}}, ["path"]),
    definition("run_command", "Run a noninteractive workspace command under the selected permission mode. Streams output; returns a job ID and bounded first output page. Default foreground Stop cancels the job. Set background=true to continue beyond this turn; use get_job_output/list_jobs/stop_job. Do not daemonize or use shell '&'.",
               {"command": STRING, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600, "default": 60},
                "max_output_bytes": {"type": "integer", "minimum": 1024, "maximum": 1000000, "default": 80000},
                "background": {"type": "boolean", "default": False}}, ["command"]),
    definition("list_jobs", "List a page of command jobs belonging to this conversation, including state and exit code. Follow next_offset for older jobs.",
               {"offset": {"type": "integer", "minimum": 0, "maximum": 104, "default": 0}}, []),
    definition("get_job_output", "Read a bounded output page for a job in this conversation. Follow next_offset; poll running jobs only when useful. Offsets count characters in retained output; truncated means later process output was discarded.",
               {"job_id": STRING, "offset": {"type": "integer", "minimum": 0, "maximum": 1000000, "default": 0},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 8000, "default": 4000}}, ["job_id"]),
    definition("wait_job", "Wait until a job of this conversation finishes (it returns at once when the job ends), until a new output line matches until_regex (Python regular expression, case-sensitive), until the job prints nothing for idle_seconds, or for at most timeout_seconds (max 300; default: about what earlier runs of the same command say is left, else 60). Choose timeout_seconds from how long the job should take rather than the maximum. Only output after from_offset counts (default: total_chars of the last result for this job you received, so a line printed since then still counts; 0 if none). Returns reason (exited, matched, idle or timeout), the matching line, the new output (its end if long), next_offset, elapsed_seconds, timeout_seconds, waited_seconds, expected_seconds (from earlier runs, when known) and, after a timeout or idle return while the job still runs, next_wait_seconds for the next wait. Stop ends the wait; the job keeps running.",
               {"job_id": STRING, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
                "until_regex": {"type": "string", "maxLength": 500},
                "idle_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
                "from_offset": {"type": "integer", "minimum": 0, "maximum": 1000000}}, ["job_id"]),
    definition("stop_job", "Stop a managed job belonging to this conversation and retrieve its final status. Does not undo completed side effects.",
               {"job_id": STRING}, ["job_id"]),
    definition("move_path", "Move or rename one regular UTF-8 text file (within the file write limit) to destination, creating folders as needed. An existing destination is refused unless overwrite=true; read it first. The move appears in the change review with Undo. Use run_command for folders, symbolic links, binary or larger files.",
               {"path": STRING, "destination": STRING, "overwrite": {"type": "boolean", "default": False}},
               ["path", "destination"]),
    definition("delete_path", "Delete one regular UTF-8 text file within the file write limit. Read it first. The deletion appears in the change review, where Undo restores it. Use run_command for folders, symbolic links, binary or larger files.",
               {"path": STRING}, ["path"]),
    definition("notebook_read", "Read a Jupyter notebook (.ipynb) as cells: index, id, type, source and a short summary of outputs. Pages of at most 8 KB; follow next_offset. A long cell's source is clipped (source_truncated, source_chars); read the rest with offset=<its index>, source_offset=<its next_source_offset>, which returns that one cell from there. Read a cell in full before replacing its source.",
               {"path": STRING, "offset": {"type": "integer", "minimum": 0},
                "max_cells": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "source_offset": {"type": "integer", "minimum": 0}}, ["path"]),
    definition("project_overview", "Summarize a project folder (default: the workspace) without reading files: file counts and sizes by language and by top-level folder, key files (README, build and configuration files, CI workflows) and the largest files. Inside a Git repository that starts within the workspace or granted folder, .gitignored files are skipped unless include_ignored=true. Very large folders give a partial, top-level overview. Use it first in an unfamiliar or large project, then find_files/search_files/outline_file. Excludes secrets and generated folders. External paths need folder access.",
               {"path": {"type": "string", "default": "."}, "include_ignored": {"type": "boolean", "default": False}}, []),
    definition("notebook_edit", "Edit one notebook cell, found by cell_id or index (0-based): replace its source (and optionally its type), insert a new cell at index (or after cell_id), delete it, or clear_outputs (of one cell, or of every cell when neither cell_id nor index is given). Read the notebook first. Edited code cells lose their outputs. Other cells and metadata are kept. One approval and a checkpoint, like edit_file. Notebooks up to 1 MB.",
               {"path": STRING, "edit_mode": {"type": "string", "enum": ["replace", "insert", "delete", "clear_outputs"],
                                              "default": "replace"},
                "cell_id": STRING, "index": {"type": "integer", "minimum": 0}, "source": STRING,
                "cell_type": {"type": "string", "enum": ["code", "markdown", "raw"]}}, ["path"]),
]
