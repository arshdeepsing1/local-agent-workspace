"""Structured hook output (one JSON object on stdout) and the lifecycle hook events: user_prompt,
session_start, agent_stop, subagent_start and subagent_stop (lifecycle_hooks.py, extensions.py)."""
import asyncio
import json
import shlex
import sys
from types import SimpleNamespace

import httpx
import pytest

from local_agent.agents import AgentManager
from local_agent.composer_api import STEER_PREFIX
from local_agent.config import Settings
from local_agent.context import HOOK_CONTINUATION_PREFIX, is_app_prompt
from local_agent.extensions import (CONFIG_LIMIT, HOOK_CONTEXT_CHARS, HOOK_EVENTS, MAX_HOOKS, parse_hook_output,
                                    validate_config)
from local_agent.instructions import load_project_instructions
from local_agent.portability import _validate_wire
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
    part = {"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
    return httpx.Response(200, text=frame({"content": "", "tool_calls": [part]}, "tool_calls") + "data: [DONE]\n\n")


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
    with pytest.raises(ValueError, match="user_prompt, session_start, agent_stop, subagent_start or subagent_stop"):
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
