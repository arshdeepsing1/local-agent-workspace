---
name: debug
description: Use when the user reports a bug, an error or a failing test to fix. Reproduces it, tests one hypothesis at a time, fixes minimally, verifies.
---

# Debug a problem

Work from evidence, one step at a time. A fix you cannot explain is a guess.

## 1. Reproduce
- Get the exact failing command, input or steps from the user's message or the project's tests. Run it and keep the exact error text and stack trace.
- If it does not fail for you, say so and ask for what differs (version, data, environment) with `ask_user`, instead of fixing blind.

## 2. Locate
- Start from the deepest frame of the stack trace that is in this project. Read that code with `read_file`; use `search_files` for the error message, the failing names and their callers, and `outline_file` for large files.
- Check recent changes to those files (`git log --oneline -n 20`, `git diff`) when the bug is new.

## 3. Hypothesize and test
- Write down one hypothesis: what is wrong, where, and what observation would confirm or refute it.
- Test it cheaply: a focused test, a one-off command, or a temporary print you remove afterwards. Change one thing at a time; if the result refutes the hypothesis, say so and form the next one.

## 4. Fix minimally
- Fix the cause, not the symptom: no broad try/except, no silenced warnings, no skipped or loosened tests.
- Keep the change as small as the cause allows and match the surrounding code's style. Add a regression test that fails without the fix when the project has tests.

## 5. Verify
- Re-run the reproduction, then the tests around the change (see the verify skill), and remove anything temporary you added.

## Report
The root cause in one or two sentences with `path:line`, the fix, how you verified it (commands and results), and anything still uncertain or related that you noticed but did not change.
