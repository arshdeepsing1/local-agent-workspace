"""Correlate a conversation's Usage rows with its saved history, as rows or CSV.

Each Usage row is one inference-ledger record (or a legacy request recorded
only on an assistant event). Agent calls name the assistant reply they
produced (event_id); the tool events that follow that reply are the tools it
called. The app tags each request with its ledger id, so a row can also be
joined to Databricks' system.ai_gateway.usage table through request_tags.
Standard library only, so scripts can use it without the web app installed.
"""
import csv
import io
import json
from datetime import datetime

from .telemetry import session_metrics


COLUMNS = (
    "n", "started_local", "started_utc", "finished_local", "seconds", "purpose", "model", "status",
    "http_status", "finish_reason", "error_kind", "attempt", "max_output_tokens", "estimated_input_tokens",
    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens",
    "reasoning_tokens", "estimated_dbu", "call_id", "conversation_id", "event_id", "response_id",
    "tools_called", "reply_excerpt",
)
EXCERPT_CHARS = 200


def _moment(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value).astimezone()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
        except ValueError:
            return None
    return None


def session_from_jsonl(path):
    """Read the parts of a conversation JSONL file that usage rows need."""
    session = {"events": [], "inference_calls": []}
    with open(path, encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            kind, data = record.get("record"), record.get("data")
            if kind == "session" and isinstance(data, dict):
                session.update({key: value for key, value in data.items() if key not in session})
            elif kind == "event":
                session["events"].append(data)
            elif kind == "inference_call":
                session["inference_calls"].append(data)
            elif kind == "field" and record.get("name") == "inference_calls" and data:
                session["inference_calls"].extend(data)
    return session


def usage_rows(session, redact=lambda text: text):
    raw = {call.get("id"): call for call in session.get("inference_calls", []) if isinstance(call, dict)}
    events = [event for event in session.get("events", []) if isinstance(event, dict)]
    position = {event.get("id"): index for index, event in enumerate(events)}
    rows = []
    for number, call in enumerate(session_metrics(session)["calls"], 1):
        record = raw.get(call["id"], {})
        event_id = record.get("event_id") or (call["id"][len("legacy-"):] if call.get("legacy") else "")
        started, finished = _moment(record.get("created", call["created"])), _moment(record.get("finished"))
        usage = call.get("usage", {})
        tools, excerpt = [], ""
        if event_id in position:
            index = position[event_id]
            excerpt = " ".join(str(events[index].get("text") or "").split())[:EXCERPT_CHARS]
            for event in events[index + 1:]:
                if event.get("type") != "tool":
                    break
                tools.append(str(event.get("name") or "tool"))
        rows.append({
            "n": number,
            "started_local": started.strftime("%Y-%m-%d %H:%M:%S %Z") if started else "",
            "started_utc": call["created"],
            "finished_local": finished.strftime("%Y-%m-%d %H:%M:%S %Z") if finished else "",
            "seconds": round((finished - started).total_seconds(), 1) if started and finished else "",
            "purpose": call["purpose"], "model": call["model"], "status": call["status"],
            "http_status": call.get("http_status", ""), "finish_reason": call.get("finish_reason", ""),
            "error_kind": call.get("error_kind", ""), "attempt": call.get("attempt", ""),
            "max_output_tokens": call.get("max_output_tokens", ""),
            "estimated_input_tokens": record.get("estimated_input_tokens", ""),
            **{key: usage.get(key, "") for key in ("input_tokens", "cache_read_input_tokens",
                                                  "cache_creation_input_tokens", "output_tokens", "reasoning_tokens")},
            "estimated_dbu": "" if call.get("estimated_dbu") is None else call["estimated_dbu"],
            "call_id": "" if call.get("legacy") else call["id"], "conversation_id": session.get("id", ""),
            "event_id": event_id, "response_id": record.get("response_id", ""),
            "tools_called": ", ".join(tools), "reply_excerpt": redact(excerpt),
        })
    return rows


def _cell(value):
    # Spreadsheet apps run text that starts with these characters as a formula.
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def usage_csv(rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows({key: _cell(row.get(key, "")) for key in COLUMNS} for row in rows)
    return output.getvalue()
