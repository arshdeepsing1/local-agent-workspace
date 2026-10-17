"""Structured hook output (one JSON object on stdout) and the lifecycle hook events: user_prompt,
session_start, agent_stop, subagent_start and subagent_stop (lifecycle_hooks.py, extensions.py)."""
import asyncio
import copy
import json
import shlex
import sys
from types import SimpleNamespace

import httpx
import pytest

from local_agent.agents import AgentManager
from local_agent.approvals import busy_status
from local_agent.composer_api import STEER_PREFIX
from local_agent.config import Settings
from local_agent.context import HOOK_CONTINUATION_PREFIX, is_app_prompt
from local_agent.extensions import (CONFIG_LIMIT, HOOK_CONTEXT_CHARS, HOOK_EVENTS, MAX_HOOKS, parse_hook_output,
                                    shown_hook_output, validate_config)
from local_agent.instructions import load_project_instructions
from local_agent.planning import CallAllowance
from local_agent.portability import _validate_wire, export_bundle, fresh_copies, validate_bundle
from local_agent.result_budget import ResultAllowance
from local_agent.store import Store
from local_agent.tools import WorkspaceTools

REAL_CLIENT = httpx.AsyncClient
TOKEN = "synthetic-token"


@pytest.fixture
async def rt(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(project), env_file="", model="test-model")
    settings.credentials = lambda: ("https://gateway.example", TOKEN)
    store = Store(settings.state_dir / "hooks.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session.update(title="Hooks", title_generated=True, permission_mode="bypassPermissions")
    store.save(session)
    manager.live[session["id"]] = session
    tools = WorkspaceTools(str(project))
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance["text"], guidance["warnings"])
    tools.skill_signature = ""
    yield SimpleNamespace(manager=manager, session=session, project=project, tools=tools, tmp=tmp_path)
    for session_id in list(manager.tasks):
        await manager.stop(session_id)
    await manager.jobs.shutdown()
    store.db.close()


def emit(value, record=None, append=False):
    """A hook command that prints value (JSON unless it is text), after saving its stdin to record."""
    text = value if isinstance(value, str) else json.dumps(value)
    prefix = f"cat {'>>' if append else '>'} {shlex.quote(str(record))}; echo >> {shlex.quote(str(record))}; " if record else ""
    return prefix + "printf '%s' " + shlex.quote(text)


def emit_long(field, size, char="y"):
    """A hook command that prints {field: char * size} (too long to spell out in the command)."""
    source = f"import json,sys; sys.stdout.write(json.dumps({{{field!r}: {char!r} * {size}}}))"
    return shlex.quote(sys.executable) + " -c " + shlex.quote(source)


def hook(hook_id, event, command, **extra):
    return {"id": hook_id, "event": event, "command": command, "enabled": True, "timeout_seconds": 10, **extra}


def configure(rt, *hooks):
    rt.manager.extensions.update_config({"hooks": list(hooks)})


def stdin_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def cards(session, name):
    return [event for event in session["events"] if event.get("name") == name]


async def pending_card(manager, name=None):
    for _ in range(500):
        for key in list(manager.pending):
            session = manager.get(key[0])
            event = next((item for item in session["events"] if item["id"] == key[1]), None)
            if event is not None and (name is None or event["name"] == name):
                return key, event
        await asyncio.sleep(0.01)
    raise AssertionError("Expected an approval card")


def frame(delta, finish=None):
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"


def reply(text):
    return httpx.Response(200, text=frame({"content": text}, "stop") + "data: [DONE]\n\n")


def call(name, arguments, call_id="call-1"):
    return calls((name, arguments, call_id))


def calls(*items):
    """One response with several tool calls: (name, arguments, call_id) each."""
    parts = [{"index": index, "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
             for index, (name, arguments, call_id) in enumerate(items)]
    return httpx.Response(200, text=frame({"content": "", "tool_calls": parts}, "tool_calls") + "data: [DONE]\n\n")


class Gateway:
    """Streamed chat completions: the top-level chat's scripted responses in order, and each child's by its
    delegated task (its first message starts with "Delegated task:\\n<task>")."""

    def __init__(self, monkeypatch, responses, children=None):
        self.responses, self.children = responses, children or {}
        self.requests, self.child_requests = [], {}
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(self)))

    async def __call__(self, request):
        body = json.loads(request.content)
        if body.get("stream") is not True:
            return httpx.Response(200, json={"choices": [{"message": {"content": "A title"}, "finish_reason": "stop"}]})
        first = next((m["content"] for m in body["messages"] if m["role"] == "user"), "")
        if isinstance(first, str) and first.startswith("Delegated task:\n"):
            task = first.removeprefix("Delegated task:\n").split("\n\n", 1)[0]
            seen = self.child_requests.setdefault(task, [])
            seen.append(body)
            return self.children[task][min(len(seen), len(self.children[task])) - 1]()
        self.requests.append(body)
        return self.responses[min(len(self.requests), len(self.responses)) - 1]()


def last_user(body):
    return next(message["content"] for message in reversed(body["messages"]) if message["role"] == "user")


# Structured output: parsing -----------------------------------------------------------------------

def test_only_one_whole_json_object_is_structured_output():
    assert parse_hook_output("before_tool", "looks fine") is None
    assert parse_hook_output("before_tool", '["deny"]') is None
    assert parse_hook_output("before_tool", '{"decision": "deny"') is None  # Malformed: plain text, as before.
    assert parse_hook_output("before_tool", 'note {"decision": "deny"}') is None
    assert parse_hook_output("before_tool", '{"decision": "deny"}', truncated=True) is None
    parsed = parse_hook_output("before_tool", '  {"decision": "deny", "reason": "Not on main", "extra": 1}\n')
    assert parsed == {"decision": "deny", "reason": "Not on main", "ignored": ["extra"]}
    assert parse_hook_output("before_tool", '{"decision": "ask"}')["reason"] == "The hook gave no reason."
    # allow is no objection; it never skips a card the mode or rules require.
    assert parse_hook_output("before_tool", '{"decision": "allow"}') == {"ignored": []}
    # Fields of another event are ignored and listed.
    assert parse_hook_output("after_tool", '{"decision": "deny", "context": "ok"}') == {"context": "ok", "ignored": ["decision"]}
    assert parse_hook_output("user_prompt", '{"prompt": "new"}')["prompt"] == "new"
    assert parse_hook_output("subagent_stop", '{"decision": "block", "report": "r"}')["report"] == "r"


@pytest.mark.parametrize("event,text,message", [
    ("before_tool", '{"decision": "block"}', "unsupported decision"),
    ("agent_stop", '{"decision": "deny"}', "unsupported decision"),
    ("before_tool", '{"arguments": [1]}', "arguments must be a JSON object"),
    ("after_tool", '{"context": 5}', "context must be text"),
    ("user_prompt", '{"prompt": "  "}', "prompt must not be empty"),
])
def test_a_known_field_with_a_wrong_value_is_an_error(event, text, message):
    with pytest.raises(ValueError, match=message):
        parse_hook_output(event, text)


def test_context_and_reason_are_bounded():
    parsed = parse_hook_output("before_tool", json.dumps({"context": "x" * 5000, "decision": "deny", "reason": "y" * 5000}))
    assert len(parsed["context"]) == len(parsed["reason"]) == HOOK_CONTEXT_CHARS
    assert parsed["context"].endswith("[…shortened by the app]")


