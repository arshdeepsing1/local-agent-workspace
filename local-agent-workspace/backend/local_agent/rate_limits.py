"""Databricks rate limits: the fields of a 429 body, and a wait shared by every chat.

A Databricks 429 body may name the limit that was hit (limit_type, limit, current and
retry_after; see the limits page). parse_rate_limit reads them defensively; a body that gives
only {error_code, message} names the limit type in its words, which limit_type_from_words reads
(never a number: no limit is learned from words).
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
    ("queries_per_minute", "queries per minute (QPM)"),  # AI Gateway's own rate limits are per minute.
)
LABELS = dict(KNOWN_LIMITS)
# The limit type a 429's words name, for a body without limit_type. Specific phrases first,
# abbreviations only as whole words; a generic "tokens per minute" only when nothing more
# specific matched. The text is lowercased, with "_" and "-" read as spaces.
WORD_LIMITS = (
    (ITPM, re.compile(r"\binput tokens? per minute\b|\bitpm\b")),
    (OTPM, re.compile(r"\boutput tokens? per minute\b|\botpm\b")),
    ("queries_per_hour", re.compile(r"\bquer(?:y|ies) per hour\b|\bqph\b")),
    ("queries_per_minute", re.compile(r"\bquer(?:y|ies) per minute\b|\bqpm\b")),
    ("queries_per_second", re.compile(r"\bquer(?:y|ies) per second\b|\bqps\b")),
)
GENERIC_TOKENS_PER_MINUTE = re.compile(r"\btokens? per minute\b|\btpm\b")
WORDS_CHARS = 2000  # The most of a 429's message, error_code and detail text the classifier reads.
MESSAGE_CHARS = 200  # A 429 message kept for notices, request details and the ledger.
CODE = re.compile(r"[A-Za-z0-9_]{1,64}")
OTPM_TOO_LOW = (
    "Max output tokens ({max_tokens:,}) is above the output-tokens-per-minute limit Databricks reported for {model} "
    "({limit:,}), so Databricks rejects every request before it starts. Lower Settings → Max output tokens to at "
    "most {limit:,}; a quarter of the limit or less leaves room for several requests per minute. With {requests} "
    "requests at once (a chat and its subagents), at most {share:,} each.")
# A compaction request's max_tokens does not come from Settings → Max output tokens: under a
# reported OTPM limit it shrinks to the smallest summary plus the thinking room, an eighth of the
# context budget up to 8,192 tokens. Only that room can still change, so the advice names the
# budget at which it fits, or the limit itself.
COMPACTION_OTPM_TOO_LOW = (
    "This context compaction request needs max_tokens {max_tokens:,} (the smallest summary, {summary:,} tokens, "
    "plus thinking room), above the output-tokens-per-minute limit Databricks reported for {model} ({limit:,}), "
    "so Databricks rejects it before it starts. {advice} History is preserved.")
COMPACTION_BUDGET_ADVICE = (
    "The thinking room is an eighth of Settings → Context budget, up to 8,192 tokens: lower the context budget to at "
    "most {budget:,} tokens, or ask your workspace admin for a higher output-tokens-per-minute limit.")
COMPACTION_ADMIN_ADVICE = (
    "No setting makes it fit: ask your workspace admin for a higher output-tokens-per-minute limit for this endpoint.")


def compaction_otpm_advice(max_tokens, limit):
    """What can make a compaction request of max_tokens fit an OTPM limit."""
    from .context import MIN_CONTEXT_WINDOW, SUMMARY_MAX_TOKENS  # context imports neither this module nor agents
    room = limit - SUMMARY_MAX_TOKENS
    # budget // 8 <= room; a model without thinking room needs only the summary.
    budget = 8 * room + 7
    if max_tokens > SUMMARY_MAX_TOKENS and room > 0 and budget >= MIN_CONTEXT_WINDOW:
        return COMPACTION_BUDGET_ADVICE.format(budget=budget)
    return COMPACTION_ADMIN_ADVICE


@dataclass(frozen=True)
class RateLimitInfo:
    limit_type: str | None = None
    label: str | None = None
    limit: int | None = None
    current: int | None = None
    retry_after: float | None = None
    code: str | None = None  # error_code, or the nested error's code or type (REQUEST_LIMIT_EXCEEDED)
    message: str | None = None  # The body's message, redacted, whitespace collapsed, at most MESSAGE_CHARS


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
    if ("minute" in normalized and "quer" in normalized) or normalized == "qpm":
        return KNOWN_LIMITS[4]
    cleaned = re.sub(r"[^a-z0-9_]", "", normalized)[:64]
    return (cleaned, cleaned.replace("_", " ")) if cleaned else (None, None)


def limit_type_from_words(*texts):
    """The one limit type the texts name, or None (none, or more than one: ambiguous)."""
    text = re.sub(r"[\s_-]+", " ", " ".join(item for item in texts if isinstance(item, str))[:WORDS_CHARS].lower())
    found = [name for name, pattern in WORD_LIMITS if pattern.search(text)]
    if not found and GENERIC_TOKENS_PER_MINUTE.search(text):
        return "tokens_per_minute"
    return found[0] if len(found) == 1 else None


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def parse_rate_limit(payload, text=None, redact=None):
    """The RateLimitInfo of a 429 body; values nested under "error" override the root's. text is the
    body as text, the message of a body that is not a JSON object; redact (settings.redact) is applied
    to the kept message. Without a limit_type field, the limit type is read from the words of the
    message, error_code and detail (limit_type_from_words)."""
    fields, words = {}, []
    root = payload if isinstance(payload, dict) else {}
    plain = None if isinstance(payload, dict) else text
    nested = root.get("error") if isinstance(root.get("error"), dict) else {}
    for source in (root, nested):
        fields.update({key: source[key] for key in ("limit_type", "limit", "current", "retry_after")
                       if key in source})
        words += [source.get(key) for key in ("message", "error_code", "detail")]
    limit_type, label = _limit_type(fields.get("limit_type"))
    if limit_type is None:
        named = limit_type_from_words(*words, root.get("error"), plain)
        limit_type, label = (named, LABELS.get(named) or named.replace("_", " ")) if named else (None, None)
    code = next((value for value in (nested.get("error_code"), root.get("error_code"), nested.get("code"),
                                     nested.get("type")) if isinstance(value, str) and CODE.fullmatch(value)
                 and (redact is None or redact(value) == value)), None)
    message = next((value for value in (nested.get("message"), root.get("message"), root.get("error"),
                                        nested.get("detail"), root.get("detail"), plain)
                    if isinstance(value, str) and value.strip()), None)
    if message is not None:
        message = _clip(" ".join((redact(message) if redact else message).split()), MESSAGE_CHARS) or None
    return RateLimitInfo(limit_type, label, _count(fields.get("limit")), _count(fields.get("current")),
                         _seconds(fields.get("retry_after")), code, message)


def describe(info):
    """'output tokens per minute (OTPM): limit 100,000, current 100,500', or '' without a known limit."""
    if info is None or info.label is None:
        return ""
    return (info.label + (f": limit {info.limit:,}" if info.limit is not None else "")
            + (f", current {info.current:,}" if info.current is not None else ""))


def request_info_fields(info):
    """The rate limit fields a request's details show (only those Databricks sent)."""
    return {key: value for key in ("limit_type", "limit", "current", "retry_after", "code", "message")
            if (value := getattr(info, key)) is not None}


