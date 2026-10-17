"""One shared history cut for Edit and resend, Retry, Fork from here and Rewind, plus the stop marker.

Pure functions without I/O. agents.py imports this module, so it never imports agents.py.

A conversation keeps two parallel histories: events (what the UI shows) and wire (what
the model receives). A user event whose text reached the wire as its own user message
has in_wire true; one that failed before its prompt was appended has in_wire false;
events saved before the flag existed have no key and are matched by exact text.
"""
import copy

from . import context, mentions, portability


STOP_MARKER = "[The user stopped this response before it finished. Tool calls it was preparing did not run.]"
INTERRUPTED_MARKER = "[This response was cut off when the app stopped. Tool calls it was preparing did not run.]"
LEGACY_UNALIGNED = ("This conversation was saved by an earlier version, and its messages cannot be matched to the "
                    "model history at this point. Fork or export the whole conversation instead.")
CHOOSE_MESSAGE = "Choose one of your messages in this conversation."
NOT_A_BOUNDARY = ("The model history cannot be cut before this message because a tool exchange is incomplete "
                  "there. Fork or export the whole conversation instead.")


class HistoryError(ValueError):
    """A cut that would corrupt the model history; nothing was changed."""


def _carries(message, text):
    """True when this wire message holds the user's text (the app may add context around it)."""
    content = message.get("content")
    return isinstance(content, str) and isinstance(text, str) and bool(text) and text in content


def _own_prompt(message, text):
    """True when a message that looks like an app prompt is the user's own text that only starts like one
    (for example a pasted Verify note). An app prompt never equals the user's message exactly."""
    content = message.get("content")
    return isinstance(content, str) and isinstance(text, str) and bool(text) and (
        content == text or content.startswith(text + "\n\n"))


def user_turns(session):
    """One entry per user event, in order: {event_index, event_id, wire_index, known}.

    wire_index is the position of the event's own user message in the wire, or None
    when its text never reached the wire. known is False for an older event whose
    position cannot be proven. A tracked event whose wire message does not hold its
    text refuses the cut (LEGACY_UNALIGNED) instead of guessing.
    """
    events, wire = session["events"], session["wire"]
    users = [(index, event) for index, event in enumerate(events) if event.get("type") == "user"]
    first = next((number for number, (_, event) in enumerate(users) if "in_wire" in event), len(users))
    legacy, tracked = users[:first], users[first:]
    if any("in_wire" not in event for _, event in tracked):
        raise HistoryError(LEGACY_UNALIGNED)
    # Tracked events are the newest; their messages are the last user messages in the wire. Match them
    # from the end, skipping app prompts unless one is exactly the user's own text.
    sent = [event for _, event in tracked if event["in_wire"] is True]
    positions, pending, split = [], len(sent), len(wire)
    for index in range(len(wire) - 1, -1, -1):
        if not pending:
            break
        message = wire[index]
        if message.get("role") != "user":
            continue
        text = sent[pending - 1].get("text")
        if context.is_app_prompt(message) and not _own_prompt(message, text):
            continue
        if not _carries(message, text):
            raise HistoryError(LEGACY_UNALIGNED)
        positions.append(index)
        pending -= 1
        split = index
    if pending:
        raise HistoryError(LEGACY_UNALIGNED)
    tracked_wire = iter(reversed(positions))
    legacy_wire = [index for index, message in enumerate(wire[:split])
                   if message.get("role") == "user" and not context.is_app_prompt(message)]
    aligned = len(legacy) == len(legacy_wire) and all(
        event.get("text") == wire[position].get("content") for (_, event), position in zip(legacy, legacy_wire))
    turns = [{"event_index": index, "event_id": event["id"], "wire_index": legacy_wire[number] if aligned else None,
              "known": aligned} for number, (index, event) in enumerate(legacy)]
    for index, event in tracked:
        position = next(tracked_wire) if event["in_wire"] is True else None
        turns.append({"event_index": index, "event_id": event["id"], "wire_index": position, "known": True})
    return turns


