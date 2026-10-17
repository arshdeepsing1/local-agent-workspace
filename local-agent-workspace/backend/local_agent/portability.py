"""Bounded, data-only conversation transfer; never restore runtime authority."""
import copy
import json
import math
import time
import uuid
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from . import uploads as attached
from .composer_api import MAX_QUEUED
from .config import MAX_MAX_AGENT_STEPS, MAX_TURN_MINUTES, MAX_TURN_TOKENS, MIN_MAX_AGENT_STEPS
from .context import LARGE_SUMMARY_MAX_BYTES, MAX_ESTIMATE_SCALE, context_boundary
from .extensions import HOOK_CONTEXT_CHARS, HOOK_EVENTS, HOOK_REPORT_CHARS, MAX_HOOKS
from .lifecycle_hooks import SHOWN_ARGUMENTS_BYTES
from .planning import MAX_STEPS, subagent_index
from .reasoning_replay import MAX_SIDECAR_BYTES, MAX_SIDECAR_ITEMS, REASONING_ITEMS
from .spending import MAX_LIMIT_DBU


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


class RateLimitDetails(Record):
    """What a final 429 said (agents.rate_limit_fields): the limit it named, its code and message, and the wait."""
    limit_type: str | None = Field(default=None, min_length=1, max_length=64)
    limit: int | None = Field(default=None, ge=0)
    current: int | None = Field(default=None, ge=0)
    retry_after: float | None = Field(default=None, ge=0)
    code: str | None = Field(default=None, max_length=64)
    message: str | None = Field(default=None, max_length=200)
    retry_after_header: str | None = Field(default=None, max_length=64)


Effort = Literal["default", "low", "medium", "high", "xhigh", "max"]  # reasoning.EFFORT_LEVELS
# A built-in subagent type or a custom agent's name (custom_agents.NAME).
AgentName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")]


class ReasoningInfo(Record):
    """The reasoning a request asked for (reasoning.request_record)."""
    effort: Effort
    format: Literal["anthropic", "anthropic_budget", "openai", "none"]
    fields: dict[str, Any] = Field(default_factory=dict)
    sent_effort: str | None = Field(default=None, max_length=16)
    effort_field: str | None = Field(default=None, max_length=64)
    skipped: Literal["rejected", "rejected_earlier"] | None = None
    rejected_field: str | None = Field(default=None, max_length=64)


class RequestInfo(Record):
    model: str = Field(max_length=256)
    status: Literal["running", "completed", "error", "cancelled", "interrupted"]
    finish_reason: str | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    error_kind: str | None = None
    usage: Usage | None = None
    rate_limit: RateLimitDetails | None = None
    reasoning: ReasoningInfo | None = None
    # A reply from Settings → Fallback endpoint (model above): the chat's endpoint and why it failed.
    fallback_from: str | None = Field(default=None, max_length=256)
    fallback_reason: str | None = Field(default=None, max_length=64)
    # Earlier replies of the turn this request sent with their signed thinking (reasoning_replay.py).
    reasoning_replayed: int | None = Field(default=None, ge=0)


class PendingCard(Record):
    """The card a running subagent waits on (planning.DelegateManager.pending_card)."""
    event_id: str = Field(min_length=1, max_length=512)
    name: str = Field(max_length=120)
    kind: Literal["approval", "question", "plan"]
    title: str = Field(max_length=200)
    warnings: list[Annotated[str, Field(max_length=80)]] | None = Field(default=None, max_length=8)
    review: bool | None = None  # The title is not the whole action, or the card warns: no Allow from the parent.
    expires: float | None = None


class Progress(Record):
    status: str
    completed_tools: int = Field(ge=0)
    last_tool: str | None = None
    terminal_reason: str | None = None
    agent_type: AgentName | None = None
    model: str | None = Field(default=None, max_length=256)  # The model and effort the subagent started with.
    effort: Effort | None = None
    started: float | None = None
    ended: float | None = None
    background: bool | None = None  # delegate_task background: the parent went on while it worked.
    pending: PendingCard | None = None


class SiblingChange(Record):
    """sibling_change: another running subagent of the chat changed this file during its run (its card asked)."""
    subagent_id: str = Field(min_length=1, max_length=512)
    agent_type: AgentName
    path: str = Field(max_length=4096)


class Origin(Record):
    kind: Literal["delegated"]
    parent_session_id: str
    parent_event_id: str
    parent_call_id: str | None = None


class Answer(Record):
    """One answer to an ask_user question."""
    question: str = Field(max_length=500)
    selected: list[Annotated[str, Field(max_length=80)]] = Field(max_length=4)
    other: str | None = Field(default=None, max_length=1000)


