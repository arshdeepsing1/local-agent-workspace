"""Provider-reported inference telemetry and documented DBU estimates."""

import json
import uuid
from datetime import datetime, timezone


LEDGER_VERSION = 1
USAGE_FIELDS = (
    "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens", "reasoning_tokens",
)
PRICING_SOURCE = "https://www.databricks.com/product/pricing/proprietary-foundation-model-serving"
PRICING_EFFECTIVE_AT = "2026-09-22"
# Versioned snapshot of the Databricks pay-per-token list rates. These are
# estimates, not invoice records; account discounts and billing adjustments are
# intentionally outside this local calculation.
MODEL_DBU_PER_MILLION = {
    "databricks-claude-opus-4-8": {
        "input_tokens": 71.429,
        "output_tokens": 357.143,
        "cache_read_input_tokens": 7.143,
        "cache_creation_input_tokens": 89.286,
    },
}
PURPOSE_LABELS = {"agent": "Agent", "compaction": "Compaction", "title": "Title"}
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
        "too_many_requests": "rate_limit", "invalid_request_error": "invalid_request",
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
    usage = {}
    for name, keys in aliases.items():
        for key in keys:
            count = value.get(key)
            if type(count) is int and count >= 0:
                usage[name] = count
                break
    for field, key, name in (("prompt_tokens_details", "cached_tokens", "cache_read_input_tokens"),
                             ("completion_tokens_details", "reasoning_tokens", "reasoning_tokens")):
        details = value.get(field)
        count = details.get(key) if isinstance(details, dict) else None
        if name not in usage and type(count) is int and count >= 0:
            usage[name] = count
    return usage


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def begin_inference_call(session, purpose, model, *, event_id=None, max_output_tokens=None, attempt=None,
                         estimated_input_tokens=None):
    """Append one provider attempt to the durable per-session inference ledger.

    estimated_input_tokens is the app's unscaled heuristic estimate of the
    request; comparing it with provider-reported usage calibrates later budgets.
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
    call["finished"] = _now()
    return call


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
    # from prompt_tokens/input_tokens.
    value = sum(usage.get(field, 0) * rate for field, rate in rates.items()) / 1_000_000
    return round(value, 9)


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
    for key in ("http_status", "attempt", "max_output_tokens"):
        if type(call.get(key)) is int:
            result[key] = call[key]
    for key in ("error_kind", "finish_reason"):
        if isinstance(call.get(key), str):
            result[key] = call[key]
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


def _totals(calls):
    totals = {"calls": len(calls), "successful": 0, "errors": 0, "rate_limited": 0,
              **{field: 0 for field in USAGE_FIELDS}}
    estimates = []
    usage_complete = True
    for call in calls:
        totals["successful"] += call["status"] == "completed"
        totals["errors"] += call["status"] == "error"
        totals["rate_limited"] += call.get("http_status") == 429 or call.get("error_kind") == "rate_limit"
        rejected = _explicit_rejection(call)
        usage = {} if rejected else call.get("usage", {})
        if not rejected and not ("input_tokens" in usage and "output_tokens" in usage):
            usage_complete = False
        for field in USAGE_FIELDS:
            totals[field] += usage.get(field, 0)
        estimates.append(call.get("estimated_dbu"))
    if not usage_complete:
        totals.update({field: None for field in USAGE_FIELDS})
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

    return {
        "scope": "session",
        "session_id": session.get("id"),
        "complete": complete,
        "totals": _totals(calls),
        "calls": calls,
        "by_purpose": by_purpose,
        "by_model": by_model,
        "pricing": {
            "currency": "DBU",
            "unit": "per_1m_tokens",
            "label": "Databricks pay-per-token list-price DBU estimate",
            "source_url": PRICING_SOURCE,
            "effective_at": PRICING_EFFECTIVE_AT,
            "estimated": True,
            "models": MODEL_DBU_PER_MILLION,
        },
    }
