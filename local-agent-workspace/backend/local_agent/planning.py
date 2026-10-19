import asyncio
import json
import logging
import re
import time
import uuid

from . import custom_agents
from .permissions import MODE_LABELS, denied_tools
from .reasoning import EFFORT_LABELS
from .redaction import redact_secrets
from .spending import own_spend
from .telemetry import sum_usage, usage_counts
from .tool_profiles import PROFILES, child_profile, covering_tool, server_off, validate_profile

log = logging.getLogger(__name__)


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
FINDINGS_CHARS = 3000  # The child's report_findings, listed in the delegate_task report before its reply.
CHANGE_TOOLS = ("write_file", "edit_file", "move_path", "delete_path", "notebook_edit", "insert_activity_log")
TERMINAL_STATUS = {"completed": "completed", "stopped": "cancelled", "interrupted": "interrupted"}
# The subagent's first prompt: what its tools allow (by its effective profile) and the report it ends with.
BRIEFS = {
    "read_only": ("You are an explore subagent: investigate with read-only tools (find, search, outline, read); "
                  "you cannot change files or run commands."),
    "file_editor": "You are an editor subagent: read and edit files (no commands); change only what the task asks for.",
    "inherit": "You are a general subagent with the parent chat's tools; change only what the task asks for.",
}
REPORT = ("Work on your own: the parent sees only your report, never this conversation. End with submit_report: a "
          "concise summary; key findings with path and line; files changed (or none); next steps, open questions or risks.")
# The first prompts of subagents started before submit_report ended with this (subagent_index reads both).
LEGACY_REPORT = ("Work on your own: the parent sees only your final reply, never this conversation. End with a concise "
                 "report: summary; key findings with path:line references; files changed (or none); open questions or risks.")
REPORT_KEYS = ("summary", "findings", "files_changed", "next_steps")
REPORT_SUBMITTED = "Report sent to the parent chat. Your turn ends here."
# A report of the subagent's last response that did not go through (unaccepted_report): how the parent's result
# labels its text. The arguments of an unusable reply's call are kept up to UNSENT_REPORT_CHARS (the result holds
# no more).
REFUSED_REPORT = "Its last report, which the submit_report schema refused (see the error), as the model wrote it:\n"
UNSENT_REPORT = "Its last report, whose submit_report arguments could not be read, as the model wrote them:\n"
UNSENT_REPORT_CHARS = RESULT_BYTES
# The model history's reply after submit_report ended a subagent's turn (a turn ends with a reply).
REPORT_SUBMITTED_REPLY = "[Report submitted to the parent chat with submit_report.]"
INITIAL_PROMPT_LABEL = "Agent's initial prompt:"
# A background delegation (delegate_task background): what the tool returns at once, how its end is told.
BACKGROUND_STARTED = ("Started in the background; this chat continues. A notice names it when it ends; read its report "
                      "with get_subagent_result.")
BACKGROUND_QUEUED = ("Queued in the background: it starts when one of the three subagent slots frees; this chat continues. "
                     "get_subagent_result without subagent_id lists this chat's background subagents.")
BACKGROUND_ENDINGS = {"completed": "finished", "failed": "failed", "cancelled": "was stopped", "interrupted": "was interrupted"}
# A scheduled run's delegate_task asking for the background (runs_in_background): the card and its result say so.
SCHEDULED_FOREGROUND = "background is ignored in a scheduled run, which waits for its subagents so its limits cover them."
NOTICE_ENTRIES = 5
LIST_ENTRIES = 20
# How a turn ended when the chat may go on by itself (queued messages, the wake below); a limit, an error or a
# stop waits for the user's Continue.
CONTINUE_REASONS = ("completed", "tool_error")
WAKE_TEXT = ("[Sent by the app: the background subagent {id} ({kind}) {ending}. Read its report with get_subagent_result "
             "and continue the task.]")
PENDING_TITLE = 200
# What a waiting card of a subagent warns about (its own card shows it); the parent's card names it and
# offers no Allow, so the user reviews the subagent's card.
PENDING_WARNINGS = (("secret_files", "names credential-looking files"), ("protected", "changes a protected file"),
                    ("sibling_change", "another subagent changed this file"), ("hook_ask", "a hook asked for it"),
                    ("hook_change", "a hook changed what the agent proposed"), ("uncertain", "a rule may cover it"))
# A custom agent's first prompt (custom_agents.py): its instructions, then what its tools allow.
AGENT_HEADER = "Custom agent: "
CUSTOM_BRIEFS = {
    "read_only": "Your tools are read-only (find, search, outline, read): you cannot change files or run commands.",
    "file_editor": "You can read and edit files (no commands); change only what the task asks for.",
    "inherit": "You have the parent chat's tools; change only what the task asks for.",
}


def transcript_name(session_id):
    """A conversation's JSONL file relative to the state folder (store.Store keeps one per chat)."""
    return f"conversations/{session_id}.jsonl"


def requested_profile(agent_type=None, tool_profile=None):
    """The tool profile a delegate_task call asks for: its built-in agent type's, narrowed by
    tool_profile (both may be given; the narrower wins). None asks for the parent's ceiling."""
    if agent_type is not None and agent_type not in AGENT_TYPES:
        raise ValueError("agent_type must be explore, general, editor, or a custom agent's name.")
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
    entry = {"id": child["id"], "task": " ".join(str(task).split())[:TASK_CHARS],
             "agent_type": child.get("agent_type") or PROFILE_TYPES.get(child.get("tool_profile"), "general"),
             "status": progress.get("status", "failed") if progress.get("terminal_reason") else "running",
             "created": progress.get("started", child.get("created")), "ended": progress.get("ended"),
             "transcript": transcript_name(child["id"])}
    # The model and effort it started with (children from before custom agents have none recorded).
    entry.update({key: progress[key] for key in ("model", "effort") if isinstance(progress.get(key), str)})
    if progress.get("background") is True:
        entry["background"] = True  # delegate_task background: the parent went on while it worked.
    if isinstance(child.get("inference_calls"), list):
        from .turns import turn_spend  # turns.py imports this module.
        # What it spent so far, for its parent's spending limit (spending.py) and Usage (usage_rollup), and what of
        # it counts in its parent's turns (turns.py: not its /btw side questions, nor a turn you continue its chat
        # in, as _record keeps it from the end of its delegated run); a header has no ledger.
        entry["estimated_dbu"], entry["unpriced_calls"] = own_spend(child)
        entry["usage"] = usage_counts(child["inference_calls"])
        entry["turn_spend"] = turn_spend(child["inference_calls"])
    return entry


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
                for report in (REPORT, LEGACY_REPORT):
                    task = task.removesuffix(f"\n\n{brief} {report}")
            task = re.split(r"\n\n(?:Parent-provided context:|" + re.escape(AGENT_HEADER) + ")", task, maxsplit=1)[0]
        index.setdefault(parent["id"], []).append(index_entry(child, task))
    return {key: value[-INDEX_ENTRIES:] for key, value in index.items()}


