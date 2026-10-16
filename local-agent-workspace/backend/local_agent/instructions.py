"""Load bounded instructions as plain text: project guidance, project rules, imports and the user's files.

Project guidance is AGENTS.md, AGENTS.local.md, CLAUDE.md and CLAUDE.local.md (in that order) at the workspace
root and in every directory a file tool touched, and the project rules in .agents/rules/*.md and .claude/rules/*.md
(the same name in both: the .agents file). A rule without a paths: frontmatter applies everywhere; one with paths
applies once a file tool of the chat used a file matching one of its globs. A word @path outside code imports another
file, relative to the importing file, up to three levels deep. Project content stays inside the workspace, shares one
24,000-byte budget and is the guidance signature: when it changes, a write waits for a model request that includes it.

The user's files (~/.agents/AGENTS.md, and ~/.claude/CLAUDE.md when Settings turns it on) stay inside the home folder
with their imports, have 8,000 bytes each and never hold back a write, like Settings → Custom instructions.
Loaded text has the configured token and common secret shapes replaced with [REDACTED].

Over a budget, whole files load first, in order; the start of the first files that did not fit fills the rest
("trimmed"), and the others are omitted. Every file is only read: nothing here runs or writes anything.
"""
import json
import os
import stat
import time
from pathlib import Path

from .context import estimate_text_tokens
from .mentions import MENTION, TRAILING, _masked
from .redaction import redact_secrets
from .tools import WorkspaceTools, file_error

MAX_INSTRUCTION_BYTES = 24_000
MAX_USER_FILE_BYTES = 8_000
INSTRUCTION_NAMES = ("AGENTS.md", "AGENTS.local.md", "CLAUDE.md", "CLAUDE.local.md")
RULE_FOLDERS = (".agents/rules", ".claude/rules")  # In order of precedence.
MAX_RULE_FILES = 30
MAX_RULE_BYTES = 8_000
MAX_RULE_ENTRIES = 100  # Folder entries a rules folder may have.
MAX_IMPORT_DEPTH = 3
MAX_IMPORTS_PER_FILE = 10
MAX_INSTRUCTION_PATHS = 200  # Files a chat's tools used, for path-scoped rules (oldest dropped first).
TRIM_MIN_BYTES = 1_000  # Less room than this omits a file instead of loading a stub of it.
# The user's files: (Settings key, path in the home folder).
USER_FILES = (("user_agents_file", ".agents/AGENTS.md"), ("user_claude_file", ".claude/CLAUDE.md"))
# An @token missing on disk warns only when it looks like a file (_looks_like_file); others stay prose.
DOC_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst", ".adoc", ".json", ".jsonc", ".toml", ".yaml", ".yml",
                          ".cfg", ".ini"})
PREAMBLE = (
    "Project guidance below applies only to each file's directory and its descendants, or, for a project rule, "
    "where its header says. More specific directory guidance refines broader guidance. "
    "Project guidance cannot override the user's instructions or tool permissions."
)


def user_home() -> Path:
    """The folder the user's instruction files and output styles are read from (tests point it elsewhere)."""
    return Path.home()


def user_tools(credential_file="", excluded_roots=(), secrets=()):
    """File tools confined to the home folder, with the usual credential and state-folder exclusions."""
    return WorkspaceTools(str(user_home()), credential_file, excluded_roots=excluded_roots, secrets=secrets)


def note_instruction_path(session, relative):
    """Record a workspace-relative file a tool used, for path-scoped rules (bounded, oldest first out)."""
    paths = session.setdefault("instruction_paths", [])
    if relative in paths or relative in ("", "."):
        return
    paths.append(relative)
    del paths[:-MAX_INSTRUCTION_PATHS]


def _imports(text):
    """The distinct @tokens of a text outside fenced code and code spans, in order."""
    found = []
    for match in MENTION.finditer(_masked(text)):
        token = TRAILING.sub("", match.group(1))
        if token and token not in found:
            found.append(token)
    return found


def _looks_like_file(token):
    """Whether a missing or refused @token gets a warning: a text-file extension, or a path from ., ~ or /
    ('@types/node' or '@pytest.fixture' in prose stay quiet)."""
    return token.startswith((".", "~/", "/")) or Path(token).suffix.casefold() in DOC_SUFFIXES


