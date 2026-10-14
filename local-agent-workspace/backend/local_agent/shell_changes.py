"""Files a foreground run_command changed, found with Git status before and after it.

Before the command, the tracker records `git status` of the project folder, the chat's granted
folders and the folders added to Workspace -> Files (each folder that is inside a Git repository),
plus each repository's HEAD, and snapshots the files that were already changed. After the
command it runs status again; a file listed either time, or changed between the old and the new
HEAD (a commit, checkout or stash), is a candidate. Its baseline is the snapshot taken before the
command when the file was already changed, else the file in the HEAD from before the command
(absent when it was not there), so a later `git add`, commit or branch switch cannot move it.

Every Git call goes through git_tools.run_git (no shell, no hooks, no fsmonitor) with the
repository's clean/smudge filters switched off; the filter names are read again before each
status, so a filter the command itself configures does not run either. A file is tracked only when the chat's tools may
use it (no credential files, generated folders or ungranted paths) and no symbolic link is on its
path, so a review row always names the file that actually changed. Everything is bounded: time,
status size, snapshot count and bytes, candidates and blob reads; past a bound the tracker skips
files and never fails the command.
"""
import asyncio
import copy
import logging
import re
import time
from pathlib import Path

from .git_tools import configured_filters, run_git
from .recovery import _snapshot, _state
from .rules import parse_command, read_only_argv

log = logging.getLogger(__name__)

GIT_TIMEOUT = 10  # seconds per Git call
AFTER_DEADLINE = 15  # seconds for each repository's part of before() and after()
MAX_STATUS_ENTRIES = 2000
MAX_SNAPSHOTS = 200
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_CANDIDATES = 500
MAX_BLOB_READS = 100
MAX_FOLDERS = 24
FILE_LIMIT = 80_000  # the largest file tracked, in bytes (like the default file write limit)
STATUS_LIMIT = 4_000_000  # bytes of status or diff output
CACHE_SECONDS = 60
UNSUPPORTED = "unsupported"  # A file changed before the command that could not be snapshotted.
STATUS_ENTRY = re.compile(rb"^[ MADRCUT?!]{2} ")
OBJECT_ID = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
# Global options for every call: no index refresh writes, and paths taken literally.
GLOBAL = ["--no-optional-locks", "--literal-pathspecs"]

# folder -> (expires, repository top): a burst of commands asks once. Filter names are not
# cached: a command can configure a new filter, which must stay off in the next status.
_locations = {}


def read_only_command(command):
    """Whether command text is on the fixed read-only list (those commands are not checked)."""
    argv, _ = parse_command(command.strip()) if isinstance(command, str) else (None, None)
    return argv is not None and read_only_argv(argv)


async def _location(folder):
    """The repository top for a folder inside a Git work tree, else None. Never raises."""
    try:
        key, now = str(folder), time.monotonic()
        cached = _locations.get(key)
        if cached and cached[0] > now:
            return cached[1]
        if not folder.is_dir():
            return None
        code, raw, truncated = await run_git(folder, ["rev-parse", "--show-toplevel"], timeout=GIT_TIMEOUT, limit=16_384)
        lines = [line for line in raw.decode("utf-8").splitlines() if line.strip()] if code == 0 and not truncated else []
        top = Path(lines[-1]) if lines else None
        if top is None or not top.is_absolute() or not folder.is_relative_to(top):
            return None
        if len(_locations) >= 256:
            _locations.clear()
        _locations[key] = (now + CACHE_SECONDS, top)
        return top
    except Exception as error:
        log.debug("Command change tracking could not locate a repository: %s", type(error).__name__)
        return None


