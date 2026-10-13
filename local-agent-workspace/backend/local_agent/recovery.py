"""Bounded, file-level recovery for edits made through the application."""
import base64
import hashlib
import json
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .edits import clip_result, plan_change, preview_diff, unified_diff
from .tools import MAX_WRITE_LIMIT, file_error

CHECKPOINT_BYTES_LIMIT = 256 * 1024 * 1024
CHECKPOINT_PRUNE_TARGET = 192 * 1024 * 1024
SYMLINK = "{} is a symbolic link; move_path and delete_path work on regular files only."
NOT_MOVABLE = ("{tool} {verb} one regular UTF-8 text file within the file write limit; "
               "use run_command for folders, binary or larger files.")


def _umask():
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as status:
            for line in status:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    old = os.umask(0)  # No /proc (macOS): read the umask by setting and restoring it.
    os.umask(old)
    return old


# New files get the usual mode for the user's umask; existing files keep theirs.
NEW_FILE_MODE = 0o666 & ~_umask()


def _lines(text):
    # Split at "\n" only, like the change review (changes.py imports this module).
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def _state(content=None, mode=None, identity=None):
    exists = content is not None
    digest = hashlib.sha256(f"{exists}:{mode}:".encode() + (content or b"")).hexdigest()
    return {"exists": exists, "content": base64.b64encode(content).decode() if exists else None,
            "mode": mode, "hash": digest, "identity": identity}


def _bytes(state):
    return base64.b64decode(state["content"]) if state["exists"] else b""


