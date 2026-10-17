# Changelog

Notable changes to Local Agent Workspace, newest first. Dates are UTC.

## Unreleased

Roadmap milestone M8 (agents, automation and delivery):

- Background subagents: `delegate_task` with `background: true` returns at once (the child's ID
  and `running`, or `queued` while the three subagent slots are taken) and the chat goes on; its
  turn may end while the subagent works. A queued one starts within what the chat allows when
  its slot frees; one that then cannot start (for example `delegate_task` was turned off, or the
  chat's mode now needs approval for it) ends as failed, with a notice and result that say why.
  The card shows **Background** and live progress. When the subagent ends, its result appears on
  the card's **Report**, a notice says so (**Background subagent explore finished: …**, with
  **Open subagent**) and the chat's next model request names it once.
- The new `get_subagent_result` tool (offered once the chat started a background subagent)
  returns one subagent's status and result, or lists them, and can wait up to 300 seconds. Stop
  on the chat, **Stop subagent** on a card and deleting the chat stop background subagents. A
  restart marks a running one interrupted and says so in the chat, and the spending limit counts
  them. Rewinding, editing, retrying and forking the chat are refused while they run. A desktop
  notification and the notification hooks (status `background_subagent`) report one that
  finished or failed while the chat was idle.
- **Settings → Subagents → Continue the chat when a background subagent finishes** (off by
  default: each such turn is a paid request) sends an idle chat a short app message, so the
  agent reads the report and goes on, once for what ended since its last step.
- Subagent cards: **Stop subagent** stops only that subagent (its call returns as cancelled; the
  chat and its other subagents go on). While a subagent waits for approval, its card names the
  action (**Waits for approval: run command · npm test**) with **Allow** and **Decline**, which
  answer the subagent's own card. **Allow** is offered only when that line is the whole action
  (a one-line command with its folder, a move or a delete). A command with more lines, a file's
  new content, another tool's arguments, or a card that warns (credential-looking or protected
  files, another subagent's change, a hook's ask; the parent's card names the warning) shows
  **Review in subagent** next to **Decline**. A question or a plan shows **Answer in subagent**.
- Two running subagents of one chat changing the same file: the second one's change shows an
  approval card naming the other subagent, in every mode (Don't ask refuses it). The files of a
  subagent whose run ended are free again, and the chat's own edits never ask. A command that
  changed such a file adds a notice in the chat.
- Custom agents: new fields `disallowed_tools`, `permission_mode`, `mcp_servers` (only those
  servers start; `[]` none), `memory: none`, `initial_prompt` (added to the first prompt) and
  `background`, and Claude Code's names `disallowedTools`, `permissionMode`, `mcpServers`,
  `initialPrompt` and `maxTurns`. The approval card and Agent types show them. The app ships a
  **plan** agent (Plan mode, no edits, no MCP servers) that returns an implementation plan; a
  project's `plan.md` replaces it.
- A custom agent's `permission_mode` applies only when it is narrower than the chat's mode, also
  after a mode change while the subagent runs, so it never runs more than the chat allows: a
  Plan subagent of a Manual or Accept edits chat asks before read-only commands (unless a rule
  allows them), and one of a Don't ask chat is refused them. The approval card says so.
- **Agent tools → Usage** adds **This chat and its subagents**: the chat's calls, tokens and
  estimated cost together with its subagents', and each subagent with its own and **Open**.
  **Export with subagents** adds their rows to the CSV (`usage.csv?include_subagents=true`). A
  subagent's entry in its parent's index keeps its token totals, also after it is deleted.
- Hook input names the agent: tool events (and any event of a subagent chat) carry `agent` with
  the session, whether it is a subagent, its type, its parent chat and its custom agent file.
- Turn summary: under the last reply of each turn a muted line sums it up, for example
  `8 steps · 14 tools · 2 min 10 s · 182k in (160k cached) / 3.1k out · ~1.4 DBU` (no DBU part
  without price data). Opened, it says how the turn ended, the tokens it processed with its
  subagents, calls without price data and the files it changed. A count no call reported shows
  `—`, never 0. The same result record is kept with the chat, ends the turn for scripts and is a
  `_Turn: …_` line in transcripts; exports, forks and imports leave it out.
- Token and time limits per turn: **Settings → Agent → Token limit per turn** and **Time limit
  per turn (minutes)** (0, the default, means none), and a chat's own in **Agent tools → Usage →
  Turn limits**. The token limit counts every token the turn processed, cache reads and its
  subagents' included, so it also caps endpoints without price data. The time limit is the
  wall-clock time since the turn started, waits for your approvals included. A subagent counts
  toward its parent's turn.
- At a turn limit the agent gets one more request, which tells it to call no tools and to reply
  with what is done and what remains (a tool call in that reply does not run). That instruction
  travels like the other step notices: at the full prompt-caching level it ends the request's
  last message, otherwise the system message. Then the turn ends with a notice, desktop
  notifications say "Reached the turn budget", and **Continue** starts a new turn with a new
  budget. Raising or removing the limit during that last request lets the turn go on as usual.
- **Agent tools → Usage → All chats**: every conversation's model calls over the last 7, 30 or
  90 days, by day (cache reads, uncached input and output), endpoint, purpose and project, with
  the estimated DBU. It reads a small usage summary each conversation keeps in its file's first
  line; a chat saved before this is read once and its summary saved, 50 per refresh, and the
  view says how many are left. `scripts/usage_report.py --all <state dir>` prints the same
  offline.
- List prices for more pay-per-token endpoints (the Standard rates of 2026-10-16 from the
  Databricks pricing pages): the other Claude Opus and Sonnet endpoints, Claude Haiku 4.5,
  GPT-5, 5.1 and 5.2, GPT-5 Mini and Nano, GPT-5.3 Codex, GPT-5.4 Mini and Nano, GPT OSS 120B
  and 20B, Llama 4 Maverick, Llama 3.3 70B, Llama 3.1 8B and Gemma 3 12B. Their calls now have
  DBU estimates and count toward the spending limit. The full table is under **All chats → List
  prices**.
- Scripting the app: `scripts/agent_run.py "prompt"` runs one prompt in a new chat of the
  running app (Plan mode by default) and prints the final reply, one JSON result or every
  message as JSON lines (`--output text|json|stream-json`). Exit codes: 0 completed, 1 error, 2
  a limit reached, 3 stopped. Cards are declined at once by default (`--approve deny`) or asked
  in the terminal (`--approve ask`), which shows control characters from a card (terminal escape
  sequences, carriage returns, bidirectional overrides) as visible escapes, so a command cannot
  hide behind them. Bypass permissions needs `--confirm-bypass`. An answer the app refuses falls
  back at once to declining (or skipping the questions), so no card is left waiting. Ctrl+C
  stops the turn, and before a turn started it exits without waiting.
- `agent_run.py` options set the chat's model, effort, mode, Agent steps, spending limit and
  turn limits. `--session` sends the prompt to an existing idle chat instead: `--mode`,
  `--model` and `--effort` then change that chat, the new-chat-only `--workspace` and
  `--max-steps` are refused, and a chat already in Bypass permissions needs `--confirm-bypass`.
- `backend/local_agent/client.py` is the standard-library Python client behind the script:
  `query(prompt, can_use_tool=..., on_question=...)` yields the chat's messages and ends with
  the turn's result. The app serves a chat's live messages as newline-delimited JSON
  (`GET /api/sessions/<id>/stream.ndjson`). A new chat can name its project folder and Agent
  steps, and its step-limit notice then says the chat has its own. Stopping the app (Ctrl+C on
  `run.py`) ends these streams at once and stops running turns as usual.
- Simulator: `start_gateway()`, `patched_settings()` and `start_app()` can be imported by tests
  and other launchers.
- **Settings → Agent → Git status in messages** (on by default): the message that starts each
  reply carries a short Git snapshot of the project after your text: the branch, its upstream
  with ahead and behind counts, how many files are staged, unstaged, untracked and conflicted
  (with up to 8 paths), and the last five commit subjects. It is at most 1,200 bytes, redacted
  and without credential files; messages sent while the agent works carry none. Git runs
  hardened as in the Changes view (no shell, fsmonitor, hooks or filters) with a 5-second limit;
  after a timeout the app does not ask again for 10 minutes, and outside a repository nothing is
  sent. The snapshot is part of the message, not the system message, so the prompt cache holds.
  A **Git status** chip under the message says it was sent. Subagents get none.
- New shipped skills: `/commit` (stage only the related files by name, write a message in the
  repository's style, commit), `/pr` (push the branch and open the pull request with
  `gh pr create`, never merge), and `/review` and `/security-review` (read-only reviews that
  report with `report_findings`). Every `git` and `gh` command they run goes through its
  approval card.
- Review findings: the new `report_findings` tool is offered once a skill that names it is
  selected. It runs without a card in every mode (Plan included) and for read-only subagents,
  and shows a **Review findings** card: counts by severity, `path:line` buttons that open the
  file at the line, details, suggestions and **Copy findings as Markdown**. The card and the
  transcript are redacted, and the transcript and the export keep the findings. A subagent's
  findings reach the parent's agent at the top of its report.
- **Workspace → Changes**: **+** and **−** on each row stage and unstage that file (both paths
  of a rename), **Stage all** and **Unstage all** act on a group, and **Commit N files** commits
  what is staged with your message. Only the chosen files are staged, never a folder or an
  ignored file. Credential files, generated folders, submodules, paths outside the project and
  files with a Git filter (such as Git LFS) are refused. Commits run without hooks or a signing
  program. A commit is refused when a staged file is a credential file (or a rename of one) or
  outside the project folder; a staged deletion of a credential file (`git rm --cached .env`)
  may be committed. Both wait while a chat in the project replies or a command runs there.
  `.env.example` is no longer treated as a credential file there: its diff is shown and it can
  be staged and committed, as the file tools and command cards already allow.
- **Workspace → Changes** shows the branch's pull request, for example "PR #12 · open · 3/4
  checks", when the GitHub CLI is installed and signed in. `gh pr view` runs without a shell and
  without the Databricks credentials (it gets the desktop keyring's session bus, so a sign-in
  kept there is found), within 10 seconds and at most once a minute per project and branch. A
  `gh` inside the project never runs.

Roadmap milestone M7 (endpoint-dependent model features):

- Endpoint checks: **Settings → Model and context** has **Test model** (one short streamed
  request) and **Check endpoint capabilities…**, about ten small requests that check the smoke
  test and usage fields, the reasoning fields and the thinking stream's shape and signature,
  prompt-cache markers and a cache read on a repeat, the finish reason with a tool call and
  whether the tool result is accepted with and without the reply's signed thinking,
  `response_format` with tools, and an image part. Checks can be unticked. The dialog lists each
  request with its max tokens and worst-case cost at list prices, and the total: at most about
  7.1 DBU for all checks on Claude Opus 5.5 and 0.6 DBU for Test model (token caps for an
  endpoint without price data). Nothing is sent until you press **Run (about N DBU)**, and every
  run asks again; the server refuses a request without that approval, or for another plan,
  without sending anything. Requests go out one at a time and are never retried: a 401, 403 or
  429, a server error or a failed connection, also one that breaks off a reply, stops the run,
  and **Stop** cancels it.
- The results are kept per endpoint in the state folder (`endpoint_checks.json`), with sanitized
  copies of each request and reply (no signatures, ids cut to their prefix, no host, URL or
  token) that the tests' replay client can read. They are shown as a capability table (probed
  with its date, documented or unknown) in the dialog and in **Agent tools → Overview**. They
  change agent requests in three places only: cache markers found rejected start the
  prompt-caching fallback lower, an image part found rejected turns the endpoint's image input
  off, and signed thinking found rejected is not sent back to it.
