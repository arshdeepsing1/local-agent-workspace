"""Bounds on tool results: a hard cap per result, a context allowance per model response, and the clipping of
results that are already in the history.

One model response can ask for many tool calls, and the next request must carry all
of their results next to the latest exchange (which compaction keeps verbatim). The
allowance (ResultAllowance) is what is left of the input budget for those results;
agents.execute_tool charges each result against it. A read that does not fit is not
sent (the model is told to ask again in its next response); a result of an action that
already ran (a command, an edit, an MCP call) is shortened instead, because its side
effects happened. Nothing already in the history is shortened by the allowance. The clipping helpers below
shorten a result's text for the summarizer's private copy (clip_result) and for the live context's trim
(trimmed_content), which replaces an old result with a notice and keeps the event's full result.
"""
import json
import math
import re

TOOL_RESULT_HARD_LIMIT = 131_072  # Bytes of one tool result as the model and the card receive it.
ALLOWANCE_MARGIN_TOKENS = 1_024  # The next step's notices and other small changes.
# A tool message's JSON framing (role, tool_call_id and quotes) next to its content.
RESULT_FRAMING_TOKENS = 40
SHORTEN_CHARS = 1_500
SHORT_STRING_CHARS = 200
MAX_PASSES = 20
OMITTED = "\n[… {n:,} characters omitted …]\n"
MARKER_RESERVE = 64  # Bytes kept free for one OMITTED marker.
SHORTENED_HINT = ("This step's context allowance was used up by earlier results, so the app shortened this result; "
                  "the action itself ran.")
COMMAND_SHORTENED_HINT = SHORTENED_HINT + " Read the rest of the output with get_job_output and its job_id."
MCP_SHORTENED_HINT = SHORTENED_HINT + " The whole result is kept: read it with get_tool_output, its result_id and next_offset."
JOB_TOOLS = ("wait_job", "get_job_output", "list_jobs")
# Tools whose results are always sent in full and charged: a job page (job_seen() has
# advanced), the user's own answers and plan feedback, which are short and bounded
# (interactions.MAX_OTHER, MAX_FEEDBACK) and exist nowhere else, and a background subagent's
# bounded result (planning.RESULT_BYTES), which its notice no longer repeats once returned.
FULL_RESULT_TOOLS = (*JOB_TOOLS, "ask_user", "submit_plan", "get_subagent_result")


def _bytes(text):
    return len(text.encode("utf-8"))


def _serialized(value):
    """The text execute_tool sends for a dict or list result."""
    return json.dumps(value, indent=2)


def _head(text, size):
    return text.encode("utf-8")[:max(0, size)].decode("utf-8", errors="ignore")


def _tail(text, size):
    data = text.encode("utf-8")
    return data[len(data) - max(0, min(size, len(data))):].decode("utf-8", errors="ignore")


def head_and_tail(text, keep_chars):
    """(text, omitted characters): the first two thirds and last third of keep_chars
    characters around an OMITTED marker, or text itself when it is not longer."""
    if len(text) <= keep_chars:
        return text, 0
    keep = max(0, keep_chars)
    head = keep * 2 // 3
    tail = keep - head
    omitted = len(text) - head - tail
    return text[:head] + OMITTED.format(n=omitted) + (text[len(text) - tail:] if tail else ""), omitted


def cap_text(text, limit=TOOL_RESULT_HARD_LIMIT):
    """text within limit UTF-8 bytes: its head (cut at a character boundary) and a marker."""
    if _bytes(text) <= limit:
        return text
    head = _head(text, limit - MARKER_RESERVE)
    return head + OMITTED.format(n=len(text) - len(head))


