import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .telemetry import LEDGER_VERSION

log = logging.getLogger(__name__)

_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
# Kept out of the header record so list and metadata reads stay small.
_SEPARATE_FIELDS = ("context_state", "instruction_directories", "file_views", "allow_rules", "inference_calls", "command_jobs",
                    "queued_messages", "pending_context", "instruction_paths")
# Context meter fields left out of list summaries and headers (see Store.summary).
_METER_FIELDS = ("last_request", "next_estimate")
# Kept in <id>.state.json beside the conversation file, never in field records: versions
# before these fields existed reject unknown field records but only read *.jsonl files.
_SIDECAR_FIELDS = ("file_views", "allow_rules", "queued_messages", "pending_context", "instruction_paths")


# JSON encodings of strings this long are kept between saves of a running conversation
# (Store._encoded): tool results and replies are most of a large conversation's bytes, and each save
# rewrites the whole file. An entry holds its string, so it can never be stale.
CACHED_STRING_CHARS = 2048
# Dicts and lists nested deeper than this are encoded in one call, so a deeply nested value (tool
# arguments are the model's) cannot exhaust Python's recursion. Long strings sit far shallower:
# record, data, content or tool_calls, block or call, function, arguments.
WALKED_DEPTH = 8
_encode_json = json.JSONEncoder(ensure_ascii=False, separators=(",", ":")).encode


def _holds_long(container, depth):
    """Whether a dict or list has a string of CACHED_STRING_CHARS or more within depth levels."""
    for item in container.values() if type(container) is dict else container:
        kind = type(item)
        if kind is str:
            if len(item) >= CACHED_STRING_CHARS:
                return True
        elif depth > 1 and (kind is dict or kind is list) and _holds_long(item, depth - 1):
            return True
    return False


def _record_line(value, previous, current, depth=WALKED_DEPTH):
    """json.dumps(value, ensure_ascii=False, separators=(",", ":")), reusing the encodings of long
    strings from previous ({id: (string, encoding)}) and recording those it used in current.
    Only the dicts and lists that hold a long string are walked here; the rest is encoded in one call."""
    kind = type(value)
    if kind is str:
        if len(value) < CACHED_STRING_CHARS:
            return _encode_json(value)
        entry = previous.get(id(value))
        text = entry[1] if entry is not None and entry[0] is value else _encode_json(value)
        current[id(value)] = (value, text)
        return text
    if depth and (kind is dict or kind is list) and _holds_long(value, depth):
        if kind is list:
            return "[" + ",".join([_record_line(item, previous, current, depth - 1) for item in value]) + "]"
        if all(type(key) is str for key in value):
            return "{" + ",".join([_encode_json(key) + ":" + _record_line(item, previous, current, depth - 1)
                                   for key, item in value.items()]) + "}"
    return _encode_json(value)


class UnreadableConversation(ValueError):
    """A conversation file that cannot be loaded. The reason never quotes its contents."""

    def __init__(self, file, reason):
        super().__init__(f"Conversation file {file} could not be read: {reason}.")
        self.file, self.reason = file, reason


def _fingerprint(path):
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_size, info.st_mtime_ns)


