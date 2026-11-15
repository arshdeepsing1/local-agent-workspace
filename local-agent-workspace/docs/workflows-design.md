# Workflows: status and design

This note was written on 2026-11-15, before the changes it proposes. "Status today" and every "today" in it describe the app before those changes. The proposals in "Decision 2" and "Parent and children as one unit" are now built. The README section "Agent tools: tasks, subagents, and activity" describes the current behaviour. The owner accepted both decisions on 2026-11-15 without changes.

A workflow is one batch of subagents that a parent chat starts for a task. Each subagent is a separate conversation. The parent combines their reports, and the chat shows one run card for the batch.

Workflows are enabled today. The model starts them by itself with the `delegate_task` tool. Three things are missing: a way for the user to start one by hand, one approval for a whole batch, and a limit on how many subagents one turn can start. This note makes two decisions. A subagent's transcript stays in its own conversation file. A workflow starts from the agent or from the user, and the model stays the orchestrator in both cases.

## Status today

Checked on 2026-11-15 against the code and the backend tests. Test names are in `backend/tests`. "Scratch test" means a test written for this check with the fixtures of `test_parallel_subagents.py`. It is not in the repository.

| # | Statement | Result | How it was checked |
|---|---|---|---|
| 1 | The model starts a subagent with `delegate_task`. | Confirmed. Every parent chat gets the tool in every permission mode, also in Plan. A subagent never gets it. | Scratch test on `offered_tools` |
| 2 | The model delegates by itself. | Confirmed. The system prompt says when to delegate. | `test_parallel_subagents.py::test_the_system_prompt_says_when_to_delegate` |
| 3 | Several calls in one response run as one parallel batch with one run card. | Confirmed when the calls are next to each other. Another tool call between them splits the batch. A lone call has no `delegation_batch`. | `test_parallel_subagents.py::test_the_cards_of_one_response_share_a_batch_from_their_first_broadcast_and_a_lone_one_has_none`, scratch test on `parallel_run` |
| 4 | Background subagents exist. | Confirmed. `get_subagent_result` is offered only after the chat started a background subagent. The wake message is a setting, off by default. | `test_background_subagents.py::test_get_subagent_result_lists_waits_and_times_out`, `::test_the_wake_option_continues_an_idle_chat_exactly_once` |
| 5 | Limits: 3 running over all chats, 24 steps (default 10), depth 1, a 6,000-byte result, one spending cap. | Confirmed. The spending cap is soft and off by default. No limit exists on subagents per turn: seven calls in one response all ran. | `test_planning.py::test_delegations_beyond_three_wait_for_a_free_slot`, `::test_delegate_uses_authoritative_parent_permissions_and_rejects_recursion`, `test_spending.py::test_subagents_count_for_their_parent_whose_limit_applies_to_them`, scratch test for seven calls |
| 6 | The user cannot start a subagent by hand. | Confirmed. No command, button or mention starts one. `execute_tool` is the only caller of `delegate`. | Read in `commands.js`, `Conversation.js`, `ProgressPanel.js`, `agents.py` |
| 7 | No declared workflow file format exists. | Confirmed. | Search of `backend/local_agent`, `skills`, `agents` |
| 8 | Each call asks on its own card, one at a time, before the batch starts. | Confirmed for Manual and Plan. Accept edits, Auto and Bypass show no card. Don't ask refuses the call unless a rule allows it. A chat rule for subagents skips the cards. | `test_parallel_subagents.py::test_manual_mode_asks_for_each_delegation_in_order_before_any_starts`, scratch test on `permission` |
| 9 | Each subagent is its own file with links both ways. | Confirmed. The file is `conversations/<id>.jsonl`. The parent's header keeps the newest 200 entries of `subagents`. | `test_parallel_subagents.py::test_the_index_survives_header_only_reads_and_a_deleted_child_is_marked`, scratch test for 200 |
| 10 | Delete keeps children by default, fork drops them, export adds them on request, the sidebar nests them, search hides them. | Confirmed, with one correction. The API default is `children=keep`, but the page already asks once when a chat has subagents, and its main button deletes them too. | `test_delete_children.py::test_deleting_and_keeping_children_detaches_them`, `test_portability.py::test_fork_preserves_full_history_compaction_and_source_without_children`, `test_session_search.py::test_subagents_are_left_out_unless_asked_for`, read in `Sidebar.js` |
| 11 | Scheduled runs ignore `background`. | Confirmed. The run waits for its subagents. | `test_schedules.py::test_a_scheduled_runs_subagents_run_in_the_foreground_and_its_chat_is_never_woken` |