def _strings(value, path=()):
    """(path, string) for every string value inside dicts and lists."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, (*path, index))


def _replace(value, path, new):
    """A copy of value with the string at path replaced by new."""
    if not path:
        return new
    head, rest = path[0], path[1:]
    if isinstance(value, dict):
        return {**value, head: _replace(value[head], rest, new)}
    copy = list(value)
    copy[head] = _replace(copy[head], rest, new)
    return copy


def bound_result(result, limit=TOOL_RESULT_HARD_LIMIT):
    """result with its serialized text (a str as it is, a dict or list as indent=2 JSON) within
    limit bytes. The longest strings keep their head and tail around a marker; a dict also gets
    "truncated": True and "omitted_bytes". The result stays valid JSON."""
    if isinstance(result, str):
        if _bytes(result) <= limit:
            return result
        available = limit - MARKER_RESERVE
        head = _head(result, available * 2 // 3)
        tail = _tail(result, available - _bytes(head))
        return head + OMITTED.format(n=len(result) - len(head) - len(tail)) + tail
    if not isinstance(result, (dict, list)):
        return result
    original = _bytes(_serialized(result))
    if original <= limit:
        return result
    value, omitted = result, 0
    for _ in range(MAX_PASSES):
        size = _bytes(_serialized(value))
        if size <= limit:
            break
        found = max(_strings(value), key=lambda item: len(item[1]), default=None)
        if found is None or len(found[1]) <= SHORT_STRING_CHARS:
            break
        path, text = found
        escaped = len(json.dumps(text)) - 2
        excess = size - limit + MARKER_RESERVE + 64
        keep = max(0, int(len(text) * max(0, escaped - excess) / max(1, escaped)))
        shorter, cut = head_and_tail(text, min(keep, len(text) - 1))
        omitted += _bytes(text) - _bytes(shorter) + _bytes(OMITTED.format(n=cut))
        value = _replace(value, path, shorter)
        if isinstance(value, dict):
            value = {**value, "truncated": True, "omitted_bytes": omitted}
    if _bytes(_serialized(value)) <= limit:
        return value
    # Many small values: an excerpt of the serialized text instead.
    text = _serialized(result)
    budget = limit // 2
    while True:
        excerpt, _ = head_and_tail(text, budget)
        fallback = {"truncated": True, "omitted_bytes": original - _bytes(excerpt), "excerpt": excerpt}
        if _bytes(_serialized(fallback)) <= limit or budget < 1_000:
            return fallback
        budget //= 2


def shorten_result(result, chars=SHORTEN_CHARS, hint=SHORTENED_HINT):
    """A short form of an action's result for the model: every non-string value and every string
    of at most SHORT_STRING_CHARS characters kept, longer strings shortened to their head and tail
    within chars characters together (a command's job_id, state and exit_code survive, and so
    does the end of its output, where failures appear)."""
    if not isinstance(result, dict):
        text = result if isinstance(result, str) else _serialized(result)
        excerpt, omitted = head_and_tail(text, chars)
        return {"result_shortened": True, "excerpt": excerpt,
                "omitted_bytes": _bytes(text) - _bytes(excerpt) + _bytes(OMITTED.format(n=omitted)) if omitted else 0,
                "hint": hint}
    long = [(path, text) for path, text in _strings(result) if len(text) > SHORT_STRING_CHARS]
    each = max(SHORT_STRING_CHARS, chars // max(1, len(long)))
    value, omitted = result, 0
    for path, text in long:
        shorter, cut = head_and_tail(text, each)
        if cut:
            omitted += _bytes(text) - _bytes(shorter) + _bytes(OMITTED.format(n=cut))
            value = _replace(value, path, shorter)
    return {**value, "result_shortened": True, "omitted_bytes": omitted, "hint": hint}


def shortened_hint(name):
    """The hint of a shortened result: only a command's output can be read again."""
    return COMMAND_SHORTENED_HINT if name == "run_command" else SHORTENED_HINT


# --- Clipping the text of a result that is already in the history ------------------------------------------

def head_bytes(text, size):
    """The longest prefix of text within size UTF-8 bytes (whole characters only)."""
    return _head(text, size)


def tail_bytes(text, size):
    """The longest suffix of text within size UTF-8 bytes (whole characters only)."""
    return _tail(text, size)


def byte_size(text):
    return _bytes(text)


def kilobytes(size):
    return f"{size / 1024:,.1f} KB" if size < 10 * 1024 else f"{round(size / 1024):,} KB"


OUTLINE_MAX_LINES = 80
ELIDED_RESULT = ("[The app shortened this tool result before summarizing: {omitted:,} of {total:,} bytes omitted "
                 "from the middle. Run the tool again if the details matter.]")
OUTLINE_HEADING = "Definition lines kept by the app (line: text):"
OUTLINE_LINE = re.compile(r"^\d+: \s*(async\s+def|def|class|function|export\s|interface\s|type\s+\w+\s*=|struct\s|"
                          r"enum\s|impl\s|fn\s|func\s|public\s|private\s|protected\s|module\s|package\s|#{1,3}\s)")


