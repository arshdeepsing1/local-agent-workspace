import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from local_agent.activity import (
    ACTIVITY_MARKER, activity_content, activity_log, insert_log, redact_secrets,
)
from local_agent.agents import AgentManager, current_time_text, output_limit_text
from local_agent.config import Settings
from local_agent.permissions import tool_decision
from local_agent.store import Store
from local_agent.tool_profiles import tool_allowed
from local_agent.tools import WorkspaceTools

FINAL_LINE = "Please read any referenced files listed above and confirm you are oriented before we begin."


def tool_event(event_id, name, created, state="completed", **fields):
    return {"id": event_id, "type": "tool", "name": name, "created": created, "state": state,
            "call_id": f"call-{event_id}", "output": fields.pop("output", ""), **fields}


def command(event_id, created, text, state="completed", exit_code=0, job_state="completed"):
    output = json.dumps({"job_id": f"job-{event_id}", "state": job_state, "exit_code": exit_code})
    return tool_event(event_id, "run_command", created, state, input={"command": text},
                      output=output if state == "completed" else "User declined this action.",
                      job_id=f"job-{event_id}")


def history(workspace):
    return [
        {"id": "u1", "type": "user", "created": 1790238000.0, "text": "Deploy it"},
        command("c1", 1790238001.0, "export GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwx123456 && git push"),
        command("c2", 1790238002.0, "make pipeline-test JOB=nightly_export_v2", exit_code=2, job_state="failed"),
        command("c3", 1790238003.0, "rm -rf build", state="rejected"),
        tool_event("r1", "read_file", 1790238004.0, input={"path": "notes.md"}),
        tool_event("r2", "read_file", 1790238005.0, input={"path": "notes.md"}),
        tool_event("e1", "edit_file", 1790238006.0, "rejected", input={"path": "src/app.py"}),
        tool_event("w1", "write_file", 1790238007.0, input={"path": f"{workspace}/out/result.md"}),
        tool_event("g1", "get_job_output", 1790238008.0, input={"job_id": "job-c2"}),
    ]


# --- Rendering ----------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("export GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwx123456", "export GITHUB_TOKEN=[REDACTED]"),
    ("git clone https://user:s3cret@github.com/org/repo", "git clone https://user:[REDACTED]@github.com/org/repo"),
    ("curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz'", "curl -H 'Authorization: Bearer [REDACTED]'"),
    ("databricks --token dapi" + "0123456789abcdef" * 2, "databricks --token [REDACTED]"),
    ("aws configure set aws_access_key_id AKIAABCDEFGHIJKLMNOP", "aws configure set aws_access_key_id [REDACTED]"),
    ("snowsql --password='hunter2' -a acct", "snowsql --password='[REDACTED]' -a acct"),
])
def test_known_secret_shapes_are_redacted(raw, expected):
    assert redact_secrets(raw) == expected


@pytest.mark.parametrize("text", [
    'TOKEN="$(cat ~/.config/token)" ./deploy.sh', "export API_KEY=$API_KEY", "kubectl get pods -n staging",
])
def test_variable_references_and_ordinary_commands_are_unchanged(text):
    assert redact_secrets(text) == text


def test_log_lists_commands_in_order_with_results_files_and_other_tools(tmp_path):
    session = {"id": "s1", "title": "Deploy", "workspace": str(tmp_path), "events": history(tmp_path)}
    jobs = [{"id": "job-c1", "state": "completed", "exit_code": 0, "background": True}]
    text, stats = activity_log(session, jobs, redact=lambda value: value.replace("nightly_export", "[SETTING]"))
    assert text.index("GITHUB_TOKEN=[REDACTED] && git push") < text.index("make pipeline-test")
    assert "ghp_" not in text
    assert "completed, exit 0, background job" in text
    assert "failed, exit 2" in text and "declined or blocked" in text
    assert "JOB=[SETTING]_v2" in text  # the app's own credential redaction also applies
    assert f"`{tmp_path / 'notes.md'}` (2 times)" in text
    assert f"`{tmp_path / 'out' / 'result.md'}`" in text
    assert f"`edit_file` `{tmp_path / 'src' / 'app.py'}` · rejected" in text
    assert "- `get_job_output`: 1" in text
    assert stats == {"commands": 3, "commands_omitted": 0, "files": 2, "other_tool_calls": 1, "declined_or_failed": 1}


