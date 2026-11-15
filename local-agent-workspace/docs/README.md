# Design notes

This folder holds design notes for Local Agent Workspace. A note records what the app does today, a decision and its reasons. The user guide is the `README.md` one level up.

| Note | What it holds |
|---|---|
| [workflows-design.md](workflows-design.md) | What multi-agent workflows do today, where a subagent's transcript lives, and how a workflow starts. |
| [harness-study.md](harness-study.md) | A study of whether the app can adopt a ready-made agent harness, with the options, the cost and a first step. |
| [live-checks.md](live-checks.md) | Checks that need a real Databricks workspace: the paid harness check, the trim default measurement and the browser behind a company proxy, each with a checklist. |

## Tool slots

Databricks accepts at most 32 functions in one request (`MAX_REQUEST_TOOLS` in `backend/local_agent/agents.py`). `offered_tools` decides which tools fill them, and `feature_definitions` decides which feature tools a chat is offered. This table lists each tool with the condition that offers it. Settings, the chat's turned-off tools and deny rules can remove any tool on top of these conditions.

| Group | Tools | Offered when |
|---|---|---|
| Core (17) | `list_files`, `find_files`, `read_file`, `outline_file`, `search_files`, `write_file`, `edit_file`, `run_command`, `list_jobs`, `get_job_output`, `wait_job`, `stop_job`, `move_path`, `delete_path`, `notebook_read`, `project_overview`, `notebook_edit` | Always. Plan mode drops the kinds it always refuses (`PLAN_REFUSED_KINDS`). |
| Feature, always | `list_skills`, `use_skill`, `list_tasks`, `get_task`, `create_task`, `update_task`, `insert_activity_log`, `ask_user` | Always. |
| Feature, main chat only | `delegate_task` | Not in a subagent. |
| Feature, plan | `submit_plan` | In Plan mode, and not in a subagent. |
| Feature, setting | `remember` | Settings, Project memory is on (or the chat was offered it before), and not in a subagent. |
| Feature, setting | `web_fetch` | Settings, Web access allows it (or the chat was offered it before). |
| Feature, subagent only | `submit_report` | A subagent's delegated turn, never one the user continues. |
| Browser | `browser` | Settings, Browsing is on (or the chat was offered it before), and `get_tool_output` is not turned off in the chat. |
| Helper, joins mid-turn | `get_tool_output` | A long result of this chat was kept (a long MCP, `web_fetch` or browser result). |
| Helper, joins mid-turn | `get_subagent_result` | The chat started a background subagent, and the chat is not a subagent. |
| Helper, joins mid-turn | `report_findings` | A selected skill names it. |
| Helper, setting | `web_search` | Settings, Web access names a search provider (or the chat was offered it before). |
| MCP | `mcp__<server>__<tool>` | The server is connected. They fill the slots that are left, in server order. |

Once a chat is offered `remember`, `web_fetch`, `web_search` or `browser`, it keeps them, so the tool list stays stable. A call to a tool that Settings turned off is refused.

### Which helpers can be active together

A main chat in Manual, Accept edits or Bypass mode has 17 core tools and 11 feature tools without `submit_plan`, `submit_report` and the four helpers. That is 28. The browser makes 29. The four helpers (`get_tool_output`, `get_subagent_result`, `web_search`, `report_findings`) can all be active in the same chat, and they make 33. That is one over the limit, so one tool is cut.

A subagent has no `delegate_task`, `remember`, `get_subagent_result` or `submit_plan`. With `submit_report`, the browser and the other three helpers it has 31, which fits.

### Order of cuts

With the browser on, `fit_tools` in `backend/local_agent/browser_tool.py` fills the slots in this order and cuts from the end:

1. Core and feature tools.
2. The browser, together with `get_tool_output`. The browser reads long pages through that helper, so it is offered only when both fit. A slot stays held for `get_tool_output` until the first long page adds it.
3. The other helpers: `get_subagent_result`, then `web_search`, then `report_findings`.
4. MCP tools, in server order.

Helpers that can still join mid-turn (`joinable_helpers`) keep a slot while MCP tools are offered. A joining helper then never pushes out an MCP tool the turn already offered. With the browser off, the built-in tools come first and MCP tools take the rest.
