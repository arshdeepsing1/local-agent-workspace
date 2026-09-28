"""Bounded, file-level recovery for edits made through the application."""
import base64
import difflib
import hashlib
import json
import os
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .tools import LIMIT, file_error


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


def _read_at(directory, name):
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return _state()
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Recovery supports regular UTF-8 files only.")
        raw = source.read(LIMIT + 1)
        after = os.fstat(source.fileno())
        if len(raw) > LIMIT:
            raise ValueError("Recovery supports files up to 80 KB.")
        if b"\x00" in raw:
            raise ValueError("Binary files cannot be checkpointed.")
        raw.decode("utf-8")
        identity = lambda info: [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns]
        if identity(before) != identity(after):
            raise ValueError("File changed while being read; retry the action.")
        return _state(raw, stat.S_IMODE(after.st_mode), identity(after))


def _snapshot(target):
    try:
        with _parent(target) as directory:
            return _read_at(directory, target.name)
    except FileNotFoundError:
        return _state()


def _same(current, expected):
    return (current["hash"] == expected["hash"]
            and (expected.get("identity") is None or current["identity"] == expected["identity"]))


def _diff(before, after, path):
    result = "".join(difflib.unified_diff(_bytes(before).decode("utf-8").splitlines(True),
                                          _bytes(after).decode("utf-8").splitlines(True),
                                          fromfile=path if before["exists"] else "/dev/null",
                                          tofile=path if after["exists"] else "/dev/null"))
    if not result and before["exists"] != after["exists"]:
        return ("Create" if after["exists"] else "Delete") + f" empty file: {path}"
    return result or "No changes."


class CheckpointManager:
    def __init__(self, store):
        self.store = store
        store.db.execute("CREATE TABLE IF NOT EXISTS checkpoints (id TEXT PRIMARY KEY, workspace TEXT, session_id TEXT, data TEXT)")
        store.db.commit()

    def _save(self, record):
        self.store.db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?, ?, ?, ?)",
                              (record["id"], record["workspace"], record["session_id"], json.dumps(record)))
        self.store.db.commit()

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
        self.store.db.execute("DELETE FROM checkpoints WHERE session_id=?", (session_id,))
        self.store.db.commit()

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
                self.store.db.execute("DELETE FROM checkpoints WHERE id=?", (record["id"],))
                self.store.db.commit()
            raise
        return record

    def apply_edit(self, tools, name, arguments, session_id=None, turn_id=None):
        if name not in ("write_file", "edit_file"):
            raise ValueError("Only app file writes and edits can create checkpoints.")
        target = tools.path(arguments["path"])
        before = _snapshot(target)
        preview = tools.change(name, arguments)
        old = _bytes(before).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        if name == "edit_file":
            needle = arguments["old_text"]
            if not needle or old.count(needle) != 1:
                raise ValueError("old_text must match exactly once; include more surrounding context.")
            new = old.replace(needle, arguments["new_text"], 1)
        else:
            new = arguments["content"]
        raw = new.encode("utf-8")
        if len(raw) > LIMIT or b"\x00" in raw:
            raise ValueError("File content must be UTF-8 text without NUL bytes and at most 80 KB.")
        after = _state(raw, before["mode"] if before["exists"] else 0o600)
        if before["hash"] == after["hash"]:
            return "No changes."
        self._apply(tools, arguments["path"], target, before, after, session_id, turn_id=turn_id)
        return preview if preview != "No changes." else _diff(before, after, tools.display_path(target))

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
