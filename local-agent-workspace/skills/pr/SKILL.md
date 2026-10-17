---
name: pr
description: Use when the user asks to open a pull request: pushes the branch and runs gh pr create; never merges.
---

# Open a pull request

The user asked for a pull request, so pushing this branch is part of the job; every push and `gh` command still goes through its approval card. Never merge, close or approve a pull request, and never turn on auto-merge, unless the user asks for that in this conversation.

## 1. Check the tools
- Run `gh --version` and `gh auth status`. If gh is missing or not signed in, stop and tell the user to install it or run `gh auth login` in their own terminal; never run the login yourself.

## 2. Get the branch ready
- Find the base: the branch the user named, else `gh repo view --json defaultBranchRef -q .defaultBranchRef.name`.
- On the base branch itself, create a branch first: `git switch -c <type>/<short-topic>`.
- Uncommitted changes that belong to the request: commit them as the commit skill describes (only related files, by name). Ask with `ask_user` when it is unclear what belongs.
- See what the pull request will contain: `git log --oneline <base>..HEAD` and `git diff --stat <base>...HEAD`.
- If `gh pr view` already shows a pull request for this branch, push and report it instead of creating another.

## 3. Push
- `git push -u origin HEAD`. Never `--force`; use `--force-with-lease` only when the user asks, after saying what it replaces.

## 4. Create it
- `gh pr create --base <base> --title "<title>" --body "<body>"`, adding `--draft` when the user asks or the work is unfinished.
- Title: what the change does, at most 72 characters, in the repository's style.
- Body, in Markdown: **Summary** (what and why, two or three sentences), **Changes** (short bullets), **Testing** (the commands you ran and their results, or what was not tested). Link an issue (`Closes #N`) only when the user named it.
- Never put secrets, tokens or personal data in the title or body.

## Report
The pull request's URL from gh's output, its base and head branches, and anything left to do (failing checks, reviewers to add). The Changes view shows the pull request's status.
