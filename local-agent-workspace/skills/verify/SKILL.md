---
name: verify
description: Use when the user asks to verify, test or check a change. Runs the project's own tests, lint and build commands and reports the results.
---

# Verify a change

The goal is evidence: commands that ran, what they printed, and what that proves. Never report something as checked unless a command checked it.

## Find the commands
1. Look for the project's own commands in this order: AGENTS.md and the project guidance already in your context, then README* and CONTRIBUTING*, then the manifests (pyproject.toml, package.json scripts, Makefile, justfile, Cargo.toml, go.mod), then the CI workflows (.github/workflows/*.yml). Prefer the exact command CI runs.
2. Work out which files changed: `git status --short` and `git diff --stat` (read-only, also allowed in Plan and Auto).

## Run them, narrowest first
1. The tests closest to the change (one file or one test name), then the project's full test suite if that passes and is not very slow.
2. Lint, format check and type check, if the project has them.
3. The build, if the change could break it.
Run independent read-only checks in one response. For a long suite, use `run_command` with background=true and `wait_job` instead of a short timeout.

## Read failures properly
- Read the first failure in full (`get_job_output` from its offset), not only the summary line. Open the failing test and the code it calls before concluding anything.
- Say whether a failure comes from the change or was already there, from what the failing test checks and what the change touched. Do not stash, revert or reset the user's work to find out without asking.
- Never change a test, skip it or loosen an assertion just to make it pass, unless the user asks.

## Report
- Each command, then passed or failed with counts (for example "pytest -q: 212 passed, 1 failed").
- Each failure: `path:line`, the assertion or error text, and the likely cause.
- What was not verified and why (no tests for this area, a check needs network or credentials, a step was too slow).