- **Settings → Reasoning → Send earlier thinking back (experimental)**, off by default: during
  tool use each request carries the signed thinking of the turn's earlier replies (at most 64 KB
  per reply and 256 KB per request), only to the endpoint that wrote it, never to a fallback
  endpoint or after a model change. It counts in the context meter and is kept in the
  conversation file only for the latest turn while the setting is on. When such a request gets
  an HTTP 400 the app sends it once more without the thinking; a 400 that names the thinking or
  a signature, or a resend that is then accepted, makes the endpoint skip it until a restart or
  a Settings save. **Export with reasoning** keeps the thinking in a conversation file; a plain
  export leaves it out.
- **Settings → Reasoning → Request format** adds **Anthropic thinking budget**
  (`thinking.budget_tokens`, 1,024 at Fast up to 32,768 at Max, below Max output tokens) for
  older Claude endpoints. When Databricks rejects the adaptive Anthropic fields for an endpoint
  that does not always think, the app now sends the request again with a thinking budget instead
  of no reasoning at all, and only then without reasoning fields.
- Attached documents and images: paste, drop on the message box or **Attach files** (the
  paperclip below the box) adds PDF, Word (`.docx`), Excel (`.xlsx`), PowerPoint (`.pptx`), CSV,
  TSV, text and Markdown files and PNG, JPEG, GIF and WebP images to the next message, at most 8
  files of 20 MB each. Each file uploads at once (`POST /api/sessions/<id>/uploads`, the raw
  bytes with the name in `X-File-Name`) and is kept in `<state>/uploads/<chat id>/` (folders
  0700, files 0600; at most 100 files and 200 MB per chat); its chip shows the type, the pages,
  sheets or slides or the pixel size, and the estimated tokens. Copied text still pastes as
  text. The message waits until its files have uploaded. Queued messages, **Edit message**,
  **Retry response** and editing a queued message keep the files, and **Edit message** can leave
  one out (**Do not send … again**). Deleting a chat deletes its files; forks and rewind backups
  get their own copies.