async def test_run_hook_reads_redacted_json_and_refuses_oversized_json(rt):
    configured = hook("check", "after_tool", emit({"context": f"token {TOKEN}"}))
    configure(rt, configured)
    result = await rt.manager.extensions.run_hook(configured, {}, rt.project)
    assert result["structured"] == {"context": "token [REDACTED]", "ignored": []}
    assert TOKEN not in result["output"]
    failing = hook("check", "after_tool", emit({"context": "x"}) + "; exit 3")
    configure(rt, failing)
    assert "structured" not in await rt.manager.extensions.run_hook(failing, {}, rt.project)
    big = hook("check", "after_tool", emit_long("context", 20_000))
    configure(rt, big)
    result = await rt.manager.extensions.run_hook(big, {}, rt.project)
    assert "structured" not in result and "longer than 16 KB" in result["structured_error"]


# Configuration -------------------------------------------------------------------------------------

def test_new_events_validate_and_the_limit_is_sixteen_hooks():
    for event in HOOK_EVENTS:
        assert validate_config({"hooks": [hook("a", event, "true")]})["hooks"][0]["event"] == event
    hooks = [hook(f"h{index}", HOOK_EVENTS[index % len(HOOK_EVENTS)], "true") for index in range(MAX_HOOKS)]
    assert MAX_HOOKS == 16 and len(validate_config({"hooks": hooks})["hooks"]) == 16
    with pytest.raises(ValueError, match="at most 16 hooks"):
        validate_config({"hooks": [*hooks, hook("extra", "agent_stop", "true")]})
    with pytest.raises(ValueError, match="agent_stop, subagent_start, subagent_stop, before_compaction or notification"):
        validate_config({"hooks": [hook("a", "on_stop", "true")]})
    with pytest.raises(ValueError, match="32 KB"):
        validate_config({"hooks": [hook(f"h{index}", "agent_stop", "x" * 4000) for index in range(MAX_HOOKS)]})
    assert CONFIG_LIMIT == 32_000


# before_tool: decisions ----------------------------------------------------------------------------

async def test_a_deny_decision_blocks_the_tool_and_the_model_sees_the_reason(rt):
    configure(rt, hook("guard", "before_tool", emit({"decision": "deny", "reason": "Main is frozen."})))
    output = await rt.manager.execute_tool(rt.session, rt.tools, "write_file", {"path": "a.txt", "content": "x"}, "w")
    assert output.startswith("Blocked by the before-tool hook guard: Main is frozen.")
    assert not (rt.project / "a.txt").exists()
    tool = next(event for event in rt.session["events"] if event.get("call_id") == "w")
    assert tool["state"] == "rejected" and tool["hook_blocked"] == {"hook_id": "guard", "reason": "Main is frozen."}
    card = cards(rt.session, "hook_before_tool")[0]
    assert card["state"] == "completed" and card["hook_result"]["decision"] == "deny"


@pytest.mark.parametrize("mode", ["auto", "bypassPermissions"])
async def test_an_ask_decision_forces_a_card_even_where_the_mode_allows_the_tool(rt, mode):
    rt.session.update(permission_mode=mode, allow_rules=[{"tool": "hook", "hook": "ask"}])  # The hook itself runs.
    configure(rt,hook("ask", "before_tool", emit({"decision": "ask", "reason": "Review writes to docs."})))
    task = asyncio.create_task(rt.manager.execute_tool(rt.session, rt.tools, "write_file",
                                                       {"path": "a.txt", "content": "x"}, "w"))
    key, event = await pending_card(rt.manager, "write_file")
    assert event["hook_ask"] == {"hook_id": "ask", "reason": "Review writes to docs."}
    assert event["rememberable"] == []
    with pytest.raises(ValueError, match="hook asked for this approval"):
        rt.manager.decide(*key, True, remember=True)
    # A switch to Bypass does not approve a card a hook asked for.
    await rt.manager.change_mode(rt.session, "bypassPermissions")
    assert key in rt.manager.pending and not (rt.project / "a.txt").exists()
    rt.manager.decide(*key, True)
    await asyncio.wait_for(task, 5)
    assert (rt.project / "a.txt").read_text() == "x"


async def test_an_ask_decision_declined_does_not_run_and_dont_ask_refuses_it(rt):
    rt.session.update(permission_mode="auto", allow_rules=[{"tool": "hook", "hook": "ask"}])
    configure(rt,hook("ask", "before_tool", emit({"decision": "ask", "reason": "Check first."})))
    task = asyncio.create_task(rt.manager.execute_tool(rt.session, rt.tools, "write_file",
                                                       {"path": "a.txt", "content": "x"}, "w"))
    key, _ = await pending_card(rt.manager, "write_file")
    rt.manager.decide(*key, False)
    assert (await asyncio.wait_for(task, 5)).startswith("User declined this action.")
    assert not (rt.project / "a.txt").exists()
    # Don't ask with a rule that allows the hook: the hook asks, and Don't ask never shows a card.
    rt.session.update(permission_mode="dontAsk", allow_rules=[{"tool": "hook", "hook": "ask"}, {"tool": "edits"}])
    output = await asyncio.wait_for(rt.manager.execute_tool(rt.session, rt.tools, "write_file",
                                                            {"path": "a.txt", "content": "x"}, "w2"), 5)
    assert output.startswith("Not run: a before-tool hook (ask) asked for your approval, which Don't ask mode does not show.")
    assert not rt.manager.pending and not (rt.project / "a.txt").exists()


async def test_an_ask_decision_on_a_read_shows_a_plain_card(rt):
    (rt.project / "notes.txt").write_text("private\n")
    configure(rt, hook("ask", "before_tool", emit({"decision": "ask", "reason": "Reads of notes need a look."}),
                       tools=["read_file"]))
    task = asyncio.create_task(rt.manager.execute_tool(rt.session, rt.tools, "read_file", {"path": "notes.txt"}, "r"))
    key, event = await pending_card(rt.manager, "read_file")
    assert '"path": "notes.txt"' in event["preview"]
    rt.manager.decide(*key, False)
    output = await asyncio.wait_for(task, 5)
    assert "private" not in output and output.startswith("User declined this action.")


# before_tool: replaced arguments -------------------------------------------------------------------

async def test_a_hook_cannot_turn_an_allowed_command_into_another_without_a_new_card(rt):
    # Auto runs pwd (read-only list) without a card; the hook's replacement is checked on its own.
    rt.session.update(permission_mode="auto", allow_rules=[{"tool": "hook", "hook": "swap"}])
    configure(rt,hook("swap", "before_tool", emit({"arguments": {"command": "touch pwned"}}), tools=["run_command"]))
    task = asyncio.create_task(rt.manager.execute_tool(rt.session, rt.tools, "run_command", {"command": "pwd"}, "c"))
    key, event = await pending_card(rt.manager, "run_command")
    assert event["input"] == {"command": "touch pwned"}  # The card shows what would run.
    assert event["hook_change"] == {"hooks": ["swap"], "original": {"command": "pwd"}}
    assert event["preview"].startswith("touch pwned")
    hook_card = cards(rt.session, "hook_before_tool")[0]
    assert hook_card["hook_result"]["arguments"] == {"before": {"command": "pwd"}, "after": {"command": "touch pwned"}}
    rt.manager.decide(*key, False)
    await asyncio.wait_for(task, 5)
    assert not (rt.project / "pwned").exists()


async def test_an_allow_rule_for_the_original_command_does_not_cover_the_replacement(rt):
    rt.session.update(permission_mode="manual", allow_rules=[{"tool": "run_command", "command": "echo hi"},
                                                             {"tool": "hook", "hook": "swap"}])
    configure(rt, hook("swap", "before_tool", emit({"arguments": {"command": "echo bye > out.txt"}})))
    task = asyncio.create_task(rt.manager.execute_tool(rt.session, rt.tools, "run_command", {"command": "echo hi"}, "c"))
    key, event = await pending_card(rt.manager, "run_command")
    assert event["input"]["command"] == "echo bye > out.txt"
    rt.manager.decide(*key, True)
    output = await asyncio.wait_for(task, 10)
    assert (rt.project / "out.txt").read_text() == "bye\n"
    # The model is told the arguments changed and what ran; the card keeps the plain result.
    assert output.startswith("[A before-tool hook (swap) changed this call's arguments before it ran. It ran with: "
                             '{"command": "echo bye > out.txt"}. Your proposed arguments did not run.]')
    tool = next(event for event in rt.session["events"] if event.get("call_id") == "c")
    assert tool["model_output"] == output and not tool["output"].startswith("[A before-tool hook")


