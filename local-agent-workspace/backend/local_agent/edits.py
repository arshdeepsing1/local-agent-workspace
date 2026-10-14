"""Exact-text edits and whole-file writes that keep a file's line endings.

Everything here is linear in the file size: edits replace raw spans found in the
LF view of the file, and whole-file writes keep the endings of the unchanged first
and last lines. Diffs trim the unchanged first and last lines and never let difflib
match a large changed middle directly.
"""
import bisect
import difflib
import re
from typing import NamedTuple

LINES = re.compile(r"[^\n]*\n|[^\n]+")
CONTEXT = 3
DIFF_PAIRS = 250_000  # Above this many line pairs, difflib's matching can turn quadratic.
MYERS_STEPS = 200_000  # Work allowed for Myers' diff on a large middle without unique lines.
RESULT_LIMIT = 8_000  # Bytes of a diff that go back to the model; the change review shows all of it.
NOT_FOUND = ("old_text was not found (after the earlier edits in this call). Read the file again and copy the "
             "text exactly; read_file line numbers are not part of the text.")


class Plan(NamedTuple):
    text: str  # The new file text, with the endings that will be written.
    prefix: str  # Put before the diff in the tool result ("" for a single plain edit or a write).


def split_lines(text):
    """Lines with their endings, split at "\\n" only (never at U+2028, form feeds or bare CR)."""
    return LINES.findall(text)


def majority_ending(text):
    """The file's most common line ending; CRLF wins a tie."""
    crlf = text.count("\r\n")
    return "\r\n" if crlf and crlf >= text.count("\n") - crlf else "\n"


def _plural(count, word):
    return f"{count} {word}" + ("" if count == 1 else "s")


def _edits(arguments):
    # Null fields, and a top-level replace_all=false next to edits, count as absent (schema defaults).
    single = arguments.get("old_text") is not None or arguments.get("new_text") is not None
    listed = arguments.get("edits") is not None
    if single and listed:
        raise ValueError("Use either old_text and new_text, or edits, not both.")
    if listed and arguments.get("replace_all") not in (None, False):
        raise ValueError("Put replace_all inside each edit when using edits.")
    if listed:
        edits = arguments["edits"]
        if not isinstance(edits, list) or not 1 <= len(edits) <= 50 or not all(isinstance(item, dict) for item in edits):
            raise ValueError("edits must be a list of 1 to 50 {old_text, new_text} objects.")
        return edits, True
    if arguments.get("old_text") is None or arguments.get("new_text") is None:
        raise ValueError("Give old_text and new_text, or edits.")
    return [arguments], False


def _replace_one(raw, old, new, replace_all, crlf):
    """Apply one replacement to the raw text; returns (text, replacements)."""
    if crlf:
        # Match in the LF view; unchanged regions keep their exact bytes.
        old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
        view = raw.replace("\r\n", "\n")
        # View positions of the "\n" of each CRLF pair, to map view offsets back to raw ones.
        pairs = [match.start() - index for index, match in enumerate(re.finditer("\r\n", raw))]
        new = new.replace("\n", majority_ending(raw))
    else:
        view, pairs = raw, []
    count = view.count(old)
    if count == 0:
        raise ValueError(NOT_FOUND)
    if count > 1 and not replace_all:
        raise ValueError(f"old_text matches {count} times; include more surrounding context so it matches once, "
                         "or set replace_all=true.")
    raw_offset = (lambda index: index + bisect.bisect_left(pairs, index)) if pairs else (lambda index: index)
    parts, position, start = [], 0, view.find(old)
    while start != -1:
        begin, end = raw_offset(start), raw_offset(start + len(old))
        parts += [raw[position:begin], new]
        position = end
        start = view.find(old, start + len(old)) if replace_all else -1
    parts.append(raw[position:])
    return "".join(parts), count if replace_all else 1


