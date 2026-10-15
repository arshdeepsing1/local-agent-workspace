import asyncio
import json
import time
import uuid

from .spending import own_spend
from .tool_profiles import PROFILES, child_profile, validate_profile


class TaskManager:
    def __init__(self, store):
        self.store = store
        store.db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, created REAL, data TEXT)")
        store.db.execute("CREATE INDEX IF NOT EXISTS tasks_session ON tasks(session_id, created)")
        store.db.commit()

    def list(self, session_id):
        if not self.store.get(session_id):
            raise ValueError("Conversation not found.")
        rows = self.store.db.execute("SELECT data FROM tasks WHERE session_id=? ORDER BY created, id", (session_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, session_id, task_id):
        """One task of this conversation; another conversation's task is not found."""
        task = next((item for item in self.list(session_id) if item["id"] == task_id), None)
        if not task:
            raise ValueError("Task not found in this conversation.")
        return task

    def _validate(self, task, tasks):
        if not isinstance(task["title"], str) or not task["title"].strip() or len(task["title"]) > 200:
            raise ValueError("Task title must contain 1 to 200 characters.")
        if not isinstance(task["description"], str) or len(task["description"]) > 4000:
            raise ValueError("Task description must be a string of at most 4000 characters.")
        if task["status"] not in ("pending", "in_progress", "completed", "cancelled"):
            raise ValueError("Unknown task status.")
        dependencies = task["depends_on"]
        if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
            raise ValueError("depends_on must be a list of task IDs.")
        if len(set(dependencies)) != len(dependencies):
            raise ValueError("Task dependencies must be unique.")
        by_id = {item["id"]: item for item in tasks}
        if any(item not in by_id for item in dependencies):
            raise ValueError("Every dependency must be a task in this conversation.")
        by_id[task["id"]] = task
        visiting, visited = set(), set()

        def visit(task_id):
            if task_id in visiting:
                raise ValueError("Task dependencies cannot contain a cycle.")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in by_id[task_id]["depends_on"]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in by_id:
            visit(task_id)
        if task["status"] in ("in_progress", "completed") and any(by_id[item]["status"] != "completed" for item in dependencies):
            raise ValueError("Complete all dependencies before starting or completing this task.")
        previous = next((item for item in tasks if item["id"] == task["id"]), None)
        if previous and previous["status"] == "completed" and task["status"] != "completed":
            if any(task["id"] in item["depends_on"] and item["status"] in ("in_progress", "completed") for item in tasks):
                raise ValueError("A completed dependency cannot be reset while a dependent task is active or completed.")

    def _save(self, session_id, task):
        self.store.db.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?, ?, ?)",
                              (task["id"], session_id, task["created"], json.dumps(task)))
        self.store.db.commit()
        return task

    def create(self, session_id, title, description="", depends_on=None):
        tasks = self.list(session_id)
        if len(tasks) >= 50:
            raise ValueError("A conversation can contain at most 50 tasks.")
        now = time.time()
        task = {"id": str(uuid.uuid4()), "title": title, "description": description,
                "status": "pending", "depends_on": [] if depends_on is None else depends_on,
                "created": now, "updated": now}
        self._validate(task, tasks)
        return self._save(session_id, task)

    def update(self, session_id, task_id, fields):
        tasks = self.list(session_id)
        task = next((item for item in tasks if item["id"] == task_id), None)
        if not task:
            raise ValueError("Task not found in this conversation.")
        if not isinstance(fields, dict) or fields.keys() - {"title", "description", "status", "depends_on"}:
            raise ValueError("Only title, description, status, and depends_on can be updated.")
        updated = {**task, **fields, "updated": time.time()}
        self._validate(updated, tasks)
        return self._save(session_id, updated)

    def delete_session(self, session_id):
        self.store.db.execute("DELETE FROM tasks WHERE session_id=?", (session_id,))
        self.store.db.commit()


