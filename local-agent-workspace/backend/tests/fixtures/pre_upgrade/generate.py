"""Writes the saved chats in this folder with the code of main at commit e59f328 (the last version before
the upgrade), so that later versions can prove old chats still open.

    /path/to/venv/bin/python generate.py            # rewrites parent.jsonl, child_a.jsonl, child_b.jsonl, compacted.jsonl

The tests never run this file: the generated files are committed. It needs the repository's Git history (for
`git archive`) and the app's dependencies, and it runs standalone, because the old package is also called
local_agent. It loads e59f328's backend/local_agent into a temporary folder, drives the old AgentManager
with a scripted gateway (the way the old test_parallel_subagents.py and test_manual_compaction.py do), reads
the chats the old code saved, replaces every random ID and time with a fixed one, and saves them again with
the old Store. Nothing in the folder is read except this script.
"""
import asyncio
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

COMMIT = "e59f328"
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[4]  # The repository root: pre_upgrade, fixtures, tests, backend, the app folder, the root.
APP_FOLDER = "local-agent-workspace"  # The app's folder inside the repository.
FIXED_IDS = {"parent": "pre-upgrade-parent", "child_a": "pre-upgrade-child-a", "child_b": "pre-upgrade-child-b",
             "compacted": "pre-upgrade-compacted"}
FIRST_TIME = 1_760_000_000.0  # Fixed times count up from here, in the order the old run made them.
TIME_STEP = 5.0
TASKS = {"a": "Survey the docs folder", "b": "Survey the tests folder"}
REPORTS = {"a": "The docs folder holds notes.txt and a guide.", "b": "The tests folder holds one example test."}
COMPACTION_SUMMARY = (
    "## Goal and current status\nRead notes.txt in /project and plan the example parser.\n\n"
    "## Open tasks and next steps\n- Write the parser for notes.txt.\n\n"
    "## User requirements, preferences and constraints\n- Keep answers short.\n\n"
    "## Decisions and reasons\n- Read notes.txt first, because it lists the cases.\n\n"
    "## Errors, causes and fixes\nNone.\n\n## Files read and changed\n- /project/notes.txt (read)\n\n"
    "## Commands and results\nNone.\n\n## Work done in detail\nThe user asked what notes.txt says; it lists two cases.\n\n"
    "## Key facts and gotchas\nNone.")


def load_old_code(work):
    """Put the old backend package on sys.path (extracted from Git into work); returns its folder."""
    if "local_agent" in sys.modules:
        raise SystemExit("Run generate.py in its own process: a local_agent package is already imported.")
    archive = work / "old.tar"
    with archive.open("wb") as output:
        subprocess.run(["git", "-C", str(ROOT), "archive", COMMIT, f"{APP_FOLDER}/backend/local_agent"],
                       stdout=output, check=True)
    with tarfile.open(archive) as tar:
        tar.extractall(work / "old")
    backend = work / "old" / APP_FOLDER / "backend"
    sys.path.insert(0, str(backend))
    return backend


def frame(delta, finish=None):
    chunk = {"choices": [{"delta": delta, "finish_reason": finish}]}
    if finish:
        chunk["usage"] = {"prompt_tokens": 1_200, "completion_tokens": 80, "total_tokens": 1_280}
    return "data: " + json.dumps(chunk) + "\n\n"


def reply(httpx, text):
    return httpx.Response(200, text=frame({"content": text}, "stop") + "data: [DONE]\n\n")


def calls_reply(httpx, *calls):
    """One model response with tool calls: (name, arguments, call_id) each."""
    parts = [{"index": index, "id": call_id, "type": "function",
              "function": {"name": name, "arguments": json.dumps(arguments)}}
             for index, (name, arguments, call_id) in enumerate(calls)]
    return httpx.Response(200, text=frame({"content": "", "tool_calls": parts}, "tool_calls") + "data: [DONE]\n\n")