def apply_edits(before_text, arguments):
    """edit_file: replacements in order, each on the text the earlier ones produced."""
    edits, listed = _edits(arguments)
    crlf = "\r\n" in before_text
    text, replacements = before_text, 0
    for index, edit in enumerate(edits):
        label = f"edits[{index}]: " if listed else ""
        old, new, replace_all = edit.get("old_text"), edit.get("new_text"), edit.get("replace_all", False)
        try:
            if not isinstance(old, str) or not isinstance(new, str) or type(replace_all) is not bool:
                raise ValueError("old_text and new_text must be strings and replace_all a boolean.")
            if not old:
                raise ValueError("old_text cannot be empty.")
            text, count = _replace_one(text, old, new, replace_all, crlf)
        except ValueError as error:
            raise ValueError(label + str(error)) from None
        replacements += count
    plain = not listed and not arguments.get("replace_all")
    prefix = "" if plain else f"Applied {_plural(len(edits), 'edit')} ({_plural(replacements, 'replacement')}).\n\n"
    return text, prefix


def keep_endings(before_text, content):
    """write_file: give LF content the endings of the file it replaces."""
    if before_text is None or "\r\n" not in before_text or "\r" in content:
        return content
    if before_text.count("\n") == before_text.count("\r\n"):
        return content.replace("\n", "\r\n")
    # Mixed endings: unchanged first and last lines keep theirs; other lines get the majority.
    old, new = split_lines(before_text), split_lines(content)
    view = lambda line: line[:-2] + "\n" if line.endswith("\r\n") else line
    limit = min(len(old), len(new))
    prefix = 0
    while prefix < limit and view(old[prefix]) == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and view(old[-1 - suffix]) == new[-1 - suffix]:
        suffix += 1
    ending = majority_ending(before_text)
    middle = [line[:-1] + ending if line.endswith("\n") else line for line in new[prefix:len(new) - suffix]]
    return "".join([*old[:prefix], *middle, *old[len(old) - suffix:]])


def plan_change(name, arguments, before_text, limit):
    """The new text of a write_file or edit_file call. Raises ValueError without side effects."""
    if name == "edit_file":
        if before_text is None:
            raise ValueError("edit_file needs an existing file; use write_file to create it.")
        text, prefix = apply_edits(before_text, arguments)
    elif name == "write_file":
        content = arguments.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be a string.")
        text, prefix = keep_endings(before_text, content), ""
    else:
        raise ValueError("Only write_file and edit_file change file text.")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        size = None
    if size is not None and size > limit and name == "edit_file" and len(before_text.encode("utf-8")) > limit:
        raise ValueError(f"The file is {len(before_text.encode('utf-8')) // 1000:,} KB, over the file write limit "
                         f"({limit // 1000:,} KB, set in Settings). Edit it with run_command, or ask the user to raise "
                         "the limit.")
    if size is None or size > limit or "\x00" in text:
        raise ValueError(f"File content must be UTF-8 text without NUL bytes and at most {limit // 1000} KB.")
    return Plan(text, prefix)


def _no_newline(lines):
    for line in lines:
        yield line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"


def _anchors(old, new, alo, ahi, blo, bhi):
    """Lines that occur exactly once on each side, as the longest run of (i, j) pairs in order on both sides."""
    counts = {}
    for line in old[alo:ahi]:
        counts[line] = counts.get(line, 0) + 1
    unique_new = {}
    for j in range(blo, bhi):
        line = new[j]
        if counts.get(line) == 1:
            unique_new[line] = None if line in unique_new else j
    pairs = [(i, unique_new[old[i]]) for i in range(alo, ahi) if unique_new.get(old[i]) is not None]
    # Longest increasing run of j (patience sorting), O(k log k).
    tails, previous, ends = [], [], []
    for index, (_, j) in enumerate(pairs):
        place = bisect.bisect_left(tails, j)
        if place == len(tails):
            tails.append(j)
            ends.append(index)
        else:
            tails[place], ends[place] = j, index
        previous.append(ends[place - 1] if place else -1)
    chain, index = [], ends[-1] if ends else -1
    while index != -1:
        chain.append(pairs[index])
        index = previous[index]
    return chain[::-1]


