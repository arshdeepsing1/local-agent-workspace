import ast
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from .context import (
    MAX_CONTEXT_WINDOW, MAX_MAX_OUTPUT_TOKENS, MIN_CONTEXT_WINDOW, MIN_MAX_OUTPUT_TOKENS, SAFETY_MARGIN,
)
from .context import DEFAULT_AUTO_COMPACT_PERCENT, MAX_AUTO_COMPACT_PERCENT, MIN_AUTO_COMPACT_PERCENT
from .model_profiles import DEFAULT_MODEL, LEGACY_DEFAULT_LIMITS, LEGACY_DEFAULT_MODEL, default_limits, known_endpoints
from .output_styles import STYLE_ID
from .reasoning import DEFAULT_EFFORT, DEFAULT_FORMAT, EFFORT_LEVELS, REQUEST_FORMATS
from .spending import validate_limit

APP_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MAX_AGENT_STEPS = 32
MIN_MAX_AGENT_STEPS = 1
MAX_MAX_AGENT_STEPS = 64
# Minutes an approval card waits for an answer; 0 waits until it is answered or stopped.
# A forgotten card keeps its chat busy, which blocks Undo and Recovery in that workspace,
# so the default is a long but finite wait.
DEFAULT_APPROVAL_MINUTES = 60
MAX_APPROVAL_MINUTES = 1440
MAX_USER_INSTRUCTIONS = 8_000
MIN_WRITE_LIMIT_KB = 16
MAX_WRITE_LIMIT_KB = 1000
MAX_COMMAND_ENV = 32
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
# Never passed to commands; equal to jobs.CREDENTIAL_ENV, DENIED_ENV and DENIED_ENV_PREFIXES.
CREDENTIAL_ENV = {"DBRICKS_TOKEN", "DATABRICKS_TOKEN", "DBRICKS_URL", "DATABRICKS_HOST", "LOCAL_AGENT_ENV_FILE"}
DENIED_ENV = {"BASH_ENV", "ENV", "ZDOTDIR", "PROMPT_COMMAND", "SHELLOPTS", "BASHOPTS", "IFS", "PYTHONSTARTUP",
              "PERL5OPT", "RUBYOPT", "NODE_OPTIONS"}
DENIED_ENV_PREFIXES = ("LD_", "DYLD_")
# Settings that .env or the process environment can set; settings.json holds only values chosen in Settings.
ENV_KEYS = {"coding_dir": "LOCAL_AGENT_CODING_DIR", "python_venv": "LOCAL_AGENT_PYTHON_VENV"}
LIMIT_KEYS = ("context_window", "max_output_tokens")
# Settings → Prompt caching: Automatic (Claude endpoints), Documented forms only (prompt_cache level "messages") or Off.
PROMPT_CACHING = ("auto", "messages", "off")
# settings.json files without this version were written before the default model changed (see Settings.__init__).
SETTINGS_VERSION = 2
# Settings → Subagents: each built-in subagent type's endpoint and reasoning effort; "" is the chat's own.
# Explore starts at Fast (low effort): its work is reading and searching. Custom agents set theirs in
# their files (custom_agents.py).
SUBAGENT_TYPES = ("explore", "general", "editor")
DEFAULT_SUBAGENT_DEFAULTS = {"explore": {"model": "", "effort": "low"}, "general": {"model": "", "effort": ""},
                             "editor": {"model": "", "effort": ""}}
