"""Git run without a shell and without repository-configured programs.

A repository's own configuration can name programs that ordinary commands run:
`core.fsmonitor` (run by `git status`), hooks, and clean/smudge filters (run by
`git status` and `git diff` on changed files). Every app-initiated Git command goes
through run_git, which disables these, limits the environment, and bounds time and
output. Filter overrides travel in GIT_CONFIG_COUNT/KEY/VALUE environment variables:
unlike `-c name=value` they are not split at an `=` inside a driver name, and child
Git processes inherit them.
"""
import asyncio
import codecs
import os
import posixpath
import re
import shutil
import stat

from .jobs import ENVIRONMENT_KEYS, discard_output, kill_group, settle
from .tools import IGNORED, SECRET_NAMES

OUTPUT_LIMIT = 80_000
SAFE_OPTIONS = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                "-c", "submodule.recurse=false", "-c", "core.pager=cat")
FILTER_KEYS = r"^filter\..*\.(clean|smudge|process|required)$"
# The file tools' credential names and `*.env`, at any depth and in any letter case.
DIFF_EXCLUDES = (":(exclude,glob,icase)**/*.env", *(f":(exclude,glob,icase){pattern}" for name in sorted(SECRET_NAMES)
                              for pattern in (f"**/{name}", f"**/{name}/**")),
                 ":(exclude,glob,icase)**/.env.*")
CHANGES_TIMEOUT = 60


def filter_settings(names):
    """(name, value) pairs that switch off the named filter driver settings."""
    return [(key, "false" if key.endswith(".required") else "") for key in names]


def git_environment(filters=()):
    env = {key: value for key, value in os.environ.items() if key in ENVIRONMENT_KEYS}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1")
    settings = filter_settings(filters)
    if settings:
        env["GIT_CONFIG_COUNT"] = str(len(settings))
        for index, (key, value) in enumerate(settings):
            env[f"GIT_CONFIG_KEY_{index}"], env[f"GIT_CONFIG_VALUE_{index}"] = key, value
    return env


def filter_overrides(names):
    """`-c` options for the same settings, for Git older than 2.31. A name that
    contains `=` cannot be given this way; the environment covers it."""
    options = []
    for key, value in filter_settings(names):
        if "=" not in key:
            options.extend(["-c", f"{key}={value}"])
    return options


async def run_git(workspace, arguments, *, filters=(), timeout=30, limit=None, git=None):
    """Run `git <arguments>` in workspace. Returns (exit code, output bytes, truncated).

    Output beyond `limit` bytes stops the command; truncated is then true.
    """
    limit = OUTPUT_LIMIT if limit is None else max(limit, 0)
    git = git or shutil.which("git")
    if not git:
        raise ValueError("Git is not installed or not on PATH.")
    spawning = asyncio.create_task(asyncio.create_subprocess_exec(
        git, *SAFE_OPTIONS, *filter_overrides(filters), "-C", str(workspace), *arguments, env=git_environment(filters),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True))
    process = None
    truncated = False
    try:
        process = await asyncio.shield(spawning)
        async with asyncio.timeout(timeout):
            raw = await process.stdout.read(limit + 1)
            # read(n) can return early; continue to EOF while keeping a hard bound.
            while len(raw) <= limit:
                chunk = await process.stdout.read(limit + 1 - len(raw))
                if not chunk:
                    break
                raw += chunk
            if len(raw) > limit:
                raw, truncated = raw[:limit], True
                kill_group(process.pid)
                # Read the rest: once over 128 KiB is buffered asyncio stops reading,
                # never sees the end of the output, and wait() alone would not return.
                await discard_output(process)
            else:
                await process.wait()
    except BaseException:
        if process is None:
            process = await spawning
        kill_group(process.pid)
        _, cancellation = await settle(asyncio.ensure_future(discard_output(process)))
        if cancellation is not None:
            raise cancellation
        raise
    return process.returncode, raw, truncated


async def configured_filters(workspace, *, timeout=30, git=None):
    _, names, _ = await run_git(workspace, ["config", "--name-only", "-z", "--get-regexp", FILTER_KEYS],
                                timeout=timeout, git=git)
    return [name for name in names.decode("utf-8", errors="surrogateescape").split("\0") if name]


