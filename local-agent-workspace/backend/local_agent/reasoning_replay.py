"""Signed reasoning kept with assistant replies, and sent back during tool use (experimental). Pure: no I/O.

Claude endpoints stream their thinking as reasoning items whose last piece carries a signature, which the
model can verify when the items come back with the reply they belong to. While Settings → Reasoning → Send
earlier thinking back is on (and the endpoint has not rejected it), the app keeps a reply's signed items in
its wire message as a sidecar, {"reasoning": {"model", "items", "bytes"}}; the content stays a string.
With the setting off no signature is written to disk. agents.model_history strips the sidecar from every
message, so estimates, compaction (summaries never hold signatures), side questions and titles see the
history as before.

apply() puts the items back into the content of the current turn's assistant replies (the tool loop after
the last user message) at request time, only for replies the request's own endpoint wrote: another
endpoint (a fallback, a model change) never gets them, and earlier turns are never sent back. A new turn
removes the sidecars of earlier turns (prune), so a conversation file keeps at most one turn's.
"""
import copy
import json

REASONING_ITEMS = ("reasoning", "thinking", "redacted_thinking")
MAX_SIDECAR_BYTES = 64_000  # One reply's items, as JSON; a reply with more keeps none.
MAX_REPLAY_BYTES = 256_000  # Items sent back in one request, newest reply first.
MAX_SIDECAR_ITEMS = 64
# Words of an HTTP 400 that point at reasoning sent back in the history.
REJECTION_TERMS = ("signature", "thinking", "reasoning", "summary_text")


def _key(model):
    return model.strip().lower() if isinstance(model, str) else ""


