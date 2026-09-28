"""Bounded, data-only conversation transfer; never restore runtime authority."""
import copy
import json
import math
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .context import MAX_ESTIMATE_SCALE, SUMMARY_MAX_BYTES


MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_SESSIONS = 32
FORMAT = "local-agent-workspace.conversations"
Profile = Literal["inherit", "read_only", "file_editor"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Usage(Record):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cache_read_input_tokens: int | None = Field(default=None, ge=0)
    cache_creation_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)


class RequestInfo(Record):
    model: str = Field(max_length=256)
    status: Literal["running", "completed", "error", "cancelled", "interrupted"]
    finish_reason: str | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    error_kind: str | None = None
    usage: Usage | None = None


class Progress(Record):
    status: str
    completed_tools: int = Field(ge=0)
    last_tool: str | None = None
    terminal_reason: str | None = None


class Origin(Record):
    kind: Literal["delegated"]
    parent_session_id: str
    parent_event_id: str
    parent_call_id: str | None = None


class Event(Record):
    id: str = Field(min_length=1, max_length=512)
    type: Literal["user", "assistant", "tool", "notice", "error"]
    created: float | None = None
    text: str | None = None
    reasoning_summary: str | None = None
    reasoning_truncated: bool | None = None
    request_info: RequestInfo | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    output: str | None = None
    preview: str | None = None
    state: Literal["pending", "running", "completed", "rejected", "cancelled", "error"] | None = None
    call_id: str | None = None
    child_session_id: str | None = None
    origin: Origin | None = None
    delegation: Progress | None = None
    terminal_reason: str | None = None
    error_kind: str | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)


class Function(Record):
    name: str = Field(min_length=1, max_length=256)
    arguments: str


class ToolCall(Record):
    id: str = Field(min_length=1, max_length=512)
    type: Literal["function"]
    function: Function