UploadId = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ImageType = Literal["image/png", "image/jpeg", "image/gif", "image/webp"]  # uploads.IMAGE_EXTENSIONS
Pixels = Annotated[int, Field(ge=1, le=100_000)]


class Attachment(Record):
    """Metadata of an @ file snapshot, a ! command output, or a document or image attached from the composer
    (uploads.py), sent with a user message (never its content)."""
    kind: Literal["file", "shell", "document", "image"]
    path: str | None = Field(default=None, max_length=4096)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=0)
    truncated: bool | None = None
    command: str | None = Field(default=None, max_length=2000)
    exit_code: int | None = None
    # Attached documents and images: the file's name and upload id, a document's format, the characters sent
    # (units: its pages, sheets or slides), whether it went as a path only; an image's type, size and bytes.
    name: str | None = Field(default=None, max_length=attached.NAME_CHARS)
    upload: UploadId | None = None
    format: Literal["pdf", "docx", "xlsx", "pptx", "csv", "tsv", "txt", "md"] | None = None  # documents.EXTENSIONS
    chars: int | None = Field(default=None, ge=0)
    units: Literal["page", "sheet", "slide"] | None = None
    count: int | None = Field(default=None, ge=0)
    as_path: bool | None = None
    media_type: ImageType | None = None
    width: Pixels | None = None
    height: Pixels | None = None
    bytes: int | None = Field(default=None, ge=0)


# A skill ID or a command template's name (extensions.SKILL_IDENTIFIER).
SkillId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]


class CommandRef(Record):
    """A command template a user message ran (AgentManager.message_parts): its name and file, never its text."""
    name: SkillId
    path: str = Field(min_length=1, max_length=4096)


class Withheld(Record):
    """A read result the model did not get (result_budget): its estimate and the allowance."""
    estimated_tokens: int = Field(ge=0)
    allowance_tokens: int = Field(ge=0)


def _shown_arguments(value):
    if isinstance(value, dict) and len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > SHOWN_ARGUMENTS_BYTES:
        raise ValueError("Hook arguments are too large.")
    return value