async def working_tree_changes(workspace):
    """`git status --short` followed by `git diff`, as {exit_code, output, truncated}.

    --ignore-submodules=dirty keeps Git from running inside submodules, whose own
    configuration may name filters; changed submodule commits are still listed."""
    try:
        async with asyncio.timeout(CHANGES_TIMEOUT):
            filters = await configured_filters(workspace, timeout=CHANGES_TIMEOUT)
            code, output, truncated = await run_git(
                workspace, ["--no-optional-locks", "status", "--short", "--untracked-files=normal",
                            "--ignore-submodules=dirty"],
                filters=filters, timeout=CHANGES_TIMEOUT)
            if code == 0 and not truncated:
                code, diff, truncated = await run_git(
                    workspace, ["--no-optional-locks", "diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=dirty",
                              "--", ".", *DIFF_EXCLUDES],
                    filters=filters, timeout=CHANGES_TIMEOUT, limit=OUTPUT_LIMIT - len(output))
                output += diff
    except TimeoutError as exc:
        raise ValueError(f"Git did not finish within {CHANGES_TIMEOUT} seconds.") from exc
    # A byte limit may cut a UTF-8 character in half; drop only that partial character.
    text = codecs.getincrementaldecoder("utf-8")(errors="replace").decode(output, final=not truncated)
    if truncated:
        text += f"\n[Output limit reached: showing the first {OUTPUT_LIMIT // 1000} KB.]"
    return {"exit_code": None if truncated else code, "output": text, "truncated": truncated}


# The structured Changes view: Git status as file entries, and one file's diff as lines.
STATUS_OUTPUT_LIMIT = 1_000_000
FILE_DIFF_LIMIT = 512_000
UNTRACKED_FILE_LIMIT = 1_000_000
STATUS_ENTRY_LIMIT = 2_000
STATUS_CODES = {"M": "modified", "T": "type_changed", "A": "added", "D": "deleted", "R": "renamed", "C": "copied"}
DIFF_OPTIONS = ("--no-ext-diff", "--no-textconv", "--ignore-submodules=dirty", "--relative")
# Generated folders (the file tools' IGNORED names) are left out of the per-file listing, so
# a large untracked node_modules neither fills the list nor pushes real files past its limits.
# A second status lists their changes, with untracked folders collapsed to one entry each.
GENERATED_FOLDERS = sorted(IGNORED - {".git"})
GENERATED_EXCLUDES = tuple(f":(exclude,glob,icase)**/{name}/**" for name in GENERATED_FOLDERS)
GENERATED_ONLY = tuple(f":(glob,icase)**/{name}/**" for name in GENERATED_FOLDERS)


def _decode(raw):
    return raw.decode("utf-8", errors="replace")


def _inside_repository(workspace):
    """Whether Git could find a repository from workspace: a .git entry in it or a parent.
    Decides "not a repository" without reading Git's message, which follows the locale."""
    folder = os.path.realpath(workspace)
    while True:
        if os.path.lexists(os.path.join(folder, ".git")):
            return True
        parent = os.path.dirname(folder)
        if parent == folder:
            return False
        folder = parent


def _git_failure(output):
    text = _decode(output).strip()
    if "not a git repository" in text.lower():
        return None
    return ValueError("Git could not read this folder: " + (text.splitlines() or ["no output"])[-1][:300])


def parse_status(raw, prefix=""):
    """Parse `git status --porcelain=v2 -z --branch` output.

    Returns (branch, entries). Paths are made relative to `prefix` (the workspace's
    folder inside the repository); entries outside it are left out."""
    records = raw.split(b"\0")
    branch, entries = None, []

    def relative(path):
        path = _decode(path)
        if path.startswith(prefix):
            return path[len(prefix):]
        return posixpath.relpath(path, prefix.rstrip("/") or ".")

    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        kind = record[:2]
        if record.startswith(b"# branch.head "):
            head = _decode(record[len(b"# branch.head "):])
            branch = None if head == "(detached)" else head
        elif kind in (b"1 ", b"2 "):
            fields = record.split(b" ", 8 if kind == b"1 " else 9)
            if len(fields) < (9 if kind == b"1 " else 10):
                continue
            original = None
            if kind == b"2 ":
                if index >= len(records) or not records[index]:
                    break  # Cut off by the output limit: drop the rename without its origin.
                original = relative(records[index])
                index += 1
            path = relative(fields[-1])
            codes = _decode(fields[1])
            for area, code in zip(("staged", "unstaged"), codes):
                if code == ".":
                    continue
                entry = {"path": path, "area": area, "status": STATUS_CODES.get(code, "modified")}
                if original is not None:
                    entry["orig_path"] = original
                entries.append(entry)
        elif kind == b"u ":
            fields = record.split(b" ", 10)
            if len(fields) == 11:
                entries.append({"path": relative(fields[-1]), "area": "unstaged", "status": "conflicted"})
        elif kind == b"? ":
            entries.append({"path": relative(record[2:]), "area": "untracked", "status": "untracked"})
    return branch, entries


