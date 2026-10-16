"""Long tool results and images, kept in memory for this app run (never saved to disk).

An MCP or web_fetch result longer than its first page is kept here, already redacted, so the
agent can read the rest with get_tool_output (a web page's pages repeat its untrusted-content
note); an image an MCP tool returned is kept for its card,
never sent to the model. Per chat at most MAX_ENTRIES entries; in all at most MAX_TOTAL
characters; the least recently used entry goes first. A restart or deleting the chat
empties it: the agent is told to call the tool again.
"""
import json
import secrets
from collections import OrderedDict

MAX_ENTRIES = 20  # per chat
MAX_TEXT = 1_000_000  # characters of one result
MAX_TOTAL = 8_000_000  # characters (text, and images as base64) of every chat together
PAGE_BYTES = 8_000  # one get_tool_output page as JSON
DEFAULT_PAGE = 4_000
MAX_PAGE = 8_000
GONE = ("Result {id} is no longer available: results are kept in memory for this app run only, at most "
        f"{MAX_ENTRIES} per chat. Call the tool again if you need it.")


class ResultStore:
    def __init__(self):
        self.entries = OrderedDict()  # (session id, result id) -> {kind, text | mime_type and data}
        self.total = 0

    def _add(self, session_id, entry, prefix):
        result_id = f"{prefix}-{secrets.token_hex(6)}"
        entry["size"] = len(entry.get("text") or entry.get("data") or "")
        self.entries[(session_id, result_id)] = entry
        self.total += entry["size"]
        own = [key for key in self.entries if key[0] == session_id]
        while len(own) > MAX_ENTRIES:
            self._drop(own.pop(0))
        while self.total > MAX_TOTAL and len(self.entries) > 1:
            self._drop(next(iter(self.entries)))
        return result_id

    def _drop(self, key):
        entry = self.entries.pop(key, None)
        if entry is not None:
            self.total -= entry["size"]

    def add(self, session_id, text, note=None):
        """Keep text (already redacted; at most MAX_TEXT characters); returns its ID. note: shown with every page."""
        return self._add(session_id, {"kind": "text", "text": text[:MAX_TEXT], **({"note": note} if note else {})}, "out")

    def add_image(self, session_id, mime_type, data):
        """Keep one image (base64 data) for its card; returns its ID."""
        return self._add(session_id, {"kind": "image", "mime_type": mime_type, "data": data}, "img")

    def has_text(self, session_id):
        return any(key[0] == session_id and entry["kind"] == "text" for key, entry in self.entries.items())

    def _get(self, session_id, result_id, kind):
        entry = self.entries.get((session_id, result_id)) if isinstance(result_id, str) else None
        if entry is None or entry["kind"] != kind:
            return None
        self.entries.move_to_end((session_id, result_id))
        return entry

    def page(self, session_id, result_id, offset=0, max_chars=DEFAULT_PAGE):
        """{result_id, output, offset, next_offset, total_chars}: at most max_chars characters from offset
        and PAGE_BYTES bytes as JSON. ValueError for an unknown ID or bad arguments."""
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer.")
        if type(max_chars) is not int or not 1 <= max_chars <= MAX_PAGE:
            raise ValueError(f"max_chars must be an integer from 1 to {MAX_PAGE:,}.")
        entry = self._get(session_id, result_id, "text")
        if entry is None:
            if self._get(session_id, result_id, "image") is not None:
                raise ValueError(f"{result_id} is an image: it is shown in the chat and never sent to the model.")
            raise ValueError(GONE.format(id=str(result_id)[:40]))
        text = entry["text"]
        if offset > len(text):
            raise ValueError(f"offset is beyond the result ({len(text):,} characters).")
        content = text[offset:offset + max_chars]

        def page(length):
            end = offset + length
            return {"result_id": result_id, **({"note": entry["note"]} if entry.get("note") else {}),
                    "output": content[:length], "offset": offset,
                    "next_offset": end if end < len(text) else None, "total_chars": len(text)}
        low, high = 0, len(content)
        while low < high:
            middle = (low + high + 1) // 2
            if len(json.dumps(page(middle), indent=2).encode("utf-8")) <= PAGE_BYTES:
                low = middle
            else:
                high = middle - 1
        return page(low)

    def image(self, session_id, image_id):
        """{mime_type, data} of a kept image, or None."""
        entry = self._get(session_id, image_id, "image")
        return {"mime_type": entry["mime_type"], "data": entry["data"]} if entry else None

    def delete_session(self, session_id):
        for key in [key for key in self.entries if key[0] == session_id]:
            self._drop(key)

    def chat(self, session_id):
        return ChatResults(self, session_id)


class ChatResults:
    """The store as one chat's MCP connection uses it."""

    def __init__(self, store, session_id):
        self.store, self.session_id = store, session_id

    def add(self, text):
        return self.store.add(self.session_id, text)

    def add_image(self, mime_type, data):
        return self.store.add_image(self.session_id, mime_type, data)