- Documents reach the agent as their extracted text, fenced as data after the message like an
  `@` snapshot and redacted like command output: at most 50,000 characters per document and
  100,000 per message, with a note when one was shortened. Word, Excel and PowerPoint text is
  read with the standard library: headings as `#` lines, tables and sheets as tab-separated rows
  (at most 5,000 rows, 200 columns and 32,767 characters per cell, Excel's own limit), and
  marker lines for pages, sheets and slides. Reading stays within limits against ZIP bombs
  (10,000 parts, 64 MB of uncompressed XML counted as it is read), refuses XML with a DOCTYPE or
  entity declaration, and wraps long lines in one pass, so a small crafted file cannot hold a
  CPU for minutes or take gigabytes of memory. PDF text needs the new optional documents extra
  (`./backend[documents]`, the `pypdf` package); without it the app says so, and **Attach as
  path** sends where the file is saved instead. A document's extracted text is kept beside the
  file (`.extracted.txt`), so the path names the file as it was attached.
- `read_file` reads `.pdf`, `.docx`, `.xlsx` and `.pptx` project files as their extracted text,
  in numbered lines wrapped at 500 characters, with what it was extracted from (`extracted`) and
  notes; the last 4 documents read stay extracted in memory, and its tool description says so.
  An `@` name of such a file is refused with a hint (attach it with **Attach files**, or ask the
  agent to read it with `read_file`), so a document's text is never extracted while the app
  serves other requests.
- Images go only to endpoints that take image input: Claude Opus 5.5 and Opus 4.8, unless an
  endpoint check found image parts rejected. For other endpoints the chip warns and sending is
  refused with a message naming the endpoint. An image can be at most 2.5 MB and 8,000 pixels on
  a side, and a message at most 4 images and 2.5 MB of them, so the base64 copy fits the 4 MB
  request limit; the app does not shrink images. Each image is sent as an `image_url` part with
  a base64 data URL, after a short text part that names it and marks any text in it as data, not
  instructions. It is in the request body only (the conversation file keeps a reference: name,
  type, size), estimated at width × height / 750 tokens after scaling to at most 2,576 pixels on
  the long side and 3.75 megapixels, and counted by the meter and the 4 MB check. Side questions
  send the chat's images like its own requests.
- After a switch to an endpoint without image input, in a request to such a fallback endpoint,
  or when its file is gone, an image becomes a note in its message; a compaction summary request
  gets a note naming each image instead of the image. An endpoint that answers image parts with
  an HTTP 400 naming them gets the request again with notes in place of the images, with a
  notice, and no images until the app restarts or Settings are saved.
- A queued message steered into a running turn must also keep the request under the 4 MB limit
  (attached images count in full); otherwise it waits and goes as the next message. A compaction
  that only the 4 MB limit called for says so in its notice instead of naming a token threshold.
- Exports include the text of attached documents (it is in the message) but not images or files:
  each image becomes a note in its message, as does an image reference in an imported bundle.
  Imported chats keep the names of their attached files but not the files, so **Retry response**
  and **Edit message** send their messages without them. Transcripts and the chat view name the
  attached files.
- Fallback endpoint: **Settings → Model and context → Fallback endpoint** (default None) names a
  second endpoint for agent and subagent requests. When the chat's endpoint fails before a reply
  starts (no connection, HTTP 502, 503 or 504, or HTTP 429) and the automatic retries are used
  up (and, for a rate limit, the automatic waits too, none when **Settings → Agent → Continue
  automatically after rate limits** is off), the app sends that one request to the fallback,
  with a notice. The next request, also the next step of the same turn, tries the chat's
  endpoint again. The fallback is never used after the reply started, for HTTP 400, 401, 403 or
  500, for a content-filter stop, or for titles, `/btw` side questions and compaction.
- The fallback request uses that endpoint's own limits, reasoning fields and prompt-cache
  markers; for an endpoint whose context window the app does not know, budgets you customised
  count only up to its defaults unless it is the Settings model. A conversation that does not
  fit its input budget is not sent there, and a notice says so. Settings accepts only an
  endpoint the app knows or one you already use (the Settings model, **Settings → Subagents** or
  one of your chats), because each request carries the conversation. A reply from the fallback
  shows **Answered by** (the fallback) and **Fallback from** (the chat's endpoint and why it
  failed) in Request details. The usage ledger records the call under the endpoint that
  answered, priced at its own rates, with `retry_reason` `fallback` and `fallback_from`. Chats
  with a fallback reply export, fork and import with these details.
- `submit_report`: subagents end with a structured report (summary, findings with path and line,
  files changed, next steps), checked against its schema: an incomplete one is an error the
  model fixes and sends again, and a valid one ends the subagent's turn without another request.
  The parent's result carries it as `report` (redacted, bounded with the rest of the 6,000-byte
  result). The card's **Report** shows the summary, findings as `path:line` links that open the
  file, and next steps. A subagent that answers in prose still reports its reply, and in a
  subagent chat you continue yourself the agent answers in its reply instead.
- Endpoint profiles gain capability flags: `vision` (image input, documented for the
  Databricks-hosted Claude models and not for GPT OSS; attached images use it) and
  `structured_output` (not confirmed for any endpoint yet, so off). `POST /api/settings/limits`
  shows them with the other profile facts.
- Every model request (agent and subagent requests, compaction, titles, `/btw`) and the
  connection check now build their URL, headers, timeouts and HTTP client in one place
  (`serving.py`). A `ReplayClient` answers them from scripted or recorded responses for offline
  tests; an unmatched request fails instead of reaching the network. Nothing changes in what is
  sent.
- Simulator: `--replay accept|require|reject` (signed thinking sent back: the gateway signs its
  reasoning items and checks the signatures), `--vision accept|reject`,
  `--response-format accept|reject`, older Claude endpoint names that take only a thinking
  budget, the single-endpoint state route, and scripted answers for the endpoint checks.

Roadmap milestone M6 (MCP, hooks and web):

- One MCP server that fails no longer ends every turn: each server connects on its own, and a
  server that does not start, answer within 15 seconds or list its tools is closed at once and
  skipped while the other servers' tools work. The chat gets one notice naming it (not again
  while it stays broken), and a failed server is tried again only 60 seconds after the failure
  (**Test saved configuration** tries it at once; saving a change resets the wait). A server
  whose tools would go over 32 MCP tools or 16 KB of definitions, with the servers before it, is
  skipped the same way, and is not started again while those servers take the same room.
- **Agent tools → Extensions → Last turn in this chat** (and a line on the Overview) shows each
  server's status: connected with its tool count and the server's name and version, failed or
  skipped with the reason and the end of a stdio server's error output, turned off in this chat,
  or disabled. That error output is shown up to 2,000 characters, redacted; only its last 8 KB
  is kept in memory while the server runs, and nothing is written to disk. Test results also
  show the server's name, version, own instructions (shown there only, never sent to the agent)
  and its tools' hints.
- **Agent tools → Tools** has a switch per configured MCP server: off, the server never starts
  in that chat (saved as `mcp__<server>__*` in the chat's turned-off tools; subagents inherit
  it).
- Databricks accepts at most 32 tools per request. Built-in tools go first: 28 in a new chat
  with the default Settings, and 32 outside Plan mode when every optional one is offered
  (`get_tool_output`, `web_search`, `report_findings` and `get_subagent_result` on top of the
  defaults), which leaves no room for MCP tools. MCP tools follow in server order; those that do
  not fit are named once in a notice and in Extensions, and a call to one is refused. Turn off
  tools or servers a chat does not need to make room.
- In Plan mode the agent is no longer offered the tools Plan always refuses: the file-changing
  tools (`write_file`, `edit_file`, `move_path`, `delete_path`, `notebook_edit`,
  `insert_activity_log`), `remember` and `stop_job`. It still gets `submit_plan`, so a chat in
  Plan sends fewer tool definitions and leaves more room for MCP tools.
- Secret references for MCP servers: a stdio server's `env` and an HTTP server's `headers` take
  text or `{"secret": "NAME"}`, read from your credential file each time the server starts. A
  header can add a `"prefix"`, which must be a scheme word and one space (such as `"Bearer "`),
  so no part of a secret is saved in plain text. Values are never saved in the configuration or
  shown, and are redacted from results, errors, test diagnostics, hook output, cards, titles,
  the usage ledger and exports. A missing key fails only that server. Plain values that look
  like secrets, loader and startup variables, the app's own credentials and transport headers
  are refused; secret headers need https (plain http only to localhost), and a reference that
  holds the Databricks token fails its server.
- Secret values a server echoes in its tool descriptions, schemas or tool titles are redacted
  before they reach the model, the saved tool definitions or the card; a server whose tool name
  holds one fails.
- `"workspace_credentials": true` sends your Databricks token to an HTTP server only when its
  URL is https on your workspace's own host and port, for Databricks managed MCP servers under
  `/api/2.0/mcp/` (their tools run on billed workspace compute; each call still asks).
- Up to 12 MCP servers can be configured, 4 of them enabled at a time. New server IDs cannot
  contain `__`, which separates the server from the tool in tool names.
- **Agent tools → Extensions** is a form (a block per server and per hook, with environment
  variables and headers as text or secret references), with **Edit as JSON** for the same
  configuration. The form keeps an argument with a line break (such as a `python -c` script) as
  it is; change it with **Edit as JSON**.
- **Import MCP servers**: this project's `.mcp.json`, the Claude desktop app's configuration or
  pasted JSON is shown as a preview and added to the form disabled. Nothing is saved or started
  until you save and enable a server. Environment and header values are never copied: each
  becomes a secret reference, and only a Bearer, Basic or Token scheme is kept as a header
  prefix. The preview lists the servers, the credential-file keys to add (and which ones your
  credential file already has, with the server that would get each; names only, never values)
  and what was skipped. It names a value that mixes text and a variable after its key, skips a
  server whose arguments pass a secret after a flag such as `--api-key` or `--token`, and warns
  that a project's `.mcp.json` was chosen by whoever wrote the project.
