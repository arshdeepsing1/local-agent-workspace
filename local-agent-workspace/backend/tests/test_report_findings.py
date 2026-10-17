"""M8f2: report_findings, the review skills' tool: its schema limits, when it is offered, that it runs without a card
in every mode (Plan and explore subagents included), the result the model gets, and the transcript and export."""
import json
from pathlib import Path

import httpx
import pytest

from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.feature_tools import FEATURE_TOOLS, FINDINGS_TOOL
from local_agent.permissions import MODE_LABELS, evaluate
from local_agent.portability import export_bundle
from local_agent.store import Store
from local_agent.tool_profiles import READ_TOOLS, tool_allowed
from local_agent.tool_schema import validate_arguments
from local_agent.transcript import transcript

DEFINITION = next(tool["function"] for tool in FEATURE_TOOLS if tool["function"]["name"] == FINDINGS_TOOL)
FINDING = {"path": "src/app.py", "line": 12, "end_line": 14, "severity": "high", "title": "Unchecked index",
           "detail": "items[0] fails on an empty list.", "suggestion": "Return early when items is empty."}
APP_SKILLS = Path(__file__).resolve().parents[2] / "skills"
TOKEN = "dapi" + "0123456789abcdef" * 2  # A token shape, built here so the source holds none.


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "test-token")
    store = Store(settings.state_dir / "findings.sqlite3")
    manager = AgentManager(store, settings)
    manager.extensions.app_skills = APP_SKILLS  # The shipped review skills.
    session = store.create(settings.values)
    session["title_generated"] = True
    store.save(session)
    yield manager, session, project
    store.db.close()


def offered(manager, session):
    return [item["function"]["name"] for item in manager.offered_tools(session, [])[0]]


def test_the_schema_bounds_findings():
    validate_arguments(DEFINITION["parameters"], {"summary": "One issue.", "findings": [FINDING]})
    validate_arguments(DEFINITION["parameters"], {"summary": "", "findings": [{"path": "a.py", "severity": "info",
                                                                                "title": "A question"}]})
    for bad in ({"summary": "x" * 2001, "findings": []}, {"summary": "", "findings": [FINDING] * 31},
                {"summary": "", "findings": [{**FINDING, "title": "t" * 121}]},
                {"summary": "", "findings": [{**FINDING, "detail": "d" * 1001}]},
                {"summary": "", "findings": [{**FINDING, "severity": "critical"}]},
                {"summary": "", "findings": [{**FINDING, "line": 0}]},
                {"summary": "", "findings": [{**FINDING, "extra": 1}]},
                {"summary": "", "findings": [{"path": "a.py", "severity": "low"}]}, {"summary": ""}):
        with pytest.raises(ValueError, match="Invalid tool arguments"):
            validate_arguments(DEFINITION["parameters"], bad)
    assert len(DEFINITION["description"]) <= 400


def test_it_needs_no_card_in_any_mode_and_an_explore_subagent_may_use_it():
    for mode in MODE_LABELS:
        assert evaluate(mode, FINDINGS_TOOL, {"summary": "", "findings": []}).decision == "allow"
    assert FINDINGS_TOOL in READ_TOOLS
    assert tool_allowed({"tool_profile": "read_only"}, FINDINGS_TOOL)
    # A deny rule still turns it off.
    rule = {"tool": FINDINGS_TOOL, "behavior": "deny"}
    assert evaluate("plan", FINDINGS_TOOL, {}, chat_rules=[rule]).decision == "deny"