async def test_replaced_arguments_must_match_the_schema_and_the_rules(rt):
    configure(rt, hook("bad", "before_tool", emit({"arguments": {"path": 5}})))
    output = await rt.manager.execute_tool(rt.session, rt.tools, "write_file", {"path": "a.txt", "content": "x"}, "w")
    assert "Before-tool hook blocked write_file: Hook output could not be used" not in output
    assert "Before-tool hook blocked write_file" in output and "not valid for write_file" in output
    assert not (rt.project / "a.txt").exists()
    # A deny rule applies to the final arguments, in Bypass too.
    rt.session["allow_rules"] = [{"behavior": "deny", "tool": "run_command", "prefix": ["rm"]}]
    configure(rt, hook("swap", "before_tool", emit({"arguments": {"command": "rm -rf build"}})))
    output = await rt.manager.execute_tool(rt.session, rt.tools, "run_command", {"command": "echo hi"}, "c")
    note, _, refusal = output.partition("\n")
    assert note == ("[A before-tool hook (swap) changed this call's arguments to: {\"command\": \"rm -rf build\"}. The "
                    "changed call did not run; the result below is about it. Your proposed arguments did not run either.]")
    assert refusal.startswith("Blocked by a deny rule for this chat: commands starting with rm")


async def test_file_guards_apply_to_the_replaced_path(rt):
    (rt.project / "kept.txt").write_text("keep me\n")  # Never read in this chat.
    configure(rt, hook("swap", "before_tool", emit({"arguments": {"path": "kept.txt", "content": "gone"}})))
    output = await rt.manager.execute_tool(rt.session, rt.tools, "write_file", {"path": "new.txt", "content": "gone"}, "w")
    assert "kept.txt" in output and "read" in output.lower()
    assert (rt.project / "kept.txt").read_text() == "keep me\n" and not (rt.project / "new.txt").exists()


async def test_hooks_chain_and_a_later_hook_sees_the_earlier_arguments(rt):
    record = rt.tmp / "second.json"
    configure(rt, hook("first", "before_tool", emit({"arguments": {"path": "b.txt", "content": "from hook"}})),
              hook("second", "before_tool", emit({}, record)))
    await rt.manager.execute_tool(rt.session, rt.tools, "write_file", {"path": "a.txt", "content": "x"}, "w")
    assert stdin_lines(record)[0]["arguments"] == {"path": "b.txt", "content": "from hook"}
    assert (rt.project / "b.txt").read_text() == "from hook" and not (rt.project / "a.txt").exists()


async def test_verify_and_the_activity_log_keep_the_apps_arguments(rt):
    configure(rt, hook("swap", "before_tool", emit({"arguments": {"command": "touch pwned"}, "context": "c"})))
    output = await rt.manager.execute_tool(rt.session, rt.tools, "run_command", {"command": "true"}, None, origin="verify")
    assert json.loads(output)["state"] == "completed" and not (rt.project / "pwned").exists()
    assert cards(rt.session, "hook_before_tool")[0]["hook_result"]["ignored"] == ["arguments"]


# Context -------------------------------------------------------------------------------------------

async def test_context_reaches_the_model_labelled_redacted_and_bounded(rt):
    configure(rt, hook("pre", "before_tool", emit({"context": f"Style guide applies; token {TOKEN}"})),
              hook("post", "after_tool", emit_long("context", 5000)),
              hook("plain", "after_tool", "echo plain text output"))
    output = await rt.manager.execute_tool(rt.session, rt.tools, "write_file", {"path": "a.txt", "content": "x"}, "w")
    head, _, notes = output.partition("\n\n[Hook context, added by the user's local hook commands (not the tool's output):]\n")
    assert "+x" in head and TOKEN not in output
    assert notes.startswith("- before_tool hook pre: Style guide applies; token [REDACTED]\n- after_tool hook post: yyy")
    assert len(notes) < 2 * HOOK_CONTEXT_CHARS
    tool = next(event for event in rt.session["events"] if event.get("call_id") == "w")
    assert tool["model_output"] == output and "Hook context" not in tool["output"]
    assert cards(rt.session, "hook_after_tool")[1].get("hook_result") is None  # Plain text: no structured result.


async def test_failure_hook_context_is_added_to_the_error(rt):
    configure(rt, hook("why", "tool_failure", emit({"context": "Run make deps first."})))
    output = await rt.manager.execute_tool(rt.session, rt.tools, "run_command", {"command": "exit 3"}, "c")
    assert output.endswith("- tool_failure hook why: Run make deps first.") and '"exit_code": 3' in output


async def test_plan_mode_skips_hooks_and_a_deny_rule_blocks_one(rt):
    rt.session["permission_mode"] = "plan"
    configure(rt, hook("guard", "before_tool", emit({"decision": "deny", "reason": "no"})))
    await rt.manager.execute_tool(rt.session, rt.tools, "list_files", {"path": "."}, "l")
    assert not cards(rt.session, "hook_before_tool")


# user_prompt ---------------------------------------------------------------------------------------

async def test_a_user_prompt_block_sends_nothing_and_says_why(rt, monkeypatch):
    gateway = Gateway(monkeypatch, [lambda: reply("Hi")])
    configure(rt, hook("guard", "user_prompt", emit({"decision": "block", "reason": "No customer data in prompts."})))
    await rt.manager.run(rt.session, "Customer 42 owes money")
    assert gateway.requests == [] and rt.session["wire"] == []
    user = next(event for event in rt.session["events"] if event["type"] == "user")
    assert user["in_wire"] is False and user["hook_blocked"]["reason"] == "No customer data in prompts."
    assert rt.session["events"][-1]["text"] == ("A user-prompt hook (guard) blocked this message: No customer data in "
                                                "prompts. It was not sent to the agent.")


async def test_a_user_prompt_edit_and_context_reach_the_model(rt, monkeypatch):
    gateway = Gateway(monkeypatch, [lambda: reply("Done")])
    record = rt.tmp / "prompt.json"
    configure(rt, hook("tidy", "user_prompt", emit({"prompt": "Fix the parser bug", "context": "Ticket PRJ-7."}, record)))
    await rt.manager.run(rt.session, "fix teh parser bug pls")
    stdin = stdin_lines(record)[0]
    assert stdin["event"] == "user_prompt" and stdin["prompt"] == "fix teh parser bug pls"
    assert stdin["session_id"] == rt.session["id"] and stdin["permission_mode"] == "bypassPermissions"
    sent = last_user(gateway.requests[0])
    assert sent.startswith("Fix the parser bug\n\n[App note: context from the user's local hook commands")
    assert sent.endswith("- user_prompt hook tidy: Ticket PRJ-7.")
    user = next(event for event in rt.session["events"] if event["type"] == "user")
    assert user["text"] == "Fix the parser bug" and user["in_wire"] is True
    assert user["hook_edit"] == {"hooks": ["tidy"], "original": "fix teh parser bug pls"}
    assert user["hook_notes"] == [{"event": "user_prompt", "hook_id": "tidy", "context": "Ticket PRJ-7."}]