- Richer MCP results: embedded text resources, links and structured content (as JSON after the
  text, left out only when the text repeats it exactly) reach the agent; binary resources and
  audio are described. Images are described for the agent, never sent to it, and up to two per
  result can be shown on the card (**Show image**) while the app runs.
- A long MCP result is no longer cut off at 8 KB: the agent gets the first page with a
  `result_id`, and the new `get_tool_output` tool (offered in a chat once it has such a result,
  then in every request) reads the rest in pages. When a response's result allowance shortens a
  result, the agent can read the omitted part from offset 0. Results are kept in memory for this
  app run (at most 20 per chat, 8,000,000 characters in all) and removed when the chat is
  deleted.
- A tool card shows the MCP server's hints about the tool (for example "Server says:
  read-only"); they never change an approval.
- README: a GitHub MCP example, the Databricks managed MCP URLs, and a 10-line Python template
  for a local stdio tool server.
- New `web_fetch` tool, on by default (**Settings → Web access → Let the agent fetch web pages**
  turns it off): the agent reads one public `http` or `https` page as text. HTML becomes plain
  text with `#` headings, `[text](url)` links, lists, tables and code blocks; JSON is indented;
  other text is kept; images, PDFs and other types are refused. The body is read up to 2 MB,
  counted after decompression (one gzip or deflate layer; stacked or other encodings are
  refused), and converted off the server's event loop. The first 8 KB come back with the call,
  and a longer page is read on with `get_tool_output`. Page text is redacted and labelled as
  untrusted web content, never instructions.
- `web_fetch` refuses, before anything is sent, every address outside the public internet:
  `localhost` and other local names (`*.local`, `*.internal`, names without a dot), loopback,
  private, link-local (cloud metadata), carrier-grade NAT, multicast, reserved and documentation
  addresses, also when an IPv6 address embeds one or an IPv4 address is written another way
  (`2130706433`, `0x7f000001`). It also refuses ports other than 80 and 443, user names and
  passwords in the URL, and URLs that carry a secret. Nothing is looked up before the approval.
  Each connection resolves the name itself, refuses it when any address is private and connects
  to the checked address, on every redirect too (at most 5, same host; another host comes back
  for a new call). No proxy, cookies or credentials are sent.
- A fetch card asks per domain: **Allow once**, **Allow *domain* in this chat**, or **Always
  allow…** for the project or all projects. New domain rules
  (`{"tool": "web_fetch", "domain": …}`): allow names one host exactly, deny and Always ask also
  cover its subdomains, and a deny without a domain removes the tool. Plan always asks, Don't
  ask fetches only what a rule allows, and Bypass fetches without a card. **Permissions → Add a
  rule…** has **Web fetches**.
- New `web_search` tool, off until **Settings → Web access → Web search provider** names Brave
  Search, Tavily or your own SearXNG address. The API key stays in the credential file: Settings
  keeps only the entry's name (`BRAVE_API_KEY` or `TAVILY_API_KEY` by default), the key is
  redacted everywhere, and Settings says when it is missing. The first search in a chat shows
  its query on a card, and approving it allows searches for the rest of that chat. Results are
  titles, URLs and snippets, labelled as untrusted.
- Custom agent files written for Claude Code may name `WebFetch` and `WebSearch`: they stand for
  this app's `web_fetch` and `web_search` (not available to read-only agents).
- New hook events: `before_compaction` runs once before a compaction's first summary request
  (its `context` joins the preservation note; it cannot stop a compaction, and a `decision` is
  ignored and listed on its card), and `notification` runs for each of the app's own
  notifications (approval needed, finished, stopped with an error, a limit reached), also with a
  page open, only when it may run without a card; its output is ignored.
- Hook `tools` lists take patterns with `*` and `?` (`mcp__github__*`, `*_file`) as well as
  exact names.
- **Agent tools → Extensions**: **Run without asking (all projects)** under a saved hook saves
  an allow rule for that hook ID in one click (**Ask again** removes it).
- Commands that name a credential-looking file always show an approval card that names the file
  ("Reads a secret file: …"), in every mode, Bypass permissions and Don't ask included. No rule
  skips it, a mode or rule change never approves it, it cannot be remembered, and Plan does not
  run it. Such files include `.env`, `.envrc` and `.env.*`, `.env-*` or `.env_*` names (not
  `.env.example`), `.databrickscfg`, `.netrc`, `.pgpass`, private keys and `*.pem`, files in
  `.ssh`, `.aws/credentials`, kube configs, `secrets.yaml`, `api_token.txt` and similar, the
  configured credential file under any spelling, and what a glob in the command matches. A
  relative name counts in the folder the command reads it from (its `cwd` or a `cd <folder>` in
  it, such as `cat config` in `~/.kube`), and a link counts as the file it points at. Source and
  tokenizer files (`tokenizer.py`, `tokenizer.json`) and bare words such as `token` in a search
  do not count. **Settings → Commands and files → Secret file names for commands** adds your own
  patterns (`*.tfvars`).
- Command and MCP results are redacted before the agent sees them: besides the Databricks token
  and MCP secret references, the credential file's values for keys that name a token, secret,
  password, key, credential, auth or cookie, GitHub, Databricks and AWS access-key shapes, long
  Bearer tokens and passwords in URLs.
- File tools and Git diffs also leave out `.databrickscfg`, `.git-credentials`, `.pypirc`,
  `.pgpass`, `id_ecdsa` and `id_dsa`.
- `run_command` takes an optional `cwd`, the folder to run in (the agent is asked to use it
  instead of `cd <folder> &&`). The project, a folder granted to the chat and the default code
  folder run there; any other folder first shows the Folder access card (refused in Don't ask
  and Plan, none needed in Bypass). The card says "Runs in: <folder>", the heading reads
  `<folder> › command`, background jobs keep their folder, and how long a command usually takes
  is learned per folder. Allow rules for an exact command or a prefix apply only in the project
  folder; **Allow for this chat** on a card for another folder remembers the folder too; deny
  and ask rules apply everywhere.
- Auto and Plan run more read-only commands without a card: commands and pipelines of read-only
  programs the app checks option by option (`cat`, `head`, `tail`, `wc`, `grep`, `rg`, `sort`,
  `uniq`, `cut`, `tr`, `ls`, `find`, `du`, `diff`, `sed -n`, a restricted `awk` that may not
  read `ENVIRON`, read-only Git and a few more), joined by `|`, `&&`, `||`, `;` or line breaks,
  after a `cd <folder> &&`, with `2>&1` and `/dev/null` redirects, on paths in approved folders
  that do not look like credentials. The app parses the text, expands globs itself, writes a
  quoted script with each program from outside the project (a planted `grep` in the project
  makes the command ask) and Git hardened, and runs it with `/bin/sh`.
- Recursive searches in such commands skip credential-looking names (the `.env` names above
  included, and names without an extension that end in token, secret, password, credential or
  apikey, such as `db_token`), generated folders and the state folder. In Auto, a part that an
  exact or prefix allow rule allows can join such a command in the project folder when no pipe
  feeds it (`python -m pytest -q 2>&1 | tail -20`). Deny and ask rules are matched part by part.
  Anything else asks as before, and in Plan the refusal says what the check refused. Cards read
  "Ran as a read-only command (checked by the app)".

Roadmap milestone M5 (context, instructions, memory, skills and commands):

