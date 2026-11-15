# Harness feasibility study

Yes, as a second engine for chosen chats on Claude endpoints. No, as a replacement for the app's own loop. Databricks documents an Anthropic Messages route and a Claude Code setup on it, and `pip install claude-agent-sdk` needs no Node.js. The recommended way: keep the loop, close the file-format gaps now (option C), and build the second engine (option B) only after one small paid check passes.

`[verified: X]` means the claim was read on 2026-11-15 in source X (see Sources) or in the named app file. `[unverified]` marks an estimate or an assumption.

## What was asked

Can Local Agent Workspace run its chats on the Claude Agent SDK, the harness behind Claude Code, instead of or beside its own agent loop? If it can, how, and what does it cost the project's standing rules?

## What the app's own loop does today

| Capability | Module | What a harness must match, or what is lost |
|---|---|---|
| Model requests | `serving.py`, `agents.py` | The app posts chat-completions requests to `/serving-endpoints/<name>/invocations` `[verified: serving.py]`. The harness sends Anthropic Messages requests. |
| Tool limit | `agents.py` | `MAX_REQUEST_TOOLS = 32`: built-in tools first, then MCP tools, the rest left out with a notice `[verified: agents.py]`. |
| Modes, rules, cards | `permissions.py`, `rules.py`, `approvals.py` | Six modes `[verified: permissions.py]`. The harness has similar modes, but its own rules decide before it asks the host. |
| Read-only shell vetting | `shell_vet.py` | Lost. The harness uses its own read-only command list. |
| DBU spending cap | `spending.py`, `telemetry.py` | Lost unless the app prices the harness's token counts itself. |
| Rate limits, request limits | `rate_limits.py`, `request_limits.py` | Lost. The harness retries by its own rules. |
| Endpoint checks, effort fields | `endpoint_probes.py`, `reasoning.py` | Not used. The harness chooses its request fields. |
| Compaction, handoff files | `context.py` | Replaced by the harness's automatic compaction. |
| Subagents, jobs | `planning.py`, `jobs.py` | Replaced by the harness's `Agent` tool and background tasks. |
| Rewind, storage | `history.py`, `store.py` | The app keeps one JSONL file per chat in its state folder `[verified: store.py]`. The harness keeps a second transcript elsewhere. |
| Hooks | `lifecycle_hooks.py`, `extensions.py` | Ten events in the app's own JSON shape `[verified: extensions.py]`. |
| Skills, MCP, agents, commands | `extensions.py`, `mcp_client.py`, `custom_agents.py`, `commands.py` | The harness loads its own copies from `.claude/` and ignores `.agents/`. |
| Schedules, headless client | `schedules.py`, `client.py`, `stream_api.py` | They keep working only if the engine emits the app's events. |

## What the harness provides

- The Agent SDK is "a library that runs the Claude Code binary", for Python and TypeScript `[verified: S1]`.
- The Python package is `claude-agent-sdk`, version 0.2.182, for Python 3.10 or later `[verified: S12]`.
- `pip install` alone is enough on macOS and Linux. PyPI has wheels for macOS (arm64, x86_64), Linux (aarch64, x86_64) and Windows (amd64), each 150 to 165 MB `[verified: S12]`.
- The wheel bundles a native Claude Code binary, and "the spawned CLI needs no separate Node.js install" `[verified: S4]`. A source install has no binary `[verified: S3]`.
- Each session is one `claude` subprocess that talks to the host over stdio. Plan 1 GiB RAM per agent `[verified: S4]`.
- Back ends: the Anthropic API, Amazon Bedrock, Claude Platform on AWS, Google Cloud's Agent Platform (formerly Vertex AI) and Microsoft Foundry `[verified: S3, S11]`.
- `ANTHROPIC_BASE_URL` selects a gateway. The gateway must serve `/v1/messages`, stream without buffering through `message_stop`, forward `anthropic-beta` and `anthropic-version` unchanged, and pass `cache_control` through `[verified: S11]`.
- `/v1/messages/count_tokens` is optional. Without it the harness estimates context by characters `[verified: S11]`. `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` stops pre-release fields `[verified: S11]`.
- Anthropic "doesn't support routing Claude Code to non-Claude models through any gateway" `[verified: S10]`.
- The fast mode check and the WebFetch domain safety check call `api.anthropic.com` directly and ignore the base URL `[verified: S11]`.
- Permission modes: `default`, `acceptEdits`, `plan`, `dontAsk`, `bypassPermissions`, `auto` `[verified: S6]`. The order is hooks, deny rules, ask rules, mode, allow rules, then the `can_use_tool` callback. A call approved earlier never reaches the callback, but a `PreToolUse` hook runs for it `[verified: S6]`.
- Python has ten hook events, among them `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`, `SubagentStop` and `PreCompact` `[verified: S2]`.
- The Python reference documents 21 built-in tool names `[verified: S2]`. Core built-ins go in every request, and tool search defers MCP tools and other built-ins by default `[verified: S5]`. The number of tool definitions per request is not documented `[unverified]`.
- `tools=[...]` keeps only the listed built-ins. Host tools can run in the host's Python process through `create_sdk_mcp_server` `[verified: S9]`.
- Sessions are JSONL files under `~/.claude/projects/<encoded-cwd>/`, or under `CLAUDE_CONFIG_DIR`. Resume, fork and `get_session_messages()` exist `[verified: S7]`. A stability promise for the file format is not documented `[unverified]`.
- Compaction is automatic and emits a `compact_boundary` message `[verified: S5]`.
- The stream has `SystemMessage`, `AssistantMessage`, `UserMessage`, `ResultMessage`, and raw `StreamEvent` deltas with `include_partial_messages=True` `[verified: S2, S5]`.
- `ResultMessage` carries `total_cost_usd` and `model_usage`. The cost is a client-side estimate, "not authoritative billing data" `[verified: S8]`.
- The repository is MIT licensed, but use of the SDK "is governed by Anthropic's Commercial Terms of Service". A product must not use the name "Claude Code" and must not offer claude.ai login `[verified: S1, S13]`. The licence of the bundled binary was not read `[unverified]`.