async def test_user_prompt_hooks_follow_the_permission_model(rt, monkeypatch):
    Gateway(monkeypatch, [lambda: reply("Done")])
    rt.session["permission_mode"] = "manual"
    configure(rt, hook("tidy", "user_prompt", emit({"prompt": "edited"})))
    task = asyncio.create_task(rt.manager.run(rt.session, "original"))
    key, event = await pending_card(rt.manager, "hook_user_prompt")
    assert event["input"] == {"hook_id": "tidy", "event": "user_prompt"}
    rt.manager.decide(*key, False)  # Declined: a warning, and the message is sent unchanged.
    await asyncio.wait_for(task, 10)
    assert rt.session["wire"][0]["content"] == "original"
    assert any(item.get("text", "").startswith("User-prompt hook warning: User declined this hook.")
               for item in rt.session["events"])
    # A deny rule blocks the hook without a notice; an allow rule runs it without a card.
    rt.session["allow_rules"] = [{"behavior": "deny", "tool": "hook", "hook": "tidy"}]
    await asyncio.wait_for(rt.manager.run(rt.session, "second"), 10)
    assert cards(rt.session, "hook_user_prompt")[-1]["approval"] == "blocked"
    assert rt.session["wire"][-2]["content"] == "second"
    rt.session["allow_rules"] = [{"tool": "hook", "hook": "tidy"}]
    await asyncio.wait_for(rt.manager.run(rt.session, "third"), 10)
    assert cards(rt.session, "hook_user_prompt")[-1]["allowed_by_rule"] is True
    assert rt.session["wire"][-2]["content"] == "edited"
    # Plan mode skips hooks.
    rt.session["permission_mode"] = "plan"
    count = len(cards(rt.session, "hook_user_prompt"))
    await asyncio.wait_for(rt.manager.run(rt.session, "fourth"), 10)
    assert len(cards(rt.session, "hook_user_prompt")) == count and rt.session["wire"][-2]["content"] == "fourth"


# session_start -------------------------------------------------------------------------------------

async def test_session_start_runs_once_before_the_first_request(rt, monkeypatch):
    gateway = Gateway(monkeypatch, [lambda: reply("One"), lambda: reply("Two")])
    record = rt.tmp / "start.json"
    configure(rt, hook("start", "session_start", emit({"context": "Branch: feature/x"}, record, append=True)))
    await rt.manager.run(rt.session, "First")
    await rt.manager.run(rt.session, "Second")
    assert len(stdin_lines(record)) == 1 and stdin_lines(record)[0]["event"] == "session_start"
    first = last_user(gateway.requests[0])
    assert first.startswith("First\n\n[App note") and "- session_start hook start: Branch: feature/x" in first
    assert last_user(gateway.requests[1]) == "Second"
    assert "Branch: feature/x" not in gateway.requests[0]["messages"][0]["content"]  # Never the system prompt.


async def test_a_blocked_first_message_leaves_session_start_for_the_next(rt, monkeypatch):
    gateway = Gateway(monkeypatch, [lambda: reply("Hi")])
    configure(rt, hook("guard", "user_prompt", "case \"$(cat)\" in *secret*) printf '{\"decision\":\"block\"}';; esac"),
              hook("start", "session_start", emit({"context": "Started."})))
    await rt.manager.run(rt.session, "a secret")
    assert not cards(rt.session, "hook_session_start")
    await rt.manager.run(rt.session, "hello")
    assert "- session_start hook start: Started." in last_user(gateway.requests[0])


# agent_stop ----------------------------------------------------------------------------------------

async def test_agent_stop_block_continues_the_turn_with_a_hidden_message(rt, monkeypatch):
    gateway = Gateway(monkeypatch, [lambda: reply("Done?"), lambda: reply("Now tests pass.")])
    record = rt.tmp / "stop.json"
    script = rt.tmp / "stop.sh"
    script.write_text(f"cat >> {shlex.quote(str(record))}; echo >> {shlex.quote(str(record))}\n"
                      f"if [ $(wc -l < {shlex.quote(str(record))}) -le 1 ]; then "
                      "printf '%s' '{\"decision\": \"block\", \"reason\": \"Run the tests first.\"}'; fi\n")
    configure(rt, hook("stop", "agent_stop", f"sh {shlex.quote(str(script))}"))
    await rt.manager.run(rt.session, "Fix it")
    assert len(gateway.requests) == 2
    hidden = last_user(gateway.requests[1])
    assert hidden.startswith(HOOK_CONTINUATION_PREFIX) and "Run the tests first." in hidden
    assert is_app_prompt({"role": "user", "content": hidden})
    runs = stdin_lines(record)
    assert [run["continuations"] for run in runs] == [0, 1] and runs[0]["reply"] == "Done?"
    notices = [event["text"] for event in rt.session["events"] if event["type"] == "notice"]
    assert "A hook asked the agent to continue: Run the tests first. (1 of 3)" in notices
    # The hidden message is not a visible message of the chat.
    assert [event["text"] for event in rt.session["events"] if event["type"] == "user"] == ["Fix it"]
    assert rt.session["terminal_reason"] == "completed"


async def test_agent_stop_continues_at_most_three_times_and_respects_the_step_limit(rt, monkeypatch):
    gateway = Gateway(monkeypatch, [lambda: reply("Again")])
    configure(rt, hook("stop", "agent_stop", emit({"decision": "block", "reason": "Keep going."})))
    await rt.manager.run(rt.session, "Work")
    assert len(gateway.requests) == 4
    assert rt.session["events"][-1]["text"] == ("A hook asked the agent to continue again: Keep going. The turn ended "
                                                "anyway: hooks can continue one turn at most 3 times in a row.")
    rt.manager.settings.values["max_agent_steps"] = 1
    await rt.manager.run(rt.session, "Once more")
    assert len(gateway.requests) == 5
    assert rt.session["events"][-1]["text"].endswith("No agent steps are left in this turn, so it ended.")


async def test_agent_stop_does_not_run_after_an_error(rt, monkeypatch):
    Gateway(monkeypatch, [lambda: httpx.Response(400, json={"message": "bad request"})])
    record = rt.tmp / "stop.json"
    configure(rt, hook("stop", "agent_stop", emit({}, record)))
    await rt.manager.run(rt.session, "Work")
    assert not record.exists() and rt.session["terminal_reason"] != "completed"


# Subagents -----------------------------------------------------------------------------------------

async def test_subagent_start_context_and_subagent_stop_block_and_report(rt, monkeypatch):
    rt.session["permission_mode"] = "acceptEdits"
    rt.session["allow_rules"] = [{"tool": "delegate_task"}, {"tool": "hook", "hook": "brief"}, {"tool": "hook", "hook": "review"}]
    gateway = Gateway(monkeypatch, [lambda: call("delegate_task", {"task": "Survey", "agent_type": "explore"}, "d1"),
                                    lambda: reply("Parent done")],
                      children={"Survey": [lambda: reply("First draft"), lambda: reply("Final findings")]})
    record = rt.tmp / "review.json"
    script = rt.tmp / "review.sh"
    script.write_text(f"cat >> {shlex.quote(str(record))}; echo >> {shlex.quote(str(record))}\n"
                      f"if [ $(wc -l < {shlex.quote(str(record))}) -le 1 ]; then "
                      "printf '%s' '{\"decision\": \"block\", \"reason\": \"Cite files.\"}'; "
                      "else printf '%s' '{\"report\": \"Short report by the hook.\"}'; fi\n")
    configure(rt, hook("brief", "subagent_start", emit({"context": "Use the docs/ folder."})),
              hook("review", "subagent_stop", f"sh {shlex.quote(str(script))}"))
    await rt.manager.run(rt.session, "Delegate")
    child_first = gateway.child_requests["Survey"][0]
    assert last_user(child_first).endswith("- subagent_start hook brief: Use the docs/ folder.")
    second = last_user(gateway.child_requests["Survey"][1])
    assert second.startswith(HOOK_CONTINUATION_PREFIX) and "subagent_stop hook review" in second and "Cite files." in second
    runs = stdin_lines(record)
    assert runs[0]["reply"] == "First draft" and runs[0]["task"] == "Survey" and runs[0]["agent_type"] == "explore"
    # The hook cards are in the parent chat, where the delegation is.
    assert cards(rt.session, "hook_subagent_start") and len(cards(rt.session, "hook_subagent_stop")) == 2
    result = json.loads(next(event for event in rt.session["events"] if event.get("call_id") == "d1")["output"])
    assert result["output"] == ("[Report replaced by the subagent_stop hook review (a local command); the subagent's "
                                "own final reply is in its transcript.]\nShort report by the hook.")
    child = rt.manager.store.get(result["child_session_id"])
    assert "A hook asked the subagent to continue: Cite files. (1 of 2)" in [e.get("text") for e in child["events"]]
    assert child["events"][0]["hook_notes"][0]["context"] == "Use the docs/ folder."


