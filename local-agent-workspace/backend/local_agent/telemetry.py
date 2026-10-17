"""Provider-reported inference telemetry and documented DBU estimates."""

import json
import math
import re
import uuid
from datetime import date, datetime, timezone


LEDGER_VERSION = 1
USAGE_FIELDS = (
    "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens", "reasoning_tokens",
)
PRICING_SOURCE = "https://www.databricks.com/product/pricing/proprietary-foundation-model-serving"
OPEN_PRICING_SOURCE = "https://www.databricks.com/product/pricing/foundation-model-serving"
PRICING_EFFECTIVE_AT = "2026-10-16"
# Versioned snapshot of the Databricks pay-per-token list rates (Standard, not Priority or Batch), copied
# from the two pricing pages above on PRICING_EFFECTIVE_AT; only endpoints with one rate per token type are
# listed (models whose rate depends on the context length are left out). These are estimates, not invoice
# records; account discounts and billing adjustments are intentionally outside this local calculation.
# Regional processing adds a 10% uplift that is not included. Cache writes use the 5-minute rate: the usage
# does not tell 1-hour writes (114.286 DBU per million for Opus 5.5) apart. A model whose page lists no
# cache rate has its cache tokens priced at its input rate (no discount is listed).
_CLAUDE_OPUS_4_5 = {"input_tokens": 71.429, "output_tokens": 357.143, "cache_read_input_tokens": 7.143,
                    "cache_creation_input_tokens": 89.286}
_CLAUDE_OPUS_4 = {"input_tokens": 214.286, "output_tokens": 1071.43, "cache_read_input_tokens": 21.429,
                  "cache_creation_input_tokens": 267.857}
_CLAUDE_SONNET_5 = {"input_tokens": 28.571, "output_tokens": 142.857, "cache_read_input_tokens": 2.857,
                    "cache_creation_input_tokens": 35.714}
_CLAUDE_SONNET_4 = {"input_tokens": 42.857, "output_tokens": 214.286, "cache_read_input_tokens": 4.286,
                    "cache_creation_input_tokens": 53.571}
_GPT_5 = {"input_tokens": 17.857, "output_tokens": 142.857, "cache_read_input_tokens": 1.786}
MODEL_DBU_PER_MILLION = {
    "databricks-claude-opus-5-5": {
        "input_tokens": 57.143,
        "output_tokens": 285.714,
        "cache_read_input_tokens": 2.857,
        "cache_creation_input_tokens": 71.429,
    },
    **{f"databricks-claude-opus-{version}": _CLAUDE_OPUS_4_5 for version in ("5", "4-8", "4-7", "4-6", "4-5")},
    **{f"databricks-claude-opus-{version}": _CLAUDE_OPUS_4 for version in ("4-1", "4")},
    **{f"databricks-claude-sonnet-{version}": _CLAUDE_SONNET_5 for version in ("5-5", "5")},
    **{f"databricks-claude-sonnet-{version}": _CLAUDE_SONNET_4 for version in ("4-6", "4-5", "4")},
    "databricks-claude-haiku-4-5": {"input_tokens": 14.286, "output_tokens": 71.429, "cache_read_input_tokens": 1.429,
                                    "cache_creation_input_tokens": 17.857},
    "databricks-gpt-5-4-mini": {"input_tokens": 10.714, "output_tokens": 64.286, "cache_read_input_tokens": 1.071},
    "databricks-gpt-5-4-nano": {"input_tokens": 2.857, "output_tokens": 17.857, "cache_read_input_tokens": 0.286},
    "databricks-gpt-5-3-codex": {"input_tokens": 25.0, "output_tokens": 200.0, "cache_read_input_tokens": 2.5},
    "databricks-gpt-5-2": {"input_tokens": 25.0, "output_tokens": 200.0, "cache_read_input_tokens": 2.5},
    "databricks-gpt-5-1": _GPT_5,
    "databricks-gpt-5": _GPT_5,
    "databricks-gpt-5-mini": {"input_tokens": 3.571, "output_tokens": 28.571, "cache_read_input_tokens": 0.357},
    "databricks-gpt-5-nano": {"input_tokens": 0.714, "output_tokens": 5.714, "cache_read_input_tokens": 0.071},
    # Open models (OPEN_PRICING_SOURCE).
    "databricks-gpt-oss-120b": {"input_tokens": 2.143, "output_tokens": 8.571},
    "databricks-gpt-oss-20b": {"input_tokens": 1.0, "output_tokens": 4.286},
    "databricks-llama-4-maverick": {"input_tokens": 7.143, "output_tokens": 21.429},
    "databricks-meta-llama-3-3-70b-instruct": {"input_tokens": 7.143, "output_tokens": 21.429},
    "databricks-meta-llama-3-1-8b-instruct": {"input_tokens": 2.143, "output_tokens": 6.429},
    "databricks-gemma-3-12b": {"input_tokens": 2.143, "output_tokens": 7.143},
}
OPEN_MODELS = ("databricks-gpt-oss-120b", "databricks-gpt-oss-20b", "databricks-llama-4-maverick",
               "databricks-meta-llama-3-3-70b-instruct", "databricks-meta-llama-3-1-8b-instruct", "databricks-gemma-3-12b")
