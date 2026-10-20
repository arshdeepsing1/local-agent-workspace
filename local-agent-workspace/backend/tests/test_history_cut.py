"""The shared history cut (history.py): one table of synthetic conversations, one per boundary type,
and the real M2 code paths that append app-generated user-role prompts."""
import asyncio
import copy
import json

import httpx
import pytest

from local_agent import history
from local_agent.agents import UNANSWERED_CONTINUATION, AgentManager
from local_agent.config import Settings
from local_agent.context import (INCOMPLETE_RESPONSE_CONTINUATION, INVALID_ARGUMENTS_CONTINUATION,
                                 OUTPUT_LIMIT_CONTINUATION, VERIFY_CONTINUATION_PREFIX, context_boundary,
                                 is_app_prompt, is_continuation, turn_start)
from local_agent.portability import _validate_wire, export_bundle, validate_bundle
from local_agent.store import Store

MISSING = object()


def user(event_id, text, in_wire=True, **extra):
    event = {"id": event_id, "type": "user", "created": 1.0, "text": text, **extra}
    if in_wire is not MISSING:
        event["in_wire"] = in_wire
    return event


def reply(event_id, text="Done.", status="completed", finish="stop"):
    return {"id": event_id, "type": "assistant", "created": 1.0, "text": text,
            "request_info": {"model": "test-model", "status": status, "finish_reason": finish}}


def tool(event_id, call_id, output="ok"):
    return {"id": event_id, "type": "tool", "created": 1.0, "name": "read_file", "input": {"path": "a.txt"},
            "state": "completed", "output": output, "call_id": call_id}


def u(text):
    return {"role": "user", "content": text}


def a(text="Done.", calls=()):
    message = {"role": "assistant", "content": text}
    if calls:
        message["content"] = None
        message["tool_calls"] = [{"id": call, "type": "function", "function": {"name": "read_file",
                                                                             "arguments": '{"path": "a.txt"}'}}
                                 for call in calls]
    return message


def result(call_id, output="ok"):
    return {"role": "tool", "tool_call_id": call_id, "content": output}


def session_of(events, wire, state=None):
    session = {"id": "chat", "title": "Cut", "workspace": "/srv/x", "model": "test-model", "created": 1.0,
               "updated": 2.0, "events": events, "wire": wire, "terminal_reason": "completed",
               "file_views": {"/srv/x/a.txt": "seen"}, "queued_messages": [{"text": "later"}],
               "pending_context": [{"command": "ls"}], "turn_running": False, "context_info": {"estimated_tokens": 1}}
    if state is not None:
        session["context_state"] = state
    return session


TOOL_WIRE = [u("First"), a(calls=["c1"]), result("c1"), a("Read."), u("Second"), a(calls=["c2"]), result("c2"),
             a("Read again."), u("Third"), a("Done.")]
TOOL_EVENTS = [user("u1", "First"), tool("t1", "c1"), reply("a1", "Read."), user("u2", "Second"), tool("t2", "c2"),
               reply("a2", "Read again."), user("u3", "Third"), reply("a3")]


def summary(through):
    return {"summary": "Earlier work.", "through": through, "compactions": 2, "handoff_files": [{"path": "h.md"}],
            "tool_definitions": [{"function": {"name": "read_file"}}]}


PASTED = VERIFY_CONTINUATION_PREFIX + " my notes from yesterday, pasted."
VERIFY_FAILED = VERIFY_CONTINUATION_PREFIX + " the project's verify command `make test` failed (exit 1)."


