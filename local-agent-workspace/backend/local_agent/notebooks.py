"""Jupyter notebooks as cells: a paged reader and one-cell edits that keep the file's format."""
import difflib
import hashlib
import json
import re
import stat

from .tools import MAX_WRITE_LIMIT, QUERY_OUTPUT_LIMIT, _integer, _output_size

NOTEBOOK_LIMIT = MAX_WRITE_LIMIT  # Before and after an edit, whatever the write limit setting.
READ_LIMIT = 16_000_000
SOURCE_CHARS = 4_000
SOURCE_PAGE_CHARS = 7_000  # One cell read from source_offset; shrunk to fit the 8 KB page.
MODES = ("replace", "insert", "delete", "clear_outputs")
CELL_TYPES = ("code", "markdown", "raw")
LINES = re.compile(r"[^\n]*\n|[^\n]+")


def _parse(text):
    try:
        notebook = json.loads(text)
    except ValueError as error:
        raise ValueError(f"Not a Jupyter notebook: invalid JSON ({error}).") from None
    if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
        raise ValueError("Not a Jupyter notebook: the file has no cells list.")
    if not all(isinstance(cell, dict) for cell in notebook["cells"]):
        raise ValueError("Not a Jupyter notebook: every cell must be an object.")
    return notebook


def _source(cell):
    source = cell.get("source", "")
    return "".join(item for item in source if isinstance(item, str)) if isinstance(source, list) else str(source or "")


def _outputs(cell):
    outputs = cell.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        return ""
    parts = []
    for output in outputs:
        if not isinstance(output, dict):
            parts.append("unknown")
            continue
        kind = str(output.get("output_type", "unknown"))
        data = output.get("data") if isinstance(output.get("data"), dict) else {}
        types = [key for key in data if key != "text/plain"] or list(data)
        if kind == "error":
            kind += f" ({output.get('ename', 'error')})"
        elif types and kind in ("display_data", "execute_result"):
            kind += f" ({', '.join(types[:3])})"
        parts.append(kind)
    shown = parts[:5] + ([f"{len(parts) - 5} more"] if len(parts) > 5 else [])
    return f"{len(outputs)} output{'s' if len(outputs) != 1 else ''}: " + ", ".join(shown)


def _language(notebook):
    metadata = notebook.get("metadata") if isinstance(notebook.get("metadata"), dict) else {}
    for key, field in (("language_info", "name"), ("kernelspec", "language")):
        value = metadata.get(key)
        if isinstance(value, dict) and isinstance(value.get(field), str):
            return value[field]
    return ""


def read_notebook(tools, path, offset=0, max_cells=20, source_offset=None):
    _integer(offset, "offset", 0)
    _integer(max_cells, "max_cells", 1, 100)
    if source_offset is not None:
        _integer(source_offset, "source_offset", 0)
    target = tools.path(path)
    info = target.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Not a Jupyter notebook: not a regular file.")
    if info.st_size > READ_LIMIT:
        raise ValueError("Not a Jupyter notebook: the file is larger than 16 MB.")
    with target.open("rb") as source:
        raw = source.read(READ_LIMIT + 1)
    if len(raw) > READ_LIMIT:
        raise ValueError("Not a Jupyter notebook: the file is larger than 16 MB.")
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise ValueError("Not a Jupyter notebook: the file is not UTF-8 text.") from None
    notebook = _parse(text)
    cells = notebook["cells"]
    result = {"path": tools.display_path(target),
              "nbformat": f"{notebook.get('nbformat', '?')}.{notebook.get('nbformat_minor', '?')}",
              "language": _language(notebook), "total_cells": len(cells), "cells": [], "next_offset": None}

    def entry(index, limit, start=0):
        cell = cells[index]
        item = {"index": index, **({"id": cell["id"]} if isinstance(cell.get("id"), str) else {}),
                "cell_type": cell.get("cell_type", "code")}
        source = _source(cell)
        item["source"] = source[start:start + limit]
        if start:
            item["source_offset"] = start
        if start + limit < len(source):
            # The rest of a long cell: notebook_read with offset=index, max_cells=1, source_offset=next_source_offset.
            item.update(source_truncated=True, source_chars=len(source), next_source_offset=start + limit)
        if item["cell_type"] == "code":
            item.update(execution_count=cell.get("execution_count"), outputs=_outputs(cell))
        return item

    def fits(item):
        return _output_size({**result, "cells": [*result["cells"], item], "next_offset": len(cells)}) <= QUERY_OUTPUT_LIMIT

    if source_offset is not None:
        # One cell, from a character offset in its source.
        if offset >= len(cells):
            raise ValueError(f"offset {offset} is out of range (the notebook has {len(cells)} cells).")
        limit = SOURCE_PAGE_CHARS
        item = entry(offset, limit, source_offset)
        while not fits(item) and limit:
            limit //= 2
            item = entry(offset, limit, source_offset)
        result["cells"] = [item]
        result["next_offset"] = offset + 1 if offset + 1 < len(cells) else None
        return result
    for index in range(offset, len(cells)):
        if len(result["cells"]) >= max_cells:
            result["next_offset"] = index
            break
        limit = SOURCE_CHARS
        item = entry(index, limit)
        if not fits(item) and result["cells"]:
            result["next_offset"] = index  # The next page starts with this cell.
            break
        while not fits(item) and limit:
            limit //= 2  # A page holds at least one cell: show less of its source.
            item = entry(index, limit)
        if not fits(item):
            raise ValueError("A notebook cell exceeds the 8 KB output limit.")
        result["cells"].append(item)
    return result