def otpm_too_low(model, max_tokens, limit, concurrent):
    return OTPM_TOO_LOW.format(model=model, max_tokens=max_tokens, limit=limit, requests=concurrent,
                               share=limit // max(1, concurrent))


class CompactionOverOtpm(ValueError):
    """A compaction request whose max_tokens is above the OTPM limit Databricks reported (limit)."""

    def __init__(self, model, max_tokens, limit):
        from .context import SUMMARY_MAX_TOKENS
        super().__init__(COMPACTION_OTPM_TOO_LOW.format(model=model, max_tokens=max_tokens, limit=limit,
                                                        summary=SUMMARY_MAX_TOKENS,
                                                        advice=compaction_otpm_advice(max_tokens, limit)))
        self.limit = limit


class RateLimitGate:
    """Per model endpoint: until when Databricks asked for a pause, and the limits it reported."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.blocked = {}  # model -> {until, owner, limit_type, max_tokens, details}
        self.learned = {}  # (model, limit_type) -> (limit, noted at)

    def note(self, model, owner, delay, info=None, max_tokens=None, details=None):
        """A 429 of owner's request to model: block it for delay seconds. details (agents.rate_limit_details)
        are shown on the notices of the chats that wait. Only a limit Databricks sent as a number is learned."""
        now = self.clock()
        delay = max(0.0, min(float(delay or 0), RATE_LIMIT_WAIT_CAP))
        previous = self.blocked.pop(model, None)
        until = max(previous["until"] if previous else now, now + delay)
        self.blocked[model] = {"until": until, "owner": owner, "limit_type": getattr(info, "limit_type", None),
                               "max_tokens": max_tokens, "details": details}
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

    def details(self, model):
        """The Details of the 429 behind model's current block, or None."""
        block = self.blocked.get(model)
        return block.get("details") if block is not None else None

    def learned_limit(self, model, limit_type):
        entry = self.learned.get((model, limit_type))
        if entry is None or self.clock() - entry[1] > LEARNED_TTL:
            return None
        return entry[0]