@pytest.mark.parametrize("decision", ["edit", "block"])
async def test_user_prompt_hooks_also_check_a_message_steered_into_the_turn(rt, monkeypatch, decision):
    rt.session.update(permission_mode="manual", allow_rules=[{"tool": "hook", "hook": "tidy"}])
    gateway = Gateway(monkeypatch, [lambda: call("write_file", {"path": "a.txt", "content": "x"}, "w"),
                                    lambda: reply("Done.")])
    output = {"edit": {"prompt": "Add a date line"}, "block": {"decision": "block", "reason": "Not now."}}[decision]
    configure(rt, hook("tidy", "user_prompt", "if grep -q '\"steered\": true'; then printf '%s' "
                       + shlex.quote(json.dumps(output)) + "; fi"))
    sid = rt.session["id"]
    rt.manager.start(sid, "Write a.txt")
    key, _ = await pending_card(rt.manager, "write_file")
    assert (await rt.manager.submit_message(sid, "also a date pls"))["queued"] is True
    rt.manager.decide(*key, True)
    await asyncio.wait_for(rt.manager.tasks[sid], 10)
    live = rt.manager.get(sid)
    steered = [event for event in live["events"] if event["type"] == "user"][-1]
    last = gateway.requests[1]["messages"][-1]
    if decision == "edit":
        assert last["content"] == STEER_PREFIX + "Add a date line"
        assert steered["text"] == "Add a date line" and steered["in_wire"] is True and steered["steered"] is True
        assert steered["hook_edit"]["original"] == "also a date pls"
    else:
        assert last["role"] == "tool" and steered["in_wire"] is False and steered["hook_blocked"]["reason"] == "Not now."
    assert live.get("queued_messages") == [] and live["terminal_reason"] == "completed"
    _validate_wire(live["wire"], complete=True)
    if decision == "block":  # Shown where it was blocked, with the reason after it.
        place = live["events"].index(steered)
        assert live["events"][place + 1]["text"].startswith("A user-prompt hook (tidy) blocked this message: Not now.")


# Review fixes: stdout only, malformed JSON, redaction after decoding -------------------------------

async def test_stderr_never_spoils_a_json_decision_and_shows_after_stdout(rt):
    configure(rt, hook("guard", "before_tool",
                       emit({"decision": "deny", "reason": "Main is frozen."}) + "; echo 'warning: x' >&2"))
    output = await rt.manager.execute_tool(rt.session, rt.tools, "write_file", {"path": "a.txt", "content": "x"}, "w")
    assert output.startswith("Blocked by the before-tool hook guard: Main is frozen.")
    assert not (rt.project / "a.txt").exists()
    card = cards(rt.session, "hook_before_tool")[0]
    assert card["hook_result"]["decision"] == "deny"
    assert card["output"] == '{"decision": "deny", "reason": "Main is frozen."}\n[stderr]\nwarning: x\n'
    # A failing hook's warning shows its stderr too.
    configure(rt, hook("post", "after_tool", "echo partial; echo 'missing tool' >&2; exit 2"))
    await rt.manager.execute_tool(rt.session, rt.tools, "list_files", {"path": "."}, "l")
    warning = [event["text"] for event in rt.session["events"] if event["type"] == "notice"][-1]
    assert warning.startswith("After-tool hook warning: Hook failed (exit 2): partial\n[stderr]\nmissing tool")
    # Both long: stderr keeps about half of what a card shows.
    text, shortened = shown_hook_output("o" * 10_000, "e" * 10_000, 3_800)
    assert shortened and len(text.encode()) <= 3_800 and "\n[stderr]\n" in text and text.count("e") > 1_500


async def test_output_that_looks_like_one_json_object_but_does_not_decode_fails_the_hook(rt):
    with pytest.raises(ValueError, match=r"looks like a JSON object but is not valid JSON \(Expecting"):
        parse_hook_output("before_tool", '{"decision": deny}')
    assert parse_hook_output("before_tool", '{"decision": "deny"') is None  # Cut off: plain text, as before.
    configure(rt, hook("guard", "before_tool", "printf '%s' '{decision: deny}'"))
    output = await rt.manager.execute_tool(rt.session, rt.tools, "write_file", {"path": "a.txt", "content": "x"}, "w")
    assert ("Before-tool hook blocked write_file: Hook output could not be used: Hook output looks like a JSON object "
            "but is not valid JSON (Expecting property name") in output
    assert not (rt.project / "a.txt").exists()
    # Any other hook: a warning, and nothing changes.
    configure(rt, hook("post", "after_tool", "printf '%s' '{context: 1}'"))
    output = await rt.manager.execute_tool(rt.session, rt.tools, "list_files", {"path": "."}, "l")
    assert "Hook context" not in output
    assert rt.session["events"][-1]["text"].startswith("After-tool hook warning: Hook output could not be used")


async def test_a_json_escaped_secret_is_redacted_after_decoding(rt, monkeypatch):
    gateway = Gateway(monkeypatch, [lambda: reply("Done")])
    escaped = "\\u" + format(ord(TOKEN[0]), "04x") + TOKEN[1:]  # Decodes to TOKEN; the raw text does not contain it.
    configure(rt, hook("ctx", "user_prompt", "printf '%s' " + shlex.quote('{"context": "key ' + escaped + '"}')))
    await rt.manager.run(rt.session, "hello")
    sent = last_user(gateway.requests[0])
    assert sent.endswith("- user_prompt hook ctx: key [REDACTED]")
    card = cards(rt.session, "hook_user_prompt")[0]
    user = next(event for event in rt.session["events"] if event["type"] == "user")
    assert card["hook_result"]["context"] == "key [REDACTED]" and user["hook_notes"][0]["context"] == "key [REDACTED]"
    # The card shows stdout re-encoded, without the escape (its preview shows the configured command).
    assert card["output"] == '{"context": "key [REDACTED]"}'
    # Replacement arguments are redacted the same way.
    configure(rt, hook("swap", "before_tool",
                       "printf '%s' " + shlex.quote('{"arguments": {"path": "a.txt", "content": "' + escaped + '"}}')))
    await rt.manager.execute_tool(rt.session, rt.tools, "write_file", {"path": "a.txt", "content": "x"}, "w")
    assert (rt.project / "a.txt").read_text() == "[REDACTED]"
    assert TOKEN not in json.dumps(rt.session["events"])


# Review fixes: a subagent chat the user continues --------------------------------------------------

