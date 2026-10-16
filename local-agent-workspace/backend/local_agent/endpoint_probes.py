"""Endpoint checks: a few small paid requests that show what a serving endpoint accepts, sent only after you
approve their estimated cost (Settings → Model and context → Check endpoint… and Test model).

This module holds the checks and what follows from them: each probe's request bodies (fixed synthetic text,
never temperature, top_p, top_k, tool_choice or an assistant prefill), the cost plan you approve (plan_id),
how the replies are read (read_stream, interpret), the capability table, sanitized fixtures, and the
results file in the state folder (CheckStore). endpoint_checks_api.EndpointCheckRunner sends the requests.

Results change what agent requests do only through explicit hooks, each read where that behaviour lives:
CheckStore.cache_level (cache markers rejected: the cache ladder starts lower), CheckStore.refused (an
image part or response_format refused, or signed thinking sent back refused: Settings → Reasoning → Send
earlier thinking back then skips the endpoint). The thinking stream's shape is recorded for reference.
"""
import base64
import copy
import hashlib
import json
import math
import os
import re
import struct
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path

from . import prompt_cache, reasoning_replay
from .context import estimate_scale, estimate_text_tokens, estimate_tokens
from .model_profiles import MAX_EXECUTION_SECONDS, profile_for, prompt_caching, thinking_reserve
from .model_stream import ToolCallBuffer
from .reasoning import effort_request, reasoning_delta, reasoning_shape, request_format
from .telemetry import (MODEL_DBU_PER_MILLION, PRICING_EFFECTIVE_AT, InferenceError, estimated_dbu, reported_usage,
                        usage_keys)

RESULTS_FILE = "endpoint_checks.json"
FIXTURES_FOLDER = "endpoint_fixtures"
MAX_ENDPOINTS = 50  # Endpoints kept in the results file; the oldest check goes first.
ENDPOINT_KEY = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
THINKING_MAX_TOKENS = 2_048  # A probe to an endpoint that thinks: room for short thinking and a short answer.
SMALL_MAX_TOKENS = 256
CACHE_TEXT_TOKENS = 6_000  # Above every Claude model's minimum cacheable prefix (up to 4,096 tokens).
PROBE_IDS = ("basic", "reasoning", "cache", "tools", "response_format", "image")
PROBES = (
    {"id": "basic", "title": "Smoke test and usage fields",
     "question": "Does a small streamed request work, how fast does it answer, and which usage fields come back?",
     "requests": (("basic", "One short streamed request (this is also Test model)"),)},
    {"id": "reasoning", "title": "Reasoning fields and the thinking stream",
     "question": "Are the endpoint's reasoning fields accepted at Medium, and how does its thinking stream "
                 "(the delta shape, a signature)?",
     "requests": (("reasoning", "A small arithmetic question at Medium effort"),)},
    {"id": "cache", "title": "Prompt-cache markers and a cache read",
     "question": "Are cache_control markers accepted, does a repeat read the cache, and does prompt_tokens count "
                 "cached tokens?",
     "requests": (("cache_write", "About 6,000 tokens of fixed text with cache markers"),
                  ("cache_write_documented", "Only if the first is rejected (not billed): the documented marker form"),
                  ("cache_read", "The same request again"))},
    {"id": "tools", "title": "Tool calls and thinking sent back",
     "question": "Which finish reason comes with a tool call, and is the tool result accepted without and with "
                 "the reply's signed thinking?",
     "requests": (("tool_call", "Asks for one call of a harmless get_weekday tool"),
                  ("replay_plain", "The tool result sent back"),
                  ("replay_signed", "The tool result with the reply's signed thinking (only if a signature came)"))},
    {"id": "response_format", "title": "Structured output with tools",
     "question": "Is response_format (a JSON schema) accepted together with tools?",
     "requests": (("response_format", "Asks for a small JSON answer, with one tool offered"),)},
    {"id": "image", "title": "Image input",
     "question": "Is an image_url content part (a 16 × 16 PNG made by the app) accepted?",
     "requests": (("image", "Asks for the colour of a small square"),)},
)
ALTERNATIVES = {"cache_write_documented": "cache_write"}  # Sent only when the other was rejected with HTTP 400.
CACHE_REQUESTS = ("cache_write", "cache_write_documented", "cache_read")
BASIC_PROMPT = "Endpoint check: reply with the single word ready."
REASONING_PROMPT = "Endpoint check: what is 17 times 23? Answer with the number only."
CACHE_PROMPT = "Endpoint check: reply with the single word cached."
TOOL_PROMPT = ("Endpoint check: call the get_weekday tool once for the date 2026-01-05, then say the weekday "
               "in one word.")
