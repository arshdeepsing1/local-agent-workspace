---
name: review
description: Use when the user asks to review changes, a branch or a pull request: reads the code and reports findings with report_findings.
---

# Review changes

A review is read-only: do not edit files, stage, commit or push. Run only read-only commands.

## 1. Scope
- What the user named. Otherwise the uncommitted changes: `git status --short`, `git diff HEAD --stat`, then `git diff HEAD -- <path>` per file, and new untracked files with read_file.
- A branch: `git diff <base>...HEAD`. A pull request: `gh pr view <number>` and `gh pr diff <number>`.
- A large change: review the riskiest files first and say what you did not cover.

## 2. Read the code, not only the diff
- For each changed file, read the changed regions with enough context (read_file with start_line) to know what the code does.
- When a change alters a function, a type or a file format, find its callers and tests (search_files) and check they still fit.
- Hand very broad reading to an explore subagent with delegate_task, asking for path:line evidence.

## 3. Look for, in this order
1. Correctness: wrong logic, edge cases (empty, None, zero, limits, Unicode), off-by-one, error handling that hides failures, races, leaks.
2. Security: untrusted input reaching commands, SQL, paths, HTML or URLs; missing checks; secrets in code or logs.
3. Regressions: callers, tests, documentation or configuration the change breaks.
4. Missing tests for new behavior, then maintainability that will cause bugs. Skip style nits unless asked.

## 4. Verify every finding
- Point at the exact line and give a concrete input or sequence that goes wrong. No speculation: when unsure, use severity info and say what would confirm it.
- Severity: high (data loss, security hole, crash or wrong result on a common path), medium (a bug on a less common path), low (minor), info (questions, observations).

## Report
- Call report_findings once: a summary (what you reviewed, the verdict) and the findings, most severe first, at most 30. Each: path relative to the project, line (end_line for a range), title, detail (what is wrong and why) and suggestion (the fix). With nothing found, call it with an empty list and say what you checked.
- Then reply with two or three sentences; do not repeat the list.
- If report_findings is not available, list the findings in your reply as `path:line` severity: title, then the detail.