async def test_a_subagent_chat_the_user_continues_runs_its_own_hooks_not_subagent_stop(rt, monkeypatch):
    # Manual, with no rule for the subagent_stop hook in the parent: a card there would leave the idle parent busy.
    rt.session.update(permission_mode="manual", allow_rules=[{"tool": "delegate_task"}, {"tool": "hook", "hook": "tidy"},
                                                             {"tool": "hook", "hook": "done"}])
    review, tidy, done = (rt.tmp / f"{name}.json" for name in ("review", "tidy", "done"))
    Gateway(monkeypatch, [lambda: call("delegate_task", {"task": "Survey", "agent_type": "general"}, "d1"),
                          lambda: reply("Parent done")],
            children={"Survey": [lambda: reply("Findings"), lambda: reply("Again")]})
    configure(rt, hook("review", "subagent_stop", emit({}, review, append=True)),
              hook("tidy", "user_prompt", emit({}, tidy, append=True)),
              hook("done", "agent_stop", emit({}, done, append=True)))
    sid = rt.session["id"]
    rt.manager.start(sid, "Delegate")
    key, _ = await pending_card(rt.manager, "hook_subagent_stop")  # The delegated turn: in the parent.
    assert key[0] == sid
    rt.manager.decide(*key, True)
    await asyncio.wait_for(rt.manager.tasks[sid], 10)
    parent = rt.manager.store.get(sid)
    child_id = json.loads(next(event for event in parent["events"] if event.get("call_id") == "d1")["output"])["child_session_id"]
    assert len(stdin_lines(review)) == 1 and stdin_lines(review)[0]["subagent_session_id"] == child_id
    # The delegated prompt got no user_prompt hook; the parent's own message and turn end did.
    assert [line["session_id"] for line in stdin_lines(tidy)] == [sid]
    assert [line["session_id"] for line in stdin_lines(done)] == [sid]
    before = rt.manager.store.get(sid)
    await rt.manager.submit_message(child_id, "One more thing")
    await asyncio.wait_for(rt.manager.tasks[child_id], 10)
    assert not rt.manager.pending and len(stdin_lines(review)) == 1
    assert stdin_lines(tidy)[-1]["session_id"] == child_id and stdin_lines(tidy)[-1]["prompt"] == "One more thing"
    assert stdin_lines(done)[-1]["session_id"] == child_id and stdin_lines(done)[-1]["reply"] == "Again"
    # The idle parent is left alone: not busy, and its stored state is as it was.
    after = rt.manager.store.get(sid)
    assert busy_status(rt.manager, sid) == "idle" and after["events"] == before["events"]
    child = rt.manager.store.get(child_id)
    assert child["turn_delegated"] is False and child["terminal_reason"] == "completed"
    # A copy keeps the subagent named on its parent's hook card (the copy's ID).
    bundle = export_bundle([after, child], sid)
    copies, _ = fresh_copies(bundle, str(rt.project))
    copied_parent, copied_child = copies
    card = next(event for event in copied_parent["events"] if event.get("name") == "hook_subagent_stop")
    assert card["input"]["subagent_session_id"] == copied_child["id"] != child_id


async def test_a_subagent_stopped_while_its_hook_card_waits_leaves_the_parent_delegating(rt, monkeypatch):
    rt.session.update(permission_mode="manual", allow_rules=[{"tool": "delegate_task"}])
    Gateway(monkeypatch, [lambda: calls(("delegate_task", {"task": "A", "agent_type": "general"}, "d1"),
                                        ("delegate_task", {"task": "B", "agent_type": "general"}, "d2")),
                          lambda: reply("Parent done")],
            children={"A": [lambda: reply("Findings A")],
                      "B": [lambda: call("write_file", {"path": "b.txt", "content": "x"}, "w"), lambda: reply("B done")]})
    configure(rt, hook("review", "subagent_stop", emit({})))
    sid = rt.session["id"]
    rt.manager.start(sid, "Delegate")
    key, card = await pending_card(rt.manager, "hook_subagent_stop")
    write, _ = await pending_card(rt.manager, "write_file")  # Subagent B waits on its own card.
    child_a =next(event for event in rt.manager.get(sid)["events"] if event.get("call_id") == "d1")["child_session_id"]
    await rt.manager.stop(child_a)
    assert card["state"] == "cancelled" and key not in rt.manager.pending
    assert rt.manager.statuses[sid] == "delegating"  # Not left waiting for a card that is gone.
    rt.manager.decide(*write, False)
    second = key
    while second == key:  # Subagent B ends too: its own card in the parent.
        await asyncio.sleep(0.01)
        second, _ = await pending_card(rt.manager, "hook_subagent_stop")
    rt.manager.decide(*second, True)
    await asyncio.wait_for(rt.manager.tasks[sid], 10)
    assert rt.manager.statuses[sid] == "idle" and rt.manager.store.get(sid)["terminal_reason"] == "completed"


# Review fixes: a steered message's user_prompt hooks ------------------------------------------------

async def test_a_steered_message_stays_queued_while_its_hook_card_waits_and_after_stop(rt, monkeypatch):
    rt.session.update(permission_mode="manual")
    gateway = Gateway(monkeypatch, [lambda: call("write_file", {"path": "a.txt", "content": "x"}, "w"),
                                    lambda: reply("Done.")])
    configure(rt, hook("tidy", "user_prompt", emit({})))
    sid = rt.session["id"]
    rt.manager.start(sid, "Write a.txt")
    key, _ = await pending_card(rt.manager, "hook_user_prompt")
    rt.manager.decide(*key, True)
    key, _ = await pending_card(rt.manager, "write_file")
    assert (await rt.manager.submit_message(sid, "also a date pls"))["queued"] is True
    rt.manager.decide(*key, True)
    await pending_card(rt.manager, "hook_user_prompt")  # The steered message's own hook card.
    for session in (rt.manager.get(sid), rt.manager.store.get(sid)):  # Saved queued too: a restart keeps it.
        assert [item["text"] for item in session["queued_messages"]] == ["also a date pls"]
        assert [event["text"] for event in session["events"] if event["type"] == "user"] == ["Write a.txt"]
    await rt.manager.stop(sid)
    stored = rt.manager.store.get(sid)
    assert [(item["text"], item["paused"]) for item in stored["queued_messages"]] == [("also a date pls", False)]
    assert [event["text"] for event in stored["events"] if event["type"] == "user"] == ["Write a.txt"]
    assert not any("also a date" in (message.get("content") or "") for message in stored["wire"])
    assert cards(stored, "hook_user_prompt")[-1]["state"] == "cancelled"
    # Send queued messages sends it, through its hook again.
    await rt.manager.send_queued(sid)
    key, _ = await pending_card(rt.manager, "hook_user_prompt")
    rt.manager.decide(*key, True)
    await asyncio.wait_for(rt.manager.tasks[sid], 10)
    assert last_user(gateway.requests[-1]) == "also a date pls" and rt.manager.store.get(sid)["queued_messages"] == []


async def test_a_steered_message_removed_while_its_hook_card_waits_is_not_sent(rt, monkeypatch):
    rt.session.update(permission_mode="manual", allow_rules=[{"tool": "edits"}])
    gateway = Gateway(monkeypatch, [lambda: call("write_file", {"path": "a.txt", "content": "x"}, "w"),
                                    lambda: reply("Done.")])
    configure(rt, hook("tidy", "user_prompt", "if grep -q '\"steered\": true'; then printf '%s' '{\"prompt\": \"edited\"}'; fi"))
    sid = rt.session["id"]
    rt.manager.start(sid, "Write a.txt")
    first, _ = await pending_card(rt.manager, "hook_user_prompt")
    queued = await rt.manager.enqueue(sid, "also a date pls")
    rt.manager.decide(*first, True)
    key = first
    while key == first:  # The steered message's own hook card, after the write.
        await asyncio.sleep(0.01)
        key, _ = await pending_card(rt.manager, "hook_user_prompt")
    await rt.manager.remove_queued(sid, queued["queued_messages"][0]["id"])
    rt.manager.decide(*key, True)
    await asyncio.wait_for(rt.manager.tasks[sid], 10)
    live = rt.manager.store.get(sid)
    assert [event["text"] for event in live["events"] if event["type"] == "user"] == ["Write a.txt"]
    assert gateway.requests[1]["messages"][-1]["role"] == "tool" and live["terminal_reason"] == "completed"