PRICED_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
# The raw usage object's key names kept per call (usage_keys): bounded, so a provider cannot write text into the ledger.
USAGE_KEY_LIMIT = 16
USAGE_KEY_PATTERN = re.compile(r"[a-z0-9_.]{1,64}\Z")
REASONING_SHAPES = ("content.reasoning", "content.thinking", "reasoning_content", "reasoning")
PROMPT_CACHE_LEVELS = ("full", "messages", "off")  # prompt_cache.LEVELS
# "probe": an endpoint check's request (endpoint_checks_api); it is in no chat's ledger, only in its request tags.
PURPOSE_LABELS = {"agent": "Agent", "compaction": "Compaction", "title": "Title", "side_question": "Side question",
                  "probe": "Endpoint check"}
# Databricks AI Gateway stores these tags in system.ai_gateway.usage.request_tags,
# so each ledger record can be joined to the provider's usage rows exactly.
REQUEST_TAGS_HEADER = "Databricks-Ai-Gateway-Request-Tags"


class InferenceError(ValueError):
    def __init__(self, message, kind="unknown", http_status=None):
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status


def http_error_kind(status):
    if status == 401:
        return "authentication"
    if status == 403:
        return "permission"
    if status == 429:
        return "rate_limit"
    if 400 <= status < 500:
        return "invalid_request"
    if status >= 500:
        return "server"
    return "unknown"


def stream_error_kind(error):
    if not isinstance(error, dict):
        return "unknown"
    for key in ("status", "status_code", "code"):
        status = error.get(key)
        if type(status) is int and 400 <= status <= 599:
            return http_error_kind(status)
    codes = {
        "authentication_error": "authentication", "unauthenticated": "authentication",
        "permission_error": "permission", "permission_denied": "permission",
        "rate_limit_error": "rate_limit", "resource_exhausted": "rate_limit",
        "too_many_requests": "rate_limit", "request_limit_exceeded": "rate_limit",
        "invalid_request_error": "invalid_request",
        "invalid_parameter_value": "invalid_request", "bad_request": "invalid_request",
        "api_error": "server", "server_error": "server", "internal_error": "server",
        "overloaded_error": "server",
    }
    for key in ("type", "code", "error_code"):
        value = error.get(key)
        if isinstance(value, str) and value.lower() in codes:
            return codes[value.lower()]
    return "unknown"


