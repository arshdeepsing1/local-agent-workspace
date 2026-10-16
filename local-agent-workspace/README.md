# Local workspace

A local browser coding assistant powered by Databricks model serving, with streamed
chat, local file tools, shell commands, and per-conversation permissions.
The application is self-contained in this folder, with a Python backend and a browser
frontend that runs without a build step.

## Features

- Streamed Markdown chat and multiple persistent conversations, with highlighted code
  blocks that each have a Copy code button.
- Unsent message drafts kept in the browser across reloads, and a Continue button after a
  turn stopped at the step or output limit, by Stop, or by a server restart.
- Databricks model selection, numbered line-range reads, paginated regex/literal
  search, file creation and exact edits, and shell commands with live output.
- Approval cards with proposed edits and command text, which you can edit before approving.
  A declined action does not run. Decline with a note the agent reads, or allow the action
  for the rest of a chat, for the project or for all projects.
- Permission rules (allow, Always ask and deny) for a chat, a project or all projects;
  protected files always ask.
- Per-conversation permissions below the composer: Manual, Auto, Accept edits, Don't ask,
  Plan and Bypass permissions. Access additional local folders through an approval card.
- Questions from the agent with options to choose from, and a plan review in Plan mode that
  approves the plan and switches the mode.
- Stop an agent response, including its foreground command or pending approval.
- Conversation status in the sidebar (needs approval, working, error, unread) with a
  Running filter, a "(n)" tab-title count of chats waiting for approval, opt-in browser
  notifications, and an optional desktop notification from the server when no page is open.
- Managed background jobs with explicit Stop, output retrieval, status/history,
  per-command time/output limits, and cleanup on normal server shutdown.
- Resume conversations after refreshing or restarting the app.
- Periodic partial-reply checkpoints, safe JSON conversation export/import, and
  completed-conversation forks with fresh permissions.
