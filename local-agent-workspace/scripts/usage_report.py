#!/usr/bin/env python3
"""Correlate a conversation's Usage rows with its JSONL history.

Usage (from the local-agent-workspace folder; needs only Python 3):
    python3 scripts/usage_report.py <state dir>/conversations/<conversation-id>.jsonl [--csv usage.csv]
    python3 scripts/usage_report.py --all <state dir> [--days 30] [--json]

Prints one line per model call (the rows of Agent tools -> Usage, oldest first)
with the reply it produced and the tools that reply called, and optionally
writes the same CSV as the Usage panel's Export CSV button. Copy the JSONL
first if the app is running; the app rewrites it on every save.

--all sums every conversation in the state folder (default .local) as
Agent tools -> Usage -> All chats does: the last --days local days (1-90) by
day, endpoint, purpose and project, from each conversation's own ledger.
--json prints the same object as GET /api/usage.

The call_id and conversation_id columns are the values the app sends in the
Databricks-Ai-Gateway-Request-Tags header, so where Databricks records
system.ai_gateway.usage you can join exactly:
    SELECT * FROM system.ai_gateway.usage
    WHERE request_tags['local_agent_conversation'] = '<conversation id>'
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from local_agent.telemetry import usage_summary  # noqa: E402
from local_agent.usage_export import session_from_jsonl, usage_csv, usage_rows  # noqa: E402
from local_agent.usage_overview import MAX_DAYS, overview  # noqa: E402


def report_all(state, days, as_json):
    """--all: every conversation of a state folder, as Agent tools -> Usage -> All chats shows it."""
    folder = Path(state).expanduser() / "conversations"
    if not folder.is_dir():
        print(f"No conversations folder in {state}.", file=sys.stderr)
        return 2
    chats, skipped = [], 0
    for path in sorted(folder.glob("*.jsonl")):
        if path.is_symlink():
            continue
        try:
            session = session_from_jsonl(path)
        except (OSError, ValueError):
            skipped += 1  # Unreadable or being rewritten: the app reports it.
            continue
        chats.append((session, usage_summary(session)))
    value = overview(chats, days)
    if as_json:
        print(json.dumps(value, indent=2))
        return 0
    totals = value["totals"]
    dbu = lambda row: f"~{row['estimated_dbu']:.3f} DBU" + (f" (+{row['unpriced_calls']} unpriced calls)" if row["unpriced_calls"] else "")
    tokens = lambda row: (f"{row['input_tokens']:,} uncached + {row['cache_read_input_tokens']:,} cache read + "
                          f"{row['cache_creation_input_tokens']:,} cache write in, {row['output_tokens']:,} out")
    print(f"{value['since']} to {value['until']}: {totals['chats']} chats, {totals['calls']:,} calls, {tokens(totals)}, {dbu(totals)}"
          + (f"; {skipped} unreadable files skipped" if skipped else ""))
    for title, rows, key in (("By day", [row for row in value["by_day"] if row["calls"]], "date"), ("By endpoint", value["by_model"], "key"),
                             ("By purpose", value["by_purpose"], "label"), ("By project", value["by_project"], "name")):
        print(f"\n{title}:")
        for row in rows:
            print(f"  {row[key]:<40} {row['calls']:>6} calls  {tokens(row)}  {dbu(row)}")
    print(f"\nList prices of {value['pricing']['effective_at']}: {value['pricing']['source_url']}")
    return 0


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0 if len(argv) > 1 else 2
    if argv[1] == "--all":
        days = int(argv[argv.index("--days") + 1]) if "--days" in argv else 30
        if not 1 <= days <= MAX_DAYS:
            print(f"--days must be from 1 to {MAX_DAYS}.", file=sys.stderr)
            return 2
        state = argv[2] if len(argv) > 2 and not argv[2].startswith("--") else ".local"
        return report_all(state, days, "--json" in argv)
    csv_path = argv[argv.index("--csv") + 1] if "--csv" in argv else None
    rows = usage_rows(session_from_jsonl(argv[1]))
    if csv_path:
        Path(csv_path).write_text(usage_csv(rows), encoding="utf-8")
        print(f"Wrote {len(rows)} rows to {csv_path}")
    dbu = lambda value: f"{value:.3f}" if isinstance(value, (int, float)) else ""
    for row in rows:
        what = row["purpose"]
        if row["reply_excerpt"] or row["tools_called"]:
            what = f"reply: {row['reply_excerpt'][:70]!r}" + (f" -> tools: {row['tools_called']}" if row["tools_called"] else "")
        print(f"{row['n']:>3} {row['started_local']} {row['purpose']:<10} {row['status']:<9} {row['http_status']!s:<4}"
              f" in={row['input_tokens']!s:>7} out={row['output_tokens']!s:>6} dbu={dbu(row['estimated_dbu']):>7}  {what}")
    number = lambda key: sum(row[key] for row in rows if isinstance(row[key], (int, float)))
    print(f"Total: {len(rows)} calls, {number('input_tokens'):,} input, {number('output_tokens'):,} output tokens, "
          f"~{number('estimated_dbu'):.1f} DBU (models with a known price only)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