# Delegation (delegate_task): each subagent is a conversation of its own (its own JSONL file and
# context); the parent's model gets back only a bounded report.
AGENT_TYPES = {"explore": "read_only", "general": "inherit", "editor": "file_editor"}
PROFILE_TYPES = {profile: kind for kind, profile in AGENT_TYPES.items()}
MAX_RUNNING = 3  # Subagents running at once over all chats; model_profiles.CONCURRENT_REQUESTS is a chat plus three.
MAX_STEPS = 24
DEFAULT_STEPS = 10
RESULT_BYTES = 6000  # The delegate_task result as indented JSON; the report (output) is shortened first.
INDEX_ENTRIES = 200  # A parent's subagents index keeps its newest entries: the header is read at startup.
TASK_CHARS = 200
CHANGED_FILES = 20
PATH_CHARS = 240
CHANGE_TOOLS = ("write_file", "edit_file", "move_path", "delete_path", "notebook_edit", "insert_activity_log")
TERMINAL_STATUS = {"completed": "completed", "stopped": "cancelled", "interrupted": "interrupted"}
# The subagent's first prompt: what its tools allow (by its effective profile) and the report it ends with.
BRIEFS = {
    "read_only": ("You are an explore subagent: investigate with read-only tools (find, search, outline, read); "
                  "you cannot change files or run commands."),
    "file_editor": "You are an editor subagent: read and edit files (no commands); change only what the task asks for.",
    "inherit": "You are a general subagent with the parent chat's tools; change only what the task asks for.",
}
REPORT = ("Work on your own: the parent sees only your final reply, never this conversation. End with a concise "
          "report: summary; key findings with path:line references; files changed (or none); open questions or risks.")


def transcript_name(session_id):
    """A conversation's JSONL file relative to the state folder (store.Store keeps one per chat)."""
    return f"conversations/{session_id}.jsonl"


def requested_profile(agent_type=None, tool_profile=None):
    """The tool profile a delegate_task call asks for: its agent type's, narrowed by tool_profile
    (both may be given; the narrower wins). None asks for the parent's ceiling."""
    if agent_type is not None and agent_type not in AGENT_TYPES:
        raise ValueError("agent_type must be explore, general, or editor.")
    asked = [value for value in (AGENT_TYPES.get(agent_type),
                                 None if tool_profile is None else validate_profile(tool_profile)) if value]
    return min(asked, key=PROFILES.index) if asked else None


def changed_paths(events):
    """The paths a subagent's completed file tools changed, in first-change order."""
    paths = []
    for event in events:
        if event.get("type") != "tool" or event.get("name") not in CHANGE_TOOLS or event.get("state") != "completed":
            continue
        values = event.get("input") if isinstance(event.get("input"), dict) else {}
        for key in ("path", "destination"):
            if isinstance(values.get(key), str) and values[key] and values[key] not in paths:
                paths.append(values[key])
    return paths


def index_entry(child, task):
    """The child's entry in its parent's subagents index (the parent's header)."""
    progress = child.get("delegation") if isinstance(child.get("delegation"), dict) else {}
    return {"id": child["id"], "task": " ".join(str(task).split())[:TASK_CHARS],
            "agent_type": child.get("agent_type") or PROFILE_TYPES.get(child.get("tool_profile"), "general"),
            "status": progress.get("status", "failed") if progress.get("terminal_reason") else "running",
            "created": progress.get("started", child.get("created")), "ended": progress.get("ended"),
            "transcript": transcript_name(child["id"])}


def subagent_index(sessions):
    """{parent id: index} rebuilt from conversations copied together (an import), oldest child first."""
    by_id = {session["id"]: session for session in sessions}
    index = {}
    def started(child):
        # Copies all get the import time as created: order by when the delegation started.
        progress = child.get("delegation") if isinstance(child.get("delegation"), dict) else {}
        source = (child.get("provenance") or {}).get("source_created") if isinstance(child.get("provenance"), dict) else None
        value = progress.get("started", source if source is not None else child.get("created", 0))
        return value if isinstance(value, (int, float)) else 0

    for child in sorted(sessions, key=started):
        parent = by_id.get(child.get("parent_session_id"))
        if parent is None:
            continue
        spawn = next((event for event in parent.get("events", []) if event.get("child_session_id") == child["id"]), {})
        task = (spawn.get("input") or {}).get("task") if isinstance(spawn.get("input"), dict) else None
        if not isinstance(task, str):
            task = next((event.get("text", "") for event in child.get("events", []) if event.get("type") == "user"), "")
            task = task.removeprefix("Delegated task:\n")
            for brief in BRIEFS.values():
                task = task.removesuffix(f"\n\n{brief} {REPORT}")
            task = task.split("\n\nParent-provided context:", 1)[0]
        index.setdefault(parent["id"], []).append(index_entry(child, task))
    return {key: value[-INDEX_ENTRIES:] for key, value in index.items()}


