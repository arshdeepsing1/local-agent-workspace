"""The README's compaction and context-meter figures, recomputed from context.py at the default
model and budgets (Claude Opus 5.5 at 200,000 and 16,384) and at the old 131,072 and 8,192, and the lists the README
keeps by hand of what the code does."""
import re
from pathlib import Path

from local_agent import context
from local_agent.context import (
    SAFETY_MARGIN, compaction_output_allowance, compaction_threshold, condense_output_tokens, summary_byte_limit,
    summary_output_tokens,
)
from local_agent.model_profiles import DEFAULT_MODEL, default_limits

README = Path(__file__).resolve().parents[2] / "README.md"


def readme():
    return " ".join(README.read_text(encoding="utf-8").split())


def figures(context_window, max_output_tokens):
    budget = context_window - max_output_tokens - SAFETY_MARGIN
    limit = summary_byte_limit(budget)
    return {"budget": budget, "limit": limit, "summary_tokens": summary_output_tokens(limit),
            "condense_tokens": condense_output_tokens(limit), "threshold": compaction_threshold(budget, 90)}


def test_the_default_figures_in_the_readme_match_the_code():
    window, output = default_limits(DEFAULT_MODEL)
    assert (window, output) == (200_000, 16_384)
    new, old = figures(window, output), figures(131_072, 8_192)
    allowance = compaction_output_allowance(DEFAULT_MODEL, window)
    text = readme()
    # The meter example.
    share = context._percent(10_000, new["budget"])
    assert f'"Last model input · 10,000 tokens · {share}%", where the percentage is of the input budget ' \
           f'(of the default {new["budget"]:,}-token input budget)' in text
    # A plain summary request's output tokens, with the thinking allowance.
    assert (f"may use {old['summary_tokens']:,} output tokens, or a third of the summary byte limit on large budgets: "
            f"{new['summary_tokens']:,} at the default 200,000 and 16,384 on Claude Opus 5.5, plus the thinking "
            f"allowance described below, {new['summary_tokens'] + allowance:,} in all.") in text
    # The retained summary's byte limit.
    assert (f"up to 32,000 bytes on large budgets (a twelfth of the input budget): {new['limit']:,} bytes with the "
            f"default settings, {old['limit']:,} at 131,072 and 8,192") in text
    # The condensing request's output tokens.
    assert (f"({old['condense_tokens']:,} tokens for {old['limit']:,} bytes, {new['condense_tokens']:,} for the "
            f"default {new['limit']:,}; at least 4,096)") in text
    # The auto-compact thresholds already in the README.
    assert f"that is about {new['threshold']:,} tokens; at 131,072 and 8,192 it is about {old['threshold']:,}" in text
    for stale in ("4,096 output tokens with the default settings", "(12,000 with the default settings)",
                  "10,000 tokens · 8.3%"):
        assert stale not in text


def test_the_summary_floor_in_the_readme_matches_the_code():
    # prepare_context stops shortening a billed summary below half the summary minimum.
    assert f"even a summary of about {context.SUMMARY_MIN_BYTES // 2:,} bytes would not fit" in readme()


def test_the_readme_handoff_read_size_matches_the_code():
    from local_agent.agents import handoff_read_bytes
    from local_agent.context import estimate_scale
    window, output = default_limits(DEFAULT_MODEL)
    size = handoff_read_bytes(window - output - SAFETY_MARGIN, estimate_scale([], DEFAULT_MODEL))
    assert f"about {size:,} bytes at the Claude Opus 5.5 default" in readme()


def test_the_readme_window_margin_figures_match_the_code():
    # The input budget near an endpoint's known window (effective_input_budget), and its auto-compact threshold.
    from local_agent.context import effective_input_budget, model_context_window
    opus = effective_input_budget(1_000_000, 16_384, model_context_window(DEFAULT_MODEL))
    gpt = effective_input_budget(131_072, 8_192, model_context_window("databricks-gpt-oss-120b"))
    text = readme()
    assert f"which is {opus:,} on Opus 5.5 at 1,000,000 and 16,384, and {gpt:,} at GPT OSS 120B's defaults" in text
    assert f"about 108,748 ({compaction_threshold(gpt, 90):,} on GPT OSS 120B, whose window those budgets fill)" in text
    window, output = default_limits(DEFAULT_MODEL)
    assert effective_input_budget(window, output, model_context_window(DEFAULT_MODEL)) == figures(window, output)["budget"]


def test_the_readme_lists_match_the_code():
    """The Changes view's credential names (tools.SECRET_NAMES), the endings the notification hook row and the
    Notifications bullet name (notify.attention_message and the scheduler's), and sentences the features made wrong."""
    from local_agent.notify import attention_message
    from local_agent.tools import SECRET_NAMES
    text = readme()
    start = text.index("Credential files at any depth")
    assert SECRET_NAMES <= set(re.findall(r"`([^`]+)`", text[start:text.index("Credential file: diff hidden", start)]))
    row = next(line for line in README.read_text(encoding="utf-8").splitlines() if line.startswith("| `notification` |"))
    for ending in ("step limit", "spending limit", "token or time limit", "approval limit", "background subagent",
                   "scheduled run"):
        assert ending in row, ending
    start = text.index("- **Notifications**: click **Notify me**")
    bullet = text[start:text.index(" - **", start + 1)]
    for reason in ("step_limit", "spending_limit", "budget_limit", "approval_expired"):
        message = attention_message({"title": "T", "terminal_reason": reason}, "running", "idle", turn_ended=True)
        assert f'"{message.removesuffix("T")}…"' in bullet, message
    for stale in ("costs nothing today because the token estimate already bounds the size",
                  "Thinking is not replayed as model history", "Six more names joined", "Settings → MCP",
                  "Agent tools → Skills", "no view adds usage across chats", "follow Settings when left empty"):
        assert stale not in text, stale