# What lifecycle_hooks records about hooks; the chat view reads these shapes, so an import must match them.
HookId = Annotated[str, Field(min_length=1, max_length=64)]
HookIds = Annotated[list[HookId], Field(min_length=1, max_length=MAX_HOOKS)]
HookText = Annotated[str, Field(max_length=HOOK_CONTEXT_CHARS)]  # a reason or context, as parse_hook_output clips it
# lifecycle_hooks.shown_arguments: the arguments object, or its JSON text shortened when it is large.
ShownArguments = Annotated[dict[str, Any] | Annotated[str, Field(max_length=SHOWN_ARGUMENTS_BYTES // 4)],
                           AfterValidator(_shown_arguments)]


class HookVerdict(Record):
    """hook_ask or hook_blocked: the hook that asked for a card or blocked a call or message."""
    hook_id: HookId
    reason: HookText | None = None


class HookChange(Record):
    """hook_change: the before-tool hooks that replaced a call's arguments and the agent's proposal."""
    hooks: HookIds
    original: ShownArguments


class HookEdit(Record):
    """hook_edit: the user_prompt hooks that replaced a message and the user's own text."""
    hooks: HookIds
    original: str = Field(max_length=100_000)  # A message, or queued messages sent as one.


class HookNote(Record):
    """One entry of hook_notes: context a hook added for the model after a message."""
    event: Literal[HOOK_EVENTS]
    hook_id: HookId
    context: HookText


class ArgumentsChange(Record):
    before: ShownArguments
    after: ShownArguments


class PromptChange(Record):
    before: str = Field(max_length=4000)
    after: str = Field(max_length=4000)


class HookResult(Record):
    """hook_result: what a hook card shows of a structured result (LifecycleHooks.hook_summary)."""
    model_config = ConfigDict(serialize_by_alias=True)
    structured: bool | None = Field(default=None, alias="json")  # "json" would shadow BaseModel.json.
    decision: Literal["deny", "ask", "block"] | None = None
    reason: HookText | None = None
    context: HookText | None = None
    report: str | None = Field(default=None, max_length=HOOK_REPORT_CHARS)
    arguments: ArgumentsChange | None = None
    prompt: PromptChange | None = None
    ignored: list[Annotated[str, Field(max_length=40)]] | None = Field(default=None, max_length=16)


class Event(Record):
    id: str = Field(min_length=1, max_length=512)
    type: Literal["user", "assistant", "tool", "notice", "error"]
    created: float | None = None
    text: str | None = None
    reasoning_summary: str | None = None
    reasoning_truncated: bool | None = None
    # When the reply's thinking began (epoch seconds), how long it took (the Thinking block's label) and
    # how long after the request was sent its first thinking arrived.
    reasoning_started: float | None = Field(default=None, ge=0)
    reasoning_seconds: float | None = Field(default=None, ge=0)
    reasoning_wait_seconds: float | None = Field(default=None, ge=0)
    request_info: RequestInfo | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    output: str | None = None
    # What the model received when it differs from output (a tool error's hint, a withheld
    # or shortened result); the wire's tool message holds the same text.
    model_output: str | None = None
    result_withheld: Withheld | None = None
    result_shortened: bool | None = None
    preview: str | None = None
    state: Literal["pending", "running", "completed", "rejected", "cancelled", "error"] | None = None
    approval: Literal["approved", "declined", "expired", "blocked"] | None = None
    approval_expires: float | None = None
    decline_note: str | None = None
    allowed_by_rule: bool | None = None
    approved_by_mode: Literal["manual", "auto", "acceptEdits", "plan", "bypassPermissions", "dontAsk"] | None = None
    read_only_command: bool | None = None
    rule_scope: Literal["chat", "project", "user"] | None = None
    without_shell: bool | None = None
    blocked_reason: str | None = Field(default=None, max_length=4000)
    edited: bool | None = None
    protected: str | None = Field(default=None, max_length=500)
    call_id: str | None = None
    child_session_id: str | None = None
    origin: Origin | None = None
    delegation: Progress | None = None
    # A background delegation's final result (its card's Report), and the parent's notice when it ended.
    delegation_result: dict[str, Any] | None = None
    subagent_id: str | None = Field(default=None, max_length=512)
    subagent_status: Literal["completed", "failed", "cancelled", "interrupted"] | None = None
    sibling_change: SiblingChange | None = None
    background_wake: bool | None = None  # The app's message that continued a chat after a background subagent.
    terminal_reason: str | None = None
    error_kind: str | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    answers: list[Answer] | None = Field(default=None, max_length=4)
    question_outcome: Literal["answered", "skipped", "expired"] | None = None
    plan_outcome: Literal["approved", "keep_planning", "expired", "superseded"] | None = None
    plan_mode: Literal["acceptEdits", "manual"] | None = None
    feedback: str | None = Field(default=None, max_length=2000)
    skipped_reason: Literal["dont_ask"] | None = None
    # User events: in_wire is true once the text reached the model history as its own
    # message (absent on events saved before it existed); steered marks a message sent
    # while the agent was working. Notices: stop_phase says what a Stop interrupted.
    in_wire: bool | None = None
    steered: bool | None = None
    # User events: the command templates the message ran (queued messages sent as one can run several),
    # the skills it selected for itself (/<skill-id>), and whether it was sent as plain text (verbatim;
    # Retry and Edit send it so again) or from the queue (queued: it selected no skill).
    commands: list[CommandRef] | None = Field(default=None, max_length=MAX_QUEUED)
    skills: list[SkillId] | None = Field(default=None, max_length=3)
    verbatim: bool | None = None
    queued: bool | None = None
    git_context: bool | None = None  # User events: the app sent a Git snapshot with it (git_tools.git_context).
    # At most 5 @ files and 5 ! outputs (mentions.py) and 8 attached files (uploads.MAX_MESSAGE_FILES).
    attachments: list[Attachment] | None = Field(default=None, max_length=10 + attached.MAX_MESSAGE_FILES)
    stop_phase: Literal["streaming", "tools", "waiting"] | None = None
    details: str | None = Field(default=None, max_length=1000)  # A rate-limit notice's Details (the 429).
    # Hooks (lifecycle_hooks): a hook card's structured result; on a tool card, the arguments a
    # before-tool hook replaced, the card it asked for, or its deny; on a user message, a hook's
    # edit or block and the context hooks added for the model.
    hook_result: HookResult | None = None
    hook_change: HookChange | None = None
    hook_ask: HookVerdict | None = None
    hook_blocked: HookVerdict | None = None
    hook_edit: HookEdit | None = None
    hook_notes: list[HookNote] | None = Field(default=None, max_length=2 * MAX_HOOKS)


class Function(Record):
    name: str = Field(min_length=1, max_length=256)
    arguments: str


class ToolCall(Record):
    id: str = Field(min_length=1, max_length=512)
    type: Literal["function"]
    function: Function


def _reasoning_items(items):
    if any(item.get("type") not in REASONING_ITEMS for item in items):
        raise ValueError("Reasoning items must be reasoning, thinking or redacted thinking.")
    if len(json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_SIDECAR_BYTES:
        raise ValueError("A reply's reasoning items are too large.")
    return items


class ReasoningSidecar(Record):
    """A reply's signed reasoning (reasoning_replay.py): the endpoint that wrote it and its items as streamed.
    In a conversation file only when it was exported with reasoning."""
    model: str = Field(min_length=1, max_length=256)
    items: Annotated[list[dict[str, Any]], Field(min_length=1, max_length=MAX_SIDECAR_ITEMS),
                     AfterValidator(_reasoning_items)]
    bytes: int = Field(ge=0, le=MAX_SIDECAR_BYTES)


class ImageRef(Record):
    """An image a user message carries (uploads.py): sent as an image part, kept in the chat's uploads folder.
    Only forks keep them; an export turns each into a note in its message's text."""
    upload: UploadId
    name: str = Field(max_length=attached.NAME_CHARS)
    media_type: ImageType
    width: Pixels
    height: Pixels
    bytes: int = Field(ge=1, le=attached.MAX_IMAGE_BYTES)


class Message(Record):
    role: Literal["user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = Field(default=None, max_length=256)
    tool_call_id: str | None = None
    reasoning: ReasoningSidecar | None = None  # Assistant replies only.
    images: list[ImageRef] | None = Field(default=None, max_length=attached.MAX_MESSAGE_IMAGES)  # User messages only.


class ContextState(Record):
    summary: str = Field(default="", max_length=LARGE_SUMMARY_MAX_BYTES)
    through: int = Field(default=0, ge=0)
    compactions: int = Field(default=0, ge=0)


class InstructionSource(Record):
    path: str
    scope: str
    status: Literal["loaded", "trimmed", "omitted"]
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
    summary_adjustment: Literal["condensed", "condensed_trimmed", "trimmed"] | None = None


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
    reasoning_effort: Effort | None = None  # Kept by forks and imports, like the model.
    spending_limit_dbu: float | None = Field(default=None, ge=0, le=MAX_LIMIT_DBU)  # Kept too (not the spend).
    # Kept too, like the spending limit: the chat's own turn limits (0 = none) and a script's Agent steps.
    turn_token_limit: int | None = Field(default=None, ge=0, le=MAX_TURN_TOKENS)
    turn_time_limit_minutes: int | None = Field(default=None, ge=0, le=MAX_TURN_MINUTES)
    max_agent_steps: int | None = Field(default=None, ge=MIN_MAX_AGENT_STEPS, le=MAX_MAX_AGENT_STEPS)
    max_steps: int | None = Field(default=None, ge=1, le=MAX_STEPS)
    agent_type: AgentName | None = None
    # Kept by forks and imports, like tool_profile: a copy never gets more tools than its source.
    disabled_tools: list[Annotated[str, Field(min_length=1, max_length=300)]] | None = Field(default=None, max_length=64)
    # A custom agent's subagent: only these tools (custom_agents.py); its file, and the model and effort its agent set.
    agent_tools: list[Annotated[str, Field(min_length=1, max_length=300)]] | None = Field(default=None, max_length=64)
    agent_file: str | None = Field(default=None, max_length=300)
    agent_model: str | None = Field(default=None, max_length=256)
    agent_effort: Effort | None = None
    # What its agent file took away or narrowed: disallowed_tools, mcp_servers, permission_mode, memory: none.
    agent_denied_tools: list[Annotated[str, Field(min_length=1, max_length=300)]] | None = Field(default=None, max_length=64)
    agent_mcp_servers: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,19}$")]] | None = Field(default=None,
                                                                                                    max_length=20)
    agent_permission_mode: Literal["manual", "auto", "acceptEdits", "plan", "bypassPermissions", "dontAsk"] | None = None
    agent_memory: Literal["none"] | None = None
    # The user's labels. Pinned and archived are view state of this app and are not exported.
    tags: list[Annotated[str, Field(min_length=1, max_length=32)]] | None = Field(default=None, max_length=10)


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
        if len(state.get("summary", "").encode("utf-8")) > LARGE_SUMMARY_MAX_BYTES:
            raise ValueError(f"The conversation summary exceeds the {LARGE_SUMMARY_MAX_BYTES:,}-byte limit.")
        if through > len(session["wire"]) or not context_boundary(session["wire"], through):
            raise ValueError("The saved summary must end at a complete turn or tool-exchange boundary.")
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
        if role != "assistant" and message.get("reasoning") is not None:
            raise ValueError("Only assistant replies can carry reasoning.")
        if role != "user" and message.get("images") is not None:
            raise ValueError("Only user messages can carry images.")
    if complete and pending:
        raise ValueError("Only a completed conversation can be forked.")


def export_bundle(sessions, root_id, with_reasoning=False, keep_images=False):
    """A validated bundle of these conversations. Replies' signed reasoning (reasoning_replay's sidecars) is
    left out unless with_reasoning (an export "with reasoning", and forks, which keep the chat's endpoint).
    Attached images stay references only in a fork (keep_images: it gets copies of the files); an export has
    no image files, so each becomes a note in its message's text (attached documents are in that text)."""
    records = []
    for source in sessions:
        record = {key: value for key, value in source.items() if key in Conversation.model_fields}
        if not with_reasoning:
            record["wire"] = [{key: value for key, value in message.items() if key != "reasoning"}
                              if isinstance(message, dict) and "reasoning" in message else message
                              for message in record.get("wire", [])]
        if not keep_images:
            record["wire"] = [attached.images_as_notes(message, attached.NOT_EXPORTED) for message in record.get("wire", [])]
        record["events"] = [{key: value for key, value in event.items() if key in Event.model_fields}
                            for event in record["events"]]
        # Keep only the fields a bundle defines; newer display details such as
        # a compaction's handoff path must not make the whole export invalid.
        for key, model in (("context_state", ContextState), ("context_info", ContextInfo)):
            if record.get(key):
                record[key] = {name: value for name, value in record[key].items() if name in model.model_fields}
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
    if reason not in (None, "completed", "hook_blocked") or not wire or wire[-1]["role"] != "assistant" or wire[-1].get("tool_calls"):
        raise ValueError("Only a completed conversation can be forked. Finish a response first.")
    if any(event.get("state") in ("pending", "running") or (event.get("request_info") or {}).get("status") == "running"
           for event in session["events"]):
        raise ValueError("Only a completed conversation can be forked.")
    if reason is None and not latest_turn_completed(session):
        raise ValueError("This older conversation has no confirmed completed response to fork.")
    _validate_wire(wire, complete=True)


def latest_turn_completed(session):
    """Whether the events show the latest turn ended in a completed reply.

    Older records lack a terminal marker. An archived assistant error or
    partial draft is not proof that its latest turn completed.
    """
    events = session["events"]
    start = max((index for index, event in enumerate(events) if event["type"] == "user"), default=0)
    last = events[-1] if events else {}
    info = last.get("request_info") or {}
    return not (last.get("type") != "assistant" or not last.get("text")
                or info.get("status", "completed") != "completed"
                or info.get("finish_reason", "stop") != "stop"
                or any(event["type"] == "error" or event.get("state") in ("error", "cancelled", "pending", "running")
                       for event in events[start:]))


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
                      permission_mode="manual", allowed_directories=[], allow_rules=[], active_skills=[],
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
            source["delegation"].pop("pending", None)
        if kind != "fork":
            # An imported image has no file in this app: a note says what it was.
            source["wire"] = [attached.images_as_notes(message, attached.NOT_EXPORTED) for message in source["wire"]]
        for event in source["events"]:
            event["id"] = events[old_id][event["id"]]
            if kind != "fork" and isinstance(event.get("attachments"), list):
                # Nor are attached files imported: their chips keep the names, and Retry response and Edit
                # message send the message without them (a document's text stays in the message sent then).
                event["attachments"] = [{key: value for key, value in item.items() if key != "upload"}
                                        if isinstance(item, dict) and item.get("kind") in ("document", "image")
                                        else item for item in event["attachments"]]
            child = event.pop("child_session_id", None)
            if child in ids:
                event["child_session_id"] = ids[child]
            subagent = event.pop("subagent_id", None)  # A background subagent's notice: the copy, or none.
            if subagent in ids:
                event["subagent_id"] = ids[subagent]
            if str(event.get("name")).startswith("hook_subagent_") and isinstance(event.get("input"), dict):
                # A subagent event's hook card names its subagent: the copy, or none outside the bundle.
                subagent = event["input"].pop("subagent_session_id", None)
                if subagent in ids:
                    event["input"]["subagent_session_id"] = ids[subagent]
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
                event["delegation"].pop("pending", None)
    # The subagents index of each copied parent lists its copied children (a bundle does not carry it).
    by_id = {source["id"]: source for source in sessions}
    for parent_id, index in subagent_index(sessions).items():
        by_id[parent_id]["subagents"] = index
    return sessions, ids[bundle["root_session_id"]]