def cut_points(session, before_event_id=None):
    """(event_cut, wire_cut): keep events[:event_cut] and wire[:wire_cut].

    None keeps everything. Otherwise the cut lands just before the given user
    message, in both histories.
    """
    events, wire = session["events"], session["wire"]
    if before_event_id is None:
        event_cut, wire_cut = len(events), len(wire)
    else:
        turns = user_turns(session)
        position = next((number for number, turn in enumerate(turns) if turn["event_id"] == before_event_id), None)
        if position is None:
            raise HistoryError(CHOOSE_MESSAGE)
        event_cut, wire_cut = turns[position]["event_index"], len(wire)
        for turn in turns[position:]:
            if not turn["known"]:
                raise HistoryError(LEGACY_UNALIGNED)
            if turn["wire_index"] is not None:
                wire_cut = turn["wire_index"]
                break
    try:
        portability._validate_wire(wire[:wire_cut])
    except ValueError as exc:
        raise HistoryError(NOT_A_BOUNDARY) from exc
    if wire_cut != len(wire) and wire[wire_cut].get("role") != "user":
        raise HistoryError(NOT_A_BOUNDARY)
    return event_cut, wire_cut


def _after_turn(event):
    """Events that can follow a finished turn without being part of its answer: notices (Verify,
    a finished job, a mode change), Verify's command cards and the user's own ! commands."""
    if event.get("type") == "notice":
        return not event.get("stop_phase") and not event.get("terminal_reason")
    return event.get("type") == "tool" and (event.get("verify") is True or event.get("name") == "user_command")


def turn_end(events):
    """The terminal_reason the latest turn of these events ended with, or None when they do not show it.

    The same reasons run() records: 'stopped' (the Stop notice), 'step_limit', 'spending_limit', 'budget_limit',
    'approval_expired' (a scheduled run's approval limit), 'interrupted' (a reply a restart cut short),
    'inference_error' or 'error', else 'completed' (or 'tool_error' when a tool failed) when the turn's last reply
    completed."""
    start = next((index for index in range(len(events) - 1, -1, -1) if events[index].get("type") == "user"), None)
    if start is None:
        return None
    turn = events[start:]
    for event in reversed(turn):
        if event.get("type") == "notice" and event.get("stop_phase"):
            return "stopped"
        if event.get("terminal_reason") in ("step_limit", "spending_limit", "budget_limit", "approval_expired"):
            return event["terminal_reason"]
        info = event.get("request_info") or {}
        if (info.get("status"), info.get("error_kind")) == ("interrupted", "incomplete_response"):
            return "interrupted"
        if event.get("type") == "error":
            return "inference_error" if event.get("error_kind") else "error"
    core = list(turn)
    while core and _after_turn(core[-1]):
        core.pop()
    if not portability.latest_turn_completed({"events": core}):
        return None
    failed = any(event.get("state") == "error" and event.get("name") != "user_command" for event in turn)
    return "tool_error" if failed else "completed"


