"""Reasoning effort and thinking text.

Requests: the JSON fields an agent request carries for a reasoning effort, per endpoint
(model_profiles: reasoning_format, effort_levels, default_effort, thinking, effort_map) and the
Settings → Reasoning choices (default effort, request format, thinking summaries). Only agent
requests (a chat's turns and its subagents') carry them. Titles, /btw side questions and
compaction requests send none: they use the endpoint's default, which their output allowance
(model_profiles.thinking_reserve, context.compaction_output_allowance) was sized for, and a chat
set to Max must not make every summary cost Max.

Responses: only reasoning text the provider supplies for display is shown, never replayed to the
model (the wire history keeps answers and tool calls only), redacted and capped at SUMMARY_LIMIT.
"""
import re

from .model_profiles import reasoning_profile

EFFORT_LEVELS = ("default", "low", "medium", "high", "xhigh", "max")
EFFORT_LABELS = {"default": "Default", "low": "Fast", "medium": "Medium", "high": "High", "xhigh": "Extra high",
                 "max": "Max"}
REQUEST_FORMATS = ("auto", "anthropic", "openai", "none")
FORMAT_LABELS = {"auto": "Automatic", "anthropic": "Anthropic fields", "openai": "OpenAI field",
                 "none": "Don't send reasoning fields"}
DEFAULT_EFFORT = "default"
DEFAULT_FORMAT = "auto"
# The top-level request fields this module adds; a resend without reasoning drops exactly these.
REQUEST_FIELDS = ("thinking", "output_config", "reasoning_effort")
# Words a Databricks 400 uses when it rejects a field this module added, and the field the notice names.
REJECTION_TERMS = (("output_config", "output_config.effort"), ("reasoning_effort", "reasoning_effort"),
                   ("budget_tokens", "thinking"), ("display", "thinking.display"), ("adaptive", "thinking"),
                   ("thinking", "thinking"), ("effort", None))
SUMMARY_LIMIT = 12_000  # Characters of reasoning text kept per response.


def valid_effort(value):
    return value if value in EFFORT_LEVELS else None


def guessed_format(model):
    """The request format Automatic uses for an endpoint without a reasoning profile (unverified)."""
    name = model.strip().lower() if isinstance(model, str) else ""
    if "claude" in name:
        return "anthropic"
    if "gpt-oss" in name or re.search(r"(?:^|[-_.])(?:gpt-5|o[134])(?:[-_.]|$)", name):
        return "openai"
    return "none"


def endpoint_reasoning(model):
    """What the app knows about how this endpoint takes an effort. verified is False for an
    endpoint without a profile: every level is offered and the format is a guess from its name."""
    profile = reasoning_profile(model)
    if profile is not None:
        return {**profile, "verified": True, "effort_levels": list(profile["effort_levels"]),
                "effort_map": dict(profile["effort_map"])}
    guess = guessed_format(model)
    return {"label": None, "verified": False, "reasoning_format": guess if guess != "none" else None,
            "effort_levels": list(EFFORT_LEVELS[1:]), "default_effort": None,
            "thinking": "adaptive" if guess == "anthropic" else None, "effort_map": {}}


def request_format(model, setting=DEFAULT_FORMAT):
    """The format requests to this endpoint use: the Settings choice, or the endpoint's own for Automatic."""
    if setting in ("anthropic", "openai", "none"):
        return setting
    return endpoint_reasoning(model)["reasoning_format"] or "none"


def sent_level(endpoint, effort):
    """The level the endpoint is sent for this effort (effort_map, else the nearest it accepts)."""
    if effort == "default":
        return None
    level = endpoint["effort_map"].get(effort, effort)
    levels = endpoint["effort_levels"]
    if level in levels:
        return level
    order = EFFORT_LEVELS[1:]
    lower = [item for item in levels if order.index(item) <= order.index(level)]
    return (lower or levels)[-1]


def default_label(endpoint):
    """What Default means on this endpoint, for "Default (Medium)" labels, or None when unknown."""
    if endpoint["default_effort"]:
        return EFFORT_LABELS[endpoint["default_effort"]]
    if endpoint["verified"] and endpoint["thinking"] == "adaptive":
        return "no thinking"
    return None


