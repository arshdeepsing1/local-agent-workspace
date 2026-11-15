"""Permission rules: parsing commands, matching rules, protected paths and the saved rule file.

Pure functions plus RuleStore. permissions.evaluate decides with them; agents.py
never matches a rule itself.

Allow rules match NARROWLY: an exact argv, or an exact prefix followed only by
listed options, and never a command that can run other programs (a wrapper such
as env or sh, an inline interpreter, a never-flag such as git -c) or that names
a credential-looking file (secret_paths). An allow rule for an argv or prefix
applies only to a command run in the project folder (no cwd, or the project's
own); an exact-text chat rule remembers the folder it was approved in. Deny and
ask rules match BROADLY, in any folder: the program's basename plus every rule
word somewhere in the command, with short-option clusters expanded. A command a
deny or ask rule might cover but that cannot be checked exactly is "uncertain"
and gets an approval card; a vetted command (shell_vet) is checked stage by stage.
"""
import json
import os
import re
import shlex
import stat
import time
import unicodedata
import uuid
from pathlib import Path, PurePosixPath

from .config import APP_ROOT  # noqa: F401  (agents.py checks protected paths against it)
from .secret_paths import looks_secret
from .tools import _identity, parts_below

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
INLINE_LETTERS = frozenset("ceEpr")
INLINE_LONG = ("--eval=", "--print=", "--command=")
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
# '=', ',' and ':' too, so the program in an alias definition (alias p='git push') is a word of its own.
WORD_SPLIT = re.compile(r"[\s;&|<>()!#^~=,:]+")
UNCERTAIN_WORDS = frozenset({"alias", "source"})
# The words a credential check reads: split like WORD_SPLIT, but a word keeps its '~' and its 'name='.
CREDENTIAL_SPLIT = re.compile(r"[\s;&|<>()`$!#{}^,:]+")

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
PROTECTED_FILES = frozenset(name.casefold() for name in ("AGENTS.md", "AGENTS.local.md", "CLAUDE.md", "CLAUDE.local.md",
                                                         ".gitattributes", ".gitmodules", ".pre-commit-config.yaml",
                                                         ".envrc"))
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


def inline_flag(token):
    """True when an interpreter option gives it code to run: -c or -e alone, in a cluster
    (perl -le, python3 -Ic), with the code attached (-esystem...), or as --eval=..."""
    if token in INLINE_FLAGS or token.startswith(INLINE_LONG):
        return True
    if token.startswith("--") or len(token) < 3 or token[0] != "-":
        return False
    if CLUSTER.match(token) and any(letter in INLINE_LETTERS for letter in token[1:]):
        return True
    return token[1] in INLINE_LETTERS  # Code attached to the option: '-esystem qw/x/', '-cimport os'.


def inline_interpreter(argv):
    return interpreter_name(argv[0]) and any(inline_flag(token) for token in argv[1:])


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


def names_credential(token, credential_file=None, patterns=()):
    """True when a command word names a credential-looking file (or carries one after '='); see secret_paths."""
    return looks_secret(token, credential_file, patterns) is not None


def text_words(text):
    """The words of command text as a credential check sees them: quotes and backslashes removed, split at
    whitespace, shell syntax, ',' and ':' (a word keeps its '~' and its 'name=': names_credential reads both)."""
    return [word for word in CREDENTIAL_SPLIT.split(text.replace("'", "").replace('"', "").replace("\\", "")) if word]


def named_credentials(text, credential_file=None, patterns=(), limit=5):
    """The words of command text that name credential-looking files (at most limit, each once)."""
    if not isinstance(text, str):
        return []
    found = []
    for word in text_words(text):
        if word not in found and names_credential(word, credential_file, patterns):
            found.append(word)
            if len(found) >= limit:
                break
    return found


def text_names_credential(text, credential_file=None):
    """True when any word of command text (quotes removed, split at shell syntax and '=') names a credential file."""
    return bool(named_credentials(text, credential_file, limit=1))


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
    return any(key in rule for key in ("command", "argv", "prefix", "flags", "path", "hook", "domain"))


LOOPBACK_KEY = re.compile(r":\d{1,5}\Z")