- Configurable context budgeting, automatic summaries of older turns (and of a single long
  request's earlier tool results), and a visible context meter. Full conversation history
  stays on disk, and a script turns it into Markdown transcripts.
- Automatic workspace-scoped `AGENTS.md` and `CLAUDE.md` guidance loading.
- Workspace file explorer (the project, the conversation's granted folders and folders you add), text
  editor with stale-file detection, Git status/diff, and a non-interactive command runner.
- File checkpoints with reviewed, reversible restore and managed Git worktrees.
- Review of the agent's file edits, as in an editor's chat: a "files changed" bar with
  Keep and Undo, dots on changed files in the file explorer, and an inline red/green
  diff with Keep and Undo for each change.
- The agent must read an existing file before overwriting it, and is told which files it
  read or wrote changed since (your saves, an Undo, a command).
- An unreadable conversation file does not stop the app: it is skipped, left unchanged,
  and named in a notice.
- Explicit MCP servers, workspace skills with a `/` command menu, and approval-controlled
  hooks around tool calls, your messages and the end of a turn, which can answer in JSON.
- Persistent tasks with dependencies, shown as a live checklist above the message box,
  and subagents the model starts on its own (explore, general, editor, or a custom agent
  defined in the project's `.agents/agents` files; several in parallel), each in its own
  conversation file linked from its parent's, with a model and effort per agent type.
- Markdown transcripts of a conversation: download or copy one from the app, or write
  them for many saved conversations with a script.
- A reasoning effort per conversation (Default, Fast, Medium, High, Extra high, Max) sent in the
  fields each endpoint takes, and a collapsible Thinking block with the endpoint's thinking
  summary and how long it thought.
- Prompt caching on Claude endpoints: agent requests mark their stable start, so input that repeats
  an earlier step is billed at the cache-read rate.
- Collapsible activity.
- Responsive desktop/mobile UI, bundled fonts, and no credential storage in the browser.

This release does not include a browser automation engine, OS computer control,
cloud sessions, a scheduler, a full PTY terminal, or an OS execution sandbox.

## Quick start

After the one-time setup below, start the app from this folder with the Python
environment you installed it into. The app is configured using an ignored `.env`
that points to your existing credential file. No token is copied into the project.

```bash
cd local-agent-workspace
.venv/bin/python run.py
```

Open http://127.0.0.1:8765. Use `--port 8766` if that port is occupied.
Stop the local server with Ctrl+C. It must remain running for the UI and agents to work.

## Install / move to another computer

Tested on macOS with Python 3.12. Install Python 3.12+ and Git for the Changes
panel and worktrees. Shell tools select zsh, bash, or sh, in that order. The
frontend is plain JavaScript loaded directly by the browser, so a fresh checkout
needs no Node.js, pnpm, or frontend build to run or change. Native Windows is not
supported by the POSIX filesystem/process handling; use a Linux environment such as WSL for that
platform. The continuous-integration workflow (`.github/workflows/tests.yml`) runs the backend
and UI test suites on Ubuntu and macOS; everyday use on Linux and WSL has not yet been checked
by hand.

Clone the repository or copy the `local-agent-workspace` folder, including
`backend`, `frontend`, and the setup files at its root. No sibling repository
is required. Recreate the Python environment on the destination system instead
of copying it from another computer.

On the new computer, open a terminal in `local-agent-workspace`. For a fresh
macOS/Linux/WSL setup, create a local Python environment and install the backend:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -c constraints.txt -e ./backend
cp .env.example .env
```

Edit `.env` before starting: set `LOCAL_AGENT_ENV_FILE` to the credential file's
absolute path on this computer, `LOCAL_AGENT_WORKSPACE` to an existing project
folder, `LOCAL_AGENT_MODEL` to your Databricks endpoint name, and
`LOCAL_AGENT_STATE_DIR` to the absolute folder where the app should keep its
private state. The example paths must be replaced. The installation command
installs runtime libraries automatically; add `./backend[test]` instead of
`./backend` only if you need to run the backend tests.

Without `LOCAL_AGENT_MODEL`, or with it empty, the app uses `databricks-claude-opus-5-5` (Claude Opus 5.5,
with a 200,000-token context budget and 16,384 Max output tokens). `LOCAL_AGENT_MODEL` in
`.env` applies only while no model is saved in Settings: once you save Settings (or choose a
model in the model picker), the saved model wins over `.env`, so change the model there, not
in `.env`. Saved settings from before Opus 5.5 became the default that still hold GPT OSS 120B
at 131,072 and 8,192 move to Opus 5.5 once, also when `.env` says
`LOCAL_AGENT_MODEL=databricks-gpt-oss-120b` (the old `.env.example` value) or
`databricks-claude-opus-5-5` (the new one); Settings says so, and Opus 5.5 costs more per token.
To switch back, choose `databricks-gpt-oss-120b` in Settings, or in the model picker of a new
conversation (in an open conversation the picker changes only that conversation). The choice is
kept after that, and Settings shows the note until then.

Two optional values can be left empty:

- `LOCAL_AGENT_CODING_DIR`: an absolute folder (or one starting with `~/`) where the agent
  creates new standalone scripts, tools and projects when you do not say where. See
  [Local access and limits](#local-access-and-limits).
- `LOCAL_AGENT_PYTHON_VENV`: a Python virtual environment (the folder that contains
  `bin/python` and `pyvenv.cfg`) that the agent's commands, Verify, hooks and the Terminal
  use (stdio MCP servers do not). See the command environment paragraph in [Local access and limits](#local-access-and-limits).

Both are also in Settings. A value changed in Settings overrides `.env`; a value left
unchanged in Settings keeps following `.env`, so a later edit of `.env` applies after a
restart. Saving an empty field in Settings turns the value off even when `.env` sets one.

The credential file is plain assignments, parsed as data (never executed):

```text
DBRICKS_URL=https://your-workspace.cloud.databricks.com
DBRICKS_TOKEN=your-token
```

`DATABRICKS_HOST` and `DATABRICKS_TOKEN` environment variables are also supported.
Shell environment variables override the credential file. Keep `.env`, credentials,
`.local`, and virtual environments out of Git.

For each normal start, open a terminal in `local-agent-workspace` and run:

```bash
.venv/bin/python run.py
```

Open http://127.0.0.1:8765 and leave that terminal running. This one Python process
serves the frontend and backend; no separate frontend server is needed.
Use `.venv/bin/python run.py --port 8766` to select another port.

To stop the whole server, press **Ctrl+C once in that terminal** and wait for
shutdown to finish. Normal shutdown cancels active agent turns and managed command
jobs and closes the conversation database. Closing the browser does not stop the
server. The chat's **Stop** button stops only that response and its foreground
work, not the server or unrelated background jobs. Restart with the same command;
saved conversations remain available. Backend dependency installation is a
one-time setup step unless those dependencies change.

Saved UI settings are in `.local/settings.json` and override `.env` defaults. Update
the credential-file path, project folder, and model in **Settings** after migration,
then start a new conversation. Existing conversations keep their workspace and model.
To change the model in an existing conversation, use the dropdown below its message
box while the conversation is idle.
The credential URL must be the Databricks workspace root, and the selected serving
endpoint must support streaming and function calling.
The sidebar footer shows the connection: **Checking Databricks…**, **Databricks connected**
or **Connection needs attention** (hover it for the error). When the check fails, a banner
above the chat says why: for a network failure **Can't reach Databricks at <host>. Check
your network or VPN connection.** with the error under **Details**, for a credential or HTTP
problem the error itself (a Databricks server problem or rate limit says **Try again in a
moment.**), and **Could not check the Databricks connection.** when no reason came back.
**Check again**, offered for a network failure, a server problem, a rate limit, an unreadable
answer and the no-reason case, repeats the check (the banner says **Checking…**, then the time
of a check that still failed); **Open settings** opens Settings. When a turn ends while the last
check failed, the connection is checked again.

The app runs with Python and its runtime dependencies only
(`python -m pip install -c constraints.txt -e ./backend`).
Git and a supported shell are still needed for their respective tools.
The backend installation automatically installs FastAPI, Uvicorn, HTTPX, regex, jsonschema,
MCP (`mcp==2.2.0`), and the MCP HTTP transport dependency HTTPX2. No Claude SDK,
Claude CLI, or sibling SDK checkout is required. An optional MCP server may have
its own runtime requirements, such as Node; install those only for the servers you choose.

To move selected chat history between machines, use **Agent tools → Export →
Export conversation**, then import the JSON on the other machine and explicitly
choose its local project folder. Imports receive fresh IDs, Manual permissions,
no folder grants, and no active skills; they never overwrite existing conversations.
For a complete local backup, stop both servers before copying the entire `.local/`
directory. The `conversations/` JSONL files retain absolute paths and grants, unlike
the safer selected-conversation import. They also retain each chat's sanitized command-job
snapshots. The SQLite file contains the separate live operational indexes/controllers
for jobs, tasks, checkpoints, and worktrees.

### Conversation durability and copies

Streamed assistant text and reported usage are checkpointed after 16 KiB of new
content or one second of dirty state, including when the provider stalls. Each save
rewrites the conversation's whole file, so in a long conversation (about 10 MB after some
180 file reads at the default budget) checkpoints are spaced to at least 20 times the
last save's duration, keeping saves to about a twentieth of streaming time; the JSON of
long texts is reused between the saves of a running turn (it is dropped when the turn ends). Normal completion and Stop flush the final state. After an abrupt process death, the last
saved partial reply is shown as interrupted; the most recent uncheckpointed text
may be lost. Incomplete streamed tool arguments are never persisted as executable
requests or replayed. A local storage failure stops the response and reports that
recent history may not have been saved. Back up the whole `conversations/` folder
(the JSONL files and their `.state.json` files) and the separate operational SQLite
database.

**Agent tools → Export** exports a versioned JSON bundle, optionally with
subagent conversations (32 total, 16 MiB maximum). Export requires idle chats.
Imports preserve historical messages, tool results, summaries, and usage, remap
conversation/event/call IDs and included child links, and cancel historical active
states. Credential settings, hooks/MCP configuration, task-board records, jobs, file checkpoint ownership,
folder grants, "Allow for this chat" rules, and the record of which files the agent read are
not transferred. Conversation text can itself contain sensitive data: inspect exported files
before sharing them. Choose a trusted bundle and an
existing destination folder; imported paths never select the destination for you.
Repeated imports intentionally create separate copies. Unsupported versions, malformed
history, oversized bundles, and authority-bearing fields are rejected atomically.

**Fork completed conversation** makes a separate full-history copy in the same
workspace, leaving the original unchanged. Only completed, idle conversations can
be forked this way. **Fork from here** on a reply (see **Edit, retry and rewind**)
copies the conversation up to that reply instead, also from a stopped or failed turn,
and can put the copy in a new Git worktree. Every fork starts in Manual with no folder
grants, allow rules or active skills, retains any tool profile restrictions and turned-off
tools, and does not copy child conversations, task-board records, jobs, queued messages,
or file checkpoints.

An in-place rewind (**Edit**, **Retry**, **Rewind to here**) first saves the whole
conversation as an archived copy titled `<title> (before rewind)`. The copy keeps the
messages, replies, tool results and summary, starts in Manual with no grants, rules or
active skills, and does not copy the usage ledger, jobs, queued messages or waiting
command output. Its own **Usage** view still shows the token counts recorded on its
replies, as historical calls marked incomplete (like a fork); no view adds usage across
chats. A worktree chat's copy keeps its `worktree` header, so it is listed with its
project. Archived copies can be opened from the rewind notice and deleted like any
conversation.

Each user message records whether its text reached the model history (`in_wire`), so
a cut before any message maps exactly onto the model history; app-generated prompts
(automatic continuations and Verify messages) always stay with the turn before them.
Messages saved by earlier versions have no record and are matched by their exact
text; when that is not possible, a conversation cut before them is refused with a
message, while code-only rewinds and whole-chat forks still work.

The sidebar reads the small metadata record at the start of each conversation file
instead of loading every event and provider-history record. Allow rules are stored as
separate records after it, so the sidebar list does not carry them. Full content is loaded
when opening a conversation or performing restart recovery.

## Working with the app

- **New conversation**: choose a model, then send a task. Enter sends; Shift+Enter adds a line.
  A message is limited to 50,000 characters. A longer one is not sent (it stays in the box)
  and the app advises saving long text such as a handoff as a file in the project and
  attaching it with `@`, so the agent can outline it and read the parts it needs.
  Opening the root URL or clicking **New** starts with an empty context. Sending the
  first message creates a unique session, even if another chat has the same title.
  Select a sidebar conversation to resume it. Each saved chat has a `?session=<uuid>`
  URL, which preserves that selection on refresh and supports browser Back/Forward.
  The header shows a short session ID when there is room; hover the title for the full title
  and ID. The sidebar shows the ID after a title that several conversations share. In a narrow
  window **Agent tools** and **Workspace** show only their icons.
  Drafts stay with their conversation while switching chats and are saved in this
  browser's `localStorage` about half a second after you stop typing (and when the page
  closes), so they survive a reload; the new-conversation view keeps its own draft, which
  moves to the conversation once it exists. A sent draft is removed (a new conversation's
  first message as soon as you send it, so **New** during the send starts empty; if sending
  fails it comes back as the new-conversation draft, also in a new view opened during the
  send); text typed while it was sending is saved as the new draft. At most 50 drafts
  are kept (the oldest goes first). A draft over 100 KB, or any draft when the browser
  blocks storage, is kept only in the page. Drafts stay in this browser profile; the
  server never receives them.
  The message box stays editable while connecting. The permissions menu opens above
  the composer; clicking the input closes it, and choosing a mode returns focus to chat.
  Open tabs reconnect after a server restart without discarding the current draft.
  While the local server cannot be reached, the banner **Reconnecting to your local
  server…** shows on every screen and the sidebar shows **Local server offline**; both clear
  when it answers again (an error it answers with is shown), and the Databricks connection is
  checked again. A request that cannot reach the server reports **Cannot reach the local server. Check that it is still
  running.**; a response the app cannot read reports **The local server sent an unexpected
  response (HTTP N).**
  A tab that does not accept an update within 5 seconds is disconnected, so it cannot
  hold up a reply, Stop or saving; it reconnects and reloads the conversation.
- **Conversation status**: each sidebar row shows what its chat is doing.
  **Needs approval** marks a chat waiting for an approval card; a pulsing dot marks a chat
  that is working (replying, compacting, naming itself or waiting for a subagent); **Error**
  marks a chat whose last reply ended with an error, or was cut off because the server
  stopped, and that you have not opened since (in the narrower sidebar of 601–1,120 px windows
  these two badges are small coloured dots with a tooltip, so the title keeps its room); a
  blue dot marks a chat that changed since you last opened it. **Running** above the list
  shows only chats that are not idle; click it again to show all. The browser remembers when
  you last opened each chat (in `localStorage`, on this browser only, shared by its tabs: a
  chat opened in one tab counts as read in the others); the first time, every
  existing chat counts as read. Renaming a chat does not mark it unread. While any chat waits
  for approval, the tab title starts with the count, for example `(2) Local — …`.
  The sidebar list refreshes every 1.5 seconds while a chat is working and every 10 seconds
  otherwise, so chats started in another tab also show up.
  - **Projects**: chats are grouped by project folder. The current project (the one
    **Settings** selects) comes first and is open; other projects are closed, and their
    heading shows the folder name, the number of chats and badges such as `1 needs approval`
    or `2 running`. Click a heading to open or close it. A chat in an app-managed worktree, and
    each of its subagent chats, is listed under the project it came from and shows its branch;
    the project button above the message box shows that project and the branch too. A group
    shows 50 chats, then
    **Show N more**.
  - **Subagents**: a subagent chat is listed under its parent behind **Show N subagents**,
    which opens by itself while a subagent is working. A subagent whose parent was deleted
    or is not listed is a chat of its own.
  - **Pin, archive and tags**: **More actions** (`…`) on a row offers **Rename**, **Pin**,
    **Archive**, **Edit tags…** and **Delete…**. Tags are comma-separated, 1 to 32 characters
    each, at most 10; tags that differ only in case count as one. Pinned chats are listed under **Pinned** above the projects.
    Archived chats are hidden, also when pinned; **Archived (N)** at the end of the list
    shows them, and **Unarchive** brings one back. **Tags** (next to **Running**, when any chat
    has tags) lists every tag with its number of chats; check tags to list only chats with any
    of them. Clicking a tag on a row adds it to that filter; **Clear tag filter** shows all
    again. Pinning, archiving and tagging do not
    change a chat's place in the newest-first order or mark it unread, and work while it is
    replying. A pinned subagent chat is listed under **Pinned**, not under its parent. Tags are
    exported with a chat and kept by forks and imports; pin and archive are not.
  - **Search**: **Search conversations** shows only chats whose titles contain every word you
    type; a `#word` filters by tag instead (chats with a tag starting with word) and is not
    searched in messages. From 3 characters, after a short pause, the messages of all chats are searched too
    (your messages and the agent's replies, not tool output) and listed under **Found in
    messages** with a short excerpt (chats that match only by title are not listed again
    there); click one to open its chat. Excerpts have the configured
    token and common secret shapes replaced with `[REDACTED]`. The search reads the
    conversation files on demand (there is no index) and stops after 64 MiB or 3 seconds,
    saying "Search stopped early; refine your words."; archived chats are included and
    marked, subagent chats are not. Esc clears the search.
  - **Unreviewed changes**: an amber dot marks a chat with agent file changes you have not yet
    kept or undone (its title says how many files), including changes its subagents made.
  - **Long chats**: opening a chat loads its latest 200 messages and actions, extended to the
    whole latest turn (at most 1,000). **Load earlier messages (N more)** at the top loads the
    100 before them without moving what you are reading. Until then, the **Activity · N
    actions** summary and the turn labels of **Agent tools → Checkpoints** cover only the loaded
    part. Reconnecting loads the latest part again.
- **Notifications**: click **Notify me** at the bottom of the sidebar to turn on browser
  notifications; the browser asks for permission only then. The switch shows **On** or
  **Off**. You are notified when a chat starts waiting for approval, finishes a reply,
  reaches the **Agent steps** limit ("Reached the step limit: …", so you can Continue it), or
  stops with an error (also during an automatic compaction, or because the server stopped
  during the reply), if this page is hidden or that chat is not the one open. A notification reads, for example,
  "Needs your approval: Fix the parser": it contains only a status and the chat title, cut to
  60 characters, never other conversation text. Until the chat is named, its title is the start
  of the first message with credentials redacted (the configured token and common secret shapes
  such as GitHub tokens). Clicking it brings the window forward and opens that chat.
  Stopping a reply yourself, or a manual compaction, does not notify. A subagent's turn started
  by its parent notifies only when it needs approval (the parent's turn reports its end); a
  subagent chat you continue yourself notifies like any other. A notification closes when it
  no longer applies (the approval was answered, the chat started another turn, or you opened
  the chat), so the next approval request alerts again. Each open tab notifies on its own; the browser replaces a
  notification with the same chat and status. If the browser blocks
  notifications for the page, the sidebar says so; allow them in the browser's site settings.
  For when no tab is open, see **Settings → Desktop notification when no browser tab is open**:
  the server then shows the same text as a system notification (`notify-send` on Linux when
  it is installed, `osascript` on macOS, nothing elsewhere). It is off by default and only
  applies while no page of the app is open: every page, also one with no chat selected,
  holds a connection to the server (`/api/presence`). It starts the notifier directly, with
  the title and message as arguments (no shell) and a 5-second limit; for `notify-send`,
  `<`, `>` and `&` in a title are escaped so that notification servers show them as text.
- **Skills (`/`)**: type `/` at the start of the message box to list the skills this
  conversation can use, with their descriptions; keep typing to filter. Use ↑/↓ to
  highlight one and Enter or Tab (or click it) to insert `/<skill-id> `, then add your
  request and press Enter to send. Esc closes the list. A message that contains a
  listed skill's command as a word, at its start or anywhere else (`Read the notes and
  use /handoff to …`), selects that skill for the whole message. Other text with `/`,
  such as a path (`/tmp/x.log`, `~/handoff/`), is sent unchanged.
  See **Agent tools: MCP, skills, and hooks** below.
- **Messages while the agent works (queue and steering)**: while a reply, a compaction or a `!`
  command runs, the box keeps **Stop response** and adds **Queue message**; Enter queues too, and
  Esc in the box stops the reply. At most 3 messages (50,000 characters in total) can wait; they
  are listed above the box, where **Edit** moves one back into the box and **Remove** drops it. At
  the next step boundary, after every result of the agent's current tool calls, the waiting
  messages reach the agent as one message of the same turn, marked as sent while it was working
  ("steered"). A message queued during the final reply is sent as the next turn once the reply
  completes (also when a tool failed in it). After **Stop**, an error, the step limit or a server
  restart, queued messages are not sent: they wait, marked **Paused** once you send something
  else, until you click **Send queued messages** (which sends all of them as one message), edit or
  remove them. Because queued messages are sent together, their `@` files must fit the limits of
  one message (see below) when you queue them. A message queued while a question or plan card
  waits stays queued; it never answers the card. Queued messages are saved with the chat (in its
  `<id>.state.json`) and are not exported.
- **Files in a message (`@`)**: type `@` to pick a project file (↑/↓, Enter or Tab inserts
  `@<path> `, Esc closes); `@src/app.py:10-40` or `@src/app.py#L10-L40` names lines. The menu
  leaves out files whose paths an `@` token cannot spell (spaces, quotes, `<>|@` or a backtick in
  the path, or a trailing punctuation mark). Chips above the box show the mentions. A long path is
  shortened from the folder side; hover a chip for the full path. When the
  message is sent, the app attaches a snapshot of each mentioned file that exists: up to 1,000
  lines of a range, or the first 200 lines of a whole file, at most 8,000 characters per file, 5
  files and 32,000 characters per message. The agent receives them fenced as data after your text;
  the message shows only your text and the file names. Snapshots follow the chat's own file
  access: credential files, generated folders, the app's state folder, folders the chat may not
  read (use **Folder access** first) and read deny rules or a turned-off `read_file` refuse the
  message with an error. Missing paths, folders and words such as `@dist` stay plain text, and so
  do tokens inside code: a fenced block, or a code span on one line (a run of backticks closes
  only at the next run of the same length, as in the quoted lines of review comments). Secrets in snapshots are redacted like command output, before a snapshot is
  shortened to 8,000 characters. An attached file counts as read, so the agent may
  edit it without reading it again. Only your typed text selects skills.
- **Shell commands (`!`)**: a message starting with `!` runs the rest as one shell command in the
  project folder, like **Workspace → Terminal** Run, when the chat is idle, for at most 60 seconds
  (use **Workspace → Terminal** for longer commands). It is an explicit user action, so it runs in
  every permission mode, Plan included, and permission rules (allow, ask and deny) do not apply to
  it: they govern the agent's actions. Text with a line break is not run. The command shows as a
  card with its output; the output (up to 8 KB, with the configured token and common secret shapes
  redacted; at most 5 commands kept) is attached to your next message, so the agent sees it then;
  a chip above the box shows it until then. **Stop** ends the command and attaches nothing.
- **Side questions (`/btw`)**: `/btw <question>` (also listed first in the `/` menu) asks the
  model about the conversation without adding to it: one non-streamed request with the whole
  conversation as context and no tool use, also while a reply runs (one side question at a time
  per chat; while it is answered, the box stays free for other messages; while a reply runs, the
  **Ask side question** button next to **Stop response** sends it too). The answer appears above
  the box with **Copy answer** and **Dismiss**; it is not saved, and the next message does not see
  it. The request is billed and listed as **Side question** in the usage panel. A conversation too
  long for its context budget needs a compaction first.
- **Model selection**: the dropdown below the message box changes the model for
  the current conversation. Its history, title, folder access, and permissions are
  preserved, and the next reply uses the selected endpoint with the retained chat
  context. The selection is saved across refreshes and restarts. Choose between
  replies; sending and model selection wait while a model change is being saved.
  Before a conversation exists, the dropdown changes the default for new chats.
- **Conversation titles**: the opening message supplies an immediate temporary title.
  After a successful reply, the same Databricks model creates a short heading for
  the sidebar and chat header. This uses one additional model request, billed by
  your endpoint, with a 10-second timeout. Only brief excerpts of the opening message
  and answer are sent; title generation does not alter the conversation context.
  Successful titles are saved and reused after restart. If naming fails or you stop
  it, the temporary title stays and naming can retry after a later successful reply.
  Older automatically named chats are eligible when resumed; custom and worktree
  titles are preserved.
  Choose **More actions** (`…`) → **Rename** on a sidebar conversation to rename it (up to 160
  characters). **Save name** or Enter saves; **Cancel rename** or Escape discards the edit.
  Focus returns to **More actions**.
  Renaming also works during a response and does not change its history, model,
  permissions, or selected conversation. Manual titles persist after restart and
  are never replaced by automatic naming, including a title request already in flight.
- **Workspace / Files**: browse a folder, edit UTF-8 text files up to the file write limit
  (Settings, 80 KB by default), or use the plus button to reference the file in chat ("Please read the project file: <path>", or
  "Please read the file: <absolute path>" for a folder outside the project). On windows narrower
  than 1,024 px the Workspace panel opens over the chat (from 601 px it stops at the sidebar, which stays usable with the keyboard and the mouse and is not dimmed): focus moves into it, and
  **Close workspace**, Escape (in the panel or the sidebar) or a click on the dimmed chat closes it (that first
  click only closes the panel). An error that comes up while it is open shows at the bottom of the panel.
  From 601 px wide, drag the panel's left edge to make it wider or narrower, or focus that edge
  and use the arrow keys (Shift for bigger steps, Home and End for the narrowest and widest);
  the width is kept in this browser, and Enter or a double-click on the edge restores the
  default. **Expand** in the panel header makes it as wide as the chat column allows (a docked
  panel leaves the chat at least 420 px); **Restore workspace width** undoes it. In a worktree
  chat the header names the project and the worktree's branch. When the
  conversation has granted folders (**Folder access**) or you added folders to Files, a row of
  folder buttons at the top switches between **Project**, each granted folder ("Allowed in this
  chat") and each added folder ("Added to Files"). Such a folder is shown by absolute paths and
  the parent button stops at that folder. A folder button shows a dot when the folder contains
  files the agent changed (screen readers hear the count). **Add folder…** adds a folder to Files for the
  project (every chat of the project shares the list; a worktree chat has its own), like an
  editor's "Add Folder to Workspace": you can browse it, edit its files and see the dots for
  files the agent changed there, for example in Bypass permissions, which needs no grant.
  Adding a folder does not give the agent access: it still needs **Folder access** to read or
  change it. Credential files stay hidden in added folders too. The filesystem root, folders
  inside the project, the app's state directory, a folder inside it or a folder containing
  it (also when reached by another name, such as a different letter case or a bind mount),
  and folders the file tools exclude (such as `.git`, `.venv`, `.local` or `dist`) cannot be
  added, and a project can have up to 20 added folders. A folder button shows the folder's
  name, with parent folders when two names match; the full path is in its tooltip. The × beside an added
  folder removes it from Files and leaves the folder itself alone; a folder that no longer
  exists or cannot be reached shows "Not found". When the agent changed files under none of these folders,
  "N changed files outside these folders" offers **Add <folder>** for up to three of their
  folders. **Agent tools → Checkpoints** can preview and restore saves and agent edits in an
  added folder. You can review (**Keep** or **Undo**) the agent's changes there whatever the
  chat's folder access.
  The list is kept in `workspace_folders.json` in the state directory. The choice
  goes back to **Project** for another conversation or when the folder's grant or entry is
  removed. Save detects concurrent disk edits.
  Unsaved editor text stays with its conversation when closing the workspace panel,
  attaching a file, or switching chats. Text entered during a save remains unsaved
  until the next save. Editor drafts are kept in the current page, not across reloads.
- **Review agent changes (Keep / Undo)**: files the agent creates or edits with its file
  tools are listed above the message box as **N files changed +added −removed**, and the
  **Workspace** button shows how many there are. Expand the bar to see each file and its
  line counts. **Keep** accepts the changes. **Undo** puts the file back as it was before
  the agent first changed it in this conversation; a file the agent created is removed.
  The **Keep** and **Undo** buttons on the bar act on every listed file; the check and
  arrow buttons on a row act on that file only. A file you keep while the agent still works
  is listed again when the agent changes it again; if it is open in the Workspace panel,
  its review opens again, and an open file without unsaved edits follows the agent's later
  edits.
  In **Workspace / Files**, a dot marks each changed file and each folder that contains
  one. Changes in a granted or added folder outside the project get their dots when you
  choose that folder with its button at the top of Files, which shows a dot too; changes
  under none of these folders are offered as **Add <folder>** there. You can review every
  file the agent changed, also outside the folders the chat may use now (for example after
  leaving Bypass permissions or removing a grant): the review reads and writes only those
  files, and the agent itself gets no access back. Click a file, in the bar or in the file list, to review it: removed lines are red
  and added lines green, and each change has its own **Keep** and **Undo**. The box at the
  bottom right shows the position (`1 of 3`), moves between changes with its arrows, and
  keeps or undoes the whole file. **Edit** switches to the text editor (an unsaved draft is
  kept) and **Review changes** switches back.
  - Changes stay listed across replies until you keep or undo them, and after a refresh or
    restart. A review always compares the file with its state before the agent's first
    edit, so your own later saves to it are part of the diff.
  - Keep works at any time. Undo waits until this conversation, and any other chat in the
    same workspace, is idle and no workspace command is running (the Recovery rule). If a
    file changed after the list or diff you looked at, its Keep or Undo is refused and the
    list refreshes, so you can look again. An Undo of several files checks all of them
    before it changes any.
  - An undo is itself a file checkpoint: **Agent tools → Checkpoints → Other file edits** can
    bring the agent's version back. Folders created for a new file are not removed.
  - The agent's `write_file`, `edit_file`, `move_path`, `delete_path`, `notebook_edit`
    and `insert_activity_log` changes are listed, and files changed by its foreground
    `run_command` commands in Git folders (below). A move is listed as the new file (created)
    and the old one (deleted); undo both to put the file back where it was.
    MCP tools and hooks are not listed; **Workspace / Changes** shows Git's view. Exports, imports and
    forks do not copy the list, and deleting a conversation forgets it and leaves the
    files as they are. Before its next request the model is told which files it read or
    wrote changed since, an undo included, and `write_file`, `delete_path` and
    `notebook_edit` refuse to change such a file until the model reads it again (see
    **Tool details**). Recovery's limits apply: regular UTF-8 files up to 1 MB. Line endings
    are kept: an edit to a Windows (CRLF) file keeps CRLF, and a file with mixed endings
    keeps each unchanged line's ending.
  - **Subagents**: a subagent's edits are listed in the subagent's conversation and in its
    parent's, marked **Subagent** in the parent's bar, so the parent's **Undo** covers what
    its subagents changed. Keeping or undoing in one leaves the other showing what differs
    from its own starting point. A file the parent changed first keeps the parent's starting
    point.
  - **Commands**: before and after each foreground `run_command` (including Verify), the app
    compares `git status` of the project folder, the chat's granted folders and the folders
    added to **Workspace / Files**, each one that is inside a Git repository (a granted folder
    that contains the project does not replace the project's own repository). Files the
    command created, changed or deleted are listed with a **Command** mark. For a file that
    was already changed before the command, the review compares with its content just before
    the command; for a file that was clean, with the last commit before the command, so a
    `git add`, `git commit` (also the first commit of a new repository), `git checkout` or
    `git stash` in the command does not hide the change. A file the agent already edited
    keeps its earlier starting point. The Git calls run without hooks, fsmonitor or the
    repository's clean and smudge filters (also a filter the command itself configures), and
    are bounded: 10 seconds per Git call, and at most about 25 seconds before the command
    starts and 15 seconds after it ends (repositories are checked in parallel), so in a very
    large repository each command can wait that long. After **Stop**, the turn waits up to
    10 seconds more for this check.
    Not covered: folders that are not in a Git repository (**Workspace / Changes** shows Git's
    view there), background jobs, commands run from **Workspace / Terminal** or as `!`
    commands, files Git ignores, credential files and folders the file tools exclude, paths
    through a symbolic link, files over 80 KB or binary, folders with more than 2,000 changed
    files before or after the command, and more than 500 files per repository: files the
    command added to `git status`, changed the status of or committed are checked first,
    then files that were already changed before it, and the rest are not listed. The read-only commands that run without a card (`pwd`, `ls`,
    `git status` and the like) are not checked at all. Edits that other programs make while
    a command runs are listed as that command's. Line-ending conversion configured in
    `.gitattributes` can show a change for a file that was clean. An undo of a command's
    change is a Recovery checkpoint like any other; Recovery has no checkpoint of the
    command's own edit.
  - **Line comments**: in a file's review, the comment button at the start of a line (shown
    on hover and keyboard focus, and always faintly on touch screens) opens a box for a
    comment on that line; **Add comment** (or Ctrl+Enter / Cmd+Enter) adds it under the
    line and Esc cancels, and focus returns to the line's button. The line buttons are one
    Tab stop: the Up and Down arrows (Home, End) move between lines. **Send N comments** in
    the box at the bottom right sends every comment of this conversation, from all files,
    as one message: the file and line, the line's text and your comment, in file and line
    order, ending with a request to address each comment and summarize the changes. It
    waits until the reply finishes; if sending fails, the comments stay. Up to 50 comments
    at a time, and the message must stay under 50,000 characters (remove some comments
    otherwise). Comments are kept in the page while you switch files or close the panel, but
    not across a reload. A comment belongs to its line's text: when the file changes and that
    line is gone or moved, it is listed under **Comments on lines no longer in this diff**.
    Comments on other files are listed under the diff, each with **Remove**. When no review
    is open, for example after you keep or undo the file or after Automatic Keep, the
    conversation's unsent comments stay at the bottom of the Workspace panel with
    **Send N comments**.
  - **Automatic Keep** (Settings, off by default): when a reply finishes, every listed change
    of that conversation is kept and the bar clears. A reply that you stop, that fails or
    that reaches the step limit keeps its list, so **Undo** stays available. A manual
    compaction does not count as a reply, and a subagent's reply clears only the subagent's
    list. Edits made with the
    agent's file tools can still be undone from **Agent tools → Checkpoints**; edits made by
    commands can no longer be undone from the app.
- **Workspace / Changes**: inspect Git status and tracked-file diffs. Untracked
  files are listed too. Files are grouped as **Staged changes**, **Changes** (not staged,
  including conflicts) and **Untracked**, each with its count, under the branch name.
  A row shows a status letter (M modified, A added, D deleted, R renamed, C copied, T type
  changed, ! conflicted, U untracked), the path and the added and removed line counts.
  Click a row for a read-only diff with a separator per hunk: **Back to changes** returns,
  **Open file** opens the file in Files, and **Open in VS Code** opens it in VS Code. This
  is Git's view of the project, so it includes changes made by commands; it refreshes when
  you open the tab, on Refresh, and when a response ends. There are no staging actions.
  The server runs Git directly (no shell) with the programs a repository's configuration
  can name switched off: `core.fsmonitor`, hooks, clean/smudge filters, external diff and
  text conversion. Git does not look inside submodule work trees, whose own configuration
  could name filters; a submodule whose commit changed is still listed. The server also
  limits Git's environment and stops after 60 seconds. It reads status up to 1 MB and
  lists at most 2,000 files; a diff shows at most 512 KB, and an untracked file is shown
  up to 1 MB. A diff covers one file: a folder path is refused. Paths are literal, so a
  file named `*.py` shows only its own diff. A project
  folder inside a larger repository shows only its own files. Credential files at any
  depth (`.env`, `.env.*` including `.env.example`, `*.env`, `env_vars.txt`, `.netrc`,
  `.npmrc`, `credentials`, `credentials.json`, `id_rsa`, `id_ed25519`, in any letter case,
  and the configured credential file) are listed as "Credential file: diff hidden",
  without line counts, and their diffs are refused, also when a rename or copy comes from
  such a file. Files in generated folders (`node_modules`, `dist`, `.venv` and the like)
  are listed after the other files, with their own 2,000-entry limit, and their diffs are
  not shown; an untracked generated folder is one row (`node_modules/`), so it never hides
  other new files. A folder that is not a Git repository shows
  "Not a Git repository." For API clients, `GET /api/git` still returns the plain
  `git status --short` and `git diff` text (at most 80 KB).
- **Workspace / Terminal**: run a command explicitly and see output while it runs.
  Set the timeout (1–3,600 seconds, default 60) and retained output limit
  (1,024–1,000,000 bytes, default 80,000). A foreground job disables Run until it
  finishes; **Run in background** lets you launch another command. Select a job
  to inspect output, or use **Stop job**. History survives closing/reopening the
  panel and refreshing the browser; a long command cut off in the history shows in full
  in its tooltip, as does a long name in **Files**. Jobs belong to the selected conversation;
  commands launched before creating a chat belong to the selected workspace.
  This remains a noninteractive command runner: stdin is closed, with no PTY.
  Closing the panel does not cancel a job. Errors pause polling until **Refresh jobs**.
- **Approvals**: approve or decline model-proposed writes, commands, MCP calls, and hooks. An unanswered
  card waits **Settings → Approval wait (minutes)**, 60 by default (1–1,440); a browser refresh
  retains it while the server runs (a server restart cancels it). After that time the card shows
  "Not answered in time", the action does not run, and the agent is told nobody answered (not
  that you declined). The card shows when it expires (with the weekday when that is not today).
  0 waits until you answer or press **Stop**, like Claude desktop, but a waiting card keeps the
  chat busy, and a busy chat blocks Undo and Recovery restores in its project. Before this
  setting existed, every card expired after five minutes. An action stopped without any card, for example by Plan mode or
  because project instructions changed mid-turn, shows "Not run:" and the reason, not "declined".
  When a busy chat blocks an action, the error names that chat and why (for example
  "“Fix the parser” is waiting for your approval. Answer or stop it before undoing agent
  changes."); when it is another chat, **Open “…”** next to the error switches to it. The
  button appears in the error banner, next to Undo errors in the Workspace file review, and
  next to Restore errors in **Agent tools** → **Checkpoints**.
  A chat waiting for approval shows **Needs approval** in the sidebar and counts in the tab
  title; **Notify me** also tells you when this page is hidden or another chat is open.
- **Allow for this chat**: next to Decline and Approve, a card offers **Always allow this
  command in this chat** (commands), **Allow edits in this chat** (`write_file`, `edit_file`,
  `insert_activity_log`, `move_path`, `delete_path`, `notebook_edit`), **Allow this tool in
  this chat** (one MCP tool), **Allow this hook in this chat**, **Allow subagents in this
  chat** (`delegate_task`) or **Allow stopping jobs in this chat** (`stop_job`). A card raised
  by an Always ask rule, an uncertain command or a protected file, or a command that names a
  credential file or contains your token, has no such button. It approves the card and stores
  a rule for this conversation. Rules match
  exactly: the same command text (only leading and trailing whitespace is ignored; never a
  prefix), the same MCP tool name, the same hook ID. A later matching action runs without a
  card in Manual, Auto, Accept edits and Don't ask, and its card says "Allowed by a rule for this chat".
  Plan still blocks it, Bypass is unchanged, and credential exclusions, folder-access cards,
  protected files, project-guidance checks and the changed-file check still apply. A command
  allowed by its exact text runs through the shell exactly as approved. Folder-access cards have no
  such button: a folder grant already lasts for the chat. Rules are saved with the chat (at most
  100), listed in the permissions menu (commands with their exact spacing), where each can be
  removed at any time, also while the chat is busy, and inherited by subagents. Forks and imports
  start without rules. Rules for one project or for all projects, and deny and ask rules, are
  described in **Permission rules** below.
  The button is joined to the **Always allow…** arrow when the action allows more: the project
  and all-project scopes, or a rule that matches the command or a folder; on a narrow command
  card it reads **Always allow this command**. A command that can only be allowed as this exact
  text in this chat (shell syntax) offers the button alone.
- **Decline with a note**: **Tell the agent why (optional)** on a card adds up to 2,000 characters
  to a Decline. The agent receives "User declined this action. Their note: … Do not retry it
  without a new instruction." (similar wording for MCP calls, hooks and folder access), and the
  collapsed card shows "Action declined: …". The note is information for the model; it never
  changes what the chat may do.
- **Tool details**: task cards show their names and task status. A completed create/update
  action does not mean the task itself is finished. Expand command cards to see the
  full command (including inline Python), options, and output separately. A long file
  is read in pages, so a read card names the lines it covered (`lines 109–243`).
  A command that exits non-zero, times out or is interrupted shows a warning icon, a label
  (`exit 1`, `signal 9` for a process ended by a signal, `timed out` or `interrupted`) and its
  last output line under the collapsed card (`Exit 1 · …`; when output past `max_output_bytes`
  was not kept, it says so instead); its expanded card's **Action** line reads `ran · exit 1`,
  not `completed`. A command started with `background` is labelled
  `started in background`. A failed tool shows its error category, and its expanded card shows
  **Error category** and the **Hint given to the agent**. Expanded command and job cards show the
  output as text, then a **State** line (state, exit code, size, and which part the result
  holds: the first part, the start and end, the latest output from a character, or a character
  range, counted from 1; the agent reads the rest with `get_job_output`), and **Raw result**
  with the full result (with the edited-command note for a command you edited; when a result
  was shortened for the agent or not sent to it, the card says so). On `wait_job`,
  `get_job_output` and `stop_job` cards a green tick means
  the tool worked; a muted label such as `job exit 1` reports the job itself. A `wait_job` card
  first says how the wait ended: `matched` (the **State** line quotes the matching line),
  `wait timed out` (no tick), `output quiet` or `job ended`.
  **Read before overwriting:** the conversation remembers the size and modification time
  of each file the agent read (`read_file` any page, or `notebook_read`) or wrote, up to 500 files.
  `write_file` on an existing file is refused, before any approval card, unless the
  conversation has read or written it and it has not changed since; the card shows
  "Not run:" and the reason, and the model reads the file and writes again. The check
  repeats after the approval and before the write. New files, `edit_file` (its
  `old_text` must match exactly) and `insert_activity_log` are not refused. The same check
  applies before `delete_path` deletes a file, before `notebook_edit` changes a notebook,
  and before `move_path` with `overwrite=true` replaces an existing destination.
  **Edits:** `edit_file` takes one `old_text`/`new_text` pair (`replace_all=true` replaces
  every occurrence), or `edits`: up to 50 replacements applied in order to one file, all
  checked before anything is written, with one approval card and one checkpoint. A
  replacement that is not found, or that matches more than once without `replace_all`,
  changes nothing. With `replace_all` or `edits` the result starts with "Applied 2 edits
  (5 replacements)." Line endings are kept: unchanged text stays byte-identical and new
  lines take the file's most common ending (CRLF on a tie). `write_file` and editor saves
  of LF text over a CRLF file keep CRLF; over a file with mixed endings they keep the
  endings of the unchanged first and last lines. Matching is linear, also for 1 MB files,
  and diffs (approval card, change review, recovery) stay fast on large files: a large
  changed region that cannot be matched quickly is shown as one change. The
  model gets at most 8 KB of an edit's diff; the change review shows all of it. One
  `write_file` call carries at most about 500 KB (tool arguments are capped at 512 KiB),
  so larger files are built with `edit_file`. An `edit_file` on a file already over the
  write limit is refused with the file's size and the limit.
  **Diagnostics:** after `write_file`, `edit_file`, `move_path` and `notebook_edit` the app
  checks the file offline: Python syntax with the server's own Python parser (the code
  never runs, and the message names the Python version, so a project on a newer Python
  may see false alarms), JSON and notebooks, TOML, and unresolved merge-conflict markers in
  any text file. Only problems the change introduced are reported, at most five, after
  "Diagnostics for <path>:" in the tool result. No project checker runs after each edit.
  **Move and delete:** `move_path` moves or renames one regular UTF-8 text file within the
  file write limit, creating folders as needed; an existing destination needs
  `overwrite=true`. `delete_path` deletes one such file. Both refuse folders, binary or
  larger files and symbolic links (a link is never followed), and suggest `run_command`
  for those. Each path needs folder access, credential files stay excluded, and both
  follow the permission mode like edits (**Allow edits in this chat** covers them).
  **Notebooks:** `notebook_read` reads a Jupyter notebook as cells (index, id, type,
  source and a short summary of outputs) in pages of at most 8 KB; a cell longer than
  4,000 characters is clipped, and `source_offset` reads the rest of that one cell.
  `notebook_edit` replaces, inserts or deletes one cell, found by id or index, or clears
  outputs; an edited code cell loses its outputs. A notebook in Jupyter's format (or
  written by `json.dumps` with indent 1, 2 or 4) stays byte-identical outside the edited
  cell; any other notebook is rewritten in Jupyter's format, and the card says so. It
  works on notebooks up to 1 MB whatever the write limit, with one approval card and a
  checkpoint. Diagnostics also report merge-conflict markers inside notebook cells.
  **Changed files:** the conversation keeps remembering the files it read or wrote.
  Before each model request the app checks the 200 most recent of these files and, if
  any changed or were deleted (your editor saves, an Undo, a command), adds one line at
  the end of the system message naming up to 10 of them. A deleted file is named once.
  The system message keeps its stable parts first and puts the folder grants, the current
  date and this line last. On Claude endpoints the stable part is cached on its own (see
  **Prompt caching**), so when one of these last lines changes, the next request still reads
  the stable part from the cache but writes the conversation after it again. It
  gives the local date and time zone but not the time of day, so it changes once a day
  rather than once a minute; the model runs `date` when it needs the exact time (a chat
  without `run_command`, such as a `read_only` subagent, is not told to).
- **Request details**: expand the details below an assistant response or tool-only
  model request to inspect its endpoint, completion status, stop reason, and
  provider-reported token usage when available. Failed requests distinguish invalid
  requests, rate limits, output limits, network failures, and other error categories;
  an HTTP status is shown when returned. Interrupted requests remain distinguishable
  after restart, and older conversations without metadata still display normally.
  A request that got no usable response is sent again unchanged, with a notice that
  says why and how long it waits: an HTTP 429 rate limit, HTTP 502, 503 or 504 before
  the reply started, or a connection that failed before any response arrived. These
  share at most three retries per request. HTTP 500, a read timeout (the model may
  still be generating, and billing) and any failure after the reply started are never
  repeated; the turn ends with the error. If Databricks still rate limits the chat
  after the three retries, the app waits up to twice per message (the provider's
  `Retry-After` up to 2 minutes, otherwise until just after the next minute) and tries
  again, so a request makes at most six attempts. The notice shows the time, and
  **Stop** cancels the wait; once the wait is over the notice reads "Continued
  automatically at ..." (or that Stop ended it) and no longer mentions **Stop**. The
  notices of these waits come before the reply they held up, live and in the saved
  and exported conversation (see **Rate limits** under **Context and project
  instructions** for the limit a 429 names and the wait shared by chats). Turn the waits off with **Settings → Continue
  automatically after rate limits**. When a reply's tool calls had invalid or
  incomplete arguments, or the reply ended without a finish reason, none of its tools
  run, and the app asks the model in a hidden message to issue them again or to
  continue: up to twice in a row, with a notice each time. A reply stopped by the
  provider's content filter, an empty reply and a stream cut off mid-message are not
  retried; the turn ends with the error. A content filter stop says so, and no
  **Continue** button is offered for it.
  An error notice in the transcript shows a short reason (for Databricks HTTP and network
  errors a plain sentence such as "Databricks rejected the request (HTTP 400)." followed by
  the provider's message, and for a final rate-limit error its advice on the context budget);
  **Details** shows the full error text.
- **Other folders**: ask, for example, `Are there any CSV files in
  ~/Downloads?`. The agent can list, read and search absolute or `~/`
  paths. The first file-tool request outside the project opens a folder-access card.
  Approving it grants that directory (and its children) to this conversation, including
  after refresh/restart. File-edit approval still follows the selected mode.
  Declining does not grant access. Start a new conversation for a fresh set of grants.
  macOS can separately block protected folders such as Downloads, Desktop and Documents.
  If the tool reports “macOS denied access,” open **System Settings → Privacy &
  Security → Files and Folders**, enable the folder for the app that launches the
  Python server (such as Terminal or Codex), then restart the server. Chat permission
  modes cannot override macOS privacy controls.
- **Folder access** (above the chat input): enter `~/Downloads` or another folder,
  click **Check access**, then **Allow folder** when the server can read it. This
  grants access only to the current conversation; before the first message it creates
  that conversation for you. The check reads no file contents and does not itself
  grant access. Remove additional grants in the same dialog. When macOS blocks access,
  expand **Python used by this server** to see the actual executable and compare it
  with the permission you enabled. Browser folder approval does not grant macOS access
  to a separate Python process.
  Beside it, **Local** (hidden when the chat column is narrow) says the conversation runs on
  this computer; hover it for the explanation.
- **Stop**: cancel the current agent turn and its foreground command. Background
  jobs continue; stop them individually in Terminal. Approved writes and completed
  shell side effects are retained. Stop also cancels its subagents. Deleting a
  conversation removes its jobs, tasks, and file checkpoint copies; edited files stay
  on disk. Delete a chat with **More actions** → **Delete…**; one that has subagent chats asks first: **Delete with subagents**
  deletes them too (at every depth, each stopped first), and **Delete, keep subagents** keeps
  them as chats of their own (the API default, `DELETE /api/sessions/<id>?children=keep`);
  a subagent turn the deletion stopped is recorded as stopped.
- **Continue**: when the latest turn of an idle conversation stopped before it finished —
  at the **Agent steps** limit, at the output-token limit after the automatic retries,
  by **Stop**, or because the server restarted before its answer finished (not while it
  was only naming the chat or during a manual compaction) — a **Continue** button above
  the message box sends `Continue from where you stopped.` as an ordinary message. It
  is not shown while the conversation is busy, after a completed turn, or after other
  errors. Your draft is left as it is, and keyboard focus moves to the message box.
  It is also offered when the model's reply still could not be used after the hidden
  retries, and when Databricks still rate limited the chat after the automatic retries.
- **Edit, retry and rewind**: while a conversation is idle, each message shows its
  actions under it.
  - **Edit message** (pencil) replaces that message and everything after it with your
    edited text and sends it. **Also undo file changes made from this message on**
    rewinds the agent's file edits too. Ctrl+Enter (Cmd+Enter) sends, Esc cancels. The new
    text is checked like any message before anything changes: an `@` file the chat may not
    attach, or too many or too large files, is an error in the editor, and the conversation
    stays as it was. If the message still cannot be sent after the rewind, its text is put in
    the message box (a typed draft is kept) with the error.
  - **Retry response** on the last reply sends the same message again in place of
    everything from it on. It is offered only when that reply answers your latest
    message: when the latest message failed or was stopped before any reply, use **Edit
    message** or **Continue** instead. It is not offered when the latest turn included a
    message sent while the agent was working; use **Edit message** then.
  - **Rewind to here** opens a dialog: **Conversation and code**, **Conversation
    only** or **Code only**. It shows how many messages and replies go, whether the
    summary of earlier turns is dropped (when it covers messages after the point; the
    next message may compact again), and each file the agent edited from that message
    on: **Will be restored**, **Already as it was**, **Changed since the agent's last
    edit**, or **Also changed by something else between the agent's edits** (an editor
    save, an Undo, a Recovery restore, a subagent or a command wrote it between two
    agent edits). Any file in the last two states, or one the chat can no longer
    reach, refuses the whole file rewind and nothing is changed. A rewound file goes
    back, in one write, to its state before the agent's first edit in that range; each
    write keeps a reverse checkpoint, so **Agent tools → Checkpoints → Other file edits**
    can undo it. Only edits made through the file tools (`write_file`, `edit_file`,
    `insert_activity_log`, `move_path`, `delete_path`, `notebook_edit`) are rewound,
    including those of turns an earlier conversation rewind removed; changes made by
    commands or by subagents are not. After a conversation rewind the message you
    rewound to is put in an empty message box (a typed draft is kept) and keyboard focus
    moves there, and a notice above the message box links to the archived earlier version.
  - The conversation keeps its id, settings, grants, usage ledger, queued messages and
    waiting command output. The output of `!` commands that the removed message itself
    carried waits again (its chip shows above the box), so **Retry response** and **Edit
    message** send it with the message again, and after **Rewind to here** it goes with your
    next message. `@` files are read again when the message is sent. Each rewound turn's
    ending is kept: a stopped or cut-short turn still offers **Continue**, and a completed one
    can still be forked whole. The record of which files the agent read is cleared, so the
    agent reads files again before overwriting them. All of this needs an idle chat,
    including no waiting approval, question or plan card, and file rewinds also need the
    other chats and commands in the workspace to be idle.
  - **Fork from here** (on the last reply of each turn) opens a new chat with the
    conversation up to that reply; optionally give it a title and create it in a new
    Git worktree (a new branch from the chat's checkout at its last commit; uncommitted
    changes are not copied). Like the other actions, it needs an idle chat with no
    waiting approval, question or plan card.
  - **Stop marker**: when you press **Stop**, the reply's partial text is kept in the
    model history followed by `[The user stopped this response before it finished. Tool
    calls it was preparing did not run.]`, after any interrupted tool call gets its
    "Execution interrupted" result, so the next message, an export or a fork sees a
    complete history at once. A turn cut short by a shutdown or a restart gets `[This
    response was cut off when the app stopped. …]` instead. A Stop before the message
    reached the model history (for example while MCP servers start) adds nothing. The
    transcript marks the stop with a divider that names what was interrupted (**Stopped
    while replying**, **Stopped during an action** for a tool or a waiting card, or
    **Stopped** for a retry wait or context preparation; recorded as `stop_phase`
    `streaming`, `tools` or `waiting`).
- **Verify**: **Agent tools → Verify** sets a command for the project folder, for
  example `pytest -q`, with a time limit (10 to 3,600 seconds, default 600) and fix
  rounds (1 to 3, default 2). After a reply in which the agent changed files inside the
  project folder (edits in other folders do not count), the app runs it as the agent's
  `run_command` with an output limit of 1,000,000 bytes, so the permission mode and
  permission rules apply as to a command the agent chose: Manual, Auto and Accept edits
  show an approval card unless an allow rule for this chat, the project or all projects
  matches (Auto also runs it without a card when it is on the read-only list), Bypass runs
  it unless a deny or ask rule covers it, deny rules block it in every mode, and Don't ask
  blocks it with a notice unless a rule allows it. Plan mode, subagents and restricted tool
  profiles never run it. Hooks and the
  project-instruction checks apply as for any command, and **Stop** stops it. A notice
  reports the result. If it fails, the agent gets the last 3,000 characters of its
  output (credentials redacted, fenced and marked as data, not instructions) in a hidden
  message and another model request to fix it. When the output was longer than the
  limit, those are the last characters of the kept part, and the message and the notice
  say that the output was cut. If you decline the approval card, let it expire, or stop
  the command with **Stop job** in Terminal, there is no fix round and Verify does not
  run again in that turn. A note typed on a declined card is passed to the agent, which
  answers it in another model request when an agent step is left. If that request fails
  or is stopped, the hidden message stays in the model history with a short placeholder
  reply, so your next message (**Continue**, say) still carries the output or the note. After the last fix
  round, or when no agent step is left, the turn ends with a notice; the full output is in the command
  card. When the agent answers a fix round without changing files (it fixed nothing, or fixed it
  with a command only), Verify does not run again and a notice says the last run failed. Each fix round uses an agent step. Leave the command empty to turn Verify off.
  The settings are kept in `<state dir>/projects.json` for each resolved workspace path
  (a worktree is its own project), never in the repository, and are not part of
  conversation exports.
- **Task checklist**: when the conversation has tasks (from the model's `create_task` /
  `update_task` or **Agent tools → Tasks**), a **Tasks 2/5 done** bar above the message
  box, under the changes bar, shows the progress and the task in progress. Expand it to
  list every task with its status; long titles wrap. It updates as tasks change; cancelled tasks are listed
  but not counted.
- **Code in replies**: once a reply is complete, its fenced code blocks are syntax
  highlighted (highlight.js, common languages). A block tagged with a language is
  highlighted in that language; an untagged block is guessed, up to 20 KB. **Copy code**
  copies a block's exact text.
- **Open files from replies**: once a reply is complete, file paths in it become links:
  an inline code span that is a file path with an extension (`src/app.py`,
  `src/app.py:12`, `app.py:12:5`), and in the text a path with a slash (`docs/guide.md`)
  or a file name followed by a line number (`app.py:12`). A name without a slash is
  linked only with a common source or text file extension (`py`, `js`, `ts`, `json`,
  `md`, `yaml`, `toml`, `txt`, `html`, `css`, `sh`, `sql`, `go`, `rs`, `java`, `c`, `h`
  and similar), so identifiers such as `os.path.join` or `console.log` and host names
  such as `db.example.com:5432` stay text. Web addresses, code blocks and
  Thinking blocks are not linked. A link opens the file in **Workspace / Files** with
  that line selected and scrolled into view; the status bar shows **Line 12**. Expanded
  tool cards of file tools (`read_file`, `write_file`, `edit_file`, `insert_activity_log`,
  the notebook tools, and `move_path`'s destination) have **Open file**, at `read_file`'s
  start line. Opening goes through the same checks as Files: a folder outside the project
  needs folder access in that conversation, and credential files and generated folders
  are refused with the reason. **Open in VS Code** in the editor toolbar opens the file at
  that line through a `vscode://file/<absolute path>:<line>` link; nothing happens when VS
  Code is not installed. A path inside the project is opened by its project path; when a
  reply names a file that does not exist as written, the editor opens the one project file
  the name most likely means (a path starting with the project's own folder name, or the
  folders above it from the repository root; in a worktree chat, the worktree's copy of a
  file named by the source project's path, which is always preferred over the source
  project's own file) and says which file it opened. Otherwise it reports that the file does
  not exist in the project folder. A file with unkept agent changes that cannot be read
  opens its review.
- **Copy and export a conversation**: **Agent tools → Export → Export Markdown**
  downloads the conversation as a Markdown transcript (`<date>-<title>-<id>.md`; a secret
  in the title becomes `redacted` in the file name), and
  **Copy conversation** copies the same text. It is the transcript the
  `scripts/conversation_transcript.py` script writes (see **Agent tools: MCP, skills,
  and hooks**): messages and replies in full, one line per tool call with its main
  input, `ask_user` questions with your answers, `submit_plan` plans in full with how you
  reviewed them and your feedback, and command output shortened to 600 characters. Common secret shapes and the
  configured Databricks token are replaced with `[REDACTED]`; review it before sharing.
  Both work during a response and show what has been saved so far; their result is shown
  right under the two buttons.

### Conversation storage

The default state directory is `local-agent-workspace/.local/`, overridable with
`LOCAL_AGENT_STATE_DIR` in `.env`. The directory is created at startup. Changing
the value does not move existing data: stop the server before copying state to a
new location, update `.env`, and then restart the app. File tools never reach the
state directory, wherever it is; keep a custom one outside the selected project
workspace anyway, because commands run in the workspace can still reach it.

- `conversations/<conversation-id>.jsonl`: one complete, private file per chat.
  Records include conversation metadata, user and assistant events, reasoning and
  request usage, tool calls and results, and the exact Databricks message/tool
  history (`wire`). Commands associated with the chat are included as sanitized job
  records with their command, state, limits, exit status, and bounded retained output.
  Files are atomically replaced as current-state snapshots rather than appended
  indefinitely while an answer streams. A subagent has a file of its own; its header names
  its parent (`parent_session_id`, `parent_event_id`), and the parent's header lists its
  subagents with their task, type, status and file (see **Linked transcripts** under
  **Agent tools: tasks, subagents, and activity**).
- `conversations/<conversation-id>.state.json`: the chat's "Allow for this chat" rules and
  the size and modification time of the files the agent last read or wrote (paths only, no
  contents). It sits beside the JSONL file so earlier versions of the app can still open the
  chat; it is never exported, and a missing or unreadable one counts as empty.
- `conversations.sqlite3`: operational state only. Its `jobs` table stores command
  metadata, states, and bounded output for live control and restart recovery; terminal
  job snapshots are also mirrored into the owning chat JSONL. Chat messages, assistant
  replies, model/tool history, and inference ledgers are not stored in SQLite.
  At most four jobs run at once; the latest 100 finished jobs are retained across
  all conversations/workspaces. Same titles never merge sessions. Browser storage
  is not the source of conversation history.
- `tasks`, `checkpoints`, and `worktrees` tables: structured task state, file recovery
  copies, and app-owned worktree metadata. File recovery copies contain original
  file bytes, so protect this local database like the project files themselves.
- `settings.json`: app settings, separate from conversation history.
- `extensions.json`: explicitly configured MCP servers and lifecycle hooks.
- `worktrees/`: actual app-created Git worktrees; copying the database alone does
  not migrate these checkouts or their Git registrations.

A conversation file that cannot be read (empty, not JSON, a damaged header or record,
or an ID that does not match the file name) does not stop the app. It is skipped, the
other conversations list and open as usual, and a notice at the top of the page names
the file and the reason; the reason never quotes the file's contents. The app never
deletes, moves or rewrites such a file; a command job of that conversation stays in
`conversations.sqlite3` and is not copied into the file. Repair or remove it with the
server stopped; it is listed again once it can be read. Requesting it through the API
reports the problem (HTTP 400) instead of a server error, and the page shows that message
instead of trying to reconnect.

Startup after a clean shutdown reads only the first line (the header) of each conversation
file. When the server stops normally, it writes `conversations/.clean-shutdown` after every
response, compaction and command job has stopped; the next start removes that marker first
and then loads in full only the chats whose header shows a turn that did not finish, plus
subagent chats whose delegation is still open or whose parent was just repaired. Finished
command jobs were copied into their chats when they ended, so they are not copied again (only
after a crash are they copied again, which loads each owning chat). Without the
marker (after a crash or a kill, a failed shutdown, or on a new or older state folder) every
conversation is loaded and repaired as before, which takes longer with many chats. One
consequence: after a clean shutdown, a file whose header is readable but whose later records
are damaged stays listed until you open it; opening it then reports the problem (HTTP 400) and
hides it like a file found unreadable at startup. Pin, archive and tags are fields of the
header (`pinned`, `archived`, `tags`), saved without changing the chat's update time.

New sessions have empty message history and fresh permissions.
Project files are shared on disk when chats select the same workspace; starting a
new chat does not copy or isolate the project filesystem. On first startup after
this storage change, legacy `sessions` rows are durably migrated to individual
JSONL files before those chat tables are removed. Stop the server before copying
state so JSONL replacements and SQLite WAL files are settled.

### Provider-reported usage

Request details retain the latest valid usage snapshot returned for each streamed
chat-model request: input, output, total, cache read/write, and reasoning tokens
when supplied. **Agent tools → Usage** adds a per-conversation graph and durable
attempt ledger for agent, title, compaction, and retry calls. Repeated stream
snapshots are not added together. Missing provider fields remain unavailable; usage
received before an interruption may be partial, not a final count.

For recognized standard pay-per-token endpoints, the Usage section calculates an
estimated DBU cost from published input, output, cache-read, and cache-write rates.
The rates are those of 2026-10-13: Claude Opus 5.5 at 57.143 DBU per million input tokens,
285.714 output, 2.857 cache read and 71.429 cache write (the 5-minute rate; 1-hour cache writes
at 114.286 cannot be told apart in the usage), and Claude Opus 4.8 at 71.429, 357.143, 7.143 and
89.286.
It does not estimate missing token counts. This is not an account-wide report or an
invoice: regional uplifts, provisioned endpoints, negotiated pricing, credits, and
delayed billing adjustments can differ. Older chats that predate the ledger show
visible agent-call totals as incomplete because title, summary, and retry attempts
cannot be reconstructed. A delegated child's requests stay in its own conversation's
usage (its parent's spending limit counts them).
Databricks responses may omit usage.
See the [Databricks API reference](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/api-reference)
for the provider's usage fields. The context meter below shows the input Databricks
reported for the last request next to the app's own pre-request estimate, and
reported input tokens calibrate that estimate (see below).

Usage is kept in three input buckets: uncached input, cache reads and cache writes.
Claude-style usage reports them separately. OpenAI-style usage counts cached tokens
inside `prompt_tokens` (`prompt_tokens_details.cached_tokens`); the app then records
`prompt_tokens` minus the cached count as uncached input, so totals, the DBU estimate
and the estimate scale do not count the cache twice (a cached count larger than
`prompt_tokens` is left as reported). Claude endpoints on Databricks report the cache counts
as top-level `cache_read_input_tokens` and `cache_creation_input_tokens`; that their
`prompt_tokens` then excludes them, as the app assumes, is still to be confirmed by one cached
request (see **Prompt caching**). Ledger records saved before
this change keep the counts they were saved with; those that used OpenAI-style cached
tokens stay double-counted, because they hold only the combined input count.
The Usage section starts with a **Last model input** row: the reported input tokens of
the conversation's latest agent request (cache included), its share of the input
budget and of the model's window when known, and the app's estimate for the same
request ("No reported input yet." before the first report). The call table has a
**Total input** column (uncached input plus cache reads and writes), and **Request
details** under a reply show **Input incl. cache**.

Every chat, title, and summary call is sent to
`DBRICKS_URL/serving-endpoints/<selected endpoint>/invocations`; the app does not
call Anthropic directly. A blank Serving endpoint health chart is not a request
ledger and does not show that traffic bypassed Databricks. Use the app's Usage
section for immediate per-conversation telemetry,
[`system.ai_gateway.usage`](https://docs.databricks.com/aws/en/ai-gateway/usage-tracking)
for workspace/account observability, and [`system.billing.usage`](https://docs.databricks.com/aws/en/ai-gateway/cost-observability)
for billable usage. Those surfaces have different scopes and update timing.

**Correlating usage with the conversation file.** Each Usage row is one
`inference_call` record in `<state dir>/conversations/<conversation-id>.jsonl`.
An agent call's `event_id` names the assistant reply it produced; the tool events
that follow that reply are the tools it called; a retried request repeats the
`event_id` with the next `attempt`. Title and compaction calls have no `event_id`.
**Export CSV** in the Usage section downloads one row per call with local and UTC
times, tokens, the DBU estimate, the reply excerpt, the tools it called, and the
IDs below. The same rows are available offline with
`python3 scripts/usage_report.py <conversation file>.jsonl [--csv usage.csv]`
(Python 3 only; copy the file first if the app is running). Excerpts pass through
the same credential redaction as other exports; cells that a spreadsheet would run
as formulas are prefixed with `'`.

Every request carries a `Databricks-Ai-Gateway-Request-Tags` header with
`local_agent_call_id` (the ledger record id), `local_agent_conversation`, and
`local_agent_purpose`. Databricks documents storing these tags in the
`request_tags` column of `system.ai_gateway.usage`, so where your endpoint's usage
is recorded there you can join exactly:

```sql
SELECT event_time, request_tags['local_agent_call_id'] AS call_id,
       input_tokens, output_tokens, status_code
FROM system.ai_gateway.usage
WHERE request_tags['local_agent_conversation'] = '<conversation id>'
ORDER BY event_time;
```

Whether a given `/serving-endpoints/.../invocations` endpoint is tracked in that
table depends on its Databricks gateway configuration; the legacy
`system.serving.endpoint_usage` table has no field this app sets (the Foundation
Model API does not document `client_request_id` or `usage_context` for chat
requests, so the app does not send them). Match there on endpoint, `request_time`
within a few seconds of `started_utc`, and the exact token counts. The ledger also
keeps each response's `id` as `response_id` (Databricks encrypts it) for support
requests.
For chat calls, an HTTP 429 `REQUEST_LIMIT_EXCEEDED` response, HTTP 502, 503 or 504,
and a connection failure, received before streaming begins, share at most three
retries per request (see **Request details**). The app honors numeric or HTTP-date
`Retry-After` headers and root/nested JSON `retry_after` values, capped at 60 seconds;
otherwise it waits 5, 15, then 40 seconds after a rate limit and 2, 5, then 15 seconds
after the others. Context-summary calls are not streamed and can run for minutes: they
retry the same rate limits and gateway errors, but of connection failures only those
before the request was sent (connect, pool and write failures), never a read failure
that may follow an accepted, billed request. A failure after streamed content begins
is not retried because repeating a partial response could be unsafe.

The ledger and the CSV (the `retry_reason` column, after `attempt`) record why the app
sent a request automatically: `rate_limit`, `server_error` or `network` for a repeat
of the same request, `rate_limit_wait` after an automatic wait, `output_limit`,
`invalid_tool_arguments` or `incomplete_response` for the request after a hidden
continuation, `verify` for a Verify fix round, `reasoning_rejected` for the resend
without reasoning fields (see **Reasoning effort and thinking**), and `cache_rejected` for the resend
with fewer cache markers (see **Prompt caching**). The Usage section's **Automatic
retries** tile counts these calls; **429 responses** counts rate-limited calls. The CSV's
`effort` column (after `max_output_tokens`) and the call table name the reasoning effort a
request asked for; it is empty for a request at Default that sent no reasoning fields.
`effort_sent` is the level that went out: GPT OSS gets `high` for Extra high and Max, and
`none` means the reasoning fields were left out after a rejection.

### Spending limit

A conversation can have a **spending limit**: a soft cap on its estimated DBU (the estimate
above, from list prices), counting its subagents too. **Settings → Agent → Spending limit per
conversation** sets the default for every chat without its own (0, the default, means no
limit); **Agent tools → Usage** shows what the chat has used against its limit, with the share
from subagents, and sets the chat's own limit (0 = none for this chat) or returns it to the
Settings default. It can be changed while a reply runs. Forks and imports keep a chat's own limit
(their spend starts again from their own calls).

The app checks the limit before each agent model request, before any compaction for it. At 80%
it adds one notice; once the chat has used the limit, it does not send the next request and
ends the turn with a notice (a subagent ends with an error its parent sees), the chat offers
**Continue**, and desktop notifications say "Reached the spending limit". Raise the limit, then
continue. A reply that arrives over the limit ends the turn the same way, without Verify or stop
hooks (they could only continue with a refused request; a subagent's final reply still reaches
its parent), and messages queued while it ran stay queued. It is a soft cap: one response can pass it before the next request is stopped.
Cache reads and writes count at their own rates (see **Prompt caching**).
Calls to endpoints without price data count as nothing and are listed as not counted; title
and compaction requests are counted but never stopped. A subagent follows its parent's limit
(a subagent chat without its parent, such as a copy of one, has its own), and its spend is kept
in the parent's subagents index after each of its turns, also when it is stopped, deleted or
continued by you (subagents that ended before this existed are not counted). The offline simulator reports usage, so
`databricks-claude-opus-5-5` there can be used to try a small limit.

### Reasoning effort and thinking

Each conversation has a **reasoning effort**, chosen in the picker beside the model picker
under the message box: **Default**, **Fast**, **Medium**, **High**, **Extra high** or **Max**.
A new conversation starts at **Settings → Reasoning → Default effort for new chats**
(Default unless you change it), and keeps its own level in its file like its model; forks and
imports keep it, and a conversation saved before this existed uses the Settings default. A
change applies from the agent's next model request, also while a response runs (a notice
says so) and for a running subagent whose type sets no effort of its own (see **Custom agents**
under **Agent tools: tasks, subagents, and activity**). The picker names what Default means where it is known,
for example **Default (Medium)**. Levels above Default make the model think longer, which
uses more output tokens (thinking counts toward **Max output tokens**) and costs more.

What agent requests to each endpoint carry (top-level fields of the chat-completions body):

| Endpoint | Default | Fast … Max |
| --- | --- | --- |
| Claude Opus 5.5 (`databricks-claude-opus-5-5`) | `thinking: {"type": "adaptive", "display": "summarized"}` (it always thinks; its own default is medium) | `thinking` as for Default and `output_config: {"effort": "low"…"max"}` |
| Claude Opus 4.8 (`databricks-claude-opus-4-8`) | nothing (no thinking) | the same as Opus 5.5 (it thinks only when `thinking` is sent) |
| GPT OSS 120B and 20B | nothing (its default is medium) | `reasoning_effort: "low"`, `"medium"` or `"high"`; Extra high and Max are sent as `"high"` (the picker says so) |
| Other endpoints | nothing | by the request format below; offered unverified |

**Settings → Reasoning → Request format** chooses how requests ask for an effort:
**Automatic (per endpoint)** (the table above; for an endpoint without a profile the format is
guessed from its name: `claude` → Anthropic fields, `gpt-oss`, `gpt-5`, `o1`, `o3`, `o4` →
OpenAI field, anything else → none), **Anthropic fields** (`thinking` and `output_config.effort`),
**OpenAI field** (`reasoning_effort`) or **Don't send reasoning fields**. Settings shows the
exact JSON the endpoint and level in the form add to agent requests, computed by the same code
that builds them. Titles, `/btw` side questions and compaction requests never carry reasoning
fields: they use the endpoint's default, which their output allowance is sized for.

Databricks documents the Anthropic fields for its Anthropic Messages route; whether its
chat-completions route, which this app uses, passes them on has not been checked against a
live endpoint. So when Databricks answers HTTP 400 before any reply streams and the error
names a reasoning field the request added (`thinking`, `output_config`, `effort`,
`reasoning_effort`, `display` or `budget_tokens`), the app sends that request once more
without reasoning fields (a rejected request is not billed), posts one notice naming the
field and **Settings → Reasoning → Request format**, and sends no reasoning fields to that
endpoint in that format until the app restarts or you save Settings. Both attempts are in the
usage ledger; the second has `retry_reason` `reasoning_rejected`. A 400 that names none of
them is not sent again without reasoning fields; on Claude endpoints the cache-marker fallback
(see **Prompt caching**) may still send it again with fewer cache markers before the error is shown.

**Show thinking summaries** (on by default) adds `"display": "summarized"` to `thinking`.
Claude Opus 5.5 omits its thinking text unless asked, so without it no thinking is shown; the
setting changes what is shown, not how much the model thinks or what it costs. A reply's
thinking appears in a **Thinking** block at its top: open with the live text and "Thinking… 12 s"
while the model thinks, then collapsed to "Thought for 12 s" when the answer or a tool call
starts or the response ends (a block you opened or closed yourself stays as you left it). The
duration is saved with the reply. An endpoint that reported reasoning tokens or thinking
without text gets one line instead, "Thought for N s · the endpoint did not return a summary".
The app shows thinking text only as the endpoint returns it (redacted, up to 12,000
characters) and never sends earlier thinking back to the model. **Request details** under a
reply show **Effort**, for example "High (output_config.effort)", and the reasoning fields
sent.

### Prompt caching

Agent and subagent requests to Databricks-hosted Claude endpoints (Claude Opus 5.5 and Opus 4.8)
carry prompt-cache markers (`"cache_control": {"type": "ephemeral"}`), so Databricks can bill
input that repeats an earlier request at the cache-read rate. Each agent step sends the whole
conversation again, so most of a step's input is the step before it: cache reads cost 2.857 DBU
per million tokens on Claude Opus 5.5 instead of 57.143 for uncached input, and cache writes
71.429 (25% more than uncached input). Titles, `/btw` side questions and compaction requests carry
no markers (they share no start with the next request, so a marker would only add the write
cost), and other endpoints (GPT OSS, endpoints without a profile) get none.

Each agent request gets two markers: one where the system message's stable part ends (before the
folder grants, the date and the step notices; see **Changed files** above) and one on its last
message (your message or the last tool result). Each request then writes the conversation up to
its end, and the next step reads it back. The markers are added to the request body only: the
context meter's estimate, the estimate scale and compaction see the request without them; the
request size and the 4 MB check count the bytes sent. A change in the stable part (another
permission mode, a selected skill, edited project or custom instructions) makes the next request
write everything again.

A cache entry expires 5 minutes after it was last written or read. A step that comes more than 5
minutes after the one before it (an approval card left waiting, a long command, a pause before your
next message) writes the conversation again at the write rate, and a request that no other follows
within 5 minutes paid the write cost for nothing: a single short question costs up to 25% more input
than without caching.

Databricks documents the markers on message content parts and on assistant tool calls; it does not
document them for the system message or a tool result, which need list content for those roles.
So when Databricks answers HTTP 400 before any reply streams to a request with markers, whatever
the error says, the app sends it again with fewer: first only the documented forms (your last
message, or the last tool call before its results), then none. Once Databricks accepts one, a
notice says so above the reply, and requests to that endpoint use that level until the app
restarts or you save Settings; if even the request without markers fails, its error is shown and
nothing is remembered. These resends do not use up the retries after rate limits and server errors,
and a rejected reasoning field (see **Reasoning effort and thinking**) is handled first. Each resend
has `retry_reason` `cache_rejected` in the usage ledger, and each agent call to a Claude endpoint
records the markers it was sent with as `prompt_cache` (`full`, `messages` or `off`).

**Settings → Model and context → Prompt caching** is **Automatic (Claude endpoints)** (the
default) or **Off**, which sends no markers at all.

How Databricks counts `prompt_tokens` while the cache is in use is still to be confirmed by one
cached request: the context meter, the estimate scale and the DBU estimate assume it excludes the
cache reads and writes, which Databricks reports as `cache_read_input_tokens` and
`cache_creation_input_tokens`. To help check it, the ledger in the conversation file keeps, per
call, the key names of the usage object Databricks sent (`usage_keys`, never values), the shape
that carried the model's reasoning (`reasoning_shape`) and whether a reasoning signature came
(`reasoning_signature`, never the signature), and for an agent request whose system message's
changing part differed from the chat's previous one, `system_tail_changed` (the conversation after
it was written to the cache again). They are not CSV columns.

### Context and project instructions

**Settings → Context budget (tokens)** and **Max output tokens** default to the endpoint's
profile in `backend/local_agent/model_profiles.py`: 200,000 and 16,384 for Claude Opus 5.5
(`databricks-claude-opus-5-5`, the default model), and 131,072 and 8,192 for GPT OSS 120B and
for endpoints without a profile. Explicit saved values, including 32,768 and the former 131,000
default, are preserved. Budgets still at the old model's defaults follow a change of the
default model (in Settings or the composer's model picker) to the new model's defaults; a value
you changed stays, and a default that would not fit with the other budget (Max output tokens
must stay below the context budget minus 2,048) is not followed. A chat whose own endpoint differs from the Settings model uses that
endpoint's defaults while Settings is at the Settings model's defaults, and your values when
you changed them.
The allowed range is 16,384–1,048,576; these are app configuration bounds, not a
claim that every endpoint supports that range or the default. Set the budget at
or below your endpoint's actual total context limit; the app knows the limits only of the
endpoints in its profile table. Requests to such an endpoint are capped at its maxima (Opus
5.5: 1,000,000 context and 128,000 output tokens; GPT OSS 120B: 131,072 context), and the
chat shows a notice once that names the capped values (again only when they change). Changes apply on the next
turn, including existing conversations. The configured total includes space for both input and
reply. **Max output tokens** accepts 1,024–131,072, but must be below the context budget minus
the 2,048-token safety margin. The output setting is also the reply reserve, so the Opus 5.5
defaults leave an estimated input budget of 181,568 tokens (120,832 at 131,072 and 8,192).

While you edit these fields, Settings shows warnings from the profile (they never block saving)
and, for an endpoint with recommended values, a **Use … defaults** button, which reads **Using …
defaults** while the form holds them. When the endpoint
field changes, budgets still at the saved model's defaults change to the new endpoint's defaults
in the form, as Save would store them, and the estimated input budget also names what requests
to an endpoint with lower maxima get. The numbers are
Databricks's documented pay-per-token defaults for the Enterprise tier, also partner models'
quota Tier 1 ([limits page](https://docs.databricks.com/gcp/en/machine-learning/foundation-model-apis/limits),
checked 2026-10-13): Opus 5.5, Opus 4.8 and GPT OSS 120B each allow 1,000,000 input tokens
(ITPM) and 100,000 output tokens (OTPM) per minute. Your workspace's limits may differ.

- Databricks reserves Max output tokens against OTPM when each request starts. Above 100,000 a
  request is rejected with HTTP 429 before it starts (an error in Settings). Above 20,000 four
  requests at once (a chat and up to three subagents, or several chats) can be rejected; the
  app then waits and retries. 20,000 keeps one fifth of OTPM free; at 16,384, four requests
  reserve 65,536.
- Each agent step sends up to the whole context budget. Above 250,000 tokens, four full steps
  in one minute reach ITPM, so long turns and subagents wait on rate limits, and every step
  costs more (a note in Settings). You can raise Opus 5.5 to 1,000,000.
- Opus 5.5 always thinks before it answers, and its thinking counts toward Max output tokens.
  Below 16,384, complex steps and large file writes can stop at the limit (a note in Settings).
  The system message tells the model so. Title and `/btw` requests add 4,096 tokens for
  thinking (a title then asks for up to 5,120).

Output-limit responses can continue automatically in smaller steps up to the bounded retry limit.
The meter estimates the last prepared model input using roughly one token per
three ASCII bytes, conservatively counting non-ASCII UTF-8 bytes, plus framing
overhead. This heuristic varies from the actual model tokenizer and is not a
guaranteed upper bound, provider usage, or billing data. Older saved byte-based
meters refresh on the next request.
Each agent and summary request records this unscaled estimate in the usage ledger.
When Databricks reports input tokens, later estimates for the same conversation and
model are multiplied by the ratio of reported to estimated input tokens across up to
the five most recent completed requests of at least 1,000 estimated tokens (cache
read/write tokens are added to input tokens). The scale only raises the heuristic,
never lowers it, and is capped at 2×. In one observed code-heavy chat, Claude
reported about 26% more input tokens than the heuristic; a scale of about 1.26 makes
automatic compaction start before the real request outgrows the context budget.
Context details show the applied scale. Before a model has reported any counts in a
conversation, the estimate uses a starting scale: 1.3 for `databricks-claude-opus-5-5`
(newer Claude tokenizers count up to about 1.35 times as many tokens as older ones),
and the unscaled heuristic for every other endpoint, including
`databricks-claude-opus-4-8`. The first reports replace it.
Expanded context details attribute that same estimate to system/project
instructions (including selected skills), tool definitions, conversation messages
and tool results, the retained summary, and request framing/rounding overhead.
The categories add up to the displayed estimate; they are not tokenizer-derived
measurements.

**Settings → Auto-compact at (% of input budget)** (default 90, 50–100) sets when
automatic compaction runs: when the next request's estimate passes that share of the
input budget (the budget left after the reply and safety reserves). With the default
settings (Claude Opus 5.5 at 200,000 and 16,384) that is about 163,411 tokens; at 131,072 and 8,192 it is
about 108,748. Lower values compact sooner and keep requests
smaller; 100 waits until the input budget is full. Compaction looks for a cut that
brings the request under the threshold. When no cut does (for example a large first
request, or a large request after a few short turns) and the request still fits the
input budget, nothing is summarized: the request is sent as it is and the context
details say so. Only a request over the input budget, or a manual compaction, falls
back to a cut that merely fits the budget, so compaction never runs a billed summary
request that leaves the request over the threshold.

**Last model input** shows what Databricks reported for the latest agent request
once a report arrives: "Last model input · 10,000 tokens · 5.5%", where the
percentage is of the input budget (of the default 181,568-token input budget), and a bar with a mark at the auto-compact
threshold. The reported input is uncached input plus cache reads and writes: cached
tokens still occupy the context window; they are cheaper, not smaller. A stream that
stopped before it finished is marked "(partial)". Expanded, the meter lists the
reported input, output, the shares of the input budget, the context budget and the
model's own window (known for `databricks-claude-opus-5-5`, 1,000,000 tokens, and
`databricks-gpt-oss-120b`, 131,072; "Not known for this endpoint" otherwise), and how
far the app's estimate for the same request was from the reported count, with why
the two differ and the scale later estimates use. After a compaction, or in a
**Context preview**, the report describes the request before it, so it is headed
"Before compaction" and the collapsed meter shows the app's estimate (~N%) instead;
after the input budget changes, or once a request to another model has been prepared,
it is headed "Earlier request" in the same way. A model change or a Settings save
moves the meter to the budgets, auto-compact mark and next-request estimate of the
chat's next request right away (a running chat follows when its turn ends; budgets apply
from its next turn). When that changed the input budget, the collapsed meter reads
**Next request · ~N%**: the next request's estimate, rescaled to the chat's model, as a
share of the new input budget. When a turn ends, the details also show the
estimate for the next request before your next message. Title, compaction and side
question requests never change this report.

The output-token limit is separate from the configurable total context capacity
and the resulting input budget. Raising it reduces the available input budget and
works only when the selected endpoint supports the requested value. If a response
ends at that limit, none of that response's tool calls run. When another configured
agent step is available, the app automatically retries in smaller steps up to twice;
requests recovered this way are recorded as interrupted rather than terminal errors.
The full partial response remains in the visible event, while model history keeps a
small omission marker and a hidden app-generated user continuation so role ordering
survives later turns and restarts. The hidden continuation is not shown as a user chat
event. If interruption leaves that internal continuation unanswered, the next real user
turn removes it before sending the new prompt. A valid tool batch resets this
consecutive-retry allowance. After two automatic retries, or when no configured step
remains, the app stops with an explicit error so the limit cannot create an unbounded loop.
Malformed tool arguments still stop immediately. A call streamed with no argument
text, which is how Claude on Databricks sends a tool that takes no input such as
`list_skills`, means `{}`. Existing malformed tool
exchanges are excluded from subsequent model requests while their original
history and recorded outcomes remain saved; the conversation can resume without
deleting it. This does not guarantee that the model can finish any size of output
in one response.
Incoming SSE events are capped at 1 MiB and accumulated tool arguments at 512 KiB
per response, across at most 64 calls. Oversized or unfinished frames fail before
any tool in that response executes; the error is recorded without saving broken
tool calls into model history.

When older turns are compacted, the same endpoint summarizes them in bounded requests.
The summarizer reads a shortened copy of the archived messages; the saved history
never changes. Tool results longer than a limit (3/32 of the summary request's input
room, at least 8,000 and at most 64,000 bytes, twice that inside one long request; at
the default 200,000-token context budget on Claude Opus 5.5 with handoffs on it is 16,290 bytes; at
131,072 tokens it is 10,596 bytes, or 9,828 on models that always think) keep
their start and end around a marker; a `read_file` page also keeps the definition
lines (`class`, `def`, `function` and similar) of its omitted middle with their line
numbers. File contents in `write_file`, `edit_file` and `notebook_edit` arguments keep
their first 600 bytes and a marker (the file on disk has the current content), and a
read whose file was later read again with the same arguments, or changed, moved or
deleted by a completed call, is replaced by a marker. The app's own hidden messages
(automatic retries, Verify results) are labelled as the app's, not the user's; the
note you write when declining Verify stays your words. Your messages are never
shortened, except for the @ file and ! output attachments in them.

A plain summary request (with handoffs off, and in subagents) may use 4,096 output tokens,
or a third of the summary byte limit on large budgets: 5,043 at the default 200,000 and
16,384 on Claude Opus 5.5, plus the thinking allowance described below, 13,235 in all.
With handoffs on (the default), that request writes the handoff instead; see below. Summary chunks use that independent
reply reserve instead of the main response reserve, so increasing the main output
setting does not multiply the number of compaction requests. The summary uses these
sections, most important first, so a reply cut off at its limit loses the least:
Goal and current status; Open tasks and next steps; User requirements, preferences and
constraints (with your exact words); Decisions and reasons; Errors, causes and fixes;
Files read and changed; Commands and results. A conversation long enough to need
several summary requests passes each request's summary on whole to the next one and
condenses only once, at the end. The retained summary may use about an eighth of the
input budget, between 3,500 and 12,000 UTF-8 bytes, and up to 32,000 bytes on large
budgets (a twelfth of the input budget): 15,130 bytes with the default settings, 12,000 at
131,072 and 8,192. That byte cap is not a token count. A summary that comes back longer is not discarded, because
producing it may have required a large billed request: a small extra request,
containing only that summary, asks the model to condense it, and a second one
condenses that result if it is shorter but still too long. A condensing request may
use about one output token per two bytes of the limit (6,000 tokens for 12,000 bytes,
7,565 for the default 15,130; at least 4,096), so a slightly long reply is not cut off. A condensed summary that
lost the goal, open tasks or requirements section counts as failed. If condensing
fails or is still too long, the app shortens the least important sections first, then
reduces them to their headings; only then does it remove whole lines from the middle of
the first three sections, in proportion to their size, keeping each one's first and last
lines; a share that a short section cannot give goes to the others. Only if whole lines
are not enough (for example a summary without those sections) does it keep the start
and end of the summary. The summary's room is reserved in bytes,
but escapes and non-ASCII text count more toward the estimate: when the request after a
compaction is still over the input budget, the summary, already paid for, is shortened
(or condensed once) to fit rather than discarded, and the compaction notice and the meter
say why; only when even a summary of about 1,750 bytes would not fit does the turn stop
with the context-budget error. A plain summary cut off at its output limit is kept with a note. These
summary limits are separate from the main reply limit and total context budget.

Next to the summary, the model gets an **app record** that the app writes itself,
not the summarizer: your earlier requests in your own words (kept whole while the record
has room; when it is full the longest are shortened in the middle, the first request is
always kept, and the oldest after it give way), the task checklist when the compaction
ran with full task IDs, the files the conversation changed before that point, and the
files it read whose contents are no longer in its context, including those from earlier
compactions until they are read again. When these notes are longer than their half of the
record, whole lines go from the longest list, which then ends with how many more there are. Those files must be read again before a write
or edit: the read-before-write check refuses with "its earlier contents were
summarized out of your context" until they are. A file read or written after the
compaction point, or attached with @ to the request still in context, stays readable.
The record is part of the conversation's saved context state but is not exported,
so an imported conversation starts a new record at its next compaction. Your
requests are marked as your own words, whose rules and constraints still apply unless
you changed them later; the app's notes (tasks and files) are marked as data. Each
compaction adds one notice with its numbers: an automatic or in-turn compaction gives
the estimate before, the threshold, how many messages were summarized in how many
requests and the estimate after; a manual one gives the messages, the requests and
the estimate after. On models
that always think, such as `databricks-claude-opus-5-5`, thinking counts against
`max_tokens`, so every summary, condense and handoff request gets an extra
min(8,192, context budget / 8) output tokens for thinking.

**Settings → Save a detailed handoff at each compaction** is on by default. Each
automatic or manual compaction then asks the model for a detailed Markdown handoff of
the turns being summarized (the summary sections above, then work done in detail and
key facts and gotchas) instead of the short summary. The same request that already
carried those turns writes it, so no extra conversation is resent; it may use up to an
eighth of the context budget in output tokens (16,000 with the default budget), plus
the thinking allowance above on models that always think, and a non-streamed reply can
take a few minutes. That stays within Databricks' 597-second execution limit and
100,000 output tokens per minute for Claude Opus 5.5. A small follow-up request
condenses the handoff into the in-context summary. The app saves the handoff, with
the app record (redacted like the rest of the file) and a generated activity log of
every command and file, to
`<workspace>/handoffs/auto/<date>-<chat-title>-compaction-<N>.md`, adding a suffix
instead of overwriting an existing file. Each handoff starts with a Contents list
giving the line of each section. After compaction, the summary message lists the
newest five saved handoffs so the model can find the section it needs (from the Contents
list, with `outline_file` when it is offered, or with `search_files`) and read it with
`read_file` `start_line` when it needs details the summary omits. An @ file attachment
that was shortened says the same: its header names the `read_file` `start_line` where the
rest begins and, when available, `outline_file`. A handoff cut off at its output limit is still saved and used, with a note. If
the file cannot be written (for example, the folder resolves outside the workspace), a
notice explains it and compaction continues with the summary only. Subagent
conversations do not write compaction handoffs. Turn the setting off for the previous
summary-only compaction.

Input tokens per minute matter on large budgets: with a 1,000,000-token budget, one
near-full request uses all of Claude Opus 5.5's default 1,000,000 input tokens per
minute, so the next step can be rate limited. Lower **Auto-compact at** or the
context budget for multi-step work.

**Request size.** Databricks accepts at most 4 MB per request. The app encodes each
request body itself (compact UTF-8 JSON) and sends exactly those bytes, so it knows the
size it sends; the context details show "Request size: 245 KB of Databricks' 4 MB limit."
for the latest request. Compaction also runs when a request would pass 3.8 MB, a check
that costs nothing today because the token estimate already bounds the size (at most three
bytes per estimated token, about 3.1 MB at the 1,048,576-token maximum). When no
compaction brings such a request under 3.8 MB, the app still compacts at the latest point
that fits when that makes the request smaller. A request still over 4 MB is not sent: the turn ends with a `payload_too_large` error that suggests smaller
steps or a lower context budget, without a **Continue** button; a compaction or `/btw`
request over the limit is refused the same way. Agent requests wait up to 300 seconds for
the next streamed data (a large prefill with thinking can be silent for minutes); a
request that gets no data for that long ends with an error saying so, which suggests
sending a message to continue and, if it repeats, a lower context budget or Max output
tokens. Compaction and `/btw` requests, which are not streamed, wait up to 600 seconds. Databricks
stops a request after 597 seconds; when a stream ends early after about that long, a
notice (once per turn) says so and suggests lowering Max output tokens or the context
budget.

**Rate limits.** A Databricks 429 body names the limit it hit: `limit_type` (input or output
tokens per minute, queries per hour or second), `limit`, `current` and `retry_after`. Retry
notices name it, for example "Databricks rate limited the model request (output tokens per
minute (OTPM): limit 100,000, current 100,500). Retrying in 3 seconds (1 of 3).", and a
429 that names a limit is retried even when its message does not say "rate limit". Request
details show a **Rate limit** row for a final 429 (one that was not retried); the ledger
records its `limit_type`. When a final 429 names the input-tokens-per-minute limit, the
error adds the arithmetic: each agent step resends the whole conversation, so the context
budget times the steps per minute must stay under ITPM (at about 400,000 tokens per request,
1,000,000 ITPM allows about 2 requests per minute; keep the budget near 200,000 tokens for
about five steps a minute). Databricks reserves Max output tokens against OTPM, so a
request whose Max output tokens is above the reported OTPM limit can never start: the app
stops at once with an `output_tokens_over_limit` error naming the limit and a per-request
value for four requests at once, remembers that limit for an hour, and refuses further
requests to that endpoint, before any compaction, until Settings changes; no **Continue**
button is offered. A compaction request is sized by the context budget, not by Max output
tokens: under a reported OTPM limit its handoff or summary shrinks to fit (a first rejection
is resent once at the reported limit; Databricks does not bill it), and only when even the
smallest summary plus thinking room cannot fit does compaction stop. The thinking room is an
eighth of the context budget, up to 8,192 tokens, so the error names the context budget at which
compaction fits (at most 47,239 tokens under a 10,000 OTPM limit) or, when no budget is small
enough, asks for a higher limit from the workspace admin. When Databricks asks one chat to pause, other chats and compaction
requests to the same endpoint wait for that pause too, plus up to 3 seconds of random
jitter so they do not all retry at once, with a notice; a request with a smaller Max output
tokens may pass an output-tokens pause, and Stop cancels the wait. `/btw` does not wait: it
says when to ask again. These pauses and learned limits live in memory and start empty when
the app starts. The app does not pace requests by tokens per minute itself: limits vary by
tier and workspace, a rejected 429 is not billed, Databricks returns `retry_after`, and the
app's token counts are estimates.
The newest turn and its complete tool exchanges are retained verbatim;
the preceding turn is also retained when space allows. The app's hidden messages
inside a turn (automatic retries, Verify results) belong to that turn and never count
as a turn of their own. One request can outgrow the
budget by itself, for example when it reads many large files. The app then summarizes
that request's earlier tool exchanges and continues instead of stopping: the request
itself stays verbatim after the summary, followed by its latest exchanges up to half
the input budget, so later results have room before another compaction. The summary
(and handoff) request is asked to keep the facts the unfinished work needs from the
files and output read so far, and a notice reports the compaction. Manual **Compact
now** still keeps the latest turn intact. Summaries persist across
restarts, while the full display transcript and original model/tool history remain
in that conversation's JSONL file. Summary requests are additional billed inference. A failed or stopped
summary leaves the previous summary state intact and performs no tools. Summaries
can lose details; they are not an exact substitute for the original transcript.
A message you send while the agent works (**steering**) is part of the current turn, not a
turn of its own: compaction never cuts there, keeps it verbatim after the summary together
with the turn's request, and never summarizes the tool results the model has not answered
yet. A queued message that would not fit the context budget together with the latest tool
results waits (not paused) for a later step boundary, with one notice per turn. If no
boundary has room for it before the reply completes, it is sent as the next turn; after
Stop, an error or the step limit it waits like any queued message (see **Messages while the
agent works**).
If the request, its latest tool results, or project guidance alone are too large, the app
reports an error instead of silently cutting it. The error states the estimated
tokens needed and the input-budget arithmetic (context budget minus Max output
tokens minus the 2,048-token safety margin). When Max output tokens is above the
endpoint's default (16,384 for Opus 5.5, 8,192 otherwise), it suggests lowering that first: for example, 131,000 context with
121,000 reserved for output leaves only 7,952 input tokens. Otherwise, increase the
budget only within your endpoint's limit, or start a new conversation with a smaller request. There
is no model tokenizer integration. Retained job output is paginated; discarded
process output cannot be recovered.

Expand the context meter and choose **Compact now** while the conversation is
idle to summarize earlier turns before reaching the automatic threshold. An
optional preservation note highlights decisions or details to keep. This uses
additional model inference and can be cancelled with **Stop**. It does not run
tools, connect MCP servers, or rewrite the archived conversation. The resulting
**Context preview** uses the last saved tool definitions (built-ins only for
older sessions without that snapshot); definitions may change on the next turn.
The details open above the meter; Escape, a click elsewhere or moving focus away closes them.

**Settings** groups its fields under Project, Model and context, Reasoning, Agent, Subagents,
Commands and files, Notifications and Credentials; on a wide screen a list of these sections beside them
jumps to each one. Longer explanations wait behind **More**. **Settings → Databricks model
endpoint** lists the endpoints with built-in facts (label and name) and those your workspace
serves; **Other endpoint…** opens a field for any other name. **Save settings** stays at the
bottom while you scroll, and a failed save shows its reason just above it.

**Settings → Custom instructions** (up to 8,000 characters) are your own instructions for
every conversation in every project, subagents included. The system message places them
right after the app's own instructions, before the permission-mode text and the project
guidance (`AGENTS.md`, `CLAUDE.md`), which cannot override them. A change applies from the
next model request; unlike changed project guidance, it does not hold back a write or
command of a running response. **Loaded instructions** in the context details lists them
as **Custom instructions (Settings) · applies to all projects**, together with the project
files and the skill index (see **Agent tools: MCP, skills, and hooks**), each with its
estimated tokens.

At each model request the app rereads root `AGENTS.md`, then `CLAUDE.md`. File tools
also discover guidance in the target directory and its ancestors, from broader
to more specific scopes. These encountered scopes persist for the conversation;
their guidance applies only to their own directories. More specific rules refine
broader rules; user instructions and enforced tool permissions retain precedence.
`CLAUDE.md` is just a supported text filename: it does not enable Claude mode or
require the Claude SDK. README files, imports, skill files (beyond the skill index),
parent directories outside
the selected workspace, and external-folder guidance are not automatically loaded.
Shell commands receive root guidance and scopes already encountered by file tools;
the app does not infer new instruction scopes from arbitrary shell command text.

If a write or command encounters newly loaded or changed guidance, that action is
deferred until a new model request includes it. Guidance is checked again after
approval. The combined wrapped guidance is limited to 16,000 UTF-8 bytes; unreadable,
excluded, or oversized files are omitted whole, with warnings in the model input
and expandable context details. Warnings do not permanently block subsequent
actions. Context details list instruction filenames, directory scopes, estimated
per-file contributions, and omission reasons. File contributions are already part
of the system-instruction estimate; do not add them again to the overall total.

### Agent tools: recovery and worktrees

Open **Agent tools** in the header. Its sections are listed on the left (a scrolling row on a
narrow window) in two groups: **This chat** (Overview, Tasks, Subagents, Tools, Usage, Export)
and **Project** (Checkpoints, Verify, Worktrees, Extensions). The dialog opens on the section
you used last in this browser, or **Overview**: the chat's model and permission mode, how many
tools are on, its subagents running and finished, task progress, files with unreviewed changes,
and for the project its file checkpoints, Verify command, managed worktrees and MCP servers and
hooks, each with **Open** for its section. A value that cannot be loaded reads
**Could not load**; its section shows the error.

**Checkpoints** (called Recovery before) lists checkpoints created before
successful `write_file`, `edit_file`, `move_path`, `delete_path`, `notebook_edit`, and
editor saves. Preview the reverse diff,
then click **Restore checkpoint**. Restoring verifies the current file's content,
mode, type, location, and access scope; conflicting external changes are rejected.
A restore creates a reverse checkpoint, so it can be undone. Restoring creation
of a new file removes that file. Stop active workspace turns and commands first.
Checkpoints belong to the conversation (or the default-workspace scope for saves
before a chat exists). Deleting a conversation deletes its checkpoint copies.
New model-driven edits record their originating user turn. **Preview turn** shows
that turn's individual checkpoints, newest first, in pages of ten. Each file still
requires its own explicit restore and current-state validation; this is not an
atomic whole-turn undo. Earlier edits to the same file may no longer be restorable.
Manual editor saves, reverse checkpoints, and older unassociated records remain
under **Other file edits**.

Recovery covers regular UTF-8 files up to 1 MB. Recovery and the change review read
files up to 1 MB whatever the file write limit (Settings, 16–1,000 KB, 80 KB by
default), so lowering the limit never blocks reviewing or undoing an earlier, larger
edit; the limit applies only to new content. The checkpoint store is capped at 256 MiB:
past that, the oldest finished checkpoints are removed until it is under 192 MiB, so
the oldest edits may no longer be restorable here. Change-review baselines are separate
copies and are not part of this cap. New files get the mode your umask gives (usually
0644); existing files keep theirs. Recovery preserves existing bytes and
permissions, uses atomic replacement, and pins directories during writes. It does
not snapshot shell commands, MCP servers, hooks, Git operations, or whole folders.
It is not a substitute for Git/backups, nor a filesystem lock against unrelated
processes writing at the exact moment of replacement. Changes to newly created
parent directories are not rolled back. Reopen an editor file after a restore to
load the restored content; unsaved browser drafts are intentionally preserved.

**Worktrees** creates a fresh branch from the repository's committed HEAD. Dirty
source files stay in the original checkout and are not copied. **Open conversation**
starts a new Manual-mode conversation in that worktree. Removal only accepts
app-owned clean worktrees, rejects modified/untracked/ignored files and worktrees
used by any saved conversation (also one hidden as unreadable whose first line still names
the worktree) or running command, and keeps the branch and its
commits. Delete the worktree's conversations and choose another default workspace
before removal. Git hooks, filters, and fsmonitor are disabled for managed worktree
operations. This is deliberate workspace isolation, not automatic per-task copies.

**New chat in worktree** on the welcome screen creates a worktree on a new branch
(default `chat/<date>-<time>`, to the second) from the selected chat's project, or the default
workspace, and opens a chat in it. **Fork from here** can put a fork in a new worktree
too; forking a worktree chat starts the new branch from that worktree's last commit,
and a plain fork of a worktree chat stays in the same worktree. Worktree chats record
`worktree: {id, branch, source_workspace}`, where `source_workspace` is always the root
project, never another worktree. Permission rules saved for a project apply to that
folder only: a worktree chat does not inherit its source project's "Always allow in
this project" rules, because a worktree is a separate folder (least privilege). Rules
allowed inside a worktree chat apply to that worktree. An archived `(before rewind)` copy
of a worktree chat also uses the worktree: delete it too before removing the worktree.

Rewinding files (**Rewind to here**, or **Edit message** with **Also undo file
changes**) uses the same checkpoints: see **Edit, retry and rewind**. API:
`GET /api/sessions/<id>/rewind-preview?before_event_id=<user event id>` and
`POST /api/sessions/<id>/rewind` `{before_event_id, conversation, code, resend_text?}`
(409 with `{message, files}` when a file changed); `POST /api/sessions/<id>/fork`
with `{before_event_id?, title?, worktree_branch?}` forks from a point.

### Agent tools: MCP, skills, and hooks

**Extensions** edits the global explicit configuration. Saving enabled MCP servers
authorizes their startup for tool discovery on each turn; **Test saved configuration**
connects and lists tools without model inference. Servers can run over stdio
(command and argument array) or Streamable HTTP (URL). Use absolute executable and
script paths. Project files cannot automatically register or launch servers.
No custom environment, authentication headers, OAuth, resource browsing, or MCP
prompt workflow is implemented in this release. Keep credentials out of this JSON.
The test reports each server independently as connected, failed (with a diagnostic),
or disabled; disabled servers are not started. Results are a test snapshot, not a
persistent live-health monitor. Failed test connections cannot contribute stale tools.
Each server is tested independently; a collection of individually healthy servers
must still fit the aggregate tool/schema limits when a chat starts.

```json
{
  "servers": [
    {"id": "example", "name": "Example", "transport": "stdio",
     "command": "/absolute/path/to/server", "args": [], "enabled": false}
  ],
  "hooks": [
    {"id": "check", "event": "before_tool", "tools": ["write_file", "edit_file"], "command": "your-check-command",
     "timeout_seconds": 10, "enabled": false}
  ]
}
```

The model sees names such as `mcp__example__tool`. MCP calls require their own
approval in Manual, Auto, and Accept edits; Plan blocks them, Don't ask blocks them unless a
rule allows them, and Bypass permits them unless a deny or ask rule covers them. **Allow this
tool in this chat** approves one exact tool name for the rest of the chat; a saved rule for the
project or all projects can allow one tool or all tools of a server (see **Permission rules**). MCP servers and hook commands have the user's OS/network access, not the
file tools' folder restrictions. Credential environment variables are stripped
from local server/hook processes. Up to four servers, 32 tools, and 16 KB of tool
schemas are supported; each call has a 60-second deadline and bounded 8 KB result.
Connections close at turn end or cancellation. A discovery failure is reported,
not silently interpreted as an empty working integration.
An MCP result with `is_error=true` is displayed as an error even when the server
request itself succeeded. Its bounded diagnostic remains available to the model;
the app does not automatically retry the action.

Install workspace skills at `.agents/skills/<skill-id>/SKILL.md`. Skills in
`.claude/skills/<skill-id>/SKILL.md`, the layout another coding tool uses, are read too;
the app never writes there. Skills shipped with the app in `skills/<skill-id>/SKILL.md`
are available in every workspace. For one ID, `.agents/skills` wins over
`.claude/skills`, which wins over the shipped skill; a workspace entry takes the ID over
even when it fails to load. A `.claude/skills` folder that resolves outside the project
(for example a link to a shared folder), cannot be read, or has more than 100 entries or
30 skills, is skipped without an error, and the shipped skills stay available. Optional simple
frontmatter supplies `name` and `description`. Enable a discovered skill in the panel, use `/<skill-id>` as a word in a
message (type `/` at the start to choose from the list), begin a message with
`/skill <skill-id>`, or ask the
model to use its `list_skills` / `use_skill` tools. Selection persists per chat, instructions reload
on each model request, and a newly selected/changed skill must reach the model
before a write, command, or MCP call. At most three selected skills fit a combined
8 KB instruction budget. Skills never override user instructions or permissions;
this version has no marketplace, installer, dependency execution, or automatic
matching engine.

Every model request's system message includes a skill index: one line per listed skill
with its ID and description (whitespace collapsed, at most 150 characters), at most 30
lines and 2,000 bytes, then "… and N more (call list_skills)". The model is told to load
a listed skill with `use_skill` when it fits the task. Skill descriptions from
`.agents/skills` and `.claude/skills` therefore reach the system message of every chat in
that workspace, with the same trust as `AGENTS.md`: review skills you did not write. The
context details list the index as **Skill index · applies to this workspace**.

The app ships a detailed handoff skill at `skills/handoff/SKILL.md`, available in every
workspace: send `/handoff Create a handoff for this conversation` (or type `/han` and
press Enter to insert the command). To change it for one project, copy it to
`<chat workspace>/.agents/skills/handoff/SKILL.md` (for example
`my-project/.agents/skills/handoff/SKILL.md`) and edit the copy; that copy then
replaces the shipped skill in that workspace. It asks for a cold-start
document in the style of a long working-session memory file, written in parts of
about half the file text one response can hold (about 16 KB at the 16,384 Opus 5.5
default, about 8 KB at 8,192) so no single response hits the output limit,
and ends by calling `insert_activity_log`. Select **Accept edits** first to avoid approving every part.
The skill uses 6 KB of the 8 KB skill budget. Automatic compaction handoffs (see
Context and project instructions) are written from the turns being compacted; use the
skill when you want a curated handoff, for example before ending a session.

A handoff can also be built from a project's earlier material, such as older handoffs,
memory files and past conversations, for example: `Read ~/notes/project-v*.md,
handoffs/*.md and history/*.md, oldest first, and use /handoff to write one detailed
handoff in handoffs/`. The skill tells the model to list the files, read each one once
in full, and rely on the app's in-request compaction when they do not all fit (see
Context and project instructions). Give it past conversations as Markdown transcripts,
not as their `.jsonl` files, which are several times larger and hold lines too long for
`read_file`:

```bash
python3 scripts/conversation_transcript.py .local/conversations/<id>.jsonl [more.jsonl ...] --out-dir ~/my-project/history
```

Each conversation becomes `<date>-<title>-<id>.md` (an existing file is never replaced):
the messages and replies in full, one line per tool call with its main input, questions
with the user's answers, plans in full with their review outcome and feedback, and
command output shortened to 600 characters (`--excerpt`). `--out all.md` joins them,
oldest first. The script needs only Python 3 and replaces common secret shapes with
`[REDACTED]`, but transcripts contain whatever the conversations did: review them before
sharing. Copy a conversation file first if the app is running.

The model receives the current local date, time, and timezone with each request, so
handoffs and file names use the real date. It is also told its per-response output
limit from **Max output tokens** and the approximate file text that fits (about 2 KB
per 1,000 tokens), so long files are split into parts that finish in one response. Its instructions keep chat replies concise
but ask for complete requested documents, split into parts rather than shortened.

`insert_activity_log` inserts an exact, app-generated Markdown log of the
conversation's commands (with results and exit codes), files created, edited, read,
listed, or searched, declined or failed actions, and other tool-call counts. The app
builds it from the saved display history, so it covers compacted turns and costs no
output tokens; the model receives only a short summary. It replaces the line
`<!-- activity-log -->` when the file has exactly one, and otherwise appends. It runs
as a `write_file` with generated content: the same permission modes, folder access,
approval diff, file checkpoint, and `write_file` hooks apply (Plan mode blocks it).
Commands are copied as they ran except that the app's configured credentials and
common secret shapes (GitHub and Databricks tokens, AWS access keys, bearer tokens,
`password=`/`token=`-style values, and URL passwords) are replaced with `[REDACTED]`;
review the file before sharing it. The finished file must stay within 80 KB, or within
the file write limit (Settings) when that is lower; a higher write limit does not raise
this cap. The oldest commands are omitted with a note when needed, so insert the log into a
separate file to keep every command. Delegated subagent conversations keep their own
logs.

Tool arguments are validated against their advertised JSON Schema before approvals,
hooks, or execution, then pass the existing semantic/path checks. Validation is
offline and bounded: only local acyclic JSON pointers are supported; remote/dynamic
references and patternProperties are rejected. Explicit dialects must be JSON Schema
2020-12. Unsupported schemas produce a diagnostic instead of executing unvalidated
input. Existing MCP schema/argument caps remain in force.

**Hooks** are local commands from this configuration (at most 16; the whole configuration stays
within 32 KB). Each one has an `event`:

| Event | Runs | JSON output it reads |
| --- | --- | --- |
| `before_tool` | Before a tool call: after its schema check and deny rules, before its approval card | `decision` (`"deny"` with `reason`, or `"ask"`), `arguments`, `context` |
| `after_tool` | After a tool call completed | `context` |
| `tool_failure` | After an invoked tool call failed | `context` |
| `user_prompt` | Before your message (also one sent while the agent works) goes to the model | `decision` (`"block"` with `reason`), `prompt`, `context` |
| `session_start` | Once, before a chat's first model request (a subagent gets `subagent_start` instead) | `context` |
| `agent_stop` | When a turn would end with a final reply (after Verify) | `decision` (`"block"` with `reason`) |
| `subagent_start` | Before a subagent's first model request | `context` |
| `subagent_stop` | When a subagent would end with its final reply | `decision` (`"block"` with `reason`), `report` |

**Input.** Each hook gets one JSON object on stdin with `event` and the conversation's
`session_id`. Tool events add `tool`, `arguments`, a stable `call_id`, `result` (after
execution) and, for `tool_failure`, the original `error`; a hook for `read_file` also runs
for `outline_file`, and one for `list_files` or `find_files` also runs for
`project_overview`, its input then has `covered_by` naming the selected tool. `user_prompt`
adds `prompt` (your text, without attached file contents), `attachments` (their names),
`steered`, `workspace`, `model`, `permission_mode` and `title`; `session_start` adds
`workspace`, `model`, `permission_mode` and `title`; `agent_stop` adds `reply` (the final
reply), `continuations` (how often hooks continued this turn), `workspace`, `model` and
`permission_mode`. The subagent events give the parent's `session_id` and add
`subagent_session_id`, `agent_type`, `task`, plus `tool_profile`, `context` and `max_steps`
(`subagent_start`) or `reply` and `continuations` (`subagent_stop`). Long text is shortened
(the `*_shortened` field then says so); hook input is at most 128 KB.

The optional `tools` array of a tool event selects exact names (including MCP names); omit it
for all tools, or use an empty array to match none. Other events ignore it. Failure hooks run
for invoked tools that fail, including MCP `is_error` results and foreground commands that
failed or timed out; schema, policy and approval rejections, cancelled actions and
background-job completion do not launch them.

**Output.** A hook may print **one JSON object** on stdout, for example
`{"decision": "deny", "reason": "Generated files are rebuilt by make; edit the template."}`.
Only stdout is read, and only when its whole trimmed text is one object; any other output (text,
a list, cut-off JSON) is plain text, shown on the hook card as before. stderr is never read as
JSON: the card shows it after stdout, under `[stderr]`, so a warning a command prints there
cannot turn a decision into plain text. Fields an event does not use are ignored and listed on
the card. Output that starts with `{` and ends with `}` but is not valid JSON, a known field with
a wrong type or value (for example `"decision": "block"` on `before_tool`), or JSON output over
16 KB counts as a failed hook. Exit code 0 means no objection plus the optional JSON; any other exit code (or a timeout)
keeps its old meaning: a failing before-tool hook blocks its tool, and other failing hooks
leave a warning notice and change nothing. Hooks run in configuration order; a later hook of
the same event sees an earlier hook's replacement `arguments` or `prompt`, and a `deny` or
`block` ends that event's hooks.

- `decision`: `"deny"` (`before_tool`) refuses the call; the agent gets the `reason`
  ("Blocked by the before-tool hook …"). `"ask"` shows an approval card even where the mode or a
  rule would run the call without one, also in **Bypass permissions** (a hook's request for a
  person is never answered silently) and also for tools that never show a card, such as reads.
  Such a card cannot be remembered as a rule, and switching modes does not approve it; Don't ask
  refuses the call instead. `"allow"`, or no decision, is no objection: it never skips a card
  the mode or rules require. `"block"` on `user_prompt` keeps your message from the model (the
  chat says why; nothing is sent or billed), on `agent_stop` asks the agent to keep working (a
  hidden message with the reason; the chat shows "A hook asked the agent to continue: … (1 of
  3)"), at most three times in a row per turn and only while agent steps are left, and on
  `subagent_stop` gives the subagent one more round, at most twice. Stopped turns, errors, the
  step limit and **Stop** never run `agent_stop` or `subagent_stop`.
- `arguments` (`before_tool`): replacement arguments for the call. They must pass the tool's
  JSON schema, or the hook fails and the call is blocked. Because before-tool hooks run before
  the call's own approval card, everything after them uses the final arguments: deny, ask and
  allow rules, protected files, folder access, the approval card (which shows what will run,
  with a **Before the hook** view of the agent's proposal), the read-before-overwrite and
  changed-file guards, and the shell-free run of a command an argv rule allows. A hook cannot
  turn an approved or allowed command into another one without the new one's own check. The
  agent is told its arguments were replaced and what ran. If you edit the call on its card
  (**Edit** before approving), your edit runs without before-tool hooks checking it again (the
  rules still do), and the agent is told the edited arguments ran. The project's Verify command
  and the activity log keep the app's arguments (the field is listed as ignored).
- `prompt` (`user_prompt`): replacement text for your message. The message then shows
  "Edited by hook …" with your original under **Your original message**; @ files and ! output
  you attached stay attached (a replacement's own @ tokens attach nothing).
- `context`: text for the agent, at most 2,000 characters per hook (4,000 per result or message),
  always labelled as coming from your local hook commands. Tool-event context is added after
  that call's result; `user_prompt` and `session_start` context after your message (never into
  the system prompt), shown under **Hook context sent with this message**; `subagent_start`
  context after the subagent's first message.
- `report` (`subagent_stop`): replacement text for the report the parent chat receives,
  labelled as replaced by the hook; the subagent's own final reply stays in its transcript.

**Queued messages and subagent chats.** A message you queue while the agent works stays
queued while its `user_prompt` hooks run or their card waits, so after **Stop**, an error or a
restart it is still queued (see **Messages while the agent works**); it leaves the queue once it
is sent or a hook blocks it, and one you remove meanwhile is not sent. If hook context makes it
too large for the context budget at that step, it waits for a later one, and its hooks run again
then. The subagent events belong to a subagent's delegated turn. A subagent chat you continue
yourself (a message, **Edit** or **Retry** after its delegation ended) is an ordinary chat for
hooks: your messages there get `user_prompt` hooks and its turns end with `agent_stop`, under
that chat's mode and rules (a subagent with a restricted tool profile, such as `explore`, runs
no hooks); nothing runs or shows in the parent chat.

**Permissions.** Every event goes through the same approvals as before: its permission is
`hook_<event>` (for example `hook_user_prompt`), each hook asks for approval except in Bypass,
Plan skips hooks, and Don't ask runs only hooks a rule allows (a before-hook without one blocks
its tool). **Allow this hook in this chat** stops asking for that hook ID in that chat; a saved
hook rule does the same for the project or all projects. Deny and ask rules for hooks apply in
Bypass too. A hook card shows in the chat it affects (the subagent events: in the parent chat,
where the delegation is) and says what the hook decided: "Hook blocked: …", "Hook asked for
approval: …", "Hook added context", "Hook changed the arguments" (with a before/after view) or
"Hook edited the message". A declined hook does not run: a declined before-hook blocks its tool,
and a declined lifecycle hook changes nothing. Hooks have a 1–30 second timeout; a card and a
warning show at most 3.8 KB of output, and secrets are redacted from everything shown or sent
(in JSON output also after decoding, so an escaped spelling of a secret is caught too).
Hooks do not recursively invoke hooks. Enable only commands you intend to run for every
applicable event; configuration can change only while agent turns are idle.

### Agent tools: tasks, subagents, and activity

**Tasks** shows persisted tasks, descriptions, status, and dependencies. Add them
manually or let the model use `create_task`, `list_tasks`, `get_task` (one task's full
description, status, and dependencies; read-only, so it runs in every mode), and
`update_task`. Every change, by the model or here, updates the task checklist above
the message box. A conversation holds up to 50 tasks; dependencies must belong to the same chat,
cannot form cycles, and must finish before dependent work starts or completes.
These planning records are available in Plan mode.

**Subagents.** The model delegates on its own with `delegate_task`, as its instructions
describe: broad codebase exploration, reading many or large files, wide searches, running a
long command and summarizing its output, or an independent investigation, so that only a
short report enters the chat's context. It does a single small lookup, or work that needs
the conversation's back-and-forth, itself. A subagent sees nothing of the chat: the model
passes the full task, constraints, paths and the report it expects (`task` and `context`).
`agent_type` picks the tools: **explore** is read-only (the `read_only` profile, for
investigation), **editor** reads and edits files without commands (`file_editor`), and
**general** (the default) has the chat's tools (`inherit`); a project's own agents are named
by their files (see **Custom agents** below). All stay within the chat's subagent tool ceiling
(below). The older `tool_profile` argument still works; given with `agent_type`, the narrower
applies. `max_steps` is 1–24 model requests (default 10). The child uses its agent type's model
and effort (**Settings → Subagents**: explore runs at Fast on the chat's model by default) and
otherwise the chat's model (and its reasoning setting when the chat has one). It inherits workspace,
permission mode, folder grants, turned-off tools, and "Allow for this chat" rules (a copy:
rules the child adds stay in the child); project and all-project rules apply to it as to the
parent. Children cannot delegate again or start managed background jobs. The child's first
prompt asks it to end with a concise report: a summary, key findings with `path:line`
references, files changed, and open questions or risks.

Several `delegate_task` calls next to each other in one model response run in parallel.
Their approval cards (Manual and Plan ask) come one at a time, in call order, and none of
them starts before every card is answered; then they all start. Other tool calls run one at
a time in their order, so a call after the delegations waits for them. At most three
subagents run at once over all chats (a chat and three subagents is the model endpoint's
request budget, see **Rate limits**); a further one waits for a free slot and its card says
**waiting for a free slot**. Every call gets exactly one result, in call order, also when a
child fails or is stopped. Files are shared, so the model gives parallel subagents
independent work, never edits that could conflict.

The parent receives a bounded result (at most 6,000 bytes as JSON; the report is shortened
first): `child_session_id`, `agent_type`, `tool_profile`, `status`
(completed/failed/cancelled), `terminal_reason` (completion, user Stop, step limit,
inference/tool error, or interruption), `steps_used`, `tool_calls`, `changed_files` (paths
its file tools changed, at most 20; `changed_files_total` when there were more; files a
command changed are in the changes bar), `transcript`, and `output` (the report).
Each subagent has its own card: its type, the effort and model it started with (for example
**Fast · Claude Opus 5.5**), live status (running, waiting for approval,
waiting for a free slot, completed, failed, cancelled), completed actions, last tool, how
long it has run, **Open subagent** and, once it ended, a collapsible **Report** with the
files it changed. Progress metadata does not copy child tool payloads into the parent's
context. **Open subagent** opens the child to review its work or approve actions;
**Back to parent conversation** returns to the parent. Stop on the parent stops all its
subagents, also those still waiting for a slot. The child's first prompt is labeled
**Delegated by parent conversation** and retains the spawning action ID; later human
messages are not labeled delegated. A subagent chat is named by its task until it gets a
title. Unfinished delegated runs are marked interrupted on restart rather than left running.

**Linked transcripts.** Every subagent is a conversation of its own:
`conversations/<id>.jsonl` in the state folder, like any chat. Its header (the first line)
keeps `parent_session_id`, `parent_event_id` (the parent's card) and `parent_call_id`, its
`agent_type`, `tool_profile`, `max_steps`, and `delegation` (status, completed actions, last
tool, the `model` and `effort` it started with, start and end times). A child whose agent set
them also keeps `agent_model`, `agent_effort`, and for a custom agent `agent_file` and
`agent_tools` (exports, imports and forks keep them, so a copy never gets more tools). The
parent's header keeps a `subagents` index, one entry per child: `{id, task (its first 200
characters), agent_type, status, created, ended, transcript, model, effort}`, plus
`steps_used` and `tool_calls` once it ended. An entry is added when the
child starts and updated when it ends and by restart recovery; the newest 200 are kept.
Deleting a subagent chat keeps its entry, marked `deleted` and without a transcript; deleting
the parent with its subagents removes them all. An import rebuilds the index of each copied
parent from the copied children; exports leave it out, and forks and rewind backups have
none. The sessions list (`GET /api/sessions`) leaves the index out;
`GET /api/sessions/<id>/children` lists the children with their `transcript` and index entry.

**Agent tools → Subagents** lists the chat's subagents with their type, status, tool profile,
effort and model, task, steps and tool calls, start and end, and transcript file name, with
**Open** for each. Below them, **Agent types** lists what `delegate_task` can start in this
project: the built-in types with their Settings defaults, each custom agent (description, file,
profile, tools, model, effort, default steps, skills, and notes such as ignored tool names), and
every agent file that cannot be used, with the reason (**Refresh agent types** reads the files again).
**Subagents → New subagent tool ceiling** selects `inherit`, `read_only`, or `file_editor`
for future children while the parent is idle. The model may request a narrower
`agent_type` or `tool_profile` in `delegate_task` but cannot widen the parent's ceiling. Read-only
permits file listing/reading/search, skill discovery/selection, task listing/reading, and
`ask_user`; file-editor additionally permits file writes/edits under normal approval rules.
Both file-only profiles exclude shell commands, jobs, MCP startup/calls, hooks,
task mutations, and further delegation. Restrictions persist on child continuation,
imports, and forks. Existing children are unchanged by a new ceiling. These are
server-enforced model-tool limits, not an OS sandbox or restrictions on the user's
explicit editor/terminal actions.

**Custom agents.** A project can define its own subagents in Markdown files, which the app
only reads: `.agents/agents/<name>.md`, then `.claude/agents/<name>.md` (when both define a
name, the `.agents` file is used and the other is noted). Frontmatter between two `---` lines
names the agent and its limits; the rest of the file is its instructions:

```markdown
---
name: security-auditor
description: Reviews changed code for injection, secrets handling and unsafe file access.
tools: [read_file, search_files, find_files, outline_file]
profile: read_only
model: databricks-claude-sonnet-4-5
effort: low
skills: [review]
max_steps: 8
---
Check every changed file for hard-coded secrets and unsafe path handling.
Report each finding with path:line, its severity and a suggested fix.
```

| Field | Meaning |
| --- | --- |
| `name` | A lowercase letter, then up to 39 lowercase letters, digits or `-` (default: the file name); not `explore`, `general` or `editor`. |
| `description` | Required: one line of at most 300 characters, which the model reads to choose the agent. |
| `tools` | Optional: the only tools the agent may use (`[a, b]`, `- item` lines, or `a, b`). Names this app does not have are ignored with a note, `delegate_task` never applies, and tools outside the profile are dropped. Left out: every tool of the profile. |
| `profile` | `read_only` (the default), `file_editor` or `inherit`, as for `tool_profile`. |
| `model` | Optional Databricks serving endpoint name, only one you already use: the Settings model, a **Settings → Subagents** model or the model of one of your chats (not a subagent chat). Any other endpoint runs on the chat's model, with a note in Agent types and on the approval card. Left out (or `inherit`): the chat's model. |
| `effort` | Optional `default`, `low` (Fast), `medium`, `high`, `xhigh` or `max`; left out: the chat's effort. |
| `skills` | Optional: up to three skill IDs loaded into the subagent like selected skills; one that does not load is left out with a note in its first prompt. A `/skill` word in the instructions or the task loads no other skill. |
| `max_steps` | Optional 1–24: the subagent's steps when the call gives no `max_steps` (a call's own value wins). |

The parser is small and strict: `key: value` lines, `[a, b]` or `- item` lists, `"..."` or
`'...'` quoting and `#` comment lines; nested or multi-line (`|`, `>`) values are refused.
Claude Code agent files work too: its tool names `Read`, `Write`, `Edit`, `MultiEdit`, `Glob`,
`Grep`, `LS`, `Bash`, `NotebookRead` and `NotebookEdit` stand for this app's, other fields such
as `color` are ignored with a note, and its model values `sonnet`, `opus`, `haiku` and `inherit`
mean the chat's model. An agent file is at most 16 KB (16,384 bytes) with up to 8,000 characters
of instructions; a folder with more than 100 entries is not read (Agent types says so) and at
most 30 agents are used. A file
that cannot be used (no description, a bad name or value, too large, a symbolic link, not UTF-8
text, a quoted value with a lone `\u` escape, a file name that is not UTF-8, a name another file
already uses, an agent folder outside the project) is listed under
**Agent tools → Subagents → Agent types** with the reason, and delegating to it fails with that
reason.

*How the model chooses.* A chat that can delegate gets an **agent index** in its system
message, right after the skill index: each agent's name and description (shortened to 150
characters), at most 30 lines and 2,000 bytes, with a count of any left out. It changes only
when agent files change, so the endpoint's prompt prefix caching keeps working. `agent_type` in
`delegate_task` is a plain string rather than an enum, because custom names differ per project
while the tool definitions stay the same in every request; the server checks it, and an unknown
name returns an error that lists every available type (and why a file of that name cannot be
used). The subagent's first prompt carries the task and context, then `Custom agent: <name>
(from <file>)` with its instructions, then what its tools allow and the report it ends with. An agent's instructions never enter the parent's system message.

*Defaults per type.* **Settings → Subagents** sets each built-in type's model (**Same as the
chat**, a listed endpoint, or **Other endpoint…**) and effort (**Same as the chat** or a level):

| Type | Tools | Model | Effort |
| --- | --- | --- | --- |
| explore | `read_only` | the chat's | Fast (`low`) |
| general | the chat's (`inherit`) | the chat's | the chat's |
| editor | `file_editor` | the chat's | the chat's |
| custom agent | its `profile` and `tools` | its `model` (an endpoint you use), else the chat's | its `effort`, else the chat's |

A subagent whose type sets an effort keeps it when you change the chat's effort; the others
follow the chat, and one still waiting for a free slot takes the chat's effort when it starts
(until then its card shows only an effort its type sets). A subagent on an endpoint its type
sets, other than its chat's and the Settings model, follows that endpoint's default budgets where
Settings is at the Settings model's defaults, and Settings budgets above that endpoint's
defaults are lowered to them (and, as for every chat, to its maxima): with Claude Opus 5.5 at
1,000,000 context and 32,000 output tokens, an agent on GPT OSS 120B sends 131,072 and 8,192,
while an agent on the Settings model keeps 1,000,000 and 32,000, also from a chat on GPT OSS 120B.
Lower Settings budgets stay as they are.

*Safety.* An agent file is project content, like `AGENTS.md`, and never widens what the chat
allows. It cannot send the subagent's requests, and the project content they carry, to an
endpoint you do not already use (see `model` above). Its profile is intersected with the chat's
subagent tool ceiling and any requested `tool_profile` (the narrowest wins), its tools with that profile and without those the chat has
turned off or an unconditional deny rule covers (a delegation with none of its tools left is
refused, and the first prompt lists only the tools it has), the chat's turned-off tools, rules
and permission mode apply as to any subagent, and subagents never delegate. A tool outside the
agent's list is refused like a turned-off one. In Manual mode the approval card names the agent
and everything it sets, for example **Start a subagent (security-auditor · read-only · model
Claude Sonnet 4.5 · effort Fast; separate paid model requests, at most 8 steps)**, followed by
the task, the agent file, its tools and skills, and the exact endpoint; the subagent that starts
is the one the card showed, even if its file changes before you approve. Text the app shows or
sends from agent files is redacted (the Databricks token and common secret shapes).

**Questions and plan review.** With `ask_user` the model asks 1–4 short multiple-choice
questions when it needs a decision. Each question has 2–4 options (single or multiple
choice), and **Other** takes your own text (up to 1,000 characters); the model may not
add an option named Other itself. **Send answers** is enabled once every question is
answered; **Skip** tells the model to continue with its best judgment and state its
assumptions. The card waits like an approval card: the sidebar shows **Needs approval**,
notifications and the busy-chat checks treat it the same way, the message box names what
is waiting, the Approval wait in Settings applies (after it the model is told you did
not answer), and Stop cancels it. `ask_user` works in every mode, including Plan. In
Don't ask mode it returns at once without waiting for you, and switching a chat to Don't
ask skips its waiting questions; the card then notes that they were skipped
automatically. `ask_user` and `submit_plan` must be the last tool call in a model
response; otherwise they are refused before any card appears, so no other call from that
response runs before the model has read your reply, or under a mode an approved plan
switched to. A subagent's question appears in the subagent's own conversation (the
parent card reads **Waiting for approval**; use **Open subagent**), is answered there,
and is never copied into the parent's context. In Plan mode the model can present its
finished plan with `submit_plan` (Plan only, never in subagents). The plan card renders
the plan as Markdown with an optional feedback box and three choices: **Keep planning**
(the model gets your feedback and stays in Plan), **Approve and use Manual** or
**Approve and use Accept edits**, which switch the chat to that mode, and the model then
implements the plan in the same turn under the new mode's rules. Changing the mode from
the Permissions menu while a plan waits, or after you answered but before the agent
continued, closes the card as not reviewed, keeps the mode you picked from the menu, and
tells the model to ask before implementing it. The plan is not written to any file; it
stays in the conversation, and exports keep answers and review outcomes.

**Activity** collapses a list of actual tool actions and their states; existing
approval cards remain usable independently. A reply's **Thinking** block appears only when
the Databricks stream supplies thinking: `reasoning` content blocks with `summary_text`
entries, `thinking` blocks, or `reasoning_content` / `reasoning` delta fields (see
**Reasoning effort and thinking**). The text persists with the conversation, displays
separately from the answer, and has a 12,000-character limit. Opaque/encrypted reasoning
is ignored. Thinking is not replayed as model history, and the app never fabricates
hidden reasoning. Endpoint support varies; ordinary responses legitimately have no
Thinking block.

### Permission modes

Choose a mode beneath the chat input. Existing conversations default to Manual.
Changes are saved per conversation and apply at once, also while a response runs or a card
waits (see **Mode changes while the agent works** below).

| Mode | Agent behavior |
| --- | --- |
| Manual | Read approved folders; ask before edits, commands, stopping jobs and subagents. |
| Auto | Accept file edits, run the fixed list of read-only commands (below), stop jobs and start subagents; ask for other commands and actions. |
| Accept edits | Accept file edits and start subagents; ask before commands and other actions. |
| Don't ask | Run only what the mode's reads or a saved rule allows; block everything else without an approval card. |
| Plan | Read/search approved folders, run the read-only command list, and describe a plan; block file changes and other commands. Subagents still need a card. |
| Bypass permissions | Run actions and access external folders without approval cards. Deny and ask rules still apply, and credential/generated-file exclusions still apply to file tools. |

Reading, listing and searching files, reading job output, waiting for a job, skills and
structured tasks are available in every mode without a card. Stopping a job is a command
action: it asks in Manual and Accept edits, is blocked in Don't ask unless a rule allows it, runs
in Auto and Bypass, and is blocked in Plan. Starting a subagent (`delegate_task`) asks in Manual and
Plan, runs in Accept edits, Auto and Bypass, and is blocked in Don't ask unless a rule allows
it; the child then follows its own (inherited) mode. Starting a background job follows the
same command rules as foreground execution. Changing modes does not cancel a previously
authorized background job. Manually saving in the editor or clicking Run in the terminal is
an explicit user action outside the agent's permission mode. The project's Verify command
runs as the agent's `run_command`, so modes, rules, protected files and Don't ask apply to it
as to a command the agent chose (Plan never runs it). Diagnostics after edits run inside the
app and start no commands.

### Permission rules

A rule turns one kind of action into an automatic approval (**allow**), a card in every mode
(**ask**, "Always ask") or a refusal (**deny**). Rules have three scopes:

- **This chat**: saved with the conversation (the **Allow for this chat** rules above, at most
  100). Inherited by subagents; forks and imports start without them. A deny or ask rule added
  to the chat while a subagent runs also reaches that subagent; other chat rule changes do not.
- **This project**: saved in the app's state folder, in `permission_rules.json`, keyed by the
  project folder's resolved path (at most 200 per project). A managed worktree is a separate
  folder, so it has its own project rules.
- **All projects**: the same file, at most 200 rules.

The rule file is written atomically with mode 0600 and is never read from a repository:
`.claude/settings*.json` and `.agents/permissions.json` are ignored. A file that cannot be read
is ignored (no project or all-project rule applies) and the permissions menu says so, without
quoting it; **Reset saved rules** moves it aside as `permission_rules.json.bad-<time>`.
Project and all-project rules are app state and are never exported.

Rule kinds:

- **Commands**: an exact command (its words as the shell would split them, for example
  `npm test`), or commands starting with one to three words followed only by listed options
  (`git status` with `-s --short`; `--name=` allows any value after `--name=`). Positional
  arguments must be listed too. Deny and ask rules name a program and up to 16 words, which
  must all appear somewhere in the command in any order (`git push` also matches
  `git -C . push --force` and `/usr/bin/git push`), with combined short options split
  (`rm -rf` matches `rm -r -f` and `rm -fr`); a deny or ask rule without words covers every command.
- **File edits** (`write_file`, `edit_file`, `insert_activity_log`, `move_path`, `delete_path`,
  `notebook_edit`), optionally only under one absolute folder. An allow rule needs every path the
  tool touches inside the folder; a deny or ask rule applies when any of them is (both ends of a move).
- **Tools**: one MCP tool, every tool of an MCP server (`mcp__server__*`, deny and ask only),
  a hook ID, `delegate_task` (subagents), `stop_job`, or any other tool name (deny only).

When rules disagree, **deny beats ask, and ask beats allow**, whatever their scope. The fixed
order is: turned-off tools, deny rules (in every mode, Bypass included), Plan's read-only limit,
ask rules, protected files, the mode itself, allow rules (never in Plan), Don't ask, and finally
a card. A tool that a deny rule without conditions covers is not offered to the agent at all.

Rules never override credential and generated-file exclusions, the state-folder exclusion,
folder-access cards (only Don't ask, which blocks them, and Bypass, which needs none, differ),
project-guidance checks, the read-before-overwrite guard, the changed-file checks, Plan mode
(allow rules are ignored there) or protected files. An allow rule cannot cover a program that
runs other programs or code (`sh`, `bash`, `env`, `sudo`, `xargs`, `nohup`, `timeout`, `ssh`,
`watch` and similar; `python -c`, `node -e` and other inline interpreters; `awk` and `sed`),
an option that runs other programs (`git -c`, `--git-dir`, `--ext-diff` and similar; `rg --pre`
and `-z`; `find -exec` and `-delete`), a word naming a credential file, or your Databricks token.
The same applies to **Allow for this chat** on a card: a command naming a credential file or
containing your token cannot be remembered, even by its exact text. A card raised by an Always
ask rule, by an uncertain command (below) or by a protected file offers nothing to remember,
because an allow rule would never apply to it.

**No shell.** Commands allowed by an exact or prefix rule, and commands on the read-only list,
run without a shell: shell syntax (`; & | < > ( ) { } [ ] * ? ~ ! # $ ^`, backticks,
backslashes, tabs and line breaks) never matches such a rule. A command allowed by its exact
text in this chat, and any command you approve on a card, still runs through the shell exactly
as approved.

**Read-only commands.** Auto and Plan run this fixed list without a card, in the foreground
only: `pwd`; `ls` with no option or `-l`, `-a`, `-la`, `-al`, `-1`; `git status` (plain,
`--short`, `-s`, `--porcelain`); `git diff` (plain, `--stat`, `--name-only`, `--cached`,
`--staged`, `--cached --stat`); `git log --oneline` (optionally `-n 20`); `git show` (plain or
`--stat`); `git branch` (plain or `--show-current`); `git rev-parse HEAD`,
`--abbrev-ref HEAD` and `--show-toplevel`; `git ls-files`. There are no test runners, package
managers, search tools, interpreters or `git remote` on the list. A command on the list runs
hardened (see **Local access and limits**) whatever mode or rule allowed it; when you approve it
on a card, it runs through the shell like any approved command.

**Uncertain commands.** A command that a deny or ask rule might cover but that cannot be checked
exactly gets a card, even in Bypass: shell syntax that contains the rule's program as a word, `$`,
backticks, backslashes or glob characters, or an `alias` or `source` line; a wrapper such as
`env`, `command`, `nohup` or `sh -c`; an inline interpreter, also with combined or attached
options (`perl -le`, `python3 -Ic`, `node --eval=…`); or a never-allowed option such as `git -c`.
Text that does not mention the rule's program (`npm test | tee out` with a deny rule for
`git push`) is not uncertain.

**Protected files.** Edits always show a card, in every mode except Bypass and even with an allow
rule, when a path (requested or resolved, inside or outside the project) is in `.vscode`, `.idea`,
`.husky`, `.githooks`, `.agents`, `.claude`, `.devcontainer`, `.github/workflows` or
`.github/actions`, is named `AGENTS.md`, `CLAUDE.md`, `CLAUDE.local.md`, `.gitattributes`,
`.gitmodules`, `.pre-commit-config.yaml` or `.envrc`, is a shell or Git startup file in your
home folder (`.bashrc`, `.bash_profile`, `.bash_login`, `.profile`, `.zshrc`, `.zshenv`,
`.zprofile`, `.zlogin`, `.gitconfig`, `.ssh`, `.config/git`), or is inside this app's own folder.
These files can make later commands or the app run code. Such a card cannot be remembered.
Don't ask blocks these edits.

**Mode changes while the agent works.** You can change the mode at any time. A waiting card
is checked again at once: switching to Accept edits approves a waiting edit, switching to Plan
blocks waiting edits and commands ("Not run: the user switched this chat to Plan mode while this
card was waiting.") but approves a waiting command from the read-only list and leaves a subagent
card waiting, switching to Don't ask blocks waiting cards, and switching to Bypass approves
waiting cards, including folder access (without adding a folder grant), except a card a
before-tool hook asked for (`"ask"`), which waits for your answer. Before-tool hooks run before an
action's own card, so an action whose hook card waited is checked again after the hook: it gets
its own card (and a folder-access card outside the project) when the new mode needs one. Switching to Bypass asks for confirmation for that reason.
Each new model request uses the current mode, and a running subagent follows the parent's
change, including its waiting cards. Adding or removing a rule, and turning a tool off, re-check
waiting cards the same way (a new allow rule approves a matching card; a deny rule blocks it).
A card resolved by a mode or rule change runs as that mode or rule allows it (without a shell
where that applies).

**Edit before approving.** On a waiting card you can change the command, the content of
`write_file`, or the replacement text of a single `edit_file` edit (not multi-edit `edits`,
notebook, move, delete, MCP or activity-log cards). The app validates the change, checks the
rules again, refuses it when the file changed since the card was shown, and shows a new
preview; the card keeps waiting with the same expiry. The agent is told that you edited the
action ("It ran as: …" for a command) and that its own proposal did not run. **Save edit** stays
off until the text differs, and changing it back to the agent's own proposal is not an edit. An
edited Verify card runs the edited command as that Verify run: the Verify notices name it, and a
failure starts a fix round as usual.

**From a card and the menu.** An approval card's **Always allow…** button opens a form that
sets where the rule is saved (**This chat**, **This project** or **All projects**; only the
scopes the server offers) and what it matches: for a command, its exact text or **Commands
starting with** one of the prefixes the server offers, plus the options allowed after it; for
edits, all file edits or only files under a folder. **Approve and remember** approves the card
and saves the rule. A command that cannot be split exactly can be remembered only by its exact
text, in this chat. An edited card is marked "Edited by you". The **Permissions** menu lists the
rules by scope, with **Deny** and **Always ask** badges and a remove button each, has **Add a
rule…** (behavior, scope, a command, file edits or a tool, with options for allow prefixes) and a
**Never asks** list naming what never needs approval. Adding or removing a chat or project rule
from a new chat creates the chat first.

**Turned-off tools.** **Agent tools → Tools** turns tools off for one chat. They are not
offered to the agent and calls to them are refused. Subagents, forks and imports keep the setting.
The tools are grouped (Files, Search, Commands and jobs, Git, Tasks and subagents, Skills,
Other); **Filter tools** matches names, groups and descriptions, and each group has
**All on** and **All off** for the tools it shows. Each tool shows the first sentence of its
description; hover it for more. MCP tools are turned off by their full name, and nothing
changes until **Save tools**.

**Don't ask** never shows a card: actions that the mode or a rule does not allow are refused,
and the agent is told to continue without them. Folder access outside the project is refused,
and enabled before-tool hooks block the tools they cover unless a hook
rule allows the hook. Other hooks (after-tool, failure and the lifecycle events) do not run unless
a hook rule allows them; their hook card says so, without a notice every time. A before-tool hook's
`"ask"` is refused there. When Verify is blocked this way (or
by a deny rule or a turned-off `run_command`), one notice says why and Verify does not run again
in that turn.

**Limitations.** Rules are a convenience, not a sandbox. A deny rule matches the command the
agent asks to run. It does not see Git aliases, scripts, Makefiles or programs those start, and
options are compared literally after splitting combined short options (`--force` and `-f` are
different words), so deny rules should name the program and subcommand. Project rules are keyed
by folder path, so a worktree or a moved project starts without them.

## Local access and limits

The server binds to `127.0.0.1`, checks Host/Origin, and uses a random local access
token to protect its API/WebSocket. Databricks credentials stay server-side. File
tools resolve paths and symlinks before checking the project and approved additional
folders. Bypass mode skips that folder boundary; credential files and generated
directories remain excluded. The app's state folder (settings, rules, extensions and
conversations; `LOCAL_AGENT_STATE_DIR`, `.local` by default) is excluded from file tools in
every mode, also after a folder grant or through a symbolic link; a workspace inside it, such
as a worktree, reaches only its own files. The folder is compared by file identity as well as
by name, so a case variant, a bind mount or a firmlink of it is refused too. Commands allowed
by an argv or prefix rule or by the read-only list run without a shell, from the program found
on the absolute entries of `PATH` (a program named by a path keeps that name, so
`.venv/bin/python` runs inside its virtual environment); the read-only `pwd` and `ls` run from
`/bin`, and read-only Git runs a `git` found outside the project, outside folders granted to
the chat and outside the default code folder, with repository programs (fsmonitor, hooks,
filters, textconv, external diff, signature checks, also inside submodules) switched off, no fetch of missing objects in a partial
clone, status, diffs and file lists limited to the workspace, and credential files (`.env.*`
except `.env.example`) left out of diffs. `git log` and `git show` still name commits that change
only files outside the workspace. With a Git older than the May 2024 security releases, read-only
Git does not run automatically in a partial clone. This is a trusted single-user local app, not a
multi-user sandbox. Commands allowed by the selected mode execute with your OS permissions and
can reach beyond the project. Commands get an environment without model credentials.

**Default code folder.** **Settings → Default folder for new code** (or
`LOCAL_AGENT_CODING_DIR`) names where the agent creates new standalone scripts, tools and
projects when you do not say where; files that belong to the open project stay in it, and
relative paths still mean the project. Every conversation may use the folder without a Folder
access card, like the project: Plan stays read-only, Manual asks before each edit, Accept edits
edits without asking, Don't ask refuses what would need a card, and Bypass is unchanged.
Commands still run in the project folder. A folder that does not exist yet is created by the
agent's first approved write there. Folder access lists it as "Default code folder · all
conversations", and Workspace → Files shows it as a root ("Created on first write" until then).
Settings refuses the filesystem root, system folders (`/etc`, `/usr`, `/System` and similar),
your home folder or a folder containing it, hidden folders in your home folder (such as
`~/.config` or `~/.ssh`), the app's state folder (or a folder containing it), the app's own
folder, credential and generated folders (`.git`, `.venv`, `node_modules` and similar), a folder
containing your credential file, a path through a symbolic link loop, and a folder whose parent
does not exist or cannot be written. A refused value from `.env` is shown in Settings as not used
and never blocks saving other settings. Files shows the folder as soon as the agent's first write
(a file tool or a command) creates it, without a reload.

**Settings → Agent steps** defaults to 32 model requests per user message and accepts
1–64. This is a model-request ceiling, not a tool-call count: one model response can
request several tools. Reaching the ceiling preserves completed actions and stops
until the user continues or raises the setting. **Max output tokens** defaults to
the endpoint's profile (16,384 for Opus 5.5, 8,192 otherwise) with the bounds described above. The editor and file edits are
limited by Settings → File write limit (80 KB by default).

Agent reads use `read_file(path, start_line=1, max_lines)` and return numbered
lines plus `next_line`; `max_lines` can be 1–2,000. A page holds at most a quarter of
**Context window** in bytes of serialized JSON, from 8 KB to 32 KB (32 KB with a
128K-token window); from a 262,144-token budget up, an eighth of it, at most 128 KB
(32,768 bytes at 262,144 tokens, 128,000 from 1,024,000 up). `max_lines` defaults to
400, and on those larger pages to the page size / 64 (512 at 32,768 bytes, 2,000 at
128 KB), so a long file takes fewer steps, and each step resends the whole context. The
first page of a file of at least 64 KB that does not fit one page, and that
`outline_file` can outline, also reports `file_bytes` and a `hint` to outline it and read
the sections needed; the hint is left out when `outline_file` is turned off or denied. `tail_lines` (1–2,000) returns the last lines instead, with their
absolute line numbers, which suits logs: it reads backwards from the end, works on files
up to 512 MB, and needs those lines to fit in 8 MB. Forward reads scan at most 8 MB from
the start of a regular file. Lines longer than 128 KB are refused. Lines end at LF only,
and one CR before it is dropped, so CRLF files read normally. Bytes that are not valid
UTF-8 are shown as U+FFFD (�) and the first 20 such lines are listed in
`invalid_utf8_lines`; files with NUL bytes are refused as binary. Reads can therefore
inspect larger files without loading them into the editor. File reads, search results,
and job-output pages have explicit continuation markers. The filesystem can change
between pages.

The agent's `list_files` returns `entries` in pages of at most 8 KB. Pass the
returned `next_offset` with the same path, depth, and glob to continue. Discovery
still stops at 300 matches per query; `listing_limit_reached` indicates that the
folder or glob should be narrowed to find additional files. The file explorer's
existing listing API is unchanged. To find files by name at any depth, the agent uses
`find_files(pattern)`: `**` matches any number of folders (`src/**/*.test.ts`), a
pattern without `/` matches file names anywhere (`*.py`), `./*.py` only directly in
the folder searched, and a pattern with `/` matches the path below that folder or the
path as the results show it. `{a,b}` gives alternatives, and letter case is ignored. `sort="modified"` lists the newest first, with UTC
modification times. Pages hold at most 500 paths and 8 KB, with `total` and `next_offset`.

`find_files` and `search_files` see the same files. Inside a Git work tree they use
`git ls-files --cached --others --exclude-standard`, so files that `.gitignore` excludes
are skipped unless the call sets `include_ignored=true`; untracked nested repositories
and submodules are listed in full. Git is used only when the repository's top level is
inside the workspace or granted folder being searched (in Bypass mode outside every
folder: anywhere) and is neither your home folder nor `/`, so a dotfiles repository in
the home folder cannot hide a project's files, also when the workspace is the home
folder. Outside Git the folder is walked without `.gitignore`. When Git fails, or the
repository's top level is above the workspace or granted folder (a package folder of a
larger repository, say), the folder is walked too and the result has a `note` saying
that `.gitignore` was not applied. Either way the fixed exclusions apply: credential
names, generated folders, symbolic links to folders and every file below them (also
when Git still lists such a file), and the configured credential file under any name,
including hard links to it. Listing
stops with an error after 10 seconds, 20,000 folders, or 200,000 files.