def _size(items):
    return len(json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class Collector:
    """A streamed reply's reasoning items, assembled for the history. Databricks streams a reasoning item's
    summary text in pieces, with its signature on the last piece (a summary_text with the signature), and
    thinking items likewise. Text without a signature is not kept (nothing a model could verify); redacted
    thinking and encrypted summaries are kept as they came. More than MAX_SIDECAR_BYTES keeps nothing."""

    def __init__(self):
        self.items, self.current, self.size, self.too_large = [], None, 0, False

    def add(self, delta):
        """Read one streamed delta's content list."""
        content = delta.get("content") if isinstance(delta, dict) else None
        if self.too_large or not isinstance(content, list):
            return
        for block in content:
            kind = block.get("type") if isinstance(block, dict) else None
            if kind == "reasoning" and isinstance(block.get("summary"), list):
                for item in block["summary"]:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "summary_text":
                        self._text("reasoning", item.get("text"), item.get("signature"))
                    elif item.get("type") == "summary_encrypted_text":
                        self._opaque({"type": "reasoning", "summary": [copy.deepcopy(item)]})
            elif kind == "thinking":
                self._text("thinking", block.get("thinking"), block.get("signature"))
            elif kind == "redacted_thinking":
                self._opaque(copy.deepcopy(block))

    def _grow(self, size):
        self.size += size
        if self.size > MAX_SIDECAR_BYTES:
            self.too_large, self.items, self.current = True, [], None
        return not self.too_large

    def _text(self, kind, text, signature):
        if self.current is not None and self.current[0] != kind:
            self._close()
        if self.current is None:
            self.current = (kind, [])
        if isinstance(text, str) and text and self._grow(len(text.encode("utf-8"))):
            self.current[1].append(text)
        if isinstance(signature, str) and signature and self._grow(len(signature)):
            self._close(signature)

    def _opaque(self, item):
        self._close()
        if self._grow(_size(item)):
            self.items.append(item)

    def _close(self, signature=None):
        if self.current is None:
            return
        kind, parts = self.current
        self.current = None
        if not signature:
            return
        text = "".join(parts)
        self.items.append({"type": "reasoning", "summary": [{"type": "summary_text", "text": text, "signature": signature}]}
                          if kind == "reasoning" else {"type": "thinking", "thinking": text, "signature": signature})

    def result(self):
        """The signed items (an open unsigned item is dropped), or [] when there were more than the limit."""
        self._close()
        return [] if self.too_large else list(self.items)


def sidecar(model, items, redact=None):
    """The sidecar a reply keeps for these items, or None: no items, too many or too large, or text that
    redact (Settings.redact) would change (a secret is never written into the history)."""
    if not items or not isinstance(model, str) or not model.strip() or len(items) > MAX_SIDECAR_ITEMS:
        return None
    size = _size(items)
    text = json.dumps(items, ensure_ascii=False)
    if size > MAX_SIDECAR_BYTES or (redact is not None and redact(text) != text):
        return None
    return {"model": model.strip(), "items": items, "bytes": size}


def valid(side):
    """True for a sidecar apply() may send back."""
    return (isinstance(side, dict) and isinstance(side.get("model"), str) and isinstance(side.get("items"), list)
            and 0 < len(side["items"]) <= MAX_SIDECAR_ITEMS
            and all(isinstance(item, dict) and item.get("type") in REASONING_ITEMS for item in side["items"]))


def content(items, text):
    """An assistant message's content with its reasoning items first, then its text (if any)."""
    return [*copy.deepcopy(items), *([{"type": "text", "text": text}] if isinstance(text, str) and text else [])]


def turn_start(wire):
    """The index after the wire's last user message: the current turn's replies and tool results follow it."""
    return next((index + 1 for index in range(len(wire) - 1, -1, -1) if wire[index].get("role") == "user"), 0)


def _replayable(raw, sent, model):
    side = raw.get("reasoning")
    # model_history rewrites a reply whose tool calls are broken: that one keeps no reasoning.
    return (raw.get("role") == "assistant" and valid(side) and _key(side["model"]) == _key(model)
            and sent.get("tool_calls") == raw.get("tool_calls"))


def apply(messages, history, wire, model, limit=MAX_REPLAY_BYTES):
    """(messages to send, how many replies carry their reasoning). messages is the prepared request
    (context.prepare_context, its tail the same objects as history, which is agents.model_history(wire));
    wire the chat's own history with the sidecars. Only replies after the last user message, written by
    model, get their items back, newest first within limit bytes; anything else is sent as it is."""
    start, result, count, size = turn_start(wire), list(messages), 0, 0
    for back in range(1, len(wire) - start + 1):
        position = len(wire) - back
        if back >= len(result) or result[-back] is not history[position]:
            break  # Compacted away, or not this message: nothing earlier is matched either.
        raw, sent = wire[position], history[position]
        if not _replayable(raw, sent, model):
            continue
        size += raw["reasoning"].get("bytes") if type(raw["reasoning"].get("bytes")) is int else _size(raw["reasoning"]["items"])
        if size > limit:
            break
        result[-back] = {**sent, "content": content(raw["reasoning"]["items"], sent.get("content"))}
        count += 1
    return (result, count) if count else (messages, 0)


def pending_bytes(wire, model):
    """The bytes of reasoning the chat's next request in this turn would send back (apply's candidates)."""
    total = 0
    for message in wire[turn_start(wire):]:
        if _replayable(message, message, model):
            total += _size(message["reasoning"]["items"])
    return min(total, MAX_REPLAY_BYTES)


def prune(wire):
    """Remove every sidecar (a new turn starts: none of them is sent back again). Returns how many."""
    count = 0
    for message in wire:
        if isinstance(message, dict) and "reasoning" in message:
            del message["reasoning"]
            count += 1
    return count


def strip(message):
    """message without its sidecar (the same object when it has none)."""
    return {key: value for key, value in message.items() if key != "reasoning"} if "reasoning" in message else message


def rejected(error_text):
    """True when an HTTP 400 body names reasoning sent back in the history."""
    text = error_text.lower() if isinstance(error_text, str) else ""
    return any(term in text for term in REJECTION_TERMS)