def reported_usage(value):
    if not isinstance(value, dict):
        return {}
    aliases = {
        "input_tokens": ("prompt_tokens", "input_tokens"),
        "output_tokens": ("completion_tokens", "output_tokens"),
        "total_tokens": ("total_tokens",),
        "cache_read_input_tokens": ("cache_read_input_tokens",),
        "cache_creation_input_tokens": ("cache_creation_input_tokens",),
        "reasoning_tokens": ("reasoning_tokens",),
    }
    usage, sources = {}, {}
    for name, keys in aliases.items():
        for key in keys:
            count = value.get(key)
            if type(count) is int and count >= 0:
                usage[name], sources[name] = count, key
                break
    for field, key, name in (("prompt_tokens_details", "cached_tokens", "cache_read_input_tokens"),
                             ("completion_tokens_details", "reasoning_tokens", "reasoning_tokens")):
        details = value.get(field)
        count = details.get(key) if isinstance(details, dict) else None
        if name not in usage and type(count) is int and count >= 0:
            usage[name], sources[name] = count, field
    # OpenAI-style usage counts cached tokens inside prompt_tokens (prompt_tokens_details.
    # cached_tokens), while Claude-style usage reports uncached input, cache reads and
    # cache writes as separate buckets. input_tokens is kept as the uncached part, so
    # the cache is not counted twice by totals, DBU estimates and the estimate scale.
    # Top-level cache fields stay separate buckets. Checked 2026-10-16 on a live workspace:
    # the Claude route returns top-level cache_read_input_tokens and
    # cache_creation_input_tokens, 0 when nothing is cached; a request that hits the cache
    # is still unchecked. Ledger records saved before this change keep their counts (they
    # hold input_tokens only and cannot be re-split).
    if (sources.get("input_tokens") == "prompt_tokens" and sources.get("cache_read_input_tokens") == "prompt_tokens_details"
            and usage["cache_read_input_tokens"] <= usage["input_tokens"]):
        usage["input_tokens"] -= usage["cache_read_input_tokens"]
    return usage


def usage_keys(value, seen=()):
    """The sorted key names of a raw usage object, with parent.child names for nested objects, joined with
    names seen before (earlier stream snapshots): at most USAGE_KEY_LIMIT names matching USAGE_KEY_PATTERN.
    Only names are kept, never values, so the ledger shows which usage shape an endpoint sends."""
    names = {name for name in seen if isinstance(name, str) and USAGE_KEY_PATTERN.match(name)}
    if isinstance(value, dict):
        for key, item in value.items():
            names.add(key)
            if isinstance(item, dict):
                names.update(f"{key}.{child}" for child in item if isinstance(child, str))
    return sorted(name for name in names if isinstance(name, str) and USAGE_KEY_PATTERN.match(name))[:USAGE_KEY_LIMIT]


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def begin_inference_call(session, purpose, model, *, event_id=None, max_output_tokens=None, attempt=None,
                         estimated_input_tokens=None, retry_reason=None):
    """Append one provider attempt to the durable per-session inference ledger.

    estimated_input_tokens is the app's unscaled heuristic estimate of the
    request; comparing it with provider-reported usage calibrates later budgets.
    retry_reason names why the app sent this request automatically (a retry of a
    failed attempt, an automatic continuation or a Verify fix round).
    """
    call = {"id": str(uuid.uuid4()), "purpose": purpose, "model": model,
            "created": _now(), "status": "running"}
    if event_id:
        call["event_id"] = event_id
    if type(max_output_tokens) is int:
        call["max_output_tokens"] = max_output_tokens
    if type(attempt) is int:
        call["attempt"] = attempt
    if type(estimated_input_tokens) is int and estimated_input_tokens >= 0:
        call["estimated_input_tokens"] = estimated_input_tokens
    if isinstance(retry_reason, str):
        call["retry_reason"] = retry_reason
    session["inference_ledger_version"] = LEDGER_VERSION
    session.setdefault("inference_calls", []).append(call)
    return call