- Built-in `/` commands in the message box: `/new`, `/clear` (a new conversation with this one's
  model, permission mode and effort; this one is kept, and Bypass permissions asks for
  confirmation first), `/compact [note]`, `/model [name]`, `/mode [mode]`, `/context`, `/usage`,
  `/export`, `/fork`, `/rename <title>`, `/copy` (`/copy all` for the whole conversation) and
  `/help`. They run in the page and are never sent to the agent or queued; `/compact`, `/model`
  and `/fork` are refused while a reply runs (stop it first). A path such as `/tmp/x.log`, an
  unknown `/word` or a command that takes nothing followed by text is sent as an ordinary
  message. The `/` menu lists them first, with their arguments, then custom commands, then
  skills.
- Custom commands: a Markdown file at `.agents/commands/<name>.md` (or, read only,
  `.claude/commands/<name>.md`, and your own `~/.agents/commands` and `~/.claude/commands`) runs
  with `/<name>` at the start of a message. Its text, with `$ARGUMENTS` and `$1` to `$9` filled
  in from what you typed, goes to the agent after your message. Frontmatter sets `description`
  and `argument-hint`; `allowed-tools` and `model` are listed as ignored, so a command never
  changes the chat's tools, permissions or model. `` !`command` `` lines are never run, and
  `@path` in the text attaches files with the same checks as when typed. A queued command is
  expanded when it is queued, so errors show at once. **Agent tools → Extensions** lists the
  commands and the files that could not be read. `/<name>` of a file in a command folder that
  could not be read as a whole (for example one with more than 100 entries) is an error naming
  the folder, not plain text.
- `/<skill-id>` in a message now sends that skill's instructions with that message only (at most
  three skills and 8 KB per message), instead of selecting it for the rest of the chat. The
  system message stays the same, so its prompt cache is kept, and a chip under the message names
  the skill. Skills checked in **Agent tools → Extensions** or loaded by the agent still apply
  to every request. Queued messages select no skill, and a skill named like a command is reached
  with `/skill <id>`.
