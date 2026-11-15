# Saved chats from before the upgrade

These four conversation files were written by the code of `main` at commit `e59f328` (the last version before the
upgrade). Later versions open them to show that old chats still load. They are static: the tests never regenerate
them.

| File | Chat |
|---|---|
| `parent.jsonl` | A parent chat (`pre-upgrade-parent`). One user message, a parallel batch of two `delegate_task` cards (`delegation_batch`, `child_session_id`, a finished `delegation`), the `subagents` index with both entries, and one assistant reply. |
| `child_a.jsonl`, `child_b.jsonl` | The two ended subagent chats (`pre-upgrade-child-a`, `pre-upgrade-child-b`): `is_subagent`, `parent_session_id`, `parent_event_id`, `agent_type`, the tool events `list_files`, `read_file` and `submit_report`. |
| `compacted.jsonl` | A chat with one compaction (`pre-upgrade-compacted`): `context_state` with a summary, `compactions: 1`, `through` (the cut in the wire) and one `handoff_files` entry, with events before and after the compaction. |

The file names are for humans. The conversation IDs inside are fixed strings, the workspace is `/project`, and the
times are fixed (they count up from 1,760,000,000 in the order the old run made them). Event, inference-call and
batch IDs are `id-0001`, `id-0002` and so on.

## How they were made

The fixtures were produced by running the old code, not built by hand. `generate.py` extracts
`backend/local_agent` of commit `e59f328` with `git archive` into a temporary folder, drives the old `AgentManager`
with a scripted gateway (as the old `test_parallel_subagents.py` and `test_manual_compaction.py` do), and reads
the chats the old code saved. It then replaces every random ID, time and temporary path with a fixed one, and saves
the chats again with the old `Store`, so the JSON Lines are exactly what the old `Store.save` writes.

To write them again, from this folder (it needs the repository's Git history and the app's dependencies):

```
/tmp/claude-wabisabi-venv/bin/python -I generate.py
```

Two runs differ only in the order of times that fall within a few microseconds. Write them again only if the old
version needs a new case, and commit the result.

## Use in tests

The fixture `pre_upgrade_store` in `backend/tests/conftest.py` copies these files into a fresh state folder
(`<state>/conversations/<id>.jsonl`) and returns a current `Store` with the four IDs. `test_shared_fixtures.py`
checks that they list and open, that the cards and headers still name each other, that the compaction state is
intact, and that saving a chat again loses no event and no field.
