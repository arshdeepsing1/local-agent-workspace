"""Bounds on tool results: a hard cap per result, and a context allowance per model response.

One model response can ask for many tool calls, and the next request must carry all
of their results next to the latest exchange (which compaction keeps verbatim). The
allowance (ResultAllowance) is what is left of the input budget for those results;
agents.execute_tool charges each result against it. A read that does not fit is not
sent (the model is told to ask again in its next response); a result of an action that
already ran (a command, an edit, an MCP call) is shortened instead, because its side
effects happened. Nothing already in the history is ever shortened here.
"""
import json
import math

from .context import estimate_text_tokens

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