# name -> (session, {user event id: (event_cut, wire_cut, summary_dropped) | error message})
CASES = {
    "a_plain": (session_of([user("u1", "One"), reply("a1"), user("u2", "Two"), reply("a2"), user("u3", "Three"),
                            reply("a3")], [u("One"), a(), u("Two"), a(), u("Three"), a()]),
                {"u1": (0, 0, False), "u2": (2, 2, False), "u3": (4, 4, False)}),
    "b_tool_exchange": (session_of([user("u1", "Read"), tool("t1", "c1"), reply("a1"), user("u2", "Next"),
                                    reply("a2")], [u("Read"), a(calls=["c1"]), result("c1"), a(), u("Next"), a()]),
                        {"u1": (0, 0, False), "u2": (3, 4, False)}),
    "c_output_limit_continuation": (
        session_of([user("u1", "Write it"), reply("a1", "", "interrupted", "length"),
                    {"id": "n1", "type": "notice", "text": "Automatically continuing."}, reply("a1b", "Written."),
                    user("u2", "Next"), reply("a2")],
                   [u("Write it"), a("[cut]"), u(OUTPUT_LIMIT_CONTINUATION), a("Written."), u("Next"), a()]),
        {"u1": (0, 0, False), "u2": (4, 4, False)}),
    "d_failed_before_append": (
        session_of([user("u1", "First"), reply("a1"), user("u2", "Again", in_wire=False),
                    {"id": "e2", "type": "error", "text": "Configure Databricks."}, user("u3", "Again"), reply("a3")],
                   [u("First"), a(), u("Again"), a()]),
        {"u1": (0, 0, False), "u2": (2, 2, False), "u3": (4, 2, False)}),
    "e_through_below_cut": (session_of(copy.deepcopy(TOOL_EVENTS), copy.deepcopy(TOOL_WIRE), summary(3)),
                            {"u1": (0, 0, True), "u2": (3, 4, False), "u3": (6, 8, False)}),
    "e_through_at_cut": (session_of(copy.deepcopy(TOOL_EVENTS), copy.deepcopy(TOOL_WIRE), summary(4)),
                         {"u1": (0, 0, True), "u2": (3, 4, False), "u3": (6, 8, False)}),
    "e_through_above_cut": (session_of(copy.deepcopy(TOOL_EVENTS), copy.deepcopy(TOOL_WIRE), summary(7)),
                            {"u1": (0, 0, True), "u2": (3, 4, True), "u3": (6, 8, False)}),
    "e_through_inside_turn": (session_of(copy.deepcopy(TOOL_EVENTS), copy.deepcopy(TOOL_WIRE), summary(5)),
                              {"u1": (0, 0, True), "u2": (3, 4, True), "u3": (6, 8, False)}),
    "f_steered": (session_of([user("u1", "Start"), tool("t1", "c1"), user("s1", "Also this", steered=True),
                              reply("a1"), user("u2", "Next"), reply("a2")],
                             [u("Start"), a(calls=["c1"]), result("c1"), u("Also this"), a(), u("Next"), a()]),
                  {"u1": (0, 0, False), "s1": (2, 3, False), "u2": (4, 5, False)}),
    "g_stopped_reply": (
        session_of([user("u1", "Explain"), reply("a1", "Partial", "cancelled", None),
                    {"id": "n1", "type": "notice", "text": "Stopped. You can continue this conversation.",
                     "stop_phase": "streaming"}, user("u2", "Go on"), reply("a2")],
                   [u("Explain"), a(history.stopped_reply("Partial", history.STOP_MARKER)), u("Go on"), a()]),
        {"u1": (0, 0, False), "u2": (3, 2, False)}),
    "h_legacy_aligned": (session_of([user("u1", "One", MISSING), reply("a1"), user("u2", "Two", MISSING),
                                     reply("a2")], [u("One"), a(), u("Two"), a()]),
                         {"u1": (0, 0, False), "u2": (2, 2, False)}),
    "h_legacy_misaligned": (session_of([user("u1", "One", MISSING), reply("a1"), user("u2", "Lost", MISSING),
                                        {"id": "e2", "type": "error", "text": "Failed."},
                                        user("u3", "Three", MISSING), reply("a3")], [u("One"), a(), u("Three"), a()]),
                            {"u1": history.LEGACY_UNALIGNED, "u2": history.LEGACY_UNALIGNED,
                             "u3": history.LEGACY_UNALIGNED}),
    "i_legacy_prefix_flagged_suffix": (
        session_of([user("u1", "One", MISSING), reply("a1"), user("u2", "Two"), reply("a2"), user("u3", "Three"),
                    reply("a3")], [u("One"), a(), u("Two"), a(), u("Three"), a()]),
        {"u1": (0, 0, False), "u2": (2, 2, False), "u3": (4, 4, False)}),
    "i_misaligned_prefix_flagged_suffix": (
        session_of([user("u1", "One", MISSING), reply("a1"), user("u1b", "Lost", MISSING), user("u2", "Two"),
                    reply("a2")], [u("One"), a(), u("Two"), a()]),
        {"u1": history.LEGACY_UNALIGNED, "u1b": history.LEGACY_UNALIGNED, "u2": (3, 2, False)}),
    # The user's own message may start like an app prompt (a pasted Verify note); a real Verify message follows it.
    "j_user_text_like_app_prompt": (
        session_of([user("u1", "One", MISSING), reply("a1"), user("u2", PASTED), reply("a2", "Checked."),
                    {"id": "n2", "type": "notice", "text": "Verify failed."}, reply("a2b", "Fixed."),
                    user("u3", "Next"), reply("a3")],
                   [u("One"), a(), u(PASTED), a("Checked."), u(VERIFY_FAILED), a("Fixed."), u("Next"), a()]),
        {"u1": (0, 0, False), "u2": (2, 2, False), "u3": (6, 6, False)}),
    "j_tracked_only_user_text_like_app_prompt": (
        session_of([user("u1", PASTED), reply("a1", "Checked."), user("u2", "Next"), reply("a2")],
                   [u(PASTED), a("Checked."), u(VERIFY_FAILED), a("Fixed."), u("Next"), a()]),
        {"u1": (0, 0, False), "u2": (2, 4, False)}),
}