def parse_numstat(raw):
    """{path: (added, removed)} from `git diff --numstat -z`; binary files give None counts."""
    counts = {}
    records = raw.split(b"\0")
    index = 0
    while index < len(records):
        fields = records[index].split(b"\t", 2)
        index += 1
        if len(fields) != 3 or not all(value == b"-" or value.isdigit() for value in fields[:2]):
            continue
        added, removed, path = fields
        if not path:  # A rename: "added\tremoved\t\0old\0new\0".
            if index + 1 >= len(records):
                break
            path = records[index + 1]
            index += 2
        counts[_decode(path)] = (None if added == b"-" else int(added), None if removed == b"-" else int(removed))
    return counts


async def working_tree_status(workspace, classify):
    """Git's view of the workspace as file entries, for the Changes view.

    classify(relative path) returns 'credential', 'generated' or None. Credential
    entries carry no counts, and their diffs are refused by the API."""
    try:
        async with asyncio.timeout(CHANGES_TIMEOUT):
            code, output, _ = await run_git(workspace, ["rev-parse", "--show-prefix"], timeout=CHANGES_TIMEOUT)
            if code != 0:
                error = _git_failure(output)
                if error is None or not await asyncio.to_thread(_inside_repository, workspace):
                    return {"repository": False, "branch": None, "files": [], "truncated": False}
                raise error
            prefix = _decode(output).strip()
            filters = await configured_filters(workspace, timeout=CHANGES_TIMEOUT)
            listings = []
            for untracked, pathspecs in (("all", (".", *GENERATED_EXCLUDES)), ("normal", GENERATED_ONLY)):
                code, output, cut = await run_git(
                    workspace, ["--no-optional-locks", "status", "--porcelain=v2", "-z", "--branch",
                                f"--untracked-files={untracked}", "--ignore-submodules=dirty", "--", *pathspecs],
                    filters=filters, timeout=CHANGES_TIMEOUT, limit=STATUS_OUTPUT_LIMIT)
                if code != 0 and not cut:
                    raise _git_failure(output) or ValueError("Git could not read this folder.")
                if cut:
                    output = output[:output.rfind(b"\0") + 1]  # Drop the record that was cut off.
                listings.append((*parse_status(output, prefix), cut))
            (branch, entries, truncated), (_, generated, generated_cut) = listings
            counts = {}
            for area, extra in (("unstaged", []), ("staged", ["--cached"])):
                _, numstat, _ = await run_git(
                    workspace, ["--no-optional-locks", "diff", *extra, "--numstat", "-z", *DIFF_OPTIONS,
                                "--", ".", *DIFF_EXCLUDES],
                    filters=filters, timeout=CHANGES_TIMEOUT, limit=STATUS_OUTPUT_LIMIT)
                counts[area] = parse_numstat(numstat)
    except TimeoutError as exc:
        raise ValueError(f"Git did not finish within {CHANGES_TIMEOUT} seconds.") from exc
    # Each listing has its own limit, so generated folders never push real files out.
    if len(entries) > STATUS_ENTRY_LIMIT:
        entries, truncated = entries[:STATUS_ENTRY_LIMIT], True
    if len(generated) > STATUS_ENTRY_LIMIT:
        generated, generated_cut = generated[:STATUS_ENTRY_LIMIT], True
    entries += generated
    truncated = truncated or generated_cut
    files = []
    for entry in entries:
        kinds = [classify(path) for path in (entry["path"], entry.get("orig_path")) if path is not None]
        hidden = "credential" if "credential" in kinds else "generated" if "generated" in kinds else None
        added, removed = counts.get(entry["area"], {}).get(entry["path"], (None, None))
        if hidden == "credential":
            added = removed = None
        files.append({**entry, "added": added, "removed": removed, "hidden": hidden})
    return {"repository": True, "branch": branch, "files": files, "truncated": bool(truncated)}