FORMAT_PROMPT = ("Endpoint check: reply with a JSON object whose answer field is the sum of 2 and 3. Do not call "
                 "any tool.")
IMAGE_PROMPT = "Endpoint check: what colour is this square? Answer in one word."
WEEKDAY_RESULT = json.dumps({"date": "2026-01-05", "weekday": "Monday"})
WEEKDAY_TOOL = {"type": "function", "function": {
    "name": "get_weekday", "description": "Returns the weekday of a date.",
    "parameters": {"type": "object", "properties": {"date": {"type": "string", "description": "A date as YYYY-MM-DD."}},
                   "required": ["date"], "additionalProperties": False}}}
RESPONSE_FORMAT = {"type": "json_schema", "json_schema": {
    "name": "endpoint_check", "strict": True,
    "schema": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"],
               "additionalProperties": False}}}
# Never sent by a probe (the endpoints' documented rules; tests/test_model_profiles.FORBIDDEN_KEYS).
FORBIDDEN_KEYS = ("temperature", "top_p", "top_k", "tool_choice")
STOPPING_STATUSES = (401, 403, 429)  # Besides 5xx and a failed connection: the run stops, nothing is retried.


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def endpoint_key(model):
    """The results file's key for an endpoint, or None for a name that is not one."""
    key = model.strip().lower() if isinstance(model, str) else ""
    return key if ENDPOINT_KEY.match(key) else None


def square_png(size=16, rgb=(220, 30, 30)):
    """A solid red size × size PNG, built here so the image probe sends no file."""
    rows = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


IMAGE_URL = "data:image/png;base64," + base64.b64encode(square_png()).decode("ascii")


def cache_text(tokens=CACHE_TEXT_TOKENS):
    """Fixed synthetic system text of about tokens estimated tokens (the same bytes every time)."""
    lines, index = ["You answer endpoint checks briefly. The reference notes below are filler for a cache check."], 0
    while estimate_text_tokens("\n".join(lines)) < tokens:
        index += 1
        lines.append(f"Reference note {index:04d}: synthetic filler for an endpoint check, value {index * 37 % 1000:03d}.")
    return "\n".join(lines)


CACHE_TEXT = cache_text()


def applies(probe_id, model):
    """Whether a probe means anything for this endpoint: reasoning needs reasoning fields to send, the cache probe
    a Claude endpoint (the only ones Databricks documents cache_control for)."""
    if probe_id == "reasoning":
        return request_format(model, "auto") != "none"
    if probe_id == "cache":
        return prompt_caching(model) or "claude" in model.lower()
    return probe_id in PROBE_IDS


def reasoning_fields(model, max_tokens):
    """The endpoint's own reasoning fields at Medium with thinking summaries (Automatic format)."""
    return effort_request(model, "medium", "auto", True, max_tokens=max_tokens)["fields"]


def max_tokens_for(model, thinking):
    """A probe request's max_tokens: room for short thinking where the endpoint thinks, else a short answer."""
    tokens = THINKING_MAX_TOKENS if thinking or thinking_reserve(model) else SMALL_MAX_TOKENS
    output_max = (profile_for(model) or {}).get("output_max")
    return min(tokens, output_max) if output_max else tokens


def _user(text):
    return {"role": "user", "content": text}


def _request(model, messages, thinking=False, **extra):
    max_tokens = max_tokens_for(model, thinking)
    fields = reasoning_fields(model, max_tokens) if thinking else {}
    return {"messages": messages, **extra, "stream": True, "max_tokens": max_tokens, **fields}


def _tool_exchange(outcome, signed):
    """The tool_call reply as an assistant message and the app's answer to each of its calls, or None."""
    parsed = (outcome or {}).get("parsed") or {}
    calls = parsed.get("tool_calls") or []
    if (outcome or {}).get("http_status") != 200 or not calls:
        return None
    items = parsed.get("items") or []
    if signed and not items:
        return None
    text = parsed.get("text") or None
    assistant = {"role": "assistant", "content": reasoning_replay.content(items, text) if signed else text,
                 "tool_calls": calls}
    return [assistant, *({"role": "tool", "tool_call_id": call["id"], "content": WEEKDAY_RESULT} for call in calls)]


TEMPLATE_EXCHANGE = [{"role": "assistant", "content": "<the tool_call reply's text>", "tool_calls": [
    {"id": "<its call id>", "type": "function", "function": {"name": "get_weekday", "arguments": "<its arguments>"}}]},
    {"role": "tool", "tool_call_id": "<its call id>", "content": WEEKDAY_RESULT}]