def turn_end(events):
    """The terminal_reason the table's kept events show: a Stop, an error, or a completed last reply."""
    start = max((index for index, event in enumerate(events) if event["type"] == "user"), default=None)
    turn = events[start:] if start is not None else []
    if any(event.get("stop_phase") for event in turn):
        return "stopped"
    error = next((event for event in reversed(turn) if event["type"] == "error"), None)
    if error is not None:
        return "inference_error" if error.get("error_kind") else "error"
    # Verify's trailing cards and notices follow the turn's last reply.
    last = next((event for event in reversed(events) if not event.get("verify") and event["type"] != "notice"), {})
    completed = last.get("type") == "assistant" and (last.get("request_info") or {}).get("status") == "completed"
    return "completed" if completed else None


def assert_cut(session, event_id, expected=None):
    """Every invariant of one cut; returns the copy."""
    original = copy.deepcopy(session)
    cut, info = history.cut_session(session, event_id)
    assert session == original  # The source is never changed.
    event_cut = next(index for index, event in enumerate(session["events"]) if event["id"] == event_id)
    wire_cut = len(cut["wire"])
    assert cut["events"] == session["events"][:event_cut]
    assert cut["wire"] == session["wire"][:wire_cut]
    if wire_cut < len(session["wire"]):
        message = session["wire"][wire_cut]
        assert message["role"] == "user" and message["content"] == session["events"][event_cut]["text"]
    if expected is not None:
        assert (event_cut, wire_cut, info["summary_dropped"]) == expected
    assert info["events_removed"] == len(session["events"]) - event_cut
    assert info["messages_removed"] == len(session["wire"]) - wire_cut
    _validate_wire(cut["wire"])
    state = cut.get("context_state") or {}
    assert context_boundary(cut["wire"], state.get("through", 0))
    if "context_state" in session:
        if info["summary_dropped"]:
            assert state == {"summary": "", "through": 0, "compactions": session["context_state"]["compactions"],
                             "tool_definitions": session["context_state"]["tool_definitions"]}
        else:
            assert state == session["context_state"]
    validate_bundle(export_bundle([cut], cut["id"]))
    assert cut.get("terminal_reason") == turn_end(cut["events"])
    assert cut["file_views"] == {}
    for key in ("context_info", "turn_running", "queued_messages", "pending_context"):
        assert key not in cut
    return cut


@pytest.mark.parametrize("name", CASES)
def test_every_user_message_of_every_boundary_type_cuts_cleanly(name):
    session, expectations = CASES[name]
    assert set(expectations) == {event["id"] for event in session["events"] if event["type"] == "user"}
    for event_id, expected in expectations.items():
        if isinstance(expected, str):
            with pytest.raises(history.HistoryError) as error:
                history.cut_session(session, event_id)
            assert str(error.value) == expected
        else:
            assert_cut(session, event_id, expected)
    # The whole conversation can always be copied.
    whole, info = history.cut_session(session)
    assert whole["events"] == session["events"] and whole["wire"] == session["wire"]
    assert info == {"events_removed": 0, "messages_removed": 0, "summary_dropped": False}