**Large files and projects.** `outline_file(path, max_level=6, offset=0)` lists the
structure of one text file of up to 8 MB with line numbers, so the agent reads only the
sections it needs: Markdown headings with the line where each section ends (front matter
and fenced code are skipped), Python classes and functions from the parser with their end
lines (files over 1 MB, files with a carriage return that is not a line ending, and files
that do not parse use a line scan; a file over 100 KB is parsed in a separate process, so
the parser does not pause the app, and one that takes over 2.5 seconds uses the line scan), and declarations found by a line scan in
JavaScript/TypeScript, Go, Rust, Java/Kotlin/C#/Swift/Scala/Dart, C/C++, Ruby, PHP, shell,
SQL, TOML/INI, YAML and Makefiles (heuristic; it can miss some). Entries read
`start-end text` (or `start text`); line numbers match `read_file`. It refuses folders,
notebooks (use `notebook_read`) and binary files, pages its entries (at most 20,000) like
`read_file` pages, and stops after 3 seconds with an error that suggests `search_files`.
Outlining is not a read: `write_file` still refuses to replace a file the conversation has
only outlined. `project_overview(path=".", include_ignored=false)` summarizes a folder from
file names and sizes, without reading any file: files and bytes by language and by
top-level folder, key files (README, build and configuration files, CI workflows, up to
two folders deep) and the largest files, in at most 12,000 bytes (longer lists are cut,
with the counts left out in `omitted`). It lists the same files as `find_files` (the same
`.gitignore` handling and exclusions); a folder too large to list (more than 200,000
files, 20,000 folders or 10 seconds) gives a partial overview of its top level with a note
instead of an error. Both are read tools: no card, allowed in Plan and Don't ask, and
external paths need folder access. A deny rule, a turned-off tool or a hook covers them
through the tool that shows the same content: rules for `read_file` cover `outline_file`, and
rules for `list_files` or `find_files` cover `project_overview`. With both tools offered,
the system message asks the model to start an unfamiliar or large project with
`project_overview`, to outline long files, to read a handoff completely when a full read
costs at most about a third of its input budget (the message gives that size in bytes of
`file_bytes`, from **Settings → Context budget**, **Max output tokens** and the model's
starting token estimate scale, allowing about a quarter more for `read_file`'s line numbers
and JSON escaping: about 111,000 bytes at the Claude Opus 5.5 default) (otherwise its status,
next actions and open questions in full, and other sections as needed), and, when it can
write files, to split a handoff longer than **Settings → File write limit** into numbered
parts listed at the top of the first.

