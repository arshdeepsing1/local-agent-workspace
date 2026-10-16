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


# The shapes that are almost never anything but a credential: applied to what commands and MCP tools
# return before the model sees it (redact_tokens). The generic name=value pattern above is left out
# there, because source code and logs match it far too often.
TOKEN_PATTERNS = (*SECRET_PATTERNS[:3],
                  (re.compile(r"(?i)(\bbearer\s+)(?!\$)[A-Za-z0-9._~+/=-]{20,}"), r"\1[REDACTED]"),
                  SECRET_PATTERNS[5])


def redact_secrets(text):
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_tokens(text):
    """text with GitHub, Databricks and AWS access-key shapes, long Bearer tokens and URL passwords replaced."""
    for pattern, replacement in TOKEN_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