def test_keep_local_keeps_queued_text_and_waiting_output():
    session, _ = CASES["a_plain"]
    cut, _ = history.cut_session(session, "u2", keep_local=True)
    assert cut["queued_messages"] == [{"text": "later"}] and cut["pending_context"] == [{"command": "ls"}]
    assert cut["file_views"] == {} and "context_info" not in cut


def notice(event_id, text, **extra):
    return {"id": event_id, "type": "notice", "created": 1.0, "text": text, **extra}


STOPPED = notice("n1", "Stopped. You can continue this conversation.", stop_phase="streaming")
VERIFY_PASSED = [{"id": "v1", "type": "tool", "created": 1.0, "name": "run_command", "input": {"command": "make test"},
                  "state": "completed", "output": "ok", "verify": True}, notice("v2", "Verify passed: make test")]
SHELL_CARD = {"id": "s1", "type": "tool", "created": 1.0, "name": "user_command", "input": {"command": "false"},
              "state": "error", "output": "exit 1"}


@pytest.mark.parametrize("turn, expected", [
    ([reply("a1")], "completed"),
    ([reply("a1"), *VERIFY_PASSED], "completed"),
    ([reply("a1"), SHELL_CARD], "completed"),
    ([reply("a1"), *VERIFY_PASSED, notice("j1", "Job finished.", job_id="j"), SHELL_CARD], "completed"),
    ([reply("a1"), {**VERIFY_PASSED[0], "state": "error"}, notice("v2", "Verify failed: make test")], "tool_error"),
    ([reply("a1", "Partial", "cancelled", None), STOPPED], "stopped"),
    ([reply("a1", "Partial", "cancelled", None), STOPPED, SHELL_CARD], "stopped"),
    ([reply("a1"), notice("n1", "Reached the 6-request agent limit.", terminal_reason="step_limit")], "step_limit"),
    ([{**reply("a1", "Part"), "request_info": {"status": "interrupted", "error_kind": "incomplete_response"}}],
     "interrupted"),
    ([{"id": "e1", "type": "error", "created": 1.0, "text": "Limit.", "error_kind": "output_limit"}], "inference_error"),
    ([{"id": "e1", "type": "error", "created": 1.0, "text": "Failed."}], "error"),
    ([reply("a1", "Part", "interrupted", "length")], None),
    ([], None),
])
def test_a_cut_keeps_how_the_kept_turn_ended(turn, expected):
    """A rewind or fork leaves the chat idle after its kept turn: Continue (stopped, step limit, interrupted)
    and the plain fork (completed) still see how that turn ended, past trailing Verify notices and ! cards."""
    events = [user("u1", "First"), *turn, user("u2", "Second"), reply("a2")]
    session = session_of(events, [u("First"), a(), u("Second"), a()])
    cut, _ = history.cut_session(session, "u2", keep_local=True)
    assert cut.get("terminal_reason") == expected
    whole, _ = history.cut_session(session_of([user("u1", "First"), *turn], [u("First"), a()]))
    assert whole.get("terminal_reason") == expected


def test_a_cut_keeps_only_the_kept_turns_result_record():
    """Scripts read the header's last_turn_result: after a cut it is the kept last turn's (its reply's footer),
    never a removed turn's; a cut that removes nothing keeps it, also for a turn that ended without a reply."""
    kept = {"terminal_reason": "completed", "started": 1.0}
    removed = {"terminal_reason": "tool_error", "started": 3.0, "changed_files": ["src/removed.py"]}
    events = [user("u1", "First"), {**reply("a1"), "turn_result": kept}, user("u2", "Second"),
              {**reply("a2"), "turn_result": removed}]
    session = {**session_of(events, [u("First"), a(), u("Second"), a()]), "last_turn_result": removed}
    assert history.cut_session(session, "u2", keep_local=True)[0]["last_turn_result"] == kept
    assert "last_turn_result" not in history.cut_session(session, "u1", keep_local=True)[0]
    older = {**session, "events": [user("u1", "First"), reply("a1"), *events[2:]]}  # A footer from before results.
    assert "last_turn_result" not in history.cut_session(older, "u2", keep_local=True)[0]
    blocked = {**session, "events": events[:3], "wire": [u("First"), a()],
               "last_turn_result": {"terminal_reason": "hook_blocked", "started": 3.0}}
    assert history.cut_session(blocked)[0]["last_turn_result"] == blocked["last_turn_result"]


