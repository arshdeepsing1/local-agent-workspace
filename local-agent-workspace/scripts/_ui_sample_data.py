"""Generic sample data for scripts/ui_screenshots.py: one demo project, eight chats and their events.

Everything here is invented. The project is `demo-project`, the files are `src/app.py` and friends.
SETUP_JS runs in the page after the UI test scenarios (frontend/tests/support/scenarios) and puts
this data into the fake backend those tests already use.
"""
import json

WORKSPACE = "/work/demo-project"
MODEL = "databricks-claude-opus-5-5"

REPLY = """## Retry with backoff

The client now retries a failed request. Three things changed:

- `fetch_json` calls the new `with_retry` helper.
- The delay doubles after each attempt, from `0.5` seconds.
- Only timeouts and `5xx` answers are retried.

```python
def with_retry(call, attempts=3, delay=0.5):
    for attempt in range(attempts):
        try:
            return call()
        except TimeoutError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay * 2 ** attempt)
```

All 24 tests pass. The change is in `src/app.py` and `tests/test_app.py`."""

PLAN = """## Goal

Move the settings from `config.py` into one typed loader.

## Steps

1. Add `src/settings.py` with a `Settings` dataclass.
2. Read the values from the environment in one place.
3. Replace the six direct `os.environ` reads in `src/app.py`.
4. Add tests for missing and invalid values.

## Checks

- `python -m pytest -q`
- `ruff check src tests`"""


def job(output, exit_code=0):
    """The JSON result of a finished run_command card."""
    return json.dumps({"job_id": "0123456789abcdef", "state": "completed", "exit_code": exit_code, "output": output, "offset": 0,
                       "next_offset": None, "total_chars": len(output), "truncated": False, "background": False}, indent=2)


def user(event_id, text, **extra):
    return {"id": event_id, "type": "user", "text": text, **extra}


def reply(event_id, text, **extra):
    return {"id": event_id, "type": "assistant", "text": text, **extra}


def tool(event_id, name, state="completed", output="", **fields):
    extra = {key: fields.pop(key) for key in list(fields) if key in EVENT_FIELDS}
    return {"id": event_id, "type": "tool", "name": name, "state": state, "input": fields, "output": output, **extra}


EVENT_FIELDS = {"preview", "rememberable", "rule_options", "editable", "delegation", "delegation_batch", "batch_approval",
                "child_session_id", "call_id"}

READ = tool("read-1", "read_file", path="src/app.py", start_line=1,
            output=json.dumps({"path": "src/app.py", "start_line": 1, "end_line": 86,
                               "content": "import json\nimport urllib.request\n\n\ndef fetch_json(url, timeout=10):\n    ..."}))
TESTS = "........................                                                 [100%]\n24 passed in 1.42s\n"

CHAT_EVENTS = [
    user("u1", "Add a retry with backoff to the HTTP client in src/app.py, then run the tests."),
    reply("a1", "I will read the client first."),
    READ,
    reply("a2", "The client has no retry. I will add a small helper and run the tests."),
    tool("edit-1", "edit_file", path="src/app.py", old_text="    return json.load(response)",
         new_text="    return with_retry(lambda: json.load(response))", output=json.dumps({"path": "src/app.py", "replacements": 1})),
    tool("cmd-1", "run_command", command="python -m pytest -q", output=job(TESTS)),
    reply("a3", "The tests pass. I will check lint, types and the working tree."),
    tool("cmd-2", "run_command", command="ruff check src tests", output=job("All checks passed!\n")),
    tool("cmd-3", "run_command", command="python -m mypy src", output=job("Success: no issues found in 4 source files\n")),
    tool("cmd-4", "run_command", command="git status --short", output=job(" M src/app.py\n M tests/test_app.py\n")),
    reply("a4", REPLY),
]

PENDING_COMMAND = tool(
    "ask-1", "run_command", state="pending", command="python -m pytest tests/test_app.py -q",
    preview="Run a command in demo-project\n\npython -m pytest tests/test_app.py -q",
    rememberable=["chat", "project", "user"], editable=["command"],
    rule_options={"argv": ["python", "-m", "pytest", "tests/test_app.py", "-q"],
                  "prefixes": [{"prefix": ["python", "-m", "pytest"], "flags": ["-q"]}]})

APPROVAL_EVENTS = [
    user("u1", "The date parser test fails now and then. Find out why."),
    reply("a1", "I will read the test, then run it a few times."),
    {**READ, "input": {"path": "tests/test_app.py", "start_line": 1}},
    reply("a2", "The test compares against the current day. I want to run it to confirm."),
    PENDING_COMMAND,
]

PLAN_EVENTS = [
    user("u1", "Plan how to move the settings out of config.py. Do not change files yet."),
    reply("a1", "I read `config.py` and the places that use it. Here is the plan."),
    tool("plan-1", "submit_plan", state="pending", title="Move settings into a typed loader", plan=PLAN),
]

BREAKDOWN = {"system_instructions": 4200, "tool_definitions": 9800, "messages_and_results": 38600, "summary": 5200,
             "request_overhead": 900}