def _myers(old, new, alo, ahi, blo, bhi):
    """Myers' O((N+M)D) diff as single-line opcodes, or None when it would take more than MYERS_STEPS."""
    n, m = ahi - alo, bhi - blo
    v, trace, steps = {1: 0}, [], 0
    for d in range(n + m + 1):
        trace.append(dict(v))
        steps += d + 1
        for k in range(-d, d + 1, 2):
            x = v[k + 1] if k == -d or k != d and v[k - 1] < v[k + 1] else v[k - 1] + 1
            y = x - k
            while x < n and y < m and old[alo + x] == new[blo + y]:
                x, y = x + 1, y + 1
                steps += 1
            v[k] = x
            if x >= n and y >= m:
                break
            if steps > MYERS_STEPS:
                return None
        else:
            continue
        break
    codes, x, y = [], n, m
    for d in range(len(trace) - 1, -1, -1):
        v, k = trace[d], x - y
        previous = k + 1 if k == -d or k != d and v[k - 1] < v[k + 1] else k - 1
        before_x = v[previous]
        before_y = before_x - previous
        while x > before_x and y > before_y:
            codes.append(("equal", alo + x - 1, alo + x, blo + y - 1, blo + y))
            x, y = x - 1, y - 1
        if d:
            codes.append(("insert" if x == before_x else "delete", alo + before_x, alo + x, blo + before_y, blo + y))
        x, y = before_x, before_y
    return codes[::-1]


def _difflib(old, new, alo, ahi, blo, bhi, out):
    matcher = difflib.SequenceMatcher(None, old[alo:ahi], new[blo:bhi], autojunk=False)
    out.extend((tag, i1 + alo, i2 + alo, j1 + blo, j2 + blo) for tag, i1, i2, j1, j2 in matcher.get_opcodes())


def _match(old, new, alo, ahi, blo, bhi, out, depth=0):
    if not depth and (ahi - alo) * (bhi - blo) <= DIFF_PAIRS:
        _difflib(old, new, alo, ahi, blo, bhi, out)  # Small: exactly what difflib gives.
        return
    while alo < ahi and blo < bhi and old[alo] == new[blo]:
        out.append(("equal", alo, alo + 1, blo, blo + 1))
        alo, blo = alo + 1, blo + 1
    tail = []
    while alo < ahi and blo < bhi and old[ahi - 1] == new[bhi - 1]:
        ahi, bhi = ahi - 1, bhi - 1
        tail.append(("equal", ahi, ahi + 1, bhi, bhi + 1))
    if alo == ahi or blo == bhi:
        if alo < ahi or blo < bhi:
            out.append(("delete" if blo == bhi else "insert", alo, ahi, blo, bhi))
    elif ahi - alo == 1 and bhi - blo == 1:
        out.append(("replace", alo, ahi, blo, bhi))  # Most gaps between anchors: one changed line.
    elif (ahi - alo) * (bhi - blo) <= DIFF_PAIRS:
        _difflib(old, new, alo, ahi, blo, bhi, out)
    elif depth < 8 and (anchors := _anchors(old, new, alo, ahi, blo, bhi)):
        for i, j in anchors:
            _match(old, new, alo, i, blo, j, out, depth + 1)
            out.append(("equal", i, i + 1, j, j + 1))
            alo, blo = i + 1, j + 1
        _match(old, new, alo, ahi, blo, bhi, out, depth + 1)
    elif (not set(old[alo:ahi]).isdisjoint(new[blo:bhi])
          and (codes := _myers(old, new, alo, ahi, blo, bhi)) is not None):
        out.extend(codes)
    elif ahi - alo == bhi - blo:
        # Many changes and nothing to anchor on, with the same number of lines: compare them in place.
        out.extend(("equal" if old[alo + k] == new[blo + k] else "replace", alo + k, alo + k + 1, blo + k, blo + k + 1)
                   for k in range(ahi - alo))
    else:
        out.append(("replace", alo, ahi, blo, bhi))
    out.extend(reversed(tail))