def bounded_result(result, limit=RESULT_BYTES):
    """result within limit bytes as indented JSON: the report is shortened first, then the file list."""
    def size(value):
        return len(json.dumps(value, indent=2).encode("utf-8"))

    if size(result) <= limit:
        return result
    output, result = result["output"], dict(result)
    result["output"] = "\n[truncated]"
    while size(result) > limit and result["changed_files"]:
        result.setdefault("changed_files_total", len(result["changed_files"]))
        result["changed_files"] = result["changed_files"][:-1]
    low, high = 0, len(output)
    while low < high:
        middle = (low + high + 1) // 2
        if size({**result, "output": output[:middle] + "\n[truncated]"}) <= limit:
            low = middle
        else:
            high = middle - 1
    result["output"] = output[:low] + "\n[truncated]"
    return result


class DelegationGate:
    """One delegate_task call of a batch that runs in parallel (AgentManager.run_delegations).
    execute_tool prepares the call (checks, approval card, hooks) and then waits here: ready is
    done once it would start. The batch sets go when every call is prepared, so they all start
    together. A finished delegation then takes the batch's lock, which finished() releases once
    that call's execute_tool has returned: results are finished (charged, cards updated, hooks
    run) one at a time, in the order the subagents end."""

    def __init__(self, lock):
        self.ready = asyncio.get_running_loop().create_future()
        self.go, self.lock, self.holding = asyncio.Event(), lock, False

    async def run(self, start):
        if not self.ready.done():
            self.ready.set_result(None)
        await self.go.wait()
        try:
            result, error = await start(), None
        except Exception as exc:  # Finished as a tool error, after the lock like a result.
            result, error = None, exc
        await self.lock.acquire()
        self.holding = True
        if error is not None:
            raise error
        return result

    def finished(self, _task=None):
        """The call's execute_tool returned or was cancelled: the next result may be finished."""
        if self.holding:
            self.holding = False
            self.lock.release()


class CallAllowance:
    """One call's view of a response's ResultAllowance: it remembers whether that call's result
    was charged, which a shared settled counter cannot tell while calls run in parallel."""

    def __init__(self, allowance):
        self.allowance, self.settled = allowance, False

    @property
    def total(self):
        return self.allowance.total

    def cost(self, text):
        return self.allowance.cost(text)

    def admit(self, text):
        admitted = self.allowance.admit(text)
        self.settled = self.settled or admitted
        return admitted

    def charge(self, text):
        self.allowance.charge(text)
        self.settled = True


