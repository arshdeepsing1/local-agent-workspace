"""Replace common secret shapes in text the app writes to files.

Standard library only, so offline scripts can use it without the web app installed.
"""
import re


# Commands are copied as they ran. Besides the app's configured credentials,
# replace values that match common secret shapes before writing them to a file.
SECRET_PATTERNS = (
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "[REDACTED]"),
    (re.compile(r"\bdapi[0-9a-f]{32}(?:-\d+)?\b"), "[REDACTED]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"(?i)(\b(?:bearer|basic|token)\s+)(?!\$)[A-Za-z0-9._~+/=-]{16,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b([a-z0-9_]*(?:password|passwd|secret|token|api[-_]?key|access[-_]?key)[a-z0-9_]*)"
                r"(\s*[=:]\s*)(['\"]?)(?!\$)[^\s'\"&;|]+\3"), r"\1\2\3[REDACTED]\3"),
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+@"), r"\1[REDACTED]@"),
)


def redact_secrets(text):
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
