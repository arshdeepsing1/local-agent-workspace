#!/usr/bin/env python3
"""Correlate a conversation's Usage rows with its JSONL history.

Usage (from the local-agent-workspace folder; needs only Python 3):
    python3 scripts/usage_report.py <state dir>/conversations/<conversation-id>.jsonl [--csv usage.csv]

Prints one line per model call (the rows of Agent tools -> Usage, oldest first)
with the reply it produced and the tools that reply called, and optionally
writes the same CSV as the Usage panel's Export CSV button. Copy the JSONL
first if the app is running; the app rewrites it on every save.

The call_id and conversation_id columns are the values the app sends in the
Databricks-Ai-Gateway-Request-Tags header, so where Databricks records
system.ai_gateway.usage you can join exactly:
    SELECT * FROM system.ai_gateway.usage
    WHERE request_tags['local_agent_conversation'] = '<conversation id>'
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from local_agent.usage_export import session_from_jsonl, usage_csv, usage_rows  # noqa: E402


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0 if len(argv) > 1 else 2
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
