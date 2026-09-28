import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .telemetry import LEDGER_VERSION


_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SEPARATE_FIELDS = ("context_state", "instruction_directories", "inference_calls", "command_jobs")


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
        return {k: v for k, v in session.items()
                if k not in ("wire", "events", *_SEPARATE_FIELDS)}

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
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                for record in self._records(session):
                    output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                    output.write("\n")
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return temporary, target

    def _sync_directory(self):
        descriptor = os.open(self.conversations, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

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
                first = source.readline()
                if not first:
                    raise ValueError(f"Conversation file is empty: {path.name}")
                record = json.loads(first)
                if (not isinstance(record, dict) or record.get("record") != "session"
                        or record.get("version") != 1 or not isinstance(record.get("data"), dict)):
                    raise ValueError(f"Conversation file has an invalid header: {path.name}")
                session = record["data"]
                if metadata_only:
                    return session
                events, wire = [], []
                for line in source:
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
                        raise ValueError(f"Conversation file has an invalid record: {path.name}")
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
        self.save(session)
        return session

    def save(self, session):
        with self._lock:
            prepared = {**session, "updated": time.time()}
            session_id = session["id"]
            jobs = self._known_jobs(session_id)
            merged = self._merge_jobs(jobs, prepared.get("command_jobs", []))
            if merged or "command_jobs" in prepared:
                prepared["command_jobs"] = merged
            self._write(prepared)
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

    def _known_jobs(self, session_id):
        if session_id not in self._command_jobs:
            try:
                session = self._read_lines(self._path(session_id))
            except FileNotFoundError:
                session = {}
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
            if jobs is None:
                try:
                    session = self._read_lines(self._path(session_id))
                except FileNotFoundError:
                    return
                jobs = self._merge_jobs(session.get("command_jobs", []))
                self._command_jobs[session_id] = jobs
            current = next((item for item in jobs if item["id"] == job["id"]), None)
            if current == job or (current is not None and current.get("updated", 0) > job.get("updated", 0)):
                return
            if session is None:
                try:
                    session = self._read_lines(self._path(session_id))
                except FileNotFoundError:
                    return
            jobs = self._merge_jobs(session.get("command_jobs", []), jobs, [job])
            session["command_jobs"] = jobs
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
                self._sync_directory()
            except BaseException:
                for temporary, _ in staged:
                    temporary.unlink(missing_ok=True)
                for target in published:
                    target.unlink(missing_ok=True)
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
            try:
                session = self._read_lines(path)
            except FileNotFoundError:
                return None
            self._command_jobs[session_id] = self._merge_jobs(
                self._command_jobs.get(session_id, []), session.get("command_jobs", []))
            return session

    def list(self):
        with self._lock:
            summaries = []
            for path in self.conversations.glob("*.jsonl"):
                if path.is_symlink():
                    continue
                metadata = self._read_lines(path, metadata_only=True)
                if metadata.get("id") != path.stem:
                    raise ValueError(f"Conversation filename does not match its ID: {path.name}")
                summaries.append(self.summary(metadata))
            return sorted(summaries, key=lambda session: session["updated"], reverse=True)

    def delete(self, session_id):
        if not self._valid_id(session_id):
            return
        with self._lock:
            self._path(session_id).unlink(missing_ok=True)
            self._command_jobs.pop(session_id, None)
            self._sync_directory()
