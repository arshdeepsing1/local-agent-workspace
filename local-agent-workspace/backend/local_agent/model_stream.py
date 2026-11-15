"""Bound SSE events before decoding, and tool-call fragments before assembly."""
import json

from .telemetry import InferenceError


# One 80 KB file may require six JSON bytes per source byte, then outer escaping.
MAX_SSE_EVENT_BYTES = 1024 * 1024
MAX_TOOL_ARGUMENT_BYTES = 512 * 1024
MAX_TOOL_CALLS = 64
STREAM_CHUNK_BYTES = 4096
MAX_ERROR_BODY_BYTES = 64 * 1024


async def error_body_prefix(response):
    body = bytearray()
    async for chunk in response.aiter_bytes():
        remaining = MAX_ERROR_BODY_BYTES - len(body)
        body.extend(memoryview(chunk)[:remaining])
        if len(body) == MAX_ERROR_BODY_BYTES:
            return body.decode("utf-8", errors="replace"), True
    return body.decode("utf-8", errors="replace"), False


async def sse_data(response):
    line, data = bytearray(), []
    event_bytes, pending_cr, first_line = 0, False, True

    def count_byte():
        nonlocal event_bytes
        event_bytes += 1
        if event_bytes > MAX_SSE_EVENT_BYTES:
            raise InferenceError(
                "The model sent an SSE event larger than 1 MiB. No tool calls from this response ran. "
                "Retry with smaller steps.", "invalid_response")

    def finish_line():
        nonlocal event_bytes, first_line
        if line:
            field = bytes(line)
            line.clear()
            if first_line:
                field = field.removeprefix(b"\xef\xbb\xbf")
            first_line = False
            if field.startswith(b"data:"):
                value = field[5:]
                data.append(value[1:] if value.startswith(b" ") else value)
            elif field == b"data":
                data.append(b"")
        else:
            event_bytes = 0
            if data:
                try:
                    value = b"\n".join(data).decode("utf-8")
                except UnicodeError as error:
                    raise InferenceError("The model sent invalid UTF-8 in its stream. No tool calls from this response ran.",
                                         "invalid_response") from error
                data.clear()
                return value
        return None

    # Do not use aiter_lines (unbounded unfinished lines) or a chunk_size that
    # coalesces small transport chunks and delays live events until its buffer fills.
    async for received in response.aiter_bytes():
        view = memoryview(received)
        for start in range(0, len(view), STREAM_CHUNK_BYTES):
            for byte in view[start:start + STREAM_CHUNK_BYTES]:
                if pending_cr:
                    pending_cr = False
                    if byte == 10:
                        count_byte()
                        value = finish_line()
                        if value is not None:
                            yield value
                        continue
                    value = finish_line()
                    if value is not None:
                        yield value
                count_byte()
                if byte == 13:
                    pending_cr = True
                elif byte == 10:
                    value = finish_line()
                    if value is not None:
                        yield value
                else:
                    line.append(byte)
    if pending_cr:
        value = finish_line()
        if value is not None:
            yield value
    if line or data:
        raise InferenceError("The model stream ended with an incomplete SSE event. No tool calls from this response ran.",
                             "invalid_response")


def well_formed(text):
    """text with each escaped pair of surrogate halves ("\\ud83d\\ude00" in the model's JSON) joined into its
    real character and every lone surrogate replaced by U+FFFD: strict UTF-8 cannot hold one."""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def well_formed_json(value):
    """value (parsed JSON) with well_formed applied to every string and every dict key inside it. Walks with its
    own stack, never by recursion: arguments nested deeper than Python's recursion limit reach it (the tool
    schema check refuses them later). Key and list order stay."""
    if isinstance(value, str):
        return well_formed(value)
    if not isinstance(value, (list, dict)):
        return value
    root = [] if isinstance(value, list) else {}
    stack = [(value, root)]
    while stack:
        source, target = stack.pop()
        for key, item in enumerate(source) if isinstance(source, list) else source.items():
            if isinstance(item, (list, dict)):
                copied = [] if isinstance(item, list) else {}
                stack.append((item, copied))
            else:
                copied = well_formed(item) if isinstance(item, str) else item
            if isinstance(target, list):
                target.append(copied)
            else:
                target[well_formed(key)] = copied
    return root


def well_formed_arguments(text):
    """A call's JSON argument text that strict UTF-8 can hold, whose parsed value is the one the tool receives.
    The text is parsed first and cleaned after: a real half next to an escaped half ("\\ude00") is one pair only
    once the escape is decoded, so cleaning the raw text would turn the real half into U+FFFD first. Text that
    parses to a string with a lone half, or that holds a real one, is written again from the cleaned value. Text
    that is not valid JSON is cleaned as it is (the call is refused later)."""
    try:
        parsed = json.loads(text)
        again = json.dumps(parsed, ensure_ascii=False)
    except (ValueError, RecursionError):
        return well_formed(text)
    try:
        again.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(well_formed_json(parsed), ensure_ascii=False)
    return well_formed(text)


def is_high_surrogate(character):
    return "\ud800" <= character <= "\udbff" and len(character) == 1


class ToolCallBuffer:
    def __init__(self):
        self.calls = {}
        self.argument_bytes = 0

    def add(self, part):
        index = part.get("index")
        if type(index) is not int or index < 0:
            raise InferenceError("The model returned an invalid tool-call index.", "invalid_tool_arguments")
        if index not in self.calls:
            if len(self.calls) >= MAX_TOOL_CALLS:
                raise InferenceError("The model requested more than 64 tool calls in one response. No calls ran; retry in smaller steps.",
                                     "invalid_tool_arguments")
            self.calls[index] = {"id": "", "type": "function", "function": {"name": [], "arguments": []}}
        call = self.calls[index]
        if part.get("id") not in (None, ""):
            if not isinstance(part["id"], str) or len(part["id"].encode("utf-8", "surrogatepass")) > 256:
                raise InferenceError("The model returned an invalid tool-call ID.", "invalid_tool_arguments")
            call["id"] = part["id"]
        for key in ("name", "arguments"):
            fragment = part.get("function", {}).get(key, "")
            if fragment is None:
                fragment = ""
            if not isinstance(fragment, str):
                raise InferenceError("The model returned an invalid tool-call fragment.", "invalid_tool_arguments")
            size = len(fragment.encode("utf-8", "surrogatepass"))
            if key == "arguments":
                if self.argument_bytes + size > MAX_TOOL_ARGUMENT_BYTES:
                    raise InferenceError(
                        "The model's tool arguments exceeded 512 KiB in one response. No tool calls ran. "
                        "Retry with smaller file edits.", "invalid_tool_arguments")
                self.argument_bytes += size
            elif sum(len(item.encode("utf-8", "surrogatepass")) for item in call["function"][key]) + size > 128:
                raise InferenceError("The model returned an oversized tool name. No tool calls ran.", "invalid_tool_arguments")
            if fragment:
                call["function"][key].append(fragment)

    def finish(self):
        calls = {}
        for index, call in self.calls.items():
            function = {key: "".join(parts) for key, parts in call["function"].items()}
            # Claude on Databricks streams a call that takes no input (list_skills,
            # list_tasks) with no argument text. That means {}, and history must
            # hold valid JSON because it is sent back to the endpoint.
            if not function["arguments"].strip():
                function["arguments"] = "{}"
            # A pair of escaped halves may be split across fragments: joined here, once all are present.
            arguments = function.pop("arguments")
            function = {key: well_formed(text) for key, text in function.items()}
            function["arguments"] = well_formed_arguments(arguments)
            calls[index] = {**call, "id": well_formed(call["id"]), "function": function}
        return calls