def scripted_gateway(httpx):
    """A MockTransport handler: each chat gets its scripted responses in order (a chat is found by its first
    user message; a delegated child by the task in its first message)."""
    seen = {}

    def script(first, count, body):
        if first.startswith("Delegated task:\n"):
            key = "a" if TASKS["a"] in first else "b"
            steps = [calls_reply(httpx, ("list_files", {"path": "."}, f"list-{key}")),
                     calls_reply(httpx, ("read_file", {"path": "notes.txt", "max_lines": 20}, f"read-{key}")),
                     calls_reply(httpx, ("submit_report", {"summary": REPORTS[key], "findings": [
                         {"path": "notes.txt", "line": 1, "text": "The first line names the example case."}]},
                         f"report-{key}"))]
            return steps[min(count, len(steps)) - 1]
        if first.startswith("Please survey"):
            steps = [calls_reply(httpx,
                                 ("delegate_task", {"task": TASKS["a"], "agent_type": "explore"}, "call-a"),
                                 ("delegate_task", {"task": TASKS["b"], "agent_type": "explore"}, "call-b")),
                     reply(httpx, "Both surveys are done: the docs folder holds notes.txt, the tests folder one test.")]
            return steps[min(count, len(steps)) - 1]
        steps = [calls_reply(httpx, ("read_file", {"path": "notes.txt"}, "read-notes")),
                 reply(httpx, "notes.txt lists two cases: an empty file and a file with one line."),
                 reply(httpx, "Next, write the parser for notes.txt."),
                 reply(httpx, "Added a test plan for the parser.")]
        return steps[min(count, len(steps)) - 1]

    async def handler(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            system = body["messages"][0]["content"]
            if system.startswith("Write a detailed handoff document") or system.startswith("Summarize conversation"):
                return httpx.Response(200, json={"choices": [{"message": {"content": COMPACTION_SUMMARY},
                                                              "finish_reason": "stop"}],
                                                 "usage": {"prompt_tokens": 900, "completion_tokens": 200}})
            return httpx.Response(200, json={"choices": [{"message": {"content": "A title"}, "finish_reason": "stop"}]})
        first = next((m["content"] for m in body["messages"] if m["role"] == "user" and isinstance(m["content"], str)), "")
        # Requests are counted per chat: a child by its task, the parent, and the chat that gets compacted (whose
        # first user message is the summary after the compaction, so it cannot be found by that message).
        key = (first.split("\n\n", 1)[0] if first.startswith("Delegated task:")
               else "parent" if first.startswith("Please survey") else "chat")
        seen[key] = seen.get(key, 0) + 1
        return script(first, seen[key], body)
    return handler


async def drive(work):
    """Run the old app: a parallel delegation and a chat with one compaction. Returns the saved sessions."""
    import httpx
    import local_agent.config
    from local_agent import extensions, instructions
    from local_agent.agents import AgentManager
    from local_agent.config import Settings
    from local_agent.store import Store

    local_agent.config.APP_ROOT = work
    home = work / "home"
    home.mkdir()
    extensions.USER_HOME = home
    instructions.user_home = lambda: home
    project = work / "project"
    project.mkdir()
    (project / "notes.txt").write_text("Example case: empty file\nExample case: one line\n")
    settings = Settings(work / "state")
    settings.env = {}
    settings.values.update(workspace=str(project), env_file="", model="test-model")
    settings.credentials = lambda: ("https://gateway.example", "synthetic-token")
    store = Store(settings.state_dir / "old.sqlite3")
    manager = AgentManager(store, settings)
    real_client = httpx.AsyncClient
    handler = scripted_gateway(httpx)
    httpx.AsyncClient = lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handler))

    async def turn(session, prompt):
        manager.start(session["id"], prompt)
        await asyncio.wait_for(manager.tasks[session["id"]], 30)

    parent = store.create(settings.values)
    parent.update(title="Survey the project", title_generated=True, permission_mode="bypassPermissions")
    store.save(parent)
    await turn(parent, "Please survey the project in two parallel parts.")
    chat = store.create(settings.values)
    chat.update(title="Plan the parser", title_generated=True, permission_mode="bypassPermissions")
    store.save(chat)
    await turn(chat, "Read notes.txt and tell me what it says.")
    await turn(chat, "What should I do next?")
    manager.start_compaction(chat["id"], "Keep the open tasks")
    await asyncio.wait_for(manager.tasks[chat["id"]], 30)
    await turn(chat, "Thanks, now add a test.")
    parent = store.get(parent["id"])
    children = [store.get(entry["id"]) for entry in parent["subagents"]]
    compacted = store.get(chat["id"])
    for session_id in list(manager.tasks):
        await manager.stop(session_id)
    await manager.jobs.shutdown()
    store.db.close()
    httpx.AsyncClient = real_client
    return parent, children, compacted, project