class Store:
    """Operational SQLite state plus one complete JSONL file per conversation."""

    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conversations = path.parent / "conversations"
        if self.conversations.is_symlink():
            raise ValueError("Conversation storage directory cannot be a symbolic link.")
        self.conversations.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.conversations.chmod(0o700)
        self._lock = threading.RLock()
        self._command_jobs = {}
        # The sidecar state last written or read, as JSON text, per conversation ID.
        self._sidecars = {}
        # Per running conversation ID (keep_encodings), the long strings encoded at its last save:
        # {id(string): (string, JSON)}.
        self._encoded = {}
        # Unreadable conversation files: {file name: reason}. They are skipped and
        # reported, never deleted, moved or rewritten.
        self._list_problems = {}
        # Files whose header lists but whose full load failed: {name: (reason, fingerprint)}.
        self._load_problems = {}

        # Requests serialize DB work on the app's event loop. Allow application
        # creation and lifespan teardown on different threads (ASGI test clients).
        # SQLite remains the store for operational records such as jobs and tasks;
        # conversation content lives exclusively in the JSONL files above.
        self.db = sqlite3.connect(path, check_same_thread=False)
        path.chmod(0o600)
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA secure_delete=ON")
            self._migrate_legacy_sessions()
            # Also finishes cleanup if a prior process stopped after committing
            # the legacy table drop but before truncating its WAL.
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except BaseException:
            self.db.close()
            raise

    @staticmethod
    def summary(session):
        summary = {k: v for k, v in session.items()
                   if k not in ("wire", "events", *_SEPARATE_FIELDS)}
        info = summary.get("context_info")
        if isinstance(info, dict) and any(key in info for key in _METER_FIELDS):
            # The context meter's reported input lives only in the full session.
            summary["context_info"] = {k: v for k, v in info.items() if k not in _METER_FIELDS}
        return summary

    @staticmethod
    def _valid_id(session_id):
        return isinstance(session_id, str) and _SAFE_SESSION_ID.fullmatch(session_id) is not None

    def _path(self, session_id):
        if not self._valid_id(session_id):
            raise ValueError("Conversation ID is not safe for local storage.")
        return self.conversations / f"{session_id}.jsonl"

    @staticmethod
    def _records(session):
        metadata = {key: value for key, value in session.items()
                    if key not in ("events", "wire", *_SEPARATE_FIELDS)}
        yield {"record": "session", "version": 1, "data": metadata}
        for name in ("context_state", "instruction_directories"):
            if name in session:
                yield {"record": "field", "name": name, "data": session[name]}
        if "inference_calls" in session:
            calls = session["inference_calls"]
            if isinstance(calls, list):
                # The marker preserves an explicitly empty ledger while each
                # call remains its own inspectable record as the ledger grows.
                yield {"record": "field", "name": "inference_calls", "data": []}
                for call in calls:
                    yield {"record": "inference_call", "data": call}
            else:
                yield {"record": "field", "name": "inference_calls", "data": calls}
        if "command_jobs" in session:
            jobs = session["command_jobs"]
            if isinstance(jobs, list):
                yield {"record": "field", "name": "command_jobs", "data": []}
                for job in jobs:
                    yield {"record": "command_job", "data": job}
            else:
                yield {"record": "field", "name": "command_jobs", "data": jobs}
        for event in session.get("events", []):
            yield {"record": "event", "data": event}
        for message in session.get("wire", []):
            yield {"record": "wire", "data": message}

    def _temporary(self, session):
        target = self._path(session["id"])
        temporary = self.conversations / f".{target.stem}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        # Rebuilt on every save, so only the strings still in the conversation are kept.
        previous, current = self._encoded.get(session["id"]), {}
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                for record in self._records(session):
                    output.write(_encode_json(record) if previous is None else _record_line(record, previous, current))
                    output.write("\n")
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        if previous is not None:
            self._encoded[session["id"]] = current
        return temporary, target

    def keep_encodings(self, session_id):
        """Reuse the JSON of long strings between saves of this conversation until
        forget_encodings. Only a running turn saves the same string objects again: a
        conversation loaded anew (store.get) has new ones, so a kept entry would never be used."""
        with self._lock:
            self._encoded.setdefault(session_id, {})

    def forget_encodings(self, session_id):
        with self._lock:
            self._encoded.pop(session_id, None)

    def _sync_directory(self):
        descriptor = os.open(self.conversations, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _state_path(self, session_id):
        return self._path(session_id).with_suffix(".state.json")

    @staticmethod
    def _sidecar_text(session):
        state = {name: session[name] for name in _SIDECAR_FIELDS if name in session}
        return json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True) if state else None

    def _write_sidecar(self, session_id, text):
        """Replace (or with text None, remove) <id>.state.json atomically."""
        target = self._state_path(session_id)
        if text is None:
            target.unlink(missing_ok=True)
            return
        temporary = self.conversations / f".{target.name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(text)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            target.chmod(0o600)
            self._sync_directory()
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _save_sidecar(self, session):
        """Write the sidecar when its fields changed since it was last written or read."""
        text = self._sidecar_text(session)
        if self._sidecars.get(session["id"], "") != text:
            self._write_sidecar(session["id"], text)
            self._sidecars[session["id"]] = text

    def _read_sidecar(self, session_id):
        """The sidecar fields, or {} when the file is missing or unreadable (never fatal)."""
        path = self._state_path(session_id)
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "r", encoding="utf-8") as source:
                state = json.loads(source.read())
            if not isinstance(state, dict):
                raise ValueError("not an object")
        except FileNotFoundError:
            self._sidecars[session_id] = None
            return {}
        except (OSError, ValueError, RecursionError) as exc:
            # Rewritten on the next save; the conversation itself stays readable.
            log.warning("Ignored unreadable conversation state file %s (%s).", path.name, type(exc).__name__)
            self._sidecars.pop(session_id, None)
            return {}
        state = {name: state[name] for name in _SIDECAR_FIELDS if name in state}
        self._sidecars[session_id] = self._sidecar_text(state)
        return state

    def _write(self, session, *, insert_only=False):
        temporary, target = self._temporary(session)
        try:
            if insert_only:
                # link() is the atomic create-if-absent primitive. It cannot
                # replace another conversation if a name appears between
                # validation and publication.
                try:
                    os.link(temporary, target)
                except FileExistsError as exc:
                    raise sqlite3.IntegrityError("conversation ID already exists") from exc
                temporary.unlink()
            else:
                os.replace(temporary, target)
            target.chmod(0o600)
            self._sync_directory()
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _read_lines(path, *, metadata_only=False):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as source:
                line_number = 1
                try:
                    first = source.readline()
                    if not first:
                        raise UnreadableConversation(path.name, "the file is empty")
                    record = json.loads(first)
                    if (not isinstance(record, dict) or record.get("record") != "session"
                            or record.get("version") != 1 or not isinstance(record.get("data"), dict)):
                        raise UnreadableConversation(path.name, "the first line is not a conversation header")
                    session = record["data"]
                    if metadata_only:
                        return session
                    events, wire = [], []
                    for line_number, line in enumerate(source, start=2):
                        record = json.loads(line)
                        kind = record.get("record") if isinstance(record, dict) else None
                        if (kind == "field" and record.get("name") in _SEPARATE_FIELDS
                                and "data" in record):
                            session[record["name"]] = record["data"]
                        elif kind == "inference_call" and "data" in record:
                            session.setdefault("inference_calls", []).append(record["data"])
                        elif kind == "command_job" and "data" in record:
                            session.setdefault("command_jobs", []).append(record["data"])
                        elif kind == "event" and "data" in record:
                            events.append(record["data"])
                        elif kind == "wire" and "data" in record:
                            wire.append(record["data"])
                        else:
                            raise UnreadableConversation(path.name, f"line {line_number} is not a valid record")
                except json.JSONDecodeError:
                    raise UnreadableConversation(path.name, f"line {line_number} is not valid JSON") from None
                except UnicodeDecodeError:
                    raise UnreadableConversation(path.name, "the file is not UTF-8 text") from None
                except RecursionError:
                    raise UnreadableConversation(path.name, f"line {line_number} is nested too deeply") from None
                return {**session, "events": events, "wire": wire}
        except BaseException:
            # os.fdopen owns and normally closes the descriptor. If constructing
            # the wrapper itself failed, make a best-effort close here.
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise

    def _migrate_legacy_sessions(self):
        tables = {row[0] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('sessions', 'session_summaries')")}
        if "sessions" in tables:
            rows = self.db.execute("SELECT id, data FROM sessions").fetchall()
            for session_id, raw in rows:
                session = json.loads(raw)
                if session.get("id") != session_id:
                    raise ValueError("Legacy conversation ID does not match its stored payload.")
                target = self._path(session_id)
                if target.exists():
                    if self._read_lines(target) != session:
                        raise ValueError(f"Legacy conversation conflicts with {target.name}; migration stopped.")
                else:
                    self._write(session, insert_only=True)
        if tables:
            # Drop only after every full payload is durably represented as JSONL.
            # A failed or interrupted migration therefore remains restartable.
            with self.db:
                self.db.execute("DROP TABLE IF EXISTS session_summaries")
                self.db.execute("DROP TABLE IF EXISTS sessions")
            # Removing the logical tables is insufficient for chat privacy: old
            # payload bytes can remain in free pages or the WAL. Compact the
            # database and truncate both pre- and post-VACUUM WAL frames.
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.db.execute("VACUUM")
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def create(self, settings: dict):
        now = time.time()
        session = {"id": str(uuid.uuid4()), "title": "New conversation", "created": now,
                   "updated": now, "workspace": settings["workspace"],
                   "model": settings["model"], "events": [], "wire": [],
                   "permission_mode": "manual", "allowed_directories": [],
                   "inference_ledger_version": LEDGER_VERSION, "inference_calls": []}
        if settings.get("reasoning_effort"):
            # Kept with the chat like its model; a chat saved without one uses the Settings default.
            session["reasoning_effort"] = settings["reasoning_effort"]
        self.save(session)
        return session

    def save(self, session, *, touch=True):
        """Write the conversation. touch=False keeps its update time (metadata changes such as
        pin, archive or tags must not reorder the sidebar or mark the chat unread)."""
        with self._lock:
            updated = time.time() if touch or type(session.get("updated")) not in (int, float) else session["updated"]
            prepared = {**session, "updated": updated}
            session_id = session["id"]
            jobs = self._known_jobs(session_id)
            merged = self._merge_jobs(jobs, prepared.get("command_jobs", []))
            if merged or "command_jobs" in prepared:
                prepared["command_jobs"] = merged
            self._write(prepared)
            self._save_sidecar(prepared)
            self._command_jobs[session_id] = merged
        session["updated"] = prepared["updated"]

    @staticmethod
    def _merge_jobs(*collections):
        jobs = {}
        for collection in collections:
            if not isinstance(collection, list):
                continue
            for job in collection:
                if not isinstance(job, dict) or not isinstance(job.get("id"), str):
                    continue
                current = jobs.get(job["id"])
                if current is None or job.get("updated", 0) >= current.get("updated", 0):
                    jobs[job["id"]] = dict(job)
        return sorted(jobs.values(), key=lambda job: (job.get("created", 0), job["id"]))

    def _read_own(self, session_id):
        """The conversation stored in <session_id>.jsonl, or None if there is none.

        Raises UnreadableConversation if the file cannot be read or holds another
        conversation, so its data is never written under a different name."""
        path = self._path(session_id)
        try:
            session = self._read_lines(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise UnreadableConversation(path.name, f"it could not be opened ({exc.strerror or type(exc).__name__})") from None
        if session.get("id") != session_id:
            raise UnreadableConversation(path.name, "its conversation ID does not match the file name")
        return session

    def _known_jobs(self, session_id):
        if session_id not in self._command_jobs:
            try:
                session = self._read_own(session_id) or {}
            except UnreadableConversation:
                session = {}  # Nothing trustworthy to merge from that file.
            self._command_jobs[session_id] = self._merge_jobs(session.get("command_jobs", []))
        return self._command_jobs[session_id]

    def save_job(self, job):
        """Mirror a sanitized operational command snapshot into its conversation."""
        session_id = job.get("session_id") if isinstance(job, dict) else None
        if not self._valid_id(session_id):
            return
        with self._lock:
            session = None
            jobs = self._command_jobs.get(session_id)
            # An unreadable conversation file is left exactly as it is: the job
            # stays in the operational database and is not mirrored into it.
            if jobs is None:
                try:
                    session = self._read_own(session_id)
                except UnreadableConversation:
                    return
                if session is None:
                    return
                jobs = self._merge_jobs(session.get("command_jobs", []))
                self._command_jobs[session_id] = jobs
            current = next((item for item in jobs if item["id"] == job["id"]), None)
            if current == job or (current is not None and current.get("updated", 0) > job.get("updated", 0)):
                return
            if session is None:
                try:
                    session = self._read_own(session_id)
                except UnreadableConversation:
                    return
                if session is None:
                    return
            jobs = self._merge_jobs(session.get("command_jobs", []), jobs, [job])
            session["command_jobs"] = jobs
            legacy = {name: session[name] for name in _SIDECAR_FIELDS if name in session}
            if legacy:  # Older field records: move them first, as the rewrite drops them.
                self._save_sidecar({"id": session_id, **legacy, **self._read_sidecar(session_id)})
            self._write(session)
            self._command_jobs[session_id] = jobs

    def insert_sessions(self, sessions):
        """Insert validated imports as a group; existing IDs are never replaced."""
        now = time.time()
        prepared = [{"created": now, "updated": now, **session} for session in sessions]
        with self._lock:
            ids = [session.get("id") for session in prepared]
            for session_id in ids:
                self._path(session_id)
            if len(set(ids)) != len(ids) or any(self._path(session_id).exists() for session_id in ids):
                raise sqlite3.IntegrityError("conversation ID already exists")

            staged = []
            published = []
            try:
                for session in prepared:
                    staged.append(self._temporary(session))
                for temporary, target in staged:
                    try:
                        os.link(temporary, target)
                    except FileExistsError as exc:
                        raise sqlite3.IntegrityError("conversation ID already exists") from exc
                    published.append(target)
                    temporary.unlink()
                for target in published:
                    target.chmod(0o600)
                for session in prepared:
                    self._save_sidecar(session)
                self._sync_directory()
            except BaseException:
                for temporary, _ in staged:
                    temporary.unlink(missing_ok=True)
                for target in published:
                    target.unlink(missing_ok=True)
                for session in prepared:
                    self._state_path(session["id"]).unlink(missing_ok=True)
                    self._sidecars.pop(session["id"], None)
                    self._encoded.pop(session["id"], None)
                self._sync_directory()
                raise
        for original, saved in zip(sessions, prepared):
            original.update(created=saved["created"], updated=saved["updated"])
            self._command_jobs[saved["id"]] = self._merge_jobs(saved.get("command_jobs", []))

    def get(self, session_id):
        if not self._valid_id(session_id):
            return None
        path = self._path(session_id)
        with self._lock:
            known = self._load_problems.get(path.name)
            if known is not None:
                if known[1] == _fingerprint(path):
                    raise UnreadableConversation(path.name, known[0])
                del self._load_problems[path.name]  # Changed since; try it again.
            session = self._read_own(session_id)
            if session is None:
                return None
            # Older files keep these as field records; the sidecar is newer when both exist.
            session.update(self._read_sidecar(session_id))
            self._command_jobs[session_id] = self._merge_jobs(
                self._command_jobs.get(session_id, []), session.get("command_jobs", []))
            return session

    def header(self, session_id):
        """The conversation's metadata without its events, or None if there is no file.

        Reads only the first line. Raises UnreadableConversation like get() for a file
        whose header is damaged, names another conversation, or whose full load failed."""
        if not self._valid_id(session_id):
            return None
        path = self._path(session_id)
        with self._lock:
            known = self._load_problems.get(path.name)
            if known is not None:
                if known[1] == _fingerprint(path):
                    raise UnreadableConversation(path.name, known[0])
                del self._load_problems[path.name]  # Changed since; try it again.
            try:
                metadata = self._read_lines(path, metadata_only=True)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise UnreadableConversation(path.name, f"it could not be opened ({exc.strerror or type(exc).__name__})") from None
            if metadata.get("id") != session_id:
                raise UnreadableConversation(path.name, "its conversation ID does not match the file name")
            return self.summary(metadata)

    def event_texts(self, session_id):
        """Yield (event id, type, text) for the user and assistant events with text, in order.

        Parses only event records, never the wire or ledgers. A missing or damaged file
        ends the generator quietly (search skips what it cannot read)."""
        if not self._valid_id(session_id):
            return
        try:
            descriptor = os.open(self._path(session_id), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return
        try:
            source = os.fdopen(descriptor, "r", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            return
        with source:
            try:
                for line in source:
                    if not line.startswith('{"record":"event",'):
                        continue
                    record = json.loads(line)
                    event = record.get("data") if isinstance(record, dict) else None
                    if (isinstance(event, dict) and event.get("type") in ("user", "assistant")
                            and isinstance(event.get("text"), str) and isinstance(event.get("id"), str)):
                        yield event["id"], event["type"], event["text"]
            except (OSError, ValueError, RecursionError):
                return

    def list(self):
        """Readable conversations, newest first. Unreadable files are skipped and
        reported in problems(); they are left exactly as they are."""
        with self._lock:
            summaries, problems = [], {}
            for path in self.conversations.glob("*.jsonl"):
                if path.is_symlink():
                    continue
                known = self._load_problems.get(path.name)
                if known is not None:
                    if known[1] == _fingerprint(path):
                        problems[path.name] = known[0]
                        continue
                    del self._load_problems[path.name]  # Changed since; try it again.
                try:
                    metadata = self._read_lines(path, metadata_only=True)
                except FileNotFoundError:
                    continue  # Deleted while listing.
                except UnreadableConversation as exc:
                    problems[path.name] = exc.reason
                    continue
                except OSError as exc:
                    problems[path.name] = f"it could not be opened ({exc.strerror or type(exc).__name__})"
                    continue
                if metadata.get("id") != path.stem:
                    problems[path.name] = "its conversation ID does not match the file name"
                elif (type(metadata.get("updated")) not in (int, float) or not isinstance(metadata.get("workspace"), str)
                      or not isinstance(metadata.get("title", ""), str)):
                    problems[path.name] = "its header lacks a valid title, workspace or update time"
                else:
                    summaries.append(self.summary(metadata))
            self._list_problems = problems
            return sorted(summaries, key=lambda session: session["updated"], reverse=True)

    def record_problem(self, file, reason):
        """Hide a conversation whose full load failed until its file changes."""
        with self._lock:
            self._load_problems[file] = (reason, _fingerprint(self.conversations / file))
            self._list_problems[file] = reason

    def problem_workspaces(self):
        """Workspaces named by the readable headers of files the latest list() skipped."""
        with self._lock:
            names = list(self._list_problems)
        workspaces = []
        for name in names:
            try:
                header = self._read_lines(self.conversations / name, metadata_only=True)
            except (OSError, ValueError):
                continue  # No readable header: nothing names a workspace.
            if isinstance(header.get("workspace"), str):
                workspaces.append(header["workspace"])
        return workspaces

    def problems(self):
        """Files skipped by the latest list(), as [{file, reason}] sorted by name."""
        with self._lock:
            return [{"file": file, "reason": reason} for file, reason in sorted(self._list_problems.items())]

    def delete(self, session_id):
        if not self._valid_id(session_id):
            return
        with self._lock:
            self._path(session_id).unlink(missing_ok=True)
            self._state_path(session_id).unlink(missing_ok=True)
            self._command_jobs.pop(session_id, None)
            self._sidecars.pop(session_id, None)
            self._encoded.pop(session_id, None)
            self._sync_directory()