def request_tag_headers(session, call):
    """Request tags naming this ledger record, its conversation and purpose."""
    tags = {"local_agent_call_id": call["id"], "local_agent_conversation": str(session.get("id", "")),
            "local_agent_purpose": call["purpose"]}
    return {REQUEST_TAGS_HEADER: json.dumps(tags, separators=(",", ":"))}


def record_response_id(call, value):
    """Keep the provider's response id (Databricks encrypts it) for support requests."""
    if call is not None and "response_id" not in call and isinstance(value, str) and 0 < len(value) <= 500:
        call["response_id"] = value


def finish_inference_call(call, info):
    """Copy only bounded, non-sensitive outcome metadata into a ledger record."""
    status = info.get("status")
    call["status"] = status if isinstance(status, str) else "error"
    for key in ("http_status", "error_kind", "finish_reason"):
        value = info.get(key)
        if (key == "http_status" and type(value) is int) or (key != "http_status" and isinstance(value, str)):
            call[key] = value
    usage = reported_usage(info.get("usage"))
    if usage:
        call["usage"] = usage
    rate_limit = info.get("rate_limit")
    if isinstance(rate_limit, dict):  # A 429 (agents.rate_limit_fields).
        limit_type = rate_limit.get("limit_type")
        if isinstance(limit_type, str) and 0 < len(limit_type) <= 64:
            call["limit_type"] = limit_type  # Which Databricks rate limit a 429 named (rate_limits.parse_rate_limit).
        message = rate_limit.get("message")
        if isinstance(message, str) and message:
            call["rate_limit_message"] = message[:200]  # Already redacted, whitespace collapsed.
        if (seconds := _seconds(rate_limit.get("retry_after"))) is not None:
            call["retry_after"] = seconds  # The wait Databricks asked for: Retry-After, else the body's retry_after.
        # Whether that wait came in a Retry-After header (agents.rate_limit_fields keeps only a header it read).
        call["retry_after_header"] = isinstance(rate_limit.get("retry_after_header"), str)
    # The response's shape, by name only: the raw usage keys, the delta shape that carried reasoning, and
    # whether a reasoning signature (or encrypted or redacted reasoning) came.
    keys = usage_keys(None, info["usage_keys"]) if isinstance(info.get("usage_keys"), list) else []
    if keys:
        call["usage_keys"] = keys
    if info.get("reasoning_shape") in REASONING_SHAPES:
        call["reasoning_shape"] = info["reasoning_shape"]
    if info.get("reasoning_signature") is True:
        call["reasoning_signature"] = True
    call["finished"] = _now()
    return call


def _seconds(value):
    """A finite number of seconds >= 0 as a float, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _iso_time(value):
    if isinstance(value, str):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return value
        except ValueError:
            pass
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, OverflowError, OSError):
            pass
    return datetime.fromtimestamp(0, timezone.utc).isoformat().replace("+00:00", "Z")


def estimated_dbu(model, usage):
    rates = MODEL_DBU_PER_MILLION.get(model.lower()) if isinstance(model, str) else None
    if rates is None:
        return None
    # Claude reports uncached input, cache reads, and cache writes as separate
    # billable buckets. Cache tokens must therefore be added, not subtracted
    # from prompt_tokens/input_tokens. A cache rate the price list leaves out is the input rate.
    value = sum(usage.get(field, 0) * rates.get(field, rates["input_tokens"]) for field in PRICED_FIELDS) / 1_000_000
    return round(value, 9)


def pricing_source(model):
    """The pricing page a model's list rates come from, or None for a model without them."""
    if not isinstance(model, str) or model.lower() not in MODEL_DBU_PER_MILLION:
        return None
    return OPEN_PRICING_SOURCE if model.lower() in OPEN_MODELS else PRICING_SOURCE


def _explicit_rejection(call):
    status = call.get("http_status")
    return type(status) is int and 100 <= status <= 599 and not 200 <= status < 300


