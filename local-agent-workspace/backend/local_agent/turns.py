"""A turn's result record (turn_result) and its token and time budget.

AgentManager.run records when each turn starts (AgentManager.turns: its start time, ledger and event
positions) and builds one result record when it ends: how it ended, how long it took, its model requests and
tool calls, the tokens and estimated DBU of its own requests and its subagents', and the files its file tools
changed. The record goes on the turn's last reply (a footer under it), into the chat's header
(last_turn_result) and into the idle status that ends the turn, which is how scripts read the end of a turn.

The budget: Settings → Agent sets a default token limit and time limit per turn (0 = none) and a chat can set
its own (PUT /api/sessions/{id}/turn-limits). The token limit counts every token the turn's requests
processed as Databricks reported them (uncached input, cache reads and writes, and output), its subagents'
included, so it also caps endpoints without price data. The time limit is the wall-clock time since the turn
started, waits for your approvals included. Both are checked before each agent model request: once one is
reached the agent gets one more request, told to stop and report what is done and what remains (no tools
run in it), and then the turn ends with terminal_reason 'budget_limit'. A subagent counts toward its parent's
turn: its requests add to the parent's tokens and the parent's start time applies, and a subagent that
reaches it wraps up and reports the stop as an error, as for the spending limit (in a delegated turn its
submit_report is the one call that runs in the wrap-up, so its report reaches the parent). A /btw side
question asked while the turn runs is not the turn's: it counts toward neither its limit nor its result.
"""
import json
import time

from .config import TURN_LIMITS
from .planning import changed_paths
from .spending import root_of
from .telemetry import USAGE_FIELDS, call_estimated_dbu, call_tokens, reported_tokens, usage_totals

LIMIT_KEYS = TURN_LIMITS  # {Settings key: its maximum}
CHANGED_FILES = 20  # Paths a result lists; changed_files_total counts them all.
ERROR_REASONS = ("error", "inference_error")
# What the wrap-up request's tool calls return: it is told not to call any (a delegated subagent: but submit_report).
WRAP_UP_REFUSAL = "Not run: this turn reached its budget. Reply with a summary of what is done and what remains."
clock = time.time  # Patched by tests of the time limit.


def validate_limit(key, value):
    """A turn limit (whole tokens or minutes, 0 = none), or ValueError."""
    maximum = LIMIT_KEYS[key]
    if type(value) is not int or not 0 <= value <= maximum:
        unit = "tokens" if key == "turn_token_limit" else "minutes"
        label = "Token limit per turn" if key == "turn_token_limit" else "Time limit per turn"
        raise ValueError(f"{label} must be a whole number of {unit} from 0 (none) to {maximum:,}.")
    return value


def scope(manager, session):
    """(the chat whose turn the budget counts, that turn's state or None): a subagent's running parent turn,
    else the chat's own turn (also for a subagent chat the user continues on its own)."""
    parent_id = session.get("parent_session_id") if session.get("is_subagent") else None
    if isinstance(parent_id, str) and parent_id in manager.turns and parent_id in manager.live:
        return manager.live[parent_id], manager.turns[parent_id]
    return session, manager.turns.get(session["id"])


def limits(manager, root):
    """{key: (limit or None when none, source 'chat' | 'settings')} of a chat (root_of: a subagent's parent)."""
    result = {}
    for key in LIMIT_KEYS:
        value, source = root.get(key), "chat"
        if type(value) is not int:
            value, source = manager.settings.values.get(key, 0), "settings"
        try:
            value = validate_limit(key, value)
        except ValueError:
            value = 0
        result[key] = (value or None, source)
    return result


def status(manager, session):
    """The usage panel's view: each limit (None when none), where it comes from, and whether the chat follows its
    parent's limits (a subagent)."""
    root = root_of(manager, session)
    values = limits(manager, root)
    return {"turn_token_limit": values["turn_token_limit"][0], "token_source": values["turn_token_limit"][1],
            "turn_time_limit_minutes": values["turn_time_limit_minutes"][0],
            "time_source": values["turn_time_limit_minutes"][1], "follows_parent": root is not session}