**Tool output bounds.** Most tools already return at most about 8 KB: `list_files`,
`find_files`, `search_files`, `notebook_read`, `get_job_output`, a `wait_job` page, the
first page of `run_command` (start and end), `list_jobs` (7.5 KB), `list_skills`,
`list_tasks`, MCP results and the diffs of edits and moves. `read_file` pages are 8–32 KB,
up to 128 KB on large budgets; `delegate_task` returns at most 6 KB; hook output on cards
and notices 3.8 KB (JSON output is read from up to 16 KB; hook context adds at most 4,000
characters to a result); task descriptions 4,000 characters; skills 8 KB combined; the model's
own tool arguments 512 KiB and 64 calls per response. Every tool result, for the card and
the model alike, is also capped at 128 KiB: the longest text values keep their start and
end around a marker, and the result says `truncated` with `omitted_bytes`.

The results of one model response also share a **context allowance**: what the input
budget leaves after the latest exchange, with room for the largest summary, the app
record, a handoff line, the tool definitions and 1,024 tokens for the next step's notices,
and at most half the input budget. Room for a short notice per tool call of the response is
kept aside, and every text sent is counted, including notices, refusals and what hooks add to
a result (their context and a changed-arguments note). A compaction
inside that turn can therefore keep every result it was given, as long as the response's
own tool calls and their notices fit the budget. The allowance uses the estimate scale
after the response's reported input tokens, as the next request does. A read (`read_file`,
`outline_file`, `search_files` and the other read tools) that does not fit runs but is not
sent: the model gets a short `result_deferred` message asking it to request the result
again in its next response (or a smaller range, when one result alone is larger than the
allowance), its card says "Result not sent to the agent" (and, for a result larger than
the whole allowance, that the agent was asked to read a smaller part), and a `read_file`
or `notebook_read` that was not sent does not count as a read for `write_file`, nor as a
read that keeps a file's contents in context after a compaction. Results of actions that
already ran (commands, edits, MCP calls, subagents) are never withheld but shortened:
short fields such as a command's `job_id`, `state` and `exit_code` are kept, long text
keeps its start and end (the end of a command's output, where failures appear, which
`get_job_output` can read in full), and the card says "Result shortened for the agent". A
result with no long text is sent as it is. `wait_job`, `get_job_output` and `list_jobs`
results are always sent, because they move the job's read position, and so are your
`ask_user` answers and `submit_plan` feedback, which are never shortened. The card always
shows the full result. Forks, exports and imports keep what the model was sent.
Nothing in the history is shortened later; compaction is the only way history shrinks.