def parse_diff(text):
    """Lines of a unified diff: hunk headers, then context, removed and added lines."""
    lines, binary = [], False
    old = new = old_left = new_left = 0
    for line in text.split("\n"):
        if old_left > 0 or new_left > 0:
            marker = line[:1]
            if marker in (" ", "") and old_left > 0 and new_left > 0:
                lines.append({"kind": "context", "text": line[1:], "old": old, "new": new})
                old, new, old_left, new_left = old + 1, new + 1, old_left - 1, new_left - 1
                continue
            if marker == "-" and old_left > 0:
                lines.append({"kind": "removed", "text": line[1:], "old": old})
                old, old_left = old + 1, old_left - 1
                continue
            if marker == "+" and new_left > 0:
                lines.append({"kind": "added", "text": line[1:], "new": new})
                new, new_left = new + 1, new_left - 1
                continue
            if marker == "\\":
                if lines and lines[-1]["kind"] != "hunk":
                    lines[-1]["newline"] = False
                continue
            old_left = new_left = 0  # Not a hunk line: the hunk ended early.
        if line.startswith("\\") and lines and lines[-1]["kind"] != "hunk":
            lines[-1]["newline"] = False
        elif line.startswith("Binary files "):
            binary = True
        else:
            header = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if header:
                old, old_left = int(header[1]), int(header[2] if header[2] is not None else 1)
                new, new_left = int(header[3]), int(header[4] if header[4] is not None else 1)
                lines.append({"kind": "hunk", "text": line})
    return binary, lines


def _untracked_diff(tools, path):
    target = tools.path(path)
    info = target.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Only regular files can be shown.")
    if info.st_size > UNTRACKED_FILE_LIMIT:
        raise ValueError(f"This file is larger than {UNTRACKED_FILE_LIMIT // 1_000_000} MB.")
    with target.open("rb") as source:
        raw = source.read(UNTRACKED_FILE_LIMIT + 1)
    if b"\0" in raw:
        return True, []
    text = _decode(raw)
    rows = text.split("\n")
    ends_with_newline = rows[-1] == ""
    if ends_with_newline:
        rows.pop()
    lines = [{"kind": "added", "text": row.removesuffix("\r"), "new": number} for number, row in enumerate(rows, 1)]
    if lines and not ends_with_newline:
        lines[-1]["newline"] = False
    return False, lines


async def file_diff(workspace, path, area, orig_path=None, tools=None, excludes=()):
    """One file's changes in the staged or unstaged area, or an untracked file's lines.

    Paths are workspace-relative; the caller validates them and refuses credential
    and generated-folder files. excludes are extra pathspecs to leave out (the
    configured credential file). A path that names a folder is refused: only the
    requested file (and a rename's origin) may appear in the diff."""
    truncated = False
    if area == "untracked":
        binary, lines = await asyncio.to_thread(_untracked_diff, tools, path)
    elif area in ("staged", "unstaged"):
        pathspecs = [f":(literal){path}", *([f":(literal){orig_path}"] if orig_path else []), *DIFF_EXCLUDES, *excludes]
        cached = ["--cached"] if area == "staged" else []
        try:
            async with asyncio.timeout(CHANGES_TIMEOUT):
                filters = await configured_filters(workspace, timeout=CHANGES_TIMEOUT)
                code, names, cut = await run_git(
                    workspace, ["--no-optional-locks", "diff", *cached, "--name-only", "-z", *DIFF_OPTIONS, "--", *pathspecs],
                    filters=filters, timeout=CHANGES_TIMEOUT, limit=64_000)
                if code == 0 and (cut or {_decode(name) for name in names.split(b"\0") if name} - {path, orig_path}):
                    raise ValueError("Choose one changed file, not a folder.")
                code, output, truncated = await run_git(
                    workspace, ["--no-optional-locks", "diff", *cached, *DIFF_OPTIONS, "--no-color", "-U3", "--", *pathspecs],
                    filters=filters, timeout=CHANGES_TIMEOUT, limit=FILE_DIFF_LIMIT)
        except TimeoutError as exc:
            raise ValueError(f"Git did not finish within {CHANGES_TIMEOUT} seconds.") from exc
        if code != 0 and not truncated:
            raise _git_failure(output) or ValueError("This folder is not a Git repository.")
        if truncated:
            output = output[:output.rfind(b"\n") + 1]  # Drop the line that was cut off.
        binary, lines = parse_diff(_decode(output))
    else:
        raise ValueError("Choose the staged, unstaged or untracked area.")
    return {"path": path, "area": area, "binary": binary, "truncated": truncated,
            "added": sum(line["kind"] == "added" for line in lines),
            "removed": sum(line["kind"] == "removed" for line in lines), "lines": lines}