def _public_call(call, *, fallback=False):
    model = call.get("model") if isinstance(call.get("model"), str) else "unknown"
    purpose = call.get("purpose") if call.get("purpose") in PURPOSE_LABELS else "agent"
    result = {
        "id": str(call.get("id") or uuid.uuid4()),
        "purpose": purpose,
        "model": model,
        "created": _iso_time(call.get("created")),
        "status": call.get("status") if isinstance(call.get("status"), str) else "unknown",
    }
    # reasoning_replayed: earlier replies of the turn sent with their signed thinking (reasoning_replay.py).
    for key in ("http_status", "attempt", "max_output_tokens", "reasoning_replayed"):
        if type(call.get(key)) is int:
            result[key] = call[key]
    # effort: the reasoning effort the request asked for (reasoning.EFFORT_LEVELS); sent_effort: the
    # level sent ('none' when the fields were skipped); effort_field: the field that carried it;
    # reasoning_skipped: why none was sent ('rejected', 'rejected_earlier').
    for key in ("error_kind", "finish_reason", "retry_reason", "limit_type", "effort", "sent_effort", "effort_field",
                "reasoning_skipped"):
        if isinstance(call.get(key), str):
            result[key] = call[key]
    # fallback_from: the chat's endpoint, which failed before this request (retry_reason 'fallback') went to model.
    if isinstance(call.get("fallback_from"), str) and call["fallback_from"]:
        result["fallback_from"] = call["fallback_from"][:128]
    fields = call.get("reasoning_fields")  # The top-level reasoning fields the request sent.
    if isinstance(fields, list) and all(isinstance(name, str) for name in fields):
        result["reasoning_fields"] = fields
    # prompt_cache: the cache-marker level an agent request went out with (prompt_cache.LEVELS);
    # system_tail_changed: its system message's changing part differed from the chat's previous agent
    # request (a cache miss for everything after it); usage_keys, reasoning_shape, reasoning_signature: the
    # response's shape (finish_inference_call).
    if call.get("prompt_cache") in PROMPT_CACHE_LEVELS:
        result["prompt_cache"] = call["prompt_cache"]
    keys = call.get("usage_keys")
    if isinstance(keys, list) and keys == usage_keys(None, keys):
        result["usage_keys"] = keys
    if call.get("reasoning_shape") in REASONING_SHAPES:
        result["reasoning_shape"] = call["reasoning_shape"]
    for key in ("system_tail_changed", "reasoning_signature"):
        if call.get(key) is True:
            result[key] = True
    # A 429's message, the wait Databricks asked for and whether it came in a Retry-After header.
    if isinstance(call.get("rate_limit_message"), str):
        result["rate_limit_message"] = call["rate_limit_message"][:200]
    if (seconds := _seconds(call.get("retry_after"))) is not None:
        result["retry_after"] = seconds
    if isinstance(call.get("retry_after_header"), bool):
        result["retry_after_header"] = call["retry_after_header"]
    usage = reported_usage(call.get("usage"))
    if usage:
        result["usage"] = usage
    if _explicit_rejection(result):
        estimate = 0.0
    elif "input_tokens" in usage and "output_tokens" in usage:
        estimate = estimated_dbu(model, usage)
    else:
        estimate = None
    result["estimated_dbu"] = estimate
    if fallback:
        result["legacy"] = True
    return result


def call_estimated_dbu(call):
    """(estimated DBU or None, priced) of one ledger record, as the usage panel counts it. priced is
    False only for a call that reported its usage to an endpoint without price data."""
    public = _public_call(call)
    if public["estimated_dbu"] is not None:
        return public["estimated_dbu"], True
    usage = public.get("usage") or {}
    return None, not ("input_tokens" in usage and "output_tokens" in usage)


