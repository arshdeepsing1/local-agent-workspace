"""Permission rules: parsing commands, matching rules, protected paths and the saved rule file.

Pure functions plus RuleStore. permissions.evaluate decides with them; agents.py
never matches a rule itself.

Allow rules match NARROWLY: an exact argv, or an exact prefix followed only by
listed options, and never a command that can run other programs (a wrapper such
as env or sh, an inline interpreter, a never-flag such as git -c) or that names
a credential file. Deny and ask rules match BROADLY: the program's basename plus
every rule word somewhere in the command, with short-option clusters expanded.
A command a deny or ask rule might cover but that cannot be checked exactly is
"uncertain" and gets an approval card.
"""
import json
import os
import re
import shlex
import time
import uuid
from pathlib import Path, PurePosixPath

from .config import APP_ROOT  # noqa: F401  (agents.py checks protected paths against it)
from .tools import SECRET_NAMES

MAX_COMMAND_TEXT = 50_000
MAX_TOKENS = 64
MAX_TOKEN_CHARS = 1_000
MAX_ALLOW_PREFIX = 3
MAX_MATCH_PREFIX = 16
MAX_FILE_BYTES = 1_000_000
MAX_PROJECT_RULES = 200
MAX_USER_RULES = 200
BEHAVIORS = ("allow", "ask", "deny")
SCOPES = ("chat", "project", "user")
RULE_FILE = "permission_rules.json"

# Shell syntax: rejected anywhere in the text, including inside quotes.
METACHARACTERS = frozenset(";&|<>(){}[]*?~!#$^`\\")
WRAPPERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "env", "sudo", "doas", "su", "xargs", "nohup",
                      "timeout", "nice", "ionice", "stdbuf", "time", "command", "builtin", "exec", "eval", "noglob",
                      "coproc", "-", "watch", "strace", "ssh", "busybox"})
INTERPRETERS = frozenset({"node", "nodejs", "deno", "bun", "perl", "ruby", "php", "lua", "pwsh", "powershell",
                          "osascript", "Rscript", "tclsh"})
INTERPRETER_PATTERN = re.compile(r"^(python|pypy)[0-9.]*$")
INLINE_FLAGS = frozenset({"-c", "-e", "-E", "-p", "-r", "--eval", "--print", "--command", "-Command"})
SCRIPT_PROGRAMS = frozenset({"awk", "gawk", "mawk", "nawk", "sed"})
# Options that make a program run other programs or read or write outside the command's view.
NEVER_FLAGS = {
    "git": ("--config-env", "--exec-path", "--git-dir", "--work-tree", "--namespace", "--ext-diff", "--textconv",
            "--upload-pack", "--receive-pack", "--output"),
    "rg": ("--pre", "--pre-glob", "-z", "--search-zip"),
    "find": ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"),
}
CLUSTER = re.compile(r"^-[A-Za-z]{2,}$")
UNCERTAIN_TEXT = frozenset("$`\\*?[{")
WORD_SPLIT = re.compile(r"[\s;&|<>()!#^~]+")

# The fixed list of read-only commands that run without a card in Auto and Plan.
READ_ONLY_COMMANDS = frozenset(tuple(text.split()) for text in (
    "pwd", "ls", "ls -l", "ls -a", "ls -la", "ls -al", "ls -1",
    "git status", "git status --short", "git status -s", "git status --porcelain",
    "git diff", "git diff --stat", "git diff --name-only", "git diff --cached", "git diff --staged",
    "git diff --cached --stat", "git log --oneline", "git log --oneline -n 20", "git show", "git show --stat",
    "git branch", "git branch --show-current", "git rev-parse HEAD", "git rev-parse --abbrev-ref HEAD",
    "git rev-parse --show-toplevel", "git ls-files"))
READ_ONLY_SPELLINGS = {"/bin/pwd": "pwd", "/bin/ls": "ls"}

PROTECTED_DIRS = frozenset({".vscode", ".idea", ".husky", ".githooks", ".agents", ".claude", ".devcontainer"})
PROTECTED_FILES = frozenset(name.casefold() for name in ("AGENTS.md", "CLAUDE.md", "CLAUDE.local.md", ".gitattributes",
                                                         ".gitmodules", ".pre-commit-config.yaml", ".envrc"))
