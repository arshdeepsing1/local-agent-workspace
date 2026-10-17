---
name: security-review
description: Use when the user asks for a security review of changes: traces untrusted input and reports exploitable issues with report_findings.
---

# Security review of changes

Read-only, like the review skill: no edits, commits or pushes, read-only commands only. Never run exploit code against real services or the network, and never write a secret's value in a finding (name the file and line).

## 1. Scope
- The change the user named, else the uncommitted changes (`git status --short`, `git diff HEAD`), a branch (`git diff <base>...HEAD`) or a pull request (`gh pr diff <number>`).
- Read each changed region with its context, and the code it calls or that calls it (read_file, search_files).

## 2. Trace untrusted input
For each input the change reads (request data, files, environment, command output, model or tool output, URLs), follow it to where it is used, and check:
- Injection: shell commands, SQL, file paths (traversal, symlinks), templates and HTML (XSS), regular expressions, deserialization.
- Access: missing or weakened authentication and authorization checks, permissions widened, defaults made less safe.
- Secrets: tokens, keys or passwords in code, logs, error messages or files written; credentials sent to the wrong host.
- Network: server-side requests to URLs the input controls, TLS checks turned off.
- Crypto and randomness: weak algorithms, predictable tokens, secrets compared without constant time.
- Files: unsafe temporary files, world-readable output, archive extraction outside the target.
- Dependencies: new packages or changed versions and where they come from.

## 3. Keep only real issues
- Each finding needs an exploit path: who controls the input, what they do, what they gain.
- Leave out theoretical issues without such a path, denial of service by resource use (unless trivial), unchanged code the change does not make reachable, and test-only code.
- Severity: high (exploitable by an outside or unprivileged user), medium (needs some access or an unusual configuration), low (defense in depth), info.

## Report
- Call report_findings once: a summary (what you checked and the overall risk) and the findings, most severe first, each with path, line, title, detail (the exploit path) and suggestion (the fix). With nothing found, call it with an empty list and say what you checked.
- Then reply with two or three sentences. If report_findings is not available, list the findings in your reply as `path:line` severity: title.
