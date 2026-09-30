# Local workspace

A local browser coding assistant powered by Databricks model serving, with streamed
chat, local file tools, shell commands, and per-conversation permissions.
The application is self-contained in this folder, with a Python backend and a browser
frontend that runs without a build step.

## Features

- Streamed Markdown chat and multiple persistent conversations.
- Databricks model selection, numbered line-range reads, paginated regex/literal
  search, file creation and exact edits, and shell commands with live output.
- Approval cards with proposed edits and command text. A declined action does not run.
- Per-conversation permissions below the composer: Manual, Auto, Accept edits,
  Plan and Bypass permissions. Access additional local folders through an approval card.
- Stop an agent response, including its foreground command or pending approval.
- Managed background jobs with explicit Stop, output retrieval, status/history,
  per-command time/output limits, and cleanup on normal server shutdown.
- Resume conversations after refreshing or restarting the app.
- Periodic partial-reply checkpoints, safe JSON conversation export/import, and
  completed-conversation forks with fresh permissions.
- Configurable context budgeting, automatic summaries of older turns (and of a single long
  request's earlier tool results), and a visible context meter. Full conversation history
  stays on disk, and a script turns it into Markdown transcripts.
- Automatic workspace-scoped `AGENTS.md` and `CLAUDE.md` guidance loading.
- Workspace file explorer, text editor with stale-file detection, Git status/diff,
  and a non-interactive command runner.
- File checkpoints with reviewed, reversible restore and managed Git worktrees.
- Explicit MCP servers, workspace skills with a `/` command menu, and approval-controlled
  tool lifecycle hooks.
- Persistent tasks with dependencies and bounded subagents in separate conversations.
- Collapsible activity and persisted provider reasoning summaries when returned by the endpoint.
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
platform. Linux/WSL have not yet been validated.

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

The app runs with Python and its runtime dependencies only
(`python -m pip install -c constraints.txt -e ./backend`).
Git and a supported shell are still needed for their respective tools.
The backend installation automatically installs FastAPI, Uvicorn, HTTPX, regex, jsonschema,
MCP (`mcp==2.2.0`), and the MCP HTTP transport dependency HTTPX2. No Claude SDK,
Claude CLI, or sibling SDK checkout is required. An optional MCP server may have
its own runtime requirements, such as Node; install those only for the servers you choose.

To move selected chat history between machines, use **Agent tools → Conversation →
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
content or one second of dirty state, including when the provider stalls. Normal
completion and Stop flush the final state. After an abrupt process death, the last
saved partial reply is shown as interrupted; the most recent uncheckpointed text
may be lost. Incomplete streamed tool arguments are never persisted as executable
requests or replayed. A local storage failure stops the response and reports that
recent history may not have been saved. Back up both the conversation JSONL files
and the separate operational SQLite database.

**Agent tools → Conversation** exports a versioned JSON bundle, optionally with
subagent conversations (32 total, 16 MiB maximum). Export requires idle chats.
Imports preserve historical messages, tool results, summaries, and usage, remap
conversation/event/call IDs and included child links, and cancel historical active
states. Credential settings, hooks/MCP configuration, task-board records, jobs, file checkpoint ownership,
and folder grants are not transferred. Conversation text can itself contain sensitive
data: inspect exported files before sharing them. Choose a trusted bundle and an
existing destination folder; imported paths never select the destination for you.
Repeated imports intentionally create separate copies. Unsupported versions, malformed
history, oversized bundles, and authority-bearing fields are rejected atomically.

**Fork completed conversation** makes a separate full-history copy in the same
workspace, leaving the original unchanged. Only completed, idle conversations can
be forked; running/failed turns and arbitrary turn selection are not supported.
Forks also start in Manual with no folder grants or active skills, retain any tool
profile restrictions, and do not copy child conversations, task-board records, jobs, or file checkpoints.

The sidebar reads the small metadata record at the start of each conversation file
instead of loading every event and provider-history record. Full content is loaded
when opening a conversation or performing restart recovery.

## Working with the app

- **New conversation**: choose a model, then send a task. Enter sends; Shift+Enter adds a line.
  Opening the root URL or clicking **New** starts with an empty context. Sending the
  first message creates a unique session, even if another chat has the same title.
  Select a sidebar conversation to resume it. Each saved chat has a `?session=<uuid>`
  URL, which preserves that selection on refresh and supports browser Back/Forward.
  The header and sidebar show a short session ID to distinguish repeated titles.
  Drafts stay with their conversation while switching chats in the current page;
  unsent drafts are not saved across a page reload.
  The message box stays editable while connecting. The permissions menu opens above
  the composer; clicking the input closes it, and choosing a mode returns focus to chat.
  Open tabs reconnect after a server restart without discarding the current draft.
- **Skills (`/`)**: type `/` at the start of the message box to list the skills this
  conversation can use, with their descriptions; keep typing to filter. Use ↑/↓ to
  highlight one and Enter or Tab (or click it) to insert `/<skill-id> `, then add your
  request and press Enter to send. Esc closes the list. A message that contains a
  listed skill's command as a word, at its start or anywhere else (`Read the notes and
  use /handoff to …`), selects that skill for the whole message. Other text with `/`,
  such as a path (`/tmp/x.log`, `~/handoff/`), is sent unchanged.
  See **Agent tools: MCP, skills, and hooks** below.
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
  Use the pencil beside a sidebar conversation to rename it (up to 160 characters).
  **Save name** or Enter saves; **Cancel rename** or Escape discards the edit.
  Renaming also works during a response and does not change its history, model,
  permissions, or selected conversation. Manual titles persist after restart and
  are never replaced by automatic naming, including a title request already in flight.
- **Workspace / Files**: browse a folder, edit UTF-8 text files up to 80 KB, or use
  the plus button to reference the file in chat. Save detects concurrent disk edits.
  Unsaved editor text stays with its conversation when closing the workspace panel,
  attaching a file, or switching chats. Text entered during a save remains unsaved
  until the next save. Editor drafts are kept in the current page, not across reloads.
- **Workspace / Changes**: inspect Git status and tracked-file diffs. Untracked
  paths appear in status; their contents are available in Files.
- **Workspace / Terminal**: run a command explicitly and see output while it runs.
  Set the timeout (1–3,600 seconds, default 60) and retained output limit
  (1,024–1,000,000 bytes, default 80,000). A foreground job disables Run until it
  finishes; **Run in background** lets you launch another command. Select a job
  to inspect output, or use **Stop job**. History survives closing/reopening the
  panel and refreshing the browser. Jobs belong to the selected conversation;
  commands launched before creating a chat belong to the selected workspace.
  This remains a noninteractive command runner: stdin is closed, with no PTY.
  Closing the panel does not cancel a job. Errors pause polling until **Refresh jobs**.
- **Approvals**: approve or decline model-proposed writes, commands, MCP calls, and hooks. An unanswered
  approval expires after five minutes; a browser refresh retains it while the server runs.
- **Tool details**: task cards show their names and task status. A completed create/update
  action does not mean the task itself is finished. Expand command cards to see the
  full command (including inline Python), options, and output separately. A long file
  is read in pages, so a read card names the lines it covered (`lines 109–243`).
- **Request details**: expand the details below an assistant response or tool-only
  model request to inspect its endpoint, completion status, stop reason, and
  provider-reported token usage when available. Failed requests distinguish invalid
  requests, rate limits, output limits, network failures, and other error categories;
  an HTTP status is shown when returned. Interrupted requests remain distinguishable
  after restart, and older conversations without metadata still display normally.
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
- **Stop**: cancel the current agent turn and its foreground command. Background
  jobs continue; stop them individually in Terminal. Approved writes and completed
  shell side effects are retained. Stop also cancels its active subagent. Deleting a
  conversation removes its jobs, tasks, and file checkpoint copies; edited files stay
  on disk. Child conversations remain separately available in the sidebar.

### Conversation storage

The default state directory is `local-agent-workspace/.local/`, overridable with
`LOCAL_AGENT_STATE_DIR` in `.env`. The directory is created at startup. Changing
the value does not move existing data: stop the server before copying state to a
new location, update `.env`, and then restart the app. Keep the state directory
outside the selected project workspace when using a custom name so agent file
tools cannot browse it.

- `conversations/<conversation-id>.jsonl`: one complete, private file per chat.
  Records include conversation metadata, user and assistant events, reasoning and
  request usage, tool calls and results, and the exact Databricks message/tool
  history (`wire`). Commands associated with the chat are included as sanitized job
  records with their command, state, limits, exit status, and bounded retained output.
  Files are atomically replaced as current-state snapshots rather than appended
  indefinitely while an answer streams.
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
It does not estimate missing token counts. This is not an account-wide report or an
invoice: regional uplifts, provisioned endpoints, negotiated pricing, credits, and
delayed billing adjustments can differ. Older chats that predate the ledger show
visible agent-call totals as incomplete because title, summary, and retry attempts
cannot be reconstructed. A delegated child's requests stay in its own conversation.
Databricks responses may omit usage.
See the [Databricks API reference](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/api-reference)
for the provider's usage fields. The context meter below remains a separate
pre-request estimate, but reported input tokens calibrate it (see below).

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
For chat and context-summary calls, an HTTP 429 `REQUEST_LIMIT_EXCEEDED` response
received before streaming begins is retried at most three times. The app honors numeric or HTTP-date `Retry-After`
headers and root/nested JSON `retry_after` values, capped at 60 seconds; otherwise
it waits 5, 15, then 40 seconds. A rate-limit signal after streamed content begins
is not retried because repeating a partial response could be unsafe.

### Context and project instructions

**Settings → Context budget (tokens)** defaults to 131,072 for new or previously
unset settings. Explicit saved values, including 32,768 and the former 131,000
default, are preserved.
The allowed range is 16,384–1,048,576; these are app configuration bounds, not a
claim that every endpoint supports that range or the default. Set the budget at
or below your endpoint's actual total context limit; the app cannot infer a
serving endpoint's limit from its name. Changes apply on the next turn, including
existing conversations. The configured total includes space for both input and
reply. **Settings → Max output tokens** defaults to 8,192 and accepts
1,024–131,072, but must be below the context budget minus the 2,048-token safety
margin. The output setting is also the reply reserve, so the defaults leave an
estimated input budget of 120,832 tokens. You can explicitly select 20,000 for one
long Claude Opus 4.8 response, but that reserves its entire standard 20,000 OTPM
allowance at admission; subsequent agent/tool-loop requests can receive 429s until
earlier output leaves the rolling window. The 8,192 default leaves throughput for
multi-step work, while output-limit responses can continue automatically in smaller
steps up to the bounded retry limit.
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
Context details show the applied scale. A conversation's first request to a model,
or older requests recorded before this calibration, use the unscaled heuristic.
Expanded context details attribute that same estimate to system/project
instructions (including selected skills), tool definitions, conversation messages
and tool results, the retained summary, and request framing/rounding overhead.
The categories add up to the displayed estimate; they are not tokenizer-derived
measurements. Automatic compaction is attempted when the estimate exceeds the
input budget, after the reply and safety reserves have been deducted.

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

When older turns no longer fit, the same endpoint summarizes them in bounded
requests with at most 4,096 output tokens per summary request. Summary chunks use
that independent reply reserve instead of the main response reserve, so increasing
the main output setting does not multiply the number of compaction requests. The
retained summary may use about an eighth of the input budget, between 3,500 and
12,000 UTF-8 bytes (12,000 with the default settings); that byte cap is not a token
count. A summary that comes back longer is not discarded, because producing it may
have required a large billed request: a small extra request, containing only that
summary, asks the model to condense it, and a second one condenses that result if it
is shorter but still too long. A condensing request may use about one output token per
two bytes of the limit (6,000 tokens for 12,000 bytes, at least 4,096), so a slightly
long reply is not cut off. If condensing fails or is still too long, the
app keeps the start and end of the summary and omits part of the middle. Either case
adds a visible notice and is shown in context details. These summary limits are
separate from the main reply limit and total context budget.

**Settings → Save a detailed handoff at each compaction** is on by default. Each
automatic or manual compaction then asks the model for a detailed Markdown handoff of
the turns being summarized (status, decisions, issues and fixes, file changes, key
facts, open items, next actions) instead of the short summary. The same request that
already carried those turns writes it, so no extra conversation is resent; it may use
up to an eighth of the context budget in output tokens (16,000 with the default
budget), and a non-streamed reply can take a few minutes. A small follow-up request
condenses the handoff into the in-context summary. The app saves the handoff, with a
generated activity log of every command and file, to
`<workspace>/handoffs/auto/<date>-<chat-title>-compaction-<N>.md`, adding a suffix
instead of overwriting an existing file. After compaction, the summary message lists
the newest five saved handoffs so the model can read the relevant section with
`read_file` or `search_files` when it needs details the summary omits. A handoff cut
off at its output limit is still saved and used, with a note. If the file cannot be
written (for example, the folder resolves outside the workspace), a notice explains
it and compaction continues with the summary only. Subagent conversations do not
write compaction handoffs. Turn the setting off for the previous summary-only
compaction.
The newest turn and its complete tool exchanges are retained verbatim;
the preceding turn is also retained when space allows. One request can outgrow the
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
If the request, its latest tool results, or project guidance alone are too large, the app
reports an error instead of silently cutting it. The error states the estimated
tokens needed and the input-budget arithmetic (context budget minus Max output
tokens minus the 2,048-token safety margin). When Max output tokens is above the
8,192 default, it suggests lowering that first: for example, 131,000 context with
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

At each model request the app rereads root `AGENTS.md`, then `CLAUDE.md`. File tools
also discover guidance in the target directory and its ancestors, from broader
to more specific scopes. These encountered scopes persist for the conversation;
their guidance applies only to their own directories. More specific rules refine
broader rules; user instructions and enforced tool permissions retain precedence.
`CLAUDE.md` is just a supported text filename: it does not enable Claude mode or
require the Claude SDK. README files, imports, skills, parent directories outside
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

Open **Agent tools** in the header. **Recovery** lists checkpoints created before
successful `write_file`, `edit_file`, and editor saves. Preview the reverse diff,
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

Recovery covers regular UTF-8 files up to 80 KB. It preserves existing bytes and
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
used by any saved conversation or running command, and keeps the branch and its
commits. Delete the worktree's conversations and choose another default workspace
before removal. Git hooks, filters, and fsmonitor are disabled for managed worktree
operations. This is deliberate workspace isolation, not automatic per-task copies.

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
approval in Manual, Auto, and Accept edits; Plan blocks them and Bypass permits
them. MCP servers and hook commands have the user's OS/network access, not the
file tools' folder restrictions. Credential environment variables are stripped
from local server/hook processes. Up to four servers, 32 tools, and 16 KB of tool
schemas are supported; each call has a 60-second deadline and bounded 8 KB result.
Connections close at turn end or cancellation. A discovery failure is reported,
not silently interpreted as an empty working integration.
An MCP result with `is_error=true` is displayed as an error even when the server
request itself succeeded. Its bounded diagnostic remains available to the model;
the app does not automatically retry the action.

Install workspace skills at `.agents/skills/<skill-id>/SKILL.md`. Skills shipped with
the app in `skills/<skill-id>/SKILL.md` are available in every workspace; a workspace
skill with the same ID replaces the shipped one. Other folders, such as
`.claude/skills`, are not read. Optional simple frontmatter supplies `name` and
`description`. Enable a discovered skill in the panel, use `/<skill-id>` as a word in a
message (type `/` at the start to choose from the list), begin a message with
`/skill <skill-id>`, or ask the
model to use its `list_skills` / `use_skill` tools. Selection persists per chat, instructions reload
on each model request, and a newly selected/changed skill must reach the model
before a write, command, or MCP call. At most three selected skills fit a combined
8 KB instruction budget. Skills never override user instructions or permissions;
this version has no marketplace, installer, dependency execution, or automatic
matching engine.

The app ships a detailed handoff skill at `skills/handoff/SKILL.md`, available in every
workspace: send `/handoff Create a handoff for this conversation` (or type `/han` and
press Enter to insert the command). To change it for one project, copy it to
`<chat workspace>/.agents/skills/handoff/SKILL.md` (for example
`my-project/.agents/skills/handoff/SKILL.md`) and edit the copy; that copy then
replaces the shipped skill in that workspace. It asks for a cold-start
document in the style of a long working-session memory file, written in parts of
about half the file text one response can hold (about 20 KB at 20,000 Max output
tokens, about 8 KB at the 8,192 default) so no single response hits the output limit,
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
the messages and replies in full, one line per tool call with its main input, and
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
review the file before sharing it. The finished file must stay within 80 KB: the
oldest commands are omitted with a note when needed, so insert the log into a
separate file to keep every command. Delegated subagent conversations keep their own
logs.

Tool arguments are validated against their advertised JSON Schema before approvals,
hooks, or execution, then pass the existing semantic/path checks. Validation is
offline and bounded: only local acyclic JSON pointers are supported; remote/dynamic
references and patternProperties are rejected. Explicit dialects must be JSON Schema
2020-12. Unsupported schemas produce a diagnostic instead of executing unvalidated
input. Existing MCP schema/argument caps remain in force.

Hooks support `before_tool`, `after_tool`, and `tool_failure`. Each enabled hook receives JSON on
stdin with the event, conversation ID, tool, arguments, stable `call_id`, and (after execution) result.
The optional `tools` array selects exact names (including MCP names); omit it for
all tools, or use an empty array to match none. Failure hooks receive the original
error/outcome for invoked tools that fail, including MCP `is_error` results. Schema,
policy, and approval rejections and cancelled actions do not launch failure hooks.
Foreground commands with failed/timed-out results also trigger failure hooks;
background-job completion does not launch a deferred hook.
Each hook separately asks for approval except in Bypass; Plan skips hooks. A declined
or failing before-hook blocks the tool. An after-hook failure displays a warning
while retaining the completed action/result. Failure-hook errors also preserve the
original error. Hooks have a 1–30 second timeout and
bounded output. They do not recursively invoke hooks. Enable only commands you
intend to run for every applicable tool; configuration can change only while agent
turns are idle.

### Agent tools: tasks, subagents, and activity

**Tasks** shows persisted tasks, descriptions, status, and dependencies. Add them
manually or let the model use `create_task`, `list_tasks`, and `update_task`. A
conversation holds up to 50 tasks; dependencies must belong to the same chat,
cannot form cycles, and must finish before dependent work starts or completes.
These planning records are available in Plan mode.

The model can use `delegate_task` for one concrete task with explicitly supplied
context. The child has a separate transcript/context and inherits workspace,
model, permission mode, and folder grants. Files remain shared: choose independent
work to avoid conflicting edits. One child runs per parent, at most three globally;
children have 1–8 model steps (default six), cannot delegate again, and cannot start
managed background jobs. The parent waits for a bounded result and an honest
completed/failed/cancelled status. **Open subagent** opens the child to review its
work or approve actions; **Back to parent conversation** returns to the parent.
Stop cancels both parent and active child. This is bounded delegation, not a
multi-agent team scheduler or automatic task assignment.
The parent card links to its child immediately and updates live with approval
waits, the last tool, completed-action count, and a structured stopping reason
(completion, user Stop, step limit, inference/tool error, or interruption).
Progress metadata does not copy child tool payloads into the parent's context.
The child's first prompt is labeled **Delegated by parent conversation** and
retains the spawning action ID; later human messages are not labeled delegated.
Unfinished delegated runs are marked interrupted on restart rather than left running.

**Tasks → New subagent tool ceiling** selects `inherit`, `read_only`, or `file_editor`
for future children while the parent is idle. The model may request a narrower
`tool_profile` in `delegate_task` but cannot widen the parent's ceiling. Read-only
permits file listing/reading/search, skill discovery/selection, and task listing;
file-editor additionally permits file writes/edits under normal approval rules.
Both file-only profiles exclude shell commands, jobs, MCP startup/calls, hooks,
task mutations, and further delegation. Restrictions persist on child continuation,
imports, and forks. Existing children are unchanged by a new ceiling. These are
server-enforced model-tool limits, not an OS sandbox or restrictions on the user's
explicit editor/terminal actions.

**Activity** collapses a list of actual tool actions and their states; existing
approval cards remain usable independently. A separate **Provider reasoning
summary** disclosure appears only when the Databricks stream supplies documented
`reasoning` content blocks with `summary_text` entries. Summaries persist with the
conversation, display separately from the answer, and have a 12,000-character
limit. Opaque/encrypted reasoning is ignored. Summaries are not replayed as model
history, and the app never fabricates hidden reasoning. Endpoint support varies;
ordinary responses legitimately have no reasoning disclosure. There is no
reasoning-effort setting yet.

### Permission modes

Choose a mode beneath the chat input. Existing conversations default to Manual.
Changes are saved per conversation; stop an active response before switching modes.

| Mode | Agent behavior |
| --- | --- |
| Manual | Read approved folders; ask before edits and commands. |
| Auto | Accept file edits and exact `pwd`, `ls`, `ls -la` commands; ask for other commands/actions. |
| Accept edits | Accept file edits; ask before commands and other actions. |
| Plan | Read/search approved folders and describe a plan; block file changes and shell commands. |
| Bypass permissions | Run actions and access external folders without approval cards. Credential/generated-file exclusions still apply to file tools. |

Inspecting/stopping existing jobs, skills, structured tasks, and delegation are available
in every mode. Delegated tools still follow the child's inherited permission mode.
Starting a background job follows the same command approval rules as foreground
execution. Changing modes does not cancel a previously authorized background job.
Auto uses the app's explicit local rules. Manually saving in the editor or clicking
Run in the terminal is an explicit user action outside the agent's permission mode.

## Local access and limits

The server binds to `127.0.0.1`, checks Host/Origin, and uses a random local access
token to protect its API/WebSocket. Databricks credentials stay server-side. File
tools resolve paths and symlinks before checking the project and approved additional
folders. Bypass mode skips that folder boundary; credential files and generated
directories remain excluded. This is a trusted single-user local app, not a multi-user sandbox.
Commands allowed by the selected mode execute with your OS permissions and can reach beyond the project.
The shell strips model credentials from its inherited environment.

**Settings → Agent steps** defaults to 32 model requests per user message and accepts
1–64. This is a model-request ceiling, not a tool-call count: one model response can
request several tools. Reaching the ceiling preserves completed actions and stops
until the user continues or raises the setting. **Max output tokens** defaults to
8,192 per request with the bounds described above. The editor and file edits retain
their 80 KB limit. Agent reads use
`read_file(path, start_line=1, max_lines=200)` and return numbered lines plus
`next_line`; `max_lines` can be 1–1,000. Reads scan at most 8 MB from the start of
a regular UTF-8 file, reject lines over 128 KB, and return at most 8 KB of serialized
JSON per page. They can therefore inspect larger source files without loading
those files into the editor. File reads, search results, and job-output pages have
explicit continuation markers. The filesystem can change between pages.

The agent's `list_files` returns `entries` in pages of at most 8 KB. Pass the
returned `next_offset` with the same path, depth, and glob to continue. Discovery
still stops at 300 matches per query; `listing_limit_reached` indicates that the
folder or glob should be narrowed to find additional files. The file explorer's
existing listing API is unchanged.

`search_files` supports literal search by default, `regex=true`, `case_sensitive`,
`context_lines` (0–5), `offset` (0–10,000), and `max_results` (1–100, default 50).
Results identify file/line, adjacent lines, clipped excerpts, omitted files, and
`next_offset`. Query length is capped at 1,000 characters. Search skips binary,
excluded, unreadable, and over-2-MB files; scan limits are 16 MB of file content,
5,000 files, 2,000 directories, and three seconds. Regex matches have an additional
50 ms timeout. Limit errors ask for a narrower query rather than claiming no matches.
The Python `regex==2026.9.10` dependency enforces regex timeouts and is installed
by the normal backend installation command.

`run_command` accepts `timeout_seconds`, `max_output_bytes`, and `background`.
It returns a job ID and the first output page. `list_jobs`, `get_job_output`, and
`stop_job` inspect, paginate, and stop jobs in the same conversation. The job's
exit code and state distinguish success, failure, timeout, and cancellation.
Only the configured prefix of output is retained; excess output is drained and
reported as truncated. Foreground cancellation, timeouts, command completion,
and normal app shutdown clean up the command's process group. Use managed
background mode instead of shell `&`, detached daemons, or commands that escape
the process group. This is process supervision, not an OS sandbox. After an
abnormal server death, previously running jobs are marked **interrupted** and
never replayed; their outcome and surviving processes must be checked manually.
Automatic compaction adds bounded summary requests beyond the configured agent steps.
Databricks inference is billed to your workspace. Endpoints must support streaming,
function calling, and non-streamed text responses for summarization.

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
- `frontend/static/vendor/`: Preact 10.29.8, htm 3.1.1, marked 18.0.14 (Markdown)
  and DOMPurify 3.4.16 (sanitizing), copied from their npm releases with their
  licenses; only the trailing source-map comment was removed. To update one,
  download the new version from `https://cdn.jsdelivr.net/npm/<package>@<version>/`
  and replace the file:

  | Vendored file | Package file |
  |---|---|
  | `preact.module.js` | `preact/dist/preact.module.js` |
  | `hooks.module.js` | `preact/hooks/dist/hooks.module.js` |
  | `htm.module.js` | `htm/dist/htm.module.js` |
  | `marked.esm.js` | `marked/lib/marked.esm.js` |
  | `purify.es.js` | `dompurify/dist/purify.es.mjs` |

  DOMPurify is dual-licensed (Apache-2.0 or MPL-2.0); both texts are included.
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
```

`python -m playwright install chromium` downloads Playwright's own Chromium; to use
a browser that is already installed instead, set `UI_TEST_CHROMIUM` to its
executable path. The frontend tests need no Node.js either.

Tests cover all permission modes, approved/denied external CSV listings, persisted
folder grants, shell classification, approval allow/deny,
browser-controlled folder checks/grants/removal, OS-denied access,
cancellation (including process-group termination), path/symlink/secret boundaries,
credential-free shell environment, restart recovery, API access control, WebSocket
snapshots, and stale editor writes. None of these automated tests calls a paid model.
Frontend tests verify that expired local tokens refresh once and that other
errors never replay a potentially completed action, that editor drafts survive saves
and navigation, that delayed permission responses or deleted conversation URLs
do not corrupt the active view, that model replies cannot inject HTML or scripts, and
that the `/` skill menu lists, filters, and inserts skills without blocking ordinary messages,
and that the pages of a long file read name the lines they covered.
Backend regressions additionally cover credential
path aliases, unreadable configuration recovery, cancellation during startup or
after a child process outlives its shell, recovery of completed tool results
after interrupted delivery, tool calls streamed without arguments, skill commands
inside a longer message, and compaction inside one request that outgrows the budget.

## Architecture

- `backend/local_agent/api.py`: local API, WebSocket stream, workspace endpoints.
- `backend/local_agent/agents.py`: session/approval events and the Databricks tool loop.
- `backend/local_agent/tools.py`: scoped file queries/edits and tool definitions.
- `backend/local_agent/jobs.py`: streamed processes, managed jobs, and cleanup.
- `backend/local_agent/permissions.py`: shared mode rules and model instructions.
- `backend/local_agent/store.py`: per-conversation JSONL storage and legacy migration.
- `backend/local_agent/drafts.py`: bounded periodic streamed-reply checkpoints.
- `backend/local_agent/portability.py` / `portability_api.py`: validated conversation copies.
- `backend/local_agent/tool_profiles.py` / `tool_schema.py`: tool ceilings and offline input validation.
- `backend/local_agent/config.py`: external credentials and portable configuration.
- `backend/local_agent/context.py`: request estimates and bounded history compaction.
- `backend/local_agent/activity.py`: app-generated activity log for handoffs (`insert_activity_log`).
- `backend/local_agent/transcript.py` / `scripts/conversation_transcript.py`: Markdown transcripts of saved conversations.
- `backend/local_agent/redaction.py`: common secret shapes replaced in logs, handoffs and transcripts.
- `skills/`: skills shipped with the app and offered in every workspace; `handoff/SKILL.md` is the detailed handoff skill. A workspace's `.agents/skills/<id>/` replaces the shipped skill with that ID.
- `backend/local_agent/telemetry.py`: inference ledger, validated usage, DBU estimates, and failure categories.
- `backend/local_agent/usage_export.py` / `scripts/usage_report.py`: usage rows and CSV correlated with conversation history.
- `backend/local_agent/instructions.py`: scoped project guidance loading.
- `backend/local_agent/recovery.py` / `worktrees.py`: file recovery and Git worktrees.
- `backend/local_agent/extensions.py` / `mcp_client.py`: skills, hooks, MCP lifecycle.
- `backend/local_agent/planning.py` / `feature_tools.py`: tasks and delegated workers.
- `backend/local_agent/feature_api.py`: scoped feature endpoints.
- `backend/local_agent/reasoning.py`: provider reasoning-summary extraction.
- `frontend/static/js/`: browser interface (Preact + htm, no build), conversation state, editor and settings.
- `frontend/tests/`: browser tests (pytest + Playwright) with a fake backend.
- `design/`: original visual concept and extracted design tokens.

Provider documentation:
[Databricks function calling](https://docs.databricks.com/aws/en/machine-learning/model-serving/function-calling),
[reasoning models](https://docs.databricks.com/aws/en/machine-learning/model-serving/query-reason-models),
[Foundation Model rate limits](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/limits),
[Foundation Model DBU pricing](https://www.databricks.com/product/pricing/proprietary-foundation-model-serving),
and [official Python MCP transports](https://py.sdk.modelcontextprotocol.io/client/transports/).