HOME_FILES = frozenset({".bashrc", ".bash_profile", ".bash_login", ".profile", ".zshrc", ".zshenv", ".zprofile",
                        ".zlogin", ".gitconfig", ".ssh"})
EDIT_KINDS_RULE = "edits"


def basename(token):
    return PurePosixPath(token).name or token


def parse_command(text):
    """(argv, None) for a plain command, or (None, reason) when it uses shell syntax or cannot be checked."""
    if not isinstance(text, str) or not text.strip():
        return None, "is empty"
    if len(text) > MAX_COMMAND_TEXT:
        return None, "is too long"
    for character in text:
        if ord(character) < 32 or ord(character) == 127:
            return None, "contains a control character such as a tab or line break"
        if character in METACHARACTERS:
            return None, f"uses shell syntax ({character})"
    try:
        argv = shlex.split(text, posix=True)
    except ValueError:
        return None, "has unbalanced quotes"
    if not argv:
        return None, "is empty"
    if len(argv) > MAX_TOKENS:
        return None, f"has more than {MAX_TOKENS} words"
    if any(len(token) > MAX_TOKEN_CHARS for token in argv):
        return None, f"has a word longer than {MAX_TOKEN_CHARS:,} characters"
    if "=" in argv[0]:
        return None, "starts with an environment assignment"
    return argv, None


def expand(tokens):
    """Tokens with clustered short options split: '-rf' -> '-r', '-f'."""
    result = []
    for token in tokens:
        if CLUSTER.match(token):
            result.extend("-" + letter for letter in token[1:])
        else:
            result.append(token)
    return result


def never_flag(program, token):
    """True when the token is an option of this program that a rule can never allow."""
    program = basename(program)
    if program == "git" and token.startswith("-c") and not token.startswith("--"):
        return True
    names = NEVER_FLAGS.get(program, ())
    candidates = [token, *(expand([token]) if CLUSTER.match(token) else [])]
    return any(candidate == name or (name.startswith("--") and candidate.startswith(name + "="))
               for candidate in candidates for name in names)


def inline_interpreter(argv):
    program = basename(argv[0])
    return ((INTERPRETER_PATTERN.match(program) is not None or program in INTERPRETERS)
            and any(token in INLINE_FLAGS for token in argv[1:]))


def interpreter_name(program):
    program = basename(program)
    return INTERPRETER_PATTERN.match(program) is not None or program in INTERPRETERS


def opaque(argv):
    """True when the command can run other programs in ways a rule cannot see."""
    if not argv:
        return True
    program = basename(argv[0])
    return (program in WRAPPERS or inline_interpreter(argv) or program in SCRIPT_PROGRAMS
            or any(never_flag(program, token) for token in argv[1:]))


def names_credential(token, credential_file=None):
    """True when a command word names a credential file (or carries one after '=')."""
    candidates = [token]
    if "=" in token:
        candidates.append(token.split("=", 1)[1])
    configured = str(credential_file) if credential_file else ""
    for candidate in candidates:
        if not candidate:
            continue
        for part in PurePosixPath(candidate).parts:
            folded = part.casefold()
            if folded in SECRET_NAMES or (folded.startswith(".env.") and folded != ".env.example"):
                return True
        if configured and (candidate == configured or os.path.expanduser(candidate) == configured):
            return True
    return False


def read_only_argv(argv):
    if not argv:
        return False
    first = READ_ONLY_SPELLINGS.get(argv[0], argv[0])
    return (first, *argv[1:]) in READ_ONLY_COMMANDS


def behavior_of(rule):
    """M1 chat rules have no behavior and mean allow."""
    return rule.get("behavior", "allow") if isinstance(rule, dict) else "allow"


def same_rule(first, second):
    return (isinstance(first, dict) and isinstance(second, dict)
            and {"behavior": "allow", **first} == {"behavior": "allow", **second})


def has_matcher(rule):
    return any(key in rule for key in ("command", "argv", "prefix", "flags", "path", "hook"))