def is_loopback_key(key):
    """True for a browser host key of this computer: it keeps its port (browser.host_key), a public host's key never does."""
    return isinstance(key, str) and LOOPBACK_KEY.search(key) is not None


def browser_host(arguments):
    """The host key of a browser call: the app's own 'host' (browser_tool.decide sets it from the page), else the
    key of an open call's URL, else None."""
    arguments = arguments if isinstance(arguments, dict) else {}
    host = arguments.get("host")
    if isinstance(host, str) and host:
        return host
    if arguments.get("action") == "open" and isinstance(arguments.get("url"), str):
        from .browser import AddressRefused, host_key
        try:
            return host_key(arguments["url"])
        except AddressRefused:
            return None
    return None


def normalize_browser_host(value):
    """A browser rule's host key from what the user typed: docs.example.com, localhost:3000, or a URL. A loopback
    address keeps its port. ValueError with a message for the user."""
    from .browser import AddressRefused, parse_address
    hint = "Enter a host such as docs.example.com or localhost:3000."
    if not isinstance(value, str) or not value.strip() or len(value) > 253:
        raise ValueError(hint)
    text = value.strip()
    try:
        return parse_address(text if "://" in text else "http://" + text).key
    except AddressRefused as exc:
        if exc.reason != "private_address" or "://" in text or ":" in text:
            raise ValueError(f"{hint} {exc}") from None
    from .web_fetch import normalize_domain  # A private name: only a deny or ask rule can use it.
    return normalize_domain(text)


def rule_label(rule):
    """Short plain text for messages, for example 'git push (any options)'."""
    tool = rule.get("tool", "")
    if tool == "run_command":
        if "command" in rule:
            return f"the command {rule['command']}" + (f" in {rule['cwd']}" if rule.get("cwd") else "")
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
    if tool == "remember":
        return "saving project memory"
    if tool == "web_fetch":
        if not rule.get("domain"):
            return "web fetches"
        return f"fetches from {rule['domain']}" + ("" if behavior_of(rule) == "allow" else " and its subdomains")
    if tool == "web_search":
        return "web searches"
    if tool == "browser":
        domain = rule.get("domain")
        if not domain:
            return "browsing"
        return f"Browse {domain}" + ("" if behavior_of(rule) == "allow" or is_loopback_key(domain) else " and its subdomains")
    if tool.startswith("mcp__") and tool.endswith("__*"):
        return f"all tools of MCP server {tool[5:-3]}"
    return f"the tool {tool}"


MCP_NAME = re.compile(r"^mcp__[A-Za-z0-9_-]{1,64}__[A-Za-z0-9_.-]{1,128}$")
MCP_WILDCARD = re.compile(r"^mcp__([A-Za-z0-9_-]{1,64})__\*$")
RULE_KEYS = frozenset({"behavior", "tool", "command", "argv", "prefix", "flags", "path", "hook", "domain"})
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
    if program in SCRIPT_PROGRAMS or (interpreter_name(program) and any(inline_flag(t) for t in words[1:] + flags)):
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
            if "path" in draft or "hook" in draft or "domain" in draft:
                raise ValueError("A command rule has no path, hook or domain.")
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
    elif tool == "web_fetch":
        # Allow names one host exactly; deny and ask cover a domain and its subdomains, or every fetch.
        from .web_fetch import normalize_domain
        if is_draft or set(draft) - {"behavior", "tool", "domain"}:
            raise ValueError("A web fetch rule can only name a domain.")
        if "domain" in draft:
            rule["domain"] = normalize_domain(draft["domain"])
        elif behavior == "allow":
            raise ValueError("An allow rule for web fetches must name one domain, such as docs.python.org.")
    elif tool == "browser":
        # Allow names one host key exactly; deny and ask cover a domain and its subdomains (a loopback key: that
        # port only), or all browsing.
        if is_draft or set(draft) - {"behavior", "tool", "domain"}:
            raise ValueError("A browser rule can only name a host.")
        if "domain" in draft:
            rule["domain"] = normalize_browser_host(draft["domain"])
        elif behavior == "allow":
            raise ValueError("An allow rule for browsing must name one host, such as docs.python.org or localhost:3000.")
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
        elif tool in ("delegate_task", "stop_job", "remember", "web_search") or MCP_NAME.match(tool):
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