def _totals(calls):
    """A group's counts and token sums. A token field no call of the group reported is None, not 0
    (Claude Opus 5.5 does not report reasoning_tokens: its thinking is counted in output_tokens); an
    explicit rejection counts as 0 input and output tokens. Every token field is None when a call that
    may have been billed has no usage."""
    totals = {"calls": len(calls), "successful": 0, "errors": 0, "rate_limited": 0, "retries": 0,
              **{field: 0 for field in USAGE_FIELDS}}
    estimates, reported = [], set()
    usage_complete = True
    for call in calls:
        totals["successful"] += call["status"] == "completed"
        totals["errors"] += call["status"] == "error"
        totals["rate_limited"] += call.get("http_status") == 429 or call.get("error_kind") == "rate_limit"
        totals["retries"] += isinstance(call.get("retry_reason"), str)
        rejected = _explicit_rejection(call)
        usage = {} if rejected else call.get("usage", {})
        if not rejected and not ("input_tokens" in usage and "output_tokens" in usage):
            usage_complete = False
        reported.update(("input_tokens", "output_tokens") if rejected else usage)
        for field in USAGE_FIELDS:
            totals[field] += usage.get(field, 0)
        estimates.append(call.get("estimated_dbu"))
    totals.update({field: None for field in USAGE_FIELDS if not usage_complete or (calls and field not in reported)})
    totals["estimated_dbu"] = (round(sum(estimates), 9)
                                if all(value is not None for value in estimates) else None)
    return totals


def session_metrics(session):
    """Build a JSON-safe, TypeScript-friendly metrics view for one session."""
    calls, event_ids, complete = [], set(), session.get("inference_ledger_version") == LEDGER_VERSION
    records = session.get("inference_calls", [])
    if not isinstance(records, list):
        records, complete = [], False
    for record in records:
        if not isinstance(record, dict):
            complete = False
            continue
        calls.append(_public_call(record))
        if isinstance(record.get("event_id"), str):
            event_ids.add(record["event_id"])

    # Pre-ledger assistant requests remain visible instead of silently vanishing
    # from upgraded conversations. They make the result explicitly incomplete
    # because historical title and compaction calls were not recorded.
    for index, event in enumerate(session.get("events", [])):
        if not isinstance(event, dict) or not isinstance(event.get("request_info"), dict):
            continue
        event_id = event.get("id")
        if isinstance(event_id, str) and event_id in event_ids:
            continue
        info = event["request_info"]
        legacy = {**info, "id": f"legacy-{event_id or index}", "purpose": "agent",
                  "created": event.get("created", session.get("created"))}
        calls.append(_public_call(legacy, fallback=True))
        complete = False

    calls.sort(key=lambda call: call["created"])
    by_purpose, by_model = [], []
    for purpose in PURPOSE_LABELS:
        group = [call for call in calls if call["purpose"] == purpose]
        if group:
            by_purpose.append({"key": purpose, "label": PURPOSE_LABELS[purpose], **_totals(group)})
    for model in dict.fromkeys(call["model"] for call in calls):
        group = [call for call in calls if call["model"] == model]
        by_model.append({"key": model, "model": model, **_totals(group)})

    info = session.get("context_info")
    last_input = info.get("last_request") if isinstance(info, dict) else None
    return {
        "scope": "session",
        "session_id": session.get("id"),
        "complete": complete,
        # What Databricks reported for the latest agent request, next to the app's estimate.
        "last_input": last_input if isinstance(last_input, dict) else None,
        "totals": _totals(calls),
        "calls": calls,
        "by_purpose": by_purpose,
        "by_model": by_model,
        "pricing": {
            "currency": "DBU",
            "unit": "per_1m_tokens",
            "label": "Databricks pay-per-token list-price DBU estimate",
            "source_url": PRICING_SOURCE,
            "open_source_url": OPEN_PRICING_SOURCE,  # The rates of OPEN_MODELS.
            "effective_at": PRICING_EFFECTIVE_AT,
            "estimated": True,
            "models": MODEL_DBU_PER_MILLION,
        },
    }


TOKEN_FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")


def call_tokens(call):
    """Every token one ledger record reports it processed: uncached input, cache reads and writes, and output
    (reasoning is part of output). 0 for a record without usage."""
    usage = reported_usage(call.get("usage")) if isinstance(call, dict) else {}
    return sum(usage.get(field, 0) for field in TOKEN_FIELDS)


