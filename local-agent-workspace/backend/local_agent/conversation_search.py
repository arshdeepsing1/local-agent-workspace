"""Bounded on-demand search over saved conversations: titles and user and assistant text.

There is no index and no model-callable tool. Each search reads conversation files newest
first and stops after MAX_BYTES of files or MAX_SECONDS, reporting that it stopped early.
Snippets are redacted (the configured token and common secret shapes) before they leave.
"""
import re
import time

from .redaction import redact_secrets

MAX_BYTES = 64 * 1024 * 1024
MAX_SECONDS = 3.0
SNIPPET_CHARS = 160
MATCHES_PER_CHAT = 3
_REDACT_WINDOW = 1000  # Characters on each side of a hit that are redacted before the snippet is cut.


def _contains_all(text, words):
    folded = text.casefold()
    return all(word in folded for word in words)


def snippet(text, words, redact=lambda value: value):
    """About SNIPPET_CHARS characters around the first hit, whitespace collapsed and redacted."""
    pattern = re.compile("|".join(re.escape(word) for word in words), re.IGNORECASE)
    flat = " ".join(text.split())
    found = pattern.search(flat)
    position = found.start() if found else 0
    start, end = max(0, position - _REDACT_WINDOW), position + _REDACT_WINDOW
    # Redact a wider window first, so a secret cut by the snippet's edge is still recognised.
    window = redact(redact_secrets(flat[start:end]))
    found = pattern.search(window)
    local = found.start() if found else min(position - start, len(window))
    begin = max(0, min(local - SNIPPET_CHARS // 3, len(window) - SNIPPET_CHARS))
    before = "…" if start + begin > 0 else ""
    after = "…" if begin + SNIPPET_CHARS < len(window) or end < len(flat) else ""
    return before + window[begin:begin + SNIPPET_CHARS] + after


def _in_workspace(header, workspace):
    worktree = header.get("worktree")
    source = worktree.get("source_workspace") if isinstance(worktree, dict) else None
    return header.get("workspace") == workspace or source == workspace


def search(store, query, *, workspace=None, include_subagents=False, limit=20, redact=lambda value: value,
           max_bytes=None, max_seconds=None, clock=time.monotonic):
    """{'results': [{id, title, updated, workspace, archived, matches: [{event_id, role, snippet}]}],
    'truncated': bool}. Every word of the query must appear in the title or in one message."""
    words = [word.casefold() for word in query.split()]
    if not words:
        return {"results": [], "truncated": False}
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    max_seconds = MAX_SECONDS if max_seconds is None else max_seconds
    started, read, results, truncated = clock(), 0, [], False
    for header in store.list():  # Newest first; unreadable files are already left out.
        if len(results) >= limit:
            break
        if not include_subagents and header.get("is_subagent"):
            continue
        if workspace is not None and not _in_workspace(header, workspace):
            continue
        if read >= max_bytes or clock() - started > max_seconds:
            truncated = True
            break
        try:
            read += (store.conversations / f"{header['id']}.jsonl").stat().st_size
        except OSError:
            continue
        title = header.get("title") if isinstance(header.get("title"), str) else ""
        matches = []
        for event_id, role, text in store.event_texts(header["id"]):
            if _contains_all(text, words):
                matches.append({"event_id": event_id, "role": role, "snippet": snippet(text, words, redact)})
                if len(matches) >= MATCHES_PER_CHAT:
                    break
            if clock() - started > max_seconds:
                truncated = True
                break
        if matches or _contains_all(title, words):
            results.append({"id": header["id"], "title": title, "updated": header.get("updated"),
                            "workspace": header.get("workspace"), "archived": header.get("archived") is True,
                            "matches": matches})
        if truncated:
            break
    return {"results": results, "truncated": truncated}
