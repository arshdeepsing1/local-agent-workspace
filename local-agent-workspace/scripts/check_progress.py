#!/usr/bin/env python3
"""Check or rewrite the numbers in PROGRESS.md from its checkbox lists.

    python3 scripts/check_progress.py [--write] [--json] [--date YYYY-MM-DD] [FILE]

Without a flag it checks that the overall line, the table and every section status line match
the `- [x]` and `- [ ]` items, and exits 1 with one `line N: ...` message per problem. With
`--write` it rewrites only those numbers (never items, headings or prose). With `--json` it
prints the computed numbers. Exit codes: 0 ok, 1 problems, 2 unreadable file or bad usage. The file always
ends with exactly one newline after `--write`. Standard library only.
"""
import argparse
import datetime
import json
import re
import sys
from pathlib import Path

ITEM = re.compile(r"^- \[([ xX])\] (.*)$")
LOOKS_LIKE_ITEM = re.compile(r"^\s*[-*+]\s*\[.?\]")
STATUS = re.compile(r"^\*\*Implemented\*\*: (\d+)% \((\d+) of (\d+)\) · \*\*Share of app\*\*: (\d+)%\s*$")
OVERALL = re.compile(r"^\*\*Overall\*\*: (\d+)% \((\d+) of (\d+) items\) · \*\*Last updated\*\*: (\S*)\s*$")
HEADER = ["| Section | Share of app | Implemented | Progress |", "|---|---|---|---|"]


def half_up(num, den):
    """Round 100 * num / den half up, with integers only."""
    return (200 * num + den) // (2 * den) if den else 0


def bar(percent):
    filled = (percent + 5) // 10
    return "█" * filled + "░" * (10 - filled)


def is_date(text):
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        return False
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text))


def iso_date(text):
    if not is_date(text):
        raise argparse.ArgumentTypeError(f"'{text}' is not a date in YYYY-MM-DD form")
    return text


def parse(text):
    lines = text.rstrip("\n").split("\n")
    model = {"lines": lines, "title": None, "overall": None, "table": None, "sections": []}
    section, heading, fenced = None, None, False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif fenced:
            continue
        elif line.startswith("## "):
            section = {"title": line[3:].strip(), "line": i, "status": None, "extra": [], "items": [], "bad": []}
            model["sections"].append(section)
            heading = None
        elif section is None:
            if line.startswith("# ") and model["title"] is None:
                model["title"] = i
            elif line.startswith("**Overall**") and model["overall"] is None:
                model["overall"] = i
            elif line.startswith("|"):
                start, end = model["table"] or (i, i)
                model["table"] = (start, i + 1) if end == i else (start, end)
        elif line.startswith("### "):
            heading = line[4:].strip()
        elif line.startswith("**Implemented**"):
            if section["status"] is None:
                section["status"] = i
            else:
                section["extra"].append(i)
        elif m := ITEM.match(line):
            section["items"].append({"line": i, "done": m.group(1) != " ", "text": m.group(2), "heading": heading})
        elif LOOKS_LIKE_ITEM.match(line):
            section["bad"].append((i, line))
    return model


