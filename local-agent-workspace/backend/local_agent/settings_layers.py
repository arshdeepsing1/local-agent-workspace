"""Project settings files (M5f3): a project's .agents/settings.json (shared with everyone who has the project) and
.agents/settings.local.json (yours, for this copy: keep it out of Git; the app never exports it) can set a few
Settings for the chats in that project.

Layers, a later one winning for the keys it sets: your Settings (settings.json in the state folder), the project file,
the local file. Only config.LAYERABLE keys are read from a project: the model, the default reasoning effort, the context
budget and output limit, the auto-compact threshold, Agent steps, compaction handoffs, prompt caching, the output style,
Automatic Keep and the spending limit. Each value is checked as a Settings save checks it (config.check_setting), and a
value that fails is not used and is named in the report. Nothing else comes from a project: not the project folder,
credentials, permission modes or rules, the commands' environment, hooks or MCP servers. Two more limits: the model must
be an endpoint the app knows or one you already use (requests carry the conversation), and a spending limit can only
lower yours. Budgets you left at your model's defaults follow a model the files set, as a Settings model change does.

The files apply only once you trust them: the first time a project with such a file is opened the app asks
(ProjectTrustDialog.js). <state>/trusted_projects.json remembers, per project (the sha256 of its resolved folder),
Trust or Ignore with the sha256 of each file you reviewed. A file that changes afterwards (also by the agent: .agents
edits always ask) is not used until you decide again. A worktree chat reads its source project's files. Standard
library only.
"""
import errno
import hashlib
import json
import os
import stat
import tempfile
import threading
import time
from pathlib import Path

from .config import ENDPOINT_NAME, LAYERABLE, LIMIT_KEYS, SAFETY_MARGIN, check_setting, follow_limits
from .model_profiles import default_limits, known_endpoints

FILES = (("project", ".agents/settings.json"), ("local", ".agents/settings.local.json"))
LAYER_FILES = dict(FILES)
MAX_FILE_BYTES = 16_384
MAX_PROJECTS = 500
MAX_LISTED_KEYS = 20
CACHE_SECONDS = 2.0  # A report is read again after this, or at once when a file or a decision changes.
VERSION = 1
DECISIONS = ("trust", "ignore")


class ChangedFiles(ValueError):
    """The files differ from the ones the user reviewed (POST /api/settings/project/trust answers HTTP 409)."""


def project_folder(session, settings):
    """The project whose settings files a chat uses: a worktree chat's source project, else its folder (the Settings
    project without a chat)."""
    session = session if isinstance(session, dict) else {}
    worktree = session.get("worktree")
    source = worktree.get("source_workspace") if isinstance(worktree, dict) else None
    folder = source if isinstance(source, str) and source else session.get("workspace")
    return folder if isinstance(folder, str) and folder else settings.values["workspace"]