def test_a_cut_keeps_the_end_notice_of_a_background_subagent_whose_card_it_keeps():
    """A background subagent started in a kept turn that ended in a removed one: its notice moves to the end of the
    kept events (and the next step names it again); a removed card's notice and other notices go with their turn."""
    def card(event_id, call_id, child):
        return {**tool(event_id, call_id), "name": "delegate_task", "child_session_id": child}

    def ended(event_id, child, created):
        return {"id": event_id, "type": "notice", "created": created, "text": "Background subagent explore finished: x",
                "subagent_id": child, "subagent_status": "completed"}

    events = [user("u1", "First"), card("t1", "c1", "kept-child"), {**reply("a1", "Going on."), "created": 2.0},
              user("u2", "Second"), ended("n1", "kept-child", 3.0), card("t2", "c2", "removed-child"),
              {**reply("a2"), "created": 4.0}, ended("n2", "removed-child", 5.0),
              {"id": "n3", "type": "notice", "created": 6.0, "text": "Mode changed."}]
    wire = [u("First"), a(calls=["c1"]), result("c1"), a("Going on."), u("Second"), a(calls=["c2"]), result("c2"), a()]
    session = {**session_of(events, wire), "subagents_reported_through": 5.0}
    for keep_local in (True, False):  # A rewind and a fork from here.
        cut, info = history.cut_session(session, "u2", keep_local=keep_local)
        assert [event["id"] for event in cut["events"]] == ["u1", "t1", "a1", "n1"]
        assert cut["subagents_reported_through"] == 2.0 and cut["terminal_reason"] == "completed"
        assert info["events_removed"] == 5
        # Cut again before its card, the notice goes too.
        assert history.cut_session(cut, "u1", keep_local=keep_local)[0]["events"] == []


def test_a_cut_keeps_the_end_notice_of_a_background_command_whose_card_it_keeps():
    """A background command started in a kept turn that ended (or was stopped) in a removed one: its end notice moves
    to the end of the kept events, in order with a subagent's, so the card still reads how the job ended. The card
    names its job by job_id, or, in a fork or an import (whose cards leave job_id out), by its result's job_id; older
    notices carry only the job_id. A removed card's notice goes with its turn."""
    def card(event_id, call_id, job, output=None, **extra):
        page = json.dumps({"job_id": job, "state": "running", "background": True, "output": ""}, indent=2)
        return {**tool(event_id, call_id, output if output is not None else page), "name": "run_command",
                "input": {"command": "sleep 30", "background": True}, **extra}

    def ended(event_id, job, created, **extra):
        return {"id": event_id, "type": "notice", "created": created, "text": "Background job stopped: sleep 30",
                "job_id": job, **extra}

    edited = "[The command was edited before it ran: sleep 30]\n" + json.dumps({"job_id": "job-c", "state": "running",
                                                                                 "output": ""}, indent=2)
    stopped = ended("n1", "job-a", 3.0, job_state="cancelled", exit_code=None)
    events = [user("u1", "First"), {**tool("t0", "c0"), "name": "delegate_task", "child_session_id": "child"},
              card("t1", "c1", "job-a", job_id="job-a"), card("t2", "c2", "job-b"),
              card("t3", "c3", "job-c", output=edited), card("t4", "c4", "job-d", output="Command refused."),
              {**reply("a1", "Started."), "created": 2.0}, user("u2", "Second"), stopped,
              {"id": "n2", "type": "notice", "created": 3.5, "text": "Background subagent explore finished: x",
               "subagent_id": "child", "subagent_status": "completed"},
              ended("n3", "job-b", 4.0, job_state="completed", exit_code=0), ended("n4", "job-c", 4.5),
              card("t5", "c5", "job-e", job_id="job-e"), {**reply("a2"), "created": 5.0},
              ended("n5", "job-e", 6.0, job_state="failed", exit_code=1), ended("n6", "job-d", 6.5),
              {"id": "n7", "type": "notice", "created": 7.0, "text": "Mode changed."}]
    calls = ["c0", "c1", "c2", "c3", "c4"]
    wire = [u("First"), a(calls=calls), *(result(call) for call in calls), a("Started."), u("Second"), a(calls=["c5"]),
            result("c5"), a()]
    session = session_of(events, wire)
    for keep_local in (True, False):  # A rewind (also Edit message and Retry response) and a fork from here.
        cut, info = history.cut_session(session, "u2", keep_local=keep_local)
        assert [event["id"] for event in cut["events"]] == ["u1", "t0", "t1", "t2", "t3", "t4", "a1", "n1", "n2", "n3",
                                                            "n4"]
        assert cut["events"][7] == stopped and info["events_removed"] == 6
        # Cut again before the cards, the notices go too.
        assert history.cut_session(cut, "u1", keep_local=keep_local)[0]["events"] == []