# A Databricks serving endpoint name (letters, digits, '.', '_' and '-'), as subagent model overrides take it.
ENDPOINT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def subagent_defaults(value, strict=True):
    """Settings → Subagents as {type: {model, effort}} for every built-in type. strict: a bad value is a
    ValueError (a save); otherwise it is the type's default (a settings.json written by hand)."""
    if value is None:
        value = {}
    if not isinstance(value, dict) or (strict and set(value) - set(SUBAGENT_TYPES)):
        if strict:
            raise ValueError("Subagent defaults name only explore, general and editor.")
        value = {}
    result = {}
    for kind in SUBAGENT_TYPES:
        entry = value.get(kind, DEFAULT_SUBAGENT_DEFAULTS[kind])
        default = DEFAULT_SUBAGENT_DEFAULTS[kind]
        if not isinstance(entry, dict) or set(entry) - {"model", "effort"}:
            if strict:
                raise ValueError(f"The {kind} subagent default takes only model and effort.")
            entry = default
        model = entry.get("model", default["model"])
        effort = entry.get("effort", default["effort"])
        model = model.strip() if isinstance(model, str) else model
        if not isinstance(model, str) or (model and not ENDPOINT_NAME.match(model)):
            if strict:
                raise ValueError(f"The {kind} subagent model must be empty (the chat's model) or an endpoint name of "
                                 "letters, digits, '.', '_' and '-' (at most 128 characters).")
            model = default["model"]
        if effort != "" and effort not in EFFORT_LEVELS:
            if strict:
                raise ValueError(f"The {kind} subagent effort must be empty (the chat's effort) or one of: "
                                 + ", ".join(EFFORT_LEVELS) + ".")
            effort = default["effort"]
        result[kind] = {"model": model, "effort": effort}
    return result


def read_env(path: Path) -> dict[str, str]:
    """Read assignments as data; never source or execute a credential file."""
    result = {}
    if not path.is_file():
        return result
    for line in path.read_text().splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*[:=]\s*(.*?)\s*$", line)
        if not match:
            continue
        key, value = match.groups()
        if value[:1] in ("'", '"'):
            try:
                value = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                continue
        else:
            value = value.split(" #", 1)[0].strip()
        if isinstance(value, str):
            result[key] = value
    return result


def follow_limits(follow, defaults, current):
    """(context window, max output tokens) after the budgets marked in follow take a model's defaults.
    A followed value that would leave the output at or above the context budget minus the safety
    margin is not followed, so a model change never makes the kept budgets invalid."""
    context_window, output = current
    if follow[1] and type(defaults[0] if follow[0] else context_window) is int:
        if defaults[1] < (defaults[0] if follow[0] else context_window) - SAFETY_MARGIN:
            output = defaults[1]
    if follow[0] and (type(output) is not int or output < defaults[0] - SAFETY_MARGIN):
        context_window = defaults[0]
    return context_window, output


def credential_assignments(values):
    return {key: values[key] if key in values else values[alias]
            for key, alias in (("DBRICKS_URL", "DATABRICKS_HOST"), ("DBRICKS_TOKEN", "DATABRICKS_TOKEN"))
            if key in values or alias in values}