def rule_label(rule):
    """Short plain text for messages, for example 'git push (any options)'."""
    tool = rule.get("tool", "")
    if tool == "run_command":
        if "command" in rule:
            return f"the command {rule['command']}"
        if "argv" in rule:
            return "the command " + shlex.join(rule["argv"])
        if "prefix" in rule:
            words = shlex.join(rule["prefix"])
            if behavior_of(rule) != "allow":
                return f"commands starting with {words} (any options)"
            flags = rule.get("flags") or []
            return f"commands starting with {words}" + (f" with options {' '.join(flags)}" if flags else "")
        return "every command"
    if tool == "edits":
        return f"file edits under {rule['path']}" if rule.get("path") else "file edits"
    if tool == "hook":
        return f"the hook {rule['hook']}" if rule.get("hook") else "hooks"
    if tool == "delegate_task":
        return "subagents"
    if tool == "stop_job":
        return "stopping jobs"
    if tool.startswith("mcp__") and tool.endswith("__*"):
        return f"all tools of MCP server {tool[5:-3]}"
    return f"the tool {tool}"


MCP_NAME = re.compile(r"^mcp__[A-Za-z0-9_-]{1,64}__[A-Za-z0-9_.-]{1,128}$")
MCP_WILDCARD = re.compile(r"^mcp__([A-Za-z0-9_-]{1,64})__\*$")
RULE_KEYS = frozenset({"behavior", "tool", "command", "argv", "prefix", "flags", "path", "hook"})
DRAFT_KEYS = frozenset({"behavior", "tool", "text", "match", "options"})


def _tokens(value, label, limit):
    if not isinstance(value, list) or not all(isinstance(token, str) for token in value):
        raise ValueError(f"{label} must be a list of words.")
    if len(value) > limit:
        raise ValueError(f"{label} can have at most {limit} words.")
    for token in value:
        if not token or len(token) > MAX_TOKEN_CHARS or any(ord(c) < 32 or ord(c) == 127 for c in token):
            raise ValueError(f"{label} contains an empty, overlong or control-character word.")
    return list(value)


def _split_options(options):
    if options is None:
        return []
    if not isinstance(options, str) or len(options) > MAX_COMMAND_TEXT:
        raise ValueError("Options must be text.")
    if not options.strip():
        return []
    for character in options:
        if ord(character) < 32 or ord(character) == 127 or character in METACHARACTERS:
            raise ValueError(f"The options use shell syntax ({character!r}).")
    try:
        return shlex.split(options, posix=True)
    except ValueError as exc:
        raise ValueError("The options have unbalanced quotes.") from exc


def _check_allow_command(words, flags, credential_file):
    program = basename(words[0])
    if program in WRAPPERS:
        raise ValueError(f"'{words[0]}' runs other programs; a rule cannot allow it. Allow the program it runs instead.")
    if program in SCRIPT_PROGRAMS or (interpreter_name(program) and any(t in INLINE_FLAGS for t in words + flags)):
        raise ValueError(f"'{words[0]}' can run code given on the command line; a rule cannot allow that.")
    for token in [*words[1:], *flags]:
        if never_flag(program, token):
            raise ValueError(f"'{token}' can run other programs; a rule cannot allow it.")
    for token in [*words, *flags]:
        if names_credential(token, credential_file):
            raise ValueError("Rules cannot allow commands that name credential files.")