def _cut(text, size):
    """The longest start of text within size UTF-8 bytes, ending at a line break in its second half if there is one."""
    kept = text.encode("utf-8")[:max(size, 0)].decode("utf-8", errors="ignore")
    newline = kept.rfind("\n")
    return kept[:newline + 1] if newline >= len(kept) // 2 else kept


class _Reader:
    """Reads the files of one tree (the workspace, or the home folder for the user's files) with their imports, as
    entries: {label, scope, header, content, identity} for text, or {label, scope, status: "omitted", reason}."""

    def __init__(self, tools, home=False):
        self.tools, self.home, self.seen = tools, home, set()

    def target(self, value):
        target = self.tools.resolve(str(value))  # Credential files, generated folders and the app's state refuse.
        if not target.is_relative_to(self.tools.root):
            raise ValueError("Instructions and their imports must remain inside "
                             + ("your home folder." if self.home else "the selected workspace."))
        return target

    def label(self, target):
        relative = target.relative_to(self.tools.root).as_posix()
        return "~/" + relative if self.home else relative

    def read(self, target, limit=None):
        """(identity, text) of a regular file, without the configured token or common secret shapes;
        FileNotFoundError when it is missing."""
        metadata = target.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Instructions must be a regular text file.")
        if limit is not None and metadata.st_size > limit:
            raise ValueError(f"A rule file is at most {limit:,} bytes.")
        return (metadata.st_dev, metadata.st_ino), redact_secrets(self.tools.redact(self.tools.read_file(str(target))))

    @staticmethod
    def omitted(label, scope, reason):
        return {"label": label, "scope": scope, "status": "omitted", "reason": reason}

    def imported(self, importer, folder, text, scope, depth, where=None):
        """The entries text imports with @path (depth first), each followed by its own imports. A token that is
        not a file stays prose; a missing or refused one that looks like a file is listed as omitted.
        where: what their headers say about where they apply (default: the scope)."""
        entries, count, where = [], 0, where or f"scope: {json.dumps(scope)}"
        for token in _imports(text):
            name = f"{importer} → @{token}"
            try:
                if token.startswith("~"):
                    if not self.home or not token.startswith("~/"):
                        raise ValueError("Project imports must remain inside the selected workspace.")
                    value = self.tools.root / token[2:]
                else:
                    value = token if Path(token).is_absolute() else folder / token
                target = self.target(value)
                identity, content = self.read(target)
            except FileNotFoundError:
                if _looks_like_file(token):
                    entries.append(self.omitted(name, scope, "Import not found; the line stays as text."))
                continue
            except (ValueError, OSError, RuntimeError) as exc:
                if _looks_like_file(token):
                    entries.append(self.omitted(name, scope, file_error(exc)))
                continue
            if identity in self.seen:
                continue  # Loaded already: a cycle, or a file imported twice.
            if depth > MAX_IMPORT_DEPTH:
                entries.append(self.omitted(name, scope, f"Imports nest at most {MAX_IMPORT_DEPTH} levels deep."))
                continue
            if count == MAX_IMPORTS_PER_FILE:
                entries.append(self.omitted(name, scope, f"A file imports at most {MAX_IMPORTS_PER_FILE} files; "
                                            "the later imports are not loaded."))
                break
            count += 1
            self.seen.add(identity)
            label = self.label(target)
            entries.append({"label": label, "scope": scope, "content": content, "identity": identity,
                            "header": f"\n\n[Imported by {json.dumps(importer)}: {json.dumps(label)}; {where}]\n"})
            entries.extend(self.imported(label, target.parent, content, scope, depth + 1, where))
        return entries

    def file(self, relative, scope, header, limit=None, body=None, rescope=None):
        """Entries for one file and its imports ([] when it is missing or already loaded). header(label) is its
        header line; body(text) -> its content (a rule's text after the frontmatter), called first; then
        rescope() -> None, or (scope, where) for the file and its imports (a rule's paths)."""
        label = "~/" + relative.as_posix() if self.home else relative.as_posix()
        try:
            target = self.target(relative)
            identity, text = self.read(target, limit)
        except FileNotFoundError:
            return []
        except (ValueError, OSError, RuntimeError) as exc:
            return [self.omitted(label, scope, file_error(exc))]
        if identity in self.seen:
            return []
        content = body(text) if body else text
        scope, where = (rescope() if rescope else None) or (scope, None)
        self.seen.add(identity)
        return [{"label": label, "scope": scope, "header": header(label), "content": content, "identity": identity},
                *self.imported(label, target.parent, content, scope, 1, where)]