async def test_a_steered_message_that_hook_context_makes_too_large_stays_queued(rt):
    configure(rt, hook("ctx", "user_prompt", emit({"context": "c" * 1500})))
    sid = rt.session["id"]
    await rt.manager.enqueue(sid, "also a date pls")
    call_part = {"id": "w", "type": "function", "function": {"name": "write_file", "arguments": "{}"}}
    wire = [{"role": "user", "content": "Write a.txt"}, {"role": "assistant", "content": None, "tool_calls": [call_part]},
            {"role": "tool", "tool_call_id": "w", "content": "ok"}]
    await rt.manager.drain_queue(rt.session, wire, rt.tools, fits=lambda message: len(message["content"]) < 1000)
    assert len(wire) == 3 and [item["text"] for item in rt.session["queued_messages"]] == ["also a date pls"]
    assert not [event for event in rt.session["events"] if event["type"] == "user"]
    assert cards(rt.session, "hook_user_prompt") and "waits for a later step" in rt.session["events"][-1]["text"]
    await rt.manager.drain_queue(rt.session, wire, rt.tools, fits=lambda message: True)
    assert wire[-1]["content"].startswith(STEER_PREFIX + "also a date pls\n\n[App note") and rt.session["queued_messages"] == []
    assert [event["text"] for event in rt.session["events"] if event["type"] == "user"] == ["also a date pls"]


# Review fixes: budgets, card edits, compaction ----------------------------------------------------

async def test_what_hooks_add_to_a_result_is_charged_to_the_allowance(rt):
    configure(rt, hook("post", "after_tool", emit({"context": "z" * 1990})))
    allowance = ResultAllowance(10_000, 1.0, calls=1)
    output = await rt.manager.execute_tool(rt.session, rt.tools, "list_files", {"path": "."}, "l", allowance=allowance)
    assert "z" * 100 in output and allowance.settled == 1
    assert allowance.total - allowance.left == allowance.cost(output)
    # A refusal is not settled by the call: the turn charges what is sent, hook context included, once.
    configure(rt, hook("guard", "before_tool", emit({"decision": "deny", "reason": "No.", "context": "y" * 1990})))
    allowance = ResultAllowance(10_000, 1.0, calls=1)
    output = await rt.manager.execute_tool(rt.session, rt.tools, "list_files", {"path": "."}, "l2", allowance=allowance)
    assert output.endswith("y" * 100) and allowance.settled == 0 and allowance.left == allowance.total - allowance.stub
    # A parallel delegation's share charges the shared allowance.
    shared = ResultAllowance(10_000, 1.0, calls=1)
    share = CallAllowance(shared)
    share.charge("short")
    share.charge_more("short" + "x" * 4000, "short")
    assert shared.total - shared.left == shared.cost("short" + "x" * 4000)


async def test_a_card_edit_after_a_hook_change_tells_the_model_what_ran(rt):
    rt.session.update(permission_mode="manual", allow_rules=[{"tool": "hook", "hook": "swap"}])
    configure(rt, hook("swap", "before_tool", emit({"arguments": {"command": "echo from-hook"}}), tools=["run_command"]))
    task = asyncio.create_task(rt.manager.execute_tool(rt.session, rt.tools, "run_command", {"command": "echo agent"}, "c"))
    key, _ = await pending_card(rt.manager, "run_command")
    await rt.manager.revise_card(*key, {"command": "echo from-user"})
    rt.manager.decide(*key, True)
    output = await asyncio.wait_for(task, 10)
    assert output.startswith("[A before-tool hook (swap) changed this call's arguments, and the user then edited them on "
                             'the approval card, before it ran. It ran with: {"command": "echo from-user"}. Your '
                             "proposed arguments did not run.]")
    assert "from-hook" not in output and '"output": "from-user\\n"' in output


async def test_compaction_tracks_the_path_a_hook_had_read_not_the_proposed_one(rt):
    (rt.project / "a.txt").write_text("a\n")
    (rt.project / "b.txt").write_text("b\n")
    await rt.manager.execute_tool(rt.session, rt.tools, "read_file", {"path": "a.txt"}, "r0")
    configure(rt, hook("swap", "before_tool", emit({"arguments": {"path": "b.txt"}}), tools=["read_file"]))
    await rt.manager.execute_tool(rt.session, rt.tools, "read_file", {"path": "a.txt"}, "r1")
    assert next(event for event in rt.session["events"] if event.get("call_id") == "r1")["input"] == {"path": "b.txt"}

    def read(call_id):
        part = {"id": call_id, "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}
        return [{"role": "assistant", "content": None, "tool_calls": [part]},
                {"role": "tool", "tool_call_id": call_id, "content": "read"}]
    wire = [{"role": "user", "content": "Earlier"}, *read("r0"), {"role": "assistant", "content": "Read."},
            {"role": "user", "content": "Now"}, *read("r1")]
    record, holder = rt.manager.compaction_record(rt.session, rt.tools)
    record(wire, 4)
    # b.txt was read after the cut, so its contents stay in context; a.txt's only read is summarized.
    assert holder["forgotten"] == [str((rt.project / "a.txt").resolve())]


# Review fixes: imported hook fields ----------------------------------------------------------------

async def test_hook_fields_are_exported_typed_and_a_bundle_with_bad_ones_is_refused(rt, monkeypatch):
    Gateway(monkeypatch, [lambda: reply("Done")])
    configure(rt, hook("tidy", "user_prompt", emit({"prompt": "Fix the bug", "context": "Ticket 7."})),
              hook("swap", "before_tool", emit({"arguments": {"command": "echo hi"}, "context": "c"})))
    await rt.manager.run(rt.session, "fix teh bug")
    await rt.manager.execute_tool(rt.session, rt.tools, "run_command", {"command": "echo agent"}, "c")
    bundle = export_bundle([rt.session], rt.session["id"])
    events = bundle["sessions"][0]["events"]
    user = next(event for event in events if event["type"] == "user")
    assert user["hook_edit"] == {"hooks": ["tidy"], "original": "fix teh bug"}
    assert user["hook_notes"] == [{"event": "user_prompt", "hook_id": "tidy", "context": "Ticket 7."}]
    card = next(event for event in events if event.get("name") == "hook_before_tool")
    assert card["hook_result"]["json"] is True and card["hook_result"]["arguments"]["after"] == {"command": "echo hi"}
    assert next(event for event in events if event.get("call_id") == "c")["hook_change"]["original"] == {"command": "echo agent"}
    for field, value in (("hook_edit", {"hooks": "x", "original": "y"}),
                         ("hook_notes", [{"event": "on_stop", "hook_id": "a", "context": "c"}]),
                         ("hook_blocked", {"hook_id": "a" * 65, "reason": "r"}),
                         ("hook_ask", {"hook_id": "a", "reason": "r" * 2001}),
                         ("hook_result", {"json": True, "decision": "allow"}),
                         ("hook_change", {"hooks": ["a"], "original": "x" * 5000}),
                         ("hook_change", {"hooks": ["a"], "original": {"content": "x" * 20_000}})):
        broken = copy.deepcopy(bundle)
        broken["sessions"][0]["events"][0][field] = value
        with pytest.raises(ValueError, match="Invalid conversation bundle schema"):
            validate_bundle(broken)


# Glob selectors, subagent attribution, before_compaction and notification ------------------------------------

async def test_glob_selectors_pick_tools_and_the_tools_they_cover(rt):
    record = rt.tmp / "glob.jsonl"
    configure(rt, hook("reads", "before_tool", emit({}, record, append=True), tools=["read_*", "mcp__docs__*"]))
    (rt.project / "a.md").write_text("# A\n")
    for name, arguments in (("read_file", {"path": "a.md"}), ("outline_file", {"path": "a.md"}),
                            ("list_files", {"path": "."})):
        await rt.manager.execute_tool(rt.session, rt.tools, name, arguments, name)
    seen = stdin_lines(record)
    assert [(item["tool"], item.get("covered_by")) for item in seen] == [("read_file", None), ("outline_file", "read_file")]


async def test_tool_hooks_name_the_agent_and_a_subagent_its_parent(rt):
    record = rt.tmp / "agent.jsonl"
    configure(rt, hook("who", "before_tool", emit({}, record, append=True), tools=["list_files"]))
    await rt.manager.execute_tool(rt.session, rt.tools, "list_files", {"path": "."}, "parent")
    child = rt.manager.store.create({"workspace": str(rt.project), "model": "test-model"})
    child.update(parent_session_id=rt.session["id"], is_subagent=True, agent_type="reviewer", tool_profile="inherit",
                 permission_mode="bypassPermissions", agent_file=".agents/agents/reviewer.md")
    rt.manager.store.save(child)
    await rt.manager.execute_tool(child, rt.tools, "list_files", {"path": "."}, "child")
    parent, sub = stdin_lines(record)
    assert parent["agent"] == {"session_id": rt.session["id"], "is_subagent": False, "agent_type": None,
                               "parent_session_id": None, "agent_file": None}
    assert sub["agent"] == {"session_id": child["id"], "is_subagent": True, "agent_type": "reviewer",
                            "parent_session_id": rt.session["id"], "agent_file": ".agents/agents/reviewer.md"}
    assert sub["session_id"] == child["id"]


def compactable(rt):
    rt.manager.settings.values["compaction_handoffs"] = False
    rt.session["wire"] = [
        {"role": "user", "content": "Earlier decision: keep the migration."}, {"role": "assistant", "content": "Done."},
        {"role": "user", "content": "What is next?"}, {"role": "assistant", "content": "Run tests."}]
    rt.session["context_state"] = {"summary": "", "through": 0, "compactions": 0}
    rt.manager.store.save(rt.session)


def summary_gateway(monkeypatch):
    requests = []

    async def gateway(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "## Summary\nKept."}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_CLIENT(**kwargs, transport=httpx.MockTransport(gateway)))
    return requests