def test_offered_once_a_skill_that_names_it_is_selected_and_then_kept(runtime):
    manager, session, project = runtime
    assert FINDINGS_TOOL not in offered(manager, session)
    tools = manager.session_tools(session)
    manager.select_skill(session, tools, "verify")  # A skill that does not name it.
    assert FINDINGS_TOOL not in offered(manager, session)
    manager.select_skill(session, tools, "review")
    assert FINDINGS_TOOL in offered(manager, session)
    manager.select_skill(session, tools, "review", enabled=False)
    assert FINDINGS_TOOL in offered(manager, session)  # The tool list stays stable.
    # A project skill that names it counts too.
    other = manager.store.create(manager.settings.values)
    skill = project / ".agents/skills/audit/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Audit the change and report with report_findings.")
    assert manager.skills_name_findings(tools, ["audit"]) and not manager.skills_name_findings(tools, ["debug"])
    manager.select_skill(other, tools, "audit")
    assert FINDINGS_TOOL in offered(manager, other)


def gateway(monkeypatch, replies):
    requests, client = [], httpx.AsyncClient

    async def handler(request):
        requests.append(json.loads(request.content))
        reply = replies[min(len(requests), len(replies)) - 1]
        delta = {"content": reply} if isinstance(reply, str) else {"tool_calls": [{"index": 0, "id": f"call-{len(requests)}",
                                                                                  "type": "function", "function": reply}]}
        chunk = {"choices": [{"delta": delta, "finish_reason": "stop" if isinstance(reply, str) else "tool_calls"}]}
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handler)))
    return requests


async def test_a_review_message_in_plan_mode_records_findings_as_a_card(runtime, monkeypatch):
    manager, session, _ = runtime
    session["permission_mode"] = "plan"
    report = {"summary": f"One bug; token {TOKEN} in a log.",
              "findings": [FINDING, {"path": "README.md", "severity": "info", "title": "Typo"}]}
    requests = gateway(monkeypatch, [{"name": FINDINGS_TOOL, "arguments": json.dumps(report)}, "One high finding."])
    await manager.run(session, "/review the parser")
    assert session["terminal_reason"] == "completed"
    # Selecting /review for the message offers the tool from that turn's first request on.
    assert FINDINGS_TOOL in [tool["function"]["name"] for tool in requests[0]["tools"]]
    card = next(event for event in session["events"] if event.get("name") == FINDINGS_TOOL)
    assert card["state"] == "completed" and "approval" not in card
    assert card["input"]["findings"][0] == FINDING
    assert TOKEN[:12] not in card["input"]["summary"] and "[REDACTED]" in card["input"]["summary"]
    result = next(message for message in requests[1]["messages"] if message["role"] == "tool")["content"]
    assert result.startswith("Recorded 2 findings; the user sees them as a card.")
    text = transcript(session)
    assert "- Review findings (2):" in text and "**high** `src/app.py:12-14` Unchecked index" in text
    assert "Suggestion: Return early when items is empty." in text and "**info** `README.md` Typo" in text
    bundle = export_bundle([session], session["id"])
    exported = next(event for event in bundle["sessions"][0]["events"] if event.get("name") == FINDINGS_TOOL)
    assert exported["input"] == card["input"]
    user = next(event for event in bundle["sessions"][0]["events"] if event["type"] == "user")
    assert user["skills"] == ["review"]


async def test_a_custom_agent_whose_skill_names_it_is_offered_it(tmp_path, monkeypatch):
    """A read-only review agent (an agent file listing the shipped review skill) can report findings."""
    from test_custom_agents import Gateway, calls_reply, card, reply, run_turn, write_agent
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(project), env_file="", model="test-model")
    settings.credentials = lambda: ("https://gateway.example", "synthetic-token")
    store = Store(settings.state_dir / "agents.sqlite3")
    manager = AgentManager(store, settings)
    manager.extensions.app_skills = APP_SKILLS
    parent = store.create(settings.values)
    parent.update(title="Parent", title_generated=True, permission_mode="bypassPermissions")
    store.save(parent)
    write_agent(project, "reviewer", "---\ndescription: Reviews diffs.\nskills: [review]\n---\nReview the diff.\n")
    write_agent(project, "mapper", "---\ndescription: Maps.\nskills: [verify]\n---\nMap it.\n")
    gateway = Gateway(monkeypatch, [lambda: calls_reply(("delegate_task", {"task": "Review", "agent_type": "reviewer"}, "a"),
                                                        ("delegate_task", {"task": "Map", "agent_type": "mapper"}, "b")),
                                    lambda: reply("Done.")])
    try:
        saved = await run_turn(manager, parent)
        tools = {body["messages"][1]["content"].split("\n")[1]: [tool["function"]["name"] for tool in body["tools"]]
                 for _, body in gateway.requests["child"]}
        assert FINDINGS_TOOL in tools["Review"] and FINDINGS_TOOL not in tools["Map"]
        assert manager.store.get(card(saved, "a")["child_session_id"])["findings_tool"] is True
        assert FINDINGS_TOOL not in offered(manager, saved)  # The parent selected no review skill.
    finally:
        await manager.jobs.shutdown()
        store.db.close()