@contextmanager
def _parent(target, create=False):
    """Pin each directory without following symlinks, including swapped parents."""
    descriptor = os.open(target.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in target.parent.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _read_at(directory, name, limit=MAX_WRITE_LIMIT):
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return _state()
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Recovery supports regular UTF-8 files only.")
        raw = source.read(limit + 1)
        after = os.fstat(source.fileno())
        if len(raw) > limit:
            raise ValueError(f"Recovery supports files up to {limit // 1000:,} KB.")
        if b"\x00" in raw:
            raise ValueError("Binary files cannot be checkpointed.")
        raw.decode("utf-8")
        identity = lambda info: [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns]
        if identity(before) != identity(after):
            raise ValueError("File changed while being read; retry the action.")
        return _state(raw, stat.S_IMODE(after.st_mode), identity(after))


def _snapshot(target, limit=MAX_WRITE_LIMIT):
    try:
        with _parent(target) as directory:
            return _read_at(directory, target.name, limit)
    except FileNotFoundError:
        return _state()


def _same(current, expected):
    return (current["hash"] == expected["hash"]
            and (expected.get("identity") is None or current["identity"] == expected["identity"]))


def _diff(before, after, path):
    result = "".join(unified_diff(_lines(_bytes(before).decode("utf-8")),
                                  _lines(_bytes(after).decode("utf-8")),
                                  fromfile=path if before["exists"] else "/dev/null",
                                  tofile=path if after["exists"] else "/dev/null"))
    if not result and before["exists"] != after["exists"]:
        return ("Create" if after["exists"] else "Delete") + f" empty file: {path}"
    return result or "No changes."


class CheckpointManager:
    def __init__(self, store):
        self.store = store
        db = store.db
        db.execute("CREATE TABLE IF NOT EXISTS checkpoints (id TEXT PRIMARY KEY, workspace TEXT, session_id TEXT, data TEXT)")
        # Record sizes live beside the records, so the store cap never reads the data column.
        db.execute("CREATE TABLE IF NOT EXISTS checkpoint_sizes (id TEXT PRIMARY KEY, size INTEGER, pending INTEGER, seq INTEGER)")
        # Stores from before the cap: size every record once, oldest saves first.
        db.execute("""INSERT INTO checkpoint_sizes (id, size, pending, seq)
                      SELECT id, length(CAST(data AS BLOB)), instr(data, '"status": "pending"') > 0, rowid
                      FROM checkpoints WHERE id NOT IN (SELECT id FROM checkpoint_sizes)""")
        db.commit()
        self.total = db.execute("SELECT COALESCE(SUM(size), 0) FROM checkpoint_sizes").fetchone()[0]

    def _save(self, record):
        data = json.dumps(record)
        size, pending = len(data.encode("utf-8")), int(record["status"] == "pending")
        db = self.store.db
        row = db.execute("SELECT size FROM checkpoint_sizes WHERE id=?", (record["id"],)).fetchone()
        db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?, ?, ?, ?)",
                   (record["id"], record["workspace"], record["session_id"], data))
        if row:
            db.execute("UPDATE checkpoint_sizes SET size=?, pending=? WHERE id=?", (size, pending, record["id"]))
        else:
            seq = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM checkpoint_sizes").fetchone()[0]
            db.execute("INSERT INTO checkpoint_sizes VALUES (?, ?, ?, ?)", (record["id"], size, pending, seq))
        db.commit()
        self.total += size - (row[0] if row else 0)

    def _delete(self, ids):
        db = self.store.db
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            db.execute(f"DELETE FROM checkpoints WHERE id IN ({marks})", chunk)
            db.execute(f"DELETE FROM checkpoint_sizes WHERE id IN ({marks})", chunk)
        db.commit()
        self.total = db.execute("SELECT COALESCE(SUM(size), 0) FROM checkpoint_sizes").fetchone()[0]

    def _prune(self, keep_id):
        """Bound the store: drop the oldest finished records once it exceeds the cap."""
        if self.total <= CHECKPOINT_BYTES_LIMIT:
            return
        excess, ids = self.total - CHECKPOINT_PRUNE_TARGET, []
        rows = self.store.db.execute("SELECT id, size FROM checkpoint_sizes WHERE pending=0 AND id != ? ORDER BY seq",
                                     (keep_id,))
        for checkpoint_id, size in rows:
            if excess < 0:
                break
            ids.append(checkpoint_id)
            excess -= size
        rows.close()
        self._delete(ids)

    @staticmethod
    def _metadata(record):
        return {key: record[key] for key in ("id", "workspace", "session_id", "path", "created", "status", "restores")} | {
            "before_exists": record["before"]["exists"], "after_exists": record["after"]["exists"],
            "after_hash": record["after"]["hash"], "turn_id": record.get("turn_id")}

    def list(self, session_id=None, workspace=None):
        workspace = str(Path(workspace).expanduser().resolve()) if workspace is not None else None
        records = (json.loads(row[0]) for row in self.store.db.execute("SELECT data FROM checkpoints"))
        return [self._metadata(record) for record in sorted(records, key=lambda record: record["created"], reverse=True)
                if (session_id is None or record["session_id"] == session_id)
                and (workspace is None or record["workspace"] == workspace)]

    def delete_session(self, session_id):
        self.store.db.execute("DELETE FROM checkpoint_sizes WHERE id IN (SELECT id FROM checkpoints WHERE session_id=?)",
                              (session_id,))
        self.store.db.execute("DELETE FROM checkpoints WHERE session_id=?", (session_id,))
        self.store.db.commit()
        self.total = self.store.db.execute("SELECT COALESCE(SUM(size), 0) FROM checkpoint_sizes").fetchone()[0]

    def _get(self, checkpoint_id, tools, session_id):
        row = self.store.db.execute("SELECT data FROM checkpoints WHERE id=?", (checkpoint_id,)).fetchone()
        record = json.loads(row[0]) if row else None
        if not record or record["workspace"] != str(tools.root) or record["session_id"] != session_id:
            raise ValueError("Checkpoint not found in this conversation or workspace.")
        target = tools.path(record["path"])
        if str(target) != record["target"]:
            raise ValueError("Checkpoint path now resolves to a different file.")
        return record, target

    def _write(self, tools, path, target, before, after):
        with _parent(target, create=after["exists"]) as directory:
            def check():
                if tools.path(path) != target:
                    raise ValueError("File path changed while preparing this edit.")
                parent = target.parent.stat()
                opened = os.fstat(directory)
                if (parent.st_dev, parent.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ValueError("File directory changed while preparing this edit.")
                if not _same(_read_at(directory, target.name), before):
                    raise ValueError("File changed since the checkpoint or preview; refusing to overwrite it.")

            temporary = f".local-agent-edit-{uuid.uuid4().hex}"
            committed = False
            try:
                if after["exists"]:
                    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o600, dir_fd=directory)
                    with os.fdopen(descriptor, "wb") as destination:
                        destination.write(_bytes(after))
                        os.fchmod(destination.fileno(), after["mode"])
                        destination.flush()
                        os.fsync(destination.fileno())
                check()
                if after["exists"]:
                    os.replace(temporary, target.name, src_dir_fd=directory, dst_dir_fd=directory)
                else:
                    os.unlink(target.name, dir_fd=directory)
                committed = True
                os.fsync(directory)
            except BaseException as error:
                error.checkpoint_written = committed
                raise
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass

    def _apply(self, tools, path, target, before, after, session_id, restores=None, turn_id=None):
        record = {"id": str(uuid.uuid4()), "workspace": str(tools.root), "session_id": session_id,
                  "path": path, "target": str(target), "created": time.time(), "status": "pending",
                  "before": before, "after": after, "restores": restores, "turn_id": turn_id}
        # The original bytes are durable before any application write occurs.
        self._save(record)
        applied = False
        try:
            self._write(tools, path, target, before, after)
            applied = True
            actual = _snapshot(target)
            if actual["hash"] != after["hash"]:
                raise ValueError("File changed immediately after the edit; its recovery checkpoint was retained.")
            record.update(after=actual, status="applied")
            self._save(record)
        except BaseException as error:
            # Keep recovery data if replacement succeeded but a later fsync or
            # metadata update failed. Remove checkpoints for unchanged failed edits.
            if not applied and not getattr(error, "checkpoint_written", False):
                self._delete([record["id"]])
            raise
        self._prune(record["id"])
        return record

    def apply_edit(self, tools, name, arguments, session_id=None, turn_id=None, on_applied=None):
        if name not in ("write_file", "edit_file"):
            raise ValueError("Only app file writes and edits can create checkpoints.")
        target = tools.path(arguments["path"])
        before = _snapshot(target)
        before_text = _bytes(before).decode("utf-8") if before["exists"] else None
        plan = plan_change(name, arguments, before_text, tools.write_limit)
        after = _state(plan.text.encode("utf-8"), before["mode"] if before["exists"] else NEW_FILE_MODE)
        if before["hash"] == after["hash"]:
            return "No changes."
        record = self._apply(tools, arguments["path"], target, before, after, session_id, turn_id=turn_id)
        if on_applied is not None:
            on_applied(record)
        diff = preview_diff(tools.display_path(target), before_text, plan.text)
        # The model gets a bounded result; the change review shows the whole diff.
        return clip_result(plan.prefix + (diff if diff != "No changes." else _diff(before, after, tools.display_path(target))))

    @staticmethod
    def check_file_operation(tools, name, arguments):
        """Validate move_path or delete_path without changing anything.

        Returns (source, source state, destination, destination state); the
        destination entries are None for delete_path.
        """
        verb = "moves" if name == "move_path" else "deletes"
        names = [("path", arguments["path"])] + ([("destination", arguments["destination"])] if name == "move_path" else [])
        for label, value in names:
            # Check the link itself, before tools.path resolves it to its target. The
            # folders above it are resolved first, so "link/../link" names the link too.
            unresolved = tools.root / Path(value).expanduser()
            if unresolved.name in ("", ".."):
                continue  # A folder; refused below.
            try:
                info = os.lstat(unresolved.parent.resolve() / unresolved.name)
            except (FileNotFoundError, NotADirectoryError):
                continue
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(SYMLINK.format(label))
        source = tools.path(arguments["path"])
        try:
            # The write limit applies to what a move or delete carries; snapshots of
            # other files read up to the recovery maximum.
            before = _snapshot(source, tools.write_limit)
        except (ValueError, UnicodeError, IsADirectoryError) as error:
            raise ValueError(NOT_MOVABLE.format(tool=name, verb=verb)) from error
        if not before["exists"]:
            raise ValueError("path does not exist.")
        if name != "move_path":
            return source, before, None, None
        destination = tools.path(arguments["destination"])
        try:
            same = destination == source or destination.exists() and destination.samefile(source)
        except OSError:
            same = False
        if same:
            raise ValueError("path and destination are the same file.")
        if destination.is_dir():
            raise ValueError("destination is a folder; give the full new file path.")
        current = _snapshot(destination)
        if current["exists"] and arguments.get("overwrite") is not True:
            raise ValueError("destination already exists; set overwrite=true to replace it (read it first).")
        return source, before, destination, current

    def apply_move(self, tools, path, destination, *, overwrite=False, session_id=None, turn_id=None, on_applied=None):
        """Move one text file as two checkpoints: create (or replace) the destination, then delete the source."""
        arguments = {"path": path, "destination": destination, "overwrite": overwrite}
        source, before, target, current = self.check_file_operation(tools, "move_path", arguments)
        created = self._apply(tools, destination, target, current, _state(_bytes(before), before["mode"]), session_id,
                              turn_id=turn_id)
        if on_applied is not None:
            on_applied(created)
        try:
            removed = self._apply(tools, path, source, before, _state(), session_id, turn_id=turn_id)
        except (ValueError, OSError) as error:
            raise ValueError(f"Created {destination} but could not remove {path}: {file_error(error)}. "
                             "Both files exist now; review them in the change review.") from error
        if on_applied is not None:
            on_applied(removed)
        return f"Moved {path} to {destination}."

    def apply_delete(self, tools, path, *, session_id=None, turn_id=None, on_applied=None):
        source, before, _, _ = self.check_file_operation(tools, "delete_path", {"path": path})
        record = self._apply(tools, path, source, before, _state(), session_id, turn_id=turn_id)
        if on_applied is not None:
            on_applied(record)
        return f"Deleted {path}. Undo restores it from the change review."

    def apply_text(self, tools, path, text, before, *, session_id=None, turn_id=None, on_applied=None):
        """Write text planned from before (the file's text, or None); refuses if the file no longer matches."""
        target = tools.path(path)
        current = _snapshot(target)
        if (_bytes(current).decode("utf-8") if current["exists"] else None) != before:
            raise ValueError("The file changed before execution. Read it again and propose a fresh edit.")
        raw = text.encode("utf-8")
        if len(raw) > MAX_WRITE_LIMIT or b"\x00" in raw:
            raise ValueError(f"File content must be UTF-8 text without NUL bytes and at most {MAX_WRITE_LIMIT // 1000} KB.")
        after = _state(raw, current["mode"] if current["exists"] else NEW_FILE_MODE)
        if after["hash"] == current["hash"]:
            return None
        record = self._apply(tools, path, target, current, after, session_id, turn_id=turn_id)
        if on_applied is not None:
            on_applied(record)
        return record

    def apply_state(self, tools, path, target, current, after, session_id=None):
        """Replace a reviewed state with another one, keeping a restorable checkpoint."""
        return self._apply(tools, path, target, current, after, session_id)

    def preview(self, checkpoint_id, tools, session_id=None):
        record, target = self._get(checkpoint_id, tools, session_id)
        current = _snapshot(target)
        allowed = record["status"] != "restored" and _same(current, record["after"])
        result = {"id": checkpoint_id, "path": record["path"], "diff": _diff(current, record["before"], record["path"]),
                  "expected_current_hash": current["hash"], "can_restore": allowed}
        if not allowed:
            result["error"] = ("This checkpoint has already been restored." if record["status"] == "restored"
                               else "File changed since this app edit; recovery will not overwrite those changes.")
        return result

    def preview_turn(self, turn_id, tools, session_id, offset=0):
        records = [item for item in self.list(session_id, str(tools.root))
                   if item["session_id"] == session_id and item["turn_id"] == turn_id]
        if not turn_id or not records:
            raise ValueError("No checkpoints found for this turn in this conversation or workspace.")
        if type(offset) is not int or not 0 <= offset < len(records):
            raise ValueError("offset must identify a checkpoint in this turn.")
        previews = []
        # Keep multi-file previews bounded; individual restores retain all their checks.
        for record in records[offset:offset + 10]:
            try:
                previews.append(self.preview(record["id"], tools, session_id))
            except (ValueError, OSError, UnicodeError) as exc:
                previews.append({"id": record["id"], "path": record["path"], "diff": "",
                                 "expected_current_hash": "", "can_restore": False, "error": file_error(exc)})
        next_offset = offset + len(previews)
        return {"turn_id": turn_id, "previews": previews,
                "next_offset": next_offset if next_offset < len(records) else None}

    def restore(self, checkpoint_id, tools, expected_current_hash, session_id=None):
        record, target = self._get(checkpoint_id, tools, session_id)
        current = _snapshot(target)
        if record["status"] == "restored" or current["hash"] != expected_current_hash or not _same(current, record["after"]):
            raise ValueError("File changed or the checkpoint was already restored; request a new recovery preview.")
        reverse = self._apply(tools, record["path"], target, current, record["before"], session_id, restores=checkpoint_id)
        record["status"] = "restored"
        self._save(record)
        return {**self._metadata(record), "reverse_checkpoint_id": reverse["id"],
                "diff": _diff(current, record["before"], record["path"])}
