"""The files that find_files and search_files consider, and ripgrep as a candidate filter.

The file universe is computed the same way for both tools. Inside a Git work tree
whose top level lies within the permitted folder, and is neither the home folder nor
'/', `git ls-files` lists tracked and untracked files and leaves out what .gitignore
excludes. Otherwise the folder is walked. In both cases the file tools' credential and
generated-folder exclusions apply, files below a symbolic link to a folder are left
out, and the configured credential file is left out by identity.

ripgrep, when installed, only narrows which files Python reads for a literal
search. Python reads every candidate and produces every result, so results are the
same with and without it. Every git and rg process runs through bounded_run: no
shell, a limited environment, a time limit and an output limit.
"""
import os
import shutil
import signal
import stat
import subprocess
import threading
import time
import unicodedata
from pathlib import Path, PurePosixPath
from typing import NamedTuple

import regex as safe_regex

from . import tools as tool_rules
from .git_tools import SAFE_OPTIONS, git_environment

UNIVERSE_LIMIT = 200_000
WALK_DIR_LIMIT = 20_000
ENUMERATION_TIMEOUT = 10.0
GIT_LIST_TIMEOUT = 5.0
RG_TIMEOUT = 5.0
PROCESS_OUTPUT_LIMIT = 32_000_000
READ_CHUNK = 65_536
RG_ENVIRONMENT = ("PATH", "HOME", "LANG", "LC_ALL")
GIT_NOTE = "Git could not list the files here, so .gitignore was not applied."
OUTSIDE_NOTE = ("This folder is inside a Git repository whose top level is outside the workspace or granted folder, "
                "so .gitignore was not applied.")
MAX_ALTERNATIVES = 20


class Entry(NamedTuple):
    relative: str  # POSIX path relative to the listed folder
    path: Path  # absolute path, not resolved: a symbolic link stays a link
    size: int  # of the target, for a symbolic link
    symlink: bool