Other facts a designer needs:

- A subagent sees nothing of the parent chat except `task` and `context`.
- A child copies the parent's mode, folder grants and chat rules when it starts. Removing a grant or a chat rule later also takes it from running subagents. Adding one does not reach them.
- A child's own tool calls ask on the child's cards. The parent's card shows a summary.
- Every save rewrites the whole conversation file. The store writes a temporary file and then replaces the old one (`store.py`).
- The limit of 3 is tied to the rate budget. `model_profiles.py` sets `CONCURRENT_REQUESTS = 4`, one chat plus three subagents, and derives each request's budgets from it.

What you can do today, with no code change:

- Ask in plain words, for example "review this in three parallel subagents".
- Put the request in a command file, `.agents/commands/<name>.md`, and run `/<name>`. A scratch test ran the example below: the text reached the model, and three scripted `delegate_task` calls ran as one batch.
- Use a skill that names `delegate_task`. The shipped `review` skill does this for broad reading.

In each case the model decides. Nothing forces it to delegate.

What you cannot do today:

- Start a subagent without the model choosing to call the tool.
- Approve a batch with one answer in Manual or Plan mode.
- Limit how many subagents one turn starts.
- Fork a chat together with its subagents, or get one transcript for both.

## Decision 1: where a subagent's transcript lives

Decision: keep one conversation file per subagent, linked both ways. The child's header names its parent. The parent's header holds the `subagents` index. No path, name or ID changes.

Reasons, each checked in the code:

- A save rewrites the whole file. With one shared file, every event of every child would rewrite the parent's history and all its siblings. Three children write at the same time, so the cost would grow with each one.
- One in-memory conversation owns one file. A shared file would need a merge of the parent's and each running child's records before every save.
- The store skips a file with one damaged record as unreadable. In a shared file, a bad record from one child would hide the parent chat too.
- The parent file stays small. Opening a chat reads its whole file.
- Each child has its own usage ledger, permission mode, rules, transcript and search entry. All of these exist and have tests.
- Every saved chat keeps working with no migration.

The agreed design input gave one more reason: a crash in one writer could damage the parent's history. The code does not support it. The store replaces the file in one step, so a crash leaves the old file intact. The damaged-record reason above replaces it.

Rejected options:

- Subagent events inside the parent's file. The costs are the first four reasons above. It also needs a migration, or two formats to read for all time.
- Renaming child files to `<slug>-subagent.jsonl`. The file name is the conversation ID. The store refuses a file whose header ID differs from its name. A rename changes the ID in the parent's cards, the index, exports, the sidebar and saved links. A slug also collides when two tasks share a title.

Effect on existing chats: none.

The owner still gets one readable file. The planned `--with-subagents` option of `scripts/conversation_transcript.py`, with a matching API option, writes one Markdown transcript: the parent, with each child's transcript under the card that started it.

## Decision 2: how a workflow starts

Decision: both the agent and the user can start a workflow. The model is the orchestrator in every case, and the design adds no model-facing tool.

Why no new tool: the model endpoint accepts at most 32 functions per request (`MAX_REQUEST_TOOLS` in `agents.py`). A usual chat already offers 28 built-in tools and holds three more slots for helpers that can join mid-turn. That leaves one slot for MCP tools. With every optional tool offered, the built-in set is exactly 32 (`test_mcp_turns.py::test_the_largest_built_in_tool_set_fits_the_request_limit`). A new tool would take the last MCP slot in a usual chat and would not fit in the largest set.

Three ways to start:

1. Automatic. Unchanged. The model calls `delegate_task` when the system prompt's rules apply.
2. Manual. A shipped skill, `skills/workflow/SKILL.md`, which the user runs as `/workflow <task>`.
3. Declared. A project's own workflow is a command file, `.agents/commands/<name>.md`, which the user runs as `/<name> <arguments>`.

The skill tells the model to:

- split the task into independent parts, within the per-turn limit, with no edits that could conflict
- start all parts in one response, as `delegate_task` calls next to each other, in the foreground
- write each task so that it stands alone: goal, constraints, paths and the report it expects
- use `agent_type` `explore` for parts that only read
- wait for all reports, then write one reply that combines them and names any part that failed
- do the work itself, and say so, when the task has only one part

The skill sets `disable-model-invocation: true`. The system message's skill list then stays as it is, and only the user selects the skill.

A worked example of a declared workflow, `.agents/commands/review-parts.md`:

