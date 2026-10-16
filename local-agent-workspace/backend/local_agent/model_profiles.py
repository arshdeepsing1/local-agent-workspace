"""Per-endpoint facts and default limits for Databricks model serving endpoints.

The one table of model facts: default budgets for a new install or a model change,
the endpoint maxima requests are capped at, the Settings warnings, the thinking
allowance of small auxiliary requests (titles and /btw side questions), and how each
endpoint takes a reasoning effort (reasoning.py builds the request fields). Pure: no I/O.
"""
from .context import DEFAULT_CONTEXT_WINDOW, DEFAULT_MAX_OUTPUT_TOKENS, SAFETY_MARGIN

DEFAULT_MODEL = "databricks-claude-opus-5-5"
LEGACY_DEFAULT_MODEL = "databricks-gpt-oss-120b"  # The default before Opus 5.5; used only by the Settings migration.
LEGACY_DEFAULT_LIMITS = (131_072, 8_192)
CONCURRENT_REQUESTS = 4  # One chat plus the three subagents planning.DelegateManager allows at once.
OTPM_HEADROOM_FRACTION = 5  # Keep one fifth of the output-tokens-per-minute limit free.
MAX_REQUEST_BYTES = 4_000_000  # Databricks payload limit per request (4 MB).
MAX_EXECUTION_SECONDS = 597  # Databricks model execution limit per request.
LIMITS_SOURCE = "https://docs.databricks.com/gcp/en/machine-learning/foundation-model-apis/limits"
LIMITS_CHECKED = "2026-10-13"

# Sources, checked 2026-10-13: the Databricks Foundation Model APIs limits page (LIMITS_SOURCE)
# and the proprietary foundation model serving pricing page. Limits are the Enterprise workspace
# tier / quota Tier 1 defaults (ITPM input tokens and OTPM output tokens per minute); a
# workspace's limits may differ. Databricks reserves a request's max_tokens against OTPM when it
# starts, so a request whose max_tokens is above OTPM is rejected with HTTP 429 before it runs.
# Claude Opus 5.5 always runs adaptive thinking, and its thinking tokens count toward max_tokens.
#
# Why the recommended Opus 5.5 budgets:
# - 16,384 output tokens: four requests at once (a chat and three subagents) reserve 65,536 of
#   the 100,000 OTPM, leaving 34,464 for output already counted in the rolling minute and for
#   titles. output_concurrency_limit (20,000) keeps one fifth free; 24,576 or more leaves none.
#   16,384 doubles the old 8,192, which always-on thinking plus a large file write could reach,
#   and is at least the 16,000-token handoff (context.handoff_output_tokens(200_000)).
# - 200,000 context: an input budget of 181,568. One chat at five steps a minute and about 70%
#   fill sends about 700,000 of the 1,000,000 ITPM; at 250,000 or more four full steps use it
#   all. A full step costs about 11.4 DBU at 57.143 DBU per million input tokens, against about
#   57 at 1,000,000. Users may raise it to 1,000,000 (a note-level warning).
# GPT OSS 120B keeps 131,072 and 8,192.
#
# Reasoning (checked 2026-10-14: the Databricks "Query reasoning models" page and the Anthropic API
# reference). reasoning_format is how the endpoint takes an effort: "anthropic" (top-level
# thinking: {"type": "adaptive"} and output_config: {"effort": level}) or "openai" (top-level
# reasoning_effort). effort_levels are the levels it accepts; effort_map sends the others as one it
# accepts; default_effort is what it uses when the request names none (None: no thinking at all).
# thinking: "always" (adaptive thinking runs on every request; "disabled" and budget_tokens are
# rejected with HTTP 400) or "adaptive" (thinks only when the request sends thinking: adaptive).
# Claude Opus 5.5 omits its thinking text unless thinking.display is "summarized" (visibility only,
# not billing). Databricks documents the Anthropic fields on its Anthropic Messages route; that its
# chat-completions route (the one this app uses) passes them on is unverified, so a 400 naming one
# of them makes the app resend without them (AgentManager.model_response, reasoning.rejected_field).
#
# Prompt caching (checked 2026-10-16: the Databricks Foundation Model APIs reference): "cache_control":
# {"type": "ephemeral"} on content parts and assistant tool_calls is accepted only by Databricks-hosted
# Claude models. prompt_caching marks the endpoints that get cache
# markers on agent requests (prompt_cache.py); endpoints without a profile get none.
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODEL_PROFILES = {
    "databricks-claude-opus-5-5": {
        "label": "Claude Opus 5.5", "context_max": 1_000_000, "output_max": 128_000,
        "itpm": 1_000_000, "otpm": 100_000,
        "recommended_context_window": 200_000, "recommended_max_output_tokens": 16_384,
        "thinking_reserve": 4_096,
        "reasoning_format": "anthropic", "effort_levels": CLAUDE_EFFORTS, "default_effort": "medium",
        "thinking": "always", "effort_map": {}, "prompt_caching": True},
    "databricks-claude-opus-4-8": {
        "label": "Claude Opus 4.8", "context_max": None, "output_max": None,
        "itpm": 1_000_000, "otpm": 100_000,
        "recommended_context_window": None, "recommended_max_output_tokens": None,
        "thinking_reserve": None,
        "reasoning_format": "anthropic", "effort_levels": CLAUDE_EFFORTS, "default_effort": None,
        "thinking": "adaptive", "effort_map": {}, "prompt_caching": True},
    "databricks-gpt-oss-120b": {
        "label": "GPT OSS 120B", "context_max": 131_072, "output_max": None,
        "itpm": 1_000_000, "otpm": 100_000,
        "recommended_context_window": 131_072, "recommended_max_output_tokens": 8_192,
        "thinking_reserve": None,
        # Databricks maps minimal to low and xhigh or max to high; the app sends high itself.
        "reasoning_format": "openai", "effort_levels": ("low", "medium", "high"), "default_effort": "medium",
        "thinking": None, "effort_map": {"xhigh": "high", "max": "high"}, "prompt_caching": False},
}
# Endpoints that take reasoning like a profiled one but whose limits are not in the table.
REASONING_ALIASES = {"databricks-gpt-oss-20b": ("databricks-gpt-oss-120b", "GPT OSS 20B")}
REASONING_KEYS = ("reasoning_format", "effort_levels", "default_effort", "thinking", "effort_map")


