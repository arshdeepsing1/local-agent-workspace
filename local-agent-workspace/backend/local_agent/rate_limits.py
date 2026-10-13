"""Databricks rate limits: the fields of a 429 body, and a wait shared by every chat.

A Databricks 429 body names the limit that was hit (limit_type, limit, current and
retry_after; see the limits page). parse_rate_limit reads them defensively.
RateLimitGate remembers, per model endpoint, until when Databricks asked for a pause,
so other chats on the same endpoint wait too instead of adding to the overload, and the
output-tokens-per-minute (OTPM) limit Databricks reported, so a request whose
max_tokens can never fit is refused before it is sent. It lives in memory only.
"""
import math
import re
import time
from dataclasses import dataclass

RATE_LIMIT_WAIT_CAP = 120.0
LEARNED_TTL = 3600.0
GATE_MODELS = 64
OTPM = "output_tokens_per_minute"
ITPM = "input_tokens_per_minute"
KNOWN_LIMITS = (
    (ITPM, "input tokens per minute (ITPM)"),
    (OTPM, "output tokens per minute (OTPM)"),
    ("queries_per_hour", "queries per hour (QPH)"),
    ("queries_per_second", "queries per second (QPS)"),
)
OTPM_TOO_LOW = (
    "Max output tokens ({max_tokens:,}) is above the output-tokens-per-minute limit Databricks reported for {model} "
    "({limit:,}), so Databricks rejects every request before it starts. Lower Settings → Max output tokens to at "
    "most {limit:,}; a quarter of the limit or less leaves room for several requests per minute. With {requests} "
    "requests at once (a chat and its subagents), at most {share:,} each.")
# A compaction request's max_tokens does not come from Settings → Max output tokens: its size is
# the summary or handoff plus the thinking room, both derived from the context budget.
COMPACTION_OTPM_TOO_LOW = (
    "This context compaction request needs max_tokens {max_tokens:,} (the summary plus thinking room), above the "
    "output-tokens-per-minute limit Databricks reported for {model} ({limit:,}), so Databricks rejects it before it "
    "starts. Its size comes from the context budget and the detailed handoff: turn off Settings → Save a detailed "
    "handoff at each compaction, or lower Settings → Context budget (thinking room is an eighth of it, up to 8,192 "
    "tokens). History is preserved.")


@dataclass(frozen=True)
class RateLimitInfo:
    limit_type: str | None = None
    label: str | None = None
    limit: int | None = None
    current: int | None = None
    retry_after: float | None = None


def _count(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and math.isfinite(value) and value >= 0:
        return int(value)
    return None


def _seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _limit_type(value):
    """(limit_type, label) for a provider's limit_type text, or (None, None)."""
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return None, None
    normalized = value.lower().replace("-", "_")
    if "input" in normalized or normalized == "itpm":
        return KNOWN_LIMITS[0]
    if "output" in normalized or normalized == "otpm":
        return KNOWN_LIMITS[1]
    if "hour" in normalized or normalized == "qph":
        return KNOWN_LIMITS[2]
    if "second" in normalized or normalized == "qps":
        return KNOWN_LIMITS[3]
    cleaned = re.sub(r"[^a-z0-9_]", "", normalized)[:64]
    return (cleaned, cleaned.replace("_", " ")) if cleaned else (None, None)


def parse_rate_limit(payload):
    """The RateLimitInfo of a 429 body; values nested under "error" override the root's."""
    fields = {}
    for source in (payload, payload.get("error") if isinstance(payload, dict) else None):
        if isinstance(source, dict):
            fields.update({key: source[key] for key in ("limit_type", "limit", "current", "retry_after")
                           if key in source})
    limit_type, label = _limit_type(fields.get("limit_type"))
    return RateLimitInfo(limit_type, label, _count(fields.get("limit")), _count(fields.get("current")),
                         _seconds(fields.get("retry_after")))


def describe(info):
    """'output tokens per minute (OTPM): limit 100,000, current 100,500', or '' without a known limit."""
    if info is None or info.label is None:
        return ""
    return (info.label + (f": limit {info.limit:,}" if info.limit is not None else "")
            + (f", current {info.current:,}" if info.current is not None else ""))


def request_info_fields(info):
    """The rate limit fields a request's details show (only those Databricks sent)."""
    return {key: value for key in ("limit_type", "limit", "current", "retry_after")
            if (value := getattr(info, key)) is not None}


def otpm_too_low(model, max_tokens, limit, concurrent):
    return OTPM_TOO_LOW.format(model=model, max_tokens=max_tokens, limit=limit, requests=concurrent,
                               share=limit // max(1, concurrent))


class CompactionOverOtpm(ValueError):
    """A compaction request whose max_tokens is above the OTPM limit Databricks reported (limit)."""

    def __init__(self, model, max_tokens, limit):
        super().__init__(COMPACTION_OTPM_TOO_LOW.format(model=model, max_tokens=max_tokens, limit=limit))
        self.limit = limit


class RateLimitGate:
    """Per model endpoint: until when Databricks asked for a pause, and the limits it reported."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.blocked = {}  # model -> {until, owner, limit_type, max_tokens}
        self.learned = {}  # (model, limit_type) -> (limit, noted at)

    def note(self, model, owner, delay, info=None, max_tokens=None):
        now = self.clock()
        delay = max(0.0, min(float(delay or 0), RATE_LIMIT_WAIT_CAP))
        previous = self.blocked.pop(model, None)
        until = max(previous["until"] if previous else now, now + delay)
        self.blocked[model] = {"until": until, "owner": owner,
                               "limit_type": getattr(info, "limit_type", None), "max_tokens": max_tokens}
        while len(self.blocked) > GATE_MODELS:
            self.blocked.pop(next(iter(self.blocked)))
        if info is not None and info.limit_type is not None and info.limit is not None:
            key = (model, info.limit_type)
            self.learned.pop(key, None)
            self.learned[key] = (info.limit, now)
            while len(self.learned) > GATE_MODELS:
                self.learned.pop(next(iter(self.learned)))

    def remaining(self, model, owner, max_tokens=None):
        """Seconds a request of owner (a chat id) to model should wait; 0 when it need not."""
        block = self.blocked.get(model)
        if block is None:
            return 0.0
        left = block["until"] - self.clock()
        if left <= 0 or block["owner"] == owner:
            return 0.0  # That chat's own retry loop already waits.
        if (block["limit_type"] == OTPM and max_tokens is not None and block["max_tokens"] is not None
                and max_tokens < block["max_tokens"]):
            return 0.0  # A smaller output reservation may be admitted.
        return left

    def learned_limit(self, model, limit_type):
        entry = self.learned.get((model, limit_type))
        if entry is None or self.clock() - entry[1] > LEARNED_TTL:
            return None
        return entry[0]