def normalize_rule(draft, *, resolve_path, credential_file=None, credential_tokens=()):
    """A canonical rule dict from a rule or a command draft, or ValueError with a message for the user."""
    from .permissions import TOOL_KINDS
    if not isinstance(draft, dict):
        raise ValueError("A rule must be an object.")
    behavior = draft.get("behavior", "allow")
    if behavior not in BEHAVIORS:
        raise ValueError("A rule's behavior must be allow, ask or deny.")
    tool = draft.get("tool")
    if not isinstance(tool, str) or not tool:
        raise ValueError("Choose what the rule applies to.")
    is_draft = "text" in draft
    unknown = set(draft) - (DRAFT_KEYS if is_draft else RULE_KEYS)
    if unknown:
        raise ValueError(f"Unknown rule field: {sorted(unknown)[0][:40]}.")
    rule = {"behavior": behavior, "tool": tool}
    if tool == "run_command":
        if "command" in draft:
            raise ValueError("Exact command text can only be allowed for one chat, from an approval card.")
        if is_draft:
            argv, reason = parse_command(draft["text"])
            if argv is None:
                raise ValueError(f"The command {reason}.")
            match = draft.get("match", "exact")
            if match not in ("exact", "prefix"):
                raise ValueError("Match must be exact or prefix.")
            flags = _split_options(draft.get("options"))
            if behavior == "allow":
                if match == "exact":
                    if flags:
                        raise ValueError("Allowed options apply only to commands starting with a prefix.")
                    rule["argv"] = argv
                else:
                    rule.update(prefix=argv, flags=flags)
            else:
                if flags:
                    raise ValueError("Deny and ask rules match any options; leave the options empty.")
                rule["prefix"] = argv
        else:
            if "argv" in draft and "prefix" in draft:
                raise ValueError("A rule has either an exact command or a prefix, not both.")
            if "argv" in draft:
                argv = _tokens(draft["argv"], "The command", MAX_TOKENS)
                if not argv:
                    raise ValueError("Enter a command.")
                if behavior == "allow":
                    rule["argv"] = argv
                else:
                    rule["prefix"] = argv
            elif "prefix" in draft:
                rule["prefix"] = _tokens(draft["prefix"], "The prefix", MAX_TOKENS)
            if "flags" in draft:
                if behavior != "allow" or "prefix" not in draft:
                    raise ValueError("Allowed options apply only to allow rules with a prefix.")
                rule["flags"] = _tokens(draft["flags"], "The options", MAX_TOKENS)
            if "path" in draft or "hook" in draft:
                raise ValueError("A command rule has no path or hook.")
        if "prefix" in rule:
            if not rule["prefix"]:
                raise ValueError("Enter a command.")
            if behavior == "allow" and len(rule["prefix"]) > MAX_ALLOW_PREFIX:
                raise ValueError(f"An allow rule can match at most {MAX_ALLOW_PREFIX} words at the start of a command.")
            if behavior != "allow" and len(rule["prefix"]) > MAX_MATCH_PREFIX:
                raise ValueError(f"A deny or ask rule can have at most {MAX_MATCH_PREFIX} words.")
            if behavior == "allow":
                rule["flags"] = list(rule.get("flags", []))
        if behavior == "allow":
            words = rule.get("argv") or rule.get("prefix")
            if not words:
                raise ValueError("An allow rule needs a command or a prefix; a rule cannot allow every command.")
            for flag in rule.get("flags", []):
                if flag.endswith("=") and len(flag) == 1:
                    raise ValueError("An option ending in '=' needs a name.")
            _check_allow_command(words, rule.get("flags", []), credential_file)
    elif tool == EDIT_KINDS_RULE:
        if set(draft) - {"behavior", "tool", "path"}:
            raise ValueError("A file-edit rule can only have a folder path.")
        if "path" in draft:
            path = draft["path"]
            if not isinstance(path, str) or not path.strip() or len(path) > 4096:
                raise ValueError("Enter an absolute folder path.")
            if not Path(path).expanduser().is_absolute():
                raise ValueError("Enter an absolute folder path.")
            rule["path"] = str(resolve_path(path))
            if not Path(rule["path"]).is_absolute():
                raise ValueError("Enter an absolute folder path.")
    elif tool == "hook":
        if set(draft) - {"behavior", "tool", "hook"}:
            raise ValueError("A hook rule can only name a hook.")
        if "hook" in draft:
            hook = draft["hook"]
            if not isinstance(hook, str) or not hook or len(hook) > 200:
                raise ValueError("Enter a hook ID.")
            rule["hook"] = hook
        elif behavior == "allow":
            raise ValueError("An allow rule for hooks must name one hook.")
    else:
        if has_matcher(draft) or is_draft:
            raise ValueError(f"A rule for {tool[:80]} has no other fields.")
        if MCP_WILDCARD.match(tool):
            if behavior == "allow":
                raise ValueError("A rule for every tool of an MCP server can only deny or ask.")
        elif tool in ("delegate_task", "stop_job") or MCP_NAME.match(tool):
            pass
        elif tool in TOOL_KINDS:
            if behavior != "deny":
                raise ValueError(f"Only deny rules apply to {tool}.")
        else:
            raise ValueError(f"Unknown tool: {tool[:80]}.")
    serialized = json.dumps(rule)
    if any(token and token in serialized for token in credential_tokens or ()):
        raise ValueError("A rule cannot contain your Databricks token.")
    return dict(sorted(rule.items()))