def turn_calls(calls):
    """The ledger records that belong to a turn: not a /btw side question asked while it ran."""
    return [call for call in calls if isinstance(call, dict) and call.get("purpose") != "side_question"]


def own_tokens(session, first_call=0):
    """The tokens of a chat's own ledger records from first_call on (turn_calls)."""
    calls = session.get("inference_calls") if isinstance(session.get("inference_calls"), list) else []
    return sum(call_tokens(call) for call in turn_calls(calls[first_call:]))


def children(manager, root, turn):
    """{child id: its ledger or index entry} for the subagents the chat started in this turn: a running one is
    read live, an ended one from the chat's subagents index (planning.index_entry keeps its tokens and DBU)."""
    found = {}
    for entry in root.get("subagents") or []:
        started = entry.get("created") if isinstance(entry, dict) else None
        if isinstance(started, bool) or not isinstance(started, (int, float)) or started < turn["started"]:
            continue
        found[entry.get("id")] = manager.live.get(entry.get("id")) or entry
    for entry in getattr(manager.delegates, "active", {}).get(root["id"], []):
        child_id = entry.get("child_session_id")
        if child_id and child_id not in found and child_id in manager.live:
            found[child_id] = manager.live[child_id]
    return found


def _child_usage(value):
    """(tokens or None when none were reported, estimated DBU, unpriced calls) of a live child (its ledger) or an
    index entry."""
    if isinstance(value.get("inference_calls"), list):
        calls = turn_calls(value["inference_calls"])
        dbu, unpriced = _spend(calls)
        return reported_tokens(calls), dbu, unpriced
    tokens = value.get("tokens") if type(value.get("tokens")) is int else None
    dbu = value.get("estimated_dbu")
    dbu = dbu if isinstance(dbu, (int, float)) and not isinstance(dbu, bool) else 0.0
    unpriced = value.get("unpriced_calls") if type(value.get("unpriced_calls")) is int else 0
    return tokens, dbu, unpriced


def _spend(calls):
    """(estimated DBU of the priced calls, calls with usage but no price data)."""
    total, unpriced = 0.0, 0
    for call in calls:
        if not isinstance(call, dict):
            continue
        estimate, priced = call_estimated_dbu(call)
        if estimate is not None:
            total += estimate
        elif not priced:
            unpriced += 1
    return total, unpriced


def used(manager, session):
    """(root, turn, tokens so far, seconds so far) of the turn the budget counts, or None outside a turn."""
    root, turn = scope(manager, session)
    if turn is None:
        return None
    tokens = own_tokens(root, turn["first_call"]) + sum(_child_usage(value)[0] or 0 for value in children(manager, root, turn).values())
    return root, turn, tokens, max(0.0, clock() - turn["started"])


def reached(manager, session):
    """(reason text, whether it is the parent's turn) once the turn reached a limit ('about N of its M-token
    limit', ...), else None."""
    state = used(manager, session)
    if state is None:
        return None
    root, _, tokens, seconds = state
    values = limits(manager, root_of(manager, session))
    reasons = []
    token_limit = values["turn_token_limit"][0]
    if token_limit and tokens >= token_limit:
        reasons.append(f"about {tokens:,} of its {token_limit:,}-token limit")
    minutes = values["turn_time_limit_minutes"][0]
    if minutes and seconds >= minutes * 60:
        reasons.append(f"{duration_text(seconds)} of its {minutes:,}-minute limit")
    return (" and ".join(reasons), root is not session) if reasons else None