class Settings:
    def __init__(self, state_dir: Path | None = None):
        self.env = {}
        for source in (read_env(APP_ROOT / ".env"), os.environ):
            self.env.update(source)
            self.env.update(credential_assignments(source))
        self.state_dir = state_dir or Path(self.env.get("LOCAL_AGENT_STATE_DIR", str(APP_ROOT / ".local")))
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.state_dir / "settings.json"
        self.values = {
            "workspace": self.env.get("LOCAL_AGENT_WORKSPACE", str(APP_ROOT.parent)),
            # An empty LOCAL_AGENT_MODEL= line means the default, as the migration below treats it.
            "model": self.env.get("LOCAL_AGENT_MODEL", "").strip() or DEFAULT_MODEL,
            "env_file": self.env.get("LOCAL_AGENT_ENV_FILE", ""),
            **{key: self.env.get(name, "").strip() for key, name in ENV_KEYS.items()},
            "max_agent_steps": DEFAULT_MAX_AGENT_STEPS,
            "compaction_handoffs": True,
            "auto_compact_percent": DEFAULT_AUTO_COMPACT_PERCENT,
            "approval_timeout_minutes": DEFAULT_APPROVAL_MINUTES,
            "system_notifications": False,
            "user_instructions": "",
            # Settings → Agent (instructions.py, memory.py, output_styles.py): the user's own instruction files
            # (~/.agents/AGENTS.md on, the read-only ~/.claude/CLAUDE.md off), project memory and the reply style.
            "user_agents_file": True,
            "user_claude_file": False,
            "project_memory": True,
            "output_style": "default",
            "write_limit_kb": 80,
            "command_env": [],
            # Settings → Commands (secret_paths.py): file name patterns that count as credential files for commands.
            "secret_file_patterns": [],
            "rate_limit_auto_continue": True,
            "auto_keep_changes": False,
            # Settings → Reasoning (reasoning.py): the effort new chats start with, how requests ask
            # for it, and whether adaptive Claude endpoints are asked to return thinking summaries.
            "reasoning_effort": DEFAULT_EFFORT,
            "reasoning_format": DEFAULT_FORMAT,
            "thinking_summaries": True,
            # Settings → Reasoning (reasoning_replay.py): send a turn's signed thinking back with its tool results
            # (experimental; off until an endpoint check shows the route accepts it).
            "thinking_replay": False,
            # Settings → Model and context (prompt_cache.py): "auto" marks the stable start of agent requests to
            # Databricks-hosted Claude endpoints for prompt caching; "messages" sends only the documented marker
            # forms (no system message or tool result marker); "off" sends no cache markers.
            "prompt_caching": "auto",
            # Settings → Agent (spending.py): the soft cap in estimated DBU of chats without their own; 0 = none.
            "spending_limit_dbu": 0,
            "subagent_defaults": subagent_defaults(None),
            # Settings → Model and context (AgentManager.fallback_endpoint): the endpoint an agent or subagent
            # request goes to once when its own failed before the reply started; "" = none.
            "fallback_model": "",
            # Settings → Commands and files (commands.py): /<name> at the start of a message runs a command template
            # and /<skill-id> selects a skill for that message; off sends such text as typed.
            "slash_commands": True,
            # Settings → Web access (web_fetch.py): the agent's web_fetch tool (a card for each new domain), and
            # web_search through a provider: "" (none), "brave", "tavily" or "searxng" (at web_search_url). The API
            # key stays in the credential file, in the entry web_search_key names ("" = the provider's usual name).
            "web_fetch": True,
            "web_search_provider": "",
            "web_search_url": "",
            "web_search_key": "",
        }
        self.migrated_from = None
        if self.path.exists():
            saved = json.loads(self.path.read_text())
            keys = set(self.values) | set(LIMIT_KEYS)
            # A file from before the default model changed that still holds the old default model and
            # budgets moves to the new default once; the next save writes SETTINGS_VERSION, so a user
            # who then picks the old model keeps it. LOCAL_AGENT_MODEL may be unset, the old default or
            # the new default (the current .env.example value).
            if ("settings_version" not in saved and saved.get("model") == LEGACY_DEFAULT_MODEL
                    and (saved.get("context_window", LEGACY_DEFAULT_LIMITS[0]),
                         saved.get("max_output_tokens", LEGACY_DEFAULT_LIMITS[1])) == LEGACY_DEFAULT_LIMITS
                    and self.env.get("LOCAL_AGENT_MODEL", "").strip() in ("", LEGACY_DEFAULT_MODEL, DEFAULT_MODEL)):
                saved = {key: value for key, value in saved.items() if key not in LIMIT_KEYS}
                saved["model"] = DEFAULT_MODEL
                self.migrated_from = LEGACY_DEFAULT_MODEL
            self.values.update({key: value for key, value in saved.items() if key in keys})
            self.values["subagent_defaults"] = subagent_defaults(self.values["subagent_defaults"], strict=False)
            # A hand-edited limit: a number written as text still counts; anything else is shown and used
            # as no limit (0) instead of a value Settings would display but the agent would ignore.
            limit = self.values["spending_limit_dbu"]
            try:
                self.values["spending_limit_dbu"] = validate_limit(float(limit) if isinstance(limit, str) else limit)
            except ValueError:
                self.values["spending_limit_dbu"] = 0
            fallback = self.values["fallback_model"]  # A hand-edited name that is not one means none.
            fallback = fallback.strip() if isinstance(fallback, str) else ""
            self.values["fallback_model"] = fallback if ENDPOINT_NAME.match(fallback) else ""
        # Budgets not saved follow the model's defaults; saved ones win.
        limits = dict(zip(LIMIT_KEYS, default_limits(self.values["model"])))
        ordered = {}
        for key, value in self.values.items():
            if key not in LIMIT_KEYS:
                ordered[key] = value
            if key == "env_file":
                ordered.update({name: self.values.get(name, limits[name]) for name in LIMIT_KEYS})
        self.values = ordered

    def credentials(self) -> tuple[str, str]:
        external = read_env(Path(self.values["env_file"]).expanduser()) if self.values["env_file"] else {}
        env = {**credential_assignments(external), **credential_assignments(self.env)}
        url = env.get("DBRICKS_URL", "").rstrip("/")
        token = env.get("DBRICKS_TOKEN", "")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("Set a valid HTTPS Databricks workspace URL in your credential file.")
        if not token:
            raise ValueError("Databricks token is missing from your credential file.")
        return url, token

    def public(self):
        from . import user_paths  # rules.py imports this module; user_paths imports tools.
        try:
            host, _ = self.credentials()
            configured = True
        except (ValueError, OSError):
            host, configured = "", False
        return {**self.values, "host": host, "configured": configured, "web_search_status": self.web_search_status(),
                "coding_dir_status": user_paths.coding_dir_status(self),
                "python_venv_status": user_paths.python_venv_status(self), "default_model": DEFAULT_MODEL,
                "default_limits": dict(zip(LIMIT_KEYS, default_limits(self.values["model"]))),
                "endpoint_profiles": known_endpoints(),
                **({"model_migrated_from": self.migrated_from} if self.migrated_from is not None else {})}

    def credential_values(self, path=None):
        """The credential file's assignments (read as data), or {} without one. path: another file (a save's)."""
        path = self.values.get("env_file") if path is None else path
        if not path or not isinstance(path, str):
            return {}
        try:
            return read_env(Path(path).expanduser())
        except (OSError, UnicodeError, ValueError):
            return {}

    def web_search_status(self):
        """Settings → Web access: {provider, ready, problem?, key_name?} for the chosen search provider (never the
        key), or None when web search is off."""
        from .web_fetch import search_setup
        setup = search_setup(self.values, self.credential_values)
        if setup is None:
            return None
        return {"provider": setup["label"], "ready": not setup.get("problem"),
                **{key: setup[key] for key in ("problem", "key_name") if setup.get(key)}}

    def env_equivalent(self, key, value, candidate=None):
        """True when value is what .env or the process environment sets for key (coding_dir or
        python_venv), as typed or as its checked form. Such a value is not saved to settings.json,
        so a later change in .env still applies."""
        from . import user_paths
        raw = self.env.get(ENV_KEYS[key], "")
        raw = raw.strip() if isinstance(raw, str) else ""
        if value == raw:
            return True
        if not raw or not value:
            return False
        check = (user_paths.check_coding_dir_setting if key == "coding_dir" else user_paths.check_python_venv_setting)
        try:
            return check(raw, self, candidate) == value
        except (ValueError, OSError):
            return False

    def update(self, values: dict):
        from . import user_paths  # rules.py imports this module; user_paths imports tools.
        candidate = {**self.values, **{k: v for k, v in values.items() if k in self.values}}
        model = candidate["model"]
        if isinstance(model, str) and model.strip() != self.values["model"]:
            # Budgets still at the old model's defaults follow the new model's, unless this save changes them.
            old, new = default_limits(self.values["model"]), default_limits(model)
            follow = [values.get(key, self.values[key]) == self.values[key] and self.values[key] == old[index]
                      for index, key in enumerate(LIMIT_KEYS)]
            followed = follow_limits(follow, new, (candidate["context_window"], candidate["max_output_tokens"]))
            candidate.update(zip(LIMIT_KEYS, followed))
        workspace = Path(candidate["workspace"]).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError("Choose an existing project directory.")
        if not candidate["model"].strip():
            raise ValueError("Enter a model or model-service ID.")
        budget = candidate["context_window"]
        if type(budget) is not int or not MIN_CONTEXT_WINDOW <= budget <= MAX_CONTEXT_WINDOW:
            raise ValueError(f"Context budget must be an integer from {MIN_CONTEXT_WINDOW:,} to {MAX_CONTEXT_WINDOW:,} tokens.")
        output_tokens = candidate["max_output_tokens"]
        if (type(output_tokens) is not int
                or not MIN_MAX_OUTPUT_TOKENS <= output_tokens <= MAX_MAX_OUTPUT_TOKENS):
            raise ValueError(f"Output-token limit must be an integer from {MIN_MAX_OUTPUT_TOKENS:,} to {MAX_MAX_OUTPUT_TOKENS:,}.")
        if output_tokens >= budget - SAFETY_MARGIN:
            raise ValueError(f"Output-token limit must be below the context budget minus the {SAFETY_MARGIN:,}-token safety margin.")
        max_steps = candidate["max_agent_steps"]
        if type(max_steps) is not int or not MIN_MAX_AGENT_STEPS <= max_steps <= MAX_MAX_AGENT_STEPS:
            raise ValueError(f"Agent step limit must be an integer from {MIN_MAX_AGENT_STEPS} to {MAX_MAX_AGENT_STEPS}.")
        if type(candidate["compaction_handoffs"]) is not bool:
            raise ValueError("Compaction handoffs must be on or off.")
        percent = candidate["auto_compact_percent"]
        if type(percent) is not int or not MIN_AUTO_COMPACT_PERCENT <= percent <= MAX_AUTO_COMPACT_PERCENT:
            raise ValueError(f"Auto-compact threshold must be an integer from {MIN_AUTO_COMPACT_PERCENT} to "
                             f"{MAX_AUTO_COMPACT_PERCENT} percent.")
        minutes = candidate["approval_timeout_minutes"]
        if type(minutes) is not int or not 0 <= minutes <= MAX_APPROVAL_MINUTES:
            raise ValueError(f"Approval wait must be 0 (no limit) or 1 to {MAX_APPROVAL_MINUTES:,} minutes.")
        if type(candidate["system_notifications"]) is not bool:
            raise ValueError("Desktop notifications must be on or off.")
        instructions = candidate["user_instructions"]
        if not isinstance(instructions, str) or len(instructions) > MAX_USER_INSTRUCTIONS or "\x00" in instructions:
            raise ValueError(f"Custom instructions must be text of at most {MAX_USER_INSTRUCTIONS:,} characters.")
        for key, label in (("user_agents_file", "Load ~/.agents/AGENTS.md"), ("user_claude_file", "Load ~/.claude/CLAUDE.md"),
                           ("project_memory", "Project memory")):
            if type(candidate[key]) is not bool:
                raise ValueError(f"{label} must be on or off.")
        if not isinstance(candidate["output_style"], str) or not STYLE_ID.match(candidate["output_style"]):
            raise ValueError("Output style must be default, concise, explanatory or the name of a style file "
                             "(1-40 lowercase letters, digits, - or _).")
        write_limit = candidate["write_limit_kb"]
        if type(write_limit) is not int or not MIN_WRITE_LIMIT_KB <= write_limit <= MAX_WRITE_LIMIT_KB:
            raise ValueError(f"File write limit must be an integer from {MIN_WRITE_LIMIT_KB} to {MAX_WRITE_LIMIT_KB:,} KB.")
        names = candidate["command_env"]
        if (not isinstance(names, list) or len(names) > MAX_COMMAND_ENV or len(set(map(str, names))) != len(names)
                or any(not isinstance(name, str) or not ENV_NAME.match(name) for name in names)):
            raise ValueError(f"List at most {MAX_COMMAND_ENV} environment variable names (letters, digits and _, not starting with a digit).")
        for name in names:
            if name in CREDENTIAL_ENV:
                raise ValueError(f"{name} cannot be passed to commands.")
            if name in DENIED_ENV or name.startswith(DENIED_ENV_PREFIXES):
                raise ValueError(f"{name} changes how programs start and cannot be passed to commands.")
        from .secret_paths import check_patterns  # tools imports jobs; keep this module's imports light.
        candidate["secret_file_patterns"] = check_patterns(candidate["secret_file_patterns"])
        if type(candidate["rate_limit_auto_continue"]) is not bool:
            raise ValueError("Continue after rate limits must be on or off.")
        if type(candidate["auto_keep_changes"]) is not bool:
            raise ValueError("Automatic Keep must be on or off.")
        if candidate["reasoning_effort"] not in EFFORT_LEVELS:
            raise ValueError("Default reasoning effort must be one of: " + ", ".join(EFFORT_LEVELS) + ".")
        if candidate["reasoning_format"] not in REQUEST_FORMATS:
            raise ValueError("Reasoning request format must be one of: " + ", ".join(REQUEST_FORMATS) + ".")
        if type(candidate["thinking_summaries"]) is not bool:
            raise ValueError("Show thinking summaries must be on or off.")
        if type(candidate["thinking_replay"]) is not bool:
            raise ValueError("Send earlier thinking back must be on or off.")
        if candidate["prompt_caching"] not in PROMPT_CACHING:
            raise ValueError("Prompt caching must be one of: " + ", ".join(PROMPT_CACHING) + ".")
        if type(candidate["slash_commands"]) is not bool:
            raise ValueError("Slash commands must be on or off.")
        self.check_web_settings(candidate)
        candidate["spending_limit_dbu"] = validate_limit(candidate["spending_limit_dbu"], "Default spending limit")
        sent = values.get("subagent_defaults")
        if isinstance(sent, dict):
            # Types and fields left out keep their current values.
            current = self.values["subagent_defaults"]
            sent = {kind: {**current.get(kind, {}), **entry} if isinstance(entry, dict) and kind in current else entry
                    for kind, entry in sent.items()}
            candidate["subagent_defaults"] = {**current, **sent}
        candidate["subagent_defaults"] = subagent_defaults(candidate["subagent_defaults"])
        fallback = candidate["fallback_model"]
        fallback = fallback.strip() if isinstance(fallback, str) else fallback
        if not isinstance(fallback, str) or (fallback and not ENDPOINT_NAME.match(fallback)):
            raise ValueError("The fallback endpoint must be empty (none) or an endpoint name of letters, digits, '.', "
                             "'_' and '-' (at most 128 characters).")
        # Equal to the model (the new-chat model picker may choose it): chats on it simply have no fallback.
        candidate["fallback_model"] = fallback
        for key, label, check in (("coding_dir", "Default code folder", user_paths.check_coding_dir_setting),
                                  ("python_venv", "Python environment", user_paths.check_python_venv_setting)):
            value = candidate[key]
            if not isinstance(value, str):
                raise ValueError(f"{label} must be text.")
            value = value.strip()
            # An unchanged value is not checked again: a stale .env path never blocks other saves
            # (Settings reports it as not used).
            if value and value != self.values.get(key, ""):
                try:
                    value = check(value, self, candidate)
                except (ValueError, OSError) as exc:
                    raise ValueError(f"{label}: {exc}") from None
            candidate[key] = value
        candidate["workspace"] = str(workspace)
        # Values equal to .env's are not saved, so a later change in .env still applies.
        persisted = {key: value for key, value in candidate.items()
                     if key not in ENV_KEYS or not self.env_equivalent(key, value, candidate)}
        self.values = candidate
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({**persisted, "settings_version": SETTINGS_VERSION}, indent=2))
        temp.chmod(0o600)
        temp.replace(self.path)
        self.migrated_from = None
        return self.public()

    def check_web_settings(self, candidate):
        """Settings → Web access, checked (and the address and key name tidied) in candidate."""
        from .web_fetch import KEY_NAME, PROVIDERS, check_search_url
        if type(candidate["web_fetch"]) is not bool:
            raise ValueError("Web fetch must be on or off.")
        provider = candidate["web_search_provider"]
        if provider != "" and provider not in PROVIDERS:
            raise ValueError("Web search provider must be none, brave, tavily or searxng.")
        address, name = candidate["web_search_url"], candidate["web_search_key"]
        if not isinstance(address, str) or not isinstance(name, str):
            raise ValueError("The web search address and key name must be text.")
        address, name = address.strip(), name.strip()
        if address:
            try:
                address = check_search_url(address)
            except ValueError:
                if provider == "searxng":
                    raise
                address = ""  # A hidden field another provider does not use.
        candidate["web_search_url"] = address
        if provider == "searxng" and not address:
            raise ValueError("Enter your SearXNG address for web search, such as http://127.0.0.1:8888.")
        if name and not KEY_NAME.match(name):
            raise ValueError("The web search key setting is the name of an entry in your credential file (capital "
                             "letters, digits and _), such as BRAVE_API_KEY; the key itself stays in that file.")
        if name in CREDENTIAL_ENV:
            raise ValueError("Web search cannot use your Databricks credentials.")
        if name and name in self.credential_values(candidate.get("env_file")).values():
            raise ValueError("Enter the name of the credential-file entry that holds the key, not the key itself.")
        candidate["web_search_key"] = name

    def redact(self, text: str) -> str:
        try:
            _, token = self.credentials()
            return text.replace(token, "[REDACTED]")
        except (ValueError, OSError):
            return text
