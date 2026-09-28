"""Bound SSE events before decoding, and tool-call fragments before assembly."""
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
            if not isinstance(part["id"], str) or len(part["id"].encode("utf-8")) > 256:
                raise InferenceError("The model returned an invalid tool-call ID.", "invalid_tool_arguments")
            call["id"] = part["id"]
        for key in ("name", "arguments"):
            fragment = part.get("function", {}).get(key, "")
            if fragment is None:
                fragment = ""
            if not isinstance(fragment, str):
                raise InferenceError("The model returned an invalid tool-call fragment.", "invalid_tool_arguments")
            size = len(fragment.encode("utf-8"))
            if key == "arguments":
                if self.argument_bytes + size > MAX_TOOL_ARGUMENT_BYTES:
                    raise InferenceError(
                        "The model's tool arguments exceeded 512 KiB in one response. No tool calls ran. "
                        "Retry with smaller file edits.", "invalid_tool_arguments")
                self.argument_bytes += size
            elif sum(len(item.encode("utf-8")) for item in call["function"][key]) + size > 128:
                raise InferenceError("The model returned an oversized tool name. No tool calls ran.", "invalid_tool_arguments")
            if fragment:
                call["function"][key].append(fragment)

    def finish(self):
        return {index: {**call, "function": {key: "".join(parts) for key, parts in call["function"].items()}}
                for index, call in self.calls.items()}