- Your own skills in `~/.agents/skills` and, read only, `~/.claude/skills` are available in
  every workspace, after the project's skills and before the shipped ones. Skill frontmatter can
  set `argument-hint` (shown in the `/` menu), `disable-model-invocation: true` (only you select
  it: the agent cannot load it, name it in a subagent's task or give it to a custom agent) and
  `user-invocable: false` (not offered with `/`). `$ARGUMENTS` in a skill's text becomes the
  text after its `/<skill-id>`.
- **Send as plain text** (the T button below the message box, or Shift+Alt+Enter) sends the next
  message exactly as typed: no command, no skill, no `@` file and no `!` shell command. Retry
  and Edit send such a message the same way again, also in a fork or an imported copy of the
  conversation. With **Settings → Commands and files → Run commands typed with /** (on by
  default) turned off, typed `/` text is sent as it is in every message; built-in commands then
  run only when chosen in the `/` menu.
- Your own instruction files: **Settings → Agent → Load my instructions file
  ~/.agents/AGENTS.md** (on by default) and **Also load ~/.claude/CLAUDE.md** (off by default)
  add them after Custom instructions, with the same precedence (8,000 bytes each, with their
  imports). The app only reads them, and a change never holds back a running reply's write.
- Project guidance gains `AGENTS.local.md` and `CLAUDE.local.md`, read after the shared file of
  the same folder and meant for personal notes listed in `.gitignore`; `AGENTS.local.md` is a
  protected file like `AGENTS.md`. `@path` imports work outside code, relative to the importing
  file, up to three levels deep and 10 per file, cycle-safe, confined to the workspace, and
  never read credential files.
- Project rules in `.agents/rules/*.md` and the read-only `.claude/rules/*.md` (at most 30 files
  of 8,000 bytes each): a rule with a `paths:` glob frontmatter applies once a file tool used a
  matching file, so the next write to such a file waits for one request that includes it.
  `{a,b}` alternatives work unquoted (`src/**/*.{ts,tsx}`), an empty `paths:` omits the rule
  with a warning instead of applying it to every file, and the files a path-scoped rule imports
  are headed with its globs.
- The project instructions budget is 24,000 bytes (was 16,000). Over it, every file that fits
  loads whole, the start of the first file that does not fit fills the rest (when at least 1,000
  bytes remain), cut at a line break and marked, and the others are omitted. The context details
  show **Trimmed · ~N tokens loaded** or **Omitted** with the reason.
- Text loaded from instruction files, their imports, project rules and style files has the
  configured Databricks token and common secret shapes replaced with `[REDACTED]` before it
  reaches the system message.
- Project memory: the agent can save short notes about a project with the new `remember` tool,
  offered while **Settings → Agent → Project memory** is on (the default). Every note shows an
  approval card with the exact text, which **Edit** can change; Manual, Accept edits and Auto
  ask, Plan blocks it, and Bypass and Don't ask save one only with an allow rule. Notes that
  look like secrets are refused; a project holds at most 50 notes and 4,000 bytes. The notes are
  kept in the app's state folder per Git repository (worktrees share them), never in the
  repository or an export. They reach every chat's system message as data, read when a reply
  starts so the cached prefix stays the same within a reply. **Agent tools → Memory** lists,
  adds, edits and deletes them.
- **Settings → Agent → Output style**: Default, Concise or Explanatory, or a style file from the
  project's `.agents/output-styles/` or your `~/.agents/output-styles/` (yours wins over a
  project file of the same name). Its text (a style file's headed with the file it came from)
  joins the stable part of the system message after your instructions and only shapes how
  replies are written.
- New shipped skills: `/verify` (run the project's own tests, lint and build and report the
  results), `/debug` (reproduce, one hypothesis at a time, minimal fix, verify) and `/init`
  (draft or update the project's `AGENTS.md` from its README, manifests and CI files).
- Step notices after the cache marker: at the full prompt-caching level (**Settings → Model and
  context → Prompt caching** at Automatic on a Claude endpoint), the changed-files,
  finished-background-jobs and turn-limit wrap-up notices end the request's last message, after
  its cache marker, as `[Note from the app, not the user's words or tool output: …]`, instead of
  the end of the system message. A notice that appears or goes no longer makes the next step
  write the whole conversation to the cache again. Only that request carries it, never the saved
  history, an export or a summary. At Documented forms only, Off, a level lowered after a
  rejection and on other endpoints, the notices stay at the end of the system message. The
  simulator's prefix cache shows every step reading back the step before it.
- Anchored estimate: when Databricks reported the whole input of the chat's previous agent
  request and the next request starts with the same messages, the estimate is that count plus
  the scaled estimate of what changed since. Automatic compaction, the budget checks, side
  questions (`/btw`) and the context meter use it ("Anchored on the last reported count: …", and
  the next-request estimate says it is anchored), so a side question fits whenever the chat
  itself has room. Chats of plain prose, which the heuristic overestimates, compact later,
  closer to their real size.
- The anchor lives in memory only. After a restart, a compaction, a rewind or an edit, or a
  change to another model (changing back before any request anchors again), the scaled estimate
  applies until the next report; a fallback request or a partial or double-counted report leaves
  the previous anchor. The usage ledger, the estimate scale, the cuts compaction considers and
  the 4 MB check keep the heuristic estimate.
- Compaction progress: "Compacting context: condensing (request 2) · 1 min 05 s" names the
  request (writing the handoff, summarizing or condensing) and counts from the server's start
  time, also on a page opened or reloaded mid-compaction. It says whether the next request or
  this request's tool results passed the auto-compact threshold (nothing for **Compact now**).
- Summary requests leave out the tool results of the older summarized turns (all but the latest
  three; results of 2 KB or more) with a short marker naming the size, tool and file; a failed
  command keeps the last 600 bytes of its output. A compaction inside one long request keeps
  that request's results. A long chat makes fewer and smaller paid summary requests; the agent's
  context and the saved history keep every result.
- The summary, handoff and condense prompts say that the app records the files read and changed,
  so the summary's "Files read and changed" names only the files that matter for the work ahead.
- Context meter: tool definitions are split per MCP server ("Built-in tools", "MCP <server>",
  also for an older server ID containing `__`) when an MCP server's tools are offered, and
  **Loaded instructions** lists each selected skill with its cost, for the chat or for the
  message that selected it. `GET /api/sessions/<id>/context` returns the meter without building
  a request.

From the stress tests of 2026-10-16 on a live Databricks workspace (Claude Opus 5.5):

- Prompt caching on Claude endpoints: agent and subagent requests to Claude Opus 5.5 and Opus
  4.8 mark where the system message's stable part ends and their last message (`"cache_control":
  {"type": "ephemeral"}`), so input that repeats an earlier step is billed at the cache-read
  rate (about 5% of the input price; writes cost 25% more and expire after 5 minutes). Titles,
  `/btw` side questions, compaction requests and other endpoints get no markers. **Settings →
  Model and context → Prompt caching** is Automatic, Documented forms only (only the marker
  forms Databricks documents, for a route that ignores the others instead of rejecting them) or
  Off.
- When Databricks answers HTTP 400 to a request with cache markers, the app sends it again with
  fewer (only the documented forms, then none) and, once one is accepted, says so in a notice
  and keeps that level for the endpoint until a restart or a Settings save. The resend is marked
  `cache_rejected` in the usage ledger and does not use up the retries after rate limits. A 400
  that names caching drops the markers before any reasoning field. A failed connection or a
  timeout on the resend is handled as on any request, not taken for another rejection, and the
  reply that is then answered shows no error category.
- The usage ledger keeps, per agent and subagent request, the cache markers it was sent with
  (`prompt_cache`), whether its system message's changing part differed from the chat's last
  answered request (`system_tail_changed`), the key names of the usage Databricks sent
  (`usage_keys`), the shape that carried the reasoning (`reasoning_shape`) and whether a
  reasoning signature came (`reasoning_signature`); names and flags only, never values.
- Simulator: Claude endpoints report usage in the real route's shape (top-level cache read and
  write counts, no reasoning tokens), check cache markers and keep a 5-minute prefix cache
  (`cache: read N, wrote M` per request), counting input at about 1.1 times the app's estimate as
  the real route does; `--cache documented|reject` rehearses a route that refuses some or all
  markers, and `--cache ignore` one that accepts them but caches nothing. Claude summaries start
  after a short pause and end with a blank line.
- Automatic compaction keeps the turn before the latest one verbatim only when the request then
  needs at most 60% of the auto-compact threshold, so a compaction leaves room for the next
  steps. Before, it could keep a large preceding turn and leave the request at over 90% of the
  threshold, so the next steps each resent that turn and it was summarized a few minutes later.
- **Compact now** summarizes every turn before the latest one (it used to keep the preceding
  turn too, so a manual compaction often freed little).
- When there is nothing to compact (every earlier turn is already summarized, or the chat has
  one turn), the context meter disables the note field and **Compact now** and says why, until
  your next message. A compaction request in that state is refused at once, also when the only
  message after the summary was sent while the agent worked; before, it started and then failed
  with an error.
- An oversized summary is condensed by one request instead of up to two, and that request asks
  for about four fifths of the size limit, because models overshoot a size they are given. A
  condensed summary still over the limit has its least important sections shortened by the app
  at no cost. The compaction notice and the meter now say so accurately (the saved handoff has
  the shortened sections in full) and count the summary and condensing requests apart.
- The Claude Opus 5.5 starting estimate ratio (1.3) is no longer replaced by the first report:
  it keeps the weight of 20,000 estimated tokens, so a first request that is mostly system
  message and tool definitions no longer makes the next large file reads look up to about 14%
  smaller than they are. Compaction requests, and reports that look like they counted cached
  tokens twice, no longer change the agent estimate scale.
- At a context budget close to the window of an endpoint the app knows (Opus 5.5: 1,000,000
  tokens; GPT OSS 120B: 131,072), the input budget keeps 5% of the window for estimate error
  (934,826 on Opus 5.5 at 1,000,000 and 16,384; 115,078 at GPT OSS 120B's defaults, which fill
  its window), and each summary request leaves room for counting 1.2 times its estimate, so a
  near-full request is not rejected for passing the window. Opus 5.5 budgets up to about
  950,000, including the default 200,000, are unchanged. Settings names the budget requests use
  next to the estimated input budget, also in the auto-compact figure.
- The context meter's details close when the chat starts waiting for your approval or answer, so
  they never cover the new card.
- Rate limits: a 429 body that gives only an error code and a message (as Databricks sends them)
  now names its limit too: the app reads input or output tokens per minute, queries per hour,
  minute or second from the message's words (one type only; numbers in words are never read, so
  no limit is learned from them). Queries per minute (QPM) is a new limit label.
- Every HTTP 429 before streaming is retried, whatever its body says (also an empty, plain-text
  or proxy page). A rate limit reported inside the stream is labelled a rate limit and still not
  retried.
- Rate-limit notices (a retry, an automatic continue, a wait for another chat) have **Details**
  with the 429 behind them: its code, its message (redacted, at most 200 characters) and the
  wait Databricks asked for, or "Retry-After: none". Request details show the message of a final
  429 even when it names no limit, and the usage ledger keeps the message, the requested wait
  and whether it came in a `Retry-After` header (the CSV is unchanged).
- A chat waiting for another chat's rate limit now retries 1 to 3 seconds after that chat's own
  retry instead of at almost the same moment.
- Settings: Max output tokens above the endpoint's output-tokens-per-minute limit is a warning,
  no longer an error: Databricks documents that such a request is rejected, but some workspaces
  accept more. The README and the context-budget note say that Databricks admits short bursts
  above its per-minute limits.
- Claude Opus 5.5 reasoning fields were checked on a live workspace: the chat-completions route
  accepts thinking at every effort level, streams the summary and counts thinking in output
  tokens. Request details now read "In output tokens (not reported separately)" for Reasoning
  tokens on such requests instead of "Unavailable", and usage totals show a token field no call
  reported as unavailable rather than 0.
- Request details show when a reply's thinking text began, for example "Thinking text began
  after 9.0 s" (Databricks sends it a few seconds after thinking starts; "Thought for N s" is
  unchanged).
- The system message names the shell commands run with (zsh, bash or sh, and that zsh does not
  split an unquoted `$VAR`) and asks polling loops to print one line per check.
- The step-limit notice adds what the turn cost, for example "This turn used about 42.5 DBU
  (estimate)." (left out without price data). The README recommends a per-chat spending limit
  for anyone raising Agent steps; the default stays 32.
- A card that starts waiting for you (an approval, a question or a plan) scrolls into view also
  when you had scrolled up, and the hint under the message box ("An action is waiting for your
  approval above.") has **Show**, which brings the waiting card back into view and moves
  keyboard focus to it.
- The changes bar names a file outside the project by the allowed folder or default code folder
  it is in (`other/pkg` instead of the whole absolute folder), or by `…/` and its last two
  folders; the tooltip and the Keep and Undo labels keep the full path.
- File tool cards name a file in the project by its project path and one in an allowed folder or
  the default code folder as `<folder>/rest`, so the file name is no longer cut off. A command
  that starts with `cd <folder> &&` into the project or such a folder reads `<folder> › rest` in
  its heading; the tooltip, the expanded card and the approval card keep the whole command.
- A running command card counts how long it has run against its timeout (`running · 8 min 42 s
  of 40 min`, `8:42` in a narrow chat) and shows its latest output line while collapsed, so a
  quiet command no longer looks stuck.
- "Compacting context…" counts the time since compaction started and, for an automatic
  compaction, says that the next request passed the auto-compact threshold and that summary
  requests, which are not streamed, can take a few minutes.

Follow-up of 2026-10-15:

- Custom agents: Markdown files in a project's `.agents/agents` (or `.claude/agents`) define
  subagents with a description, tools, profile, model, effort, skills and default steps. The
  model chooses them from a short agent index in the system message, and `delegate_task`
  checks the name. Agent tools → Subagents lists every agent type and any agent file that
  cannot be used, with the reason.
- Each subagent type can have its own endpoint and reasoning effort (**Settings →
  Subagents**); explore now runs at Fast by default. Cards, Agent tools and the subagents index
  show the effort and model a subagent started with, and a subagent on a smaller endpoint keeps
  its requests within that endpoint's budgets.
- A custom agent never widens what its chat allows: its tools stay within the subagent tool
  ceiling, turned-off tools, rules and mode, and Manual mode's approval card names the agent,
  its tools, model and effort before it starts. Its `model` must be an endpoint you already use
  (the Settings model, Settings → Subagents or one of your chats); any other runs on the
  chat's model, with a note.
- Spending limit per conversation: a soft cap on the estimated DBU of a chat and its
  subagents, checked before each agent model request. **Settings → Agent** sets the default
  (0 = none), **Agent tools → Usage** shows the spend against the limit and sets a chat's own.
  One notice at 80%; at the limit the turn ends before the next request, with **Continue**
  and a "Reached the spending limit" notification. A subagent's spend stays counted when it is
  stopped, deleted or continued by you.
- Hooks can answer with one JSON object on stdout (stderr is shown on the card, never read as
  JSON; output that looks like JSON but does not decode fails the hook): a before-tool hook can
  deny a call, ask for an approval card (also in Bypass), replace its arguments (checked again
  against the schema, rules and card) or add context for the agent; after-tool and failure
  hooks can add context. Decoded text is redacted like plain output.
- New hook events: `user_prompt` (block, edit or extend your message), `session_start`,
  `agent_stop` (keep the agent working, at most 3 times), `subagent_start` and `subagent_stop`
  (one more round, or a replacement report). Up to 16 hooks.
- Hook cards say what a hook decided, with a before/after view of changed arguments; messages
  show a hook's edit or block; Agent tools → Extensions lists the events and their JSON output.

Requests of 2026-10-14 (testing on a new machine):

- Reasoning effort per conversation: **Default**, **Fast**, **Medium**, **High**, **Extra
  high** or **Max**, chosen beside the model picker and applied from the agent's next request,
  also while a response runs; subagents use their chat's effort. Each endpoint gets the fields
  it takes: Claude Opus 5.5 and 4.8 `thinking` (adaptive) and `output_config.effort`, GPT OSS
  `reasoning_effort` (Extra high and Max are sent as high). **Settings → Reasoning** sets the
  default for new chats, the request format (Automatic, Anthropic fields, OpenAI field, none),
  **Show thinking summaries**, and previews the exact JSON added to agent requests. If
  Databricks rejects a reasoning field, the request is sent once more without it and a notice
  says how to change the format. Titles, compaction and side questions send no reasoning
  fields.
- Replies show the model's thinking in a collapsible **Thinking** block ("Thinking… 12 s" while
  it thinks, "Thought for 12 s" once the answer starts). Claude Opus 5.5 returns thinking text
  only when asked, so with summaries on its requests ask for it. Request details and the usage
  table name the effort.
- Settings: the "Use … defaults" button stays as a confirmed state instead of disappearing,
  the endpoint is chosen from a list that keeps every endpoint choosable (plus **Other
  endpoint…**), and the dialog has a section list on wide screens, shorter help with **More**,
  and one type scale.
- Subagents: the agent hands broad exploration, wide searches, large reads and long command
  output to subagents on its own, so only their short reports enter the chat. It chooses
  **explore** (read-only), **editor** (file edits) or **general**, with up to 24 steps. Several
  subagents run at once (three at a time; others wait for a free slot); their approval cards
  come one by one and Stop stops them all. Each has its own card with type, live status,
  elapsed time, **Open subagent** and a collapsible **Report**. Every subagent keeps its own
  conversation file, and the parent's file lists its subagents (task, type, status, times,
  file).
- Agent tools: sections are grouped in a side navigation (**This chat**: Overview, Tasks,
  Subagents, Tools, Usage, Export; **Project**: Checkpoints, Verify, Worktrees, Extensions)
  with a new **Overview**; **Tools** has groups, a filter and per-group switches; the dialog
  reopens on the last section. **Recovery** is now **Checkpoints** and **Conversation** is
  now **Export**.
- Workspace panel: drag its left edge (or use the arrow keys on it) to resize it; the width is
  kept, Enter or a double-click restores it, and **Expand** makes it as wide as the chat allows.
  A worktree chat's panel names its project and branch. A file the agent edits again after
  **Keep** opens its review again, and an open file without unsaved edits follows the agent's
  later edits. Files a reply, a tool card or Git names are opened by their project path; a file
  named from the project's folder name, the repository root or a worktree's source project is
  found, and a missing file is reported with the folder searched.
- Sidebar: a **Tags** menu lists every tag with its number of chats and filters by any checked
  tag; `#word` in the search box filters by tag.
- Background jobs: `wait_job` can return when a job's output goes quiet (`idle_seconds`), and
  without `timeout_seconds` waits about what earlier runs of the same command say is left
  instead of 60 seconds; results suggest the next wait, and the agent is asked to size waits
  to the job.
- `scripts/simulate_databricks.py` runs the app against a simulated Databricks workspace (no
  network, no billed requests) to try effort levels, the Thinking block, subagents, jobs and
  change review; it checks reasoning fields the way the endpoints document them.

UI layout and behaviour (from a survey of the app at six window sizes, desktop to phone):

- Layout: below 1,024 px the Workspace panel opens over the chat instead of squeezing it (focus
  moves into it; Escape, **Close workspace** or a click outside closes it), and the sidebar no
  longer changes width. In a narrow chat column **Agent tools** and **Workspace** show only
  their icons, so the title stays readable (hover it for the full title and session ID). Sidebar
  rows have one **More actions** menu (**Rename**, **Pin**, **Archive**, **Edit tags…**,
  **Delete…**), titles use the whole row, the session ID shows only for repeated titles, and the
  menu opens upward near the bottom of the list. **Last model input** details open as a popover
  above the meter, and the bars above the message box no longer squeeze the conversation. The
  Agent tools dialog keeps its size and tabs in place; Settings is grouped, named and keeps
  **Save** visible; the permission menu fits the window; long `@` chips stay on one line; the
  welcome text sits above the message box.
- Behaviour: a command that exits non-zero, times out or is interrupted shows a warning icon and
  `exit N`, `timed out` or `interrupted` instead of a success tick, and expanded cards show the
  output as text with a **State** line and the **Raw result**. Error notices give a short reason
  with **Details**. A stop is marked by one divider that says what was interrupted. While the
  local server is unreachable one banner says so on every screen and the sidebar shows **Local
  server offline**; Databricks network errors read as plain sentences with **Check again**. On
  approval cards the allow-in-this-chat button and **Always allow…** form one split button. Plan
  cards scale their headings, drop the inner scroll and keep **Approve** reachable. **Local** is
  a label, not a button.
- Readability: muted text, links and the accent colour meet WCAG AA contrast (4.5:1 for text,
  3:1 for icons and controls) on every surface, also when hovered; every focusable element shows
  the same 2 px focus ring, including the message box and the Terminal fields; no text is
  smaller than 11 px (12 px for phone banners); on touch screens controls are at least 24 px;
  Markdown tables keep code cells whole and scroll sideways; the model picker's label sits next
  to its arrow; a long error no longer pushes the message box off screen.
- From a second survey: rate-limit wait notices come before the reply they delay and stop
  asking for Stop once the wait is over; in the tablet drawer the sidebar stays usable with the
  mouse as well as the keyboard; `wait_job` cards say whether the wait matched, timed out or the
  job ended; a failed command's card says it ran rather than completed; long file names and
  Terminal commands show in full on hover; in a narrower sidebar status badges become coloured
  dots so titles stay readable.

Default model, code folder, Python environment, large inputs and compaction:

- The default model is Claude Opus 5.5 (`databricks-claude-opus-5-5`) with a 200,000-token
  context budget and 16,384 Max output tokens. Saved settings still at the old default (GPT OSS
  120B at 131,072 and 8,192) move to Opus 5.5 once; Settings says so, notes that Opus 5.5 costs
  more per token and how to switch back. Budgets left at a model's defaults follow a model
  change. Settings shows each endpoint's limits and warns about budgets above what it accepts or
  above its per-minute limits; requests use the endpoint's caps. Opus 5.5 usage rates are in the
  usage panel.
- `LOCAL_AGENT_CODING_DIR` (Settings → **Default folder for new code**): where the agent creates
  new standalone scripts, tools and projects when you do not say where. Every chat may read and
  write there without a Folder access card (edits still follow the permission mode); home,
  hidden, system and app folders are refused.
- `LOCAL_AGENT_PYTHON_VENV` (Settings → **Python virtual environment**): the agent's commands,
  Verify, hooks and the Terminal run with that virtual environment first on `PATH` (and without
  the app server's own environment), the system prompt tells the agent to use it, and edits
  inside it always ask outside Bypass.
- Large inputs within the Databricks limits: requests stay under the 4 MB request limit
  (compaction also runs on size, and an oversized request is refused before it is sent);
  rate-limit responses show which limit was hit and are waited out together by every chat on the
  endpoint; a Max output tokens setting above the endpoint's output-tokens-per-minute limit is
  refused with what to set instead. New read tools `outline_file` (sections of large Markdown,
  Python and other source files with line ranges) and `project_overview` (a bounded summary of a
  large codebase); read pages grow to 128 KB on large budgets; each response's tool results
  share an allowance so one step cannot overflow the context (a read that does not fit is
  withheld with a note, other results keep their start and end). Agent requests wait up to 300
  seconds for the stream, compaction and side questions up to 600. The system prompt carries the
  date only, so Databricks can reuse its cached prefix all day.
- Compaction keeps what matters: the summary has seven fixed sections (goal and status, open
  tasks and next steps, your requirements and constraints in your words, decisions, errors,
  files, commands), large tool output is shortened before summarizing, your earlier requests are
  kept word for word in an app record next to the summary, and files whose contents left the
  context must be read again before they are overwritten. **Settings → Auto-compact at**
  (50–100% of the input budget, 90 by default) sets when automatic compaction runs. Models that
  always think get extra output room for summaries.
- **Last model input** shows the input tokens Databricks reported for the last request and their
  share of the input budget, with the components (cached input included once) and how far the
  app's estimate was off; it says when it shows an estimate instead, and follows model and
  Settings changes. A request-size line shows the body size against the 4 MB limit.
- Large files and limits, continued: `search_files` searches one named file up to 8 MB (a
  folder search still skips files over 2 MB); a compaction under a low output-tokens-per-minute
  limit is sized to fit it, and when nothing fits the error names the context budget that would;
  a message over 50,000 characters is not sent and suggests saving it as a file and attaching it
  with `@`; a stream that sends nothing for 300 seconds says so.
- Safety and speed: the configured token and secret shapes are redacted before any tool result,
  outline entry or search excerpt is shortened; hooks for `read_file`, `list_files` or
  `find_files` also run for `outline_file` and `project_overview`; long conversations save
  faster, and outlining a large Python file or starting a compaction pauses the app much less.

Roadmap milestone M4 (conversation control and history):

- Edit, retry and rewind: **Edit message** replaces a message and everything after it (and can
  undo the agent's file edits from that message on), **Retry response** sends the latest
  message again, and **Rewind to here** offers **Conversation and code**, **Conversation only**
  or **Code only**, listing each file it would restore and refusing when a file changed since.
  The earlier version is kept as an archived `(before rewind)` copy, linked from a notice above
  the message box. **Fork from here** opens a new chat up to a reply, optionally in a new Git
  worktree; **New chat in worktree** on the welcome screen does the same for a new chat. All of
  them need an idle chat.
- **Stop** keeps the reply's partial text in the model history with a marker saying you stopped
  it, so the next message knows what was cut short; a server restart marks it the same way.
- Messages while the agent works: **Queue message** keeps up to 3 messages (50,000 characters in
  total). They reach the agent at the next step boundary as part of the same turn, or as the
  next turn after the final reply; after Stop or an error they wait until **Send queued
  messages**. Queued messages are saved with the chat.
- `@path` (and `@path:10-40`) attaches a redacted snapshot of a project file, read with the
  chat's own file access; `!command` runs one shell command as a user action and attaches its
  output to your next message; `/btw <question>` asks a side question about the conversation
  without adding to it (billed, listed as **Side question** in the usage panel).
- Change review: a subagent's edits also show in its parent's changes bar, marked
  **Subagent**. Files a foreground `run_command` (Verify included) created, changed or deleted
  in a Git repository of the project, a granted folder or a folder added to Files are listed
  with a **Command** mark and can be kept or undone. **Automatic Keep** (Settings, off by
  default) keeps a completed reply's changes. **Line comments** in a file's review are sent
  together as one message.
- Sidebar: **Pin**, **Archive** and **Edit tags…** from a row's **More actions**, a tag filter,
  an amber dot for chats with unreviewed changes, and **Search conversations** that also
  searches message text (redacted excerpts, bounded at 64 MiB or 3 seconds, no index).
- Long chats open with their latest 200 messages (extended to the whole latest turn) and
  **Load earlier messages** loads more. After a clean shutdown the app starts by reading only
  each conversation's header line.

Roadmap milestone M3 (permission rules, plan hand-off, questions):

- Permission rules: **Always allow…** on an approval card saves a rule for this chat, this
  project or all projects: an exact command, commands starting with a prefix followed only by
  listed options, all file edits or edits under one folder, an MCP tool or a hook. The
  Permissions menu lists rules by scope, adds **Deny** and **Always ask** rules (**Add a
  rule…**) and removes them. Deny beats ask, and ask beats allow, in every mode (Bypass
  included); rules never override credential exclusions, folder access or Plan. Project and
  all-project rules live in `permission_rules.json` in the state folder, never in a repository.
- Commands allowed by an exact or prefix rule run without a shell, and shell syntax never
  matches such a rule. Programs that run other programs or code (`sh`, `sudo`, `env`, `xargs`,
  `python -c`, `git -c`, `rg --pre` and similar) cannot be allowed by a rule, and a command a
  deny or ask rule might cover but cannot be checked exactly gets a card.
- Auto and Plan run a fixed list of read-only command forms (`pwd`, `ls`, `git status`,
  `git diff`, `git log --oneline` and a few more) without a card, with Git hardened against a
  repository's own programs (fsmonitor, hooks, filters, external diff and text conversion,
  pager, signing, lazy fetch). No test runners or package managers are on the list.
- Modes: a new **Don't ask** mode refuses instead of asking. The mode can change while a
  response runs or a card waits; waiting cards are checked again, a running subagent follows,
  and switching to Bypass asks for confirmation. Protected files (editor, CI and agent
  configuration such as `.github/workflows`, `.vscode`, `AGENTS.md`, `CLAUDE.md`, shell startup
  files) always ask outside Bypass. **Agent tools → Tools** turns tools off for one chat.
  `delegate_task` and `stop_job` ask in Manual mode.
- **Edit before approving**: change a waiting command, a `write_file` content or a single
  `edit_file` replacement; the app checks it again and shows the new preview.
- Plan hand-off: in Plan the agent calls `submit_plan`; its card offers **Approve** (then
  Accept edits or Manual) or **Keep planning** with feedback. Questions: `ask_user` asks 1–4
  questions with 2–4 options plus **Other**, also in Plan; subagents' questions reach you
  through the parent chat. Both cards use the approval wait.
- The state folder (settings, rules, conversations) cannot be reached through any file tool.
- Markdown transcripts include answers to questions and plan reviews.

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