def profile_for(model):
    """A copy of the endpoint's profile, or None for an endpoint without one."""
    if not isinstance(model, str):
        return None
    profile = MODEL_PROFILES.get(model.strip().lower())
    return dict(profile) if profile is not None else None


def reasoning_profile(model):
    """{label, reasoning_format, effort_levels, default_effort, thinking, effort_map} for an endpoint
    whose reasoning is known, or None."""
    name = model.strip().lower() if isinstance(model, str) else ""
    source, label = REASONING_ALIASES.get(name, (name, None))
    profile = MODEL_PROFILES.get(source)
    if profile is None or not profile.get("reasoning_format"):
        return None
    return {"label": label or profile["label"], **{key: profile[key] for key in REASONING_KEYS}}


def prompt_caching(model):
    """True for an endpoint whose agent requests get prompt-cache markers (a Databricks-hosted Claude profile)."""
    return bool((profile_for(model) or {}).get("prompt_caching"))


def known_endpoints():
    """[{model, label}] of the endpoints this table describes, for the Settings endpoint chooser."""
    return ([{"model": model, "label": profile["label"]} for model, profile in MODEL_PROFILES.items()]
            + [{"model": model, "label": label} for model, (_, label) in REASONING_ALIASES.items()])


def default_limits(model):
    """(context window, max output tokens) for a new install or a model change."""
    profile = profile_for(model) or {}
    return (profile.get("recommended_context_window") or DEFAULT_CONTEXT_WINDOW,
            profile.get("recommended_max_output_tokens") or DEFAULT_MAX_OUTPUT_TOKENS)


def thinking_reserve(model):
    """Output tokens a model that always thinks needs on top of a small answer."""
    profile = profile_for(model) or {}
    return profile.get("thinking_reserve") or 0


def auxiliary_output_tokens(model, wanted):
    """max_tokens for a small app request whose input is tiny (a title, a side question):
    the answer plus the thinking reserve, capped at the endpoint's output maximum."""
    profile = profile_for(model) or {}
    tokens = wanted + thinking_reserve(model)
    return min(tokens, profile["output_max"]) if profile.get("output_max") else tokens


