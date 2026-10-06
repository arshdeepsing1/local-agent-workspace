#!/usr/bin/env python3
"""Fail if tracked files contain shapes of personal data or credentials.

Run from anywhere inside the repository (Python 3 and Git only):
    python3 local-agent-workspace/scripts/check_personal_data.py

It looks for home-directory paths, e-mail addresses, Databricks workspace hosts and
Databricks tokens. Two fake credentials in tests are expected and allowed. This is a
safety net for a public repository, not a complete secret scanner.
"""
import subprocess
import sys

PATTERN = r"/Users/[a-z]|/home/[a-z]|@[a-z0-9-]+\.(com|org|net)\b|dbc-[a-z0-9]|dapi[0-9a-f]{8}"
# (path suffix, text) pairs that are deliberate test fixtures.
ALLOWED = [
    ("backend/tests/test_activity_log.py", "user:s3cret@github.com"),
    ("backend/tests/test_extensions.py", "user:secret@example.com"),
]


def main():
    root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip()
    result = subprocess.run(["git", "-C", root, "grep", "-n", "-I", "-i", "-E", PATTERN, "--", ".", ":!*/vendor/*"],
                            capture_output=True, text=True)
    if result.returncode not in (0, 1):
        print(result.stderr, file=sys.stderr)
        return 2
    hits = [line for line in result.stdout.splitlines()
            if not any(line.split(":", 1)[0].endswith(path) and text in line for path, text in ALLOWED)]
    for line in hits:
        print(line)
    if hits:
        print(f"{len(hits)} line(s) look like personal data or credentials. Replace them with generic examples.", file=sys.stderr)
        return 1
    print("No personal-data shapes found (the two known test fixtures are allowed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