```markdown
---
description: Review a change in three parallel parts and combine the reports.
argument-hint: <what to review>
---
Review this as a workflow of three subagents: $ARGUMENTS

Start the three parts below in one response, as three delegate_task calls next to each other, so
that they run in parallel. Do not review the code yourself first. A subagent sees nothing of this
chat, so put the scope, the paths and the report you expect into each task.

1. Correctness. agent_type explore, max_steps 12. Look for wrong logic, missed edge cases and
   error handling that hides failures.
2. Security. agent_type explore, max_steps 12. Look for untrusted input that reaches commands,
   paths, SQL or HTML, and for secrets in code or logs.
3. Tests. agent_type explore, max_steps 12. List the changed behaviour that no test covers, and
   the tests that the change breaks.

Ask each subagent for at most 15 findings, each with path:line, a severity (high, medium or low)
and a suggested fix.

When all three reports are back, write one reply:
- a verdict in one sentence
- the findings of all parts in one list, most severe first, without duplicates
- what no part covered

Change no file.
```

`/review-parts the uncommitted changes` puts that text in place of `$ARGUMENTS`. A file with no placeholder gets the arguments on a last line. Two more rules of `commands.py` matter to an author. `$1` to `$9` are also filled in, so write no other `$` before a digit. An `@` token in the text attaches a file.

Approval: one card approves a whole batch. It lists each subagent's task, agent type, tool profile and step limit. It offers Allow all, Decline all and Review one by one, which is today's behaviour. Modes and rules apply as today, so a rule that allows `delegate_task` skips the card. Inside each subagent nothing changes: its own tool calls still ask one by one on its own cards, and the parent's card shows that it waits.

Limits:

| Limit | Today | Planned |
|---|---|---|
| Subagents running at once, over all chats | 3, the constant `MAX_RUNNING` | Setting `subagents_running_max`, default 3, range 1 to 8 |
| Subagents started in one turn of one chat | No limit | Setting `subagents_per_turn_max`, default 6, range 1 to 24 |
| Steps per subagent | Default 10, at most 24 | Unchanged |
| Depth | 1 | Unchanged |
| Spending | One soft cap for the parent and its children | Unchanged |

A call over the per-turn limit returns a refusal that names the limit. The model continues with the subagents it has.

## Parent and children as one unit

| Operation | Today | Planned |
|---|---|---|
| Fork | Copies the parent only. Refused while a background subagent runs. | Copies ended children and rewrites their links. The refusal stays. |
| Delete | The page asks once when the chat has subagents: delete them or keep them. The API default keeps them. | The same single dialog. Deleting ended children becomes the default answer. |
| Export | Children only on request (`include_children`), up to 32 conversations and 16 MiB. | Children by default, within the same limits. |
| Transcript | One file per conversation. | One Markdown file for a parent and its children with `--with-subagents`. |
| Search and sidebar | Children nest under the parent, one level. Search leaves them out unless asked. | Unchanged. |

## What this does not include

- A scripted engine that runs agents from a file without the model choosing the split. It needs a new file format, a scheduler and a new view, and no current request needs it.
- Depth above 1. The spending cap and the sidebar assume one level.
- A live child transcript inside the parent's card. The card already shows the step, the current tool and the waiting action, and Open subagent shows the rest.

## Risks and open questions

- The model can ignore `/workflow` or a command file. The status check proved that the text reaches the model. The tests script the model's replies, so the check could not confirm what a real model does with the text.
- The status check did not run the browser tests or a live model endpoint. It confirmed the sidebar, the run card and the delete dialog by reading only.
- A higher running limit needs a matching change to `CONCURRENT_REQUESTS`. The app divides the endpoint's rate limits by that number, so a value of 9 lowers each request's output and context budget.
- Export by default can now fail where it worked before. More than 31 children or a busy child refuses the export today. The default needs a fallback, such as an export without children and a notice.
- Fork from a chosen message must copy only the children whose cards the fork keeps.
- The batch card must hold the rule that no subagent starts before every call is answered, also under Review one by one.
- The parent's index keeps 200 entries. Export already finds children by their header link, so fork and the joint transcript should do the same.

## Decision record

| Decision | Outcome | Status |
|---|---|---|
| 1. Transcript storage | One conversation file per subagent, linked both ways | Proposed 2026-11-15 |
| 2. Invocation | Agent and user, the model as orchestrator, no new model-facing tool | Proposed 2026-11-15 |
| Owner's acceptance | Accepted on 2026-11-15 without changes. | Accepted |