def _program_matches(rule_words, argv):
    if basename(argv[0]) != basename(rule_words[0]):
        return False
    present = set(expand(argv[1:]))
    return all(word in present for word in expand(rule_words[1:]))


def _flag_allowed(token, flags):
    if token in flags:
        return True
    if "=" in token:
        name = token.split("=", 1)[0] + "="
        return name in flags
    return False


def rule_matches(rule, name, arguments, *, resolve_path, credential_file=None):
    """True when the rule applies to this tool call (allow narrowly, deny and ask broadly)."""
    from .permissions import EDIT_TOOL_PATHS, tool_kind
    behavior = behavior_of(rule)
    tool = rule.get("tool")
    arguments = arguments if isinstance(arguments, dict) else {}
    if tool == "run_command":
        if name != "run_command":
            return False
        text = arguments.get("command")
        if not isinstance(text, str):
            return False
        if "command" in rule:
            return behavior == "allow" and text.strip() == rule["command"]
        argv, _ = parse_command(text)
        if behavior == "allow":
            if argv is None or opaque(argv) or any(names_credential(token, credential_file) for token in argv):
                return False
            if "argv" in rule:
                return argv == rule["argv"]
            if "prefix" in rule:
                prefix, flags = rule["prefix"], rule.get("flags") or []
                return (argv[:len(prefix)] == prefix
                        and all(_flag_allowed(token, flags) for token in argv[len(prefix):]))
            return False
        words = rule.get("prefix") or rule.get("argv")
        if not words:
            return True
        return argv is not None and _program_matches(words, argv)
    if tool == EDIT_KINDS_RULE:
        if tool_kind(name) != "edit":
            return False
        if not rule.get("path"):
            return True
        # Allow: every path the tool touches is inside the folder. Deny and ask: any of them is
        # (a move into or out of the folder), and a path that cannot be resolved counts as inside.
        root = Path(rule["path"])
        results = []
        for key in EDIT_TOOL_PATHS.get(name, ()):
            value = arguments.get(key)
            if value is None:
                continue
            try:
                results.append(isinstance(value, str) and resolve_path(value).is_relative_to(root))
            except (ValueError, OSError):
                results.append(behavior != "allow")
        return bool(results) and (all(results) if behavior == "allow" else any(results))
    if tool == "hook":
        return (isinstance(name, str) and name.startswith("hook_")
                and (rule.get("hook") is None or arguments.get("hook_id") == rule["hook"]))
    if isinstance(tool, str):
        wildcard = MCP_WILDCARD.match(tool)
        if wildcard:
            return isinstance(name, str) and name.startswith(f"mcp__{wildcard.group(1)}__")
    return name == tool


def uncertain_match(rule, text, argv):
    """True when a command deny or ask rule might apply but the command cannot be checked exactly."""
    if rule.get("tool") != "run_command" or behavior_of(rule) == "allow" or "command" in rule:
        return False
    if argv is not None:
        return opaque(argv)
    if not isinstance(text, str):
        return False
    if any(character in UNCERTAIN_TEXT for character in text):
        return True
    words = rule.get("prefix") or rule.get("argv")
    program = basename(words[0]) if words else None
    for word in WORD_SPLIT.split(text.replace("'", "").replace('"', "")):
        if not word:
            continue
        name = basename(word)
        if name in WRAPPERS or interpreter_name(name) or program is None or name == program:
            return True
    return False