def read_file(root, relative):
    """(bytes, problem) of a settings file in root, or (None, None) when there is none. Only a regular file of at most
    16 KB, in a folder that stays inside the project, is read, and never through a link."""
    path = root / relative
    try:
        if not path.parent.is_dir():
            return None, None
        if not path.parent.resolve().is_relative_to(root):
            return (b"", "Its folder is a link that leaves the project, so it is not read.") if os.path.lexists(path) else (None, None)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        if exc.errno == errno.ELOOP or os.path.islink(path):
            return b"", "It is a link, so it is not read."
        return b"", f"It cannot be read ({exc.strerror or type(exc).__name__})."
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return b"", "It is not a regular file, so it is not read."
        if info.st_size > MAX_FILE_BYTES:
            return b"", f"It is larger than {MAX_FILE_BYTES // 1024} KB, so it is not read."
        chunks, size = [], 0
        while size <= MAX_FILE_BYTES:
            chunk = os.read(descriptor, MAX_FILE_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_FILE_BYTES:
            return b"", f"It is larger than {MAX_FILE_BYTES // 1024} KB, so it is not read."
        return data, None
    except OSError as exc:  # A read that fails part way (a dropped network mount answers EIO).
        return b"", f"It cannot be read ({exc.strerror or type(exc).__name__})."
    finally:
        os.close(descriptor)


def signature(path):
    """What changes when a file is replaced or written (lstat: a link is not followed), or None."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode


def checked_value(key, value, user, model_allowed):
    """A value from a project file as Settings would use it, or ValueError saying why it is not used."""
    value = check_setting(key, value)
    if key == "model":
        name = value.strip()
        if not ENDPOINT_NAME.match(name):
            raise ValueError("Name a serving endpoint (letters, digits, '.', '_' and '-', at most 128 characters).")
        if name != user.get("model") and not model_allowed(name):
            raise ValueError(f"{name} is neither an endpoint the app knows nor one you use (the Settings model, "
                             "Settings → Subagents or one of your chats), so Settings' model is used. Requests carry "
                             "the conversation, so a project file cannot choose a new endpoint.")
        return name
    limit = user.get("spending_limit_dbu")
    if key == "spending_limit_dbu" and isinstance(limit, (int, float)) and limit > 0 and (value == 0 or value > limit):
        raise ValueError(f"A project file can lower your spending limit ({limit:g} DBU), never raise or remove it.")
    return value


def parse(data, user, model_allowed):
    """(values, problems, ignored key names) of one settings file's bytes."""
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return {}, ["It is not valid JSON, so nothing in it is used."], []
    if not isinstance(document, dict):
        return {}, ["It must hold one JSON object of settings, so nothing in it is used."], []
    values, problems, ignored = {}, [], []
    for key, value in document.items():
        if key not in LAYERABLE:
            ignored.append(key if len(key) <= 64 else f"{key[:63]}…")
            continue
        try:
            values[key] = checked_value(key, value, user, model_allowed)
        except ValueError as exc:
            problems.append(f"{key}: {exc}")
    if len(ignored) > MAX_LISTED_KEYS:
        ignored = [*ignored[:MAX_LISTED_KEYS], f"and {len(ignored) - MAX_LISTED_KEYS:,} more"]
    return values, problems, ignored


def merge(user, layers):
    """(the changed values, their sources, budgets that follow the files' model, notes) of the files' valid values
    (layers: [(layer, values)], later ones winning) over the user's Settings."""
    values, sources = {}, {}
    for layer, layer_values in layers:
        for key, value in layer_values.items():
            values[key], sources[key] = value, layer
    notes, followed = [], []

    def budgets(own):
        """The budgets with those the files set (own) and, after a model change, those left at the user model's
        defaults following the files' model (config.follow_limits)."""
        current = tuple(values[key] if own and key in values else user[key] for key in LIMIT_KEYS)
        model = values.get("model", user["model"])
        if model == user["model"]:
            return current, []
        old, new = default_limits(user["model"]), default_limits(model)
        follow = [not (own and key in values) and user[key] == old[index] for index, key in enumerate(LIMIT_KEYS)]
        result = follow_limits(follow, new, current)
        return result, [key for index, key in enumerate(LIMIT_KEYS) if result[index] != current[index]]

    result, followed = budgets(True)
    if result[1] >= result[0] - SAFETY_MARGIN:
        notes.append("The context budget and output limit from the project's files do not fit together (the output "
                     f"limit must be below the budget minus {SAFETY_MARGIN:,}), so your Settings budgets are used.")
        for key in LIMIT_KEYS:
            values.pop(key, None)
            sources.pop(key, None)
        result, followed = budgets(False)
    for key, value in zip(LIMIT_KEYS, result):
        if key in followed:
            values[key], sources[key] = value, sources["model"]
    return values, sources, followed, notes


class ProjectLayers:
    """Reads a project's settings files and remembers your Trust or Ignore. model_allowed(name) says whether a model the
    files name may be used (AgentManager sets it to fallback_allowed); without it, only endpoints the app knows."""

    def __init__(self, settings):
        self.settings = settings
        self.path = Path(settings.state_dir) / "trusted_projects.json"
        self.model_allowed = None
        self._lock = threading.Lock()
        self._cache = {}

    def allowed(self, name):
        if self.model_allowed is not None:
            return self.model_allowed(name)
        return name.lower() in {item["model"] for item in known_endpoints()}

    # The decisions file

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        projects = data.get("projects") if isinstance(data, dict) and data.get("version") == VERSION else None
        return {key: value for key, value in projects.items() if isinstance(value, dict)} if isinstance(projects, dict) else {}

    def _write(self, projects):
        if len(projects) > MAX_PROJECTS:
            def updated(item):
                value = item[1].get("updated")
                return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
            projects = dict(sorted(projects.items(), key=updated)[-MAX_PROJECTS:])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, filename = tempfile.mkstemp(prefix="trusted-", suffix=".tmp", dir=self.path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                json.dump({"version": VERSION, "projects": projects}, target, indent=2)
            os.replace(filename, self.path)
        finally:
            Path(filename).unlink(missing_ok=True)

    # Reports

    @staticmethod
    def key(folder):
        return hashlib.sha256(str(folder).encode("utf-8", "surrogateescape")).hexdigest()

    def report(self, workspace, fresh=False):
        """What the project's settings files hold and whether they apply:
        {workspace, status ('none', 'pending', 'trusted' or 'ignored'), changed (pending because the files changed
        after a decision), decision, sha256 (of the files as read now; send it back with a decision), files: [{layer,
        name, sha256, values, problems, ignored}], values and sources (the keys whose value a trusted file sets, and
        'project' or 'local'), followed (budgets that follow the files' model), notes}."""
        try:
            folder = Path(workspace).expanduser().resolve()
            found = folder.is_dir()
        except (OSError, RuntimeError, ValueError):
            # A folder the app may not look at (no access, a dropped network mount): no files apply, and the page,
            # which reports the Settings project's files, still loads.
            found = False
        if not found:
            return self._empty(str(workspace))
        user = {key: self.settings.values.get(key) for key in LAYERABLE}
        cache_key = (str(folder), json.dumps(user, sort_keys=True, default=str),
                     tuple(signature(folder / name) for _, name in FILES), signature(folder / ".agents"),
                     signature(self.path))
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and not fresh and now - cached[0] < CACHE_SECONDS:
                return cached[1]
        result = self._build(folder, user)
        with self._lock:
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache[cache_key] = (now, result)
        return result

    def _empty(self, workspace):
        return {"workspace": workspace, "status": "none", "changed": False, "decision": None, "sha256": "",
                "files": [], "values": {}, "sources": {}, "followed": [], "notes": []}

    def _build(self, folder, user):
        files = []
        for layer, name in FILES:
            data, problem = read_file(folder, name)
            if data is None:
                continue
            values, problems, ignored = ({}, [problem], []) if problem else parse(data, user, self.allowed)
            files.append({"layer": layer, "name": name, "sha256": hashlib.sha256(data).hexdigest(), "values": values,
                          "problems": problems, "ignored": ignored})
        result = self._empty(str(folder))
        if not files:
            return result
        hashes = {entry["layer"]: entry["sha256"] for entry in files}
        record = self._read().get(self.key(folder)) or {}
        decision = record.get("decision") if record.get("decision") in DECISIONS else None
        same = decision is not None and record.get("files") == hashes
        status = ("trusted" if decision == "trust" else "ignored") if same else "pending"
        result.update(status=status, changed=decision is not None and not same, decision=decision, files=files,
                      sha256=hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest())
        if status == "trusted":
            values, sources, followed, notes = merge(user, [(entry["layer"], entry["values"]) for entry in files])
            result.update(values=values, sources=sources, followed=followed, notes=notes)
        return result

    def effective(self, workspace):
        """Settings for chats in this project: the user's values with a trusted file's values over the keys it sets."""
        values = self.report(workspace)["values"]
        return {**self.settings.values, **values} if values else self.settings.values

    def decide(self, workspace, decision, digest):
        """Remember Trust or Ignore for the files the user reviewed (digest: the report's sha256): the new report.
        ChangedFiles when the files are no longer the ones reviewed."""
        if decision not in DECISIONS:
            raise ValueError("Choose trust or ignore.")
        current = self.report(workspace, fresh=True)
        if not current["files"]:
            raise ValueError("This project has no settings files.")
        if current["sha256"] != digest:
            raise ChangedFiles("The project's settings files changed while you reviewed them. Review them again.")
        folder = current["workspace"]
        with self._lock:
            projects = self._read()
            projects.pop(self.key(folder), None)
            projects[self.key(folder)] = {"decision": decision, "updated": time.time(),
                                          "files": {entry["layer"]: entry["sha256"] for entry in current["files"]}}
            self._write(projects)
            self._cache.clear()
        return self.report(folder, fresh=True)