def _kill(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def bounded_run(argv, cwd, env, timeout, limit):
    """Run argv (a list, never a shell string). Returns (exit code or None after a
    timeout, stdout bytes, overflow). The process group is killed on timeout, and
    once stdout exceeds limit bytes; the rest of the output is read and dropped."""
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    fired = threading.Event()

    def expire():
        fired.set()
        _kill(process.pid)

    timer = threading.Timer(max(timeout, 0), expire)
    timer.daemon = True
    timer.start()
    chunks, size, overflow = [], 0, False
    try:
        while chunk := process.stdout.read(READ_CHUNK):
            if overflow:
                continue
            if size + len(chunk) > limit:
                overflow = True
                _kill(process.pid)
                continue
            chunks.append(chunk)
            size += len(chunk)
        process.wait()
    finally:
        timer.cancel()
        if process.poll() is None:
            _kill(process.pid)
            process.wait()
        process.stdout.close()
    return (None if fired.is_set() else process.returncode), b"".join(chunks), overflow


def check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise ValueError(f"Listing files took longer than {ENUMERATION_TIMEOUT:g} seconds; narrow the path.")


def _identity(path):
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return None
    return info.st_dev, info.st_ino


def _home():
    try:
        return Path.home().resolve()
    except (RuntimeError, KeyError, OSError):
        return None


def _permitted_root(tools, base):
    if base.is_relative_to(tools.root):
        return tools.root
    return next((folder for folder in tools.allowed_directories if base.is_relative_to(folder)), None)


def _git_names(tools, base, deadline):
    """(names relative to base, None), (None, GIT_NOTE) when Git failed in a work
    tree, (None, OUTSIDE_NOTE) when the repository starts above the permitted folder,
    or (None, None) when Git does not apply here."""
    git = shutil.which("git")
    if not git:
        return None, None
    check_deadline(deadline)
    env = git_environment()
    code, output, overflow = bounded_run([git, *SAFE_OPTIONS, "-C", str(base), "rev-parse", "--show-toplevel"],
                                         base, env, min(GIT_LIST_TIMEOUT, deadline - time.monotonic()), 65_536)
    if code != 0 or overflow:
        return None, None
    text = output.decode("utf-8", errors="surrogateescape")
    toplevel = Path(text[:-1] if text.endswith("\n") else text).resolve()
    # A dotfiles repository in the home folder, often with a '*' ignore rule, must
    # not hide a project's files, also when the workspace is the home folder itself.
    if toplevel in (_home(), Path("/")):
        return None, None
    # Nor may a repository that starts above the permitted folder decide what is
    # visible inside it; the result says that .gitignore was not applied.
    root = _permitted_root(tools, base)
    if root is not None and not toplevel.is_relative_to(root):
        return None, OUTSIDE_NOTE
    check_deadline(deadline)
    code, output, overflow = bounded_run(
        [git, *SAFE_OPTIONS, "-C", str(base), "--no-optional-locks", "ls-files", "-z", "--cached", "--others",
         "--exclude-standard"], base, env, min(GIT_LIST_TIMEOUT, deadline - time.monotonic()), PROCESS_OUTPUT_LIMIT)
    if code != 0 or overflow:
        return None, GIT_NOTE
    names, seen = [], set()
    for raw in output.split(b"\0"):
        if not raw or raw in seen:  # Unmerged entries are listed once per stage.
            continue
        seen.add(raw)
        name = raw.decode("utf-8", errors="surrogateescape")
        # Git never writes absolute names or "." and ".." parts, but a hand-made index can hold
        # them, and they would name files outside base that no permission check sees.
        if name.startswith("/") or any(part in ("", ".", "..") for part in name.rstrip("/").split("/")):
            continue
        names.append(name)
    return names, None


def _walk(tools, folder, deadline, counter):
    """Relative POSIX paths of the files below folder, skipping excluded and
    symlinked folders. counter limits the folders visited in one listing."""
    def unreadable(error):
        raise error

    for current, dirnames, filenames in os.walk(folder, followlinks=False, onerror=unreadable):
        check_deadline(deadline)
        counter[0] += 1
        if counter[0] > WALK_DIR_LIMIT:
            raise ValueError(f"Directory scan limit reached ({WALK_DIR_LIMIT:,} folders); narrow the path.")
        kept = []
        for name in sorted(dirnames):
            child = Path(current) / name
            try:
                if child.is_symlink():
                    continue
                tools.path(str(child))
            except (OSError, ValueError):
                continue
            kept.append(name)
        dirnames[:] = kept
        prefix = Path(current).relative_to(folder)
        for name in sorted(filenames):
            yield (prefix / name).as_posix()


def list_universe(tools, base, *, include_ignored, deadline):
    """(entries sorted by relative path, note or None) for a file or folder that
    tools.path() has already checked."""
    base = Path(base)
    if base.is_file():
        # Named explicitly: listed even when ignored. tools.path applied the credential rules.
        return [Entry(base.name, base, base.stat().st_size, False)], None
    credential = _identity(tools.credential_file) if tools.credential_file else None
    counter = [0]
    real_base = os.path.realpath(base)
    plain = {}  # Parent folder -> True when no folder from base down to it is a symbolic link.

    def plain_folders(relative):
        """Git can list a file below a folder that is now a symbolic link (to a folder
        outside the workspace, or to .git). The walk never enters such links; neither
        does this listing."""
        parent = PurePosixPath(relative).parent.as_posix()
        if parent == ".":
            return True
        if parent not in plain:
            # parent is relative and normalised (see _git_names), so os.path.join keeps real_base.
            plain[parent] = (not PurePosixPath(parent).is_absolute()
                             and os.path.realpath(base / parent) == os.path.join(real_base, parent))
        return plain[parent]
    names, note = (None, None) if include_ignored else _git_names(tools, base, deadline)

    def expanded():
        if names is None:
            yield from _walk(tools, base, deadline, counter)
            return
        for name in names:
            folder = base / name
            # Untracked nested repositories end in "/"; submodules are directories.
            # Both are walked with the fixed exclusions only.
            if name.endswith("/") or (folder.is_dir() and not folder.is_symlink()):
                name = name.rstrip("/")
                try:
                    if folder.is_symlink() or not folder.is_dir():
                        continue
                    tools.path(str(folder))
                except (OSError, ValueError):
                    continue
                yield from (f"{name}/{inner}" for inner in _walk(tools, base / name, deadline, counter))
            else:
                yield name

    entries, seen = [], set()
    for relative in expanded():
        check_deadline(deadline)
        if relative in seen:
            continue
        seen.add(relative)
        try:
            relative.encode("utf-8")
        except UnicodeEncodeError:
            continue  # Not representable as text in a tool result.
        if tool_rules.excluded(PurePosixPath(relative).parts):
            continue
        try:
            if not plain_folders(relative):
                continue
        except (OSError, ValueError):
            continue
        path = base / relative
        try:
            info = os.lstat(path)
        except (OSError, ValueError):
            continue  # Listed by Git but removed since, or unreadable.
        if credential is not None and (info.st_dev, info.st_ino) == credential:
            continue  # The configured credential file, or a hard link to it, under any name.
        if stat.S_ISREG(info.st_mode):
            entry = Entry(relative, path, info.st_size, False)
        elif stat.S_ISLNK(info.st_mode):
            try:
                target = tools.path(str(path))
                target_info = target.stat()
            except (OSError, ValueError):
                continue
            if not stat.S_ISREG(target_info.st_mode):
                continue
            if credential is not None and (target_info.st_dev, target_info.st_ino) == credential:
                continue
            entry = Entry(relative, path, target_info.st_size, True)
        else:
            continue
        entries.append(entry)
        if len(entries) > UNIVERSE_LIMIT:
            raise ValueError(f"More than {UNIVERSE_LIMIT:,} files under this path; narrow the path.")
    entries.sort(key=lambda entry: entry.relative)
    return entries, note


def _class_text(text):
    out = []
    for character in text:
        out.append("\\" + character if character in "\\^[]&~|" else character)
    return "".join(out)


def _brace_end(pattern, start):
    """The index of the "}" that closes the "{" at start, or None if it is not closed."""
    for index in range(start + 1, len(pattern)):
        if pattern[index] == "{":
            raise ValueError("Nested {…} groups are not supported in pattern.")
        if pattern[index] == "}":
            return index
    return None


def _translate(pattern, segment_start=True):
    out, index = [], 0
    while index < len(pattern):
        at_start = segment_start if index == 0 else pattern[index - 1] == "/"
        if at_start and pattern.startswith("**/", index):
            out.append("(?:.*/)?")  # Zero or more folders.
            index += 3
        elif pattern[index:] == "/**":
            out.append("/.*")  # Everything below.
            index += 3
        elif pattern.startswith("**", index):
            out.append("[^/]*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        elif pattern[index] == "[":
            end = index + 1
            if end < len(pattern) and pattern[end] in "!^":
                end += 1
            if end < len(pattern) and pattern[end] == "]":
                end += 1
            end = pattern.find("]", end)
            if end < 0:
                out.append(safe_regex.escape("["))
                index += 1
                continue
            body = pattern[index + 1:end]
            if body[:1] in ("!", "^"):
                out.append("[^/" + _class_text(body[1:]) + "]")
            else:
                out.append("[" + _class_text(body) + "]")
            index = end + 1
        elif pattern[index] == "{":
            end = _brace_end(pattern, index)
            if end is None:
                out.append(safe_regex.escape("{"))
                index += 1
                continue
            alternatives = pattern[index + 1:end].split(",")
            if len(alternatives) > MAX_ALTERNATIVES:
                raise ValueError(f"A {{…}} group in pattern can have at most {MAX_ALTERNATIVES} alternatives.")
            out.append("(?:" + "|".join(_translate(item, at_start) for item in alternatives) + ")")
            index = end + 1
        else:
            out.append(safe_regex.escape(pattern[index]))
            index += 1
    return "".join(out)


def compile_glob(pattern):
    """A case-insensitive matcher for find_files patterns. '*' and '?' never cross '/';
    '**/' is zero or more folders; a pattern without '/' matches names at any depth.
    A leading './' anchors the pattern at the folder searched ('./*.py': only there)."""
    if not isinstance(pattern, str) or not 1 <= len(pattern) <= 500 or "\x00" in pattern:
        raise ValueError("pattern must be 1 to 500 characters.")
    anchored = pattern.startswith("./")
    while pattern.startswith("./"):
        pattern = pattern[2:]
    if not pattern:
        raise ValueError("pattern must be 1 to 500 characters.")
    if "/" not in pattern and not anchored:
        pattern = "**/" + pattern
    try:
        return safe_regex.compile(_translate(pattern), safe_regex.IGNORECASE | safe_regex.VERSION0 | safe_regex.DOTALL)
    except (safe_regex.error, RecursionError) as error:
        raise ValueError(f"Invalid pattern: {error}") from error


def glob_match(matcher, relative, deadline):
    try:
        return matcher.fullmatch(relative, timeout=max(0.001, deadline - time.monotonic())) is not None
    except TimeoutError:
        check_deadline(deadline)
        raise ValueError("Matching the pattern took too long; simplify it.") from None


def ripgrep():
    """The rg executable, or None. Tests replace this."""
    return shutil.which("rg")


_fold_classes = None


def fold_classes():
    """For each ASCII letter, every character that Python's simple case folding
    (regex IGNORECASE | VERSION0) treats as equal to it, computed once. Upper and
    lower case can differ: 'i' also matches U+0130 and 'I' also matches U+0131."""
    global _fold_classes
    if _fold_classes is None:
        everything = "".join(chr(code) for code in range(0x10000) if not 0xD800 <= code <= 0xDFFF)
        classes = {}
        for letter in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ":
            matcher = safe_regex.compile(safe_regex.escape(letter), safe_regex.IGNORECASE | safe_regex.VERSION0)
            classes[letter] = "".join(sorted(set(matcher.findall(everything))))
        _fold_classes = classes
    return _fold_classes


def rg_pattern(query):
    """An rg regular expression for an ASCII query that matches a superset of what
    Python's simple case folding matches, without relying on rg's own case tables."""
    classes = fold_classes()
    out = []
    for character in query:
        if character in classes:
            out.append("[" + classes[character] + "]")
        elif character in "\\.+*?()|[]{}^$#&-~":
            out.append("\\" + character)
        else:
            out.append(character)
    return "".join(out)


def candidate_key(name):
    """A file name compared loosely: rg prints the spelling on disk, Git its own, and
    on macOS they can differ in letter case or Unicode normal form. A looser match only
    makes Python read more files, never changes a result."""
    return unicodedata.normalize("NFC", name).casefold()


def rg_candidates(rg, base, query, case_sensitive, deadline):
    """candidate_key()s of relative paths of files under base that may contain query,
    or None when rg cannot be trusted for this search (any error, a timeout or too
    much output, or a query that cannot be passed as an argument)."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    argv = [rg, "--no-config", "--files-with-matches", "--null", "--no-messages", "--hidden", "--no-ignore",
            "--encoding", "none", "--max-filesize", str(tool_rules.SEARCH_FILE_LIMIT)]
    if case_sensitive:
        argv.append("--fixed-strings")
    for name in sorted(tool_rules.IGNORED):
        argv += ["--iglob", "!" + name]
    argv += ["-e", query if case_sensitive else rg_pattern(query), "--", "."]
    env = {key: value for key, value in os.environ.items() if key in RG_ENVIRONMENT}
    try:
        code, output, overflow = bounded_run(argv, base, env, min(RG_TIMEOUT, remaining), PROCESS_OUTPUT_LIMIT)
    except (OSError, ValueError):
        return None  # ValueError: for example a lone surrogate, which no argument can hold.
    if overflow or code not in (0, 1):
        return None  # 2 also means that some file could not be read.
    names = set()
    for raw in output.split(b"\0"):
        if raw:
            name = raw.decode("utf-8", errors="surrogateescape")
            names.add(candidate_key(name[2:] if name.startswith("./") else name))
    return names