def test_log_excludes_its_own_event_and_fences_commands_containing_backticks(tmp_path):
    events = [command("c1", 1.0, "echo ```nested``` && echo `date`"),
              tool_event("self", "insert_activity_log", 2.0, "running", input={"path": "h.md"})]
    text, _ = activity_log({"id": "s", "workspace": str(tmp_path), "events": events}, exclude_event_id="self")
    assert "````bash\necho ```nested``` && echo `date`\n````" in text
    assert "insert_activity_log" not in text


def test_oldest_commands_are_omitted_to_fit_and_the_newest_are_kept(tmp_path):
    events = [command(f"c{i}", float(i), f"echo step-{i:03d} " + "x" * 2000) for i in range(60)]
    text, stats = activity_log({"id": "s", "workspace": str(tmp_path), "events": events}, max_bytes=20_000)
    assert len(text.encode()) <= 20_000
    assert stats["commands_omitted"] > 0 and f"{stats['commands_omitted']} earlier commands were omitted" in text
    assert "step-059" in text and "step-000" not in text
    assert f"### Commands run (60)" in text


def test_insert_replaces_marker_appends_or_creates_and_rejects_duplicates():
    log = "## Activity log\n"
    assert insert_log(f"# H\n{ACTIVITY_MARKER}\n{FINAL_LINE}\n", log) == (f"# H\n## Activity log\n{FINAL_LINE}\n", "marker")
    assert insert_log("# H\n", log) == ("# H\n\n## Activity log\n", "appended")
    assert insert_log(None, log) == (log, "new file")
    with pytest.raises(ValueError, match="more than once"):
        insert_log(f"{ACTIVITY_MARKER}\n{ACTIVITY_MARKER}", log)


def test_file_too_close_to_the_limit_asks_for_a_separate_file(tmp_path):
    with pytest.raises(ValueError, match="separate, new file"):
        activity_content({"id": "s", "workspace": str(tmp_path), "events": []}, "x" * 79_500)


def test_activity_tool_is_a_file_edit_for_profiles_and_permission_modes():
    assert tool_allowed({"tool_profile": "file_editor"}, "insert_activity_log")
    assert not tool_allowed({"tool_profile": "read_only"}, "insert_activity_log")
    assert tool_decision("acceptEdits", "insert_activity_log", {}) == "allow"
    assert tool_decision("manual", "insert_activity_log", {}) == "ask"
    assert tool_decision("plan", "insert_activity_log", {}) == "deny"


def test_current_time_text_states_local_date_time_and_offset():
    moment = datetime(2026, 9, 24, 13, 55, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    text = current_time_text(moment)
    assert text.startswith("Current local date and time: 2026-09-24 13:55 ")
    assert "(UTC+05:30)" in text and "do not infer today's date from file names" in text


# --- Agent integration --------------------------------------------------------------

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    (project / "handoffs").mkdir(parents=True)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session["events"] = history(project)
    store.save(session)
    yield manager, session, project
    store.db.close()


def stream(text="Done.", call=None):
    delta = {"tool_calls": [{"index": 0, **call}]} if call else {"content": text}
    chunk = {"choices": [{"delta": delta, "finish_reason": "tool_calls" if call else "stop"}]}
    return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")


def insert_call(path="handoffs/h.md"):
    return {"id": "log-1", "type": "function",
            "function": {"name": "insert_activity_log", "arguments": json.dumps({"path": path})}}


def mock_gateway(monkeypatch, gateway):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(gateway)))


