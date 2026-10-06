# Changelog

Notable changes to Local Agent Workspace, newest first. Dates are UTC.

## Unreleased

Roadmap milestone M1 (attention, approval quick wins, safety):

- Notifications: opt-in browser notifications when a chat needs approval, finishes or stops with
  an error; a "(n)" count in the tab title; optional desktop notifications from the server when no
  page is open (Settings).
- Sidebar status: Needs approval, working, Error and unread markers, and a Running filter.
- Approvals: **Allow for this chat** (an exact command, edits, an MCP tool or a hook), **Decline
  with a note** that reaches the agent, and errors that name the busy chat with an Open button.
- Safety: Workspace → Changes runs Git without a shell, hooks, fsmonitor or filters; an unreadable
  conversation file is reported and hidden instead of stopping the app; a stalled browser tab is
  disconnected after 5 seconds; `write_file` refuses to overwrite a file the agent has not read
  (or that changed since), and the agent is told which files changed under it.
- Conversation: a Continue button after a step limit, output limit, Stop or restart; a live task
  checklist and a `get_task` tool; message drafts saved across reloads; highlighted code blocks
  with Copy code; Markdown export and Copy conversation in Agent tools → Conversation.
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