def command_folder(arguments, resolve_path):
    """The resolved folder a run_command call names with cwd, None without one, or "" when it cannot be resolved."""
    cwd = arguments.get("cwd") if isinstance(arguments, dict) else None
    if cwd is None:
        return None
    if resolve_path is None or not isinstance(cwd, str):
        return ""
    try:
        return str(resolve_path(cwd))
    except (ValueError, OSError, TypeError):
        return ""


def at_project_root(arguments, resolve_path):
    """True when a command runs in the project folder: no cwd, or one that resolves to the project itself."""
    folder = command_folder(arguments, resolve_path)
    if folder is None:
        return True
    try:
        return bool(folder) and folder == str(resolve_path("."))
    except (ValueError, OSError, TypeError):
        return False


def _same_folder(stored, arguments, resolve_path):
    """An exact-text chat rule's folder (None: the project) against the folder the call runs in."""
    if at_project_root(arguments, resolve_path):
        return stored is None
    return stored is not None and command_folder(arguments, resolve_path) == stored


def allows_argv(rule, argv, credential_file=None):
    """True when a command allow rule (an argv or a prefix) allows exactly this argument vector."""
    if (rule.get("tool") != "run_command" or behavior_of(rule) != "allow" or "command" in rule or not argv
            or opaque(argv) or any(names_credential(token, credential_file) for token in argv)):
        return False
    argv = list(argv)
    if "argv" in rule:
        return argv == rule["argv"]
    if "prefix" in rule:
        prefix, flags = rule["prefix"], rule.get("flags") or []
        return argv[:len(prefix)] == prefix and all(_flag_allowed(token, flags) for token in argv[len(prefix):])
    return False


def stage_matches(rule, argv):
    """True when a command deny or ask rule covers one stage of a vetted command, compared word by word."""
    if rule.get("tool") != "run_command" or behavior_of(rule) == "allow" or "command" in rule:
        return False
    words = rule.get("prefix") or rule.get("argv")
    return not words or _program_matches(words, list(argv))