def body(request_id, model, outcomes=None):
    """The request body of one probe request, or None when it is not sent (an alternative whose request was not
    rejected, or a follow-up whose reply did not come). Without outcomes (a plan), follow-ups get placeholders."""
    planning = outcomes is None
    outcomes = outcomes or {}
    if request_id == "basic":
        return _request(model, [_user(BASIC_PROMPT)])
    if request_id == "reasoning":
        return _request(model, [_user(REASONING_PROMPT)], thinking=True)
    if request_id in CACHE_REQUESTS:
        if request_id == "cache_write_documented" and not planning and (outcomes.get("cache_write") or {}).get("http_status") != 400:
            return None
        base = _request(model, [{"role": "system", "content": CACHE_TEXT}, _user(CACHE_PROMPT)])
        level = "messages" if request_id == "cache_write_documented" else "full"
        if request_id == "cache_read" and not planning:
            accepted = [name for name in ("cache_write", "cache_write_documented")
                        if (outcomes.get(name) or {}).get("http_status") == 200]
            if not accepted:
                return None
            level = "full" if accepted[0] == "cache_write" else "messages"
        return prompt_cache.marked(base, len(CACHE_TEXT), level)
    if request_id == "tool_call":
        return _request(model, [_user(TOOL_PROMPT)], thinking=applies("reasoning", model), tools=[WEEKDAY_TOOL])
    if request_id in ("replay_plain", "replay_signed"):
        exchange = (copy.deepcopy(TEMPLATE_EXCHANGE) if planning
                    else _tool_exchange(outcomes.get("tool_call"), request_id == "replay_signed"))
        if exchange is None:
            return None
        return _request(model, [_user(TOOL_PROMPT), *exchange], thinking=applies("reasoning", model),
                        tools=[WEEKDAY_TOOL])
    if request_id == "response_format":
        return _request(model, [_user(FORMAT_PROMPT)], tools=[WEEKDAY_TOOL], response_format=RESPONSE_FORMAT)
    if request_id == "image":
        return _request(model, [{"role": "user", "content": [{"type": "text", "text": IMAGE_PROMPT},
                                                              {"type": "image_url", "image_url": {"url": IMAGE_URL}}]}])
    raise ValueError(f"Unknown endpoint check request: {request_id}.")


def chosen_probes(model, probes=None):
    """The probes a plan covers: those asked for that apply to the endpoint (all that apply by default)."""
    wanted = list(PROBE_IDS) if probes is None else list(dict.fromkeys(probes))
    unknown = [item for item in wanted if item not in PROBE_IDS]
    if unknown:
        raise ValueError(f"Unknown endpoint check: {unknown[0]}.")
    chosen = [probe for probe in PROBES if probe["id"] in wanted and applies(probe["id"], model)]
    if not chosen:
        raise ValueError("None of the chosen checks applies to this endpoint.")
    return chosen