class Message(Record):
    role: Literal["user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = Field(default=None, max_length=256)
    tool_call_id: str | None = None


class ContextState(Record):
    summary: str = Field(default="", max_length=SUMMARY_MAX_BYTES)
    through: int = Field(default=0, ge=0)
    compactions: int = Field(default=0, ge=0)


class InstructionSource(Record):
    path: str
    scope: str
    status: Literal["loaded", "omitted"]
    estimated_tokens: int = Field(ge=0)
    reason: str | None = None


class Breakdown(Record):
    system_instructions: int = Field(ge=0)
    tool_definitions: int = Field(ge=0)
    messages_and_results: int = Field(ge=0)
    summary: int = Field(ge=0)
    request_overhead: int = Field(ge=0)


class ContextInfo(Record):
    estimated_tokens: int = Field(ge=0)
    input_budget: int = Field(ge=0)
    context_window: int = Field(ge=0)
    reply_reserve: int = Field(ge=0)
    compactions: int = Field(ge=0)
    summarized_messages: int = Field(ge=0)
    estimate_method: Literal["weighted_utf8", "conservative_utf8"]
    instruction_files: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    instruction_sources: list[InstructionSource] | None = None
    prepared_for_next_turn: bool | None = None
    breakdown: Breakdown | None = None
    estimate_scale: float | None = Field(default=None, ge=1, le=MAX_ESTIMATE_SCALE)
    summary_adjustment: Literal["condensed", "trimmed"] | None = None


class Conversation(Record):
    id: str = Field(min_length=1, max_length=512)
    title: str = Field(max_length=1000)
    workspace: str = Field(max_length=4096)  # Historical information, not an import destination.
    model: str = Field(min_length=1, max_length=256)
    created: float
    updated: float
    events: list[Event] = Field(max_length=50000)
    wire: list[Message] = Field(max_length=50000)
    runtime: str | None = None
    context_state: ContextState | None = None
    context_info: ContextInfo | None = None
    is_subagent: bool | None = None
    parent_session_id: str | None = None
    parent_event_id: str | None = None
    parent_call_id: str | None = None
    delegation: Progress | None = None
    terminal_reason: str | None = None
    title_generated: bool | None = None
    tool_profile: Profile = "inherit"
    subagent_tool_profile: Profile = "inherit"
    max_steps: int | None = Field(default=None, ge=1, le=8)


class Bundle(Record):
    format: Literal["local-agent-workspace.conversations"]
    version: Literal[1]
    root_session_id: str
    exported_at: float
    sessions: list[Conversation] = Field(min_length=1, max_length=MAX_SESSIONS)


def _bounded_tree(value):
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 32 or count > 200000:
            raise ValueError("Conversation bundle is too complex.")
        if isinstance(item, dict):
            if len(item) + len(pending) + count > 200000:
                raise ValueError("Conversation bundle is too complex.")
            for key in item:
                _utf8(key)
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            if len(item) + len(pending) + count > 200000:
                raise ValueError("Conversation bundle is too complex.")
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            _utf8(item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Conversation bundle contains a non-finite number.")


def _utf8(value):
    if not isinstance(value, str):
        raise ValueError("Conversation JSON object keys must be strings.")
    try:
        if len(value) > MAX_BUNDLE_BYTES:
            raise ValueError("Conversation bundle exceeds the 16 MiB limit.")
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Conversation bundle contains invalid UTF-8 text.") from exc


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Conversation bundle contains duplicate JSON keys.")
        result[key] = value
    return result


def parse_bundle(raw):
    if len(raw) > MAX_BUNDLE_BYTES:
        raise ValueError("Conversation bundle exceeds the 16 MiB limit.")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Choose a valid UTF-8 conversation JSON bundle.") from exc
    return validate_bundle(value)


def validate_bundle(value):
    _bounded_tree(value)
    try:
        result = Bundle.model_validate(value).model_dump(exclude_unset=True, exclude_none=True)
    except ValidationError as exc:
        # Do not reflect imported text or credentials from rejected fields.
        location = ".".join(str(part)[:80] for part in exc.errors()[0]["loc"])
        raise ValueError(f"Invalid conversation bundle schema at {location or 'root'}.") from exc
    if type(value.get("version")) is not int:
        raise ValueError("Unsupported conversation bundle version.")
    sessions = result["sessions"]
    # Null assistant content is valid provider history; retain it verbatim.
    for source, session in zip(value["sessions"], sessions):
        for original, message in zip(source["wire"], session["wire"]):
            if "content" in original and original["content"] is None:
                message["content"] = None
    by_id = {session["id"]: session for session in sessions}
    root = result["root_session_id"]
    if len(by_id) != len(sessions) or root not in by_id:
        raise ValueError("Conversation IDs must be unique and include the bundle root.")
    for session in sessions:
        event_ids = {event["id"] for event in session["events"]}
        if len(event_ids) != len(session["events"]):
            raise ValueError("Event IDs must be unique within each conversation.")
        state = session.get("context_state") or {}
        through = state.get("through", 0)
        if len(state.get("summary", "").encode("utf-8")) > SUMMARY_MAX_BYTES:
            raise ValueError(f"The conversation summary exceeds the {SUMMARY_MAX_BYTES:,}-byte limit.")
        if through > len(session["wire"]) or through and through < len(session["wire"]) and session["wire"][through]["role"] != "user":
            raise ValueError("The saved summary must end at a complete turn boundary.")
        if through and not state.get("summary"):
            raise ValueError("A summarized prefix requires a conversation summary.")
        _validate_wire(session["wire"])
        seen, current = set(), session["id"]
        while current != root:
            if current in seen or current not in by_id:
                raise ValueError("Included conversations must be descendants of the bundle root.")
            seen.add(current)
            current = by_id[current].get("parent_session_id")
    return result


def _validate_wire(wire, *, complete=False):
    pending = set()
    for message in wire:
        role = message["role"]
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not call_id or call_id not in pending or message.get("tool_calls"):
                raise ValueError("Tool results must match the preceding assistant tool requests.")
            pending.remove(call_id)
        else:
            if pending or message.get("tool_call_id") or role != "assistant" and message.get("tool_calls"):
                raise ValueError("Conversation contains an incomplete tool exchange before a later message.")
            for call in message.get("tool_calls") or []:
                if call["id"] in pending:
                    raise ValueError("Tool call IDs must be unique within an assistant response.")
                pending.add(call["id"])
        if role in ("user", "tool") and not isinstance(message.get("content"), str):
            raise ValueError("User messages and tool results require text content.")
    if complete and pending:
        raise ValueError("Only a completed conversation can be forked.")


def export_bundle(sessions, root_id):
    records = []
    for source in sessions:
        record = {key: value for key, value in source.items() if key in Conversation.model_fields}
        record["events"] = [{key: value for key, value in event.items() if key in Event.model_fields}
                            for event in record["events"]]
        if record.get("context_state"):
            record["context_state"] = {key: value for key, value in record["context_state"].items() if key in ContextState.model_fields}
        records.append(record)
    bundle = {"format": FORMAT, "version": 1, "root_session_id": root_id,
              "exported_at": time.time(), "sessions": records}
    # Check the projected archive before validation/copying; never materialize a
    # second complete JSON string for an oversized history.
    remaining = MAX_BUNDLE_BYTES
    for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(bundle):
        if len(chunk) > remaining:
            raise ValueError("Conversation bundle exceeds 16 MiB. Try exporting without subagents.")
        remaining -= len(chunk.encode("utf-8"))
        if remaining < 0:
            raise ValueError("Conversation bundle exceeds 16 MiB. Try exporting without subagents.")
    return validate_bundle(bundle)


def completed_for_fork(session):
    wire = session["wire"]
    reason = session.get("terminal_reason")
    if reason not in (None, "completed") or not wire or wire[-1]["role"] != "assistant" or wire[-1].get("tool_calls"):
        raise ValueError("Only a completed conversation can be forked. Finish a response first.")
    if any(event.get("state") in ("pending", "running") or (event.get("request_info") or {}).get("status") == "running"
           for event in session["events"]):
        raise ValueError("Only a completed conversation can be forked.")
    if reason is None:
        # Older records lack a terminal marker. An archived assistant error or
        # partial draft is not proof that its latest turn completed.
        events = session["events"]
        start = max((index for index, event in enumerate(events) if event["type"] == "user"), default=0)
        last = events[-1] if events else {}
        info = last.get("request_info") or {}
        if (last.get("type") != "assistant" or not last.get("text")
                or info.get("status", "completed") != "completed"
                or info.get("finish_reason", "stop") != "stop"
                or any(event["type"] == "error" or event.get("state") in ("error", "cancelled", "pending", "running")
                       for event in events[start:])):
            raise ValueError("This older conversation has no confirmed completed response to fork.")
    _validate_wire(wire, complete=True)


def _remap_calls(source):
    """Give each exchange its own IDs, including providers' reused call IDs."""
    occurrences, active = {}, {}
    for message in source["wire"]:
        for call in message.get("tool_calls") or []:
            old_call = call["id"]
            entry = {"id": "call_" + uuid.uuid4().hex, "name": call["function"]["name"], "result": None}
            occurrences.setdefault(old_call, []).append(entry)
            active[old_call] = entry
            call["id"] = entry["id"]
        if message.get("tool_call_id"):
            entry = active.pop(message["tool_call_id"])
            message["tool_call_id"] = entry["id"]
            entry["result"] = message["content"]

    grouped = {}
    for event in source["events"]:
        if event.get("call_id"):
            grouped.setdefault(event["call_id"], []).append(event)
    event_calls = {}
    for old_call, group in grouped.items():
        entries = occurrences.get(old_call, [])
        aligned = len(entries) == len(group) and all(event.get("name") == entry["name"] for event, entry in zip(group, entries))
        by_result = {}
        for entry in entries:
            signature = (entry["name"], entry["result"])
            by_result[signature] = None if signature in by_result else entry["id"]
        used = set()
        for index, event in enumerate(group):
            matched = entries[index]["id"] if aligned else by_result.get((event.get("name"), event.get("output")))
            # Missing/ambiguous legacy events must not lend their recorded result
            # to a different interrupted request with the same provider ID.
            new_call = matched if matched and matched not in used else "call_" + uuid.uuid4().hex
            used.add(new_call)
            event_calls[event["id"]] = (old_call, new_call)

    latest = {}
    for event in source["events"]:
        if event.get("call_id"):
            old_call, new_call = event_calls[event["id"]]
            latest[old_call] = event["call_id"] = new_call
        elif event.get("name") in ("hook_before_tool", "hook_after_tool", "hook_tool_failure") and isinstance(event.get("input"), dict):
            arguments = event["input"]
            old_call = arguments.pop("call_id", None)
            if isinstance(old_call, str) and old_call in latest:
                arguments["call_id"] = latest[old_call]
    unique = {old_call: entries[0]["id"] for old_call, entries in occurrences.items() if len(entries) == 1}
    return event_calls, unique


def _parent_call(links, event_id, call_id):
    event_calls, unique = links
    event_call = event_calls.get(event_id)
    return event_call[1] if event_call and event_call[0] == call_id else unique.get(call_id)


def fresh_copies(bundle, workspace, *, kind="import"):
    """New identity and no live ownership. Historical requests are never replayed."""
    sessions = copy.deepcopy(bundle["sessions"])
    ids = {source["id"]: str(uuid.uuid4()) for source in sessions}
    events = {source["id"]: {event["id"]: str(uuid.uuid4()) for event in source["events"]} for source in sessions}
    calls = {source["id"]: _remap_calls(source) for source in sessions}
    now = time.time()
    for source in sessions:
        old_id, old_parent = source["id"], source.pop("parent_session_id", None)
        parent_event = source.pop("parent_event_id", None)
        parent_call = source.pop("parent_call_id", None)
        source.update(id=ids[old_id], workspace=workspace, created=now, updated=now,
                      permission_mode="manual", allowed_directories=[], active_skills=[],
                      provenance={"kind": kind, "source_session_id": old_id, "source_workspace": source["workspace"],
                                  "source_created": source["created"], "copied_at": now})
        if old_id == bundle["root_session_id"] and kind == "fork":
            source["title"] = (source["title"] + " (fork)")[:1000]
        if old_id != bundle["root_session_id"] and old_parent in ids:
            source.update(parent_session_id=ids[old_parent], is_subagent=True)
            if parent_event in events[old_parent]:
                source["parent_event_id"] = events[old_parent][parent_event]
            remapped_call = _parent_call(calls[old_parent], parent_event, parent_call)
            if remapped_call:
                source["parent_call_id"] = remapped_call
        if source.get("delegation") and not source["delegation"].get("terminal_reason"):
            source["delegation"].update(status="interrupted", terminal_reason="interrupted")
        for event in source["events"]:
            event["id"] = events[old_id][event["id"]]
            child = event.pop("child_session_id", None)
            if child in ids:
                event["child_session_id"] = ids[child]
            origin = event.pop("origin", None)
            if origin and origin["parent_session_id"] in ids:
                parent = origin["parent_session_id"]
                if origin["parent_event_id"] in events[parent]:
                    event["origin"] = {"kind": "delegated", "parent_session_id": ids[parent],
                                       "parent_event_id": events[parent][origin["parent_event_id"]],
                                       "parent_call_id": _parent_call(calls[parent], origin["parent_event_id"], origin.get("parent_call_id"))}
            if event.get("state") in ("pending", "running"):
                event["state"] = "cancelled"
                source["terminal_reason"] = "interrupted"
            if (event.get("request_info") or {}).get("status") == "running":
                event["request_info"].update(status="interrupted", error_kind="incomplete_response")
                source["terminal_reason"] = "interrupted"
            if event.get("delegation") and not event["delegation"].get("terminal_reason"):
                event["delegation"].update(status="interrupted", terminal_reason="interrupted")
    return sessions, ids[bundle["root_session_id"]]