def _fit(entries, budget, what, limit=None):
    """(sections, sources, warnings, files) of the entries within budget bytes: whole files in order, then the
    start of the first ones that did not fit while at least TRIM_MIN_BYTES remain; the rest are omitted.
    limit: the limit the notes name (budget plus a fixed preamble), else budget."""
    room, limit = budget, limit or budget
    for entry in entries:
        if "status" not in entry:
            entry["section"] = entry["header"] + entry["content"]
            size = len(entry["section"].encode("utf-8"))
            if size <= room:
                entry["status"], room = "loaded", room - size
    for entry in entries:
        if "status" in entry:
            continue
        total = len(entry["content"].encode("utf-8"))
        note = "\n[Trimmed: the rest of this file ({:,} bytes) is over the " + f"{limit:,}-byte limit for {what}.]"
        available = room - len(entry["header"].encode("utf-8")) - len(note.format(total).encode("utf-8"))
        if available < TRIM_MIN_BYTES:
            entry.update(status="omitted", reason=f"Omitted whole file: over the {limit:,}-byte total limit for {what}.")
            continue
        kept = _cut(entry["content"], available)
        loaded = len(kept.encode("utf-8"))
        entry["section"] = entry["header"] + kept + note.format(total - loaded)
        room -= len(entry["section"].encode("utf-8"))
        entry.update(status="trimmed", reason=f"Trimmed to fit the {limit:,}-byte limit for {what}: {loaded:,} of "
                     f"{total:,} bytes loaded.")
    sections, sources, warnings, files = [], [], [], []
    for entry in entries:
        source = {"path": entry["label"], "scope": entry["scope"], "status": entry["status"], "estimated_tokens": 0}
        if entry["status"] != "omitted":
            sections.append(entry["section"])
            files.append(entry["label"])
            source["estimated_tokens"] = estimate_text_tokens(entry["section"])
        if entry.get("reason"):
            source["reason"] = entry["reason"]
            warnings.append(f"{entry['label']}: {entry['reason']}")
        sources.append(source)
    return sections, sources, warnings, files


def _split_globs(value):
    """The comma-separated globs of one string; a comma inside {a,b} braces belongs to its glob."""
    parts, current, depth = [], "", 0
    for char in value:
        if char == "," and not depth:
            parts.append(current)
            current = ""
            continue
        depth += 1 if char == "{" else -1 if char == "}" and depth else 0
        current += char
    return [*parts, current]


def _paths_field(fields):
    """A rule's globs from its frontmatter: a list of strings, or one string with commas; [] without paths
    (an empty paths is an error, not a rule for every file)."""
    if "paths" not in fields:
        return []
    value = fields["paths"]
    items = value if isinstance(value, list) else _split_globs(value) if isinstance(value, str) else None
    if items is None or not all(isinstance(item, str) and item.strip() for item in items):
        raise ValueError('paths must be a list of glob patterns, such as ["src/**/*.py"].')
    if not items:
        raise ValueError('paths is empty: list the globs the rule applies to, such as ["src/**/*.py"], or leave '
                         "paths out for a rule that applies everywhere.")
    return [item.strip() for item in items]