CONTEXT = {
    "estimated_tokens": 58700, "input_budget": 120832, "context_window": 131072, "reply_reserve": 8192, "compactions": 1,
    "summarized_messages": 12, "estimate_method": "weighted_utf8", "instruction_files": ["AGENTS.md"], "warnings": [],
    "breakdown": BREAKDOWN, "tool_groups": {"Built-in tools": 7600, "MCP docs": 2200}, "auto_compact_percent": 90,
    "auto_compact_threshold": 108748, "percent_used": 49, "used_tokens": 58700, "limit_tokens": 120832,
    "limit_name": "context budget", "is_estimate": True, "compact_at_percent": 90, "last_request_cached": 21000,
}

CHANGES = {"files": [
    {"path": "src/app.py", "status": "modified", "added": 14, "removed": 2, "current_hash": "hash-app"},
    {"path": "tests/test_app.py", "status": "modified", "added": 21, "removed": 0, "current_hash": "hash-test"},
    {"path": "docs/retry.md", "status": "created", "added": 9, "removed": 0, "current_hash": "hash-doc"},
], "added": 44, "removed": 2}

TASKS = [
    {"id": "t1", "title": "Read the HTTP client", "description": "", "status": "completed", "depends_on": []},
    {"id": "t2", "title": "Add the retry helper", "description": "", "status": "completed", "depends_on": ["t1"]},
    {"id": "t3", "title": "Cover the retry with tests", "description": "", "status": "in_progress", "depends_on": ["t2"]},
    {"id": "t4", "title": "Document the new option", "description": "", "status": "pending", "depends_on": ["t3"]},
]

QUEUED = [{"id": "q1", "text": "Also add the retry option to the README.", "created": 1, "paused": False}]

DRAFT = "One more thing for the retry:\n- make the number of attempts a setting\n- log each retry at debug level"

DIFF = {
    "path": "src/app.py", "status": "modified", "added": 6, "removed": 1, "baseline_hash": "base", "current_hash": "hash-app",
    "hunks": [{"index": 0, "line": 1, "removed": 0, "added": 1}, {"index": 1, "line": 8, "removed": 1, "added": 5}],
    "lines": [
        {"kind": "context", "text": "import json", "old": 1, "new": 1},
        {"kind": "added", "text": "import time", "new": 2, "hunk": 0},
        {"kind": "context", "text": "import urllib.request", "old": 2, "new": 3},
        {"kind": "context", "text": "", "old": 3, "new": 4},
        {"kind": "context", "text": "", "old": 4, "new": 5},
        {"kind": "context", "text": "def fetch_json(url, timeout=10):", "old": 5, "new": 6},
        {"kind": "context", "text": "    with urllib.request.urlopen(url, timeout=timeout) as response:", "old": 6, "new": 7},
        {"kind": "removed", "text": "        return json.load(response)", "old": 7, "hunk": 1},
        {"kind": "added", "text": "        return with_retry(lambda: json.load(response))", "new": 8, "hunk": 1},
        {"kind": "added", "text": "", "new": 9, "hunk": 1},
        {"kind": "added", "text": "", "new": 10, "hunk": 1},
        {"kind": "added", "text": "def with_retry(call, attempts=3, delay=0.5):", "new": 11, "hunk": 1},
        {"kind": "added", "text": "    for attempt in range(attempts):", "new": 12, "hunk": 1},
    ],
}

FILES = [{"path": name, "name": name, "directory": directory} for name, directory in [
    ("docs", True), ("src", True), ("tests", True), ("AGENTS.md", False), ("README.md", False), ("pyproject.toml", False)]]

BATCH_TASKS = ["Check the error handling of the retry", "Check the tests for gaps", "Check the docs against the code"]


def subagent(index, status, **delegation):
    done = status == "completed"
    output = json.dumps({"child_session_id": f"child-{index}", "agent_type": "explore", "tool_profile": "read_only",
                         "status": "completed", "terminal_reason": "completed", "steps_used": 4, "tool_calls": 6,
                         "changed_files": [], "transcript": f"conversations/child-{index}.jsonl", "output": "",
                         "report": {"summary": "The retry handles timeouts. It does not handle a refused connection."}}) if done else ""
    return tool(f"sub-{index}", "delegate_task", state="completed" if done else "running", output=output,
                task=BATCH_TASKS[index], agent_type="explore", max_steps=12, tool_profile="read_only",
                child_session_id=f"child-{index}", call_id=f"call-{index}",
                delegation={"status": status, "agent_type": "explore", "max_steps": 12, **delegation},
                delegation_batch={"id": "run-1", "index": index, "size": 3, "origin": "user", "approval": "batch"})


def waiting(index, task):
    return tool(f"next-{index}", "delegate_task", state="pending", task=task, agent_type="explore", max_steps=12,
                tool_profile="read_only", call_id=f"next-call-{index}",
                preview=f"Start a subagent (Explore; separate paid model requests, at most 12 steps)\n\nTask: {task}",
                rememberable=["chat", "project", "user"], rule_options=None, editable=[],
                delegation_batch={"id": "run-2", "index": index, "size": 2, "origin": "agent"},
                batch_approval={"id": "run-2", "size": 2, "event_ids": ["next-0", "next-1"], "review": False})