def reported_findings(events, limit=FINDINGS_CHARS):
    """The findings a subagent recorded with report_findings (redacted when recorded), one line each, for the
    parent's model: the child's card is not part of its reply, and the parent sees only the report."""
    findings = [item for event in events if event.get("type") == "tool" and event.get("name") == "report_findings"
                and event.get("state") == "completed" and isinstance(event.get("input"), dict)
                for item in event["input"].get("findings") or [] if isinstance(item, dict)]
    if not findings:
        return ""
    lines = []
    for item in findings:
        line, end = item.get("line"), item.get("end_line")
        where = str(item.get("path") or "")[:PATH_CHARS] + (
            f":{line}" + (f"-{end}" if type(end) is int and end > line else "") if type(line) is int else "")
        detail = " ".join(str(item.get("detail") or "").split())
        detail = detail if len(detail) <= 160 else detail[:159] + "…"
        lines.append(f"- {item.get('severity') or 'info'} {where}: " + " ".join(str(item.get("title") or "").split())
                     + (f" ({detail})" if detail else ""))
    shown, size = [], 0
    for line in lines:
        if size + len(line) + 1 > limit:
            break
        shown.append(line)
        size += len(line) + 1
    rest = len(lines) - len(shown)
    return (f"Findings the subagent reported ({len(findings)}):\n" + "\n".join(shown)
            + (f"\n- and {rest} more in its transcript" if rest else "") + "\n\n")


def submitted_report(events):
    """The structured report of a subagent's newest completed submit_report call (its arguments, which the
    tool's schema checked), or None: then its final reply is the report."""
    for event in reversed(events):
        if (event.get("type") == "tool" and event.get("name") == "submit_report" and event.get("state") == "completed"
                and isinstance(event.get("input"), dict) and isinstance(event["input"].get("summary"), str)):
            return {key: event["input"][key] for key in REPORT_KEYS if key in event["input"]}
    return None


def unaccepted_report(events):
    """The report a subagent's last response sent that did not go through, as labelled text, or None: a
    submit_report call the schema refused or one in a reply that could not be used (kept on the reply as
    unsent_report), its arguments as the model wrote them either way. The wrap-up at a turn's limit sends no request
    to fix it, so the parent gets it this way."""
    start = next((index for index in range(len(events) - 1, -1, -1) if events[index].get("type") == "assistant"), None)
    if start is None:
        return None
    for event in reversed(events[start + 1:]):
        values = event.get("input")
        if (event.get("type") == "tool" and event.get("name") == "submit_report" and event.get("state") == "error"
                and isinstance(values, dict)):
            # As written, not as report_text: the part the schema refused (a summary that is not text, a key it does
            # not take) is the part report_text would leave out, and often holds what remains.
            return REFUSED_REPORT + json.dumps(values, ensure_ascii=False)
    unsent = events[start].get("unsent_report")
    return UNSENT_REPORT + unsent if isinstance(unsent, str) and unsent else None


def retried_reports(events):
    """The IDs of submit_report calls that failed (the schema refused a report) before a later one was
    accepted: the retry the tool invites, not a failure of the subagent's run."""
    accepted, retried = False, set()
    for event in reversed(events):
        if event.get("type") == "tool" and event.get("name") == "submit_report":
            if event.get("state") == "completed":
                accepted = True
            elif accepted and event.get("state") == "error":
                retried.add(event.get("id"))
    return retried


def redacted_report(report, redact):
    """A submit_report report with every text redacted (the model may have copied a secret into it)."""
    def clean(value):
        if isinstance(value, str):
            return redact(redact_secrets(value))
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        return value
    return clean(report)


def report_text(report):
    """A structured report as plain text (hooks, notices and the parent's prose fallback read it)."""
    lines = [f"Summary: {report.get('summary', '')}"]
    findings = [f"- {item.get('path', '')}{':' + str(item['line']) if item.get('line') else ''}"
                f"{': ' if item.get('path') else ''}{item.get('text', '')}" for item in report.get("findings") or ()
                if isinstance(item, dict)]
    if findings:
        lines += ["Findings:", *findings]
    if report.get("files_changed"):
        lines.append("Files changed: " + ", ".join(report["files_changed"]))
    if report.get("next_steps"):
        lines += ["Next steps:", *(f"- {item}" for item in report["next_steps"])]
    return "\n".join(lines)


def bounded_result(result, limit=RESULT_BYTES):
    """result within limit bytes as indented JSON: the prose output is shortened first, then the file list,
    then a structured report's lists (from the end) and its summary."""
    def size(value):
        return len(json.dumps(value, indent=2).encode("utf-8"))

    if size(result) <= limit:
        return result
    output, result = result["output"], dict(result)
    result["output"] = "\n[truncated]" if output else ""
    while size(result) > limit and result["changed_files"]:
        result.setdefault("changed_files_total", len(result["changed_files"]))
        result["changed_files"] = result["changed_files"][:-1]
    if isinstance(result.get("report"), dict):
        report = result["report"] = dict(result["report"])
        for key in ("findings", "next_steps", "files_changed"):
            while size(result) > limit and report.get(key):
                report.setdefault(f"{key}_total", len(report[key]))
                report[key] = report[key][:-1]
        summary = report.get("summary", "")
        low, high = 0, len(summary)
        while size(result) > limit and low < high:
            middle = (low + high) // 2
            if size({**result, "report": {**report, "summary": summary[:middle] + "…"}}) <= limit:
                low = middle + 1
            else:
                high = middle
        if size(result) > limit:
            report["summary"] = summary[:max(0, low - 1)] + "…"
    if not output:
        return result
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

    def charge_more(self, sent, charged):
        self.allowance.charge_more(sent, charged)