_CASE_BY_DEVICE = {}  # (st_dev, inode of the volume's top folder) -> (True when names ignore letter case, time kept).
_CASE_KEEP_SECONDS = 600  # A removable volume can reuse a device number, so an old answer is probed again.
_PROBE_CHILDREN = 8
_ASCII_SWAP = str.maketrans("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
                            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


def _clock():
    return time.monotonic()


def _case_variant(name):
    """The name with every ASCII letter swapped, or None when it has none. Other letters stay as they are: a
    disk may not fold them (APFS keeps the dotless i apart from I), and that must not read as 'case-sensitive'."""
    variant = name.translate(_ASCII_SWAP)
    return variant if variant != name else None


def _same_entry(first, second):
    """True or False when two names are or are not one file entry (lstat identity), None when it cannot be told."""
    try:
        one = os.lstat(first)
    except OSError:
        return None
    try:
        other = os.lstat(second)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return None
    return os.path.samestat(one, other)


def _probe_name(parent, name):
    """Whether the volume of the folder 'parent' ignores case, judged by one existing folder name. None when the
    name has no ASCII letter, is not a folder (a file can have two hard links), or the lookup failed. Only
    reads: nothing is created."""
    variant = _case_variant(name)
    if variant is None:
        return None
    try:
        if not stat.S_ISDIR(os.lstat(Path(parent) / name).st_mode):
            return None
    except (OSError, ValueError):
        return None
    return _same_entry(Path(parent) / name, Path(parent) / variant)


def _child_results(folder):
    """The probe answers of the first few sub-folders of folder. Files are skipped: two hard links to one file
    look like one entry on a case-sensitive disk."""
    try:
        with os.scandir(folder) as entries:
            for index, entry in enumerate(entries):
                if index >= _PROBE_CHILDREN:
                    break
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                result = _probe_name(folder, entry.name)
                if result is not None:
                    yield result
    except (OSError, ValueError):
        return


def _probe_results(folder, device):
    """The non-empty probe answers in order: the folder's own name, its ancestors on the same device (a name
    without an ASCII letter, such as 2024, cannot tell), then a few children."""
    folder = Path(folder)
    for candidate in (folder, *folder.parents):
        if candidate == candidate.parent:
            break
        try:
            if os.stat(candidate.parent).st_dev != device:
                break  # Another volume: its case rule says nothing about this one.
        except (OSError, ValueError):
            break
        result = _probe_name(candidate.parent, candidate.name)
        if result is not None:
            yield result
    yield from _child_results(folder)


def _probe_volume(folder, device):
    """True, False or None (cannot tell) for the volume holding folder's entries. 'Same file' is trusted at
    once. 'Does not exist' needs a second probe to agree when there is one, and two probes that disagree
    mean None."""
    try:
        results = _probe_results(folder, device)
        first = next(results, None)
        if first is not False:
            return first
        second = next(results, None)
        return None if second is True else False
    except (OSError, ValueError):
        return None


def _volume_key(folder, device):
    """(device, inode of the highest folder above this one on the same device): the volume's mount root."""
    path, top = Path(folder), None
    while True:
        info = os.stat(path)
        if info.st_dev != device:
            break
        top = info.st_ino
        if path.parent == path:
            break
        path = path.parent
    return (device, top)


def _volume_ignores_case(folder, default=False, device=None):
    """True when names directly inside this existing folder match without regard to letter case. The answer is
    kept per volume for ten minutes. When no probe can tell, the answer is `default`, which is never kept.
    device is the folder's st_dev when the caller already has it."""
    try:
        if device is None:
            device = os.stat(folder).st_dev
        key = _volume_key(folder, device)
        kept = _CASE_BY_DEVICE.get(key)
        if kept is not None and _clock() - kept[1] < _CASE_KEEP_SECONDS:
            return kept[0]
        result = _probe_volume(folder, device)
    except (OSError, ValueError):
        return default
    if result is None:
        return default
    _CASE_BY_DEVICE[key] = (result, _clock())
    return result


def _inside_folder(path, root, unknown_matches=False):
    """True when path is root or inside it. resolve() keeps the spelling the caller typed, so the comparison
    cannot be by name alone: on a case-insensitive disk 'private' and 'Private' are one folder, and on a
    case-sensitive one they are two. An existing folder is settled by file identity (which also covers composed
    and decomposed Unicode names). A folder that does not exist yet has no identity, so its nearest readable
    existing parent is matched by identity and the missing names are compared exactly, or without letter case
    when that parent's volume ignores it. unknown_matches says what to assume when the volume cannot be probed:
    True for deny and ask rules (match the variant), False for allow rules. An unreadable folder counts as
    missing, and a path that differs by name at every level never matches. An exact-name match costs no stat."""
    path, root = Path(path), Path(root)
    if path.is_relative_to(root):
        return True
    missing = []
    anchor = root
    target = _identity(anchor)
    while target is None and anchor != anchor.parent:
        missing.insert(0, anchor.name)
        anchor = anchor.parent
        target = _identity(anchor)
    if target is None:
        return False
    below = None
    if path.is_relative_to(anchor):
        below = path.relative_to(anchor).parts
    else:
        for ancestor in (path, *path.parents):
            if _identity(ancestor) == target:
                below = path.relative_to(ancestor).parts
                break
    if below is None:
        return False
    if not missing:
        return True
    if len(below) < len(missing):
        return False
    wanted, got = list(missing), list(below[:len(missing)])
    if wanted == got:
        return True
    if not _volume_ignores_case(anchor, default=unknown_matches, device=target[0]):
        return False
    fold = lambda parts: [unicodedata.normalize("NFC", part).casefold() for part in parts]
    return fold(wanted) == fold(got)


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
            return (behavior == "allow" and text.strip() == rule["command"]
                    and _same_folder(rule.get("cwd"), arguments, resolve_path))
        argv, _ = parse_command(text)
        if behavior == "allow":
            # Another folder has other Makefiles and scripts: argv and prefix rules apply in the project only.
            return argv is not None and at_project_root(arguments, resolve_path) and allows_argv(rule, argv, credential_file)
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
                results.append(isinstance(value, str) and _inside_folder(resolve_path(value), root, behavior != "allow"))
            except (ValueError, OSError):
                results.append(behavior != "allow")
        return bool(results) and (all(results) if behavior == "allow" else any(results))
    if tool == "hook":
        return (isinstance(name, str) and name.startswith("hook_")
                and (rule.get("hook") is None or arguments.get("hook_id") == rule["hook"]))
    if tool == "web_fetch":
        if name != "web_fetch":
            return False
        from .web_fetch import domain_of
        domain, host = rule.get("domain"), domain_of(arguments.get("url"))
        if not domain:
            return behavior != "allow"
        if host is None:
            return behavior != "allow"  # A URL that cannot be checked never matches an allow rule.
        return host == domain if behavior == "allow" else host == domain or host.endswith("." + domain)
    if tool == "browser":
        if name != "browser":
            return False
        domain, host = rule.get("domain"), browser_host(arguments)
        if not domain:
            return behavior != "allow"
        if host is None:
            return behavior != "allow"  # A page that cannot be named never matches an allow rule.
        if behavior == "allow" or is_loopback_key(domain) or is_loopback_key(host):
            return host == domain
        return host == domain or host.endswith("." + domain)
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
        if (name in WRAPPERS or name in UNCERTAIN_WORDS or interpreter_name(name) or program is None
                or name == program):
            return True
    return False


def default_rule(name, arguments):
    """The rule an 'Always allow' answer stores when the request sends none, or None."""
    from .permissions import tool_kind
    arguments = arguments if isinstance(arguments, dict) else {}
    kind = tool_kind(name)
    if kind == "command":
        argv, _ = parse_command(arguments.get("command"))
        if (argv is None or opaque(argv) or any(names_credential(token) for token in argv)
                or arguments.get("cwd") is not None):
            return None  # A command in another folder is remembered only by its exact text, in this chat.
        return {"argv": argv, "behavior": "allow", "tool": "run_command"}
    if kind == "edit":
        return {"behavior": "allow", "tool": EDIT_KINDS_RULE}
    if kind in ("mcp", "delegate", "job_stop", "memory") or name == "web_search":
        return {"behavior": "allow", "tool": name}
    if name == "web_fetch":
        from .web_fetch import domain_of
        domain = domain_of(arguments.get("url"))
        return {"behavior": "allow", "domain": domain, "tool": "web_fetch"} if domain else None
    if name == "browser":
        host = browser_host(arguments)
        return {"behavior": "allow", "domain": host, "tool": "browser"} if host else None
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
        if not at_project_root(arguments, resolve_path):
            return {"argv": None, "prefixes": [], "reason": "This command runs in another folder than the project."}
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
        # The deepest folder holding every path the tool touches (both ends of a move), so a
        # 'Files under' rule for it matches the card; none when they share only the filesystem root.
        from .permissions import EDIT_TOOL_PATHS
        folder = None
        values = [arguments.get(key) for key in EDIT_TOOL_PATHS.get(name, ()) if arguments.get(key) is not None]
        if values and resolve_path is not None and all(isinstance(value, str) for value in values):
            try:
                common = Path(os.path.commonpath([str(resolve_path(value).parent) for value in values]))
                folder = str(common) if common != Path(common.anchor) else None
            except (ValueError, OSError):
                folder = None
        return {"folder": folder}
    if name == "web_fetch":
        from .web_fetch import domain_of
        return {"domain": domain_of(arguments.get("url"))}
    if name == "browser":
        return {"domain": browser_host(arguments)}
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
        # By name and by file identity, so another name for home or the app folder is not a way around.
        below = parts_below(candidate, home) if home is not None else None
        if below:
            inside = [str(part).casefold() for part in below]
            if inside[0] in HOME_FILES:
                return f"shell and Git startup files (~/{below[0]})"
            if inside[:2] == [".config", "git"]:
                return "Git configuration (~/.config/git)"
        if app_root is not None and parts_below(candidate, app_root) is not None:
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
