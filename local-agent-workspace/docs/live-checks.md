# Live checks

The offline tests cannot answer these questions. Each check needs a machine with access to a real Databricks workspace, and two of them cost money. Run them on that machine, then record the result where each check says.

| Check | Cost | Answers |
|---|---|---|
| [1. Harness check](#1-harness-check) | One or two small requests | Can the Claude Agent SDK run against the workspace's Anthropic route? |
| [2. Trim default](#2-trim-default) | Two long chats (the largest cost here) | Should "Trim old tool results from requests" stay on by default? |
| [3. Browser behind a company proxy](#3-browser-behind-a-company-proxy) | Free (no model call) | Which proxy mode does the browser detect, and do pages load? |

## Before you start

- [ ] Clone the repository and check out the branch or commit you want to check.
- [ ] From the `local-agent-workspace` folder, create a virtual environment and install the app:

  ```bash
  python3.12 -m venv .venv
  .venv/bin/python -m pip install -c constraints.txt -e './backend[test,browser]'
  .venv/bin/python -m playwright install chromium
  ```

- [ ] Put `DBRICKS_URL` and `DBRICKS_TOKEN` in the app's credential file (`.env` in the app folder, or the file that `LOCAL_AGENT_ENV_FILE` names). Never paste the token into a chat, a log or a commit.
- [ ] Optional: set **Settings → Agent → Spending limit per conversation** so a mistake cannot run up a large bill.
- [ ] Run the offline suites once so you know the machine is healthy:

  ```bash
  cd backend && ../.venv/bin/python -m pytest -q -p no:cacheprovider --ignore=tests/review
  ```

- [ ] Note the date, the commit (`git rev-parse --short HEAD`) and the endpoint names you use. Each result below needs them.

## 1. Harness check

`docs/harness-study.md` recommends a second, optional chat engine built on the Claude Agent SDK, but only after one small paid request shows that the SDK works against the workspace. This check is that request. It does not touch the app.

**Cost:** one or two small requests. The size of the harness's system prompt is not documented, so assume up to 20,000 input tokens per request. Use the smallest Claude model the workspace serves.

### Steps

- [ ] Check that the workspace serves the Anthropic Messages route. In the Databricks UI, open **Serving** and confirm that a Claude pay-per-token model or an external Claude model exists. Note its full Unity Catalog name (for example `system.ai.<model>`).
- [ ] Create a throwaway virtual environment outside the app folder and install the SDK:

  ```bash
  python3.12 -m venv /tmp/harness-check
  /tmp/harness-check/bin/python -m pip install claude-agent-sdk
  /tmp/harness-check/bin/python -m pip show claude-agent-sdk   # note the version
  ```

- [ ] Save this script as `/tmp/harness-check/check.py`. It reads the token from your environment, so the token never sits in the file.

  ```python
  import asyncio
  import os

  from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

  HOST = os.environ["DBRICKS_HOST"]        # workspace host name, no https://
  MODEL = os.environ["HARNESS_MODEL"]      # full Unity Catalog name of the model

  options = ClaudeAgentOptions(
      max_turns=1,
      tools=["Read"],
      setting_sources=[],
      env={
          "ANTHROPIC_BASE_URL": f"https://{HOST}/ai-gateway/anthropic",
          "ANTHROPIC_AUTH_TOKEN": os.environ["DBRICKS_TOKEN"],
          "ANTHROPIC_MODEL": MODEL,
          "ANTHROPIC_CUSTOM_HEADERS": "x-databricks-use-coding-agent-mode: true",
      },
  )


  async def main():
      async for message in query(prompt="Reply with the word ok", options=options):
          if isinstance(message, ResultMessage):
              print("subtype:", message.subtype)
              print("is_error:", message.is_error)
              print("stop_reason:", message.stop_reason)
              print("result:", message.result)
              print("usage:", message.usage)


  asyncio.run(main())
  ```

- [ ] Run it. Export the token in the same shell only, then clear it:

  ```bash
  export DBRICKS_HOST=<workspace-host> HARNESS_MODEL=<full model name>
  read -rs DBRICKS_TOKEN && export DBRICKS_TOKEN   # paste the token, it is not echoed
  /tmp/harness-check/bin/python /tmp/harness-check/check.py
  unset DBRICKS_TOKEN
  ```

- [ ] If an HTTP 400 names a field or a beta header, run it once more with `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` added to the `env` dictionary.
- [ ] Behind a company proxy that inspects TLS, the SDK's bundled program may not trust the company's root certificate. If you see a certificate error, export the company root as a PEM file and add `"NODE_EXTRA_CA_CERTS": "<path to the PEM>"` to `env`. If the proxy is not picked up, also add `"HTTPS_PROXY": "http://<proxy host>:<port>"`.
- [ ] Delete `/tmp/harness-check` when you are done.

### Pass or fail

- **Pass:** `subtype: success`, `is_error: False`, and `usage` shows non-zero `input_tokens` and `output_tokens`.
- **Fail:** any `error_*` subtype, or an HTTP error. Copy the error text without the token.

### Record

- [ ] In `docs/harness-study.md`, section "The one paid check": replace "Status: waiting for the owner's approval" with the date, SDK version, model name, pass or fail, the usage numbers and any error text.
- [ ] Answer what you can of "Open questions" in the same file (for example the model name the route needs, and whether the beta switch was needed).
- [ ] In `PROGRESS.md`, add the result to the harness item, then run `.venv/bin/python scripts/check_progress.py --write`.

## 2. Trim default

**Settings → Model and context → Trim old tool results from requests** is on by default (`context_trim` in `settings.json`). Trimming lets a long chat run longer before it compacts, but it changes text inside the cached part of the request, so the cache writes more. In the offline scripted chat the first compaction moved from step 12 to step 17 and cache writes rose to 1.52 times. This check measures the same trade on the real endpoint, so you can decide the default.

**Cost:** two long chats on the same task. Use a task that reads many files, for example a review of a mid-sized repository. Expect the cost of two normal long sessions.

### Steps

- [ ] Pick one real repository and one task that reads many large files over many steps (for example "Read every module under `src/` and write a summary of each into NOTES.md"). Write the exact prompt down. Both chats use it word for word.
- [ ] Start the app: `.venv/bin/python run.py`, then open http://127.0.0.1:8765.
- [ ] In **Settings → Model and context**, note the input budget, the auto-compact percentage and the trim marks (start 70%, target 55% by default). Keep them the same for both chats.
- [ ] Chat A, trim off:
  - [ ] Turn **Trim old tool results from requests** off and save.
  - [ ] Start a new chat on the repository and send the prompt. Answer approvals the same way you will in chat B, or use a permission mode that needs none.
  - [ ] Let it finish. Note the step at which the first compaction happened (the chat shows a compaction notice).
  - [ ] In **Agent tools → Usage**, click **Export CSV** and save it as `trim-off.csv`.
- [ ] Chat B, trim on:
  - [ ] Turn the setting on and save.
  - [ ] Start a second new chat on a fresh copy of the same repository (undo chat A's changes first) and send the same prompt.
  - [ ] Note the step of the first compaction and the trim notices ("Shortened N old tool results ...").
  - [ ] Export the Usage CSV as `trim-on.csv`.
- [ ] Compare the two exports. Sum, for each chat, the uncached input, cache reads, cache writes, output tokens and estimated DBU. The same rows are also available offline:

  ```bash
  .venv/bin/python scripts/usage_report.py <state dir>/conversations/<chat id>.jsonl --csv trim-on.csv
  ```

  Copy the conversation file first if the app is running.
- [ ] Compare the quality of both results. Did either chat lose track of the task after a compaction or a trim?

### Decide

| What you see | Suggested default |
|---|---|
| Trim on compacts later, the DBU difference is small, and the quality is the same or better | Keep it on |
| Trim on costs clearly more DBU and compacts only a little later | Turn it off by default |
| Trim on loses facts the task needed | Turn it off and report the case |

To change the default for everyone, edit `"context_trim": True` in `backend/local_agent/config.py`, update the README paragraph "Trimming old tool results" and the tests that assert the default, and add a CHANGELOG line. To change it only on one machine, use the Settings switch.

### Record

- [ ] In the README paragraph "Why trimming costs cache", add the measured numbers on the real endpoint next to the offline ones: first compaction step for each chat, cache writes ratio, DBU for each chat, and the date. Do not paste raw logs, repository names or file paths from the real task.
- [ ] Record your decision in `CHANGELOG.md` if the default changes.

## 3. Browser behind a company proxy

The browser tool sends every page request through the app's local filtering proxy. On a laptop with a company VPN and a proxy client such as Zscaler, the proxy either connects directly (tunnel mode) or passes traffic to the company's local proxy (local proxy mode). This check confirms which mode the app found and that pages load. It makes no model call.

### Steps

- [ ] Start the app and open **Settings → Browsing**. Turn on **Let the agent use a browser** and save.
- [ ] Read the status line under **Proxy for the browser** (`auto` by default). Note what it says:
  - "direct": no system proxy is set. This is normal for tunnel mode.
  - "system proxy host:port": the app found the company's local proxy (Zscaler often uses `127.0.0.1:9000`).
  - "PAC file found": the laptop uses a PAC file. Ask IT for the proxy address, or find it in the PAC file, and enter `http://host:port` in the field.
- [ ] Start a chat in Manual mode and ask: "Open https://example.com with the browser and tell me the page title." Approve the host when the card asks.
  - [ ] Pass: the reply names the page title.
  - [ ] If a certificate error appears, the company root certificate is not trusted by Chromium. Check that it is in the system keychain.
- [ ] Ask the chat to open a company intranet address you know (for example an internal wiki host). The app must refuse it with "blocked by the address check". Do not approve any change to the address rules for this.
- [ ] Ask the chat to open `http://127.0.0.1:<port>` for a port you did not allow. The app must refuse it too.
- [ ] Turn **Let the agent use a browser** off again if you do not need it.

### Record

- [ ] Note the detected mode and the results in your handoff log or notes. Do not put company host names into the public repository.

## Checklist summary

- [ ] Before you start: install, credential file, offline suite green, commit noted
- [ ] 1. Harness check run, result recorded in `docs/harness-study.md` and `PROGRESS.md`
- [ ] 2. Trim default measured, numbers in the README, decision made
- [ ] 3. Browser proxy mode noted, public page loads, intranet and loopback refused