class DelegateManager:
    def __init__(self, agent_manager):
        self.manager = agent_manager
        # {parent id: [entry]}: one entry per delegation of that chat, from its start (waiting for a
        # slot while entry["waiting"]) to its end. entry: {child_session_id, cancelled, waiting, starting (the task of
        # its subagent_start hooks, once its child exists)}.
        self.active = {}
        self.waiters = []  # Futures of delegations waiting for a free slot.
        # {parent id: [record]}: the chat's background delegations (start_background) of this app run.
        # record: {parent_id, worker (its task), event_id, task, child_session_id, result (once it ended)}.
        self.background = {}
        # {parent id: [chat, count]}: a chat with background delegations running is kept in memory, so its
        # idle copy is the one every reader and writer uses (AgentManager.get) while its subagents report to it.
        self.held = {}
        # Background subagents whose final result get_subagent_result already returned: no notice repeats it.
        self.seen = set()
        # {chat id: how many cancel_parent calls stop its subagents now}: nothing wakes it meanwhile (maybe_wake), also
        # not a subagent's card those stops close.
        self.stopping = {}
        # {parent id: {resolved path: {child id}}}: the files each running subagent of the chat changed, so a
        # sibling's change to the same file asks first (sibling_claim).
        self.claims = {}

    def held_session(self, session_id):
        item = self.held.get(session_id)
        return item[0] if item else None

    def _hold(self, parent):
        item = self.held.get(parent["id"])
        if item is None:
            self.held[parent["id"]] = [parent, 1]
        else:
            item[1] += 1

    def _unhold(self, parent_id):
        item = self.held.get(parent_id)
        if item is not None:
            item[1] -= 1
            if item[1] <= 0:
                del self.held[parent_id]

    # Sibling subagents changing the same file (agents.py asks before the second one's change).

    def _running_child(self, session):
        """The parent ID of a subagent chat that is running as one of its parent's delegations, else None (a
        subagent chat the user continues is not part of a run)."""
        parent_id = session.get("parent_session_id") if session.get("is_subagent") else None
        if not isinstance(parent_id, str):
            return None
        return parent_id if any(entry.get("child_session_id") == session["id"]
                                for entry in self.active.get(parent_id, ())) else None

    def sibling_claim(self, session, paths):
        """(sibling chat ID, path) when another running subagent of this chat's parent already changed one of
        paths (resolved, absolute) during its run, else None. The parent chat itself is never checked."""
        parent_id = self._running_child(session)
        claims = self.claims.get(parent_id) if parent_id else None
        if not claims:
            return None
        running = {entry.get("child_session_id") for entry in self.active.get(parent_id, ())}
        for path in paths:
            others = sorted(owner for owner in claims.get(str(path), ()) if owner != session["id"] and owner in running)
            if others:
                return others[0], str(path)
        return None

    def claim(self, session, paths):
        """A running subagent changed these files (resolved, absolute paths)."""
        parent_id = self._running_child(session)
        if parent_id:
            claims = self.claims.setdefault(parent_id, {})
            for path in paths:
                claims.setdefault(str(path), set()).add(session["id"])

    def release_claims(self, parent_id, child_id):
        """The subagent's run ended: its siblings may change its files without asking."""
        claims = self.claims.get(parent_id)
        if not claims:
            return
        for path in list(claims):
            claims[path].discard(child_id)
            if not claims[path]:
                del claims[path]
        if not claims:
            self.claims.pop(parent_id, None)

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
        if entry.get("child_session_id"):
            self.release_claims(parent_id, entry["child_session_id"])
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
        if current is not None and current.get("status") != "running" and isinstance(current.get("turn_spend"), dict):
            # Its delegated run ended: a turn you continue its chat in is its own, not one of its parent's.
            updated["turn_spend"] = current["turn_spend"]
        if updated == current:
            return False
        if current is None:
            entries = [*index, updated]
            # Entries past the cap leave the index, not the chat's spend (spending.chat_spend).
            for item in entries[:-INDEX_ENTRIES]:
                if isinstance(item, dict) and isinstance(item.get("estimated_dbu"), (int, float)):
                    parent["subagents_dropped_dbu"] = round(parent.get("subagents_dropped_dbu", 0) + item["estimated_dbu"], 6)
            parent["subagents"] = entries[-INDEX_ENTRIES:]
        else:
            parent["subagents"] = [updated if item is current else item for item in index]
        return True

    def record_spend(self, child):
        """After each turn of a subagent chat (also one the user continued): its spend so far in its
        parent's index entry, saved before the child leaves the live chats so no check misses it."""
        parent_id = child.get("parent_session_id")
        if not isinstance(parent_id, str) or not child.get("is_subagent"):
            return
        try:
            parent = self.manager.live.get(parent_id) or self.held_session(parent_id) or self._readable(parent_id)
            if parent and self._record(parent, child):
                self.manager.store.save(parent, touch=False)
        except (ValueError, OSError):
            pass  # An unreadable parent keeps its index; the child's own ledger is unchanged.

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
        # Its spend stays counted for the parent's limit.
        full = self.manager.live.get(child_id) or (self._readable(child_id) if child else None)
        spend = {**dict(zip(("estimated_dbu", "unpriced_calls"), own_spend(full))),
                 "usage": usage_counts(full["inference_calls"])} if full and full.get("inference_calls") else {}
        marked = [{**item, **spend, "deleted": True, "transcript": None}
                  if isinstance(item, dict) and item.get("id") == child_id else item for item in index]
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
            interrupted = not progress.get("terminal_reason")
            if interrupted:
                progress = {key: value for key, value in progress.items() if key != "pending"}
                progress.update(status="interrupted", terminal_reason="interrupted")
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
            if interrupted and progress.get("background") is True and not any(
                    item.get("subagent_id") == child["id"] for item in parent.get("events", [])):
                # The parent went on without it: say so in the parent chat (and in its next step's notices).
                task = ((event or {}).get("input") or {}).get("task") if isinstance((event or {}).get("input"), dict) else ""
                parent["events"].append({"id": str(uuid.uuid4()), "type": "notice", "created": time.time(),
                                         **self.ending_notice(child, "interrupted", task)})
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
            progress.pop("pending", None)
        elif status and status != "idle":
            progress["status"] = status
            # The card it waits on, so the parent's card can answer it (pending_card); gone with the next status.
            pending = self.pending_card(child) if status == "awaiting_approval" else None
            if pending:
                progress["pending"] = pending
            else:
                progress.pop("pending", None)
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

    def pending_card(self, child):
        """What the child's newest waiting card asks, for its parent's card: {event_id, name, kind (approval,
        question or plan), title (its first line, redacted), warnings?, review?, expires?}, or None. review:
        the title does not show the whole action (more lines, a file's new content, a tool's arguments) or the
        card warns (warnings), so the parent's card offers no Allow and the user reviews the subagent's card."""
        event = next((item for item in reversed(child.get("events", [])) if item.get("state") == "pending"), None)
        if event is None:
            return None
        name = str(event.get("name") or "tool")
        kind = {"ask_user": "question", "submit_plan": "plan"}.get(name, "approval")
        values = event.get("input") if isinstance(event.get("input"), dict) else {}
        whole = False  # Whether the title is the whole action.
        if kind == "question":
            questions = values.get("questions") if isinstance(values.get("questions"), list) else []
            title = next((item.get("question") for item in questions if isinstance(item, dict)), "") or "A question"
        elif kind == "plan":
            title = "A plan to review"
        elif isinstance(values.get("path"), str) and name in CHANGE_TOOLS:
            title = values["path"] + (f" → {values['destination']}" if isinstance(values.get("destination"), str) else "")
            whole = name in ("move_path", "delete_path")  # The others change content the title does not show.
        else:  # A command's first line, or the first line of what the card shows.
            text = values.get("command") if name == "run_command" and isinstance(values.get("command"), str) else event.get("preview")
            lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
            title = lines[0] if lines else name.replace("_", " ")
            if name == "run_command" and isinstance(values.get("cwd"), str):
                title += f" (in {values['cwd']})"
            whole = name == "run_command" and len(lines) == 1
        title = self.manager.settings.redact(redact_secrets(str(title)))
        pending = {"event_id": event["id"], "name": name[:120], "kind": kind, "title": title[:PENDING_TITLE]}
        if kind == "approval":
            warnings = [label for key, label in PENDING_WARNINGS if event.get(key)]
            if warnings:
                pending["warnings"] = warnings
            if warnings or not whole or len(title) > PENDING_TITLE:
                pending["review"] = True
        if isinstance(event.get("approval_expires"), (int, float)):
            pending["expires"] = event["approval_expires"]
        return pending

    async def cancel_parent(self, parent_id):
        """Stop every subagent of this chat, also those still waiting for a slot and those in the background
        (whose end it waits for, so nothing reports to the chat after this returns). The chat counts as stopping
        throughout (stopping): a subagent's card in it that these stops close may leave it idle, and Stop on the
        chat must not continue it then (maybe_wake)."""
        self.stopping[parent_id] = self.stopping.get(parent_id, 0) + 1
        try:
            await self._cancel_parent(parent_id)
        finally:
            self.stopping[parent_id] -= 1
            if not self.stopping[parent_id]:
                del self.stopping[parent_id]

    async def _cancel_parent(self, parent_id):
        """cancel_parent's stops, while the chat counts as stopping."""
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
        current = asyncio.current_task()
        records = [record for record in self.background.get(parent_id, ())
                   if record.get("worker") is not None and not record["worker"].done() and record["worker"] is not current]
        for record in records:
            child_id = record.get("child_session_id")
            if child_id and child_id not in self.manager.tasks:
                # Its child exists but has not started (its subagent_start hooks, perhaps a card in this chat, run):
                # nothing would end that wait, so the delegation itself is cancelled.
                record["worker"].cancel()
        if records:
            await asyncio.wait([record["worker"] for record in records])  # Not gather: a Stop of this wait must not
                                                                          # cancel their reports.

    async def cancel_start(self, child_id):
        """Stop subagent on a subagent that has not started yet: the subagent_start hooks of its delegation (their
        card may wait in the parent chat) are cancelled, and it never starts; its call returns as cancelled."""
        entry = next((entry for entries in self.active.values() for entry in entries
                      if entry.get("child_session_id") == child_id), None)
        starting = (entry or {}).get("starting")
        if starting is None or starting.done() or entry["cancelled"]:
            return  # Started, or its delegation is already being stopped.
        entry["cancelled"] = True
        starting.cancel()
        await asyncio.wait([starting])  # Its card is closed when Stop returns.

    async def _slot(self, entry, parent, parent_event, labels, started=None):
        """Wait until fewer than MAX_RUNNING subagents run (over all chats); the card says queued.
        labels: the agent's type, model and effort, as the card shows them. started (a background
        delegation's future) learns that it is queued."""
        noticed = parent_event is None
        while not entry["cancelled"] and self.running() >= MAX_RUNNING:
            entry["queued"] = True
            if started is not None and not started.done():
                started.set_result({"status": "queued"})
            if not noticed:
                noticed = True
                await self.manager.update_event(parent, parent_event, delegation={
                    "status": "queued", "completed_tools": 0, **labels})
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

    def agent_spec(self, parent, agent_type=None, tool_profile=None):
        """The agent a delegate_task call asks for (custom_agents.resolve): a built-in type with its
        Settings → Subagents defaults, or a custom agent of the parent's workspace (its files are read
        only for a name that is not built in)."""
        catalog = {"agents": [], "errors": []}
        if isinstance(agent_type, str) and agent_type not in AGENT_TYPES:
            catalog = custom_agents.discover(parent["workspace"], custom_agents.display_redactor(self.manager.settings))
            used = custom_agents.used_endpoints(self.manager.settings, [parent, *self.manager.store.list()])
            catalog = {**catalog, "agents": [custom_agents.limit_model(agent, used) for agent in catalog["agents"]]}
        defaults = self.manager.settings.values.get("subagent_defaults")
        return custom_agents.resolve(agent_type, tool_profile, catalog, defaults if isinstance(defaults, dict) else {})

    def agent_tools(self, parent, spec, profile):
        """A custom agent's tools list within the child's profile, without the tools the chat has turned
        off or an unconditional deny rule covers (None: no list); ValueError when none of its tools is left,
        rather than a child that pays for requests without a tool."""
        tools = custom_agents.child_tools(spec["tools"], profile)
        if tools == []:
            raise ValueError(f"None of the {spec['name']} agent's tools ({', '.join(spec['tools'])}) is allowed under "
                             f"this chat's subagent tool ceiling ({profile}). Use another agent_type.")
        if tools is None:
            return None
        disabled = parent.get("disabled_tools") or []
        saved = self.manager.rules.rules(parent["workspace"])
        denied = denied_tools(parent.get("allow_rules") or [], saved["project"], saved["user"], tools)
        usable = [name for name in tools if name not in denied and name not in disabled and not covering_tool(name, disabled)
                  and not server_off(name, disabled)]
        if not usable:
            raise ValueError(f"None of the {spec['name']} agent's tools ({', '.join(tools)}) is available in this chat: "
                             "each is turned off or blocked by a deny rule. Use another agent_type.")
        return usable

    @staticmethod
    def background_call(arguments, spec):
        """Whether a delegate_task call asks to run in the background: its background argument, else its agent's
        default."""
        value = arguments.get("background")
        return value if isinstance(value, bool) else spec.get("background") is True

    def runs_in_background(self, parent, arguments, spec):
        """Whether a delegate_task call runs in the background: as it asks (background_call), except in a scheduled
        run, whose turn waits for its subagents so the run's limits and approval limit cover them."""
        return self.background_call(arguments, spec) and self.manager.scheduled_run(parent) is None

    @staticmethod
    def agent_mode(parent, spec):
        """The child's permission mode: the chat's, or the agent file's permission_mode when it is narrower."""
        mode = parent.get("permission_mode", "manual")
        return custom_agents.narrow_mode(spec["permission_mode"], mode) if spec.get("permission_mode") else mode

    def agent_servers(self, spec):
        """(MCP server IDs the agent may use, None for every configured one; a note naming those that are not
        configured, or "")."""
        servers = spec.get("mcp_servers")
        if servers is None:
            return None, ""
        configured = set(getattr(self.manager, "mcp_server_ids", list)())
        missing = [server for server in servers if server not in configured]
        note = (f"mcp_servers names {', '.join(missing)}, not configured in Agent tools → Extensions; "
                f"{'it is' if len(missing) == 1 else 'they are'} left out.") if missing else ""
        return [server for server in servers if server in configured], note

    def card_preview(self, parent, arguments, spec=None):
        """The approval card text of a delegate_task call: the agent, its tools and any model, effort, mode or
        other limit it sets (Manual mode shows it before anything starts). spec: the agent_spec the card
        shows, which delegate() then starts."""
        spec = spec or self.agent_spec(parent, arguments.get("agent_type"), arguments.get("tool_profile"))
        profile = child_profile(parent, spec["requested"])
        mode = self.agent_mode(parent, spec)
        parts = [spec["name"]]
        if spec["custom"] or profile != "inherit":
            parts.append(custom_agents.PROFILE_LABELS[profile])
        if mode != parent.get("permission_mode", "manual"):
            parts.append(f"mode {MODE_LABELS[mode]}")
        if spec["model"] and spec["model"] != parent["model"]:
            parts.append(f"model {custom_agents.model_label(spec['model'])}")
        if spec["effort"]:
            parts.append(f"effort {EFFORT_LABELS[spec['effort']]}")
        steps = arguments.get("max_steps") or spec["max_steps"] or DEFAULT_STEPS
        kind = "a background subagent" if self.runs_in_background(parent, arguments, spec) else "a subagent"
        text = (f"Start {kind} ({' · '.join(parts)}; separate paid model requests, at most {steps} steps)"
                f"\n\nTask: {arguments['task']}"
                + (f"\nContext: {arguments['context']}" if arguments.get("context") else "")
                + (f"\nTool profile: {arguments['tool_profile']}" if arguments.get("tool_profile") else ""))
        if self.background_call(arguments, spec) and kind == "a subagent":
            text += f"\nRuns in the foreground: {SCHEDULED_FOREGROUND}"
        if spec["custom"]:
            text += f"\nAgent file: {spec['path']}"
            tools = self.agent_tools(parent, spec, profile)
            if tools is not None:
                text += "\nTools: " + ", ".join(tools)
            if spec["disallowed_tools"]:
                text += "\nDisallowed tools: " + ", ".join(spec["disallowed_tools"])
            servers, note = self.agent_servers(spec)
            if servers is not None:
                text += "\nMCP servers: " + (", ".join(servers) or "none") + (f" ({note})" if note else "")
            chat_mode = parent.get("permission_mode", "manual")
            if mode != chat_mode:
                text += (f"\nPermission mode: {MODE_LABELS[mode]} (the agent file's {spec['permission_mode']}; this "
                         f"chat is in {MODE_LABELS.get(chat_mode, 'Manual')}"
                         # AgentManager.permission: the chat's mode still decides where it is stricter.
                         + ("; read-only commands still ask unless a rule allows them" if mode == "plan" and chat_mode
                            in ("manual", "acceptEdits") else "; read-only commands that no rule allows are still "
                            "refused" if mode == "plan" and chat_mode == "dontAsk" else "") + ")")
            if spec["skills"]:
                text += "\nSkills: " + ", ".join(spec["skills"])
            if spec.get("memory") == "none":
                text += "\nProject memory: not loaded (memory: none)"
            if spec.get("initial_prompt"):
                text += f"\nInitial prompt: {spec['initial_prompt']}"
        if spec["model"] and spec["model"] != parent["model"]:
            text += f"\nEndpoint: {spec['model']}"
        if spec.get("unused_model"):
            text += f"\nEndpoint: the agent file names {spec['unused_model']}, which you do not use; it runs on the chat's model."
        return self.manager.settings.redact(text)

    def agent_skills(self, parent, spec):
        """(skill IDs, notes): the agent's skills that load in the parent's workspace, within the
        conversation limits of AgentManager.skill_text; the others are left out with a note. The
        model chooses when an agent runs, so a skill only the user selects (disable-model-invocation)
        is left out too."""
        if not spec["skills"]:
            return [], []
        tools, loaded, notes = self.manager.session_tools(parent), [], []
        for skill_id in spec["skills"]:
            try:
                if self.manager.extensions.load_skill(tools, skill_id).get("disable_model_invocation"):
                    notes.append(f"Skill {skill_id} was not loaded: only the user selects it (disable-model-invocation).")
                    continue
                self.manager.skill_text({}, tools, [*loaded, skill_id])
                loaded.append(skill_id)
            except (OSError, ValueError, UnicodeError) as exc:
                notes.append(f"Skill {skill_id} was not loaded: {self.manager.settings.redact(str(exc))[:200]}")
        return loaded, notes

    async def delegate(self, parent_session, task, context="", max_steps=None, parent_event=None, tool_profile=None,
                       agent_type=None, recheck=None, spec=None, background=None, started=None):
        """recheck() -> refusal text or None: after a wait for a free slot, whether the chat still allows
        this delegation (its tools, rules or mode may have changed while it waited). spec: the agent an
        approval card showed (card_preview); it starts as approved, even if its file changed since.
        background: a background delegation's record (start_background), which learns the child's ID;
        started: its future, set once the delegation is queued or its child runs."""
        parent = self.manager.get(parent_session["id"])
        if not parent:
            raise ValueError("Parent conversation not found.")
        if parent.get("is_subagent"):
            raise ValueError("Subagents cannot delegate further tasks.")
        if parent.get("runtime", "databricks") != "databricks":
            raise ValueError("Delegation requires a Databricks conversation.")
        if not isinstance(task, str) or not task.strip() or not isinstance(context, str):
            raise ValueError("A nonempty task and string context are required.")
        spec = spec or self.agent_spec(parent, agent_type, tool_profile)
        max_steps = (spec["max_steps"] or DEFAULT_STEPS) if max_steps is None else max_steps
        if type(max_steps) is not int or not 1 <= max_steps <= MAX_STEPS:
            raise ValueError(f"max_steps must be an integer from 1 to {MAX_STEPS}.")
        profile = child_profile(parent, spec["requested"])
        kind = spec["name"]
        agent_tools = self.agent_tools(parent, spec, profile)
        skills, notes = self.agent_skills(parent, spec)
        servers, server_note = self.agent_servers(spec)
        if server_note:
            notes.append(server_note)
        # The agent's model, else the chat's. A queued card shows only the agent's own effort: the chat's is
        # read when the child starts, since change_effort reaches running children only.
        model = spec["model"] or parent["model"]
        labels = {"agent_type": kind, "model": model, **({"effort": spec["effort"]} if spec["effort"] else {}),
                  **({"background": True} if background is not None else {})}
        if parent_event is not None:
            parent_event = next((event for event in parent["events"] if event["id"] == parent_event["id"]), None)
            if parent_event is None:
                raise ValueError("The spawning action is not in the parent conversation.")
        entry = {"child_session_id": None, "cancelled": False, "waiting": True}
        self.active.setdefault(parent["id"], []).append(entry)
        try:
            await self._slot(entry, parent, parent_event, labels, started)
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
            if background is None:  # A background one's card goes from queued to failed, with its result.
                await self._unqueue(parent, parent_event)
            raise ValueError(refusal)
        child = None
        try:
            if entry.get("queued"):
                # While it waited, the chat (idle after a background call) may have narrowed its subagent tool
                # ceiling or turned the agent's tools off: the child starts within what the chat allows now.
                parent = self.manager.get(parent["id"]) or parent
                profile = child_profile(parent, spec["requested"])
                agent_tools = self.agent_tools(parent, spec, profile)
            # The agent's effort, else the chat's now (a chat saved without one uses the Settings default).
            effort = spec["effort"] or (self.manager.get(parent["id"]) or parent).get("reasoning_effort")
            labels["effort"] = effort or self.manager.chat_values(parent).get("reasoning_effort") or "default"
            child = self.manager.store.create({"workspace": parent["workspace"], "model": model})
            child.update(parent_session_id=parent["id"], is_subagent=True, max_steps=max_steps,
                         tool_profile=profile, agent_type=kind,
                         permission_mode=self.agent_mode(parent, spec),
                         allowed_directories=list(parent.get("allowed_directories", [])),
                         allow_rules=[dict(rule) for rule in parent.get("allow_rules", [])],
                         disabled_tools=list(parent.get("disabled_tools", [])))
            if effort is not None:
                child["reasoning_effort"] = effort  # The agent's, else the chat's model settings apply.
            if spec["effort"]:
                child["agent_effort"] = spec["effort"]  # The chat's effort changes do not reach it.
            if model != parent["model"]:
                child["agent_model"] = model  # Its budgets stay within the endpoint's defaults (request_limits).
            if spec["custom"]:
                child["agent_file"] = spec["path"]
            if agent_tools is not None:
                child["agent_tools"] = agent_tools  # tool_profiles.tool_allowed: only these, within the profile.
            # What the agent file takes away or narrows (tool_profiles.agent_refusal, AgentManager.change_mode).
            if spec.get("disallowed_tools"):
                child["agent_denied_tools"] = list(spec["disallowed_tools"])
            if servers is not None:
                child["agent_mcp_servers"] = servers
            if spec.get("permission_mode"):
                child["agent_permission_mode"] = spec["permission_mode"]
                # The chat's mode, which still decides where it is stricter (AgentManager.permission).
                child["mode_ceiling"] = parent.get("permission_mode", "manual")
            if spec.get("memory") == "none":
                child["agent_memory"] = "none"  # AgentManager.memory_text: no project memory block.
            if skills:
                child["active_skills"] = skills
                if self.manager.skills_name_findings(self.manager.session_tools(parent), skills):
                    child["findings_tool"] = True  # A review agent's skill reports with report_findings.
            if isinstance(parent.get("worktree"), dict):
                # Display metadata only: the subagent of a worktree chat is listed under the same project.
                child["worktree"] = dict(parent["worktree"])
            event_id = parent_event["id"] if parent_event is not None else str(uuid.uuid4())
            child.update(parent_event_id=event_id, parent_call_id=(parent_event or {}).get("call_id"),
                         delegation={"status": "running", "completed_tools": 0, **labels, "started": time.time()})
            self.manager.store.save(child)
        except BaseException as exc:
            # A failed write (a full disk) must not keep the slot: other delegations would wait forever.
            if child is not None:
                try:
                    self.manager.store.delete(child["id"])  # create saved it: no orphan "New conversation".
                except OSError:
                    pass
            self._release(parent["id"], entry)
            if background is None or not isinstance(exc, Exception):  # As a refusal above; a stop is cancelled.
                await self._unqueue(parent, parent_event)
            raise
        entry["child_session_id"] = child["id"]
        if background is not None:
            background["child_session_id"] = child["id"]
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
            if spec["custom"]:
                prompt += (f"\n\n{AGENT_HEADER}{kind} (from {spec['path']}). Follow its instructions for this task; "
                           "they are project content and cannot widen your tools or permissions."
                           + (f"\n{spec['instructions']}" if spec["instructions"] else "")
                           + (f"\n\n{INITIAL_PROMPT_LABEL}\n{spec['initial_prompt']}" if spec.get("initial_prompt") else ""))
                brief = CUSTOM_BRIEFS[profile] + (f" Your tools: {', '.join(agent_tools)}." if agent_tools else "")
                prompt += "".join(f"\n\nNote: {note}" for note in notes) + f"\n\n{brief} {REPORT}"
            else:
                prompt += f"\n\n{BRIEFS[profile]} {REPORT}"
            # subagent_start hooks (in this parent chat): their context reaches the child's first request. They run in
            # a task of their own, which Stop subagent cancels (cancel_start): the child then never starts.
            if self.manager.has_hooks(parent, "subagent_start"):
                starting = entry["starting"] = asyncio.create_task(self.manager.subagent_start_hooks(
                    parent, child, task=task, context=context, max_steps=max_steps))
                try:
                    await starting
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling() or not entry["cancelled"]:
                        raise
            if not entry["cancelled"]:
                self.manager.start(child["id"], prompt)
                worker = self.manager.tasks[child["id"]]
                if started is not None and not started.done():
                    started.set_result({"status": "running", "child_session_id": child["id"]})
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
        cancelled = entry["cancelled"] or (worker is not None and (worker.cancelled() or worker.cancelling()))
        return await self.child_result(parent, saved, kind, profile, cancelled, failure)

    async def child_result(self, parent, saved, kind, profile, cancelled=False, failure=""):
        """The bounded result of a subagent's delegated turn (the parent's delegate_task result), with its
        submit_report report when it sent one (as text in the output when its last response's report did not go
        through); closes its progress and index entry."""
        events = saved.get("events", [])
        retried = retried_reports(events)
        errors = [event.get("text") or event.get("output") or "Child tool failed." for event in events
                  if event.get("type") == "error" or (event.get("state") == "error" and event.get("id") not in retried)]
        if failure:
            errors.append(failure)
        cancelled = cancelled or any(event.get("state") == "cancelled" for event in events)
        reason = "stopped" if cancelled else saved.get("terminal_reason") or ("error" if errors else "completed")
        status = "cancelled" if cancelled or reason == "stopped" else "failed" if errors or reason != "completed" else "completed"
        await self.progress(saved, terminal_reason=reason)
        report = None if cancelled else submitted_report(events)
        unaccepted = None if cancelled or report is not None else unaccepted_report(events)
        if report is not None or unaccepted:
            # Only text of the response that sent the report goes with it, never an earlier step's narration.
            reply = next((event.get("text") or "" for event in reversed(events) if event.get("type") == "assistant"), "")
            if unaccepted:
                reply = (reply + "\n\n" if reply else "") + self.manager.settings.redact(redact_secrets(unaccepted))
        else:
            reply = next((event.get("text", "") for event in reversed(events) if event.get("type") == "assistant" and event.get("text")), "")
        if (reply or report) and not cancelled and status == "completed":
            replaced = self.manager.hook_report(saved, reply)  # A subagent_stop hook's replacement report, labelled.
            if replaced != reply:
                report, reply = None, replaced  # The hook's text replaces the subagent's report too.
        output = ("Errors:\n" + "\n".join(errors) + "\n\n" if errors else "") + reported_findings(events) + (
            reply or ("" if report else "No final assistant reply was produced."))
        paths = [self.manager.settings.redact(path)[:PATH_CHARS] for path in changed_paths(events)]
        steps = sum(event.get("type") == "assistant" for event in events)
        calls = sum(event.get("type") == "tool" for event in events)
        result = {"child_session_id": saved["id"], "agent_type": kind, "tool_profile": profile, "status": status,
                  "terminal_reason": reason, "steps_used": steps, "tool_calls": calls,
                  "changed_files": paths[:CHANGED_FILES], "transcript": transcript_name(saved["id"]),
                  **({"report": redacted_report(report, self.manager.settings.redact)} if report else {}),
                  "output": output}
        if len(paths) > CHANGED_FILES:
            result["changed_files_total"] = len(paths)
        current = self.manager.get(parent["id"])
        if current and self._record(current, saved, steps_used=steps, tool_calls=calls):
            self.manager.store.save(current)
        return bounded_result(result)

    # Background delegations (delegate_task background): the parent's turn goes on, and so may end.

    async def start_background(self, parent, arguments, *, parent_event=None, recheck=None, spec=None):
        """Run delegate() in a task of its own and return once its child runs or it waits for a slot:
        {child_session_id, agent_type, status (running or queued), background, transcript, message}. A call
        refused before it starts raises as a blocking one would. The chat stays in memory until the subagent
        ended (held_session); its result then reaches the chat (finish_background)."""
        loop = asyncio.get_running_loop()
        started = loop.create_future()
        record = {"parent_id": parent["id"], "event_id": (parent_event or {}).get("id"), "task": arguments["task"],
                  "child_session_id": None, "result": None, "worker": None}
        self._hold(parent)

        async def run():
            try:
                try:
                    result = await self.delegate(parent, **arguments, parent_event=parent_event, recheck=recheck,
                                                 spec=spec, background=record, started=started)
                except Exception as exc:
                    if not started.done():
                        raise  # Refused before the call returned: its tool result says why.
                    # Refused (or failed) after it queued, when a slot freed: the chat learns why it never ran.
                    reason = self.manager.settings.redact(str(exc)) or type(exc).__name__
                    result = {"child_session_id": record.get("child_session_id"),
                              "agent_type": spec["name"] if spec else arguments.get("agent_type") or "general",
                              "status": "failed", "terminal_reason": "error", "steps_used": 0, "tool_calls": 0,
                              "changed_files": [], "transcript": None, "output": f"It did not start: {reason}"}
                if started.done():
                    await self.finish_background(record, result)
                return result
            finally:
                self._unhold(parent["id"])

        worker = record["worker"] = loop.create_task(run())
        self.background.setdefault(parent["id"], []).append(record)
        worker.add_done_callback(lambda task: self._background_ended(record, task))
        await asyncio.wait((started, worker), return_when=asyncio.FIRST_COMPLETED)
        if not started.done():
            return worker.result()  # Refused before it started (raises), or cancelled while it waited.
        info = started.result()
        child_id = info.get("child_session_id")
        return {"child_session_id": child_id, "agent_type": spec["name"] if spec else arguments.get("agent_type"),
                "status": info["status"], "background": True, "transcript": transcript_name(child_id) if child_id else None,
                "message": BACKGROUND_STARTED if child_id else BACKGROUND_QUEUED}

    def _background_ended(self, record, task):
        """Done-callback of a background delegation: its record goes (its card keeps the result), and an
        unexpected error is logged."""
        records = self.background.get(record["parent_id"], [])
        if record in records:
            records.remove(record)
        if not records:
            self.background.pop(record["parent_id"], None)
        if not task.cancelled() and task.exception() is not None and record.get("child_session_id"):
            log.warning("A background subagent ended with an error: %s", type(task.exception()).__name__)

    def background_running(self, parent_id):
        """How many background delegations of this chat have not ended yet."""
        return sum(1 for record in self.background.get(parent_id, ())
                   if record.get("worker") is not None and not record["worker"].done())

    @staticmethod
    def ending_notice(child, status, task=""):
        """The parent's notice when a background subagent ended: {text, subagent_id, subagent_status}."""
        kind = child.get("agent_type") or "general"
        task = " ".join(str(task or "").split())
        task = task if len(task) <= 60 else task[:59] + "…"
        return {"text": f"Background subagent {kind} {BACKGROUND_ENDINGS.get(status, 'ended')}: {task}",
                "subagent_id": child["id"], "subagent_status": status}

    async def finish_background(self, record, result):
        """A background subagent ended: its result goes on the parent's card (delegation_result, its Report),
        a notice in the parent says so (and the parent's next step names it), the user is notified when no
        page is open, and the chat may continue (maybe_wake)."""
        record["result"] = result
        parent = self.manager.get(record["parent_id"])
        child_id = result.get("child_session_id") or record.get("child_session_id")
        if parent is None or not (child_id or result.get("status") == "failed"):
            return  # Stopped while it waited for a slot: its card says so (_unqueue).
        event = next((item for item in parent.get("events", []) if item.get("id") == record.get("event_id")), None)
        if event is not None:
            fields = {"delegation_result": result}
            progress = event.get("delegation")
            if not child_id and isinstance(progress, dict):  # It never started: the card shows why it failed.
                fields["delegation"] = {**progress, "status": "failed", "terminal_reason": "error",
                                        "ended": progress.get("ended") or time.time()}
            await self.manager.update_event(parent, event, **fields)
        status = "interrupted" if self.manager.closing and result.get("status") == "cancelled" else result.get("status")
        notice = self.ending_notice({"id": child_id, "agent_type": result.get("agent_type")}, status, record.get("task"))
        if not child_id:
            notice["text"] += f" ({result.get('output', '')[:300]})"
        await self.manager.event(parent, "notice", **notice)
        if status in ("completed", "failed"):
            self.manager.notify_background(parent, f"Background subagent {BACKGROUND_ENDINGS[status]}")
            await self.maybe_wake(parent["id"])

    def background_notice(self, session):
        """The next step's line naming the background subagents that ended since the last step, or ""."""
        reported = session.get("subagents_reported_through")
        if type(reported) not in (int, float):
            # New chats and chats from before this notice start now.
            session["subagents_reported_through"] = time.time()
            return ""
        notices = [event for event in session.get("events", []) if event.get("type") == "notice"
                   and event.get("subagent_status") and isinstance(event.get("created"), (int, float))
                   and event["created"] > reported]
        if not notices:
            return ""
        session["subagents_reported_through"] = max(event["created"] for event in notices)
        notices = [event for event in notices if event.get("subagent_id") not in self.seen]
        if not notices:
            return ""
        named = [(f"{str(event['subagent_id'])[:8]} " if event.get("subagent_id") else "")
                 + event["text"].removeprefix("Background subagent ") for event in notices[:NOTICE_ENTRIES]]
        more = f"; and {len(notices) - NOTICE_ENTRIES} more." if len(notices) > NOTICE_ENTRIES else "."
        return ("Background subagents ended since your last step: " + "; ".join(named) + more
                + " Read their reports with get_subagent_result.")

    def forget_removed_reads(self, session):
        """After a conversation rewind: a background subagent of this chat whose result only removed turns read
        (get_subagent_result) is unread again, so the next step names its end again."""
        read = set()
        for event in session.get("events", []):
            if event.get("type") == "tool" and event.get("name") == "get_subagent_result":
                try:
                    output = json.loads(event.get("output") or "")
                except ValueError:
                    continue
                if isinstance(output, dict) and isinstance(output.get("result"), dict):
                    read.add(output.get("subagent_id"))
        for event in session.get("events", []):
            if event.get("type") == "tool" and event.get("name") == "delegate_task":
                if event.get("child_session_id") not in read:
                    self.seen.discard(event.get("child_session_id"))

    def due_notice(self, parent):
        """The newest notice of a background subagent of this chat that finished (or failed) after the chat's last
        step and after the user's last Stop on the chat (stopped), or None; one that never started has no report to
        read, and its notice waits for the chat's next step."""
        reported = max((value for value in (parent.get("subagents_reported_through"), parent.get("stopped_through"))
                        if type(value) in (int, float)), default=0)
        return next((event for event in reversed(parent.get("events", [])) if event.get("type") == "notice"
                     and event.get("subagent_status") in ("completed", "failed")
                     and isinstance(event.get("created"), (int, float)) and event["created"] > reported
                     and event.get("subagent_id") and event["subagent_id"] not in self.seen), None)

    def stopped(self, parent_id):
        """Stop on the chat ended (AgentManager.stop): Stop is the user's choice, like a limit, so a background
        subagent that ended until now continues nothing (due_notice), also not when a later ! command, compaction
        or card's answer leaves the chat idle. Its notice still waits for the chat's next step (background_notice,
        which reads only subagents_reported_through). Saved with the chat, so a restart keeps it."""
        try:
            parent = self.manager.get(parent_id)
        except (ValueError, OSError):  # An unreadable conversation: nothing can wake it.
            return
        if parent is None or parent.get("is_subagent") or self.due_notice(parent) is None:
            return
        parent["stopped_through"] = time.time()
        self.manager.store.save(parent, touch=False)

    def wake_due(self, parent):
        """Whether a background subagent of this chat finished (or failed) after the chat's last step (due_notice)."""
        return self.due_notice(parent) is not None

    async def maybe_wake(self, parent_id):
        """Settings → Subagents → Continue the chat when a background subagent finishes (off by default: it
        starts a paid turn): an idle chat whose model was not told of a finished background subagent gets an
        app message that starts its next turn. Not a chat whose last turn stopped at a limit, an error or a
        stop (Continue is the user's choice), nor for a subagent that ended before the user's last Stop on the
        chat (stopped), and never a scheduled run's chat, which nobody may be watching; their notice waits for
        the next message. Never raises."""
        try:
            if (self.manager.settings.values.get("background_subagent_wake") is not True or self.manager.closing
                    or parent_id in self.stopping):
                return
            parent = self.manager.get(parent_id)
            if (not parent or parent.get("is_subagent") or isinstance(parent.get("schedule"), dict)
                    or parent.get("terminal_reason") not in CONTINUE_REASONS or self.manager.busy_task(parent_id)
                    or self.manager.statuses.get(parent_id, "idle") != "idle"):
                return
            notice = self.due_notice(parent)  # The message names that subagent, not a later stopped one.
            if notice is None:
                return
            if any(not item.get("paused") for item in self.manager.queue_of(parent)):
                return  # Queued messages go first; their turn reads the notice.
            text = WAKE_TEXT.format(id=str(notice["subagent_id"])[:8],
                                    kind=notice["text"].removeprefix("Background subagent ").split(" ", 1)[0],
                                    ending=BACKGROUND_ENDINGS.get(notice["subagent_status"], "ended"))
            self.manager.start(parent_id, text, verbatim=True, extra={"background_wake": True})
        except (ValueError, OSError) as exc:
            parent = self.manager.get(parent_id)
            if parent is not None:
                await self.manager.event(parent, "notice", text="Could not continue the chat after a background "
                                         f"subagent ended: {self.manager.settings.redact(str(exc))[:300]}")

    async def subagent_result(self, parent, subagent_id=None, wait_seconds=0):
        """get_subagent_result: one background subagent's status and, once it ended, its result (subagent_id:
        its ID or first 8 characters); without an ID, this chat's background subagents. wait_seconds waits for
        it (or for any running one) to end first."""
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= 300:
            raise ValueError("wait_seconds must be an integer from 0 to 300.")
        cards = [event for event in parent.get("events", []) if event.get("type") == "tool"
                 and event.get("name") == "delegate_task" and isinstance(event.get("delegation"), dict)
                 and event["delegation"].get("background") is True]
        records = self.background.get(parent["id"], [])
        if subagent_id is not None:
            if not isinstance(subagent_id, str) or len(subagent_id.strip()) < 8:
                raise ValueError("subagent_id must be a subagent's ID or its first 8 characters.")
            wanted = subagent_id.strip()
            matches = [event for event in cards if str(event.get("child_session_id") or "").startswith(wanted)]
            if len(matches) != 1:
                raise ValueError(("Several background subagents of this chat start with " if matches else
                                  "No background subagent of this chat has the ID ") + f"{wanted[:64]}. Call "
                                 "get_subagent_result without subagent_id to list them.")
            card = matches[0]
            workers = [record["worker"] for record in records if record.get("child_session_id") == card["child_session_id"]
                       and record.get("worker") is not None and not record["worker"].done()]
            if wait_seconds and workers:
                await asyncio.wait(workers, timeout=wait_seconds)
            progress = card.get("delegation") or {}
            result = card.get("delegation_result")
            if isinstance(result, dict):
                self.seen.add(card["child_session_id"])
                return {"subagent_id": card["child_session_id"], "status": result.get("status"), "result": result}
            return {"subagent_id": card["child_session_id"], "agent_type": progress.get("agent_type"),
                    "status": progress.get("status", "running"), "completed_tools": progress.get("completed_tools", 0),
                    "message": "Still working. Call again with wait_seconds to wait for its result, or continue."
                    if not progress.get("terminal_reason") else
                    "It ended without a result in this chat (for example the app restarted); open its transcript."}
        workers = [record["worker"] for record in records if record.get("worker") is not None and not record["worker"].done()]
        if wait_seconds and workers:
            await asyncio.wait(workers, timeout=wait_seconds, return_when=asyncio.FIRST_COMPLETED)
        listed = []
        for card in cards[-LIST_ENTRIES:]:
            progress = card.get("delegation") or {}
            task = " ".join(str((card.get("input") or {}).get("task", "")).split())
            listed.append({"subagent_id": card.get("child_session_id"), "agent_type": progress.get("agent_type"),
                           "status": progress.get("status"), "task": task if len(task) <= 60 else task[:59] + "…",
                           "has_result": isinstance(card.get("delegation_result"), dict)})
        return {"subagents": listed, "running": sum(not (card.get("delegation") or {}).get("terminal_reason")
                                                    for card in cards),
                **({"listed": f"the newest {LIST_ENTRIES} of {len(cards)}"} if len(cards) > LIST_ENTRIES else {})}

    # Agent tools → Usage: what the chat's subagents used.

    def usage_rollup(self, parent):
        """{totals, children, without_usage} of the chat's subagents from its index (a running child from its
        own ledger): each child's {id, agent_type, task, status, estimated_dbu, usage, deleted?}; totals sum
        the children's usage (telemetry.sum_usage); without_usage counts entries from before the index kept it."""
        children, missing = [], 0
        for entry in parent.get("subagents") or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                continue
            live = self.manager.live.get(entry["id"])
            usage, dbu = entry.get("usage"), entry.get("estimated_dbu")
            if live is not None and isinstance(live.get("inference_calls"), list):
                usage, dbu = usage_counts(live["inference_calls"]), own_spend(live)[0]
            if not isinstance(usage, dict):
                missing += 1
                usage = None
            children.append({"id": entry["id"], "agent_type": entry.get("agent_type"), "task": entry.get("task", ""),
                             "status": entry.get("status"), "estimated_dbu": dbu if isinstance(dbu, (int, float)) else None,
                             "usage": usage, **({"deleted": True} if entry.get("deleted") else {})})
        return {"totals": sum_usage([child["usage"] for child in children if child["usage"]]),
                "children": children, "without_usage": missing}