def _format(text, notebook):
    """The json.dumps settings that reproduce the original text, or None for Jupyter's own."""
    for indent in (1, 2, 4):
        for sort_keys in (True, False):
            for ensure_ascii in (False, True):
                options = {"indent": indent, "sort_keys": sort_keys, "ensure_ascii": ensure_ascii}
                dumped = json.dumps(notebook, **options)
                if text in (dumped, dumped + "\n"):
                    return options, text.endswith("\n")
    return None, True


def _size_error(size):
    return ValueError(f"The notebook is {size // 1000:,} KB; notebook_edit works on notebooks up to "
                      f"{NOTEBOOK_LIMIT // 1000:,} KB. Clear outputs with run_command "
                      "(jupyter nbconvert --clear-output --inplace <path>) first.")


def check_size(target):
    """Refuse a notebook over the edit limit with advice, before reading it."""
    try:
        size = target.stat().st_size
    except FileNotFoundError:
        return
    if size > NOTEBOOK_LIMIT:
        raise _size_error(size)


def _new_id(cells, seed):
    """An unused 8-hex-digit id derived from seed, so the approval card and the write agree."""
    used = {cell.get("id") for cell in cells}
    counter = 0
    while (value := hashlib.sha256(f"{counter}:{seed}".encode()).hexdigest()[:8]) in used:
        counter += 1
    return value


def _wants_ids(notebook, cells):
    major, minor = notebook.get("nbformat"), notebook.get("nbformat_minor")
    major = major if type(major) is int else 4
    minor = minor if type(minor) is int else 0
    return major > 4 or major == 4 and minor >= 5 or any("id" in cell for cell in cells)


def _set_type(cell, cell_type):
    cell["cell_type"] = cell_type
    if cell_type == "code":
        cell.setdefault("outputs", [])
        cell.setdefault("execution_count", None)
    else:
        cell.pop("outputs", None)
        cell.pop("execution_count", None)


def _clear(cell):
    had = bool(cell.get("outputs")) or cell.get("execution_count") is not None
    if cell.get("cell_type") == "code":
        cell["outputs"], cell["execution_count"] = [], None
    return had