## The provider question

The harness speaks Anthropic Messages. The app speaks OpenAI-style chat completions. This was the decisive constraint.

- Databricks documents an Anthropic Messages route: `https://<workspace-host>/ai-gateway/anthropic/v1/messages` `[verified: D1, updated 2026-11-10]`.
- "The Anthropic Messages API is only compatible with Anthropic pay per token foundation models and external models" `[verified: D1]`.
- Databricks documents Claude Code on that route: `ANTHROPIC_BASE_URL=https://<workspace-hostname>/ai-gateway/anthropic`, `ANTHROPIC_AUTH_TOKEN` (a Databricks token), `ANTHROPIC_MODEL` ("the full Unity Catalog name of a model service") and `ANTHROPIC_CUSTOM_HEADERS="x-databricks-use-coding-agent-mode: true"` `[verified: D2]`.
- The SDK passes variables to its subprocess through the `env` option `[verified: S2]`. Databricks does not name the Agent SDK `[verified: D2, D4]`.
- So a translating proxy is not needed. LiteLLM is the fallback: it serves `/v1/messages` for its providers and has a `databricks/` provider, but its Messages page does not name Databricks `[verified: L1, L2]`.
- The function calling page states the 32-function limit `[verified: D3]`. Its effect on the Anthropic route is not documented `[unverified]`.
- Databricks does not document `count_tokens`, accepted beta headers or rate limits for this route `[unverified]`.
- The app uses endpoint names such as `databricks-claude-opus-5-5` `[verified: scripts/simulate_databricks.py]`. The gateway page lists `system.ai.claude-opus-5-5` `[verified: D1]`. The right name for one workspace is not verified `[unverified]`.
- It is not verified that the owner's workspace has this route enabled `[unverified]`.

## Options compared

