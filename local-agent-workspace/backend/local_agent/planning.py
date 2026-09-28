import asyncio
import json
import time
import uuid

from .tool_profiles import child_profile


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


class DelegateManager:
    def __init__(self, agent_manager):
        self.manager = agent_manager
        self.active = {}

    def children(self, parent_id):
        return [session for session in self.manager.store.list() if session.get("parent_session_id") == parent_id]

    def recover(self):
        for summary in self.manager.store.list():
            child = self.manager.store.get(summary["id"])
            progress = child.get("delegation")
            if not progress or not child.get("parent_event_id"):
                continue
            if not progress.get("terminal_reason"):
                progress = {**progress, "status": "interrupted", "terminal_reason": "interrupted"}
                child.update(delegation=progress, terminal_reason="interrupted")
                self.manager.store.save(child)
            parent = self.manager.store.get(child["parent_session_id"])
            event = next((item for item in (parent or {}).get("events", [])
                          if item["id"] == child["parent_event_id"] and item.get("child_session_id") == child["id"]), None)
            if event is not None and not event.get("delegation", {}).get("terminal_reason"):
                event["delegation"] = dict(progress)
                self.manager.store.save(parent)

    async def progress(self, child, status=None, terminal_reason=None):
        previous = child.get("delegation")
        if previous is None or previous.get("terminal_reason"):
            return
        tools = [event for event in child["events"] if event["type"] == "tool"]
        progress = {**previous, "completed_tools": sum(event.get("state") == "completed" for event in tools)}
        if tools:
            progress["last_tool"] = self.manager.settings.redact(str(tools[-1].get("name", "Tool")))[:160]
        if terminal_reason:
            progress.update(terminal_reason=terminal_reason,
                            status={"completed": "completed", "stopped": "cancelled", "interrupted": "interrupted"}.get(terminal_reason, "failed"))
        elif status and status != "idle":
            progress["status"] = status
        if progress == previous:
            return
        child["delegation"] = progress
        if terminal_reason:
            child["terminal_reason"] = terminal_reason
        self.manager.store.save(child)
        parent = self.manager.get(child["parent_session_id"])
        event = next((item for item in (parent or {}).get("events", [])
                      if item["id"] == child["parent_event_id"] and item.get("child_session_id") == child["id"]), None)
        if event is not None and not event.get("delegation", {}).get("terminal_reason"):
            await self.manager.update_event(parent, event, delegation=dict(progress))

    async def cancel_parent(self, parent_id):
        entry = self.active.get(parent_id)
        if entry:
            entry["cancelled"] = True
            try:
                await self.manager.stop(entry["child_session_id"])
            finally:
                if self.active.get(parent_id) is entry:
                    self.active.pop(parent_id)

    async def delegate(self, parent_session, task, context="", max_steps=6, parent_event=None, tool_profile=None):
        parent = self.manager.get(parent_session["id"])
        if not parent:
            raise ValueError("Parent conversation not found.")
        if parent.get("is_subagent"):
            raise ValueError("Subagents cannot delegate further tasks.")
        if parent.get("runtime", "databricks") != "databricks":
            raise ValueError("Delegation requires a Databricks conversation.")
        if not isinstance(task, str) or not task.strip() or not isinstance(context, str):
            raise ValueError("A nonempty task and string context are required.")
        if type(max_steps) is not int or not 1 <= max_steps <= 8:
            raise ValueError("max_steps must be an integer from 1 to 8.")
        profile = child_profile(parent, tool_profile)
        if parent["id"] in self.active:
            raise ValueError("A subagent is already running for this conversation.")
        if len(self.active) >= 3:
            raise ValueError("At most three subagents can run at once.")
        if parent_event is not None:
            parent_event = next((event for event in parent["events"] if event["id"] == parent_event["id"]), None)
            if parent_event is None:
                raise ValueError("The spawning action is not in the parent conversation.")
        child = self.manager.store.create({"workspace": parent["workspace"], "model": parent["model"]})
        child.update(parent_session_id=parent["id"], is_subagent=True, max_steps=max_steps,
                     tool_profile=profile,
                     permission_mode=parent.get("permission_mode", "manual"),
                     allowed_directories=list(parent.get("allowed_directories", [])))
        event_id = parent_event["id"] if parent_event is not None else str(uuid.uuid4())
        child.update(parent_event_id=event_id, parent_call_id=(parent_event or {}).get("call_id"),
                     delegation={"status": "running", "completed_tools": 0})
        self.manager.store.save(child)
        entry = {"child_session_id": child["id"], "cancelled": False}
        self.active[parent["id"]] = entry
        failure = ""
        worker = None
        try:
            if parent_event is None:
                await self.manager.event(parent, "notice", id=event_id, text=f"Subagent created for: {task[:160]}",
                                         child_session_id=child["id"], delegation=dict(child["delegation"]))
            else:
                await self.manager.update_event(parent, parent_event, child_session_id=child["id"], delegation=dict(child["delegation"]))
            prompt = f"Delegated task:\n{task}"
            if context:
                prompt += f"\n\nParent-provided context:\n{context}"
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
            if self.active.get(parent["id"]) is entry:
                self.active.pop(parent["id"])
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
        result = {"child_session_id": child["id"], "status": status, "terminal_reason": reason, "output": output}
        if len(json.dumps(result, indent=2).encode("utf-8")) > 6000:
            low, high = 0, len(output)
            while low < high:
                middle = (low + high + 1) // 2
                result["output"] = output[:middle] + "\n[truncated]"
                if len(json.dumps(result, indent=2).encode("utf-8")) <= 6000:
                    low = middle
                else:
                    high = middle - 1
            result["output"] = output[:low] + "\n[truncated]"
        return result