def plan_edit(before_text, arguments):
    """Apply one notebook_edit to the notebook text: returns (after_text, summary for preview)."""
    if before_text is None:
        raise ValueError("path does not exist.")
    size = len(before_text.encode("utf-8"))
    if size > NOTEBOOK_LIMIT:
        raise _size_error(size)
    notebook = _parse(before_text)
    options, newline = _format(before_text, notebook)
    cells = notebook["cells"]
    mode = arguments.get("edit_mode", "replace")
    if mode not in MODES:
        raise ValueError("edit_mode must be replace, insert, delete or clear_outputs.")
    cell_id, index = arguments.get("cell_id"), arguments.get("index")
    source, cell_type = arguments.get("source"), arguments.get("cell_type")
    if cell_id is not None and index is not None:
        raise ValueError("Give either cell_id or index, not both.")
    if cell_type is not None and cell_type not in CELL_TYPES:
        raise ValueError("cell_type must be code, markdown or raw.")
    if source is not None and not isinstance(source, str):
        raise ValueError("source must be a string.")
    position = None
    if cell_id is not None:
        position = next((number for number, cell in enumerate(cells) if cell.get("id") == cell_id), None)
        if position is None:
            raise ValueError(f"Cell id '{cell_id}' was not found; call notebook_read to see cell ids.")
    elif index is not None:
        if type(index) is not int or index < 0 or index >= len(cells) + (mode == "insert"):
            raise ValueError(f"index {index} is out of range (the notebook has {len(cells)} cells).")
        position = index
    summary = {"mode": mode, "index": position, "old_source": "", "new_source": "", "cleared": False,
               "reformatted": options is None}
    if mode == "clear_outputs":
        targets = range(len(cells)) if position is None else [position]
        summary["cleared"] = sum(_clear(cells[number]) for number in targets)
        summary["cells"] = len(targets)
        if position is not None:
            summary.update(cell_type=cells[position].get("cell_type"), id=cells[position].get("id"))
    elif mode == "insert":
        if source is None:
            raise ValueError("insert needs source.")
        if position is None:
            position = len(cells)
        elif cell_id is not None:
            position += 1  # After the named cell.
        cell = {"cell_type": cell_type or "code", "metadata": {}, "source": LINES.findall(source)}
        if _wants_ids(notebook, cells):
            cell["id"] = _new_id(cells, f"{position}:{before_text}")
        _set_type(cell, cell["cell_type"])
        cells.insert(position, cell)
        summary.update(index=position, new_source=source, cell_type=cell["cell_type"], id=cell.get("id"))
    else:
        if position is None:
            raise ValueError(f"Give cell_id or index to {mode} a cell.")
        cell = cells[position]
        summary.update(old_source=_source(cell), cell_type=cell.get("cell_type"), id=cell.get("id"))
        if mode == "delete":
            summary["cleared"] = bool(cell.get("outputs"))
            del cells[position]
        else:
            if source is None:
                raise ValueError("replace needs source.")
            had_outputs = bool(cell.get("outputs"))
            if cell_type is not None and cell_type != cell.get("cell_type"):
                _set_type(cell, cell_type)
            if source != summary["old_source"]:
                cell["source"] = LINES.findall(source)
                if cell.get("cell_type") == "code":
                    summary["cleared"] = _clear(cell)
            if had_outputs and "outputs" not in cell:
                summary["cleared"] = True  # Changed away from code.
            summary.update(new_source=source, cell_type=cell.get("cell_type"))
    options = options or {"indent": 1, "sort_keys": True, "ensure_ascii": False}  # Jupyter's own format.
    after = json.dumps(notebook, **options) + ("\n" if newline else "")
    size = len(after.encode("utf-8"))
    if size > NOTEBOOK_LIMIT:
        raise _size_error(size)
    return after, summary


def preview(display_path, summary, done=False):
    """What an edit does, for the approval card, or (done) what it did, for the tool result."""
    mode = summary["mode"]
    if mode == "clear_outputs" and summary["index"] is None:
        return f"Notebook {display_path}: clear outputs of all {summary['cells']} cells"
    words = {"replace": "replace", "insert": "insert", "delete": "delete", "clear_outputs": "clear outputs of"}
    details = summary.get("cell_type") or "code"
    if summary.get("id"):
        details += f", id {summary['id']}"
    lines = [f"Notebook {display_path}: {words[mode]} cell {summary['index']} ({details})"]
    if mode != "clear_outputs":
        old, new = LINES.findall(summary["old_source"]), LINES.findall(summary["new_source"])
        old = [line if line.endswith("\n") else line + "\n" for line in old]
        new = [line if line.endswith("\n") else line + "\n" for line in new]
        diff = "".join(difflib.unified_diff(old, new, fromfile="cell source", tofile="cell source"))
        if diff:
            lines.append(diff.rstrip("\n"))
    if summary["cleared"] and mode in ("replace", "clear_outputs"):
        lines.append("Its outputs were cleared." if done else "Its outputs will be cleared.")
    if summary.get("reformatted"):
        lines.append(("The notebook's JSON was" if done else "The notebook's JSON will be") + " rewritten in Jupyter's"
                     " format (indent 1, sorted keys), so lines outside this cell may change too.")
    return "\n".join(lines)