def cut_session(session, before_event_id=None, *, keep_local=False):
    """A deep copy cut before the given user message (or whole), and what the cut removed.

    keep_local keeps the queued messages and waiting command output, which belong
    to the user rather than to the history (an in-place rewind keeps them). The ! output
    the removed message itself carried waits again too, so a resend attaches it again.
    """
    event_cut, wire_cut = cut_points(session, before_event_id)
    result = copy.deepcopy(session)
    result["events"] = result["events"][:event_cut]
    result["wire"] = result["wire"][:wire_cut]
    summary_dropped = False
    state = result.get("context_state")
    if isinstance(state, dict):
        through = state.get("through", 0)
        if not (through <= wire_cut and context.context_boundary(result["wire"], through)):
            # A summary of removed turns would leak them back into context. No summary
            # per boundary is stored, so it is dropped; the next turn compacts again.
            replacement = {"summary": "", "through": 0, "compactions": state.get("compactions", 0)}
            if "tool_definitions" in state:
                replacement["tool_definitions"] = state["tool_definitions"]
            result["context_state"] = replacement
            summary_dropped = True
    for key in ("context_info", "turn_running", *(() if keep_local else ("queued_messages", "pending_context"))):
        result.pop(key, None)
    # The read-before-write guard must not trust reads the model no longer remembers.
    result["file_views"] = {}
    reason = turn_end(result["events"])
    if reason:
        result["terminal_reason"] = reason
    else:
        result.pop("terminal_reason", None)
    if event_cut < len(session["events"]):
        # Scripts read the header's result record: the remaining last turn's (its last reply's footer), never a
        # removed turn's.
        reply = None
        for event in reversed(result["events"]):
            if event.get("type") == "user":
                break
            reply = reply or (event if event.get("type") == "assistant" else None)
        if reply is not None and isinstance(reply.get("turn_result"), dict):
            result["last_turn_result"] = reply["turn_result"]
        else:
            result.pop("last_turn_result", None)
    if keep_local and before_event_id is not None:
        restored = _carried_output(session, before_event_id)
        if restored:
            result["pending_context"] = [*restored, *(result.get("pending_context") or [])][-mentions.MAX_PENDING:]
    info = {"events_removed": len(session["events"]) - event_cut, "messages_removed": len(session["wire"]) - wire_cut,
            "summary_dropped": summary_dropped}
    return result, info


def _carried_output(session, event_id):
    """The ! output the given user message carried to the model, read back from its own wire message."""
    event = next((item for item in session["events"] if item.get("id") == event_id), None)
    if not event or not any(isinstance(item, dict) and item.get("kind") == "shell" for item in event.get("attachments") or []):
        return []
    turn = next((item for item in user_turns(session) if item["event_id"] == event_id), None)
    if turn is None or turn["wire_index"] is None:
        return []
    return mentions.sent_notes(session["wire"][turn["wire_index"]].get("content"), event.get("text"))


def rewind_turn_ids(session, before_event_id, records=()):
    """The turn ids whose file edits a code rewind to this user message undoes.

    The user events at or after it, plus turns an earlier conversation-only rewind
    removed: checkpoint records of this chat whose turn is no longer an event and
    that were made at or after the chosen message. Needs no wire mapping.
    """
    events = session["events"]
    index = next((number for number, event in enumerate(events)
                  if event.get("type") == "user" and event.get("id") == before_event_id), None)
    if index is None:
        raise HistoryError(CHOOSE_MESSAGE)
    ids = [event["id"] for event in events[index:] if event.get("type") == "user"]
    current = {event.get("id") for event in events if event.get("type") == "user"}
    created = events[index].get("created")
    if isinstance(created, (int, float)):
        for record in records:
            turn = record.get("turn_id")
            stamp = record.get("created")
            if (turn is not None and turn not in current and turn not in ids
                    and isinstance(stamp, (int, float)) and stamp >= created):
                ids.append(turn)
    return ids


def stopped_reply(text, marker):
    """The model-history reply of a stopped response: its partial text, then the marker."""
    return (text.rstrip() + "\n\n" if text and text.strip() else "") + marker


def stop_phase(turn_events):
    """What a Stop interrupted: 'tools' (a tool, approval or question card), 'streaming'
    (a reply) or 'waiting' (a retry wait, context preparation, MCP discovery)."""
    if any(event.get("type") == "tool" and event.get("state") in ("running", "pending") for event in turn_events):
        return "tools"
    last = next((event for event in reversed(turn_events) if event.get("type") == "assistant"), None)
    if last is not None and (last.get("request_info") or {}).get("status") == "cancelled":
        return "streaming"
    return "waiting"


def close_stopped(session, events, text, repair):
    """Close a stopped or cut-short turn in the model history.

    First complete any interrupted tool exchange (repair is agents.repair_tool_history),
    so the marker never follows unanswered tool calls; then, when the wire ends with
    a user message or tool results, append text as the assistant's reply.
    """
    wire, state = repair(session["wire"], events, session.get("context_state", {}))
    session["wire"] = wire
    if "context_state" in session or state:
        session["context_state"] = state
    if wire and wire[-1].get("role") in ("user", "tool"):
        wire.append({"role": "assistant", "content": text})
