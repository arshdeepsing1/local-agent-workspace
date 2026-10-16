"""Vetted read-only commands (M6h): shell text the app parses, checks program by program and runs itself.

In Auto and Plan, a command that is more than one plain argument vector (pipes, &&, ||, ;, line
breaks, a leading `cd <folder> &&`, redirects to /dev/null) runs without an approval card when
every stage is a read-only program from the table below, with each option and operand checked,
or, in Auto, when a stage is exactly what an argv or prefix allow rule allows (phase 2: such a stage
runs in the project folder and is never fed by a pipe, so a pipe cannot hand it code). Anything
else gets a card as before: when in doubt, vet() refuses, with a reason.

The text never reaches a shell as written. vet() returns a Plan; script() builds the shell script
that runs: every word quoted, each read-only program by its absolute path found outside the
project and the chat's folders, Git through git_tools.hardened_git_command (with `env -i` and its
own environment), and options before operands (with `--` when an operand starts with '-'), so GNU
and BSD tools read them alike. A leading `cd DIR` becomes the folder the script starts in; DIR,
every path operand and every glob match must lie in the project, a folder granted to the chat or
the default code folder, and must not look like a credential file (secret_paths). Recursive
searches (grep -r, rg) get exclusions for credential-looking names. Unquoted globs and a leading ~
are expanded by the app (sorted, at most 500 matches, dotfiles only when the pattern starts with a
dot, kept literally when nothing matches, as sh does).

Refused outside quotes: $, backticks, ( ) { } !, a lone &, |&, heredocs and every redirect except
2>&1, 1>&2, >/dev/null, 2>/dev/null and </dev/null, a backslash (except at the end of a line),
Unicode quotes, a first word that is an assignment (NAME=...) or a path, and every program or option
not in the table: never awk's system/getline/ARGV or output redirection, sed's w, e, r or -i,
find's -exec or -delete, sort -o, uniq's output operand, tail -f, rg --pre or -z, git branch <name>.
"""
import glob
import itertools
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path


class Refused(ValueError):
    """The command cannot be checked as read-only; the text says why (shown to the model in Plan)."""