def outline_lines(content, head_text, tail_text, limit):
    """Definition lines from the part of a read_file page that head_text and tail_text omit."""
    numbers = lambda text: [int(found) for found in re.findall(r'(?:"|\\n)(\d+): ', text)]
    before, after = max(numbers(head_text), default=0), min(numbers(tail_text), default=math.inf)
    kept, size = [], 0
    for line in content.split("\n"):
        number = re.match(r"(\d+): ", line)
        if not number or not before < int(number.group(1)) < after or not OUTLINE_LINE.match(line):
            continue
        if len(kept) == OUTLINE_MAX_LINES or size + byte_size(line) + 1 > limit:
            break
        kept.append(line)
        size += byte_size(line) + 1
    return kept


def clip_result(text, limit, stats=None):
    """A tool result (or other app text) the summarizer sees: whole when it fits in
    limit bytes, else its head and tail around ELIDED_RESULT. A read_file page also
    keeps the definition lines (class, def, ...) of its omitted middle."""
    total = byte_size(text)
    if total <= limit:
        return text
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("content"), str) and isinstance(parsed.get("path"), str):
        head, tail = head_bytes(text, limit * 2 // 3), tail_bytes(text, limit // 6)
        outline = outline_lines(parsed["content"], head, tail, limit // 4)
        omitted = total - byte_size(head) - byte_size(tail)
        clipped = (head + "\n" + (OUTLINE_HEADING + "\n" + "\n".join(outline) + "\n" if outline else "")
                   + tail + "\n" + ELIDED_RESULT.format(omitted=omitted, total=total))
    else:
        head, tail = head_bytes(text, limit * 2 // 3), tail_bytes(text, limit // 3)
        omitted = total - byte_size(head) - byte_size(tail)
        clipped = head + "\n" + ELIDED_RESULT.format(omitted=omitted, total=total) + "\n" + tail
    if stats is not None:
        stats["elided_bytes"] += omitted
        stats["shortened"] += 1
    return clipped


# --- Notices for results the live context no longer carries --------------------------------------------------

TRIM_KEEP_BYTES = 1_500  # A shortened result keeps this much of its text (two thirds head, one third tail).
TRIM_SMALL_BYTES = 4_096  # The automatic pass drops a result under this size instead of shortening it.
TRIM_DROPPED = "[Cleared by the app to save context: the {what}, {size}. {how}]"
TRIM_SHORTENED = ("[Shortened by the app to save context: the {what}, {omitted:,} of {total:,} bytes omitted from "
                  "the middle. {how}]")
TRIM_FILE = "Read the file again if you need it."
TRIM_COMMAND = "Run the command again or use get_job_output if it was a job."
TRIM_STORED = "Use get_tool_output with result_id {id} to read it again (results are kept for this app run only)."
TRIM_SUBAGENT = "Call get_subagent_result with subagent_id {id} to read the report again."
TRIM_SUBAGENTS = "Call get_subagent_result again to read the report."
READ_TOOLS = ("read_file", "notebook_read")
COMMAND_TOOLS = ("run_command", "get_job_output", "wait_job")
# Results the app never trims, automatically or by hand: the user's own words and decisions, what the model must keep
# to act (instructions, grants, tasks, a saved note) and a foreground subagent's report, which nothing lets the model
# read again (get_subagent_result reads background subagents only).
NEVER_TRIM_TOOLS = ("ask_user", "submit_plan", "submit_report", "remember", "create_task", "update_task", "list_tasks",
                    "get_task", "use_skill", "list_skills", "access_directory", "delegate_task")
# A background subagent's report can be read again with get_subagent_result: it may be shortened, never dropped.
SHORTEN_ONLY_TOOLS = ("get_subagent_result",)
# A result that starts so records the user's decision on the call (an edited approval).
DECISION_MARKERS = ("[The user edited",)


def trim_how(name, content):
    """How the model gets a trimmed result's content again: the result store (a result_id in the result), a file
    read again, a command run again; "" when the notice just says the result was cleared to save context."""
    try:
        parsed = json.loads(content)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("result_id"), str) and parsed["result_id"]:
        return TRIM_STORED.format(id=parsed["result_id"])
    if name in SHORTEN_ONLY_TOOLS:
        found = parsed.get("subagent_id") if isinstance(parsed, dict) else None
        return TRIM_SUBAGENT.format(id=found) if isinstance(found, str) and found else TRIM_SUBAGENTS
    if name in READ_TOOLS:
        return TRIM_FILE
    return TRIM_COMMAND if name in COMMAND_TOOLS else ""


def locked_result(name, content):
    """Whether a tool result is one the app never trims (NEVER_TRIM_TOOLS, or a result that records the user's
    decision on the call)."""
    return name in NEVER_TRIM_TOOLS or (isinstance(content, str) and content.startswith(DECISION_MARKERS))


def trim_mode(name, content, mode, keep=TRIM_KEEP_BYTES):
    """The mode a result is trimmed in when mode is asked for it: "dropped" when the text sent is the cleared notice
    (asked for, or a shortened result too small to keep a head and tail of), else "shortened". None when the result
    would have to be dropped and its tool only allows shortening (SHORTEN_ONLY_TOOLS)."""
    kept = mode == "shortened" and min(keep, _bytes(content) // 2) >= 200
    if not kept and name in SHORTEN_ONLY_TOOLS:
        return None
    return "shortened" if kept else "dropped"


def trimmed_content(content, name, subject, mode, keep=TRIM_KEEP_BYTES):
    """What the model receives in place of a tool result's content: mode "dropped" is a notice that names the
    tool and its subject (a path, command or URL; redacted by the caller), the original size and how to get the
    content again; "shortened" keeps a head and tail of content (keep bytes at most, half of a small result) around
    that notice. The same arguments always give the same text, so every later request sends the same bytes."""
    total = _bytes(content)
    tool = name if isinstance(name, str) and name else "tool"
    what = f"{tool} result" + (f" for {subject}" if subject else "")
    how = trim_how(tool, content)
    keep = min(keep, total // 2)
    if mode != "shortened" or keep < 200:
        return TRIM_DROPPED.format(what=what, size=kilobytes(total), how=how).replace(" ]", "]")
    head, tail = _head(content, keep * 2 // 3), _tail(content, keep // 3)
    omitted = total - _bytes(head) - _bytes(tail)
    notice = TRIM_SHORTENED.format(what=what, omitted=omitted, total=total, how=how).replace(" ]", "]")
    return head + "\n" + notice + "\n" + tail


class ResultAllowance:
    """The input tokens one model response's tool results may still add (see the module docstring).

    Every call of the response sends at least a short text (a deferred read's notice, a
    refusal), so room for one STUB per call (calls) is kept aside until that call's result
    is settled by admit or charge; settled counts the calls settled so far."""

    def __init__(self, total, scale=1.0, calls=0):
        self.total = max(0, int(total))
        self.scale = scale
        self.stub = self.cost(STUB_TEXT)
        self.pending = max(0, int(calls))
        self.left = self.total - self.pending * self.stub
        self.settled = 0

    def cost(self, text):
        """Estimated scaled tokens a result adds to the next request, with its message framing."""
        from .context import estimate_text_tokens  # context imports this module for the clipping helpers below.
        return math.ceil((estimate_text_tokens(text) + RESULT_FRAMING_TOKENS) * self.scale)

    def _settle(self):
        """Release the room kept for the current call's notice."""
        self.settled += 1
        if self.pending:
            self.pending -= 1
            self.left += self.stub

    def admit(self, text):
        """True, and charged, when the result fits what is left (with the room kept for this
        call's notice). False leaves the call unsettled: the caller charges what it sends instead."""
        cost = self.cost(text)
        if cost > self.left + (self.stub if self.pending else 0):
            return False
        self._settle()
        self.left -= cost
        return True

    def charge(self, text):
        """Charge a result that is sent in any case (left may go below zero)."""
        self._settle()
        self.left -= self.cost(text)

    def charge_more(self, sent, charged):
        """A settled result grew before it was sent (hook notes): charge what sent adds to charged."""
        self.left -= max(0, self.cost(sent) - self.cost(charged))


def deferred_text(name, cost, total):
    """The model's result for a read that did not fit this response's allowance."""
    if cost > total:
        message = (f"This {name} result (about {cost:,} tokens) is larger than this step's context allowance "
                   f"({total:,} tokens), so it was not included.")
        hint = "Read a smaller range (start_line and max_lines), or use outline_file or search_files to find the part you need."
    else:
        message = (f"This {name} call ran, but its result (about {cost:,} tokens) was not included: earlier results "
                   f"in this response already use this step's context allowance ({total:,} tokens).")
        hint = "Call it again in your next response, alone or with fewer large reads."
    return json.dumps({"error_kind": "result_deferred", "message": message, "hint": hint})


# The longest deferred-read notice (a long tool name, large numbers): the room kept per call.
STUB_TEXT = max((deferred_text("x" * 64, 10 ** 12, total) for total in (10 ** 12, 1)), key=len)
