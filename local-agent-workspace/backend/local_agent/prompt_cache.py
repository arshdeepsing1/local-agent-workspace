"""Prompt-cache breakpoints for agent requests to Databricks-hosted Claude endpoints. Pure: no I/O.

Databricks documents "cache_control": {"type": "ephemeral"} (a 5-minute cache entry) on content
parts in messages[].content and on assistant tool_calls items, accepted only by Databricks-hosted
Claude models. It does not document list content for role system or tool, a TTL field, a minimum
size or a breakpoint count (Anthropic allows 4). The markers go only into the encoded body of an
agent request (AgentManager._run_databricks); the payload that estimates, the ledger, the meter and
compaction read stays unmarked.

Levels, best first:
- "full": two breakpoints. The system message is split where its stable text ends (the folder
  grants, date and step notices follow), and the last message, a user message or a tool result,
  becomes one marked text part. Each request then writes the conversation up to its end, and the
  next request reads it back.
- "messages": only the documented forms. A last user message becomes a marked text part; after
  tool results, the last tool call of the assistant message that made them is marked.
- "off": no markers.
A request that Databricks rejects with HTTP 400 is sent again one level lower (AgentManager).
"""
CACHE_CONTROL = {"type": "ephemeral"}
LEVELS = ("full", "messages", "off")
REJECTION_TERMS = ("cache_control", "cache control", "prompt cache", "prompt caching", "ephemeral")


def _part(text, mark=False):
    return {"type": "text", "text": text, **({"cache_control": dict(CACHE_CONTROL)} if mark else {})}


def _marked_last_text(messages, roles):
    """True after the last message, when its role is in roles and its content is non-empty text, became one
    marked text part (in place on the copied list)."""
    last = messages[-1]
    if len(messages) > 1 and last.get("role") in roles and isinstance(last.get("content"), str) and last["content"]:
        messages[-1] = {**last, "content": [_part(last["content"], True)]}
        return True
    return False


def marked(payload, stable_chars, level):
    """A new payload with the cache markers of this level (at most 2). payload is never changed: only the
    messages that get a marker are copied. stable_chars is where the system message's stable text ends
    (AgentManager.context_inputs's guidance["cache_prefix_chars"])."""
    result = dict(payload)
    messages = payload.get("messages")
    if level not in ("full", "messages") or not isinstance(messages, list) or not messages:
        return result
    messages = list(messages)
    if level == "full":
        first = messages[0]
        content = first.get("content")
        if (first.get("role") == "system" and isinstance(content, str) and type(stable_chars) is int
                and stable_chars > 0 and content):
            stable, tail = content[:stable_chars], content[stable_chars:]
            messages[0] = {**first, "content": [_part(stable, True), *([_part(tail)] if tail else [])]}
        _marked_last_text(messages, ("user", "tool"))
    elif not _marked_last_text(messages, ("user",)) and messages[-1].get("role") == "tool":
        index = next((index for index in range(len(messages) - 1, -1, -1) if messages[index].get("role") == "assistant"
                      and isinstance(messages[index].get("tool_calls"), list) and messages[index]["tool_calls"]), None)
        if index is not None and isinstance(messages[index]["tool_calls"][-1], dict):
            calls = list(messages[index]["tool_calls"])
            calls[-1] = {**calls[-1], "cache_control": dict(CACHE_CONTROL)}
            messages[index] = {**messages[index], "tool_calls": calls}
    result["messages"] = messages
    return result


def markers(payload):
    """How many cache markers a payload carries."""
    count = 0
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        for item in (message.get("content") if isinstance(message.get("content"), list) else []) + (
                message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []):
            count += isinstance(item, dict) and "cache_control" in item
    return count


def unmarked(payload):
    """What marked() was given: the markers removed, and marked text parts joined back into strings.
    Tests and the offline simulator compare requests with it."""
    result = dict(payload)
    messages = []
    for message in payload.get("messages") or []:
        if isinstance(message, dict):
            content, calls = message.get("content"), message.get("tool_calls")
            if (isinstance(content, list) and any(isinstance(part, dict) and "cache_control" in part for part in content)
                    and all(isinstance(part, dict) and part.get("type") == "text" for part in content)):
                message = {**message, "content": "".join(part.get("text", "") for part in content)}
            if isinstance(calls, list) and any(isinstance(item, dict) and "cache_control" in item for item in calls):
                message = {**message, "tool_calls": [
                    {key: value for key, value in item.items() if key != "cache_control"} if isinstance(item, dict)
                    else item for item in calls]}
        messages.append(message)
    if "messages" in payload:
        result["messages"] = messages
    return result


def rejected(error_text):
    """True when an HTTP 400 body names prompt caching (only the notice's wording depends on it: Databricks
    may word a rejection of the markers any way)."""
    text = error_text.lower() if isinstance(error_text, str) else ""
    return any(term in text for term in REJECTION_TERMS)


def next_level(level):
    """The level below this one ("off" stays "off")."""
    return LEVELS[min(LEVELS.index(level) + 1, len(LEVELS) - 1)] if level in LEVELS else "off"