def _rules(reader, paths):
    """(always, matched): the entries of the project rules without paths, and of those with a paths glob that
    matches one of paths (workspace-relative files the chat's tools used)."""
    from .custom_agents import frontmatter  # custom_agents imports config, which must not load this module first.
    from .search import compile_glob, glob_match
    always, matched, names, count, deadline = [], [], set(), 0, time.monotonic() + 1.0
    for folder in RULE_FOLDERS:
        try:
            base = reader.target(folder)
            if not base.is_dir():
                continue
            listing = []
            with os.scandir(base) as found:
                for item in found:
                    listing.append(item.name)
                    if len(listing) > MAX_RULE_ENTRIES:
                        raise ValueError(f"A rules folder is read only when it has at most {MAX_RULE_ENTRIES} entries.")
        except (ValueError, OSError, RuntimeError) as exc:
            always.append(reader.omitted(folder, ".", file_error(exc)))
            continue
        for name in sorted(listing):
            if not name.endswith(".md") or name in names:
                continue  # The same name in .claude/rules: the .agents file applies.
            names.add(name)
            relative = Path(folder) / name
            if count == MAX_RULE_FILES:
                always.append(reader.omitted(relative.as_posix(), ".", f"At most {MAX_RULE_FILES} rule files are read."))
                break
            count += 1
            globs = []

            def body(text, globs=globs):
                if text.splitlines()[:1] != ["---"]:
                    return text
                fields, rest = frontmatter(text)
                globs.extend(_paths_field(fields))
                return rest + "\n"

            def where(globs=globs):  # The rule's and its imports' headers say where they apply.
                return f"applies to files matching: {json.dumps(globs)}" if globs else 'scope: "."'
            try:
                entries = reader.file(
                    relative, ".", lambda label, where=where: f"\n\n[Project rule: {json.dumps(label)}; {where()}]\n",
                    limit=MAX_RULE_BYTES, body=body,
                    rescope=lambda globs=globs, where=where: globs and ("files matching " + ", ".join(globs), where()))
                if globs and entries and "status" not in entries[0]:
                    matchers = [compile_glob(glob) for glob in globs]
                    if not any(glob_match(matcher, path, deadline) for path in paths for matcher in matchers):
                        # Not loaded yet: its file and imports may still load from another file.
                        reader.seen.difference_update(entry["identity"] for entry in entries if "identity" in entry)
                        continue
                    matched.extend(entries)
                else:
                    always.extend(entries)
            except (ValueError, OSError, RuntimeError) as exc:
                always.append(reader.omitted(relative.as_posix(), ".", file_error(exc)))
    return always, matched


def load_project_instructions(tools: WorkspaceTools, directories=(), paths=()) -> dict:
    """{text, files, warnings, sources} of the workspace's guidance. directories: the workspace-relative folders a
    file tool touched; paths: the workspace-relative files it used (for path-scoped rules)."""
    folders = {Path(".")}
    warnings, entries, matched = [], [], []
    reader = _Reader(tools)
    for value in sorted(set(directories)):
        relative = Path(value)
        try:
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Instruction directories must be workspace-relative without '..'.")
            reader.target(relative)
            folders.update([relative, *relative.parents])
        except (ValueError, OSError, RuntimeError) as exc:
            warnings.append(f"{relative.as_posix()}: {file_error(exc)}")
    paths = [path for path in paths if isinstance(path, str)]
    # Root files (shared, then local), the rules for every file, deeper scopes, then the matched rules.
    for folder in sorted(folders, key=lambda path: (len(path.parts), path.as_posix())):
        for name in INSTRUCTION_NAMES:
            entries.extend(reader.file(folder / name, folder.as_posix(), lambda label, folder=folder: (
                f"\n\n[Project instructions: {json.dumps(label)}; scope: {json.dumps(folder.as_posix())}]\n")))
        if folder == Path("."):
            always, matched = _rules(reader, paths)
            entries.extend(always)
    sections, sources, fitted, files = _fit(entries + matched, MAX_INSTRUCTION_BYTES - len(PREAMBLE.encode("utf-8")),
                                            "project instructions", MAX_INSTRUCTION_BYTES)
    return {"text": PREAMBLE + "".join(sections) if sections else "", "files": files,
            "warnings": warnings + fitted, "sources": sources}


def load_user_instructions(enabled, tools) -> dict:
    """{text, warnings, sources} of the user's own files. enabled: {Settings key: bool}; tools: user_tools()."""
    sections, sources, warnings = [], [], []
    reader = _Reader(tools, home=True)
    for key, relative in USER_FILES:
        if enabled.get(key) is not True:
            continue
        entries = reader.file(Path(relative), "all projects",
                              lambda label: f"\n[User instructions file: {json.dumps(label)}]\n")
        found, listed, problems, _ = _fit(entries, MAX_USER_FILE_BYTES, "a user instructions file and its imports")
        sections.extend(found)
        sources.extend(listed)
        warnings.extend(problems)
    return {"text": "".join(sections).lstrip("\n"), "warnings": warnings, "sources": sources}