async def test_a_before_compaction_hook_adds_context_to_the_summary_request(rt, monkeypatch):
    compactable(rt)
    record = rt.tmp / "compact.jsonl"
    configure(rt, hook("keep", "before_compaction", emit({"context": "Keep the ticket number PRJ-9."}, record)))
    requests = summary_gateway(monkeypatch)
    await rt.manager.manual_compact(rt.session, "Keep the migration decision.")
    payload = stdin_lines(record)[0]
    assert payload["trigger"] == "manual" and payload["preservation_note"] == "Keep the migration decision."
    assert payload["estimated_input_tokens"] > 0 and payload["threshold"] > 0 and "agent" not in payload
    prompt = requests[0]["messages"][1]["content"]
    assert "Keep the migration decision." in prompt and "before_compaction hook keep: Keep the ticket number PRJ-9." in prompt
    assert prompt.index("Keep the migration decision.") < prompt.index("PRJ-9")
    assert rt.session["context_state"]["compactions"] == 1
    assert cards(rt.session, "hook_before_compaction")[0]["hook_result"]["context"] == "Keep the ticket number PRJ-9."


async def test_a_before_compaction_hook_cannot_block_a_compaction(rt, monkeypatch):
    compactable(rt)
    configure(rt, hook("hold", "before_compaction", emit({"decision": "block", "reason": "Release in progress.",
                                                         "context": "Keep the release plan."})))
    requests = summary_gateway(monkeypatch)
    await rt.manager.manual_compact(rt.session)
    assert len(requests) >= 1 and rt.session["context_state"]["compactions"] == 1
    assert "before_compaction hook hold: Keep the release plan." in requests[0]["messages"][1]["content"]
    assert not [event for event in rt.session["events"] if event["type"] == "error"]
    result = cards(rt.session, "hook_before_compaction")[0]["hook_result"]
    assert result["ignored"] == ["decision", "reason"] and "decision" not in result
    note = {"text": ""}
    before = rt.manager.compaction_hook(rt.session, "auto", "", note)
    shown = len(rt.session["events"])
    assert "Keep the release plan." in await before({"estimated_input_tokens": 1, "threshold": 2, "input_budget": 3})
    assert not [event for event in rt.session["events"][shown:] if event["type"] in ("notice", "error")]


@pytest.mark.parametrize("mode", ["bypassPermissions", "manual"])
async def test_stop_during_a_manual_compactions_hook_cancels_its_card(rt, monkeypatch, mode):
    from local_agent.portability import completed_for_fork
    compactable(rt)
    rt.session.update(permission_mode=mode, terminal_reason="completed")
    rt.manager.store.save(rt.session)
    rt.manager.live.pop(rt.session["id"], None)
    configure(rt, hook("slow", "before_compaction", "sleep 5; printf '{}'"))
    summary_gateway(monkeypatch)
    rt.manager.start_compaction(rt.session["id"])
    # Bypass runs the hook (its card running); Manual first asks (its card pending).
    waiting = "running" if mode == "bypassPermissions" else "pending"
    for _ in range(500):
        if any(event["state"] == waiting for event in cards(rt.manager.get(rt.session["id"]), "hook_before_compaction")):
            break
        await asyncio.sleep(0.01)
    await rt.manager.stop(rt.session["id"])
    saved = rt.manager.store.get(rt.session["id"])
    assert [event["state"] for event in cards(saved, "hook_before_compaction")] == ["cancelled"]
    assert saved["events"][-1]["text"].startswith("Context compaction stopped.")
    assert rt.manager.statuses.get(rt.session["id"], "idle") == "idle" and not rt.manager.pending
    completed_for_fork(saved)  # Forkable again, as before the compaction.


async def test_prepare_context_runs_the_before_compaction_callback_once_when_it_compacts():
    from local_agent.context import prepare_context
    calls = []

    async def before(info):
        calls.append(info)
        return "From a hook: keep PRJ-9."

    async def summarize(previous, chunk, limit_bytes):
        return "## Summary\nKept."
    wire = [{"role": "user", "content": "old " * 4000}, {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "new"}]
    system = {"role": "system", "content": "s"}
    await prepare_context(wire, {}, system, [], 16_384, summarize, reply_reserve=2_048, before_compaction=before)
    assert calls == []  # Nothing to compact: the callback never runs.
    await prepare_context(wire, {}, system, [], 16_384, summarize, reply_reserve=2_048, force_compact=True,
                          before_compaction=before)
    assert len(calls) == 1 and set(calls[0]) == {"estimated_input_tokens", "threshold", "input_budget"}


async def test_notification_hooks_never_show_a_card_and_need_a_rule_outside_bypass(rt):
    record = rt.tmp / "notify.jsonl"
    configure(rt, hook("ping", "notification", emit("ignored output", record)))
    rt.session.update(permission_mode="manual", title=f"Deploy {TOKEN}")
    events = len(rt.session["events"])
    rt.manager.notify_status(rt.session, "running", "awaiting_approval")
    await asyncio.sleep(0.05)
    assert not record.exists() and len(rt.session["events"]) == events and not rt.manager.pending
    rt.manager.rules.add("user", rt.session["workspace"], {"behavior": "allow", "tool": "hook", "hook": "ping"})
    rt.manager.notify_status(rt.session, "running", "awaiting_approval")
    rt.manager.notify_status(rt.session, "running", "running")  # No notification: nothing runs.
    await asyncio.gather(*rt.manager._notification_runs.values())
    payload, = stdin_lines(record)
    assert payload == {"event": "notification", "session_id": rt.session["id"], "status": "awaiting_approval",
                       "message": "Needs your approval: Deploy [REDACTED]", "title": "Deploy [REDACTED]",
                       "page_open": False}
    assert len(rt.session["events"]) == events  # Never a card, never a notice.