def effort_request(model, effort=DEFAULT_EFFORT, setting=DEFAULT_FORMAT, summaries=True, rejected=None):
    """The reasoning fields an agent request to this endpoint carries, and what to record about them.

    rejected: the field Databricks rejected earlier for this endpoint and format (AgentManager keeps
    it until a restart or a Settings save); then nothing is sent. Returns {effort, format, fields
    (the exact top-level JSON added), sent_effort, effort_field, skipped?, rejected_field?}."""
    effort = valid_effort(effort) or DEFAULT_EFFORT
    endpoint = endpoint_reasoning(model)
    fmt = request_format(model, setting)
    level = sent_level(endpoint, effort)
    fields, field = {}, None
    if fmt == "anthropic":
        display = {"display": "summarized"} if summaries else {}
        if level is not None:
            fields = {"thinking": {"type": "adaptive", **display}, "output_config": {"effort": level}}
            field = "output_config.effort"
        elif display and endpoint["thinking"] == "always":
            # Default sends no effort. An endpoint that always thinks still gets the display
            # choice: it changes what is shown, not how much it thinks or costs.
            fields = {"thinking": {"type": "adaptive", **display}}
    elif fmt == "openai" and level is not None:
        fields, field = {"reasoning_effort": level}, "reasoning_effort"
    result = {"effort": effort, "format": fmt, "fields": fields, "sent_effort": level if fields else None,
              "effort_field": field}
    if rejected and fields:
        result.update(fields={}, sent_effort=None, effort_field=None, skipped="rejected_earlier",
                      rejected_field=rejected)
    return result


def without_fields(request, field):
    """The record of a resend without reasoning fields after Databricks rejected field."""
    return {**request, "fields": {}, "sent_effort": None, "effort_field": None, "skipped": "rejected",
            "rejected_field": field}


def plain_default(request):
    """True for a request at Default that sends and skips nothing (nothing worth recording)."""
    return request["effort"] == "default" and not request["fields"] and not request.get("skipped")


def request_record(request):
    """The bounded summary request_info and the inference ledger keep."""
    record = {key: request[key] for key in ("effort", "format", "sent_effort", "effort_field", "skipped",
                                            "rejected_field") if request.get(key) is not None}
    record["fields"] = request["fields"]
    return record


def rejected_field(error_text, request):
    """The field a Databricks HTTP 400 body names when it rejects a reasoning field this request
    added, or None (no reasoning fields were added, or the body names none of them)."""
    fields = request.get("fields") if isinstance(request, dict) else None
    if not fields or not isinstance(error_text, str):
        return None
    text = error_text.lower()
    for term, field in REJECTION_TERMS:
        if re.search(r"(?<![a-z_])" + term + r"(?![a-z_])", text):
            field = field or request.get("effort_field") or next(iter(fields))  # A bare "effort".
            if field.split(".")[0] in fields:
                return field
    return None


def preview(model, effort, setting, summaries, rejected=None):
    """Settings → Reasoning and the composer's effort picker: what this endpoint gets for each level."""
    endpoint = endpoint_reasoning(model)
    request = effort_request(model, effort, setting, summaries, rejected)
    fmt = request["format"]
    meaning = default_label(endpoint)
    levels = []
    for value in EFFORT_LEVELS:
        label = EFFORT_LABELS[value]
        if value == "default":
            levels.append({"value": value, "label": f"Default ({meaning})" if meaning else label, "sent": None})
            continue
        sent = sent_level(endpoint, value) if fmt != "none" else None
        item = {"value": value, "label": label, "sent": sent}
        if sent is not None and sent != value:
            item["note"] = f"sent as {EFFORT_LABELS[sent]}"
        levels.append(item)
    notes = []
    name = endpoint["label"] or model
    if fmt == "none":
        notes.append("Request format is Don't send reasoning fields: requests carry none, so the endpoint uses its "
                     "own default." if setting == "none" else
                     f"No reasoning profile for {model}, and its name does not say which fields it takes, so requests "
                     "carry no reasoning fields. Choose a Request format to send them (unverified).")
    else:
        if not endpoint["verified"]:
            notes.append(f"No reasoning profile for {model}: every level is offered and these fields are unverified"
                         + (" (the format is a guess from its name)" if setting == "auto" else "")
                         + ". If Databricks rejects them, the app sends the request once more without them.")
        if request["effort"] == "default":
            notes.append(f"Default sends no effort field: {name} uses its own default"
                         + (f" ({meaning})." if meaning else "."))
            if request["fields"].get("thinking"):
                notes.append(f"{name} thinks on every request; thinking.display only asks it to return a summary "
                             "of its thinking (visibility, not depth or cost).")
        elif request["sent_effort"] and request["sent_effort"] != request["effort"]:
            accepted = ", ".join(EFFORT_LABELS[item] for item in endpoint["effort_levels"])
            notes.append(f"{name} accepts {accepted}: {EFFORT_LABELS[request['effort']]} is sent as "
                         f"{EFFORT_LABELS[request['sent_effort']]}.")
    if request.get("skipped"):
        notes.append(f"Databricks rejected {rejected} for this endpoint, so requests go without reasoning fields "
                     "until the app restarts or Settings are saved.")
    if request["effort"] not in ("default", "low") and request["fields"]:
        notes.append("Levels above the endpoint's default think longer and use more output tokens.")
    return {"model": model, "label": endpoint["label"], "verified": endpoint["verified"], "format": fmt,
            "format_setting": setting, "endpoint_format": endpoint["reasoning_format"],
            "default_effort": endpoint["default_effort"], "default_label": meaning, "levels": levels,
            **request_record(request), "notes": [note for note in notes if note]}


