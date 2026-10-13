import ast
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from .context import (
    DEFAULT_CONTEXT_WINDOW, DEFAULT_MAX_OUTPUT_TOKENS, MAX_CONTEXT_WINDOW,
    MAX_MAX_OUTPUT_TOKENS, MIN_CONTEXT_WINDOW, MIN_MAX_OUTPUT_TOKENS, SAFETY_MARGIN,
)
from .context import DEFAULT_AUTO_COMPACT_PERCENT, MAX_AUTO_COMPACT_PERCENT, MIN_AUTO_COMPACT_PERCENT

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
            "model": self.env.get("LOCAL_AGENT_MODEL", "databricks-gpt-oss-120b"),
            "env_file": self.env.get("LOCAL_AGENT_ENV_FILE", ""),
            "context_window": DEFAULT_CONTEXT_WINDOW,
            "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
            "max_agent_steps": DEFAULT_MAX_AGENT_STEPS,
            "compaction_handoffs": True,
            "auto_compact_percent": DEFAULT_AUTO_COMPACT_PERCENT,
            "approval_timeout_minutes": DEFAULT_APPROVAL_MINUTES,
            "system_notifications": False,
            "user_instructions": "",
            "write_limit_kb": 80,
            "command_env": [],
            "rate_limit_auto_continue": True,
            "auto_keep_changes": False,
        }
        if self.path.exists():
            saved = json.loads(self.path.read_text())
            self.values.update({key: value for key, value in saved.items() if key in self.values})

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
        try:
            host, _ = self.credentials()
            configured = True
        except (ValueError, OSError):
            host, configured = "", False
        return {**self.values, "host": host, "configured": configured}

    def update(self, values: dict):
        candidate = {**self.values, **{k: v for k, v in values.items() if k in self.values}}
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
        if type(candidate["rate_limit_auto_continue"]) is not bool:
            raise ValueError("Continue after rate limits must be on or off.")
        if type(candidate["auto_keep_changes"]) is not bool:
            raise ValueError("Automatic Keep must be on or off.")
        candidate["workspace"] = str(workspace)
        self.values = candidate
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(candidate, indent=2))
        temp.chmod(0o600)
        temp.replace(self.path)
        return self.public()

    def redact(self, text: str) -> str:
        try:
            _, token = self.credentials()
            return text.replace(token, "[REDACTED]")
        except (ValueError, OSError):
            return text