def default_rule(name, arguments):
    """The rule an 'Always allow' answer stores when the request sends none, or None."""
    from .permissions import tool_kind
    arguments = arguments if isinstance(arguments, dict) else {}
    kind = tool_kind(name)
    if kind == "command":
        argv, _ = parse_command(arguments.get("command"))
        if argv is None or opaque(argv) or any(names_credential(token) for token in argv):
            return None
        return {"argv": argv, "behavior": "allow", "tool": "run_command"}
    if kind == "edit":
        return {"behavior": "allow", "tool": EDIT_KINDS_RULE}
    if kind in ("mcp", "delegate", "job_stop"):
        return {"behavior": "allow", "tool": name}
    if kind == "hook":
        hook = arguments.get("hook_id")
        return {"behavior": "allow", "hook": hook, "tool": "hook"} if isinstance(hook, str) and hook else None
    return None


def _why_not(argv, credential_file):
    program = basename(argv[0])
    if program in WRAPPERS or program in SCRIPT_PROGRAMS or inline_interpreter(argv):
        return f"{argv[0]} can run other programs or code, so it cannot be allowed as a rule."
    if any(never_flag(program, token) for token in argv[1:]):
        return "It uses an option that can run other programs, so it cannot be allowed as a rule."
    if any(names_credential(token, credential_file) for token in argv):
        return "It names a credential file, so it cannot be allowed as a rule."
    return None


def card_rule_options(name, arguments, *, resolve_path=None, credential_file=None, credential_tokens=()):
    """What an approval card can offer to remember: {argv, prefixes, reason} for commands, {folder} for edits."""
    from .permissions import tool_kind
    arguments = arguments if isinstance(arguments, dict) else {}
    kind = tool_kind(name)
    if kind == "command":
        argv, reason = parse_command(arguments.get("command"))
        if argv is None:
            return {"argv": None, "prefixes": [], "reason": f"This command {reason}."}
        context = {"resolve_path": resolve_path, "credential_file": credential_file, "credential_tokens": credential_tokens}
        exact = None
        try:
            exact = normalize_rule({"tool": "run_command", "behavior": "allow", "argv": argv}, **context)["argv"]
            why = None
        except ValueError as exc:
            why = _why_not(argv, credential_file) or str(exc)
        prefixes = []
        for size in range(1, MAX_ALLOW_PREFIX + 1):
            if size > len(argv) or argv[size - 1].startswith("-"):
                break
            try:
                rule = normalize_rule({"tool": "run_command", "behavior": "allow", "prefix": argv[:size],
                                       "flags": argv[size:]}, **context)
            except ValueError:
                continue
            prefixes.append({"prefix": rule["prefix"], "flags": rule["flags"]})
        return {"argv": exact, "prefixes": prefixes if exact is not None else [], "reason": why}
    if kind == "edit":
        value = arguments.get("path")
        folder = None
        if isinstance(value, str) and resolve_path is not None:
            try:
                folder = str(resolve_path(value).parent)
            except (ValueError, OSError):
                folder = None
        return {"folder": folder}
    return None


def _relative_parts(path, root):
    path = Path(path)
    parts = path.relative_to(root).parts if path.is_relative_to(root) else path.parts
    return [str(part).casefold() for part in parts]


def protected_reason(requested, resolved, *, root, home, app_root):
    """Why a file edit target is protected (it can make later commands or the app run code), or None."""
    for candidate in (Path(requested), Path(resolved)):
        parts = _relative_parts(candidate, Path(root))
        for part in parts:
            if part in PROTECTED_DIRS:
                return f"editor and repository configuration ({part})"
        for first, second in zip(parts, parts[1:]):
            if first == ".github" and second in ("workflows", "actions"):
                return f"CI configuration (.github/{second})"
        if parts and parts[-1] in PROTECTED_FILES:
            return f"agent and repository configuration ({candidate.name})"
        if home is not None and candidate.is_relative_to(home):
            inside = [str(part).casefold() for part in candidate.relative_to(home).parts]
            if inside and inside[0] in HOME_FILES:
                return f"shell and Git startup files (~/{candidate.relative_to(home).parts[0]})"
            if inside[:2] == [".config", "git"]:
                return "Git configuration (~/.config/git)"
        if app_root is not None and candidate.is_relative_to(app_root):
            return "this app's own files"
    return None


def project_key(workspace):
    return str(Path(workspace).expanduser().resolve())