MAX_TEXT = 20_000
MAX_STAGES = 32
MAX_WORDS = 2_000
MAX_GLOB_MATCHES = 500
MAX_SCRIPT = 200_000
OPERATORS = ("|", "&&", "||", ";")
OPERATOR_TEXT = {"|": " | ", "&&": " && ", "||": " || ", ";": "; "}
CONFUSABLE = frozenset("‘’‚‛“”„‟   ​⁠﻿")
SPECIAL = {"$": "$ (expansion)", "`": "backticks", "(": "parentheses", ")": "parentheses", "{": "braces",
           "}": "braces", "!": "!"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
COUNT = re.compile(r"^[+-]?\d{1,15}[A-Za-z]{0,2}$")
GIT_REVISION = re.compile(r"^[A-Za-z0-9_./~^@{}+=-]+$")
GIT_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9_./*?\[\]+=-]+$")
REDIRECT_TARGET = re.compile(r"[A-Za-z0-9/._-]")


@dataclass
class Word:
    text: str = ""
    pattern: str = ""  # text with quoted glob characters escaped, for glob expansion
    quoted: bool = False
    glob: bool = False
    tilde: bool = False


@dataclass(frozen=True)
class Stage:
    """One simple command. argv: the words as the program receives them (globs and ~ expanded), which
    rules are matched against. run: what runs, (absolute program, *arguments) for 'read' and 'rule',
    the Git arguments for 'git'. cwd: the folder it runs in."""
    argv: tuple
    run: tuple
    kind: str
    cwd: str
    redirects: tuple = ()


@dataclass(frozen=True)
class Plan:
    """A vetted command: where it starts, its stages and the operators between them, and the
    (rule, scope) pairs that allowed rule stages (empty when every stage is read-only)."""
    cwd: str
    stages: tuple
    operators: tuple
    rules: tuple = ()

    @property
    def read_only(self):
        return not self.rules


@dataclass
class Context:
    """What vet() needs from the conversation. check_path(absolute text) returns the resolved Path or raises
    Refused (outside the approved folders, excluded or credential-looking); find_program(name) is the absolute
    program outside the project and the chat's folders; allow(argv) is (rule, scope) for an allow rule (None in
    Plan); rule_program(name, cwd) resolves a rule stage's program like a rule-allowed command."""
    cwd: Path
    root: Path
    home: Path
    check_path: object
    find_program: object
    secret: object
    allow: object = None
    rule_program: object = None
    excludes: tuple = ((), ())


# Lexing ---------------------------------------------------------------------------------------------------


def lex(text):
    """Tokens of shell text: ('word', Word), ('op', '|' '&&' '||' ';' or '\\n') and ('redir', normalized text)."""
    if not isinstance(text, str) or not text.strip():
        raise Refused("the command is empty")
    if len(text) > MAX_TEXT:
        raise Refused("the command is too long to check")
    tokens = []
    word = None
    i, n = 0, len(text)

    def end():
        nonlocal word
        if word is not None:
            tokens.append(("word", word))
            word = None

    def current():
        nonlocal word
        if word is None:
            word = Word()
        return word

    while i < n:
        c = text[i]
        if c == "\\":
            if i + 1 < n and text[i + 1] == "\n":
                i += 2
                continue
            raise Refused("it uses a backslash outside quotes")
        if c in " \t":
            end()
            i += 1
            continue
        if c == "\n":
            end()
            tokens.append(("op", "\n"))
            i += 1
            continue
        if c == "#" and word is None:
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "'":
            close = text.find("'", i + 1)
            if close < 0:
                raise Refused("it has unbalanced quotes")
            _quoted(current(), text[i + 1:close])
            i = close + 1
            continue
        if c == '"':
            j, chars = i + 1, []
            while True:
                if j >= n:
                    raise Refused("it has unbalanced quotes")
                ch = text[j]
                if ch == '"':
                    break
                if ch in "$`":
                    raise Refused(f"it uses {SPECIAL[ch]} inside double quotes")
                if ch == "\\" and j + 1 < n:
                    following = text[j + 1]
                    if following in '"\\$`':
                        chars.append(following)
                        j += 2
                        continue
                    if following == "\n":
                        j += 2
                        continue
                chars.append(ch)
                j += 1
            _quoted(current(), "".join(chars))
            i = j + 1
            continue
        if c == "|":
            end()
            if text.startswith("||", i):
                tokens.append(("op", "||"))
                i += 2
            elif text.startswith("|&", i):
                raise Refused("it uses |&")
            else:
                tokens.append(("op", "|"))
                i += 1
            continue
        if c == "&":
            end()
            if text.startswith("&&", i):
                tokens.append(("op", "&&"))
                i += 2
                continue
            raise Refused("it uses & (a background job or a redirect of both outputs)")
        if c == ";":
            end()
            if text.startswith(";;", i):
                raise Refused("it uses ;;")
            tokens.append(("op", ";"))
            i += 1
            continue
        if c in "<>":
            fd = None
            if word is not None and not word.quoted and word.text in ("0", "1", "2"):
                fd, word = word.text, None
            end()
            i, redirect = _redirect(text, i, fd)
            tokens.append(("redir", redirect))
            continue
        if c in SPECIAL:
            raise Refused(f"it uses {SPECIAL[c]}")
        if c in CONFUSABLE:
            raise Refused("it uses a typographic quote or an invisible space")
        if ord(c) < 32 or ord(c) == 127:
            raise Refused("it uses a control character")
        part = current()
        if c == "~" and not part.text and not part.quoted:
            part.tilde = True
        if c in "*?[":
            part.glob = True
        part.text += c
        part.pattern += c
        i += 1
    end()
    return tokens


def _quoted(word, literal):
    if any((ord(c) < 32 and c not in "\t\n") or ord(c) == 127 for c in literal):
        raise Refused("it uses a control character")
    word.quoted = True
    word.text += literal
    word.pattern += glob.escape(literal)


def _redirect(text, i, fd):
    """(next index, normalized redirect) for the redirects vetted commands may use; Refused for the others."""
    n = len(text)
    operator = text[i]
    i += 1
    if i < n and text[i] in ">&|<":
        operator += text[i]
        i += 1
    while i < n and text[i] in " \t":
        i += 1
    start = i
    while i < n and REDIRECT_TARGET.match(text[i]):
        i += 1
    target = text[start:i]
    if i < n and text[i] not in " \t\n;&|<>":
        target = None  # Quotes or other syntax in the target.
    if operator in (">", ">>") and fd in (None, "1", "2") and target == "/dev/null":
        return i, f"{fd or '1'}{operator}/dev/null"
    if operator == ">&" and fd in (None, "1", "2") and target in ("1", "2"):
        return i, f"{fd or '1'}>&{target}"
    if operator == "<" and fd in (None, "0") and target == "/dev/null":
        return i, "</dev/null"
    if operator.startswith("<<"):
        raise Refused("it uses a here-document")
    if operator.startswith("<"):
        raise Refused("it redirects input from a file")
    raise Refused("it redirects output to a file (only 2>&1 and /dev/null are checked)")


def split_stages(tokens):
    """[(words, redirects)] and the operators between them, from lex() tokens."""
    stages, operators = [], []
    words, redirects = [], []
    pending = None  # the operator before the current stage
    for kind, value in tokens:
        if kind == "word":
            words.append(value)
        elif kind == "redir":
            redirects.append(value)
        elif value in (";", "\n"):
            if words or redirects:
                stages.append((words, redirects))
                operators.append(";")
                words, redirects, pending = [], [], ";"
            elif pending in ("|", "&&", "||"):
                if value == ";":
                    raise Refused(f"it has {pending} followed by ;")
            elif value == ";":
                raise Refused("it has an empty command before ;")
        else:
            if not (words or redirects):
                raise Refused(f"it has {value} without a command before it")
            stages.append((words, redirects))
            operators.append(value)
            words, redirects, pending = [], [], value
    if words or redirects:
        stages.append((words, redirects))
    elif operators and operators[-1] != ";":
        raise Refused(f"it ends with {operators[-1]}")
    elif operators:
        operators.pop()
    if not stages:
        raise Refused("the command is empty")
    if len(stages) > MAX_STAGES:
        raise Refused(f"it has more than {MAX_STAGES} commands")
    for words, _ in stages:
        if not words:
            raise Refused("it has a redirect without a command")
    return stages, operators


# Expansion and paths ----------------------------------------------------------------------------------------


def expand(word, cwd, ctx):
    """The words one shell word becomes: ~ replaced by the home folder, globs by their sorted matches."""
    text, pattern = word.text, word.pattern
    if word.tilde:
        if text != "~" and not text.startswith("~/"):
            raise Refused("it uses ~user")
        home = str(ctx.home)
        text, pattern = home + text[1:], glob.escape(home) + pattern[1:]
    if not word.glob:
        return [text]
    matches = list(itertools.islice(glob.iglob(pattern, root_dir=str(cwd)), MAX_GLOB_MATCHES + 1))
    if len(matches) > MAX_GLOB_MATCHES:
        raise Refused(f"a glob matches more than {MAX_GLOB_MATCHES} files")
    if not matches:
        return [text]  # As sh does: a pattern without a match stays as it is.
    matches.sort()
    for match in matches:
        if ctx.secret(match):
            raise Refused(f"a glob matches {match}, which looks like a credential file")
        # Also where no path is expected (echo /srv/*): a glob lists only approved folders.
        ctx.check_path(os.path.join(str(cwd), match))
    return matches


def path_operand(value, cwd, ctx, *, directory=False, file_only=False):
    """A path operand checked (inside the approved folders, not excluded, not credential-looking)."""
    if value in ("-", "/dev/null", "/dev/stdin"):
        return value
    if not value:
        raise Refused("it has an empty path")
    if ctx.secret(value):
        raise Refused(f"{value} looks like a credential file")
    resolved = ctx.check_path(os.path.join(str(cwd), value))
    if directory and not resolved.is_dir():
        raise Refused(f"{value} is not a folder")
    if file_only and resolved.is_dir():
        raise Refused(f"{value} is a folder (only files are compared without -r)")
    return value


# Options ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Spec:
    """A program's options: boolean short letters, short options with a value (letter -> kind), long options
    without, with and with an optional '=' value, and digits: -NUM means that short option (head -20).
    Kinds: 'count', 'text', 'path', 'rev'."""
    flags: str = ""
    values: dict = field(default_factory=dict)
    long: frozenset = frozenset()
    long_values: dict = field(default_factory=dict)
    long_optional: frozenset = frozenset()
    digits: str | None = None


def _value(kind, value, cwd, ctx, program, option):
    if kind == "count" and not COUNT.match(value):
        raise Refused(f"{program} {option} needs a number")
    if kind == "path":
        path_operand(value, cwd, ctx)
    if kind == "rev":
        _revision(value)
    return value


def parse_options(program, words, spec, cwd, ctx):
    """(options as they will run, operands, the option names seen). Options anywhere before '--' count
    (GNU order), and run first, so BSD tools read them the same way. A short option's value runs attached
    (-C2, -n5): some BSD tools (grep -C, git -U) take an optional value only when it is attached, and would
    read a separate one as an operand the app did not check."""
    options, operands, seen = [], [], {}
    i, ended = 0, False
    while i < len(words):
        word = words[i]
        i += 1
        if ended or word == "-" or not word.startswith("-"):
            operands.append(word)
            continue
        if word == "--":
            ended = True
            continue
        if word.startswith("--"):
            name, equals, value = word[2:].partition("=")
            if name in spec.long and not equals:
                options.append(word)
            elif name in spec.long_optional:
                options.append(word)
            elif name in spec.long_values:
                if not equals:
                    if i >= len(words):
                        raise Refused(f"{program} --{name} needs a value")
                    value, i = words[i], i + 1
                _value(spec.long_values[name], value, cwd, ctx, program, f"--{name}")
                options.append(f"--{name}={value}")
            else:
                raise Refused(f"{program} --{name} is not an option the app checks")
            seen.setdefault(name, []).append(value)
            continue
        letters = word[1:]
        if spec.digits and letters.isdigit():
            options.append(f"-{spec.digits}{letters}")
            seen.setdefault(spec.digits, []).append(letters)
            continue
        for index, letter in enumerate(letters):
            if letter in spec.flags:
                options.append(f"-{letter}")
                seen.setdefault(letter, []).append(None)
                continue
            if letter in spec.values:
                value = letters[index + 1:]
                if not value:
                    if i >= len(words):
                        raise Refused(f"{program} -{letter} needs a value")
                    value, i = words[i], i + 1
                _value(spec.values[letter], value, cwd, ctx, program, f"-{letter}")
                options.extend([f"-{letter}{value}"] if value else [f"-{letter}", value])
                seen.setdefault(letter, []).append(value)
                break
            raise Refused(f"{program} -{letter} is not an option the app checks")
    return options, operands, seen


def _with_operands(program, options, operands, *, end=True):
    """The run words: options, then '--' when an operand starts with '-' (and the program takes it), then operands."""
    dashed = any(item.startswith("-") and item != "-" for item in operands)
    if dashed and not end:
        raise Refused(f"{program} has an operand that starts with -")
    return [*options, *(["--"] if dashed else []), *operands]


# Programs ---------------------------------------------------------------------------------------------------


def _set(text):
    return frozenset(text.split())


SPECS = {
    "cat": Spec("AbeEnstTuv", long=_set("show-all number-nonblank show-ends number squeeze-blank show-tabs "
                                        "show-nonprinting")),
    "head": Spec("qvz", {"n": "count", "c": "count"}, _set("quiet silent verbose zero-terminated"),
                 {"lines": "count", "bytes": "count"}, digits="n"),
    "tail": Spec("qvzr", {"n": "count", "c": "count"}, _set("quiet silent verbose zero-terminated"),
                 {"lines": "count", "bytes": "count"}, digits="n"),
    "wc": Spec("clmwL", long=_set("bytes chars lines words max-line-length")),
    "sort": Spec("bdfgiMhnRrsuVcCzm", {"k": "text", "t": "text", "S": "text"},
                 _set("ignore-leading-blanks dictionary-order ignore-case general-numeric-sort ignore-nonprinting "
                      "month-sort human-numeric-sort numeric-sort random-sort reverse stable unique version-sort "
                      "check zero-terminated merge debug"),
                 {"key": "text", "field-separator": "text", "buffer-size": "text", "sort": "text",
                  "parallel": "count"}),
    "uniq": Spec("cdDiuz", {"f": "count", "s": "count", "w": "count"},
                 _set("count repeated unique ignore-case zero-terminated"),
                 {"skip-fields": "count", "skip-chars": "count", "check-chars": "count"}),
    "cut": Spec("snz", {"b": "text", "c": "text", "f": "text", "d": "text"},
                _set("complement only-delimited zero-terminated"),
                {"bytes": "text", "characters": "text", "fields": "text", "delimiter": "text",
                 "output-delimiter": "text"}),
    # -I, -T and -w take a value in GNU ls but none in BSD ls (which would list the next word): left out.
    "ls": Spec("aAbBcCdfFgGhiklmnNopqQrRsStuUvxXZ1", {},
               _set("all almost-all human-readable recursive reverse classify directory inode size "
                    "group-directories-first full-time numeric-uid-gid no-group literal si author escape "
                    "hide-control-chars show-control-chars ignore-backups"),
               {"sort": "text", "time": "text", "time-style": "text", "format": "text", "block-size": "text",
                "ignore": "text", "hide": "text", "width": "count", "indicator-style": "text",
                "quoting-style": "text", "tabsize": "count"}, _set("color colour")),
    "grep": Spec("EFGPiyvwxclLoqsbHhnTZzaIrU", {"e": "text", "f": "path", "m": "count", "A": "count", "B": "count",
                                                "C": "count", "d": "text", "D": "text"},
                 _set("extended-regexp fixed-strings basic-regexp perl-regexp ignore-case no-ignore-case "
                      "invert-match word-regexp line-regexp count files-with-matches files-without-match "
                      "only-matching quiet silent no-messages byte-offset with-filename no-filename line-number "
                      "initial-tab null null-data text recursive line-buffered"),
                 {"regexp": "text", "file": "path", "max-count": "count", "after-context": "count",
                  "before-context": "count", "context": "count", "include": "text", "exclude": "text",
                  "exclude-dir": "text", "label": "text", "binary-files": "text", "directories": "text",
                  "devices": "text"}, _set("color colour"), digits="C"),
    "rg": Spec("iSsvwxclnNHIoqFPUua0b.p", {"e": "text", "f": "path", "g": "text", "t": "text", "T": "text",
                                          "m": "count", "A": "count", "B": "count", "C": "count", "M": "count",
                                          "j": "count", "r": "text", "d": "count", "E": "text"},
               _set("ignore-case smart-case case-sensitive invert-match word-regexp line-regexp count count-matches "
                    "files files-with-matches files-without-match line-number no-line-number with-filename "
                    "no-filename only-matching quiet fixed-strings pcre2 multiline multiline-dotall unrestricted "
                    "text null byte-offset hidden no-hidden no-ignore no-ignore-vcs no-ignore-parent no-ignore-dot "
                    "no-ignore-global heading no-heading column vimgrep json stats trim passthru no-messages "
                    "sort-files one-file-system crlf null-data glob-case-insensitive pretty no-unicode"),
               {"regexp": "text", "file": "path", "glob": "text", "iglob": "text", "type": "text", "type-not": "text",
                "max-count": "count", "after-context": "count", "before-context": "count", "context": "count",
                "max-columns": "count", "threads": "count", "replace": "text", "max-depth": "count",
                "max-filesize": "text", "encoding": "text", "sort": "text", "sortr": "text", "color": "text",
                "colors": "text", "engine": "text", "context-separator": "text", "path-separator": "text"}),
    "tr": Spec("cCdst", long=_set("complement delete squeeze-repeats truncate-set1")),
    "nl": Spec("p", {"b": "text", "d": "text", "f": "text", "h": "text", "i": "count", "l": "count", "n": "text",
                     "s": "text", "v": "count", "w": "count"}),
    "tac": Spec("br", {"s": "text"}, _set("before regex"), {"separator": "text"}),
    "paste": Spec("sz", {"d": "text"}, _set("serial zero-terminated"), {"delimiters": "text"}),
    "comm": Spec("123iz", long=_set("check-order nocheck-order zero-terminated total"),
                 long_values={"output-delimiter": "text"}),
    "cmp": Spec("bls", {"n": "count", "i": "text"}, _set("print-bytes verbose quiet silent"),
                {"bytes": "count", "ignore-initial": "text"}),
    "diff": Spec("qsuciwbBEZaTtpNy", {"U": "count", "C": "count", "W": "count", "I": "text", "F": "text"},
                 _set("brief report-identical-files side-by-side suppress-common-lines ignore-all-space "
                      "ignore-space-change ignore-blank-lines ignore-case text expand-tabs initial-tab minimal "
                      "strip-trailing-cr new-file show-c-function speed-large-files suppress-blank-empty "
                      "left-column normal"),
                 {"label": "text", "ignore-matching-lines": "text", "show-function-line": "text", "width": "count",
                  "tabsize": "count", "horizon-lines": "count"}, _set("unified context color")),
    "du": Spec("achksmxl0bP", {"d": "count", "B": "text", "t": "text"},
               _set("all total human-readable summarize one-file-system apparent-size bytes count-links null si "
                    "inodes"),
               {"max-depth": "count", "block-size": "text", "threshold": "text", "exclude": "text",
                "time-style": "text"}, _set("time")),
    "basename": Spec("az", {"s": "text"}, _set("multiple zero"), {"suffix": "text"}),
    "dirname": Spec("z", long=_set("zero")),
    "pwd": Spec("LP"),
    "date": Spec("uR", {"d": "text"}, _set("utc universal rfc-email"), {"date": "text", "rfc-3339": "text"},
                 _set("iso-8601")),
}
# Operands: 'paths' (checked), 'text' (never read as files), 'none'; and how many.
OPERANDS = {"cat": "paths", "head": "paths", "tail": "paths", "wc": "paths", "sort": "paths", "uniq": "paths",
            "cut": "paths", "ls": "paths", "nl": "paths", "tac": "paths", "paste": "paths", "comm": "files",
            "cmp": "files", "diff": "files", "du": "paths", "tr": "text", "basename": "text", "dirname": "text",
            "pwd": "none", "date": "format"}
MAX_OPERANDS = {"uniq": 1, "comm": 2, "cmp": 2, "diff": 2, "tr": 2, "nl": 1}
EXACT_OPERANDS = {"comm": 2, "diff": 2}
NO_END = frozenset({"tr"})  # Programs whose '--' handling differs between GNU and BSD.
PROGRAMS = frozenset({*SPECS, "grep", "rg", "find", "sed", "awk", "git", "echo", "true", "false", "cd"})


def _generic(program, words, cwd, ctx):
    spec = SPECS[program]
    options, operands, _ = parse_options(program, words, spec, cwd, ctx)
    kind = OPERANDS[program]
    limit = MAX_OPERANDS.get(program)
    if limit is not None and len(operands) > limit:
        raise Refused(f"{program} takes at most {limit} operand{'s' if limit > 1 else ''} here"
                      + (" (a second one is its output file)" if program == "uniq" else ""))
    if program in EXACT_OPERANDS and len(operands) != EXACT_OPERANDS[program]:
        raise Refused(f"{program} needs exactly {EXACT_OPERANDS[program]} files")
    if kind == "none" and operands:
        raise Refused(f"{program} takes no operands")
    if kind == "format" and any(not item.startswith("+") for item in operands):
        raise Refused("date takes only a +FORMAT operand here (other operands set the clock)")
    for item in operands:
        if kind == "paths":
            path_operand(item, cwd, ctx)
        elif kind == "files":
            path_operand(item, cwd, ctx, file_only=True)
    return _with_operands(program, options, operands, end=program not in NO_END)


def _grep(program, words, cwd, ctx):
    options, operands, seen = parse_options(program, words, SPECS[program], cwd, ctx)
    given = any(key in seen for key in ("e", "f", "regexp", "file"))
    if program == "rg" and "files" in seen:
        given = True  # rg --files lists files: no pattern.
    if not given:
        if not operands:
            raise Refused(f"{program} needs a pattern")
        pattern, operands = operands[0], operands[1:]
    for item in operands:
        path_operand(item, cwd, ctx)
    names, folders = ctx.excludes
    if program == "grep":
        recursive = ("r" in seen or "recursive" in seen or "recurse" in seen.get("d", [])
                     or "recurse" in seen.get("directories", []))
        excludes = []
        if recursive:
            if not operands:
                operands = ["."]  # GNU grep -r searches the folder; BSD grep would read stdin.
            excludes = [*(f"--exclude={name}" for name in names), *(f"--exclude-dir={name}" for name in folders)]
        tail = [*options, *excludes]
    else:
        # rg searches folders by default; --no-config: no RIPGREP_CONFIG_PATH file adds options (such as --pre).
        tail = ["--no-config", *options, *(f"--iglob=!{name}" for name in [*names, *folders])]
    rest = ([] if given else [pattern]) + operands
    dashed = any(item.startswith("-") and item != "-" for item in rest)
    return [*tail, *(["--"] if dashed else []), *rest]


FIND_GLOBAL = frozenset({"-P"})
FIND_NO_VALUE = _set("-print -print0 -prune -empty -true -false -depth -xdev -mount -daystart -ls -nouser -nogroup "
                     "-readable -writable -executable -quit -not -a -o -and -or ! ( ) ,")
FIND_VALUE = {**dict.fromkeys(_set("-name -iname -path -ipath -wholename -iwholename -regex -iregex -type -xtype "
                                   "-perm -user -group -size -mtime -mmin -atime -amin -ctime -cmin -regextype "
                                   "-fstype -used -lname -ilname -printf"), "text"),
              **dict.fromkeys(_set("-uid -gid -links -inum -maxdepth -mindepth"), "count"),
              **dict.fromkeys(_set("-samefile -newer -anewer -cnewer"), "path")}


def _find(program, words, cwd, ctx):
    run, i = [], 0
    while i < len(words) and words[i] in FIND_GLOBAL:
        run.append(words[i])
        i += 1
    starts = []
    while i < len(words) and not (words[i].startswith("-") or words[i] in ("!", "(", ")", ",")):
        starts.append(path_operand(words[i], cwd, ctx))
        i += 1
    run.extend(starts or ["."])
    while i < len(words):
        word = words[i]
        i += 1
        if word in FIND_NO_VALUE:
            run.append(word)
            continue
        if word in FIND_VALUE:
            if i >= len(words):
                raise Refused(f"find {word} needs a value")
            value = _value(FIND_VALUE[word], words[i], cwd, ctx, "find", word)
            run.extend([word, value])
            i += 1
            continue
        raise Refused(f"find {word} is not an expression the app checks"
                      + (" (it can run programs or change files)" if word in ("-exec", "-execdir", "-ok", "-okdir",
                                                                              "-delete") else ""))
    return run


SED_SIMPLE = frozenset("pdqQ=lnNPDhHgGxz")
SED_FLAGS = frozenset("gipImM0123456789")


def check_sed(script):
    """Refused unless a sed script only prints, deletes, substitutes or transliterates: commands p d q = l n N P D
    h H g G x z, s/re/text/flags (flags g i I p m M and digits) and y/abc/xyz/, with line, $, /re/ and step
    addresses. Never w, W, r, R, e, a, i, c, labels, braces or the s flags w and e."""
    i, n = 0, len(script)

    def regex(start, delimiter):
        j = start
        while j < n:
            if script[j] == "\\":
                j += 2
                continue
            if script[j] == delimiter:
                return j
            if script[j] == "\n" and delimiter != "\n":
                break
            j += 1
        raise Refused("its sed script has an unterminated expression")

    def address(j):
        if j < n and script[j].isdigit():
            while j < n and (script[j].isdigit() or script[j] == "~"):
                j += 1
            return j, True
        if j < n and script[j] == "$":
            return j + 1, True
        if j < n and script[j] == "/":
            j = regex(j + 1, "/") + 1
            while j < n and script[j] in "IM":
                j += 1
            return j, True
        return j, False

    while True:
        while i < n and script[i] in " \t\n;":
            i += 1
        if i >= n:
            return
        i, found = address(i)
        if found and i < n and script[i] == ",":
            i += 1
            if i < n and script[i] in "+~":
                i += 1
                while i < n and script[i].isdigit():
                    i += 1
            else:
                i, again = address(i)
                if not again:
                    raise Refused("its sed script has an address range without an end")
        while i < n and script[i] in " \t":
            i += 1
        if i < n and script[i] == "!":
            i += 1
            while i < n and script[i] in " \t":
                i += 1
        if i >= n:
            raise Refused("its sed script has an address without a command")
        command = script[i]
        i += 1
        if command in SED_SIMPLE:
            if command in "qQ":
                while i < n and script[i].isdigit():
                    i += 1
        elif command in "sy":
            if i >= n or script[i] in "\\\n ":
                raise Refused(f"its sed {command} command has no delimiter")
            delimiter = script[i]
            i = regex(i + 1, delimiter) + 1
            i = regex(i, delimiter) + 1
            if command == "s":
                while i < n and script[i] in SED_FLAGS:
                    i += 1
                if i < n and script[i] in "we":
                    raise Refused("its sed script writes a file or runs a command (the w or e flag)")
        else:
            raise Refused(f"its sed script uses the {command!r} command, which is not checked as read-only"
                          + (" (it writes files or runs commands)" if command in "wWrRe" else ""))
        while i < n and script[i] in " \t":
            i += 1
        if i < n and script[i] not in ";\n":
            raise Refused("its sed script has text after a command")


SED_SPEC = Spec("nErsuz", {"e": "text"}, _set("quiet silent regexp-extended separate unbuffered null-data posix"),
                {"expression": "text"})


def _sed(program, words, cwd, ctx):
    options, operands, seen = parse_options(program, words, SED_SPEC, cwd, ctx)
    scripts = [*seen.get("e", []), *seen.get("expression", [])]
    run = []
    for item in options:  # --expression=x as -e x, in place: the scripts run in the order given
        run.extend([f"-e{item.split('=', 1)[1]}"] if item.startswith("--expression=") else [item])
    if not scripts:
        if not operands:
            raise Refused("sed needs a script")
        scripts, operands = [operands[0]], operands[1:]
        run.extend(["-e", scripts[0]])
    for script in scripts:
        check_sed(script)
    for item in operands:
        path_operand(item, cwd, ctx)
    dashed = any(item.startswith("-") and item != "-" for item in operands)
    return [*run, *(["--"] if dashed else []), *operands]


AWK_DENIED = frozenset({"system", "getline", "close", "ARGV", "ARGC", "PROCINFO", "SYMTAB", "FUNCTAB",
                        "ENVIRON"})
AWK_KEYWORDS = frozenset({"print", "printf", "return", "if", "while", "for", "do", "else", "in", "delete", "exit",
                          "next", "nextfile", "function", "func", "BEGIN", "END", "case", "default", "switch"})
AWK_OPERATORS = frozenset("(,{};!~=&|?:+-*/%^<>[\n")  # '/' here is division: it follows an operand


def check_awk(source):
    """Refused unless an awk program cannot run commands, read other files or the environment, or write any:
    no system(), getline, close(), ARGV/ARGC, ENVIRON, @-directives or pipes, and no > or >> after print or
    printf (a comparison inside parentheses is fine). A '/' starts a regular expression only after an
    operator or a keyword."""
    i, n = 0, len(source)
    depth = 0
    printing = None  # the parenthesis depth of the print statement being read
    previous = "operator"
    while i < n:
        c = source[i]
        if c in " \t":
            i += 1
            continue
        if c == "\\" and i + 1 < n and source[i + 1] == "\n":
            i += 2
            continue
        if c in "\n;{}":
            printing = None
            previous = "operator"
            i += 1
            continue
        if c == "#":
            while i < n and source[i] != "\n":
                i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n and source[j] != '"':
                j += 2 if source[j] == "\\" else 1
            if j >= n:
                raise Refused("its awk program has an unterminated string")
            i, previous = j + 1, "operand"
            continue
        if c == "/" and previous == "operator":
            j = i + 1
            while j < n and source[j] != "/":
                if source[j] == "\n":
                    raise Refused("its awk program has an unterminated regular expression")
                j += 2 if source[j] == "\\" else 1
            if j >= n:
                raise Refused("its awk program has an unterminated regular expression")
            i, previous = j + 1, "operand"
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (source[j].isalnum() or source[j] == "_"):
                j += 1
            name = source[i:j]
            if name in AWK_DENIED:
                raise Refused(f"its awk program uses {name}, which can run commands or read other files or the "
                              "environment")
            if name in ("print", "printf"):
                printing = depth
            previous = "operator" if name in AWK_KEYWORDS else "operand"
            i = j
            continue
        if c.isdigit() or c == ".":
            j = i
            while j < n and (source[j].isalnum() or source[j] == "."):
                j += 1
            i, previous = j, "operand"
            continue
        if c == "|":
            if source.startswith("||", i):
                i, previous = i + 2, "operator"
                continue
            raise Refused("its awk program uses a pipe, which runs a command")
        if c == ">":
            if printing is not None and depth == printing:
                raise Refused("its awk program redirects print output to a file")
            i, previous = i + 1, "operator"
            continue
        if c in "([":
            depth += 1
            i, previous = i + 1, "operator"
            continue
        if c in ")]":
            depth -= 1
            if depth < 0:
                raise Refused("its awk program has unbalanced parentheses")
            i, previous = i + 1, "operand"
            continue
        if c == "$":
            i, previous = i + 1, "operator"
            continue
        if c == "@":
            raise Refused("its awk program uses an @ directive")
        if c in "+-" and source.startswith(c * 2, i):
            i += 2  # ++ and --: what follows decides; a '/' after them is division.
            previous = "operand"
            continue
        if c in AWK_OPERATORS:
            i, previous = i + 1, "operator"
            continue
        raise Refused(f"its awk program uses {c!r}")
    if depth:
        raise Refused("its awk program has unbalanced parentheses")


AWK_SPEC = Spec("", {"F": "text", "v": "text"})


def _awk(program, words, cwd, ctx):
    options, operands, seen = parse_options(program, words, AWK_SPEC, cwd, ctx)
    for value in seen.get("v", []):
        if not ASSIGNMENT.match(value):
            raise Refused("awk -v needs name=value")
    if not operands:
        raise Refused("awk needs a program")
    source, rest = operands[0], operands[1:]
    check_awk(source)
    for item in rest:
        if not ASSIGNMENT.match(item):  # name=value operands are assignments, not files
            path_operand(item, cwd, ctx)
    return [*options, "--", source, *rest]


def _echo(program, words, cwd, ctx):
    return list(words)  # Every word is text; -n, -e and -E at the start are options.


def _true(program, words, cwd, ctx):
    if words:
        raise Refused(f"{program} takes no operands here")
    return []


GIT_DIFF_FLAGS = ("stat numstat shortstat name-only name-status cached staged ignore-all-space ignore-space-change "
                  "ignore-blank-lines no-color summary minimal patience histogram find-renames find-copies raw check "
                  "exit-code quiet patch no-patch compact-summary")
GIT_SPECS = {
    "status": Spec("sbvz", {}, _set("short branch long verbose show-stash ahead-behind no-ahead-behind renames "
                                   "no-renames ignore-submodules"),
                   {}, _set("porcelain untracked-files ignored find-renames column")),
    "diff": Spec("wbpuRMCz", {"U": "count"}, _set(GIT_DIFF_FLAGS),
                 {"unified": "count", "diff-filter": "text", "relative": "text", "stat-width": "count",
                  "abbrev": "count"}, _set("color word-diff dirstat")),
    "show": Spec("swbpuMCz", {"U": "count"}, _set(GIT_DIFF_FLAGS + " oneline abbrev-commit no-notes"),
                 {"unified": "count", "diff-filter": "text", "abbrev": "count", "date": "text"},
                 _set("color word-diff decorate")),
    "log": Spec("iz", {"n": "count"},
                _set("oneline all branches tags remotes graph no-decorate reverse first-parent no-merges merges stat "
                     "shortstat name-only name-status abbrev-commit topo-order date-order author-date-order "
                     "regexp-ignore-case all-match invert-grep left-right cherry-pick no-walk simplify-by-decoration "
                     "full-history"),
                {"max-count": "count", "skip": "count", "since": "text", "until": "text", "after": "text",
                 "before": "text", "author": "text", "committer": "text", "grep": "text", "date": "text",
                 "abbrev": "count", "min-parents": "count", "max-parents": "count"},
                _set("decorate color"), digits="n"),
    "branch": Spec("arlv", {}, _set("all remotes list verbose show-current no-color omit-empty ignore-case"),
                   {"sort": "text", "merged": "rev", "no-merged": "rev", "contains": "rev", "no-contains": "rev",
                    "points-at": "rev"}, _set("color column abbrev")),
    "rev-parse": Spec("q", {}, _set("show-toplevel abbrev-ref verify quiet is-inside-work-tree is-inside-git-dir "
                                   "is-bare-repository show-prefix show-cdup symbolic symbolic-full-name all "
                                   "branches tags"), {}, _set("short")),
    "ls-files": Spec("cdmoskutvz", {}, _set("cached deleted modified others ignored stage killed unmerged "
                                           "exclude-standard full-name directory no-empty-directory eol"), {}),
}
GIT_REVISION_COMMANDS = {"diff": None, "show": None, "log": None, "rev-parse": None, "branch": "patterns",
                         "status": 0, "ls-files": 0}
GIT_PATHSPECS = frozenset({"status", "diff", "show", "log", "ls-files"})  # 'git <command> ... -- <paths>'



def _revision(value):
    if not value or value.startswith("-") or ":" in value or not GIT_REVISION.match(value):
        raise Refused(f"{value!r} is not a revision the app checks (a revision with ':' names a file's contents)")


def _git(program, words, cwd, ctx):
    """(Git arguments for hardened_git_command, the folder it runs in)."""
    i = 0
    while i < len(words) and words[i].startswith("-"):
        if words[i] == "--no-pager":
            i += 1
        elif words[i] == "-C" and i + 1 < len(words):
            target = os.path.join(str(cwd), words[i + 1])
            if ctx.secret(words[i + 1]):
                raise Refused(f"{words[i + 1]} looks like a credential folder")
            cwd = ctx.check_path(target)
            if not cwd.is_dir():
                raise Refused(f"git -C {words[i + 1]} is not a folder")
            i += 2
        else:
            raise Refused(f"git {words[i]} is not an option the app checks")
    if i >= len(words) or words[i] not in GIT_SPECS:
        name = words[i] if i < len(words) else ""
        raise Refused(f"git {name} is not a read-only Git command the app checks".replace("git  ", "git "))
    sub, rest = words[i], words[i + 1:]
    paths = []
    if "--" in rest:
        if sub not in GIT_PATHSPECS:
            raise Refused(f"git {sub} -- is not checked")
        rest, paths = rest[:rest.index("--")], rest[rest.index("--") + 1:]
        for item in paths:
            if item.startswith(":"):
                raise Refused("git pathspec magic (a path starting with ':') is not checked")
            path_operand(item, cwd, ctx)
    options, operands, seen = parse_options(f"git {sub}", rest, GIT_SPECS[sub], cwd, ctx)
    allowed = GIT_REVISION_COMMANDS[sub]
    if allowed == 0 and operands:
        raise Refused(f"git {sub} takes paths only after -- here")
    if allowed == "patterns" and operands and not ({"l", "list"} & set(seen)):
        raise Refused("git branch with a name creates a branch")
    for item in operands:
        if allowed != "patterns":
            _revision(item)
        elif not GIT_BRANCH_PATTERN.match(item) or item.startswith("-"):
            raise Refused(f"{item!r} is not a branch pattern the app checks")
    return [sub, *options, *operands, *(["--", *paths] if paths else [])], cwd


HANDLERS = {**dict.fromkeys(SPECS, _generic), "grep": _grep, "rg": _grep, "find": _find, "sed": _sed, "awk": _awk,
            "echo": _echo, "true": _true, "false": _true}


# Vetting ----------------------------------------------------------------------------------------------------


def vet(text, ctx):
    """The Plan for shell text whose every stage is read-only (or, with ctx.allow, allowed by a rule), else Refused."""
    stages, operators = split_stages(lex(text))
    cwd = Path(ctx.cwd)
    # A leading 'cd DIR &&' (or ';') is where the script starts: each DIR must be an approved folder.
    while len(stages) > 1 and stages[0][0][0].text == "cd" and not stages[0][0][0].quoted:
        words, redirects = stages[0]
        if redirects or len(words) != 2:
            raise Refused("cd needs exactly one folder here")
        if operators[0] not in ("&&", ";"):
            raise Refused(f"cd followed by {operators[0]} is not checked")
        target = words[1]
        if target.glob:
            raise Refused("cd with a glob is not checked")
        folder = expand(target, cwd, ctx)[0]
        if folder in ("-", "") or folder.startswith("-"):
            raise Refused("cd - is not checked")
        if ctx.secret(folder):
            raise Refused(f"{folder} looks like a credential folder")
        resolved = ctx.check_path(os.path.join(str(cwd), folder))
        if not resolved.is_dir():
            raise Refused(f"cd {folder}: not a folder")
        cwd = resolved
        stages, operators = stages[1:], operators[1:]
    planned, rules, total = [], [], 0
    for index, (words, redirects) in enumerate(stages):
        first = words[0]
        if ASSIGNMENT.match(first.text) and not first.quoted:
            raise Refused("it starts a command with an environment assignment")
        if first.glob or first.tilde or "/" in first.text:
            raise Refused("it runs a program given by a path")
        program = first.text
        if program == "cd":
            raise Refused("cd is checked only at the start, followed by && or ;")
        argv = [program]
        for word in words[1:]:
            argv.extend(expand(word, cwd, ctx))
        total += len(argv)
        if total > MAX_WORDS:
            raise Refused(f"it has more than {MAX_WORDS:,} words")
        for item in argv:
            if "\0" in item:
                raise Refused("it has a NUL character")
            if ctx.secret(item):
                raise Refused(f"{item} looks like a credential file")
        piped = index > 0 and operators[index - 1] == "|"
        refusal = None
        if program in PROGRAMS:
            try:
                planned.append(_stage(program, argv, cwd, ctx, tuple(redirects)))
                continue
            except Refused as exc:
                refusal = exc
        allowed = ctx.allow(argv) if ctx.allow is not None else None
        if allowed is not None and not piped and ctx.rule_program is not None and Path(cwd) == Path(ctx.root):
            program_path = ctx.rule_program(program, str(cwd))
            planned.append(Stage(tuple(argv), (program_path, *argv[1:]), "rule", str(cwd), tuple(redirects)))
            rules.append(allowed)
            continue
        if refusal is not None:
            raise refusal
        if allowed is not None and piped:
            raise Refused(f"{program} is allowed by a rule but reads the output of a pipe")
        if allowed is not None:
            raise Refused(f"{program} is allowed by a rule only in the project folder")
        raise Refused(f"{program} is not a read-only program the app checks")
    return Plan(str(cwd), tuple(planned), tuple(operators), tuple(rules))


def _stage(program, argv, cwd, ctx, redirects):
    if program == "git":
        run, folder = _git(program, argv[1:], cwd, ctx)
        return Stage(tuple(argv), tuple(run), "git", str(folder), redirects)
    run = HANDLERS[program](program, argv[1:], cwd, ctx)
    path = ctx.find_program(program)
    if "=" in path:
        raise Refused(f"{program} resolves to a path with '='")
    return Stage(tuple(argv), (path, *run), "read", str(cwd), redirects)


async def script(plan, *, find_program, git_command):
    """The /bin/sh script for a Plan. git_command(folder, arguments, pathspecs) is (argv, env) from
    git_tools.hardened_git_command; Git runs through `env -i` with exactly that environment."""
    parts = []
    for index, stage in enumerate(plan.stages):
        if stage.kind == "git":
            arguments = list(stage.run)
            paths = arguments[arguments.index("--") + 1:] if "--" in arguments else []
            arguments = arguments[:arguments.index("--")] if "--" in arguments else arguments
            argv, env = await git_command(stage.cwd, arguments, paths)
            words = [find_program("env"), "-i", *(f"{key}={value}" for key, value in sorted(env.items())), argv[0],
                     *(["-C", stage.cwd] if stage.cwd != plan.cwd else []), *argv[1:]]
        else:
            words = list(stage.run)
        text = " ".join(shlex.quote(word) for word in words)
        if stage.redirects:
            text += " " + " ".join(stage.redirects)
        parts.append(text)
        if index < len(plan.operators):
            parts.append(OPERATOR_TEXT[plan.operators[index]])
    text = "".join(parts)
    if len(text) > MAX_SCRIPT:
        raise ValueError("The checked command is too long to run.")
    return text
