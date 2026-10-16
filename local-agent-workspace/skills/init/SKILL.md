---
name: init
description: Use when the user asks to create or update AGENTS.md, the project's instructions for agents. Drafts it from the README, manifests and CI files.
---

# Create or update AGENTS.md

AGENTS.md is the project guidance the app loads into every conversation in this project (see the user manual: Context and project instructions). Write it for a capable newcomer who has never seen the project: the facts they need before their first change, not a tour.

## Gather facts first
1. Call `project_overview`, then read what exists among: README*, CONTRIBUTING*, the build manifests (pyproject.toml, setup.cfg, package.json, Cargo.toml, go.mod, pom.xml, build.gradle, Makefile, justfile, Taskfile.yml), the CI files (.github/workflows/*.yml, .gitlab-ci.yml) and any existing AGENTS.md, AGENTS.local.md, CLAUDE.md or .agents/rules/*.md. Batch independent reads in one response.
2. Take commands from these files exactly as they are written (for example the test job of a CI workflow). Never invent a command; if one is unclear, say so in your reply instead of guessing.
3. Do not run builds or tests just to write this file unless the user asks.

## Write it
- If AGENTS.md exists, update it with `edit_file`: keep the user's own sections and wording, fix what is wrong, and add what is missing. Otherwise create it with `write_file`.
- Keep it under 4 KB, in these sections (leave out one that has nothing true to say):
  1. **Overview**: what the project is, in two or three sentences.
  2. **Commands**: install, build, test (all and a single test), lint and format, each as a copy-paste command.
  3. **Layout**: the main folders and what lives in each.
  4. **Conventions**: code style, naming, error handling, test patterns and commit style that the code actually follows.
  5. **Gotchas**: environment requirements, generated files not to edit, slow or flaky steps.
- Facts only: no generic advice ("write clean code"), no secrets, tokens, internal hostnames or personal paths. Point to a file with `@docs/file.md` on its own line only when the agent should always read that file; imports count against the 24,000-byte instructions limit.
- AGENTS.md is a protected file: every edit to it shows an approval card, even in Accept edits.

## Finish
Reply with the path, the sections you wrote or changed, and anything you could not confirm. Mention that personal notes belong in AGENTS.local.md, which should be listed in .gitignore.