async def test_agent_inserts_the_log_as_a_checkpointed_write_with_a_compact_result(runtime, monkeypatch):
    manager, session, project = runtime
    session["permission_mode"] = "acceptEdits"
    handoff = project / "handoffs" / "h.md"
    handoff.write_text(f"# Handoff\n\n## Commands\n{ACTIVITY_MARKER}\n\n{FINAL_LINE}\n")
    requests, hooks = [], []
    run_hooks = manager.run_hooks

    async def record_hooks(session, phase, name, *args, **kwargs):
        hooks.append((phase, name))
        return await run_hooks(session, phase, name, *args, **kwargs)

    async def gateway(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return stream(call=insert_call()) if len(requests) == 1 else stream()

    manager.run_hooks = record_hooks
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Add the activity log to the handoff")

    text = handoff.read_text()
    assert ACTIVITY_MARKER not in text and text.rstrip().endswith(FINAL_LINE)
    assert "## Activity log (generated by the app)" in text
    assert "export GITHUB_TOKEN=[REDACTED] && git push" in text and "ghp_" not in text
    assert "insert_activity_log" not in text
    result = json.loads(requests[1]["messages"][-1]["content"])
    assert result["commands"] == 3 and result["placement"] == "marker"
    assert "git push" not in requests[1]["messages"][-1]["content"]
    event = next(item for item in session["events"] if item.get("name") == "insert_activity_log")
    assert event["state"] == "completed" and event["input"] == {"path": "handoffs/h.md"}
    assert "+## Activity log (generated by the app)" in event["preview"]
    assert [item["path"] for item in manager.checkpoints.list(session["id"])] == ["handoffs/h.md"]
    assert ("before_tool", "write_file") in hooks and ("after_tool", "write_file") in hooks


@pytest.mark.parametrize("mode", ["manual", "plan"])
async def test_declined_or_plan_mode_insert_leaves_the_file_unchanged(runtime, monkeypatch, mode):
    manager, session, project = runtime
    session["permission_mode"] = mode
    handoff = project / "handoffs" / "h.md"
    handoff.write_text(f"# Handoff\n{ACTIVITY_MARKER}\n")
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        return stream(call=insert_call()) if len(requests) == 1 else stream()

    async def deny(session, event):
        return False

    manager.approve = deny
    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Add the activity log")
    assert handoff.read_text() == f"# Handoff\n{ACTIVITY_MARKER}\n"
    event = next(item for item in session["events"] if item.get("name") == "insert_activity_log")
    assert ("declined" in event["output"]) if mode == "manual" else (event["state"] == "rejected")
    assert not manager.checkpoints.list(session["id"])


def test_output_limit_text_states_tokens_and_approximate_file_size():
    assert output_limit_text(20000).startswith(
        "Each of your responses can contain at most 20,000 output tokens, roughly 40 KB of file text")
    assert "at most 8,192 output tokens, roughly 16 KB" in output_limit_text(8192)


async def test_system_prompt_states_the_date_and_that_documents_need_not_be_short(runtime, monkeypatch):
    manager, session, _ = runtime
    manager.settings.values["max_output_tokens"] = 20000
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        return stream()

    mock_gateway(monkeypatch, gateway)
    await manager.run(session, "Hello")
    system = requests[0]["messages"][0]["content"]
    assert re.search(r"Current local date and time: \d{4}-\d{2}-\d{2} \d{2}:\d{2} .*\(UTC[+-]\d{2}:\d{2}\)", system)
    assert "Keep chat replies concise" in system
    assert "conciseness\napplies to chat replies, not to those files" in system
    assert "instead of shortening their" in system
    assert "at most 20,000 output tokens, roughly 40 KB of file text" in system
    assert requests[0]["max_tokens"] == 20000
    assert "insert_activity_log" in [tool["function"]["name"] for tool in requests[0]["tools"]]


def test_shipped_handoff_skill_loads_within_limits_and_uses_the_activity_log(runtime):
    manager, session, project = runtime
    source = Path(__file__).resolve().parents[2] / "skills" / "handoff" / "SKILL.md"
    target = project / ".agents" / "skills" / "handoff" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())
    tools = WorkspaceTools(str(project))
    skill = manager.extensions.load_skill(tools, "handoff")
    assert skill["name"] == "handoff" and "handoff" in skill["description"]
    session["active_skills"] = ["handoff"]
    assert len(manager.skill_text(session, tools).encode()) <= 8000
    assert ACTIVITY_MARKER in skill["text"] and "insert_activity_log" in skill["text"]
    # Part size follows the output limit stated in the system prompt, not a fixed size.
    assert "half the file text the system prompt says one response can hold" in skill["text"]
    assert skill["text"].rstrip().endswith("Reply briefly with the path, the approximate size, and anything you could "
                                           "not recover (for example compacted turns).")