class DelegateManager:
    def __init__(self, agent_manager):
        self.manager = agent_manager
        # {parent id: [entry]}: one entry per delegation of that chat, from its start (waiting for a
        # slot while entry["waiting"]) to its end. entry: {child_session_id, cancelled, waiting}.
        self.active = {}
        self.waiters = []  # Futures of delegations waiting for a free slot.

    def children(self, parent_id):
        return [session for session in self.manager.store.list() if session.get("parent_session_id") == parent_id]

    def running(self):
        return sum(not entry.get("waiting") for entries in self.active.values() for entry in entries)

    def active_children(self, parent_id):
        """The parent's running subagent chats (live dicts)."""
        children = []
        for entry in self.active.get(parent_id, ()):
            child = self.manager.get(entry["child_session_id"]) if entry.get("child_session_id") else None
            if child is not None:
                children.append(child)
        return children

    def _release(self, parent_id, entry):
        entries = [item for item in self.active.get(parent_id, ()) if item is not entry]
        if entries:
            self.active[parent_id] = entries
        else:
            self.active.pop(parent_id, None)
        self._wake()

    def _wake(self):
        for waiter in self.waiters:
            if not waiter.done():
                waiter.set_result(None)

    def _readable(self, session_id):
        try:
            return self.manager.store.get(session_id)
        except (ValueError, OSError):
            return None  # Reported by the store; recovery skips it.

    @staticmethod
    def _record(parent, child, task=None, **extra):
        """Add (task given) or update the child's entry in the parent's subagents index, unsaved.
        True when it changed. Children started before the index existed get no entry."""
        index = parent.get("subagents") if isinstance(parent.get("subagents"), list) else []
        current = next((item for item in index if isinstance(item, dict) and item.get("id") == child["id"]), None)
        if current is None and task is None:
            return False
        if current is not None and current.get("deleted"):
            return False  # Deleted while it ran: the entry keeps no transcript.
        updated = {**(current or {}), **index_entry(child, current.get("task", "") if task is None else task), **extra}
        if updated == current:
            return False
        parent["subagents"] = ([*index, updated][-INDEX_ENTRIES:] if current is None
                               else [updated if item is current else item for item in index])
        return True

    def forget(self, child_id, keep=()):
        """A subagent chat is being deleted: its parent's index entry stays, marked deleted and
        without a transcript, unless the parent is deleted too (keep: the IDs being deleted)."""
        try:
            child = self.manager.live.get(child_id) or self.manager.store.header(child_id)
            parent_id = (child or {}).get("parent_session_id")
            parent = self.manager.get(parent_id) if isinstance(parent_id, str) and parent_id not in keep else None
        except (ValueError, OSError):
            return  # An unreadable parent keeps its index as it is.
        index = (parent or {}).get("subagents")
        if not isinstance(index, list):
            return
        marked = [{**item, "deleted": True, "transcript": None} if isinstance(item, dict) and item.get("id") == child_id
                  else item for item in index]
        if marked != index:
            parent["subagents"] = marked
            self.manager.store.save(parent, touch=False)

    def recover(self, summaries=None, full_scan=True, recovered=()):
        """Close delegations a restart cut short. After a clean shutdown (full_scan False) only
        children whose delegation is still open or whose parent was just recovered are loaded."""
        for summary in self.manager.store.list() if summaries is None else summaries:
            header = summary.get("delegation")
            if not isinstance(header, dict) or not summary.get("parent_event_id"):
                continue
            if not (full_scan or not header.get("terminal_reason") or summary.get("parent_session_id") in recovered):
                continue
            child = self._readable(summary["id"])
            progress = (child or {}).get("delegation")
            if not progress or not child.get("parent_event_id"):
                continue
            if not progress.get("terminal_reason"):
                progress = {**progress, "status": "interrupted", "terminal_reason": "interrupted"}
                child.update(delegation=progress, terminal_reason="interrupted")
                self.manager.store.save(child)
            parent = self._readable(child["parent_session_id"])
            if parent is None:
                continue
            changed = self._record(parent, child)
            event = next((item for item in parent.get("events", [])
                          if item["id"] == child["parent_event_id"] and item.get("child_session_id") == child["id"]), None)
            if event is not None and not event.get("delegation", {}).get("terminal_reason"):
                event["delegation"] = dict(progress)
                changed = True
            if changed:
                self.manager.store.save(parent)

    async def progress(self, child, status=None, terminal_reason=None):
        if not child.get("parent_session_id") or not child.get("parent_event_id"):
            return  # A child whose parent was deleted (its links were cleared).
        previous = child.get("delegation")
        if previous is None or previous.get("terminal_reason"):
            return
        tools = [event for event in child["events"] if event["type"] == "tool"]
        progress = {**previous, "completed_tools": sum(event.get("state") == "completed" for event in tools)}
        if tools:
            progress["last_tool"] = self.manager.settings.redact(str(tools[-1].get("name", "Tool")))[:160]
        if terminal_reason:
            progress.update(terminal_reason=terminal_reason, status=TERMINAL_STATUS.get(terminal_reason, "failed"),
                            ended=time.time())
        elif status and status != "idle":
            progress["status"] = status
        if progress == previous:
            return
        child["delegation"] = progress
        if terminal_reason:
            child["terminal_reason"] = terminal_reason
        self.manager.store.save(child)
        parent = self.manager.get(child["parent_session_id"])
        if not parent:
            return
        recorded = bool(terminal_reason) and self._record(parent, child)
        event = next((item for item in parent.get("events", [])
                      if item["id"] == child["parent_event_id"] and item.get("child_session_id") == child["id"]), None)
        if event is not None and not event.get("delegation", {}).get("terminal_reason"):
            await self.manager.update_event(parent, event, delegation=dict(progress))
        elif recorded:
            self.manager.store.save(parent)

    async def cancel_parent(self, parent_id):
        """Stop every subagent of this chat, also those still waiting for a slot."""
        entries = list(self.active.get(parent_id, ()))
        for entry in entries:
            entry["cancelled"] = True
        self._wake()  # A waiting delegation sees its cancel and ends without starting.
        try:
            for entry in entries:
                if entry.get("child_session_id"):
                    await self.manager.stop(entry["child_session_id"])
        finally:
            for entry in entries:
                self._release(parent_id, entry)

    async def _slot(self, entry, parent, parent_event, kind):
        """Wait until fewer than MAX_RUNNING subagents run (over all chats); the card says queued."""
        noticed = parent_event is None
        while not entry["cancelled"] and self.running() >= MAX_RUNNING:
            entry["queued"] = True
            if not noticed:
                noticed = True
                await self.manager.update_event(parent, parent_event, delegation={
                    "status": "queued", "completed_tools": 0, "agent_type": kind})
                continue  # Check again: a slot may have been freed during the update.
            waiter = asyncio.get_running_loop().create_future()
            self.waiters.append(waiter)
            try:
                await waiter
            finally:
                if waiter in self.waiters:
                    self.waiters.remove(waiter)
        entry["waiting"] = False

    async def _unqueue(self, parent, parent_event):
        """A delegation that ended while it waited for a slot: its card says it was cancelled."""
        progress = (parent_event or {}).get("delegation") or {}
        if progress.get("status") == "queued":
            await self.manager.update_event(parent, parent_event, delegation={
                **progress, "status": "cancelled", "terminal_reason": "stopped", "ended": time.time()})

    async def delegate(self, parent_session, task, context="", max_steps=None, parent_event=None, tool_profile=None,
                       agent_type=None, recheck=None):
        """recheck() -> refusal text or None: after a wait for a free slot, whether the chat still allows
        this delegation (its tools, rules or mode may have changed while it waited)."""
        parent = self.manager.get(parent_session["id"])
        if not parent:
            raise ValueError("Parent conversation not found.")
        if parent.get("is_subagent"):
            raise ValueError("Subagents cannot delegate further tasks.")
        if parent.get("runtime", "databricks") != "databricks":
            raise ValueError("Delegation requires a Databricks conversation.")
        if not isinstance(task, str) or not task.strip() or not isinstance(context, str):
            raise ValueError("A nonempty task and string context are required.")
        max_steps = DEFAULT_STEPS if max_steps is None else max_steps
        if type(max_steps) is not int or not 1 <= max_steps <= MAX_STEPS:
            raise ValueError(f"max_steps must be an integer from 1 to {MAX_STEPS}.")
        profile = child_profile(parent, requested_profile(agent_type, tool_profile))
        kind = agent_type or PROFILE_TYPES.get(tool_profile, "general")
        if parent_event is not None:
            parent_event = next((event for event in parent["events"] if event["id"] == parent_event["id"]), None)
            if parent_event is None:
                raise ValueError("The spawning action is not in the parent conversation.")
        entry = {"child_session_id": None, "cancelled": False, "waiting": True}
        self.active.setdefault(parent["id"], []).append(entry)
        try:
            await self._slot(entry, parent, parent_event, kind)
        except BaseException:
            self._release(parent["id"], entry)
            await self._unqueue(parent, parent_event)
            raise
        if entry["cancelled"]:  # Its parent was stopped while it waited.
            self._release(parent["id"], entry)
            await self._unqueue(parent, parent_event)
            return bounded_result({"child_session_id": None, "agent_type": kind, "tool_profile": profile,
                                   "status": "cancelled", "terminal_reason": "stopped", "steps_used": 0, "tool_calls": 0,
                                   "changed_files": [], "transcript": None,
                                   "output": "The subagent was cancelled before it started."})
        refusal = recheck() if recheck is not None and entry.get("queued") else None
        if refusal:
            self._release(parent["id"], entry)
            await self._unqueue(parent, parent_event)
            raise ValueError(refusal)
        try:
            child = self.manager.store.create({"workspace": parent["workspace"], "model": parent["model"]})
            child.update(parent_session_id=parent["id"], is_subagent=True, max_steps=max_steps,
                         tool_profile=profile, agent_type=kind,
                         permission_mode=parent.get("permission_mode", "manual"),
                         allowed_directories=list(parent.get("allowed_directories", [])),
                         allow_rules=[dict(rule) for rule in parent.get("allow_rules", [])],
                         disabled_tools=list(parent.get("disabled_tools", [])))
            if parent.get("reasoning_effort") is not None:
                child["reasoning_effort"] = parent["reasoning_effort"]  # The chat's model settings apply to its subagents.
            if isinstance(parent.get("worktree"), dict):
                # Display metadata only: the subagent of a worktree chat is listed under the same project.
                child["worktree"] = dict(parent["worktree"])
            event_id = parent_event["id"] if parent_event is not None else str(uuid.uuid4())
            child.update(parent_event_id=event_id, parent_call_id=(parent_event or {}).get("call_id"),
                         delegation={"status": "running", "completed_tools": 0, "agent_type": kind, "started": time.time()})
            self.manager.store.save(child)
        except BaseException:
            # A failed write (a full disk) must not keep the slot: other delegations would wait forever.
            self._release(parent["id"], entry)
            await self._unqueue(parent, parent_event)
            raise
        entry["child_session_id"] = child["id"]
        failure = ""
        worker = None
        try:
            self._record(parent, child, task=task)  # Saved with the spawning event below.
            if parent_event is None:
                await self.manager.event(parent, "notice", id=event_id, text=f"Subagent created for: {task[:160]}",
                                         child_session_id=child["id"], delegation=dict(child["delegation"]))
            else:
                await self.manager.update_event(parent, parent_event, child_session_id=child["id"], delegation=dict(child["delegation"]))
            prompt = f"Delegated task:\n{task}"
            if context:
                prompt += f"\n\nParent-provided context:\n{context}"
            prompt += f"\n\n{BRIEFS[profile]} {REPORT}"
            self.manager.start(child["id"], prompt)
            worker = self.manager.tasks[child["id"]]
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                entry["cancelled"] = True
                await self.manager.stop(child["id"])
        except asyncio.CancelledError:
            entry["cancelled"] = True
            await self.manager.stop(child["id"])
            await self.progress(self.manager.get(child["id"]) or child, terminal_reason="stopped")
            raise
        except Exception as exc:
            failure = self.manager.settings.redact(str(exc)) or type(exc).__name__
            await self.progress(self.manager.get(child["id"]) or child, terminal_reason="error")
        finally:
            self._release(parent["id"], entry)
        saved = self.manager.get(child["id"]) or child
        events = saved.get("events", [])
        errors = [event.get("text") or event.get("output") or "Child tool failed."
                  for event in events if event.get("type") == "error" or event.get("state") == "error"]
        if failure:
            errors.append(failure)
        cancelled = entry["cancelled"] or (worker is not None and (worker.cancelled() or worker.cancelling()))
        cancelled = cancelled or any(event.get("state") == "cancelled" for event in events)
        reason = "stopped" if cancelled else saved.get("terminal_reason") or ("error" if errors else "completed")
        status = "cancelled" if cancelled or reason == "stopped" else "failed" if errors or reason != "completed" else "completed"
        await self.progress(saved, terminal_reason=reason)
        reply = next((event.get("text", "") for event in reversed(events) if event.get("type") == "assistant" and event.get("text")), "")
        output = ("Errors:\n" + "\n".join(errors) + "\n\n" if errors else "") + (reply or "No final assistant reply was produced.")
        paths = [self.manager.settings.redact(path)[:PATH_CHARS] for path in changed_paths(events)]
        steps = sum(event.get("type") == "assistant" for event in events)
        calls = sum(event.get("type") == "tool" for event in events)
        result = {"child_session_id": child["id"], "agent_type": kind, "tool_profile": profile, "status": status,
                  "terminal_reason": reason, "steps_used": steps, "tool_calls": calls,
                  "changed_files": paths[:CHANGED_FILES], "transcript": transcript_name(child["id"]), "output": output}
        if len(paths) > CHANGED_FILES:
            result["changed_files_total"] = len(paths)
        current = self.manager.get(parent["id"])
        spent, unpriced = own_spend(saved)  # Counted for the parent's spending limit (spending.py).
        if current and self._record(current, saved, steps_used=steps, tool_calls=calls, estimated_dbu=spent,
                                    unpriced_calls=unpriced):
            self.manager.store.save(current)
        return bounded_result(result)