class RuleStore:
    """Project and user rules in <state_dir>/permission_rules.json (0600, atomic writes, cached by mtime and size).

    Rules never live in repository files: .claude/settings*.json and .agents/permissions.json are never read.
    """

    def __init__(self, state_dir):
        self.path = Path(state_dir) / RULE_FILE
        self.problem = None
        self._signature = None
        self._data = {"version": 1, "user": [], "projects": {}}
        self._load()

    @staticmethod
    def _valid_rules(value):
        return isinstance(value, list) and all(isinstance(rule, dict) and isinstance(rule.get("tool"), str)
                                               and behavior_of(rule) in BEHAVIORS for rule in value)

    def _load(self):
        try:
            info = self.path.stat()
        except FileNotFoundError:
            self._signature, self.problem = None, None
            self._data = {"version": 1, "user": [], "projects": {}}
            return
        except OSError as exc:
            self.problem = f"Saved permission rules could not be read: {exc.strerror or 'the file is not accessible'}."
            self._data = {"version": 1, "user": [], "projects": {}}
            return
        signature = (info.st_mtime_ns, info.st_size)
        if signature == self._signature:
            return
        self._signature = signature
        self._data = {"version": 1, "user": [], "projects": {}}
        try:
            if info.st_size > MAX_FILE_BYTES:
                raise ValueError("the file is larger than 1 MB")
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("the file is not valid JSON") from exc
            if (not isinstance(data, dict) or data.get("version") != 1 or not self._valid_rules(data.get("user"))
                    or not isinstance(data.get("projects"), dict)
                    or not all(isinstance(key, str) and self._valid_rules(value) for key, value in data["projects"].items())):
                raise ValueError("the file does not have the expected shape")
            self._data = {"version": 1, "user": data["user"], "projects": data["projects"]}
            self.problem = None
        except OSError as exc:
            self.problem = f"Saved permission rules could not be read: {exc.strerror or 'the file is not accessible'}."
        except ValueError as exc:
            self.problem = f"Saved permission rules could not be read: {exc}."

    def rules(self, workspace):
        self._load()
        if self.problem:
            return {"project": [], "user": []}
        return {"project": [dict(rule) for rule in self._data["projects"].get(project_key(workspace), [])],
                "user": [dict(rule) for rule in self._data["user"]]}

    def _write(self, data):
        text = json.dumps(data, indent=1, sort_keys=True)
        if len(text.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("Saved permission rules are limited to 1 MB. Remove some rules first.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.parent / f".{RULE_FILE}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        os.chmod(self.path, 0o600)
        self._signature = None
        self._load()

    def _editable(self):
        self._load()
        if self.problem:
            raise ValueError("Saved permission rules could not be read; reset them first.")
        return json.loads(json.dumps(self._data))

    def add(self, scope, workspace, rule):
        data = self._editable()
        if scope == "user":
            rules = data["user"]
            if any(same_rule(rule, item) for item in rules):
                return
            if len(rules) >= MAX_USER_RULES:
                raise ValueError(f"You already have {MAX_USER_RULES} rules for all projects. Remove one first.")
        elif scope == "project":
            rules = data["projects"].setdefault(project_key(workspace), [])
            if any(same_rule(rule, item) for item in rules):
                return
            if len(rules) >= MAX_PROJECT_RULES:
                raise ValueError(f"This project already has {MAX_PROJECT_RULES} rules. Remove one first.")
        else:
            raise ValueError("Scope must be project or user.")
        rules.append(dict(rule))
        self._write(data)

    def remove(self, scope, workspace, rule):
        data = self._editable()
        if scope == "user":
            data["user"] = [item for item in data["user"] if not same_rule(rule, item)]
        elif scope == "project":
            key = project_key(workspace)
            remaining = [item for item in data["projects"].get(key, []) if not same_rule(rule, item)]
            if remaining:
                data["projects"][key] = remaining
            else:
                data["projects"].pop(key, None)
        else:
            raise ValueError("Scope must be project or user.")
        self._write(data)

    def reset(self):
        """Move a file that could not be read aside, as permission_rules.json.bad-<unix time>, and start empty."""
        if self.path.exists():
            os.replace(self.path, self.path.with_name(f"{RULE_FILE}.bad-{int(time.time())}"))
        self._signature, self.problem = None, None
        self._data = {"version": 1, "user": [], "projects": {}}