class _Repository:
    def __init__(self, folder, top):
        self.folder, self.top, self.filters = folder, top, []
        relative = folder.relative_to(top).as_posix() if folder != top else "."
        self.pathspec = relative
        self.head = None
        self.changed = {}  # {path relative to top: status code} that status listed before the command.
        self.snapshots = {}  # Path -> state before the command, or UNSUPPORTED.

    async def git(self, arguments, limit=STATUS_LIMIT):
        return await run_git(self.top, [*GLOBAL, *arguments], filters=self.filters, timeout=GIT_TIMEOUT, limit=limit)

    async def read_filters(self):
        """The repository's filter names as configured now (the command may have added one)."""
        self.filters = await configured_filters(self.top, timeout=GIT_TIMEOUT)

    async def status(self):
        """{path: status code} as status lists them, or None when status fails or lists too many."""
        code, raw, truncated = await self.git(["status", "--porcelain=v1", "-z", "--untracked-files=all",
                                               "--ignore-submodules=dirty", "--no-renames", "--", self.pathspec])
        if code != 0 or truncated:
            return None
        entries = [item for item in raw.split(b"\0") if item]
        if len(entries) > MAX_STATUS_ENTRIES:
            return None
        paths = {}
        for item in entries:
            if STATUS_ENTRY.match(item):
                try:
                    paths[item[3:].decode("utf-8")] = item[:2]
                except UnicodeDecodeError:
                    continue  # A path that is not UTF-8 cannot be listed in the review.
        return paths

    async def current_head(self):
        code, raw, _ = await self.git(["rev-parse", "-q", "--verify", "HEAD^{commit}"], limit=4096)
        text = raw.decode("ascii", errors="replace").strip().splitlines()
        return text[-1] if code == 0 and text and OBJECT_ID.match(text[-1]) else None

    async def moved(self, head):
        """Paths that differ between the HEAD before the command and head; every path of head
        when the repository had no commit before (its first commit)."""
        if self.head is None:
            arguments = ["ls-tree", "-r", "-z", "--name-only", head, "--", self.pathspec]
        else:
            arguments = ["diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", self.head, head, "--", self.pathspec]
        code, raw, truncated = await self.git(arguments)
        if code != 0:
            return set()
        items = raw.split(b"\0")
        if truncated:
            items = items[:-1]  # The last name may be cut off.
        paths = set()
        for item in items:
            try:
                if item:
                    paths.add(item.decode("utf-8"))
            except UnicodeDecodeError:
                continue
        return paths

    async def entries(self, paths):
        """{path: (mode, type, object id)} in the HEAD from before the command."""
        found = {}
        paths = sorted(paths)
        for start in range(0, len(paths), 100):
            code, raw, truncated = await self.git(["ls-tree", "-z", "--full-tree", self.head, "--", *paths[start:start + 100]])
            if code != 0 or truncated:
                raise ValueError("Could not list the files of the previous commit.")
            for item in raw.split(b"\0"):
                header, _, name = item.partition(b"\t")
                fields = header.split(b" ")
                if len(fields) == 3 and name:
                    try:
                        found[name.decode("utf-8")] = tuple(field.decode("ascii") for field in fields)
                    except UnicodeDecodeError:
                        continue
        return found