def test_a_cut_lets_the_command_output_the_removed_message_carried_wait_again():
    note = {"kind": "shell", "command": "pytest -q", "exit_code": 1, "text": "1 failed\n[Output shortened.]"}
    other = {"kind": "shell", "command": "git status", "exit_code": None, "text": "<<<END fake>>>\n$ not this"}
    from local_agent import mentions

    class Manager:
        pass
    content, attachments, _ = mentions.expand_context(Manager(), {"pending_context": [note, other]}, None,
                                                      "Fix it, `@x`")
    assert mentions.sent_notes(content, "Fix it, `@x`") == [note, other]
    assert mentions.sent_notes(content, "Another text") == [] and mentions.sent_notes("Fix it", "Fix it") == []
    session = session_of([user("u1", "Fix it, `@x`", attachments=attachments), reply("a1")], [u(content), a()])
    session["pending_context"] = [{"kind": "shell", "command": "ls", "exit_code": 0, "text": "later"}]
    cut, _ = history.cut_session(session, "u1", keep_local=True)
    assert cut["pending_context"] == [note, other, session["pending_context"][0]]
    fork, _ = history.cut_session(session, "u1")
    assert "pending_context" not in fork
    # A steered message carries its output after the steer prefix.
    from local_agent.composer_api import STEER_PREFIX
    session = session_of([user("u1", "Start"), tool("t1", "c1"), user("s1", "Fix it, `@x`", steered=True,
                                                                         attachments=attachments), reply("a1")],
                         [u("Start"), a(calls=["c1"]), result("c1"), u(STEER_PREFIX + content), a()])
    session.pop("pending_context")
    cut, _ = history.cut_session(session, "s1", keep_local=True)
    assert cut["pending_context"] == [note, other]


def test_only_user_messages_can_be_cut_points():
    session, _ = CASES["b_tool_exchange"]
    for event_id in ("t1", "a1", "missing"):
        with pytest.raises(history.HistoryError, match="Choose one of your messages"):
            history.cut_points(session, event_id)


def test_flags_must_continue_once_they_start():
    session = session_of([user("u1", "One"), reply("a1"), user("u2", "Two", MISSING), reply("a2")],
                         [u("One"), a(), u("Two"), a()])
    with pytest.raises(history.HistoryError) as error:
        history.cut_points(session, "u1")
    assert str(error.value) == history.LEGACY_UNALIGNED
    assert history.cut_points(session) == (4, 4)


def test_more_flagged_messages_than_wire_messages_are_refused():
    session = session_of([user("u1", "One"), reply("a1"), user("u2", "Two"), reply("a2")], [u("One"), a()])
    with pytest.raises(history.HistoryError):
        history.user_turns(session)


def test_a_flagged_message_that_does_not_hold_its_text_is_refused():
    # Its own wire message is missing (for example the app dropped it), so the match would shift to an
    # earlier turn: refused rather than cut at the wrong place.
    session = session_of([user("u1", "One", MISSING), reply("a1"), user("u2", "Two"), reply("a2"),
                          user("u3", INVALID_ARGUMENTS_CONTINUATION), {"id": "e3", "type": "error", "text": "Failed."},
                          user("u4", "Four"), reply("a4")],
                         [u("One"), a(), u("Two"), a(), u("Four"), a()])
    for event_id in ("u1", "u2", "u3", "u4"):
        with pytest.raises(history.HistoryError) as error:
            history.cut_points(session, event_id)
        assert str(error.value) == history.LEGACY_UNALIGNED