# Response side.

def _text(value):
    return value if isinstance(value, str) else ""


def reasoning_summary(content):
    """Reasoning text in a content list: reasoning items' summary_text and thinking items' thinking."""
    return reasoning_parts(content)[0]


def reasoning_parts(content):
    """(text, seen) for a content list: the displayable reasoning text, and whether it held a
    reasoning or thinking item at all (one with empty text still means the model is thinking)."""
    if not isinstance(content, list):
        return "", False
    texts, seen = [], False
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "reasoning":
            seen = True
            summary = block.get("summary")
            if isinstance(summary, list):
                texts.extend(_text(item.get("text")) for item in summary
                             if isinstance(item, dict) and item.get("type") == "summary_text")
        elif kind == "thinking":
            seen = True
            texts.append(_text(block.get("thinking")))
        elif kind == "redacted_thinking":
            seen = True
    return "".join(texts), seen


REASONING_ITEMS = ("reasoning", "thinking", "redacted_thinking")


def without_reasoning(messages):
    """messages with no reasoning in assistant messages (reasoning, thinking and redacted thinking content items,
    and reasoning_content or reasoning fields), for a request to an endpoint other than the one that wrote them:
    a reasoning signature is valid only for its own model. The app's history keeps none today; this keeps a
    fallback request (AgentManager.fallback_attempt) free of them if history ever does."""
    result = []
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "assistant":
            message = {key: value for key, value in message.items() if key not in ("reasoning_content", "reasoning")}
            if isinstance(message.get("content"), list):
                message["content"] = [part for part in message["content"]
                                      if not (isinstance(part, dict) and part.get("type") in REASONING_ITEMS)]
        result.append(message)
    return result


def reasoning_delta(delta):
    """(text, seen) for one streamed delta: reasoning items in delta.content, delta.reasoning_content
    (a string) and delta.reasoning (a string or {"text"} / {"summary"})."""
    if not isinstance(delta, dict):
        return "", False
    text, seen = reasoning_parts(delta.get("content"))
    for key in ("reasoning_content", "reasoning"):
        value = delta.get(key)
        if isinstance(value, dict):
            value = value.get("text", value.get("summary", value.get("content")))
        if isinstance(value, str):
            seen = True
            text += value
        elif isinstance(value, list):
            part, found = reasoning_parts(value)
            seen = seen or found
            text += part
    return text, seen


def _block_shape(content):
    """(kind, signature) of a content list's reasoning items: 'reasoning' or 'thinking' (the first seen,
    redacted thinking counting as thinking) or None, and whether a signature, an encrypted summary
    (summary_encrypted_text) or a redacted thinking item is among them."""
    kind, signature = None, False
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") not in ("reasoning", "thinking", "redacted_thinking"):
            continue
        kind = kind or ("reasoning" if block["type"] == "reasoning" else "thinking")
        items = block.get("summary") if block["type"] == "reasoning" else [block]
        signature = signature or block["type"] == "redacted_thinking" or any(
            isinstance(item, dict) and (item.get("type") == "summary_encrypted_text"
                                        or (isinstance(item.get("signature"), str) and item["signature"] != ""))
            for item in (items if isinstance(items, list) else []))
    return kind, signature


def reasoning_shape(delta):
    """(shape, signature) of one streamed delta, for the usage ledger: the shape that carried reasoning, as
    reasoning_delta reads it ('content.reasoning', 'content.thinking', 'reasoning_content', 'reasoning'; None
    without reasoning), and True when a signature, an encrypted summary or a redacted thinking item came
    with it. The values themselves are never kept."""
    if not isinstance(delta, dict):
        return None, False
    kind, signature = _block_shape(delta.get("content"))
    shape = "content." + kind if kind else None
    for key in ("reasoning_content", "reasoning"):
        value = delta.get(key)
        if isinstance(value, dict):
            signature = signature or (isinstance(value.get("signature"), str) and value["signature"] != "")
            value = value.get("text", value.get("summary", value.get("content")))
        found, signed = _block_shape(value) if isinstance(value, list) else (None, False)
        signature = signature or signed
        if shape is None and (isinstance(value, str) or found):
            shape = key
    return shape, signature