def line_opcodes(old, new):
    """difflib-style opcodes for two lists of lines, without difflib's quadratic worst cases.

    Unchanged first and last lines are trimmed; a small changed middle goes to difflib,
    a large one is first split at lines that occur once on each side (patience diff).
    """
    pieces = []
    _match(old, new, 0, len(old), 0, len(new), pieces)
    codes = []
    for tag, i1, i2, j1, j2 in pieces:
        if codes and (codes[-1][0] == "equal") == (tag == "equal"):
            _, k1, _, l1, _ = codes.pop()
            i1, j1 = k1, l1
            if tag != "equal":
                tag = "replace" if i1 < i2 and j1 < j2 else "delete" if i1 < i2 else "insert"
        codes.append((tag, i1, i2, j1, j2))
    return codes


def _grouped(codes, context):
    """difflib.SequenceMatcher.get_grouped_opcodes for precomputed opcodes."""
    codes = list(codes) or [("equal", 0, 1, 0, 1)]
    if codes[0][0] == "equal":
        tag, i1, i2, j1, j2 = codes[0]
        codes[0] = tag, max(i1, i2 - context), i2, max(j1, j2 - context), j2
    if codes[-1][0] == "equal":
        tag, i1, i2, j1, j2 = codes[-1]
        codes[-1] = tag, i1, min(i2, i1 + context), j1, min(j2, j1 + context)
    group = []
    for tag, i1, i2, j1, j2 in codes:
        if tag == "equal" and i2 - i1 > 2 * context:
            group.append((tag, i1, min(i2, i1 + context), j1, min(j2, j1 + context)))
            yield group
            group = []
            i1, j1 = max(i1, i2 - context), max(j1, j2 - context)
        group.append((tag, i1, i2, j1, j2))
    if group and not (len(group) == 1 and group[0][0] == "equal"):
        yield group


def _range(start, stop):
    length = stop - start
    if length == 1:
        return str(start + 1)
    return f"{start + 1 if length else start},{length}"


def unified_diff(old, new, fromfile, tofile=None, context=CONTEXT):
    """The lines of difflib.unified_diff(old, new, fromfile, tofile), using line_opcodes."""
    for number, group in enumerate(_grouped(line_opcodes(old, new), context)):
        if not number:
            yield f"--- {fromfile}\n"
            yield f"+++ {fromfile if tofile is None else tofile}\n"
        yield f"@@ -{_range(group[0][1], group[-1][2])} +{_range(group[0][3], group[-1][4])} @@\n"
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                yield from (" " + line for line in old[i1:i2])
                continue
            yield from ("-" + line for line in old[i1:i2])
            yield from ("+" + line for line in new[j1:j2])


def preview_diff(display_path, before_text, after_text):
    """A unified diff of the LF views."""
    before, after = before_text or "", after_text or ""
    old_view, new_view = before.replace("\r\n", "\n"), after.replace("\r\n", "\n")
    if old_view == new_view:
        return "No changes." if before == after else "Only line endings change."
    return "".join(_no_newline(unified_diff(split_lines(old_view), split_lines(new_view), display_path)))


def clip_result(text, limit=RESULT_LIMIT):
    """A tool result for the model: at most about limit bytes of it, cut at a line end."""
    if len(text.encode("utf-8")) <= limit:
        return text
    kept, size = [], 0
    lines = split_lines(text)
    for line in lines:
        size += len(line.encode("utf-8"))
        if size > limit:
            break
        kept.append(line)
    rest = len(lines) - len(kept)
    return "".join(kept) + f"… diff truncated ({_plural(rest, 'more line')}); the change review shows it in full."