class ShellChangeTracker:
    """One foreground command's view of the files it changed. Use before() and after()."""

    def __init__(self, tools, repositories):
        self.tools = tools
        self.repositories = repositories
        self.snapshot_count = self.snapshot_bytes = self.blob_reads = 0

    @classmethod
    async def before(cls, tools, command=None, *, folders=()):
        """A tracker with the state before the command, or None (not a Git folder, too many
        changed files, a read-only command, or any failure). Never raises."""
        try:
            if command is not None and read_only_command(command):
                return None
            # The tools may also use the added folders, so their files can be listed and reviewed.
            extra = [Path(folder).resolve() for folder in folders]
            tools = copy.copy(tools)
            tools.allowed_directories = [*tools.allowed_directories, *extra]
            # The project first, then the other folders in order, at most MAX_FOLDERS.
            folders = list(dict.fromkeys([tools.root, *tools.allowed_directories]))[:MAX_FOLDERS]
            tops = await asyncio.gather(*(_location(folder) for folder in folders))
            # A folder inside another folder of the same repository is covered by that folder's
            # status. Folders outside a work tree are left out only now, so a granted parent of the
            # project that is no repository, or a different repository, never hides the project's.
            located = sorted(((folder, top) for folder, top in zip(folders, tops) if top is not None),
                             key=lambda item: len(item[0].parts))
            roots = []
            for folder, top in located:
                if not any(top == other_top and folder.is_relative_to(other) for other, other_top in roots):
                    roots.append((folder, top))
            roots.sort(key=lambda item: item[0] != tools.root)
            tracker = cls(tools, [])
            prepared = await asyncio.gather(*(tracker._prepare(folder, top) for folder, top in roots))
            tracker.repositories = [repository for repository in prepared if repository is not None]
            return tracker if tracker.repositories else None
        except Exception as error:  # Tracking is best effort; the command runs either way.
            log.debug("Command change tracking is off for this command: %s", type(error).__name__)
            return None

    async def _prepare(self, folder, top):
        try:
            async with asyncio.timeout(AFTER_DEADLINE):
                repository = _Repository(folder, top)
                await repository.read_filters()
                changed = await repository.status()
                if changed is None:
                    return None
                repository.head = await repository.current_head()
                repository.changed = changed
                for path in sorted(changed):
                    target = self._admit(repository, path)
                    if target is not None:
                        repository.snapshots[path] = self._snapshot_before(target)
                return repository
        except Exception as error:
            log.debug("Command change tracking skipped a folder: %s", type(error).__name__)
            return None

    def _admit(self, repository, path):
        """The file's absolute path when it may be tracked, else None."""
        target = repository.top / path
        if not target.is_relative_to(repository.folder):
            return None
        try:
            resolved = self.tools.path(str(target))
        except (ValueError, OSError):
            return None  # Credential files, generated folders and folders this chat may not use.
        # No symbolic link anywhere on the path: the row must name the file that changed.
        return resolved if str(resolved) == str(target) else None

    def _snapshot_before(self, target):
        if self.snapshot_count >= MAX_SNAPSHOTS or self.snapshot_bytes >= MAX_SNAPSHOT_BYTES:
            return UNSUPPORTED
        try:
            state = _snapshot(target, FILE_LIMIT)
        except (ValueError, OSError, UnicodeError):
            return UNSUPPORTED  # Too large, binary, a link or unreadable.
        size = len(state["content"] or "")
        if self.snapshot_bytes + size > MAX_SNAPSHOT_BYTES:
            return UNSUPPORTED
        self.snapshot_count += 1
        self.snapshot_bytes += size
        return state

    async def after(self):
        """[(absolute path, state before the command)] for the files the command changed. Each
        repository gets AFTER_DEADLINE; on timeout it returns what it found so far. Never raises."""
        found = {}
        await asyncio.gather(*(self._collect(repository, found) for repository in self.repositories))
        return sorted(found.items(), key=lambda item: str(item[0]))

    async def _collect(self, repository, found):
        try:
            async with asyncio.timeout(AFTER_DEADLINE):
                await repository.read_filters()
                changed = await repository.status()
                if changed is None:
                    return
                head = await repository.current_head()
                moved = set()
                if head and head != repository.head:
                    moved = await repository.moved(head)  # git commit, checkout, stash or a first commit.
                # What the command surely touched comes first: paths status lists only now or with
                # another code, and paths the HEAD change names; then files changed before it.
                before = repository.changed
                fresh = {path for path, code in changed.items() if before.get(path) != code} | moved
                rest = (set(before) | set(changed)) - fresh
                pending, admitted = [], 0
                for path in [*sorted(fresh), *sorted(rest)]:
                    if admitted >= MAX_CANDIDATES:
                        log.debug("Command change tracking reached its limit of %d files.", MAX_CANDIDATES)
                        break
                    target = self._admit(repository, path)
                    if target is None:
                        continue
                    admitted += 1
                    try:
                        current = _snapshot(target, FILE_LIMIT)
                    except (ValueError, OSError, UnicodeError):
                        continue  # A link, binary, too large or unreadable now.
                    if path in repository.changed:
                        baseline = repository.snapshots.get(path, UNSUPPORTED)
                        if baseline != UNSUPPORTED:
                            self._add(found, target, baseline, current)
                    elif repository.head is None:
                        self._add(found, target, _state(), current)  # No commit yet: it did not exist.
                    else:
                        pending.append((path, target, current))
                if pending:
                    await self._committed(repository, pending, found)
        except TimeoutError:
            log.debug("Command change tracking stopped at its deadline.")
        except Exception as error:
            log.debug("Command change tracking failed for a folder: %s", type(error).__name__)

    async def _committed(self, repository, pending, found):
        """Baselines from the HEAD before the command, for files that were clean before it."""
        entries = await repository.entries([path for path, _, _ in pending])
        for path, target, current in pending:
            entry = entries.get(path)
            if entry is None:
                self._add(found, target, _state(), current)  # Not in that commit: it did not exist.
                continue
            mode, kind, object_id = entry
            if kind != "blob" or mode not in ("100644", "100755") or not OBJECT_ID.match(object_id):
                continue  # A link or submodule in that commit.
            if self.blob_reads >= MAX_BLOB_READS:
                log.debug("Command change tracking reached its limit of %d committed files.", MAX_BLOB_READS)
                continue
            self.blob_reads += 1
            code, raw, truncated = await repository.git(["cat-file", "blob", object_id], limit=FILE_LIMIT + 1)
            if code != 0 or truncated or len(raw) > FILE_LIMIT or b"\0" in raw:
                continue
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            fallback = 0o755 if mode == "100755" else 0o644
            self._add(found, target, _state(raw, current["mode"] if current["exists"] else fallback), current)

    @staticmethod
    def _add(found, target, baseline, current):
        if baseline["exists"] == current["exists"] and baseline["content"] == current["content"]:
            return  # Unchanged by the command.
        found[target] = baseline