async def test_a_subagents_findings_reach_the_parent_with_its_report(tmp_path, monkeypatch):
    """The parent model sees only the child's report, never its card: the findings are listed in it."""
    from test_custom_agents import REAL_CLIENT, calls_reply, card, reply, run_turn, write_agent
    from local_agent.prompt_cache import unmarked
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(project), env_file="", model="test-model")
    settings.credentials = lambda: ("https://gateway.example", "synthetic-token")
    store = Store(settings.state_dir / "agents.sqlite3")
    manager = AgentManager(store, settings)
    manager.extensions.app_skills = APP_SKILLS
    parent = store.create(settings.values)
    parent.update(title="Parent", title_generated=True, permission_mode="bypassPermissions")
    store.save(parent)
    write_agent(project, "reviewer", "---\ndescription: Reviews diffs.\nskills: [review]\n---\nReview the diff.\n")
    report = {"summary": "One bug.", "findings": [{**FINDING, "detail": f"Logs {TOKEN} too."}]}
    requests = {"parent": [], "child": []}

    async def handler(request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "A title"}, "finish_reason": "stop"}]})
        first = next((m["content"] for m in unmarked(body)["messages"] if m["role"] == "user"), "")
        side = "child" if isinstance(first, str) and first.startswith("Delegated task:\n") else "parent"
        requests[side].append(body)
        if side == "child":
            return calls_reply((FINDINGS_TOOL, report, "f")) if len(requests["child"]) == 1 else reply("Verdict: one high issue.")
        if len(requests["parent"]) == 1:
            return calls_reply(("delegate_task", {"task": "Review the diff", "agent_type": "reviewer"}, "a"))
        return reply("Done.")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(handler)))
    try:
        saved = await run_turn(manager, parent)
        child = manager.store.get(card(saved, "a")["child_session_id"])
        assert next(event for event in child["events"] if event.get("name") == FINDINGS_TOOL)["state"] == "completed"
        told = next(m for m in requests["child"][1]["messages"] if m["role"] == "tool")["content"]
        assert told.startswith("Recorded 1 finding; the parent receives them with your report.")
        delegated = json.loads(next(m for m in requests["parent"][1]["messages"] if m["role"] == "tool")["content"])
        assert delegated["output"].startswith("Findings the subagent reported (1):\n- high src/app.py:12-14: Unchecked index"
                                              " (Logs [REDACTED] too.)\n\nVerdict: one high issue.")
        assert TOKEN[:12] not in json.dumps(delegated)
    finally:
        await manager.jobs.shutdown()
        store.db.close()


def test_the_findings_in_a_subagents_report_are_bounded():
    from local_agent.planning import reported_findings
    events = [{"type": "tool", "name": FINDINGS_TOOL, "state": "completed",
               "input": {"summary": "", "findings": [{**FINDING, "detail": "d" * 1000}] * 30}},
              {"type": "tool", "name": FINDINGS_TOOL, "state": "error", "input": {"findings": [FINDING]}}]
    text = reported_findings(events, limit=1000)
    assert text.startswith("Findings the subagent reported (30):\n") and text.endswith(" more in its transcript\n\n")
    assert len(text) < 1200 and reported_findings(events[1:]) == ""