def test_a_cut_into_an_unanswered_tool_exchange_is_refused():
    # A broken history: a user message follows a tool call without its result.
    session = session_of([user("u1", "One"), tool("t1", "c1"), user("u2", "Two"), reply("a2"), user("u3", "Three"),
                          reply("a3")], [u("One"), a(calls=["c1"]), u("Two"), a(), u("Three"), a()])
    with pytest.raises(history.HistoryError) as error:
        history.cut_points(session, "u3")
    assert str(error.value) == history.NOT_A_BOUNDARY
    # A trailing unanswered call is allowed: the next turn completes it (repair_tool_history).
    assert history.cut_points(session, "u2") == (2, 2)


def test_rewind_turn_ids_include_turns_removed_by_an_earlier_conversation_rewind():
    events = [user("u1", "One"), reply("a1"), dict(user("u2", "Two"), created=5.0), reply("a2"),
              dict(user("u3", "Three"), created=9.0)]
    session = session_of(events, [])
    records = [{"turn_id": "u1", "created": 2.0}, {"turn_id": "gone-early", "created": 3.0},
               {"turn_id": "gone-late", "created": 6.0}, {"turn_id": "gone-late", "created": 7.0},
               {"turn_id": None, "created": 8.0}, {"turn_id": "u3", "created": 9.5}]
    assert history.rewind_turn_ids(session, "u2", records) == ["u2", "u3", "gone-late"]
    assert history.rewind_turn_ids(session, "u1") == ["u1", "u2", "u3"]
    with pytest.raises(history.HistoryError):
        history.rewind_turn_ids(session, "a1")


def test_stopped_reply_and_stop_phase():
    assert history.stopped_reply("Partial  \n", history.STOP_MARKER) == "Partial\n\n" + history.STOP_MARKER
    assert history.stopped_reply("  ", history.INTERRUPTED_MARKER) == history.INTERRUPTED_MARKER
    running = {"type": "tool", "state": "running"}
    pending = {"type": "tool", "state": "pending"}
    cancelled_reply = {"type": "assistant", "request_info": {"status": "cancelled"}}
    waiting_reply = {"type": "assistant", "request_info": {"status": "running"}}
    assert history.stop_phase([user("u", "x"), running]) == "tools"
    assert history.stop_phase([user("u", "x"), pending, cancelled_reply]) == "tools"
    assert history.stop_phase([user("u", "x"), cancelled_reply]) == "streaming"
    assert history.stop_phase([user("u", "x"), waiting_reply]) == "waiting"
    assert history.stop_phase([]) == "waiting"


def test_turn_start_uses_the_app_prompt_predicate():
    wire = [u("Task"), a("[cut]"), u(OUTPUT_LIMIT_CONTINUATION), a(calls=["c"]), result("c"),
            u(VERIFY_CONTINUATION_PREFIX + " failed"), a("Fixed.")]
    assert [turn_start(wire, index) for index in range(len(wire))] == [0] * len(wire)
    for text in (OUTPUT_LIMIT_CONTINUATION, INVALID_ARGUMENTS_CONTINUATION, INCOMPLETE_RESPONSE_CONTINUATION,
                 VERIFY_CONTINUATION_PREFIX + " the user declined"):
        assert is_app_prompt(u(text)) and is_continuation(u(text))
    assert not is_app_prompt(u("Automatic verification")) and not is_app_prompt(a(OUTPUT_LIMIT_CONTINUATION))


# Real code paths that append app prompts (M2): each conversation is built by AgentManager.run
# against a fake gateway, then every user message is cut.

@pytest.fixture
async def real(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(project), env_file="")
    settings.credentials = lambda: ("https://gateway.example", "private-token")
    store = Store(tmp_path / "tests.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session.update(title="Real", title_generated=True, permission_mode="bypassPermissions")
    store.save(session)
    yield manager, session, project
    await manager.jobs.shutdown()
    store.db.close()


def frame(delta, finish=None):
    return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"


def say(text, finish="stop"):
    return lambda: httpx.Response(200, text=frame({"content": text}, finish) + "data: [DONE]\n\n")


def calls(*items, finish="tool_calls"):
    parts = [{"index": index, "id": f"call-{index}-{name}", "function": {"name": name, "arguments": arguments}}
             for index, (name, arguments) in enumerate(items)]
    return lambda: httpx.Response(200, text=frame({"tool_calls": parts}, finish) + "data: [DONE]\n\n")


def gateway(monkeypatch, *responses):
    requests = []

    async def handle(request):
        requests.append(json.loads(request.content))
        response = responses[len(requests) - 1]()
        return await response if asyncio.iscoroutine(response) else response
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handle)))
    return requests


