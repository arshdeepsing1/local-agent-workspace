"""The README's compaction and context-meter figures, recomputed from context.py at the default
model and budgets (Claude Opus 5.5 at 200,000 and 16,384) and at the old 131,072 and 8,192."""
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
