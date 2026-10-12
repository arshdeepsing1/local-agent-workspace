"""Review of the files an agent changed: list them, show the diff, Keep or Undo.

The first model edit to a file in a conversation records the file as it was before
that edit (its baseline). Later edits keep the baseline, so the review always
compares the file on disk with its state before the agent touched it. Keep accepts
the current content (the baseline moves forward); Undo writes the baseline back
through the checkpoint machinery, so an undo can itself be restored from
Agent tools -> Recovery. Both work on a whole file or on one change (hunk).
"""
import difflib
import json
import re
import time
from pathlib import Path

from .recovery import _bytes, _snapshot, _state
from .tools import file_error

LINE = re.compile(r"[^\n]*\n|[^\n]+")
STALE = "{path} changed since you reviewed it. Review it again."


def _lines(state):
    return LINE.findall(_bytes(state).decode("utf-8")) if state["exists"] else []


def _same_content(left, right):
    return left["exists"] == right["exists"] and left["content"] == right["content"]


def _status(baseline, current):
    if not baseline["exists"]:
        return "created"
    return "modified" if current["exists"] else "deleted"


def _opcodes(old, new):
    return difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes()


def _counts(opcodes):
    changed = [op for op in opcodes if op[0] != "equal"]
    return sum(j2 - j1 for _, _, _, j1, j2 in changed), sum(i2 - i1 for _, i1, i2, _, _ in changed)


def _line(kind, raw, **numbers):
    line = {"kind": kind, "text": raw.removesuffix("\n").removesuffix("\r"), **numbers}
    if kind != "context" and not raw.endswith("\n"):
        line["newline"] = False
    return line