def assert_real_cuts(session, prompt_count=2):
    """Each user message maps to its own wire message; app prompts are never a cut point."""
    users = [event for event in session["events"] if event["type"] == "user"]
    assert len(users) == prompt_count and all(event["in_wire"] is True for event in users)
    for event in users:
        cut = assert_cut(session, event["id"])
        if event is not users[0]:
            assert session["wire"][len(cut["wire"])] == {"role": "user", "content": event["text"]}
            # The kept part never ends on an app prompt: it is answered (by a reply or a placeholder).
            assert not is_app_prompt(cut["wire"][-1])


WRITE = ("write_file", json.dumps({"path": "a.txt", "content": "ok\n"}))


@pytest.mark.parametrize("trigger,continuation", [
    (calls(("list_files", "{}"), ("write_file", '{"path": "a.txt", "content": "unfinished')),
     INVALID_ARGUMENTS_CONTINUATION),
    (calls(("list_files", "{}"), finish=None), INCOMPLETE_RESPONSE_CONTINUATION),
    (say("A very long answer", finish="length"), OUTPUT_LIMIT_CONTINUATION),
], ids=["invalid_arguments", "incomplete_response", "output_limit"])
async def test_real_hidden_continuations_are_not_turns(real, monkeypatch, trigger, continuation):
    manager, session, _ = real
    gateway(monkeypatch, trigger, say("Recovered."), say("Second answer."))
    await manager.run(session, "First task")
    await manager.run(session, "Second task")
    assert {"role": "user", "content": continuation} in session["wire"]
    assert_real_cuts(session)


async def test_real_verify_failure_message_is_not_a_turn(real, monkeypatch):
    manager, session, project = real
    manager.projects.update(str(project.resolve()), {"verify_command": "test -f ok.txt"})
    gateway(monkeypatch, calls(WRITE), say("Done."), calls(("write_file", json.dumps({"path": "ok.txt",
                                                                                         "content": "ok\n"}))),
            say("Fixed."), say("Second answer."))
    await manager.run(session, "First task")
    manager.projects.update(str(project.resolve()), {"verify_command": ""})
    await manager.run(session, "Second task")
    assert any(message["role"] == "user" and message["content"].startswith(VERIFY_CONTINUATION_PREFIX)
               for message in session["wire"])
    assert_real_cuts(session)


async def test_real_declined_verify_note_and_its_placeholder_stay_with_their_turn(real, monkeypatch):
    manager, session, project = real
    manager.projects.update(str(project.resolve()), {"verify_command": "pytest -q"})
    session.update(permission_mode="acceptEdits")
    manager.store.save(session)
    # The request answering the note fails, so the next turn answers the note with a placeholder.
    gateway(monkeypatch, calls(WRITE), say("Done."), lambda: httpx.Response(400, text="Bad request."),
            say("Second answer."))
    manager.start(session["id"], "First task")
    live = manager.live[session["id"]]
    for _ in range(500):
        if any(event.get("verify") and event.get("state") == "pending" for event in live["events"]):
            break
        await asyncio.sleep(0.01)
    card = next(event for event in live["events"] if event.get("verify"))
    manager.decide(session["id"], card["id"], False, note="Use make lint instead.")
    await asyncio.wait_for(manager.tasks[session["id"]], 10)
    saved = manager.get(session["id"])
    await manager.run(saved, "Second task")
    wire = saved["wire"]
    note = next(index for index, message in enumerate(wire)
                if message["role"] == "user" and message["content"].startswith(VERIFY_CONTINUATION_PREFIX))
    assert wire[note + 1] == {"role": "assistant", "content": UNANSWERED_CONTINUATION}
    assert wire[note + 2] == {"role": "user", "content": "Second task"}
    assert_real_cuts(saved)