def wrap_up_notice(text, report=False):
    """The step notice of the wrap-up request (the system message's changing part). report: a subagent's
    delegated turn, whose submit_report is the one call that runs in it (so its report reaches the parent)."""
    if report:
        return (f"This turn has used {text} per turn. Call no tool except submit_report and do not start new work: "
                "send your report now with what is done, what remains and how to continue. This is the turn's last "
                "request.")
    return (f"This turn has used {text} per turn. Do not call any tools and do not start new work: reply now with "
            "a short summary of what is done, what remains and how to continue. This is the turn's last request.")


def stop_text(text, parent, wrapped):
    """The notice (a subagent's error) that ends a turn at its budget. parent: the budget is the parent's turn."""
    who = "Its parent conversation's turn" if parent else "This turn"
    how = "The agent wrapped up instead of continuing." if wrapped else "The agent stopped before its next model request."
    return (f"{who} has used {text} per turn. {how} Raise the limit in Agent tools → Usage (or the default in "
            "Settings → Agent), or press Continue: the next turn starts with a new budget.")


def duration_text(seconds):
    """'42 s', '2 min 10 s' or '1 h 5 min'."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60} s" if seconds % 60 else f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min" if seconds % 3600 // 60 else f"{seconds // 3600} h"


def _delegated_files(events):
    """The changed_files its subagents reported (delegate_task results)."""
    paths = []
    for event in events:
        if event.get("type") != "tool" or event.get("name") != "delegate_task" or not isinstance(event.get("output"), str):
            continue
        try:
            result = json.loads(event["output"])
        except ValueError:
            continue
        files = result.get("changed_files") if isinstance(result, dict) else None
        paths += [path for path in files or [] if isinstance(path, str)]
    return paths


def turn_result(manager, session, turn, terminal_reason, ended=None):
    """The turn's result record (types.js TurnResult): its own requests' usage (a token field no call reported is
    None), its tokens and estimated DBU with its subagents' (tokens None when no call reported a count, DBU None
    when no request of the turn had price data), and the files its file tools and subagents changed (paths as the
    tools named them, redacted)."""
    ended = clock() if ended is None else ended
    events = [event for event in session["events"][turn["first_event"]:] if isinstance(event, dict)]
    calls = turn_calls((session.get("inference_calls") or [])[turn["first_call"]:])
    totals = usage_totals(calls)
    dbu, unpriced = _spend(calls)
    priced = any(call_estimated_dbu(call)[0] is not None for call in calls)
    child_tokens, child_dbu, child_unpriced = None, 0.0, 0
    found = children(manager, session, turn) if not session.get("is_subagent") else {}
    for value in found.values():
        tokens, estimate, missing = _child_usage(value)
        if tokens is not None:
            child_tokens = (child_tokens or 0) + tokens
        child_dbu, child_unpriced = child_dbu + estimate, child_unpriced + missing
        priced = priced or estimate > 0
    own = reported_tokens(calls)
    redact = manager.settings.redact
    paths = list(dict.fromkeys([redact(path)[:300] for path in changed_paths(events)] + _delegated_files(events)))
    result = {
        "terminal_reason": terminal_reason, "is_error": terminal_reason in ERROR_REASONS,
        "started": round(turn["started"], 3), "ended": round(ended, 3),
        "duration_s": round(max(0.0, ended - turn["started"]), 1),
        "steps": sum(event.get("type") == "assistant" and isinstance(event.get("request_info"), dict) for event in events),
        "tool_calls": sum(event.get("type") == "tool" and event.get("name") != "user_command" for event in events),
        "usage": {"calls": totals["calls"], **{field: totals[field] for field in USAGE_FIELDS}},
        "tokens": None if own is None and child_tokens is None else (own or 0) + (child_tokens or 0),
        "estimated_dbu": round(dbu + child_dbu, 6) if priced else None,
        "unpriced_calls": unpriced + child_unpriced,
        "subagents": {"count": len(found), "tokens": 0 if not found else child_tokens, "estimated_dbu": round(child_dbu, 6)},
        "changed_files": paths[:CHANGED_FILES],
    }
    if len(paths) > CHANGED_FILES:
        result["changed_files_total"] = len(paths)
    return result
