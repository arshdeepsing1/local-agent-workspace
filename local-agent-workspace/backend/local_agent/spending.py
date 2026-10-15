"""A conversation's spending limit: a soft cap on the estimated DBU of a chat and its subagents.

The estimate is the usage panel's (telemetry.estimated_dbu: Databricks's list prices per million
tokens, from the usage each response reports). It is checked before each agent model request, so
one response can pass the limit before the next request is refused. Calls to an endpoint without
price data count as zero and are reported as unpriced. A subagent's requests count for its parent
chat, and the parent's limit applies to them.
"""
import math

from .telemetry import call_estimated_dbu

MAX_LIMIT_DBU = 100_000
WARN_FRACTION = 0.8  # One notice when the chat passes this share of its limit.


def validate_limit(value, label="Spending limit"):
    """A limit in DBU (0 = off) rounded to three decimals, or ValueError."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not 0 <= value <= MAX_LIMIT_DBU:
        raise ValueError(f"{label} must be a number of DBU from 0 (off) to {MAX_LIMIT_DBU:,}.")
    if 0 < value < 0.001:  # Rounded to 0 it would mean no limit.
        raise ValueError(f"{label} must be 0 (off) or at least 0.001 DBU.")
    return round(float(value), 3)


OWN_SPEND = "_own_spend"  # On a parent read from its header (root_of): its own spend, computed once.


def own_spend(session):
    """(estimated DBU, unpriced calls) of one conversation's own recorded model calls."""
    if OWN_SPEND in session:
        return session[OWN_SPEND]
    total, unpriced = 0.0, 0
    for call in session.get("inference_calls") or []:
        if not isinstance(call, dict):
            continue
        estimate, priced = call_estimated_dbu(call)
        if estimate is not None:
            total += estimate
        elif not priced:
            unpriced += 1
    return round(total, 6), unpriced


def root_of(manager, session):
    """The chat whose limit applies: a subagent's parent, else the chat itself (also a subagent chat
    whose parent is gone, such as a copy of one subagent chat). A parent that is not running is read
    from its header (one line) plus its own spend, cached until the parent is next saved with a new
    update time: a subagent chat the user continues checks every step without loading the parent."""
    parent_id = session.get("parent_session_id") if session.get("is_subagent") else None
    if not isinstance(parent_id, str):
        return session
    if parent_id in manager.live:
        return manager.live[parent_id]
    try:
        header = manager.store.header(parent_id)
        if not header:
            return session
        cache = manager.__dict__.setdefault("spending_own_cache", {})
        cached = cache.get(parent_id)
        if cached is None or cached[0] != header.get("updated"):
            full = manager.get(parent_id)
            if not full:
                return session
            cached = cache[parent_id] = (header.get("updated"), own_spend(full))
    except (ValueError, OSError):
        return session
    return {**header, OWN_SPEND: cached[1]}


def _limit(manager, root):
    value = root.get("spending_limit_dbu")
    source = "chat"
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        value, source = manager.settings.values.get("spending_limit_dbu", 0), "settings"
    try:
        value = validate_limit(value)
    except ValueError:
        value = 0
    return (value or None), source


def limit_for(manager, session):
    """(limit in DBU or None when off, source 'chat' | 'settings') for this chat or its subagent."""
    return _limit(manager, root_of(manager, session))


def _spend(manager, root, session):
    own, unpriced = own_spend(root)
    children = {}  # child id -> (DBU, unpriced calls); a live child's own ledger wins over its entry
    for entry in root.get("subagents") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") in manager.live:
            children[entry["id"]] = own_spend(manager.live[entry["id"]])
        elif isinstance(entry.get("estimated_dbu"), (int, float)) and not isinstance(entry.get("estimated_dbu"), bool):
            missing = entry.get("unpriced_calls")
            children[entry.get("id")] = (entry["estimated_dbu"], missing if type(missing) is int else 0)
    for entry in getattr(manager.delegates, "active", {}).get(root["id"], []):
        child_id = entry.get("child_session_id")
        if child_id and child_id not in children and child_id in manager.live:
            children[child_id] = own_spend(manager.live[child_id])
    if root is not session:
        children[session["id"]] = own_spend(session)
    dropped = root.get("subagents_dropped_dbu")  # Entries past the index's cap (planning._record).
    spent_children = sum(value for value, _ in children.values()) + (
        dropped if isinstance(dropped, (int, float)) and not isinstance(dropped, bool) else 0)
    unpriced += sum(missing for _, missing in children.values())
    return {"spent_dbu": round(own + spent_children, 6), "own_dbu": own, "subagents_dbu": round(spent_children, 6),
            "unpriced_calls": unpriced}


def chat_spend(manager, session):
    """{spent_dbu, own_dbu, subagents_dbu, unpriced_calls} of a chat (or a subagent's parent): its own
    calls and its subagents' — a running or continued one is read live, an ended one from its index
    entry (planning.index_entry keeps its estimate). Subagents that ended before the index kept
    estimates are not counted."""
    return _spend(manager, root_of(manager, session), session)


def status(manager, session):
    """The usage panel's view: the limit, its source, what the chat spent, the share used, and whether
    the chat follows its parent's limit."""
    root = root_of(manager, session)
    limit, source = _limit(manager, root)
    spend = _spend(manager, root, session)
    return {"limit_dbu": limit, "source": source, **spend, "follows_parent": root is not session,
            "percent": round(spend["spent_dbu"] * 100 / limit, 1) if limit else None}


def reached(manager, session):
    """Whether the chat has used its limit (no notice, nothing saved): a request now would be refused."""
    root = root_of(manager, session)
    limit, _ = _limit(manager, root)
    return bool(limit) and _spend(manager, root, session)["spent_dbu"] >= limit


def check(manager, session):
    """Before an agent model request: ('stop', text) once the limit is reached, ('warn', text) the
    first time the chat passes WARN_FRACTION of this limit, else (None, None)."""
    root = root_of(manager, session)
    limit, _ = _limit(manager, root)
    if not limit:
        return None, None
    spent = _spend(manager, root, session)["spent_dbu"]
    shown = f"about {spent:,.3f} of its {limit:,.3f} DBU spending limit (an estimate from list prices)"
    if spent >= limit:
        who = "Its parent conversation has used" if root is not session else "This conversation has used"
        return "stop", (f"{who} {shown}. The agent stops before its next model request until you raise the limit "
                        "in Agent tools → Usage (or the default in Settings → Agent); then send a message to "
                        "continue.")
    if root is session and spent >= limit * WARN_FRACTION and root.get("spending_warned") != limit:
        root["spending_warned"] = limit  # Saved with the notice; once per limit value.
        return "warn", f"This conversation has used {shown}."
    return None, None