class ChangeReview:
    def __init__(self, store, checkpoints):
        self.store, self.checkpoints = store, checkpoints
        store.db.execute("CREATE TABLE IF NOT EXISTS agent_changes (session_id TEXT, target TEXT, workspace TEXT, "
                         "data TEXT, PRIMARY KEY (session_id, target))")
        store.db.commit()

    def _save(self, entry):
        self.store.db.execute("INSERT OR REPLACE INTO agent_changes VALUES (?, ?, ?, ?)",
                              (entry["session_id"], entry["target"], entry["workspace"], json.dumps(entry)))
        self.store.db.commit()

    def _drop(self, entry):
        self.store.db.execute("DELETE FROM agent_changes WHERE session_id=? AND target=?",
                              (entry["session_id"], entry["target"]))
        self.store.db.commit()

    def delete_session(self, session_id):
        self.store.db.execute("DELETE FROM agent_changes WHERE session_id=?", (session_id,))
        self.store.db.commit()

    def has(self, session_id):
        return self.store.db.execute("SELECT 1 FROM agent_changes WHERE session_id=? LIMIT 1", (session_id,)).fetchone() is not None

    def track(self, record, tools):
        """Add an applied model edit (its checkpoint record) to the conversation's review."""
        if not record.get("session_id"):
            return
        row = self.store.db.execute("SELECT data FROM agent_changes WHERE session_id=? AND target=?",
                                    (record["session_id"], record["target"])).fetchone()
        now = time.time()
        if row:
            entry = json.loads(row[0])
            entry.update(updated=now, edits=entry.get("edits", 0) + 1)
        else:
            entry = {"session_id": record["session_id"], "workspace": record["workspace"], "target": record["target"],
                     "path": tools.display_path(Path(record["target"])), "baseline": record["before"],
                     "created": now, "updated": now, "edits": 1}
        self._save(entry)

    def _resolve(self, entry, tools):
        target = tools.path(entry["path"])
        if str(target) != entry["target"]:
            raise ValueError("This path now resolves to a different file.")
        return target

    def _find(self, session_id, tools, path):
        row = self.store.db.execute("SELECT data FROM agent_changes WHERE session_id=? AND target=? AND workspace=?",
                                    (session_id, str(tools.path(path)), str(tools.root))).fetchone()
        if not row:
            raise ValueError("No agent changes to review for this file.")
        entry = json.loads(row[0])
        return entry, self._resolve(entry, tools)

    def _summary(self, entry, tools):
        try:
            current = _snapshot(self._resolve(entry, tools))
        except (ValueError, OSError, UnicodeError) as exc:
            return {"path": entry["path"], "status": "unavailable", "added": 0, "removed": 0,
                    "current_hash": None, "error": file_error(exc)}
        if _same_content(entry["baseline"], current):
            self._drop(entry)  # Edited back to where it started: nothing left to review.
            return None
        added, removed = _counts(_opcodes(_lines(entry["baseline"]), _lines(current)))
        return {"path": entry["path"], "status": _status(entry["baseline"], current),
                "added": added, "removed": removed, "current_hash": current["hash"]}

    def list(self, session_id, tools):
        rows = self.store.db.execute("SELECT data FROM agent_changes WHERE session_id=? AND workspace=?",
                                     (session_id, str(tools.root))).fetchall()
        entries = sorted((json.loads(row[0]) for row in rows), key=lambda entry: entry["created"])
        files = [summary for entry in entries if (summary := self._summary(entry, tools))]
        return {"files": files, "added": sum(item["added"] for item in files),
                "removed": sum(item["removed"] for item in files)}

    def diff(self, session_id, tools, path):
        entry, target = self._find(session_id, tools, path)
        current = _snapshot(target)
        if _same_content(entry["baseline"], current):
            self._drop(entry)
            raise ValueError("No agent changes to review for this file.")
        old, new = _lines(entry["baseline"]), _lines(current)
        lines, hunks = [], []
        for tag, i1, i2, j1, j2 in _opcodes(old, new):
            if tag == "equal":
                lines.extend(_line("context", old[i1 + k], old=i1 + k + 1, new=j1 + k + 1) for k in range(i2 - i1))
                continue
            index = len(hunks)
            hunks.append({"index": index, "line": len(lines), "removed": i2 - i1, "added": j2 - j1})
            lines.extend(_line("removed", old[i], old=i + 1, hunk=index) for i in range(i1, i2))
            lines.extend(_line("added", new[j], new=j + 1, hunk=index) for j in range(j1, j2))
        return {"path": entry["path"], "status": _status(entry["baseline"], current),
                "added": sum(hunk["added"] for hunk in hunks), "removed": sum(hunk["removed"] for hunk in hunks),
                "baseline_hash": entry["baseline"]["hash"], "current_hash": current["hash"],
                "hunks": hunks, "lines": lines}

    def _result(self, action, entry, current, request):
        """The state Keep makes the new baseline, or Undo writes to disk."""
        baseline = entry["baseline"]
        if request.get("hunk") is None or not (baseline["exists"] and current["exists"]):
            # A created or deleted file is one change: the hunk is the whole file.
            return current if action == "keep" else baseline
        if request.get("baseline_hash") != baseline["hash"]:
            raise ValueError(STALE.format(path=entry["path"]))
        old, new = _lines(baseline), _lines(current)
        changed = [op for op in _opcodes(old, new) if op[0] != "equal"]
        if not request["hunk"] < len(changed):
            raise ValueError(STALE.format(path=entry["path"]))
        _, i1, i2, j1, j2 = changed[request["hunk"]]
        if action == "keep":
            return _state("".join(old[:i1] + new[j1:j2] + old[i2:]).encode("utf-8"), baseline["mode"])
        return _state("".join(new[:j1] + old[i1:i2] + new[j2:]).encode("utf-8"), current["mode"])

    def act(self, session_id, tools, action, requests):
        """Keep or undo files (or single changes); every request is checked before anything changes."""
        if action not in ("keep", "undo"):
            raise ValueError("Unknown review action.")
        plans, seen = [], set()
        for request in requests:
            entry, target = self._find(session_id, tools, request["path"])
            if entry["target"] in seen:
                raise ValueError("Each file can be reviewed once per action.")
            seen.add(entry["target"])
            if action == "keep" and request.get("hunk") is None and request.get("current_hash") is None:
                plans.append((entry, target, None, None))  # Stop tracking, even an unreadable file.
                continue
            if request.get("current_hash") is None:
                raise ValueError("Review the file before undoing or keeping a single change.")
            current = _snapshot(target)
            if current["hash"] != request["current_hash"]:
                raise ValueError(STALE.format(path=entry["path"]))
            plans.append((entry, target, current, self._result(action, entry, current, request)))
        for entry, target, current, result in plans:
            if action == "keep":
                if current is None or _same_content(result, current):
                    self._drop(entry)
                else:
                    entry.update(baseline=result, updated=time.time())
                    self._save(entry)
                continue
            if not _same_content(result, current):
                self.checkpoints.apply_state(tools, entry["path"], target, current, result, session_id)
            if _same_content(result, entry["baseline"]):
                self._drop(entry)
        return self.list(session_id, tools)