`search_files` supports literal search by default, `regex=true`, `case_sensitive`,
`context_lines` (0–5), `offset` (0–10,000), and `max_results` (1–100, default 50).
Case-insensitive literal search uses Unicode simple case folding, one character for
one: `K` also finds the Kelvin sign, but `STRASSE` does not find `Straße`. Regular
expressions use full case folding and find it. `output_mode` chooses the result:
`content` (the default) returns file/line, adjacent lines, and clipped excerpts;
`files_with_matches` lists the matching files; `count` gives matching lines per file
and `total_matches`. `offset` counts matches in `content` mode and files in the other
modes, and every result has `next_offset`. Query length is capped at 1,000 characters.
Binary (NUL) and non-UTF-8 files are not searched; a folder search skips files over 2 MB,
and `skipped_files` counts them and files that could not be read. One file given as `path`
is searched up to 8 MB, as far as `read_file` and `outline_file` reach; a larger one gets an
error pointing to `read_file` with `tail_lines` or `grep -n`. Before reading, a search refuses more than
20,000 files, or more than 64 MB in files of up to 2 MB, after `glob` filtering.
Reading and matching then have 10 seconds, and each regex match 50 ms. A regular
expression (also `wait_job`'s `until_regex`) whose counted repeats such as `{1000}` add
up to more than 100,000 repeated items, nested counts multiplied, is refused before it
compiles, because compiling it alone could take seconds and gigabytes. Limit errors
ask for a narrower query rather than claiming no matches. The Python `regex==2026.9.10`
dependency enforces regex timeouts and is installed by the normal backend installation command.

ripgrep (`rg`) is optional. When it is installed, literal searches of a folder use it
to find candidate files faster, and the results are the same without it: Python reads
every candidate and produces every match, count, and `skipped_files`, and it always
reads symbolic links, which rg skips. rg runs without a shell, configuration, or ignore
files, and never with `-i`: a case-insensitive query becomes character classes built
from Python's own case folding. Regular expressions, case-insensitive queries with
non-ASCII letters, and single-file searches do not use rg. When rg fails, for example
in a folder with an unreadable file, takes more than 5 seconds, or prints too much,
the search reads every file itself: slower, with the same results.

`run_command` accepts `timeout_seconds`, `max_output_bytes`, and `background`.
It returns a job ID and the first output page. When a command failed, timed out, or
was stopped and its output does not fit one page, the first result shows up to 1,500
characters of the start and up to 4,500 of the end of the retained output instead
(`excerpt: "head_and_tail"`), with the omitted range and the `get_job_output` offset
where it begins; when the command printed more than `max_output_bytes`, the excerpt
ends with a line saying that the rest was not kept. Successful commands keep their
first page. `list_jobs`, `get_job_output`, `wait_job`, and
`stop_job` inspect, paginate, wait for, and stop jobs in the same conversation.
`wait_job` waits up to 300 seconds until the job ends, a new output line matches
`until_regex`, or (with `idle_seconds`) the job prints nothing for that long, and returns
the new output and `next_offset` to wait again. It returns as soon as the job ends, so a
long wait costs time only for jobs that keep running, such as servers. The app learns how
long commands take: a background `run_command` whose command already ran to the end in
this folder (not stopped or timed out) says `expected_seconds` (the median of the last
five runs) and `next_wait_seconds`, and `wait_job` without
`timeout_seconds` waits about what is left (a quarter more, at least 5 seconds) instead of
60. Only completed runs count, or failed ones when none completed. Each result gives
`elapsed_seconds`, `timeout_seconds`, `waited_seconds` (how long this wait took) and, after a
timeout or an idle return while the job still runs, `next_wait_seconds` (from the expected
duration, else twice this wait, at least 10 seconds), and the system prompt asks the agent to
size waits to the job instead of waiting the maximum.
Without `from_offset` it scans from the end of the last result the conversation got
for that job, so a line printed in between still counts. Stop ends the wait, not the
job. The job's
exit code and state distinguish success, failure, timeout, and cancellation.
Only the configured prefix of output is retained; excess output is drained and
reported as truncated. Foreground cancellation, timeouts, command completion,
and normal app shutdown clean up the command's process group. A timeout, Stop, or
shutdown sends SIGTERM to the group first, so commands can clean up, and SIGKILL
2 seconds later. Use managed
background mode instead of shell `&`, detached daemons, or commands that escape
the process group. This is process supervision, not an OS sandbox. After an
abnormal server death, previously running jobs are marked **interrupted** and
never replayed; their outcome and surviving processes must be checked manually.

Commands get `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`, `SHELL`, `USER`, `LOGNAME`, and
`VIRTUAL_ENV` from the server's environment, plus the variables named in
**Settings → Environment variables for commands**, with values from the server's
environment when each job starts. These are never passed: the model credential
variables (`DBRICKS_TOKEN`, `DATABRICKS_TOKEN`, `DBRICKS_URL`, `DATABRICKS_HOST`,
`LOCAL_AGENT_ENV_FILE`), loader variables (`LD_*`, `DYLD_*`), shell and interpreter
startup variables (`BASH_ENV`, `ENV`, `ZDOTDIR`, `PROMPT_COMMAND`, `SHELLOPTS`,
`BASHOPTS`, `IFS`, `PYTHONSTARTUP`, `PERL5OPT`, `RUBYOPT`, `NODE_OPTIONS`), and any
value that contains the Databricks token. Hooks do not get these variables.

**Python environment.** With **Settings → Python virtual environment** (or
`LOCAL_AGENT_PYTHON_VENV`) set, these commands use it: the agent's `run_command`
(through the shell and the argv rules), Verify, hooks, the Terminal panel, `/api/command` and
`!` commands. Stdio MCP servers keep the app server's environment; to run one from the
environment, give `<venv>/bin/python` (or the server's program in `<venv>/bin`) as its command. `VIRTUAL_ENV` is set to it, its `bin/` comes first on `PATH`, the previous
`VIRTUAL_ENV`'s `bin/` and the app server's own interpreter folders are removed from `PATH`
(`run.py` puts the app's `.venv/bin` first, which would otherwise shadow your tools), and
`PYTHONHOME` is removed. An allow rule for `pytest` then runs `<venv>/bin/pytest`. The read-only
commands Auto runs without a card (`pwd`, `ls`, read-only Git) never use it. Hooks get the
environment but still not the variables named in Settings. Agent file edits inside the
environment (a write, edit, move or delete under it) always ask, also in Accept edits and Auto,
because a program placed there would run through an allow rule; Don't ask refuses them and Bypass
allows them. The environment is checked whenever it is used, and it is not used when it is
missing, has no executable `bin/python`, has neither `pyvenv.cfg` nor `conda-meta/`, is the
app's state folder (or contains it), is inside the app's own folder, or is the environment the
app server runs in. The app reads the Python version from `pyvenv.cfg` and never runs that
`python`. The system message tells the agent which environment commands use, or, without one,
that `python` may be the app server's own environment and it should ask before installing
packages.

When a background job that the agent started completes, fails, or times out, its
conversation gets a notice, also when the chat is idle, and the agent's next model
request names the job once, unless a `wait_job` or `get_job_output` result already
showed that it ended. Nothing wakes an idle chat. Jobs started from the
Terminal panel, and stopped or cancelled jobs, get no notice.
Automatic compaction adds bounded summary requests beyond the configured agent steps.
Databricks inference is billed to your workspace. Endpoints must support streaming,
function calling, and non-streamed text responses for summarization.

## Trying the app offline (simulator)

`scripts/simulate_databricks.py` starts the app against a simulated Databricks workspace on
your computer: no network, no token, no billed requests. Use it to try effort levels, the
Thinking display, subagents, background jobs and change review before using a paid workspace.

```
.venv/bin/python scripts/simulate_databricks.py                 # http://127.0.0.1:8766
.venv/bin/python scripts/simulate_databricks.py --speed 0.3     # less waiting
.venv/bin/python scripts/simulate_databricks.py --workspace ~/code/demo
```

- A fake gateway on `127.0.0.1` answers the two routes the app uses
  (`/api/2.0/serving-endpoints` and `/serving-endpoints/<name>/invocations`) with the same
  chat-completion chunks as Databricks, and lists `databricks-claude-opus-5-5`,
  `databricks-claude-opus-4-8` and `databricks-gpt-oss-120b`. Your credential file is never
  read; the app's credentials point at the gateway.
- It checks reasoning fields as the endpoints document them: Claude Opus 5.5 always thinks
  (effort `low` to `max` in `output_config.effort`, default `medium`; `budget_tokens` or
  disabled thinking is refused with HTTP 400), Opus 4.8 thinks only with
  `thinking.type: "adaptive"`, GPT OSS takes `reasoning_effort` `low`/`medium`/`high`. Claude
  endpoints stream a summary only when the request asks for `thinking.display: "summarized"`, after
  a short pause and ending with a blank line, as the real route does; longer efforts think longer and
  use more output tokens. Each request's reasoning fields are printed in the terminal.
- Usage has the shape the real route reports: for Claude endpoints `prompt_tokens` (the uncached
  part), `completion_tokens`, `total_tokens` and top-level `cache_read_input_tokens` and
  `cache_creation_input_tokens`, without reasoning tokens; GPT OSS keeps the OpenAI-style shape
  (cached tokens inside `prompt_tokens`, `completion_tokens_details.reasoning_tokens`).
- Claude endpoints take the app's cache markers (only `{"type": "ephemeral"}`, at most 4; other
  endpoints refuse them with HTTP 400) and keep an in-memory prefix cache per endpoint (5 minutes,
  refreshed on each read), so a step reads what the step before it wrote; the terminal prints
  `cache: read N, wrote M` per request. `--cache documented` refuses list content for the system
  message and tool results (the forms Databricks does not document), `--cache reject` refuses any
  `cache_control`, to rehearse the fallback in **Prompt caching**, and `--cache ignore` accepts the
  markers and reports no cache use (the default is `accept`).
- `--claude-fields anthropic|openai|none` makes the simulated route refuse one style of
  reasoning field for Claude endpoints, to rehearse what happens if the real chat-completions
  route does (the default `both` accepts either).
- Words in your message choose a scripted turn: `edit` (writes a file, then edits it twice
  with pauses, so you can press **Keep** in between), `job` (a background command and
  `wait_job`), `delegate` or `parallel` (one or two read-only subagents; `delegate custom` uses
  the first agent of the project's agent index, see **Custom agents**), `ask` (a question
  card), `task`, `read`, `rate limit` (one HTTP 429 with `Retry-After: 2`), `long`. Any other
  message gets a reply that says which effort and fields the request carried.
- Simulated chats and settings live in `.local/simulator/` (`--state-dir` to change), apart
  from your real conversations. Without `--workspace` a small demo Git project is created
  there. The app runs on port 8766, so the real app on 8765 can run at the same time.

## Development and checks

The frontend has no build step. The browser loads the files in `frontend/static/`
directly, so a UI change is: edit a file, save, refresh the browser. Nothing is
generated and nothing needs rebuilding or restarting.

- `frontend/index.html`: the page, with an import map naming the vendored libraries.
- `frontend/static/js/`: the UI as ES modules. Components use
  [Preact](https://preactjs.com) (React's component and hook model) with
  [htm](https://github.com/developit/htm) templates, which look like JSX but are
  ordinary JavaScript: `` html`<button class="primary" onClick=${save}>Save</button>` ``.
  `static/js/lib.js` lists the differences from React worth knowing (`class`,
  `onInput` for live text input). `static/js/types.js` documents the API shapes.
- `frontend/static/styles.css`: all styling.
- `frontend/static/vendor/`: Preact 10.29.8, htm 3.1.1, marked 18.0.14 (Markdown),
  DOMPurify 3.4.16 (sanitizing) and highlight.js 11.12.0 (code highlighting, the ES
  module build with its common languages), copied from their npm releases with their
  licenses; only the trailing source-map comment was removed (highlight.js has none). To update one,
  download the new version from `https://cdn.jsdelivr.net/npm/<package>@<version>/`
  and replace the file:

  | Vendored file | Package file |
  |---|---|
  | `preact.module.js` | `preact/dist/preact.module.js` |
  | `hooks.module.js` | `preact/hooks/dist/hooks.module.js` |
  | `htm.module.js` | `htm/dist/htm.module.js` |
  | `marked.esm.js` | `marked/lib/marked.esm.js` |
  | `purify.es.js` | `dompurify/dist/purify.es.mjs` |
  | `highlight.min.js` | `@highlightjs/cdn-assets/es/highlight.min.js` |

  DOMPurify is dual-licensed (Apache-2.0 or MPL-2.0); both texts are included.
  highlight.js is BSD-3-Clause (`LICENSE-highlightjs.txt`); its file was checked against
  the npm registry tarball's sha512 integrity.
- `frontend/static/fonts/`: the DM Sans `.woff2` files and licence from
  `@fontsource/dm-sans` 5.3.0 (`files/` folder), unchanged.
- `frontend/static/js/icons.js`: the Lucide icons used by the UI, as inline SVG copied from
  `lucide-static` 0.468.0 (`icons/<name>.svg`), the version the React UI used.

The server sends `Cache-Control: no-cache` for these files, so a refresh always
picks up edits. `python run.py --reload` also restarts the backend on Python changes.

```bash
# Backend tests (no paid model calls):
(cd backend && python -m pytest -q)

# Frontend tests run the real UI modules in Chromium with a fake backend.
# One-time setup in the same Python environment:
python -m pip install -c constraints.txt -e './backend[test,ui-test]'
python -m playwright install chromium
# Then:
(cd frontend && python -m pytest -q)

# Static checks and the personal-data check (also run by CI):
python -m pip install -c constraints.txt -e './backend[lint]'
python -m ruff check --select F,E9 backend frontend/tests scripts
python3 scripts/check_personal_data.py
```

GitHub Actions (`.github/workflows/tests.yml` at the repository root) runs the personal-data
check, the lint check and both test suites on Ubuntu and macOS for pull requests, pushes to
`main`, and on demand. It uses no credentials and makes no model calls. Notable changes are
listed in `CHANGELOG.md`.

`python -m playwright install chromium` downloads Playwright's own Chromium; to use
a browser that is already installed instead, set `UI_TEST_CHROMIUM` to its
executable path. The frontend tests need no Node.js either.

Tests cover all permission modes, approved/denied external CSV listings, persisted
folder grants, shell classification, approval allow/deny,
browser-controlled folder checks/grants/removal, OS-denied access,
cancellation (including process-group termination), path/symlink/secret boundaries,
credential-free shell environment, restart recovery, API access control, WebSocket
snapshots, stale editor writes, and keeping or undoing agent changes (whole files and single
changes, stale-review refusal, undo as a restorable checkpoint), read-before-write refusals
and the changed-files notice, `/api/git` not running a repository's `core.fsmonitor`
program or filters (a driver name containing `=` and a submodule's own filters included)
and leaving nested credential files out of the diff, startup with unreadable conversation
files (with command jobs, and a file holding another conversation's ID), dropping a
WebSocket that stalls, "Allow for this chat" rules (exact matching, Plan still denying,
exclusions and folder cards kept, persistence, removal, subagent inheritance, none in forks
or imports), decline notes reaching the model, and 409 details that name the blocking chat.
Permission-rule tests cover the rule matcher (exact and prefix allow rules with listed options,
broad deny and ask rules, short-option clusters, wrappers, inline interpreters and options that
run other programs never allowed, uncertain commands), project and all-project rules in
`permission_rules.json` (an unreadable file ignored until reset), the fixed decision order in
every mode including Don't ask and deny or ask rules in Bypass, protected files, the read-only
command list, commands allowed by a rule running from an argument vector without a shell,
read-only Git that runs no repository-configured program, the state folder kept out of the file
tools, mode and rule changes re-checking waiting cards and reaching a running subagent, edit
before approving (including a Verify card), turned-off tools, Verify following the mode and
rules, and `ask_user` and `submit_plan` (answers, skips, expiry, Keep planning, an approved plan
switching the mode, a mode change superseding the review, export and the Markdown transcript).
None of these automated
tests calls a paid model.
Frontend tests verify that expired local tokens refresh once and that other
errors never replay a potentially completed action, that editor drafts survive saves
and navigation, that delayed permission responses or deleted conversation URLs
do not corrupt the active view, that model replies cannot inject HTML or scripts, and
that the `/` skill menu lists, filters, and inserts skills without blocking ordinary messages,
that the pages of a long file read name the lines they covered, that agent changes
show in the changes bar, as dots in the file list and as an inline diff whose Keep and Undo
send the reviewed file state, that unreadable conversation files are named in a
dismissible notice that returns keyboard focus to the message box, that opening one
shows the server's message without a reconnect loop, that each kind of approval card offers
its own "Allow for this chat" button and sends decline notes, that the permissions menu lists
and removes rules, that an error naming another chat offers to open it, and that notifications
(with a faked Notification API and `document.hidden`), the tab-title count, sidebar status
badges, unread and Running filter, and the Files root switcher with change dots in granted
folders work without `localStorage` too, that the Continue button appears only after a
step-limit, output-limit, stopped or interrupted turn and sends its message, that the task
checklist follows pushed tasks per conversation, that drafts survive a reload, are evicted oldest
first, are removed when sent and keep text typed during the send, that code blocks are
highlighted only when a reply is complete and copy their exact text, and that the Markdown
transcript downloads and copies.
Permission-rule UI tests check the **Always allow…** form (only the scopes and prefixes the
server offers), that cards offering nothing to remember have no chat button, Edit on a card
(Save edit waits for a change), the Bypass confirmation, Don't ask, mode changes while the chat
is busy, rules by scope in the Permissions menu with removal, reset and keyboard focus, the
**Never asks** list, the add-rule dialog and turned-off tools in **Agent tools → Tools**.
Question and plan tests check the `ask_user` card (options, multi-select, Other, skip, expiry)
and the `submit_plan` card (approve with a mode, Keep planning with feedback, superseded).
UI tests also cover a turn that fails during an automatic compaction (notifies) and a manual
compaction (does not), a turn cut off by a server restart, closing answered notifications,
read state shared between tabs, and the page's presence connection.
Backend tests check the desktop notifier with a fake runner: nothing is sent while a page is
connected (with or without a chat open), a turn failing during automatic compaction notifies
and a manual compaction does not, a turn cut off by a restart is marked `interrupted`, the
command is an argument list without a shell, titles are cut to 60 characters, redacted and
escaped for `notify-send`, a step-limit stop is not called finished, a subagent chat you continue
yourself notifies, and notifier failures are ignored.
Backend regressions additionally cover credential
path aliases, unreadable configuration recovery, cancellation during startup or
after a child process outlives its shell, recovery of completed tool results
after interrupted delivery, tool calls streamed without arguments, skill commands
inside a longer message, compaction inside one request that outgrows the budget,
how a turn ended in the idle status, which restarts mark a turn interrupted, task pushes on change and on connect, `get_task`
scoping and permissions, and the Markdown transcript endpoint's content, redaction and headers
(including the file name).

## Architecture

- `backend/local_agent/api.py`: local API, WebSocket stream, page presence socket, workspace endpoints.
- `backend/local_agent/agents.py`: session/approval events and the Databricks tool loop.
- `backend/local_agent/tools.py`: scoped file queries/edits and tool definitions.
- `backend/local_agent/jobs.py`: streamed processes, managed jobs, and cleanup.
- `backend/local_agent/command_exec.py`: commands run from an argument vector without a shell (read-only `pwd` and `ls` from `/bin`, hardened Git).
- `backend/local_agent/rules.py`: the rule engine (command parsing, rule matching, uncertain commands), protected paths, the read-only command list and the saved `permission_rules.json`.
- `backend/local_agent/permissions.py`: the permission modes, the one decision every tool call and waiting card goes through (turned-off tools, rules, protected files, Don't ask), and model instructions.
- `backend/local_agent/approvals.py`: rules from cards and the Permissions menu (chat, project and all-project routes, reset), edit before approving, turned-off tools and the tool list, decline-note wording, and 409 details naming a busy chat.
- `backend/local_agent/interactions.py`: `ask_user` questions and `submit_plan` reviews that wait for the user inside a turn.
- `backend/local_agent/store.py`: per-conversation JSONL storage, unreadable-file reports, and legacy migration.
- `backend/local_agent/drafts.py`: bounded periodic streamed-reply checkpoints.
- `backend/local_agent/portability.py` / `portability_api.py`: validated conversation copies.
- `backend/local_agent/tool_profiles.py` / `tool_schema.py`: tool ceilings and offline input validation.
- `backend/local_agent/config.py`: external credentials and portable configuration.
- `backend/local_agent/model_profiles.py`: per-endpoint limits, default budgets and Settings limit warnings.
- `backend/local_agent/user_paths.py`: checks for the default code folder and the Python environment, and the environment commands run with.
- `backend/local_agent/context.py`: request estimates and bounded history compaction.
- `backend/local_agent/request_limits.py`: the request body as sent, the 4 MB request limit and request timeouts.
- `backend/local_agent/rate_limits.py`: Databricks 429 details, the learned OTPM limit and the wait shared by chats.
- `backend/local_agent/outline.py` / `overview.py`: `outline_file` and `project_overview`
  (`outline_ast.py`: the Python definitions, also run as a child process for large files).
- `backend/local_agent/result_budget.py`: the 128 KiB tool-result cap and the per-response context allowance.
- `backend/local_agent/activity.py`: app-generated activity log for handoffs (`insert_activity_log`).
- `backend/local_agent/transcript.py` / `scripts/conversation_transcript.py`: Markdown transcripts of saved conversations (also `GET /api/sessions/<id>/transcript.md`).
- `backend/local_agent/redaction.py`: common secret shapes replaced in logs, handoffs and transcripts.
- `skills/`: skills shipped with the app and offered in every workspace; `handoff/SKILL.md` is the detailed handoff skill. A workspace's `.agents/skills/<id>/` replaces the shipped skill with that ID.
- `backend/local_agent/telemetry.py`: inference ledger, validated usage, DBU estimates, and failure categories.
- `backend/local_agent/usage_export.py` / `scripts/usage_report.py`: usage rows and CSV correlated with conversation history.
- `backend/local_agent/instructions.py`: scoped project guidance loading.
- `backend/local_agent/recovery.py` / `worktrees.py`: file recovery and Git worktrees.
- `backend/local_agent/git_tools.py`: Git without a shell or repository-configured programs (worktrees, `/api/git`).
- `backend/local_agent/notify.py`: optional desktop notifications (status and title only) when no page is open.
- `backend/local_agent/changes.py`: the agent's file changes per conversation, their diffs, Keep and Undo.
- `backend/local_agent/shell_changes.py`: the files a foreground `run_command` changed, from Git status before and after it.
- `backend/local_agent/extensions.py` / `mcp_client.py`: skills, hooks, MCP lifecycle.
- `backend/local_agent/planning.py` / `feature_tools.py`: tasks and subagents (agent types, the slot limit, parallel batches, the subagents index).
- `backend/local_agent/feature_api.py`: scoped feature endpoints.
- `backend/local_agent/reasoning.py`: reasoning effort request fields per endpoint and format, the rejection
  check, and the thinking text of streamed deltas.
- `frontend/static/js/`: browser interface (Preact + htm, no build), conversation state, editor and settings;
  `attention.js` holds browser notifications, the tab-title count and unread tracking, `drafts.js` saves
  message drafts, `markdown.js` renders and highlights replies, and `components/AgentToolsDialog.js` is the Agent tools dialog.
- `frontend/tests/`: browser tests (pytest + Playwright) with a fake backend.
- `design/`: original visual concept and extracted design tokens.

Provider documentation:
[Databricks function calling](https://docs.databricks.com/aws/en/machine-learning/model-serving/function-calling),
[reasoning models](https://docs.databricks.com/aws/en/machine-learning/model-serving/query-reason-models),
[Foundation Model rate limits](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/limits),
[Foundation Model DBU pricing](https://www.databricks.com/product/pricing/proprietary-foundation-model-serving),
and [official Python MCP transports](https://py.sdk.modelcontextprotocol.io/client/transports/).
