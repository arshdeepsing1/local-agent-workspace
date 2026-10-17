"""Usage across chats: the model calls of every conversation by day, endpoint, purpose and project, read from the
usage summary each conversation keeps in its header (telemetry.usage_summary), so no conversation body is loaded.
Tokens are those Databricks reported; the DBU estimate sums the calls with list prices and counts the others as
unpriced. Days are local calendar dates, as in the usage CSV. Standard library only (scripts/usage_report.py
--all uses it without the web app).
"""
from datetime import date, timedelta
from pathlib import PurePath

from .telemetry import (
    MODEL_DBU_PER_MILLION, OPEN_PRICING_SOURCE, PRICING_EFFECTIVE_AT, PRICING_SOURCE, PURPOSE_LABELS, TOKEN_FIELDS,
    summary_rows,
)

MAX_DAYS = 90
MAX_PROJECTS = 20  # The rest are summed into one "Other projects" row.
FIELDS = ("calls", *TOKEN_FIELDS, "estimated_dbu", "unpriced_calls")


def _empty():
    return {**{field: 0 for field in FIELDS}, "estimated_dbu": 0.0}


def _add(target, row):
    for field in FIELDS:
        target[field] += row[field]
    target["estimated_dbu"] = round(target["estimated_dbu"], 6)


def project_of(header):
    """The project a chat counts for: its worktree's source project, else its workspace."""
    worktree = header.get("worktree") if isinstance(header.get("worktree"), dict) else {}
    source = worktree.get("source_workspace")
    return source if isinstance(source, str) and source else str(header.get("workspace") or "")


def overview(chats, days=30, today=None):
    """{days, since, until, totals (with chats: those with calls in the window), by_day (every date of the window,
    oldest first), by_model, by_purpose, by_project (most DBU first, then most tokens), pricing} from
    [(header, usage summary)] pairs."""
    days = max(1, min(MAX_DAYS, int(days)))
    today = today or date.today()
    since = (today - timedelta(days=days - 1)).isoformat()
    until = today.isoformat()
    totals, by_day = {**_empty(), "chats": 0}, {(today - timedelta(days=offset)).isoformat(): _empty()
                                                   for offset in range(days - 1, -1, -1)}
    by_model, by_purpose, by_project = {}, {}, {}
    for header, summary in chats:
        counted = False
        project = project_of(header)
        for row in summary_rows(summary):
            if not since <= row["date"] <= until:
                continue
            counted = True
            _add(totals, row)
            _add(by_day[row["date"]], row)
            _add(by_model.setdefault(row["model"], _empty()), row)
            _add(by_purpose.setdefault(row["purpose"] if row["purpose"] in PURPOSE_LABELS else "agent", _empty()), row)
            _add(by_project.setdefault(project, _empty()), row)
        totals["chats"] += counted

    def ranked(groups):
        return sorted(groups.items(), key=lambda item: (-item[1]["estimated_dbu"], -sum(item[1][field] for field in TOKEN_FIELDS),
                                                        item[0]))
    projects = ranked(by_project)
    rest = _empty()
    for _, values in projects[MAX_PROJECTS:]:
        _add(rest, values)
    return {
        "days": days, "since": since, "until": until, "totals": totals,
        "by_day": [{"date": key, **values} for key, values in by_day.items()],
        "by_model": [{"key": key, "priced": key.lower() in MODEL_DBU_PER_MILLION, **values} for key, values in ranked(by_model)],
        "by_purpose": [{"key": key, "label": PURPOSE_LABELS[key], **values} for key, values in ranked(by_purpose)],
        "by_project": [{"key": key, "name": PurePath(key).name or key, **values} for key, values in projects[:MAX_PROJECTS]]
        + ([{"key": "", "name": f"{len(projects) - MAX_PROJECTS} other projects", **rest}]
           if len(projects) > MAX_PROJECTS else []),
        "pricing": {"effective_at": PRICING_EFFECTIVE_AT, "source_url": PRICING_SOURCE, "open_source_url": OPEN_PRICING_SOURCE,
                    "models": MODEL_DBU_PER_MILLION},
    }
