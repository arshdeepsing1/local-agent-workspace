"""Databricks request limits: the request body as bytes, its 4 MB cap, and request timeouts.

The app serializes every request body itself (encode_payload) and sends those exact
bytes, so the size it measures is the size Databricks receives, whatever httpx version
is installed. This module imports nothing from context or agents (context imports it);
the limits mirror model_profiles.MAX_REQUEST_BYTES and MAX_EXECUTION_SECONDS, which a
test keeps equal (model_profiles imports context, so importing it here would be a cycle).
"""
import json

from .telemetry import InferenceError  # telemetry imports neither this module nor context.

PAYLOAD_LIMIT_BYTES = 4_000_000  # Databricks accepts at most 4 MB per request (read as decimal).
PAYLOAD_TARGET_BYTES = 3_800_000  # The app's own target, leaving room for headers and proxies.
STREAM_READ_TIMEOUT = 300.0  # A long prefill with adaptive thinking can be silent for minutes.
LONG_REQUEST_TIMEOUT = 600.0  # Non-streamed requests: compaction and /btw.
EXECUTION_LIMIT_SECONDS = 597  # Databricks stops a request after this many seconds.


def encode_payload(payload):
    """The exact request body: compact UTF-8 JSON, as httpx 0.28 encodes json=."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


# An image_url content part around its data URL: {"type":"image_url","image_url":{"url":"data:image/webp;base64,"}}.
IMAGE_PART_BYTES = 80


def image_bytes(messages):
    """The bytes the image references of user messages ("images", uploads.py) add to the request body, where
    each becomes an image_url part with a base64 data URL (uploads.expand_images)."""
    total = 0
    for message in messages:
        refs = message.get("images") if isinstance(message, dict) else None
        for ref in refs if isinstance(refs, list) else ():
            size = ref.get("bytes") if isinstance(ref, dict) else None
            if type(size) is int and size > 0:
                total += 4 * -(-size // 3) + IMAGE_PART_BYTES
    return total


def request_bytes_over(messages, tools, raw_estimate, target):
    """Whether the request body of messages and tools is over target bytes, attached images included.

    raw_estimate is the UNSCALED context.estimate_tokens(messages, tools). The compact
    body is never longer than three bytes per estimated token, so a request whose
    estimate is at most a third of target (less its images' bytes) is not encoded at all."""
    images = image_bytes(messages)
    if 3 * raw_estimate + images <= target:
        return False
    return len(encode_payload({"messages": messages, "tools": list(tools)})) + images > target


class PayloadTooLarge(InferenceError):
    """A request body over PAYLOAD_LIMIT_BYTES: never sent and never retried."""

    def __init__(self, size):
        super().__init__(
            f"This request would be {size / 1_000_000:.1f} MB, and Databricks accepts at most 4 MB per request, so "
            "it was not sent. The latest request, its newest tool results and the project instructions are too "
            "large on their own. Continue in smaller steps (fewer or smaller reads per response), or lower "
            "Settings → Context budget so compaction starts earlier.", "payload_too_large", None)
        self.size = size


def check_payload(payload):
    """The encoded body of payload, or PayloadTooLarge when it is over the limit. Nothing is
    shortened here: shrinking history is compaction's job."""
    body = encode_payload(payload)
    if len(body) > PAYLOAD_LIMIT_BYTES:
        raise PayloadTooLarge(len(body))
    return body