# Times are seconds before "now"; SETUP_JS turns the START/END markers into epoch seconds in the page.
WORKFLOW_EVENTS = [
    user("u1", "/workflow Review the retry change from three angles."),
    reply("a1", "I will start three subagents, one for each angle."),
    subagent(0, "completed", terminal_reason="completed", completed_tools=6, steps=4, tool_calls=6, started=-150, ended=-70),
    subagent(1, "running", completed_tools=3, steps=2, tool_calls=3, last_tool="search_files", last_tool_state="completed",
             last_tool_subject="with_retry", started=-150),
    subagent(2, "awaiting_approval", completed_tools=2, steps=2, tool_calls=3, last_tool="run_command", started=-148,
             pending={"event_id": "child-ask", "name": "run_command", "kind": "approval",
                      "title": "python -m pytest tests/test_app.py -q"}),
    reply("a2", "Two more areas need a look. I will start two more subagents."),
    waiting(0, "Check the changelog entry"),
    waiting(1, "Check the type hints of the new helper"),
]


def session(session_id, title, age, events=(), **extra):
    """age: seconds since the chat last changed (the sidebar groups chats by day)."""
    return {"id": session_id, "title": title, "workspace": WORKSPACE, "model": MODEL, "status": "idle", "events": list(events),
            "updated": -age, "permission_mode": "manual", "allowed_directories": [], **extra}


HOUR, DAY = 3600, 86400
SESSIONS = [
    session("chat", "Add retry to the HTTP client", 120, CHAT_EVENTS, context_info=CONTEXT, pinned=True, tags=["backend"]),
    session("busy", "Add retry to the HTTP client (follow-up)", 60, CHAT_EVENTS[:6], context_info=CONTEXT, status="running",
            queued_messages=QUEUED),
    session("approval", "Fix the flaky date parser test", 300, APPROVAL_EVENTS, context_info=CONTEXT, status="awaiting_approval"),
    session("workflow", "Review the retry change", 2 * HOUR, WORKFLOW_EVENTS, context_info=CONTEXT, status="awaiting_approval"),
    session("plan", "Move settings into a typed loader", 5 * HOUR, PLAN_EVENTS, context_info=CONTEXT,
            status="awaiting_approval", permission_mode="plan"),
    session("docs", "Write the README quickstart", DAY + HOUR, tags=["docs"]),
    session("deps", "Upgrade the test dependencies", 2 * DAY, terminal_reason="error"),
    session("explore", "Explore the project structure", 6 * DAY),
    session("notes", "Draft the release notes", 12 * DAY, archived=True),
]

# Installed after the scenarios: the demo project replaces their one-line answers, and the handlers of both scenarios
# (the whole app's and the Agent tools dialog's) stay behind it.
SETUP_JS = """
(() => {
  const now = Date.now() / 1000
  const stamp = value => typeof value === 'number' && value <= 0 ? now + value : value
  const sample = __SAMPLE__
  for (const item of sample.sessions) {
    item.updated = stamp(item.updated)
    for (const event of item.events) for (const key of ['started', 'ended']) {
      if (event.delegation && key in event.delegation) event.delegation[key] = stamp(event.delegation[key])
    }
  }
  window.sample = sample
  fake.sessions = new Map(sample.sessions.map(item => [item.id, item]))
  fake.settings = { ...fake.settings, workspace: sample.workspace, model: sample.model, default_model: sample.model }
  fake.memory = { ...fake.memory, project: sample.workspace }
  fake.projectLayers = { ...fake.projectLayers, workspace: sample.workspace }
  window.projectSettings = { ...window.projectSettings, workspace: sample.workspace }
  const scenarios = window.sampleScenarioHandlers
  fake.handle = (path, options = {}) => {
    const method = options.method || 'GET'
    if (path === '/api/connection') return fake.json({ connected: true, models: [sample.model, 'databricks-gpt-oss-120b'], error: null })
    if (path.startsWith('/api/files?')) return fake.json(sample.files)
    if (/^\\/api\\/sessions\\/[^/]+\\/changes\\/diff\\?/.test(path)) return fake.json(sample.diff)
    if (/^\\/api\\/sessions\\/[^/]+\\/approvals\\//.test(path) || path.endsWith('/stop')) return fake.json({ ok: true })
    if (path.startsWith('/api/git/status')) return fake.json({ available: false, reason: 'Not a Git repository.' })
    for (const handler of scenarios) {
      const answer = handler(path, options)
      if (answer) return answer
    }
  }
})()
"""


def setup_js():
    sample = {"workspace": WORKSPACE, "model": MODEL, "sessions": SESSIONS, "files": FILES, "diff": DIFF}
    return SETUP_JS.replace("__SAMPLE__", json.dumps(sample))