Effort figures are estimates `[unverified]`. A work session is one focused sitting. The app has about 4,500 backend and 1,280 UI tests (the owner's figures).

### A. Replace the app's loop with the harness

- What changes: an adapter replaces `agents.py` and most modules in the table above.
- Gain: one loop to maintain, with Claude Code behaviour by default.
- Cost: 20 or more sessions. Most backend tests drive the loop through the chat-completions simulator and need new fixtures.
- Risk: high. Non-Claude endpoints such as `databricks-gpt-oss-120b` stop working. The DBU cap, learned rate limits and endpoint checks have no equivalent.

### B. Offer the harness as a second engine for chosen chats

- What changes: a per-chat engine field, off by default, and one new module that turns SDK messages into the app's events. A `PreToolUse` hook and `can_use_tool` feed the app's approval cards. `model_usage` feeds the usage ledger, priced in DBU by the app. The SDK is an optional dependency.
- Gain: the owner can compare engines on real work, with no new loop code.
- Cost: 6 to 10 sessions. Existing tests stay. New backend tests need a Messages route in the simulator. UI tests gain an engine selector.
- Risk: medium. Each chat has two histories, so rewind and fork need a rule. The harness can approve a call before the app sees it, so every app check must sit in the `PreToolUse` hook.

### C. Keep the loop and adopt compatible parts

Already compatible:

- `.claude/agents/<name>.md`, with Claude Code tool names (`Read`, `Write`, `Edit`, `MultiEdit`, `Glob`, `Grep`, `LS`, `Bash`, `NotebookRead`, `NotebookEdit`, `WebFetch`, `WebSearch`), field names (`disallowedTools`, `permissionMode`, `mcpServers`, `initialPrompt`, `maxTurns`) and model values, which mean the chat's model `[verified: custom_agents.py]`.
- `.claude/skills/<id>/SKILL.md` and `~/.claude/skills`, read only, with `disable-model-invocation` and `user-invocable` `[verified: extensions.py]`.
- `.claude/commands/<name>.md` and `~/.claude/commands`, with `$ARGUMENTS` and `$1` to `$9` `[verified: commands.py]`.
- `CLAUDE.md`, `.claude/rules/*.md`, and `~/.claude/CLAUDE.md` when Settings turns it on `[verified: instructions.py]`.
- `.mcp.json`, as an import preview with every server disabled `[verified: mcp_import.py]`.

Still missing:

- Hook shapes. The app sends `event`, `tool` and `arguments`, and reads `decision`, `reason`, `arguments` and `context` `[verified: extensions.py, lifecycle_hooks.py]`. Claude Code sends `hook_event_name`, `tool_name` and `tool_input`, and reads `hookSpecificOutput.permissionDecision`, `updatedInput` and `additionalContext` `[verified: S2]`. A Claude Code hook script does not work unchanged.
- Hooks and permission rules in `.claude/settings.json`. The app never reads that file `[verified: rules.py]`.
- Command frontmatter `allowed-tools` and `model` (ignored) and `!` shell lines (never run) `[verified: commands.py]`.
- Agent file tool names `Agent`, `Task`, `TodoWrite` and `Skill` have no alias `[verified: custom_agents.py]`.
- MCP over SSE, and MCP OAuth `[verified: README.md, mcp_import.py]`.

What changes: small additive readers and aliases. Gain: a project prepared for Claude Code works with less editing, with no new dependency. Cost: one session per item, 3 to 5 in total, backend tests only. Risk: low.

| | A. Replace | B. Second engine | C. Adopt parts |
|---|---|---|---|
| Effort (estimate) | 20+ sessions | 6 to 10 sessions | 3 to 5 sessions |
| Existing tests | Most backend tests rewritten | Unchanged | Unchanged |
| Non-Claude endpoints | Lost | Kept | Kept |
| DBU cap, rate-limit learning | Lost | Own loop only | Kept |
| New dependency | 150+ MB, required | 150+ MB, optional | None |
| Needs the paid check | Yes | Yes | No |
| Risk | High | Medium | Low |

## Fit with the project's standing rules

| Rule | A | B | C |
|---|---|---|---|
| Databricks provider | Claude endpoints only, on the Anthropic route | Same, for harness chats | No change |
| No Node.js | Holds `[verified: S4]` | Holds | Holds |
| Offline tests | Simulator must speak Messages for all tests | Simulator needs one Messages route | No change |
| Public repository, no personal data | Transcripts go to the home folder unless `CLAUDE_CONFIG_DIR` points into the state folder | Same | No change |
| 32-tool limit | Host passes a `tools` list. The limit on this route is unknown | Same | No change |
| No paid calls without approval | Every chat is paid, as today | Engine is off by default | No change |

For offline tests the simulator would need `POST /ai-gateway/anthropic/v1/messages` with server-sent events through `message_delta` and `message_stop`. It may reject the `HEAD /api/hello` probe `[verified: S11]`. Tests would set `setting_sources=[]` so that a tester's own settings do not load `[verified: S4]`. It is not verified that the bundled binary starts with no network at all `[unverified]`.

## Recommendation

Do option C now. It needs no paid call and no dependency, and it closes the most visible gap, the hook shapes.

Revisit option B when the paid check passes. Only B gives a working harness chat, and it is safe because it is off by default.

Do not do option A. Three things would change that: the app drops non-Claude endpoints, Databricks documents rate limits and token counting on the Anthropic route, and the owner accepts USD estimates in place of the DBU cap.

## First step

Read Claude Code's hook output shape in `parse_hook_output` (`extensions.py`), for `before_tool` hooks only.

- Build: when the output has `hookSpecificOutput` with `hookEventName: "PreToolUse"`, map `permissionDecision` to `decision`, `permissionDecisionReason` to `reason`, `updatedInput` to `arguments` and `additionalContext` to `context`. Refuse an output that sets a field under both names, as `custom_agents.py` does for agent fields. Do not change the hook's stdin.
- Existing chats do not change. Today the app lists `hookSpecificOutput` as an ignored field `[verified: extensions.py]`.
- It worked when new backend tests show that a `deny`, an `ask` and an `updatedInput` in the Claude Code shape act like the app's own shape, and the existing hook tests pass unchanged.

## The one paid check

Status: the owner chose on 2026-11-15 to run it later on a machine with workspace access. The steps and a checklist are in [live-checks.md](live-checks.md#1-harness-check). This study sent no request to any model.

- Run: in a throwaway virtual environment, `pip install claude-agent-sdk`. Then send one `query()` with the prompt "Reply with the word ok", `max_turns=1`, `tools=["Read"]`, `setting_sources=[]`, and `env` set to the four variables from "The provider question". Use the smallest Claude model that the workspace serves.
- Pass: a `ResultMessage` with subtype `success` and non-zero `usage`, and no HTTP 400 that names a field or a beta header.
- If a field fails: run once more with `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`.
- Cost: one or two small requests. The size of the harness's system prompt is not documented. Assume up to 20,000 input tokens, which is a few US cents or less on a Haiku-class endpoint `[unverified]`.

## Open questions

- Does the 32-tool limit apply on the Anthropic route, and how many tool definitions does the harness send?
- Does the route serve `count_tokens`, and which `anthropic-beta` values does it accept?
- Which model name does `ANTHROPIC_MODEL` need for the owner's workspace?
- How many requests does one harness turn send, and does Databricks bill all of them in DBU?
- Can the bundled binary run with no route to `api.anthropic.com`?
- Which licence covers the bundled binary, and do the Commercial Terms allow an optional dependency in a public app?
- How do rewind and fork work when a harness chat has two histories?

## Sources

All read on 2026-11-15. S1 to S11 are pages under `https://code.claude.com/docs/en/`.

- S1: `agent-sdk/overview` (Agent SDK overview)
- S2: `agent-sdk/python` (Python SDK reference)
- S3: `agent-sdk/quickstart` (Quickstart)
- S4: `agent-sdk/hosting` (Hosting the Agent SDK)
- S5: `agent-sdk/agent-loop` (How the agent loop works)
- S6: `agent-sdk/permissions` (Configure permissions)
- S7: `agent-sdk/sessions` (Work with sessions)
- S8: `agent-sdk/cost-tracking` (Track cost and usage)
- S9: `agent-sdk/custom-tools` (Give Claude custom tools)
- S10: `llm-gateway` (Other LLM gateways)
- S11: `llm-gateway-protocol` (Claude Code gateway compatibility guide)
- S12: https://pypi.org/pypi/claude-agent-sdk/json (PyPI metadata, version 0.2.182)
- S13: https://github.com/anthropics/claude-agent-sdk-python (README and licence)
- D1: https://docs.databricks.com/aws/en/machine-learning/model-serving/query-anthropic-messages (Query with the Anthropic Messages API)
- D2: https://docs.databricks.com/aws/en/ai-gateway/coding-agent-claude (Connect Claude Code)
- D3: https://docs.databricks.com/aws/en/machine-learning/model-serving/function-calling (Function calling, updated 2026-11-11)
- D4: https://learn.microsoft.com/en-us/azure/databricks/ai-gateway/coding-agent-quickstart (Get started with coding agents)
- L1: https://docs.litellm.ai/docs/anthropic_unified (LiteLLM `/v1/messages`)
- L2: https://docs.litellm.ai/docs/providers/databricks (LiteLLM Databricks provider)

App files read: `README.md`, `backend/pyproject.toml`, `scripts/simulate_databricks.py`, and in `backend/local_agent/`: `serving.py`, `agents.py` (tool limit), `custom_agents.py`, `commands.py`, `extensions.py`, `lifecycle_hooks.py`, `mcp_import.py`, `instructions.py`, `rules.py`, `permissions.py`, `store.py`, `history.py`, `spending.py`, `stream_api.py`. The study describes the other modules in the first table from the README only.