def output_concurrency_limit(profile):
    """The largest max_tokens at which CONCURRENT_REQUESTS requests still leave a fifth of OTPM free."""
    return profile["otpm"] * (OTPM_HEADROOM_FRACTION - 1) // OTPM_HEADROOM_FRACTION // CONCURRENT_REQUESTS


def context_rate_limit(profile):
    """The context budget above which CONCURRENT_REQUESTS full steps in one minute reach ITPM."""
    return profile["itpm"] // CONCURRENT_REQUESTS


def effective_limits(model, context_window, max_output_tokens):
    """The budgets requests to this endpoint use: Settings capped at the endpoint's maxima."""
    profile = profile_for(model) or {}
    context = min(context_window, profile["context_max"]) if profile.get("context_max") else context_window
    output = min(max_output_tokens, profile["output_max"]) if profile.get("output_max") else max_output_tokens
    output = min(output, context - SAFETY_MARGIN - 1)
    return {"context_window": context, "max_output_tokens": output,
            "clamped": (context, output) != (context_window, max_output_tokens)}


def limit_warnings(model, context_window, max_output_tokens):
    """Settings warnings for these budgets on this endpoint: [{code, level, text}]."""
    profile = profile_for(model)
    if profile is None:
        return []
    label, otpm, itpm = profile["label"], profile["otpm"], profile["itpm"]
    effective = effective_limits(model, context_window, max_output_tokens)
    output, context = effective["max_output_tokens"], effective["context_window"]
    concurrency, rate = output_concurrency_limit(profile), context_rate_limit(profile)
    context_max, output_max = profile.get("context_max"), profile.get("output_max")
    warnings = []

    def add(code, level, text):
        warnings.append({"code": code, "level": level, "text": text})
    if context_max and context_window > context_max:
        add("context_over_endpoint", "warning",
            f"{label} accepts at most {context_max:,} tokens of context, so requests to it use {context_max:,} "
            f"instead of {context_window:,}.")
    if output_max and max_output_tokens > output_max:
        add("output_over_endpoint", "warning",
            f"{label} writes at most {output_max:,} output tokens per response, so requests to it use "
            f"{output_max:,} instead of {max_output_tokens:,}.")
    if output > otpm:
        add("output_over_otpm", "error",
            "Databricks rejects a request with HTTP 429 before it starts when Max output tokens is above the "
            f"endpoint's output-tokens-per-minute limit ({otpm:,} by default for {label}). Lower it to at most "
            f"{concurrency:,} unless your workspace has a higher limit.")
    elif output > concurrency:
        add("output_concurrency", "warning",
            f"Databricks reserves Max output tokens against {label}'s limit of {otpm:,} output tokens per minute "
            f"when each request starts. Above {concurrency:,}, four requests at once (this chat and up to three "
            "subagents, or several chats) can be rejected with HTTP 429 before they start; the app then waits and "
            "retries. One chat on its own can use more.")
    if context > rate:
        add("context_itpm", "note",
            f"Each agent step sends up to the whole context budget. Above {rate:,} tokens, four full steps in one "
            f"minute reach {label}'s limit of {itpm:,} input tokens per minute, so long turns and subagents wait on "
            "rate limits, and every step costs more.")
    recommended = profile.get("recommended_max_output_tokens")
    if profile.get("thinking_reserve") and recommended and output < recommended:
        add("output_below_recommended", "note",
            f"{label} always thinks before it answers, and its thinking counts toward Max output tokens. Below "
            f"{recommended:,}, complex steps and large file writes can stop at the limit.")
    return warnings


def limits_note(model):
    """Where the endpoint's limits come from, for Settings."""
    profile = profile_for(model)
    if profile is None:
        return "No built-in limits for this endpoint. Set the context budget at or below its context window."
    return (f"Limits for {profile['label']} are Databricks's documented pay-per-token defaults (Enterprise tier, "
            f"checked {LIMITS_CHECKED}); your workspace's limits may differ.")


def public_profile(model):
    """The profile for the Settings dialog, or None."""
    profile = profile_for(model)
    if profile is None:
        return None
    return {**profile, "model": model.strip().lower(), "output_concurrency_limit": output_concurrency_limit(profile),
            "context_rate_limit": context_rate_limit(profile)}