RANDOM_ID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\b[0-9a-f]{32}\b|\b[0-9a-f]{12}\b")


def walk(value, change):
    """value with change(x) applied to every string (dict keys too) and number."""
    if isinstance(value, dict):
        return {(change(key) if isinstance(key, str) else key): walk(item, change) for key, item in value.items()}
    if isinstance(value, list):
        return [walk(item, change) for item in value]
    return change(value)


ISO_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z")
DAY = re.compile(r"(?<![\dT:-])\d{4}-\d\d-\d\d(?![\dT])")
FIRST_NANOSECONDS = 1_760_000_000_000_000_000  # A file's modification time in nanoseconds (file_views).


def fixed_day():
    return time.strftime("%Y-%m-%d", time.gmtime(FIRST_TIME))


def normalize(sessions, replacements):
    """Replace random IDs, times and temporary paths with fixed ones, the same everywhere: ids by order of
    appearance, times (seconds, ISO strings and file times in nanoseconds) by their rank, days by one day."""
    names = {}

    def collect(value):
        if isinstance(value, str):
            for found in RANDOM_ID.findall(value) + ISO_TIME.findall(value):
                names.setdefault(found, None)
        elif isinstance(value, float) and 1_500_000_000 < value < 2_500_000_000:
            names.setdefault(value, None)
        elif type(value) is int and 1_500_000_000_000_000_000 < value < 2_500_000_000_000_000_000:
            names.setdefault(value, None)
        return value
    for session in sessions:
        walk(session, collect)
    ids = [name for name in names if isinstance(name, str) and not ISO_TIME.fullmatch(name)]
    for number, name in enumerate(ids, start=1):
        names[name] = f"id-{number:04d}"
    for kind, make in ((float, lambda rank: FIRST_TIME + TIME_STEP * rank), (int, lambda rank: FIRST_NANOSECONDS + rank),
                       (str, lambda rank: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(FIRST_TIME + rank)) + ".000000Z")):
        ranked = sorted(name for name in names if type(name) is kind and (kind is not str or ISO_TIME.fullmatch(name)))
        for rank, name in enumerate(ranked):
            names[name] = make(rank)

    def change(value):
        if isinstance(value, str):
            for old, new in replacements.items():
                value = value.replace(old, new)
            value = ISO_TIME.sub(lambda match: names[match.group(0)], value)
            return DAY.sub(fixed_day(), RANDOM_ID.sub(lambda match: names[match.group(0)], value))
        if type(value) in (float, int) and value in names:
            return names[value]
        return value
    return [walk(session, change) for session in sessions]


def main():
    with tempfile.TemporaryDirectory() as folder:
        work = Path(folder).resolve()
        load_old_code(work)
        parent, children, compacted, project = asyncio.run(drive(work))
        assert len(children) == 2 and [c["id"] for c in children] == [e["id"] for e in parent["subagents"]]
        replacements = {parent["id"]: FIXED_IDS["parent"], children[0]["id"]: FIXED_IDS["child_a"],
                        children[1]["id"]: FIXED_IDS["child_b"], compacted["id"]: FIXED_IDS["compacted"],
                        str(project): "/project", str(work): "/state"}
        replacements = {**replacements, **{str(Path(old).resolve()): new for old, new in replacements.items()
                                           if old.startswith("/")}}
        fixed = normalize([parent, *children, compacted], replacements)
        text = json.dumps(fixed)
        for leftover in (str(work), "/Users/", "/private/", "/var/folders"):
            assert leftover not in text, f"{leftover} is still in the generated chats"
        from local_agent.store import Store
        store = Store(work / "fixture" / "sessions.db")
        for session in fixed:
            store.save(session, touch=False)
        store.db.close()
        for name, session_id in FIXED_IDS.items():
            (HERE / f"{name}.jsonl").write_bytes((work / "fixture" / "conversations" / f"{session_id}.jsonl").read_bytes())
    print("Wrote", ", ".join(f"{name}.jsonl" for name in FIXED_IDS), "from commit", COMMIT)


if __name__ == "__main__":
    main()
