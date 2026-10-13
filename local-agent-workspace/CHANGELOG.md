# Changelog

Notable changes to Local Agent Workspace, newest first. Dates are UTC.

## Unreleased

Roadmap milestone M2 (agent quality):

- Search and reading: a new `find_files` tool (globs with `**` and `{a,b}`, sort by path or
  last change, paging). `search_files` honours `.gitignore` inside Git repositories by
  default (`include_ignored` turns it off), has output modes (matching lines, matching
  files, counts) and uses ripgrep when it is installed, with the same results without it.
  Case-insensitive literal search now uses simple case folding ("STRASSE" no longer finds
  "Straße"; regular expressions keep full folding), and results come in path order.
  `read_file` pages grow with the context window (8–32 KB), read up to 2,000 lines, can
  read the last lines of a file (`tail_lines`) and replace invalid UTF-8 with a note. Lines
  are split at LF only, so line numbers match editors.
- Commands: a failed or timed-out command shows the start and the end of its output;
  Settings → Command environment passes named variables to commands (credential and loader
  variables are refused); Stop and time limits send SIGTERM and wait 2 seconds before
  killing. A new `wait_job` tool waits for a background job to end or to print a line
  matching a pattern, and background jobs the agent started post a notice when they end.
- File edits: `edit_file` can replace every match (`replace_all`) and make several edits in
  one call (`edits`); edits keep a file's line endings (CRLF stays CRLF). Settings → File
  write limit (16–1,000 KB, 80 KB by default) governs new file content and the editor. New
  `move_path` and `delete_path` tools for single text files, with Undo, and
  `notebook_read`/`notebook_edit` for Jupyter notebooks. After an edit the app checks the
  file offline (Python syntax, JSON, TOML, notebooks, merge-conflict markers) and tells the
  agent only about problems the edit introduced. Diffs stay fast on large files.
- Agent loop: 502/503/504 responses and connection errors before a response starts are
  retried (up to 3 times, each recorded in Agent tools → Usage). After those retries, a
  rate limit is waited out (Retry-After up to 120 seconds, at most 2 extra waits; Stop
  cancels; Settings toggle, on by default). Invalid tool arguments or an incomplete reply
  get up to 2 hidden repair requests. Tool errors reach the agent as
  `{error_kind, message, hint}`. A per-project **Verify** command (Agent tools → Verify) runs
  after the agent changes files, through the normal approvals, for at most 3 rounds.
- Context: Settings → Custom instructions (up to 8,000 characters) apply to every chat. A
  short index of available skills (at most 2 KB) is part of the system prompt, and skills in
  `.claude/skills` are read (never written).
- Replies: `path:line` mentions become links that open the file at that line in Workspace →
  Files, which also offers **Open in VS Code**.
- Workspace → Changes shows Git's view as **Staged changes**, **Changes** and **Untracked**
  with counts and per-file diffs; generated folders are grouped and credential files never
  show a diff.
- Workspace → Files: **Add folder…** adds a folder to Files for the project (view only: the
  agent still needs Folder access), with change dots, and offers **Add <folder>** for changed
  files outside every folder. Every file the agent changed can be reviewed, kept or undone,
  also after leaving Bypass permissions or removing a folder grant.
- Safety: regular expressions whose counted repeats would expand too far are refused, and
  paths a Git index lists outside the searched folder are skipped.
- CI installs ripgrep on Ubuntu and macOS.

Roadmap milestone M1 (attention, approval quick wins, safety):

- Notifications: opt-in browser notifications when a chat needs approval, finishes, reaches the
  step limit or stops with an error; a "(n)" count in the tab title; optional desktop notifications
  from the server when no page is open (Settings). They show only a status and the chat title,
  with credentials redacted from a title made from the first message.
- Sidebar status: Needs approval, working, Error and unread markers, and a Running filter.
- Approvals: **Allow for this chat** (an exact command, edits, an MCP tool or a hook), **Decline
  with a note** that reaches the agent, and errors that name the busy chat with an Open button.
- Safety: Workspace → Changes runs Git without a shell, hooks, fsmonitor or filters; an unreadable
  conversation file is reported and hidden instead of stopping the app; a stalled browser tab is
  disconnected after 5 seconds; `write_file` refuses to overwrite a file the agent has not read
  (or that changed since), and the agent is told which files changed under it.
- Storage: "Allow for this chat" rules and the files the agent read are kept in
  `conversations/<id>.state.json`, so earlier versions can still open chats saved by this one.
- Conversation: a Continue button after a step limit, output limit, Stop or restart; a live task
  checklist and a `get_task` tool; message drafts saved across reloads; highlighted code blocks
  with Copy code; Markdown export and Copy conversation in Agent tools → Conversation.
- Stopping commands: on macOS, Workspace → Changes and command jobs no longer fail with "Operation
  not permitted" when the command has just exited. A command that does not end when stopped (for
  example one running as another user through sudo) no longer makes Stop or a time limit hang:
  after 5 seconds the job ends with a note that the command may still be running.
- Workspace → Files can browse folders granted to the chat, with change dots there too.
- The volatile parts of the system prompt (time, granted folders) now come last.
- Continuous integration: offline backend and UI tests, a lint check and a personal-data check on
  Ubuntu and macOS for every pull request and push to `main` (`.github/workflows/tests.yml`).
  `scripts/check_personal_data.py` runs the same personal-data check locally.
- `handoffs/` folders are ignored by Git, so automatic compaction handoffs written into a clone
  of this repository are not committed by mistake.

## 2026-10-06

- Approval wait: **Settings → Approval wait (minutes)**, 60 by default (1–1,440; 0 waits until
  answered). An unanswered card says "Not answered in time" and the agent is told nobody
  answered, not that you declined. Actions blocked without a card say "Not run:" and why.
- Review agent changes as in an editor's chat: a "files changed" bar above the message box with
  Keep and Undo, dots on changed files in Workspace → Files, and a red/green diff with Keep and
  Undo for each change. Undo is a normal file checkpoint and can be restored from Recovery.

## 2026-09-30

- One long request can outgrow the context budget: its earlier tool results are summarized and
  it continues. Read cards name the lines each page covered.
- Tools called without arguments run, and `/<skill-id>` anywhere in a message selects that skill.
- `scripts/conversation_transcript.py` turns saved conversations into Markdown transcripts.

## 2026-09-28

- `/` in the message box lists and inserts skills; the app's own skills are offered in every
  workspace.
- First public release: a no-build Preact frontend served by the Python backend (no Node.js),
  streamed chat with Databricks models, file and command tools with approvals and permission
  modes, background jobs, compaction, checkpoints, worktrees, MCP, skills, hooks, tasks and
  subagents.