def plan(model, probes=None):
    """What a run sends, with its estimated cost, before anything is sent: {model, probes: [{id, title, question,
    requests: [{id, label, estimated_input_tokens, max_tokens, estimated_dbu, alternative_to?}]}], total_dbu (at
    most, at list prices; None without price data), priced, max_output_tokens, pricing_effective_at, plan_id}.
    Input is the app's estimate times the endpoint's starting estimate ratio; the worst case bills all of it (at
    the cache-write rate for the cache requests) plus every max_tokens. A request sent only after another was
    rejected replaces it (Databricks does not bill a rejected request). plan_id names the bodies and the prices,
    so an approval covers exactly this plan."""
    model = model.strip()
    rates = MODEL_DBU_PER_MILLION.get(model.lower())
    prior = estimate_scale([], model)
    entries, canonical, totals = [], [], {}
    for probe in chosen_probes(model, probes):
        requests = []
        for request_id, label in probe["requests"]:
            sent = body(request_id, model)
            canonical.append([request_id, sent])
            tokens = math.ceil(estimate_tokens(sent["messages"], sent.get("tools", ())) * prior)
            cost = None
            if rates is not None:
                input_rate = rates["cache_creation_input_tokens"] if request_id in CACHE_REQUESTS else rates["input_tokens"]
                cost = round((tokens * input_rate + sent["max_tokens"] * rates["output_tokens"]) / 1_000_000, 6)
            entry = {"id": request_id, "label": label, "estimated_input_tokens": tokens, "max_tokens": sent["max_tokens"],
                     "estimated_dbu": cost}
            if request_id in ALTERNATIVES:
                entry["alternative_to"] = ALTERNATIVES[request_id]
            totals[request_id] = (cost or 0, sent["max_tokens"])
            requests.append(entry)
        entries.append({"id": probe["id"], "title": probe["title"], "question": probe["question"], "requests": requests})
    worst, output = 0.0, 0
    for request_id, (cost, max_tokens) in totals.items():
        primary = ALTERNATIVES.get(request_id)
        if primary in totals:  # Replaces a rejected request: only what it costs more counts.
            cost, max_tokens = max(0.0, cost - totals[primary][0]), max(0, max_tokens - totals[primary][1])
        worst += cost
        output += max_tokens
    digest = hashlib.sha256(json.dumps({"model": model, "bodies": canonical, "pricing": PRICING_EFFECTIVE_AT,
                                        "rates": rates}, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return {"model": model, "probes": entries, "total_dbu": round(worst, 3) if rates is not None else None,
            "priced": rates is not None, "max_output_tokens": output, "pricing_effective_at": PRICING_EFFECTIVE_AT,
            "plan_id": digest.hexdigest()}


def planned_requests(plan_value):
    """[(probe id, request id, label)] in the order a run sends them."""
    return [(probe["id"], request["id"], request["label"]) for probe in plan_value["probes"] for request in probe["requests"]]


# Reading the replies.

def _visible(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content
                       if isinstance(part, dict) and part.get("type") in ("text", "output_text"))
    return ""


def read_stream(chunks):
    """What a probe reply's chunks show: {text, finish_reason, usage, usage_keys, reasoning_seen, reasoning_shape,
    signature, items (its signed reasoning, assembled as the history keeps it), tool_calls, error}."""
    buffer, collector = ToolCallBuffer(), reasoning_replay.Collector()
    text, finish, usage, keys, shape, signed, seen, error = [], None, {}, [], None, False, False, None
    try:
        for chunk in chunks:
            if not isinstance(chunk, dict):
                raise InferenceError("The endpoint sent a chunk that is not a JSON object.", "invalid_response")
            if isinstance(chunk.get("usage"), dict):
                keys = usage_keys(chunk["usage"], keys)
                usage = reported_usage(chunk["usage"]) or usage
            if "error" in chunk:
                raise InferenceError(str(chunk["error"]), "stream_error")
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") if isinstance(choice, dict) else None
                if not isinstance(delta, dict):
                    continue
                _, thought = reasoning_delta(delta)
                if thought:
                    seen = True
                    kind, has_signature = reasoning_shape(delta)
                    shape, signed = shape or kind, signed or has_signature
                    collector.add(delta)
                text.append(_visible(delta.get("content")))
                for part in delta.get("tool_calls") or []:
                    buffer.add(part)
                if isinstance(choice.get("finish_reason"), str):
                    finish = choice["finish_reason"]
    except InferenceError as problem:
        error = str(problem)[:300]
    calls = buffer.finish()
    return {"text": "".join(text), "finish_reason": finish, "usage": usage, "usage_keys": keys, "reasoning_seen": seen,
            "reasoning_shape": shape, "signature": signed, "items": collector.result(),
            "tool_calls": [calls[index] for index in sorted(calls)], "error": error}


def outcome_status(outcome):
    """accepted, rejected (an HTTP 4xx answer to the request itself), error (anything else) or None (not sent)."""
    if not outcome or outcome.get("skipped"):
        return None
    status = outcome.get("http_status")
    if status == 200:
        parsed = outcome.get("parsed") or {}
        return "error" if parsed.get("error") or parsed.get("finish_reason") is None else "accepted"
    if type(status) is int and 400 <= status < 500 and status not in STOPPING_STATUSES:
        return "rejected"
    return "error"


def outcome_text(outcome):
    """How one request went, in a few words."""
    status = outcome.get("http_status")
    if status is None:
        return outcome.get("error") or "No answer."
    if status != 200:
        return f"HTTP {status}" + (f": {outcome['error'][:200]}" if outcome.get("error") else "")
    parsed = outcome.get("parsed") or {}
    if parsed.get("error"):
        return f"The stream failed: {parsed['error'][:200]}"
    return f"Finish reason {parsed.get('finish_reason') or 'none'}"


def _tokens(value):
    return f"{value:,}" if type(value) is int else "0"


def prompt_counting(first, second):
    """How prompt_tokens counts cached tokens, from a request and its repeat: 'separate' (uncached input only;
    the buckets add up to the same input both times), 'included' (prompt_tokens still counts the cache read) or
    'unclear'; None when the repeat read nothing from the cache."""
    read = second.get("cache_read_input_tokens") or 0
    if not read:
        return None
    prompt = second.get("input_tokens") or 0

    def total(usage):
        return sum(usage.get(key) or 0 for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    if prompt * 10 >= read * 9:
        return "included"
    if prompt * 2 < read and abs(total(first) - total(second)) * 20 <= max(total(first), 1):
        return "separate"
    return "unclear"


def interpret(probe_id, outcomes):
    """One probe's result from its requests' outcomes ({request id: outcome}), or None when its first request
    was not sent: {status: accepted|rejected|not_seen|error, summary, checked, ...facts}. not_seen: the
    endpoint answered but did not show what was asked (no thinking, nothing read from the cache, no JSON)."""
    first = {"basic": "basic", "reasoning": "reasoning", "cache": "cache_write", "tools": "tool_call",
             "response_format": "response_format", "image": "image"}[probe_id]
    if outcome_status(outcomes.get(first)) is None:
        return None
    result = {"checked": now()}
    if probe_id == "cache":
        return {**result, **_cache_result(outcomes)}
    if probe_id == "tools":
        return {**result, **_tools_result(outcomes)}
    outcome = outcomes[first]
    status, parsed = outcome_status(outcome), outcome.get("parsed") or {}
    result.update(status=status, http_status=outcome.get("http_status"), finish_reason=parsed.get("finish_reason"))
    if status != "accepted":
        return {**result, "summary": outcome_text(outcome)}
    answer = " ".join(parsed.get("text", "").split())[:80]
    if probe_id == "basic":
        result.update(usage_keys=parsed.get("usage_keys") or [], answer=answer, elapsed=outcome.get("elapsed"),
                      ttfb=outcome.get("ttfb"))
        if parsed.get("finish_reason") != "stop" or not answer:
            result["status"] = "not_seen"
        result["summary"] = (f"Answered “{answer}” in {outcome.get('elapsed') or 0:.1f} s (first byte after "
                             f"{outcome.get('ttfb') or 0:.1f} s), finish reason {parsed.get('finish_reason')}.")
    elif probe_id == "reasoning":
        shape, signature = parsed.get("reasoning_shape"), parsed.get("signature") is True
        result.update(reasoning_shape=shape, signature=signature,
                      reasoning_tokens="reasoning_tokens" in (parsed.get("usage") or {}))
        if not parsed.get("reasoning_seen"):
            result.update(status="not_seen", summary="The reasoning fields were accepted, but no thinking came back.")
        else:
            result["summary"] = (f"Accepted. Thinking came as {shape or 'an unknown shape'}"
                                 + (", with a signature." if signature else ", without a signature."))
    elif probe_id == "response_format":
        try:
            value = json.loads(parsed.get("text") or "")
        except ValueError:
            value = None
        if isinstance(value, dict) and "answer" in value:
            result["summary"] = "Accepted: the reply was the JSON object the schema asked for."
        else:
            result.update(status="not_seen", summary="Accepted, but the reply was not the JSON object the schema asked for.")
    elif probe_id == "image":
        result.update(answer=answer, summary=f"Accepted: the endpoint answered “{answer}” for a red square.")
    return result


def _cache_result(outcomes):
    write, documented, read = (outcomes.get(name) for name in CACHE_REQUESTS)
    accepted = next((name for name in ("cache_write", "cache_write_documented")
                     if outcome_status(outcomes.get(name)) == "accepted"), None)
    if accepted is None:
        rejected = all(outcome_status(item) in ("rejected", None) for item in (write, documented))
        status = "rejected" if rejected and outcome_status(write) == "rejected" else "error"
        last = documented if outcome_status(documented) is not None else write
        return {"status": status, "level": "off" if status == "rejected" else None, "summary": (
            "Cache markers were rejected, also in the documented form: " if status == "rejected" else "") + outcome_text(last)}
    level = "full" if accepted == "cache_write" else "messages"
    named = "All cache markers were accepted" if level == "full" else (
        "Markers on the system message were rejected; the documented form was accepted")
    first_usage = (outcomes[accepted].get("parsed") or {}).get("usage") or {}
    result = {"level": level, "cache_write_tokens": first_usage.get("cache_creation_input_tokens")}
    if outcome_status(read) != "accepted":
        return {**result, "status": "error" if read else "not_seen",
                "summary": named + (f"; the repeat failed: {outcome_text(read)}." if read else "; the repeat was not sent.")}
    second = (read.get("parsed") or {}).get("usage") or {}
    reads = second.get("cache_read_input_tokens") or 0
    counting = prompt_counting(first_usage, second)
    result.update(cache_read_tokens=reads, prompt_counting=counting)
    if not reads:
        return {**result, "status": "not_seen", "summary": named + ", but the repeat read nothing from the cache."}
    words = {"separate": "prompt_tokens leaves cached tokens out (separate buckets)",
             "included": "prompt_tokens also counts the cached tokens", "unclear": "how prompt_tokens counts them is unclear"}
    return {**result, "status": "accepted", "summary": (
        f"{named}; the repeat read {_tokens(reads)} tokens from the cache (the first wrote "
        f"{_tokens(result['cache_write_tokens'])}), and {words[counting]}.")}


def _tools_result(outcomes):
    call, plain, signed = (outcomes.get(name) for name in ("tool_call", "replay_plain", "replay_signed"))
    status = outcome_status(call)
    parsed = (call or {}).get("parsed") or {}
    calls, finish = parsed.get("tool_calls") or [], parsed.get("finish_reason")
    result = {"http_status": call.get("http_status"), "finish_reason": finish, "tool_calls": len(calls),
              "signature": bool(parsed.get("items")), "replay_plain": outcome_status(plain),
              "replay_signed": outcome_status(signed) or ("skipped" if calls else None)}
    if status != "accepted":
        return {**result, "status": status, "summary": outcome_text(call)}
    if not calls:
        return {**result, "status": "not_seen", "summary": f"No tool call came (finish reason {finish}); nothing was sent back."}
    words = {"accepted": "accepted", "rejected": "rejected", "error": "failed", "skipped": "not sent (no signature came)",
             None: "not sent"}
    stop = " (finish reason stop although a complete tool call came)" if finish == "stop" else ""
    return {**result, "status": "accepted", "finish_stop_with_calls": finish == "stop", "summary": (
        f"A tool call came with finish reason {finish}{stop}. The tool result was {words[result['replay_plain']]} "
        f"without the reply's thinking and {words[result['replay_signed']]} with its signed thinking.")}


# The capability table (the Check endpoint dialog and Agent tools → Overview).

def _row(key, capability, result, source, status):
    return {"id": key, "capability": capability, "result": result, "source": source, "status": status}


def capabilities(model, saved):
    """[{id, capability, result, source, status}] for an endpoint: what its checks showed (source "probed
    <date>"), else what Databricks documents for it (model_profiles), else unknown."""
    results = (saved or {}).get("results") or {}
    profile = profile_for(model) or {}
    rows = []

    def probed(probe_id):
        value = results.get(probe_id)
        return (value, f"probed {str(value.get('checked', ''))[:10]}") if isinstance(value, dict) else (None, "unknown")

    def documented(flag, yes, no="Unknown"):
        return (yes, "documented", "documented") if profile.get(flag) is True else (no, "unknown", "unknown")

    words = {"accepted": "Accepted", "rejected": "Rejected", "not_seen": "Accepted, not shown", "error": "Failed",
             "skipped": "Not sent"}
    basic, source = probed("basic")
    rows.append(_row("streaming", "Streamed chat request", basic["summary"] if basic else "Not checked yet", source,
                     basic["status"] if basic else "unknown"))
    keys = (basic or {}).get("usage_keys")
    rows.append(_row("usage", "Usage fields", ", ".join(keys) if keys else "Not checked yet", source if keys else "unknown",
                     "accepted" if keys else "unknown"))
    value, source = probed("reasoning")
    if value:
        rows.append(_row("reasoning", "Reasoning fields", words.get(value["status"], value["status"]), source, value["status"]))
        shape = value.get("reasoning_shape")
        rows.append(_row("thinking_shape", "Thinking stream", (shape + (", signed" if value.get("signature") else ", unsigned"))
                         if shape else "No thinking came back", source, "accepted" if shape else "not_seen"))
    else:
        known = request_format(model, "auto") != "none" and bool(profile)
        rows.append(_row("reasoning", "Reasoning fields", "Documented" if known else "Unknown",
                         "documented" if known else "unknown", "documented" if known else "unknown"))
        rows.append(_row("thinking_shape", "Thinking stream", "Unknown", "unknown", "unknown"))
    value, source = probed("cache")
    if value:
        level = {"full": "All markers accepted", "messages": "Documented forms only", "off": "Rejected"}.get(
            value.get("level"), words.get(value["status"], value["status"]))
        rows.append(_row("cache_control", "Prompt-cache markers", level, source,
                         "rejected" if value.get("level") == "off" else value["status"] if value.get("level") is None
                         else "accepted"))
        reads = value.get("cache_read_tokens")
        rows.append(_row("cache_read", "Cache read on a repeat", f"{_tokens(reads)} tokens read" if reads else "Nothing read",
                         source, "accepted" if reads else "not_seen"))
        counting = {"separate": "Leaves cached tokens out", "included": "Includes cached tokens", "unclear": "Unclear"}
        rows.append(_row("cache_counting", "prompt_tokens with a cache read", counting.get(value.get("prompt_counting"), "Unknown"),
                         source if value.get("prompt_counting") else "unknown",
                         "accepted" if value.get("prompt_counting") else "unknown"))
    else:
        rows.append(_row("cache_control", "Prompt-cache markers", *documented("prompt_caching", "Documented (Claude)")))
        rows.append(_row("cache_read", "Cache read on a repeat", "Unknown", "unknown", "unknown"))
        rows.append(_row("cache_counting", "prompt_tokens with a cache read", "Unknown", "unknown", "unknown"))
    value, source = probed("tools")
    rows.append(_row("tool_finish", "Finish reason with a tool call",
                     (value.get("finish_reason") or "none") if value and value.get("tool_calls") else
                     (words.get(value["status"], value["status"]) if value else "Unknown"),
                     source, value["status"] if value else "unknown"))
    replay = (value or {}).get("replay_signed")
    rows.append(_row("replay", "Signed thinking sent back", words.get(replay, "Unknown") if replay else "Unknown",
                     source if replay else "unknown", replay if replay in ("accepted", "rejected", "error") else "unknown"))
    value, source = probed("response_format")
    rows.append(_row("structured_output", "response_format with tools",
                     words.get(value["status"], value["status"]) if value else "Not confirmed",
                     source, value["status"] if value else "unknown"))
    value, source = probed("image")
    if value:
        rows.append(_row("vision", "Image input (image_url)", words.get(value["status"], value["status"]), source, value["status"]))
    else:
        rows.append(_row("vision", "Image input (image_url)", *documented("vision", "Documented",
                                                                          "Not documented" if profile else "Unknown")))
    rows.append(_row("request_limit", "Request time limit", f"{MAX_EXECUTION_SECONDS} s", "documented", "documented"))
    return rows


# Fixtures: a probe's request and reply, sanitized, in the format serving.ReplayTransport.from_fixture reads.

ID_PREFIX = re.compile(r"[A-Za-z]+(?:[_-][A-Za-z]+)*[_-]")
ID_KEYS = ("id", "tool_call_id", "response_id")


def id_prefix(value):
    """A provider id cut to its prefix: msg_bdrk_01A… → msg_bdrk_…"""
    match = ID_PREFIX.match(value)
    return (match.group(0) if match else "") + "…"


def sanitize(value, redact=None, parent_type=None):
    """A fixture-safe copy: signatures, redacted thinking and encrypted summaries replaced by their length, ids cut
    to their prefix, and text passed through redact (Settings.redact)."""
    if isinstance(value, dict):
        kind = value.get("type") if isinstance(value.get("type"), str) else parent_type
        result = {}
        for key, item in value.items():
            if isinstance(item, str) and item and (key == "signature" or (kind == "redacted_thinking" and key == "data")
                                                   or (kind == "summary_encrypted_text" and key != "type")):
                result[key] = f"<{key}: {len(item)} chars>"
            elif isinstance(item, str) and key in ID_KEYS:
                result[key] = id_prefix(item)
            else:
                result[key] = sanitize(item, redact, kind)
        return result
    if isinstance(value, list):
        return [sanitize(item, redact, parent_type) for item in value]
    if isinstance(value, str) and redact is not None:
        return redact(value)
    return value


def fixture(model, request_id, sent, outcome, redact=None):
    """One request's fixture: the body sent and the answer (status, the content-type and Retry-After headers, and
    the chunks or the error text), sanitized; never a host, a URL or other headers."""
    headers = {key: value for key, value in (outcome.get("headers") or {}).items() if key in ("content-type", "retry-after")}
    record = {"model": model, "probe": request_id, "recorded": now(), "request": sanitize(sent, redact)}
    if outcome.get("http_status") is None:
        return {**record, "error": "connect", "note": sanitize(outcome.get("error") or "", redact)}
    record.update(status=outcome["http_status"], headers=headers)
    if outcome["http_status"] == 200:
        record["chunks"] = sanitize(outcome.get("chunks") or [], redact)
    else:
        record["text"] = sanitize(outcome.get("error") or "", redact)
    return record


def call_record(request_id, model, outcome):
    """A request's line in the results file: status, finish reason, reported usage, estimated DBU, timings."""
    parsed = outcome.get("parsed") or {}
    usage = parsed.get("usage") or {}
    record = {"request": request_id, "http_status": outcome.get("http_status"), "finish_reason": parsed.get("finish_reason"),
              "usage": usage, "ttfb": outcome.get("ttfb"), "elapsed": outcome.get("elapsed")}
    if outcome.get("http_status") == 200 and "input_tokens" in usage and "output_tokens" in usage:
        record["estimated_dbu"] = estimated_dbu(model, usage)
    elif type(outcome.get("http_status")) is int:
        record["estimated_dbu"] = 0.0 if outcome["http_status"] != 200 else None  # A rejection is not billed.
    return record


class CheckStore:
    """Endpoint check results in <state>/endpoint_checks.json (0600, atomic writes, reread when the file changes)
    and sanitized fixtures in <state>/endpoint_fixtures/<endpoint>/<request>.json. Kept in the state folder,
    never in a project or the repository. An unreadable file reads as no results (and is replaced by the next
    check)."""

    def __init__(self, state_dir):
        self.path = Path(state_dir) / RESULTS_FILE
        self.fixtures = Path(state_dir) / FIXTURES_FOLDER
        self._signature, self._data, self.problem = None, {"version": 1, "endpoints": {}}, None

    def _load(self):
        try:
            info = self.path.stat()
        except OSError:
            self._signature, self._data, self.problem = None, {"version": 1, "endpoints": {}}, None
            return self._data
        signature = (info.st_mtime_ns, info.st_size)
        if signature != self._signature:
            self._signature = signature
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("endpoints"), dict):
                    raise ValueError("not an endpoint checks file")
                self._data, self.problem = data, None
            except (OSError, ValueError) as exc:
                self._data, self.problem = {"version": 1, "endpoints": {}}, str(exc)[:200]
        return self._data

    def get(self, model):
        """The saved checks of an endpoint ({checked, plan_id, results: {probe: result}, calls}) or None."""
        key = endpoint_key(model)
        entry = self._load()["endpoints"].get(key) if key else None
        return copy.deepcopy(entry) if isinstance(entry, dict) else None

    def save(self, model, plan_id, results, calls):
        """Merge a run's probe results into the endpoint's (other probes keep theirs) and keep its calls."""
        key = endpoint_key(model)
        if key is None:
            return None
        data = copy.deepcopy(self._load())
        endpoints = data["endpoints"]
        entry = endpoints.get(key) if isinstance(endpoints.get(key), dict) else {}
        entry = {"checked": now(), "plan_id": plan_id,
                 "results": {**(entry.get("results") if isinstance(entry.get("results"), dict) else {}), **results},
                 "calls": calls}
        endpoints[key] = entry
        while len(endpoints) > MAX_ENDPOINTS:
            oldest = min(endpoints, key=lambda name: str((endpoints[name] or {}).get("checked", "")))
            del endpoints[oldest]
        _write(self.path, json.dumps({"version": 1, "endpoints": endpoints}, indent=1, sort_keys=True))
        self._signature = None
        return copy.deepcopy(entry)

    def write_fixture(self, model, request_id, record):
        key = endpoint_key(model)
        if key is None or not re.fullmatch(r"[a-z_]{1,40}", request_id):
            return None
        folder = self.fixtures / key
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / f"{request_id}.json"
        _write(path, json.dumps(record, indent=1, ensure_ascii=False))
        return path

    def result(self, model, probe_id):
        entry = self.get(model)
        value = ((entry or {}).get("results") or {}).get(probe_id)
        return value if isinstance(value, dict) else None

    def cache_level(self, model):
        """The cache-marker level the cache check found accepted ("messages" or "off"), or None (not checked, or
        every marker accepted): agent requests to the endpoint start the cache ladder there."""
        value = self.result(model, "cache")
        return value.get("level") if value and value.get("level") in ("messages", "off") else None

    def refused(self, model, capability):
        """True when a check showed the endpoint rejects this: "vision" (an image part), "structured_output"
        (response_format with tools) or "replay" (signed thinking sent back with a tool result)."""
        if capability == "replay":
            value = self.result(model, "tools")
            return bool(value) and value.get("replay_signed") == "rejected"
        probe = {"vision": "image", "structured_output": "response_format"}.get(capability)
        if probe is None:
            raise ValueError(f"Unknown capability: {capability}.")
        value = self.result(model, probe)
        return bool(value) and value.get("status") == "rejected"


def _write(path, text):
    """Write text to path atomically with mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)