def compute(model):
    secs = model["sections"]
    total = sum(len(s["items"]) for s in secs)
    done = sum(i["done"] for s in secs for i in s["items"])
    floors = [100 * len(s["items"]) // total if total else 0 for s in secs]
    rems = [100 * len(s["items"]) % total if total else 0 for s in secs]
    order = sorted(range(len(secs)), key=lambda k: (-rems[k], k))
    for k in order[:100 - sum(floors)]:
        floors[k] += 1
    out = []
    for s, share in zip(secs, floors):
        d = sum(i["done"] for i in s["items"])
        out.append({"title": s["title"], "done": d, "total": len(s["items"]),
                    "percent": half_up(d, len(s["items"])), "share": share})
    return {"overall": {"done": done, "total": total, "percent": half_up(done, total)}, "sections": out}


def table_rows(calc):
    ranked = sorted(range(len(calc["sections"])), key=lambda k: (-calc["sections"][k]["total"], k))
    return [f"| {s['title']} | {s['share']}% | {s['percent']}% | {bar(s['percent'])} |"
            for s in (calc["sections"][k] for k in ranked)]


def overall_line(calc, date):
    o = calc["overall"]
    return f"**Overall**: {o['percent']}% ({o['done']} of {o['total']} items) · **Last updated**: {date}"


def status_line(s):
    return f"**Implemented**: {s['percent']}% ({s['done']} of {s['total']}) · **Share of app**: {s['share']}%"


def check(model):
    """Return (structural, message, line_number) tuples. `structural` problems cannot be fixed by --write."""
    found, lines, secs = [], model["lines"], model["sections"]
    if not secs:
        return [(True, "no sections (## headings) found", 1)]
    calc = compute(model)
    seen, titles = {}, {}
    for s, c in zip(secs, calc["sections"]):
        n = s["line"] + 1
        if s["title"] in titles:
            found.append((True, f"repeated section '{s['title']}' (first on line {titles[s['title']]})", n))
        titles.setdefault(s["title"], n)
        for i in s["extra"]:
            found.append((True, f"extra status line in section '{s['title']}'", i + 1))
        for i, line in s["bad"]:
            found.append((True, f"malformed item, expected '- [ ] text' or '- [x] text': {line.strip()}", i + 1))
        if not s["title"] or ord(s["title"][0]) < 128:
            found.append((True, f"section '{s['title']}' needs an emoji as its first character", n))
        if not s["items"]:
            found.append((True, f"section '{s['title']}' has no items", n))
        for it in s["items"]:
            key = it["text"].strip().lower()
            if key in seen:
                found.append((True, f"repeated item '{it['text'].strip()}' (first on line {seen[key]})", it["line"] + 1))
            seen.setdefault(key, it["line"] + 1)
            if it["heading"] not in ("Done", "Pending"):
                found.append((True, f"item outside Done and Pending: {it['text'].strip()}", it["line"] + 1))
            if it["done"] and it["heading"] == "Pending":
                found.append((True, f"checked item under Pending: {it['text'].strip()}", it["line"] + 1))
            if not it["done"] and it["heading"] == "Done":
                found.append((True, f"unchecked item under Done: {it['text'].strip()}", it["line"] + 1))
        if s["status"] is None:
            found.append((False, f"section '{s['title']}' has no status line", n))
            continue
        m = STATUS.match(lines[s["status"]])
        n = s["status"] + 1
        if not m:
            found.append((False, f"status line is malformed, expected '{status_line(c)}'", n))
            continue
        if tuple(map(int, m.groups()[:3])) != (c["percent"], c["done"], c["total"]):
            found.append((False, f"Implemented is {m[1]}% ({m[2]} of {m[3]}), expected "
                                 f"{c['percent']}% ({c['done']} of {c['total']})", n))
        if int(m[4]) != c["share"]:
            found.append((False, f"Share of app is {m[4]}%, expected {c['share']}%", n))
    o = calc["overall"]
    if model["overall"] is None:
        found.append((False, "overall line is missing", 1))
    else:
        n = model["overall"] + 1
        m = OVERALL.match(lines[model["overall"]])
        if not m:
            found.append((False, "overall line is malformed", n))
        else:
            if tuple(map(int, m.groups()[:3])) != (o["percent"], o["done"], o["total"]):
                found.append((False, f"Overall is {m[1]}% ({m[2]} of {m[3]} items), expected "
                                     f"{o['percent']}% ({o['done']} of {o['total']} items)", n))
            if not is_date(m[4]):
                found.append((False, f"Last updated '{m[4]}' is not in YYYY-MM-DD form", n))
    found += check_table(model, calc)
    return sorted(found, key=lambda p: p[2])


def check_table(model, calc):
    if model["table"] is None:
        return [(False, "table is missing", 1)]
    start, end = model["table"]
    lines, found = model["lines"], []
    if lines[start:start + 2] != HEADER:
        found.append((False, "table header is wrong, expected '" + HEADER[0] + "'", start + 1))
    expected = table_rows(calc)
    titles = [r.split("|")[1].strip() for r in expected]
    got = {}
    for i in range(start + 2, end):
        title = lines[i].strip().strip("|").split("|")[0].strip()
        if title in got:
            found.append((False, f"duplicate table row '{title}' (first on line {got[title] + 1})", i + 1))
        else:
            got[title] = i
    for row, title in zip(expected, titles):
        i = got.get(title)
        if i is None:
            found.append((False, f"table row for '{title}' is missing, expected '{row}'", start + 1))
        elif lines[i].strip() != row:
            found.append((False, f"table row is '{lines[i].strip()}', expected '{row}'", i + 1))
    for title, i in got.items():
        if title not in titles:
            found.append((False, f"extra table row '{title}'", i + 1))
    present = [t for t in sorted(got, key=got.get) if t in titles]
    if present != [t for t in titles if t in got]:
        found.append((False, "table order is wrong, expected item count descending: " + ", ".join(titles), start + 1))
    return found


def rewrite(text, date):
    model = parse(text)
    calc = compute(model)
    lines = list(model["lines"])
    edits = []  # (start, end, new lines); end > start replaces, end == start inserts
    for s, c in zip(model["sections"], calc["sections"]):
        if s["status"] is None:
            edits.append((s["line"] + 1, s["line"] + 1, ["", status_line(c)]))
        else:
            edits.append((s["status"], s["status"] + 1, [status_line(c)]))
    over, table = [overall_line(calc, date)], HEADER + table_rows(calc)
    t = model["title"]
    after = 0 if t is None else t + 1
    o, tb = model["overall"], model["table"]
    if o is not None:
        edits.append((o, o + 1, over))
    if tb is not None:
        edits.append((tb[0], tb[1], table))
    if o is None and tb is None:
        edits.append((after, after, ["", *over, "", *table]))
    elif o is None:
        edits.append((after, after, ["", *over]))
    elif tb is None:
        edits.append((o + 1, o + 1, ["", *table]))
    for start, end, new in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        if start == end:
            new = new + [""] if start >= len(lines) or lines[start].strip() else new
            if start == 0:
                new = new[1:] + [""] if new and new[0] == "" else new
        lines[start:end] = new
    return "\n".join(lines).rstrip("\n") + "\n"


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Check or rewrite the numbers in PROGRESS.md.")
    ap.add_argument("file", nargs="?", default=str(Path(__file__).resolve().parents[1] / "PROGRESS.md"))
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="rewrite the overall line, table and status lines")
    mode.add_argument("--json", action="store_true", help="print the computed numbers as JSON")
    ap.add_argument("--date", type=iso_date, default=datetime.date.today().isoformat(),
                    help="Last updated date for --write (YYYY-MM-DD)")
    args = ap.parse_args()
    path = Path(args.file)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"{path}: not found")
        return 2
    except (OSError, UnicodeDecodeError) as error:
        print(f"{path}: {error}")
        return 2
    model = parse(text)
    found = check(model)
    structural = [f"line {n}: {msg}" for s, msg, n in found if s]
    if args.json:
        if structural:
            print("\n".join(structural), file=sys.stderr)
            return 1
        print(json.dumps(compute(model), ensure_ascii=False))
        return 0
    if args.write:
        if structural:
            print("\n".join(structural))
            return 1
        old = OVERALL.match(model["lines"][model["overall"]]) if model["overall"] is not None else None
        keep = old[4] if old and is_date(old[4]) else args.date
        if rewrite(text, keep) == text.rstrip("\n") + "\n":
            print("PROGRESS.md: already up to date")
        else:
            path.write_text(rewrite(text, args.date), encoding="utf-8")
            print("PROGRESS.md: numbers updated")
        return 0
    if found:
        print("\n".join(f"line {n}: {msg}" for _, msg, n in found))
        return 1
    o = compute(model)["overall"]
    print(f"PROGRESS.md: {o['percent']}% ({o['done']} of {o['total']} items), {len(model['sections'])} sections, "
          "numbers match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