def reported_tokens(records):
    """The tokens these ledger records processed (call_tokens), or None when none of them reported a token count."""
    records = [record for record in records if isinstance(record, dict)]
    if not any(field in reported_usage(record.get("usage")) for record in records for field in TOKEN_FIELDS):
        return None
    return sum(call_tokens(record) for record in records)


def usage_totals(records):
    """The usage panel's totals (_totals) of these ledger records: counts, token sums (a field no call reported
    is None) and the estimated DBU (None when a call has no estimate)."""
    return _totals([_public_call(record) for record in records if isinstance(record, dict)])


# The usage summary each conversation keeps in its header (usage_summary), so a view of every chat's usage reads
# headers only: one row per local date, model and purpose, the newest SUMMARY_DAYS dates and SUMMARY_ROWS rows.
SUMMARY_VERSION = 1
SUMMARY_COLUMNS = ("date", "model", "purpose", "calls", *TOKEN_FIELDS, "estimated_dbu", "unpriced_calls")
SUMMARY_DAYS = 90
SUMMARY_ROWS = 120


def local_date(value):
    """The local calendar date (YYYY-MM-DD) of a ledger time, as the usage CSV shows it."""
    try:
        moment = datetime.fromisoformat(_iso_time(value).replace("Z", "+00:00"))
        return moment.astimezone().date().isoformat()
    except (ValueError, OverflowError, OSError):
        return "1970-01-01"


def usage_summary(session):
    """{version, calls, rows: [[date, model, purpose, calls, input, cache read, cache write, output, estimated DBU,
    unpriced calls]]} from the conversation's ledger. Tokens are those reported; DBU sums the priced calls, and
    unpriced_calls counts calls that reported usage to an endpoint without list rates."""
    groups = {}
    records = session.get("inference_calls") if isinstance(session.get("inference_calls"), list) else []
    for record in records:
        if not isinstance(record, dict):
            continue
        call = _public_call(record)
        key = (local_date(call["created"]), call["model"][:128], call["purpose"])
        row = groups.setdefault(key, [*key, 0, 0, 0, 0, 0, 0.0, 0])
        row[3] += 1
        usage = call.get("usage") or {}
        for index, field in enumerate(TOKEN_FIELDS, start=4):
            row[index] += usage.get(field, 0)
        estimate, priced = call_estimated_dbu(record)
        if estimate is not None:
            row[8] = round(row[8] + estimate, 9)
        elif not priced:
            row[9] += 1
    dates = sorted({key[0] for key in groups}, reverse=True)[:SUMMARY_DAYS]
    rows = sorted((row for key, row in groups.items() if key[0] in dates), key=lambda row: (row[0], row[1], row[2]))
    return {"version": SUMMARY_VERSION, "calls": len(records), "rows": rows[-SUMMARY_ROWS:]}


def _summary_date(value):
    """Whether value is a calendar date written YYYY-MM-DD (the form local_date writes)."""
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def summary_rows(summary):
    """The valid rows of a header's usage_summary as dicts (SUMMARY_COLUMNS); [] for a missing or damaged one."""
    if not isinstance(summary, dict) or summary.get("version") != SUMMARY_VERSION or not isinstance(summary.get("rows"), list):
        return []
    rows = []
    for row in summary["rows"][-SUMMARY_ROWS:]:
        if (not isinstance(row, list) or len(row) != len(SUMMARY_COLUMNS) or not all(isinstance(item, str) for item in row[:3])
                or not _summary_date(row[0]) or not all(type(item) is int and item >= 0 for item in (*row[3:8], row[9]))
                or isinstance(row[8], bool) or not isinstance(row[8], (int, float)) or not math.isfinite(row[8])):
            continue
        rows.append(dict(zip(SUMMARY_COLUMNS, row)))
    return rows
