---
name: commit
description: Use when the user asks to commit changes: stages only the related files by name, then commits with a clear message.
---

# Commit changes

Commit only what the user asked for, in one reviewable commit. Every `git add` and `git commit` is a command with its approval card; never work around a declined card.

## 1. Look first
- The Git status the app sent with the user's message gives the branch and change counts. Then run `git status --short`, `git diff --stat` and `git diff --cached --stat` (read-only) in one response.
- Read the diff of the files you plan to commit (`git diff -- <path>`, `git diff --cached -- <path>`), so the message describes what really changed.
- Run `git log --oneline -n 10` to see the repository's message style.

## 2. Stage only related files
- Name each file: `git add -- path/one path/two`. Never `git add -A`, `git add .`, `git commit -a` or `-f`.
- Never stage credential files (`.env`, keys, tokens), build output, or changes unrelated to the request. Leave those alone and mention them in your report.
- If something is already staged that does not belong in this commit, ask with `ask_user` before unstaging it.

## 3. Write the message
- Follow the style `git log` shows. Without one, use Conventional Commits: `type(scope): summary` (feat, fix, docs, refactor, test, chore), in the imperative, at most 72 characters.
- Add a body when the why is not obvious: `git commit -m "<summary>" -m "<body>"`, wrapped at about 72 characters.
- Never put secrets, tokens or personal data in the message.

## 4. Commit
- Run `git commit -m ...` with run_command. Never add `--no-verify`, `--amend` (unless the user asked) or `--no-gpg-sign`, and never change Git configuration.
- If a hook fails, read its output, fix the cause, stage the fix and run the commit again as a new commit.
- Do not push. Pushing and pull requests are a separate request (the pr skill).
- In Plan mode nothing is committed: describe the files and the message you would use.

## Report
The commit's hash and subject (`git log --oneline -n 1`), the files it contains, and what you left uncommitted and why.
