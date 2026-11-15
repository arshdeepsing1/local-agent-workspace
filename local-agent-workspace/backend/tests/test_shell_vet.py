"""Vetted read-only commands (shell_vet): the accept and refuse table, scripts, and runs in Auto and Plan."""
import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from local_agent import shell_vet
from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.instructions import load_project_instructions
from local_agent.permissions import evaluate
from local_agent.store import Store

needs_git = pytest.mark.skipif(not shutil.which("git"), reason="git is required")
needs_rg = pytest.mark.skipif(not shutil.which("rg"), reason="ripgrep is required")


def build_project(root):
    project = root / "project"
    for folder in ("src", "notes", "keys", "data", "bin"):
        (project / folder).mkdir(parents=True)
    (project / "README.md").write_text("hello world\nsecond line\n")
    (project / "src" / "a.py").write_text("alpha = 1\n")
    (project / "src" / "b.py").write_text("beta = 2\n")
    (project / ".env").write_text("TOKEN=project-env-secret\n")
    (project / "notes" / "secrets.yaml").write_text("password: x\n")
    (project / "keys" / "credentials.json").write_text("{}\n")
    (project / "data" / "values.txt").write_text("3\n1\n2\n1\n")
    (project / ".ssh").mkdir()
    (project / ".ssh" / "deploy_key").write_text("PRIVATE-KEY-MATERIAL\n")  # a key name the credential list lacks
    (project / ".ssh" / "known_hosts").write_text("host ssh-ed25519 AAAA\n")
    other = root / "other"
    other.mkdir()
    (other / "file.txt").write_text("other file\n")
    (root / "outside.txt").write_text("outside\n")
    return project.resolve(), other.resolve()


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("vet")
    project, other = build_project(root)
    settings = Settings(root / "state")
    settings.values.update(workspace=str(project), env_file="")
    store = Store(settings.state_dir / "vet.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session["allowed_directories"] = [str(other)]
    yield manager, session, project, other, root
    store.db.close()


def plan_of(world, text, cwd=None, allow=None):
    manager, session, *_ = world
    tools = manager.session_tools(session)
    arguments = {"command": text, **({"cwd": cwd} if cwd is not None else {})}
    return manager.vet_command(tools, arguments, allow)


ACCEPTED = [
    ("cat README.md | head -5", ["cat", "head"]),
    ("grep -n hello README.md", ["grep"]),
    ("grep -rn hello src", ["grep"]),
    ("grep -c x README.md src/a.py", ["grep"]),
    ("grep -e -x README.md", ["grep"]),
    ("grep -- -x README.md", ["grep"]),
    ("grep -A2 -B 1 hello README.md", ["grep"]),
    ("grep -inE 'a|b' README.md", ["grep"]),
    ("cd src && ls", ["ls"]),
    ("cd src; ls -la", ["ls"]),
    ("cd src && cd .. && ls", ["ls"]),
    ("ls *.md", ["ls"]),
    ("wc -l src/*.py", ["wc"]),
    ("ls nomatch*", ["ls"]),
    ("head -20 README.md", ["head"]),
    ("head -n5 README.md", ["head"]),
    ("tail -n +2 README.md", ["tail"]),
    ("tail -c 10 README.md", ["tail"]),
    ("sort -rn data/values.txt | uniq -c", ["sort", "uniq"]),
    ("sort -t, -k2,2n data/values.txt", ["sort"]),
    ("cut -d: -f1 README.md", ["cut"]),
    ("find . -name '*.py' -type f", ["find"]),
    ("find src -maxdepth 1 -newer README.md -print", ["find"]),
    ("find . -not -path './keys/*' -name '*.md'", ["find"]),
    ("find", ["find"]),
    ("sed -n '1,5p' README.md", ["sed"]),
    ("sed -E 's/a|b/c/g' README.md", ["sed"]),
    ("sed -n '/start/,/end/p' README.md", ["sed"]),
    ("sed -n '$p' README.md", ["sed"]),
    ("sed -e 1d -e 's#x#y#2' README.md", ["sed"]),
    ("sed 'y/abc/xyz/' README.md", ["sed"]),
    ("sed -n '2,+3p;10q' README.md", ["sed"]),
    ("awk '{print $1}' README.md", ["awk"]),
    ("awk -F: 'NR > 1 && $3 > 100 {print $1}' README.md", ["awk"]),
    ("awk '{ if ($1 > 2) print ($1 > 3) }' README.md", ["awk"]),
    ("awk -v n=2 '$0 ~ /hello/ {c++} END {print c / n}' README.md", ["awk"]),
    ("awk '{s += $1} END {printf \"%d\\n\", s}' x=1 data/values.txt", ["awk"]),
    ("echo done", ["echo"]),
    ("echo 'a b' \"c d\"", ["echo"]),
    ("pwd && ls", ["pwd", "ls"]),
    ("ls 2>/dev/null", ["ls"]),
    ("grep hello README.md 2>&1 | head", ["grep", "head"]),
    ("cat README.md >/dev/null 2>&1", ["cat"]),
    ("ls </dev/null", ["ls"]),
    ("ls\n\nls -a\n", ["ls", "ls"]),
    ("ls \\\n  -la", ["ls"]),
    ("ls # a comment", ["ls"]),
    ("ls src |\n  grep a", ["ls", "grep"]),
    ("ls src && wc -l README.md || true", ["ls", "wc", "true"]),
    ("cat 'README.md' \"src/a.py\"", ["cat"]),
    ("ls\t-la", ["ls"]),
    ("diff README.md src/a.py", ["diff"]),
    ("cmp -s README.md src/a.py", ["cmp"]),
    ("tr a-z A-Z", ["tr"]),
    ("date +%F", ["date"]),
    ("basename src/a.py .py", ["basename"]),
    ("du -sh src", ["du"]),
    ("nl README.md", ["nl"]),
    ("paste -d, src/a.py src/b.py", ["paste"]),
    ("cat /dev/null", ["cat"]),
    ("ls -1 | sort | uniq | wc -l", ["ls", "sort", "uniq", "wc"]),
]


@pytest.mark.parametrize("text, programs", ACCEPTED)
def test_read_only_commands_are_accepted(world, text, programs):
    plan, reason = plan_of(world, text)
    assert plan is not None, reason
    assert [Path(stage.run[0]).name for stage in plan.stages] == programs
    assert plan.read_only and all(stage.kind == "read" for stage in plan.stages)


REFUSED = [
    ("npm test; rm -rf ~", "npm is not a read-only program"),
    ("ls $(pwd)", "$"),
    ("ls `pwd`", "backticks"),
    ("echo \"$HOME\"", "inside double quotes"),
    ("cat README.md > out", "redirects output to a file"),
    ("cat README.md >> out", "redirects output to a file"),
    ("ls 2>err.txt", "redirects output to a file"),
    ("ls >| x", "redirects output"),
    ("ls &>/dev/null", "&"),
    ("ls &", "&"),
    ("(ls)", "parentheses"),
    ("{ ls; }", "braces"),
    ("ls src/{a,b}.py", "braces"),
    ("cat <<EOF\nx\nEOF", "here-document"),
    ("cat < README.md", "redirects input"),
    ("diff <(ls) README.md", "redirects input"),
    ("a=b cat README.md", "environment assignment"),
    ("PATH=. ls", "environment assignment"),
    ("./ls", "program given by a path"),
    ("/bin/ls -R", "program given by a path"),
    ("rg --pre=x foo", "--pre"),
    ("rg -z foo", "-z"),
    ("rg -L foo", "-L"),
    ("git -c core.pager=less status", "git -c"),
    ("git branch new-branch", "creates a branch"),
    ("git diff --output=x", "--output"),
    ("git push", "not a read-only Git command"),
    ("git show HEAD:.env", "':'"),
    ("git log -p", "-p"),
    ("git diff --no-index a b", "--no-index"),
    ("git log --format=%H", "--format"),
    ("git diff -- ':!x'", "pathspec magic"),
    ("git -C ../.. status", "outside"),
    ("find . -delete", "can run programs or change files"),
    ("find . -exec cat '{}' ';'", "can run programs or change files"),
    ("find -L .", "-L"),
    ("find . -fprint out", "-fprint"),
    ("sort -o out data/values.txt", "sort -o"),
    ("sort --compress-program=gzip data/values.txt", "--compress-program"),
    ("sort -T /tmp data/values.txt", "sort -T"),
    ("uniq data/values.txt out", "output file"),
    ("tail -f README.md", "tail -f"),
    ("tail --follow README.md", "--follow"),
    ("sed -i 's/a/b/' README.md", "sed -i"),
    ("sed 'w out' README.md", "writes files"),
    ("sed '1e date' README.md", "'e'"),
    ("sed 's/a/b/w out' README.md", "the w or e flag"),
    ("sed 's/a/b/e' README.md", "the w or e flag"),
    ("sed -n '/x/{p}' README.md", "'{'"),
    ("sed -f script.sed README.md", "sed -f"),
    ("awk '{system(\"id\")}' README.md", "system"),
    ("awk '{print | \"sh\"}' README.md", "pipe"),
    ("awk '{print > \"out\"}' README.md", "redirects print output"),
    ("awk '{printf(\"%s\", $1) >> \"out\"}' README.md", "redirects print output"),
    ("awk 'BEGIN { while ((getline line < \"/etc/hosts\") > 0) print line }'", "getline"),
    ("awk 'BEGIN { ARGV[1] = \"/etc/hosts\"; ARGC = 2 } { print }'", "ARGV"),
    ("awk '@include \"x\"'", "@"),
    ("awk -f prog.awk README.md", "awk -f"),
    ("awk 'BEGIN { for (k in ENVIRON) print k, ENVIRON[k] }'", "ENVIRON"),
    ("cat ../outside.txt", "outside the project"),
    ("cd .. && cat outside.txt", "outside the project"),
    ("cat ~/.ssh/id_rsa", "credential"),
    ("cat .env", "credential"),
    ("cat .envrc", "credential"),
    ("cat notes/secrets.yaml", "credential"),
    ("ls keys/*", "credential"),
    ("grep -f .env README.md", "credential"),
    ("xargs cat", "xargs is not a read-only program"),
    ("cat README.md | tee out", "tee is not a read-only program"),
    ("env ls", "env is not a read-only program"),
    ("python -c 'print(1)'", "python is not a read-only program"),
    ("curl https://example.com", "curl is not a read-only program"),
    ("egrep x README.md", "egrep is not a read-only program"),
    ("ls |& cat", "|&"),
    ("ls ;; ls", ";;"),
    ("| ls", "without a command before it"),
    ("ls |", "ends with |"),
    ("ls ; ; ls", "empty command before ;"),
    ("ls && ; ls", "followed by ;"),
    ("ls “README.md”", "typographic quote"),
    ("ls -la", "invisible space"),
    ("ls \x01", "control character"),
    ("echo 'unbalanced", "unbalanced quotes"),
    ("cd src | ls", "cd followed by |"),
    ("ls && cd src", "cd is checked only at the start"),
    ("cd src", "cd is checked only at the start"),
    ("cd - && ls", "cd -"),
    ("cd src notes && ls", "cd needs exactly one folder"),
    ("cd src/a.py && ls", "not a folder"),
    ("ls ~root", "~user"),
    ("echo a\\b", "backslash outside quotes"),
    ("! ls", "!"),
    ("[[ -f x ]]", "program given by a path"),
    ("make test", "make is not a read-only program"),
    ("date -s now", "date -s"),
    ("date 0101", "+FORMAT"),
    ("diff -r src notes", "diff -r"),
    ("diff src notes", "is a folder"),
    ("grep -R x .", "grep -R"),
    ("head -n many README.md", "needs a number"),
    ("comm README.md", "exactly 2 files"),
    ("tr -d -- -x", "starts with -"),
    ("cat /etc/hostname", "outside the project"),
    ("ls -l | less", "less is not a read-only program"),
    ("cat README.md | sh", "sh is not a read-only program"),
    ("", "empty"),
    ("   \n  ", "empty"),
]


@pytest.mark.parametrize("text, reason", REFUSED)
def test_anything_else_is_refused_with_a_reason(world, text, reason):
    plan, why = plan_of(world, text)
    assert plan is None, text
    assert reason in why, (text, why)


def test_the_table_is_large():
    assert len(ACCEPTED) + len(REFUSED) >= 80


AWK_HIDDEN = [
    # A '/' after ')' may start a regex: its quote must not open a string that hides what follows.
    'awk \'BEGIN { if (1) /"/; print "x" > "out.txt"; if (1) /"/ }\'',
    'awk \'BEGIN { if (1) /"/; system("echo ran > ran.txt"); if (1) /"/ }\'',
    'awk \'BEGIN { if (1) /"/; print "x" | "cat > piped.txt"; if (1) /"/ }\'',
    'awk \'BEGIN { if (1) /"/; getline l < "README.md"; if (1) /"/ }\'',
    # A print statement goes on after a newline that follows ',', '&&', '||' (and in gawk '?' and ':').
    'awk \'BEGIN { print "x",\n "y" > "pwned" }\'',
    'awk \'BEGIN { print "x" &&\n "y" > "pwned" }\'',
    'awk \'BEGIN { print "x" ||\n "y" > "pwned" }\'',
    'awk \'BEGIN { printf "%s",\n "y" > "pwned" }\'',
    'awk \'BEGIN { print "x", # note\n "y" >> "pwned" }\'',
    # H1: awk allows any number of newlines and comment lines there.
    'awk \'BEGIN { print "x",\n\n "y" > "pwned" }\'',
    'awk \'BEGIN { print "x",\n# note\n "y" > "pwned" }\'',
    'awk \'BEGIN { print "x",\n \t\n "y" > "pwned" }\'',
    'awk \'BEGIN { print "x" &&\n\n "y" > "pwned" }\'',
    'awk \'BEGIN { print "x" ||\n\n\n "y" > "pwned" }\'',
    'awk \'BEGIN { printf "%s",\n\n "y" >> "pwned" }\'',
    'awk \'BEGIN { print "x" &&\n# one\n\n# two\n "y" > "pwned" }\'',
    'awk \'BEGIN { print 1 ?\n\n 2 :\n 3 > "pwned" }\'',
    # H2: a number glued to a name is a number and then the name (1system(...) calls system).
    'awk \'BEGIN { x = 1system("touch pwned") }\'',
    'awk \'BEGIN { x = .5system("touch pwned") }\'',
    'awk \'BEGIN { x = 1e3system("touch pwned") }\'',
    'awk \'BEGIN { print 1system("touch pwned") }\'',
    'awk \'BEGIN { x = 1getline line < "README.md"; print line }\'',
    'awk \'BEGIN { x = 1ENVIRON["HOME"]; print x }\'',
    "awk '{ print 1ARGV[1] }' README.md",
    'awk \'BEGIN { x = 1e+system("touch pwned") }\'',
    'awk \'BEGIN { x = 0x1gsystem("touch pwned") }\'',
    'awk \'BEGIN { x = 0x1system("touch pwned") }\'',
    'awk \'BEGIN { x = 0x1close("pwned") }\'',
    'awk \'BEGIN { x = 1close("cat") }\'',
    'awk \'BEGIN { x = 1fflush() }\'',
    'awk \'BEGIN { 1print "x" > "pwned" }\'',
    'awk \'BEGIN { 1printf "x" > "pwned" }\'',
    'awk \'BEGIN { x = "a"system("touch pwned") }\'',
    'awk \'BEGIN { x = 1 system("touch pwned") }\'',
    # H3: case, default and switch are plain variables in one-true-awk (macOS), so '/' after them divides.
    'awk \'BEGIN { x = default /"1/ 1 "; system("touch pwned") # " \n}\'',
    'awk \'BEGIN { x = case /"1/ 1 "; print "x" > "pwned" # " \n}\'',
    'awk \'BEGIN { x = switch /"1/ 1 "; while (("cat README.md" | getline l) > 0) print l # " \n}\' README.md',
    'awk \'BEGIN { x = func /"1/ 1 "; system("touch pwned") # " \n}\'',
    'awk \'BEGIN { x = nextfile /"1/ 1 "; system("touch pwned") # " \n}\'',
    'awk \'BEGIN { x = BEGINFILE /"1/ 1 "; system("touch pwned") # " \n}\'',
    'awk \'BEGIN { x = ENDFILE /"1/ 1 "; system("touch pwned") # " \n}\'',
    'awk \'BEGIN { x = delete /"1/ 1 "; system("touch pwned") # " \n}\'',
    'awk \'BEGIN { x = Default /"1/ 1 "; system("touch pwned") # " \n}\'',
    'awk \'BEGIN { x = DEFAULT /"1/ 1 "; system("touch pwned") # " \n}\'',
    'awk \'BEGIN { x = PRINT /"1/ 1 "; system("touch pwned") # " \n}\'',
    "awk '{ x = length /\"1/ 1 \"; system(\"touch pwned\") # \" \\n}' README.md",
    'awk \'BEGIN { x = in /"1/ 1 "; system("touch pwned") # " \n}\'',
    'awk \'BEGIN { x = do /"1/ 1 "; system("touch pwned") # " \n}\'',
    # Names in another letter case are other names in awk, but a check that guesses is a check that fails.
    'awk \'BEGIN { SYSTEM("touch pwned") }\'',
    'awk \'BEGIN { System("touch pwned") }\'',
    'awk \'BEGIN { Getline x < "README.md" }\'',
    # A regex after else, ';', '{', '!', '~', ',' and a quote inside it: lexed as a regex, then what follows is seen.
    'awk \'BEGIN { if (0) x = 1; else /"/; system("touch pwned"); /"/ }\'',
    'awk \'BEGIN { x = 1; /"/; system("touch pwned"); /"/ }\'',
    "awk '{ /\"/; print \"x\" > \"pwned\"; /\"/ }' README.md",
    "awk '!/\"/ { system(\"touch pwned\") } /\"/' README.md",
    "awk '$0 ~ /\"/ { system(\"touch pwned\") } $0 ~ /\"/' README.md",
    'awk \'BEGIN { print 1, /"/; print "x" > "pwned"; print 2, /"/ }\'',
    'awk \'BEGIN { do /"/; system("touch pwned"); while (0) }\'',
    'awk \'BEGIN { for (k in /"/) print k; system("touch pwned"); x = /"/ }\'',
    # A comment with a quote before a redirect, tabs, carriage returns and a continuation inside a print.
    'awk \'BEGIN { x = 1 # say "hi\n print "x" > "pwned" }\'',
    'awk \'BEGIN {\tprint\t"x"\t>\t"pwned" }\'',
    'awk \'BEGIN { print "x",\r\n "y" > "pwned" }\'',
    # A bracket expression with '/' ends the regex in some awks and not in others.
    "awk '$0 ~ /[/]/ { print 1 }' README.md",
    "awk '$0 ~ /[[:alpha:]/]/ { print 1 }' README.md",
    "awk '$0 ~ /[\\]/]/ { print 1 }' README.md",
    # A comment that ends in a backslash may go on in some awks.
    "awk 'BEGIN { x = 1 # note \\\n print \"x\" > \"pwned\" }'",
]

# These already passed before the first fix round: guards, not regressions.
AWK_ALWAYS_REFUSED = [
    'awk \'BEGIN { print "x" \\\n "y" > "pwned" }\'',
    'awk \'BEGIN { print "x", \\\n "y" > "pwned" }\'',
    "awk 'BEGIN { print \"x\" | \"cat > piped.txt\" }'",
    "awk 'BEGIN { print \"x\" > \"/dev/stderr\" }'",
]
AWK_HIDDEN += AWK_ALWAYS_REFUSED

AWK = "/usr/bin/awk"

# Programs the macOS awk (one-true-awk) does NOT run as the hostile program: it reports a syntax error, or reads the
# program differently from gawk or mawk. The decision "not vetted" protects against another implementation, so
# nothing is asserted about the local effect of these. Every other entry of AWK_HIDDEN really creates a file here.
AWK_OTHER_AWK = frozenset({
    "awk '$0 ~ /[/]/ { print 1 }' README.md",
    "awk '$0 ~ /[[:alpha:]/]/ { print 1 }' README.md",
    "awk '$0 ~ /[\\]/]/ { print 1 }' README.md",
    'awk \'BEGIN { x = 1fflush() }\'',
    "awk '{ print 1ARGV[1] }' README.md",
    'awk \'$0 ~ /"/ { system("touch pwned") } $0 ~ /"/\' README.md',
    'awk \'BEGIN { 1print "x" > "pwned" }\'',
    'awk \'BEGIN { 1printf "x" > "pwned" }\'',
    'awk \'BEGIN { Getline x < "README.md" }\'',
    'awk \'BEGIN { SYSTEM("touch pwned") }\'',
    'awk \'BEGIN { System("touch pwned") }\'',
    'awk \'BEGIN { do /"/; system("touch pwned"); while (0) }\'',
    'awk \'BEGIN { for (k in /"/) print k; system("touch pwned"); x = /"/ }\'',
    'awk \'BEGIN { if (1) /"/; getline l < "README.md"; if (1) /"/ }\'',
    'awk \'BEGIN { print "x" > "/dev/stderr" }\'',
    'awk \'BEGIN { print 1 ?\n\n 2 :\n 3 > "pwned" }\'',
    'awk \'BEGIN { x = 0x1close("pwned") }\'',
    'awk \'BEGIN { x = 0x1gsystem("touch pwned") }\'',
    'awk \'BEGIN { x = 0x1system("touch pwned") }\'',
    'awk \'BEGIN { x = 1ENVIRON["HOME"]; print x }\'',
    'awk \'BEGIN { x = 1close("cat") }\'',
    'awk \'BEGIN { x = 1getline line < "README.md"; print line }\'',
    'awk \'BEGIN { x = delete /"1/ 1 "; system("touch pwned") # " \n}\'',
    'awk \'BEGIN { x = do /"1/ 1 "; system("touch pwned") # " \n}\'',
    'awk \'BEGIN { x = func /"1/ 1 "; system("touch pwned") # " \n}\'',
    'awk \'BEGIN { x = in /"1/ 1 "; system("touch pwned") # " \n}\'',
    'awk \'BEGIN { x = nextfile /"1/ 1 "; system("touch pwned") # " \n}\'',
    'awk \'BEGIN { x = switch /"1/ 1 "; while (("cat README.md" | getline l) > 0) print l # " \n}\' README.md',
    "awk '{ x = length /\"1/ 1 \"; system(\"touch pwned\") # \" \\n}' README.md",
})

# A1: a '/' after '++' or '--' is division in gawk and a regex in mawk (its scanner sends a '/' that follows an
# increment or decrement token to the regex collector). Under mawk each of these runs the code the vetter would
# read as a string. macOS awk reports a syntax error and gawk divides, so only the decision is asserted.
MAWK_SLASH = [
    'awk \'BEGIN { c = "touch pwned"; x++ /"a/ ; system(c) ; x++ /"a/ }\'',
    "awk '{ f = \"pwned\"; n-- /\"a/ ; print $0 > f ; n-- /\"a/ }' README.md",
    'awk \'BEGIN { c = "touch pwned"; x++ /="a/ ; system(c) ; x++ /="a/ }\'',
    # Postfix on every kind of operand.
    'awk \'BEGIN { c = "touch pwned"; x-- /"a/ ; system(c) ; x-- /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x-- /="a/ ; system(c) ; x-- /="a/ }\'',
    'awk \'{ c = "touch pwned"; $1++ /"a/ ; system(c) ; $1++ /"a/ }\' README.md',
    'awk \'{ c = "touch pwned"; $NF-- /"a/ ; system(c) ; $NF-- /"a/ }\' README.md',
    'awk \'BEGIN { c = "touch pwned"; a[1]++ /"a/ ; system(c) ; a[1]++ /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; a["k"]-- /"a/ ; system(c) ; a["k"]-- /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x++\t/"a/ ; system(c) ; x++\t/"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x++/"a/ ; system(c) ; x++/"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; (x)++ /"a/ ; system(c) ; (x)++ /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x = y++ /"a/ ; system(c) ; x = y++ /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; print x++ /"a/ ; system(c) ; print x++ /"a/ }\'',
    "awk '{ x++ / 2 }' README.md",
    "awk '{ print x++ / 2 }' README.md",
    "awk '{ print n-- / 2 }' README.md",
]

# A1 audit: every other token after which gawk, mawk and one-true-awk could disagree about '/'.
AWK_SLASH_AMBIGUOUS = [
    # A regex literal directly followed by '/': division in some scanners, a regex start in others.
    'awk \'BEGIN { c = "touch pwned"; x = /a/ /"b/ ; system(c) ; y = /a/ /"b/ }\'',
    'awk \'{ c = "touch pwned"; if ($0 ~ /a/ /"b/ ) system(c) ; if ($0 ~ /a/ /"b/ ) x }\' README.md',
    # '>' is a redirect after print and a comparison elsewhere.
    'awk \'BEGIN { c = "touch pwned"; x = (1 > /"a/ ; system(c) ; y = (1 > /"a/) }\'',
    'awk \'BEGIN { c = "touch pwned"; x = (1 >/"a/ ; system(c) ; y = (1 >/"a/) }\'',
    # '[' and '$' take an expression, but the scanners differ on what a '/' after them is.
    'awk \'BEGIN { c = "touch pwned"; a[/"x/ ; system(c) ; a[/"x/] = 1 }\'',
    'awk \'{ c = "touch pwned"; x = $/"x/ ; system(c) ; y = $/"x/ }\' README.md',
    # A '/' after a division operator.
    'awk \'BEGIN { c = "touch pwned"; x = 4 / /"a/ ; system(c) ; y = 4 / /"a/ }\'',
    # A name that is a keyword or a builtin function in some awk and a plain variable in another.
    'awk \'BEGIN { c = "touch pwned"; x = toupper /"a/ ; system(c) ; y = toupper /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x = tolower /"a/ ; system(c) ; y = tolower /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x = substr /"a/ ; system(c) ; y = substr /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x = sprintf /"a/ ; system(c) ; y = sprintf /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x = index /"a/ ; system(c) ; y = index /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x = split /"a/ ; system(c) ; y = split /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x = match /"a/ ; system(c) ; y = match /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x = int /"a/ ; system(c) ; y = int /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x = exp /"a/ ; system(c) ; y = exp /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x = srand /"a/ ; system(c) ; y = srand /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x = and /"a/ ; system(c) ; y = and /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x = strftime /"a/ ; system(c) ; y = strftime /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x = systime /"a/ ; system(c) ; y = systime /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x = gensub /"a/ ; system(c) ; y = gensub /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x = BEGIN /"a/ ; system(c) ; y = BEGIN /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x = END /"a/ ; system(c) ; y = END /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x = exit /"a/ ; system(c) ; y = exit /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x = next /"a/ ; system(c) ; y = next /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x = break /"a/ ; system(c) ; y = break /"a/ }\'',
    'awk \'BEGIN { c = "touch pwned"; x = continue /"a/ ; system(c) ; y = continue /"a/ }\'',
]


def effects_of(text, folder):
    """The files the raw awk command text creates when it runs for real with /usr/bin/awk in folder."""
    (folder / "README.md").write_text("hello world\nsecond line\n")
    before = set(os.listdir(folder))
    words = shlex.split(text)
    assert words[0] == "awk"
    subprocess.run([AWK, *words[1:]], cwd=folder, stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    return sorted(set(os.listdir(folder)) - before)


def awk_cases(texts, implementation):
    return [pytest.param(text, id=f"{implementation}:{index}:{text[:40]!r}") for index, text in enumerate(texts)]


HIDDEN_CASES = [
    *awk_cases([text for text in AWK_HIDDEN if text not in AWK_OTHER_AWK], "macos-awk"),
    *awk_cases([text for text in AWK_HIDDEN if text in AWK_OTHER_AWK], "gawk-or-mawk"),
    *awk_cases(MAWK_SLASH, "mawk"),
    *awk_cases(AWK_SLASH_AMBIGUOUS, "mawk-gawk-or-onetrue"),
]


@pytest.mark.parametrize("text", HIDDEN_CASES)
def test_awk_that_could_hide_a_write_or_a_program_is_not_vetted(world, text):
    plan, reason = plan_of(world, text)
    assert plan is None, text
    assert reason, text


@pytest.mark.skipif(sys.platform != "darwin" or not os.path.exists(AWK),
                    reason="these cases document the macOS /usr/bin/awk")
@pytest.mark.parametrize("text", awk_cases([text for text in AWK_HIDDEN if text not in AWK_OTHER_AWK], "macos-awk"))
def test_the_refused_awk_really_creates_a_file_with_the_local_awk(tmp_path, text):
    # Independent of the decision: the raw command, run in a scratch folder, does what the case documents.
    assert effects_of(text, tmp_path), (text, "created no file here: move it to AWK_OTHER_AWK")


def test_the_awk_cases_that_run_elsewhere_are_known_ones():
    assert AWK_OTHER_AWK <= set(AWK_HIDDEN)
    assert len(MAWK_SLASH) >= 3 and len(AWK_SLASH_AMBIGUOUS) >= 15


@pytest.mark.skipif(not os.path.exists(AWK), reason="/usr/bin/awk is required")
def test_the_local_awk_does_not_run_the_mawk_slash_cases(tmp_path):
    # One-true-awk reports a syntax error for '/' after '++': only mawk executes the hidden code.
    assert effects_of(MAWK_SLASH[0], tmp_path) == []


ORDINARY_AWK = [
    "awk '{ print $1 }' README.md",
    "awk -F, '$2 ~ /x/ { n++ } END { print n }' README.md",
    "awk -F: '{ print $1 }' README.md",
    "awk 'BEGIN { x = 1 }\n{ print $1 }\n{ print $2,\n $1 }\nEND { print NR }' README.md",
    "awk '{ print $1 }\n{ if ($1 > 2) print 1 }' README.md",
    # M2: '/' after ']' and after any ')' but a if/while/for header's divides.
    "awk '{c[$1]++} END {for (k in c) print k, c[k]/NR}' README.md",
    "awk '{print length($0)/2}' README.md",
    "awk '{print ($1+$2)/2}' README.md",
    "awk '{a[NR]=$1} END {print a[1]/a[2]}' README.md",
    "awk 'BEGIN { printf \"%d\\n\", (3+4) / 2 }'",
    "awk '{ print substr($0,1,1) / 2 }' README.md",
    "awk 'BEGIN { a[1] = 4; print a[1]/2 }'",
    "awk '{ print $1 /2/ 3 }' README.md",
    "awk '{ s += $1 } END { print s / NR }' README.md",
    "awk '$2 ~ /x/ { n++ } END { print n }' README.md",
    "awk 'NR % 2 == 0' README.md",
    "awk '{ if ($1 > 5) print $0 }' README.md",
    "awk '{ for (i = 1; i <= NF; i++) n++ } END { print n/2 }' README.md",
    "awk 'BEGIN { x = 0x1f; y = 1e3; z = .5; print x, y, z }'",
    "awk '/start/,/end/' README.md",
    "awk '!seen[$0]++' README.md",
    "awk '{ if ($1 ~ /x/) print $1; else if ($2 ~ /y\\/z/) print $2 }' README.md",
    "awk '/[0-9]+/ { n++ } /[^a-z]x/ { m++ } END { print n, m }' README.md",
    "awk '{ if (NR > 1) /x/ }' README.md",
    "awk '{ while (i < 3) /a/; i++ }' README.md",
    "awk '# header\nBEGIN { n = 0 } # start\n\n# count\n{ n += NF }  # fields\nEND {\n  # total\n  print n\n}' README.md",
    "awk '{ print $1,\n\n  $2 }' README.md",
    "awk '{ print $1 &&\n\n  $2 }' README.md",
    "awk 'BEGIN { print (1 > 2) }'",
    # A1: '/' after a NAME, a number, a string, ']', ')' and a field divides in every awk, and a regex starts
    # after the operators and separators in AWK_REGEX_AFTER_CHARS: all three awk implementations agree.
    "awk '{ n++ } END { print n/2 }' README.md",
    "awk '{ print $1/$2 }' README.md",
    "awk '{ print NR/2, NF/3 }' README.md",
    "awk '{ s += $1 } END { print s/NR }' README.md",
    "awk '{ a[$1]++ } END { for (k in a) print k, a[k]/NR }' README.md",
    "awk 'NR > 1 && $3 ~ /x/' README.md",
    "awk '!/^#/' README.md",
    "awk '/a/ || /b/' README.md",
    "awk '{ if ($1 ~ /^[0-9]+$/) n++ } END { print n }' README.md",
    "awk '$1 == \"x\" { print $2 / 2 }' README.md",
    "awk '{ x = $1 * 2 / 3; print x }' README.md",
    "awk '{ print ($1 > 5) ? \"big\" : \"small\" }' README.md",
    "awk '/a/ { n++ } /b/ { m++ } END { print n, m }' README.md",
    "awk '{ x = 6 / 3 / 2; print x }' README.md",
    "awk '{ x = \"8\" / 2; print x }' README.md",
    "awk '$1 != /x/ { print }' README.md",
    "awk '{ x = (y < /a/) + (y <= /b/) + (y >= /c/); print x }' README.md",
    "awk '{ x = $1 ? /a/ : /b/; print x }' README.md",
    "awk '{ x = 1 + /a/ - /b/ * /c/ % /d/ ^ /e/; print x }' README.md",
    "awk '{ print ++n / 2 }' README.md",
    "awk '{ n++; print n / 2 }' README.md",
    "awk '{ n++\n print n / 2 }' README.md",
    "awk '{ n++ ; /x/ }' README.md",
    "awk '{ n++ } /x/' README.md",
    "awk '{ n += $1 / 2; m -= $2 / 3; k *= 2 / 1 }' README.md",
    "awk 'BEGIN { x = 1; x /= 2; print x }'",
]


@pytest.mark.parametrize("text", ORDINARY_AWK)
def test_ordinary_awk_programs_are_still_vetted(world, text):
    plan, reason = plan_of(world, text)
    assert plan is not None, reason


def test_a_slash_right_after_an_increment_asks_and_a_slash_after_a_name_does_not(world):
    assert plan_of(world, "awk '{ n++ } END { print n/2 }' README.md")[0] is not None
    assert plan_of(world, "awk '{ print n++/2 }' README.md")[0] is None
    assert plan_of(world, "awk '{ x++ / 2 }' README.md")[0] is None
    assert plan_of(world, "awk '{ print ++n/2 }' README.md")[0] is not None  # '/' follows the name n


def test_there_are_enough_ordinary_awk_programs():
    assert len(ORDINARY_AWK) >= 30


@pytest.mark.parametrize("text", awk_cases([*MAWK_SLASH, *AWK_SLASH_AMBIGUOUS], "slash"))
def test_an_ambiguous_slash_is_why_the_program_is_not_vetted(world, text):
    # Not refused because of some later word (system, a redirect): the '/' itself is where the lexer stops.
    plan, reason = plan_of(world, text)
    assert plan is None and "'/'" in reason, (text, reason)


@pytest.fixture
def credential_folders(world):
    """Ordinary and nested folders under the names of the five credential folders, removed after the test."""
    project = world[2]
    made = [".aws", ".docker", ".kube", "sub", "link", ".ssh/keys.pub"]
    for path in (".aws/cli/cache", ".docker", ".kube", "sub/.ssh", ".ssh/keys.pub", ".ssh/sub", ".aws/sso/cache"):
        (project / path).mkdir(parents=True, exist_ok=True)
    (project / ".aws/cli/cache/abc.json").write_text("{}\n")
    (project / ".aws/sso/cache/def.json").write_text("{}\n")
    (project / ".aws/plain.txt").write_text("x\n")
    (project / ".docker/compose.yml").write_text("a: 1\n")
    (project / ".kube/notes.txt").write_text("x\n")
    (project / "sub/.ssh/known_hosts").write_text("x\n")
    (project / ".ssh/keys.pub/deploy").write_text("x\n")
    (project / "link").symlink_to(project / ".ssh")
    yield project
    for name in made:
        target = project / name
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)


@pytest.mark.parametrize("text", [
    "grep -r PRIVATE-KEY-MATERIAL .ssh/",
    "grep -r PRIVATE-KEY-MATERIAL .ssh",
    "grep -r x .SSH/",
    "grep -r x ./.ssh",
    "rg x .ssh",
    "find .ssh -type f",
    "ls -R .ssh",
    "cd .ssh && grep -r PRIVATE-KEY-MATERIAL",
    "cd .ssh/ && ls",
    "cd .SSH && ls",
    "cat .ssh/deploy_key",
    "cd .ssh && cat deploy_key",
    # M3: a folder or a file deeper in a credential folder (token caches) asks, as 'cd' into it does.
    "grep -r PRIVATE .aws/cli",
    "cat .aws/cli/cache/abc.json",
    "cat .aws/sso/cache/def.json",
    "grep -r x .aws/sso",
    "grep -r PRIVATE .ssh/keys.pub",
    "cat .ssh/keys.pub/deploy",
    "cat .ssh/sub/../keys.pub/deploy",
    "ls .ssh/keys.pub",
    "ls .aws/cli",
    "head -1 .aws/cli/cache/abc.json",
    "wc -l .aws/cli/cache/abc.json",
    "cd .aws/cli && cat cache/abc.json",
    "cd .aws/cli && ls",
    # Recursion and contents in the five folders.
    "ls -R .docker",
    "ls -R .kube",
    "ls --recursive .aws",
    "grep -r x .kube",
    "rg x .docker",
    "find .docker -type f",
    "find .aws",
    "du .aws",
    "du -a .aws",
    "du -sa .aws",
    "grep -r x link",
    "ls -R link",
    "cat link/deploy_key",
    "cd .docker && ls",
    "cd .kube/ && ls",
    # L1: git -C into a credential folder, by name, by a nested name and by a link.
    "git -C .ssh status",
    "git -C .ssh ls-files",
    "git -C sub/.ssh diff",
    "git -C link status",
    "git -C .AWS log",
    "git -C .aws/cli status",
])
def test_a_credential_folder_is_not_searched_read_or_entered_without_a_card(world, credential_folders, text):
    plan, reason = plan_of(world, text)
    assert plan is None, text
    assert "credential" in reason, (text, reason)


@pytest.mark.parametrize("text", [
    # L2: the names in the top level of a credential folder, as before the first fix round.
    "ls -la .docker",
    "ls .kube",
    "du -sh .aws",
    "du -s .aws",
    "ls -ld .ssh",
    "ls -l .ssh",
    "ls .ssh/",
    "ls .AWS",
    "ls -la sub/.ssh",
    # A file directly in a credential folder that the credential rules leave alone.
    "cat .docker/compose.yml",
    "cat .aws/plain.txt",
    "cat .kube/notes.txt",
    "cat sub/.ssh/known_hosts",
    "cat .ssh/known_hosts",
    "ls .ssh/known_hosts",
    "grep -r TODO .",
    "grep -r TODO src/",
    "cd src && grep -r TODO",
    "find . -name '*.py'",
])
def test_names_and_ordinary_files_of_a_credential_folder_are_still_vetted(world, credential_folders, text):
    plan, reason = plan_of(world, text)
    assert plan is not None, (text, reason)


@pytest.mark.parametrize("text", ["ls -la .docker", "cat README.md", "grep -r hello src", "cd src && ls",
                                  "ls .ssh", "cat .docker/compose.yml"])
def test_a_project_below_a_folder_with_a_credential_name_is_still_vetted(tmp_path, text):
    # Only the parts of a path below the project count: ~/.docker/work/project is an ordinary project.
    parent = tmp_path / ".docker" / "work"
    parent.mkdir(parents=True)
    project, _ = build_project(parent)
    (project / ".docker").mkdir()
    (project / ".docker" / "compose.yml").write_text("a: 1\n")
    settings = Settings(parent / "state")
    settings.values.update(workspace=str(project), env_file="")
    store = Store(settings.state_dir / "vet.sqlite3")
    try:
        manager = AgentManager(store, settings)
        session = store.create(settings.values)
        session["allowed_directories"] = []
        tools = manager.session_tools(session)
        plan, reason = manager.vet_command(tools, {"command": text}, None)
        assert plan is not None, (text, reason)
        plan, reason = manager.vet_command(tools, {"command": "grep -r x .ssh"}, None)
        assert plan is None and "credential" in reason
        plan, reason = manager.vet_command(tools, {"command": "git -C .ssh status"}, None)
        assert plan is None
    finally:
        store.db.close()


def test_a_credential_folder_is_not_a_working_folder(world):
    assert plan_of(world, "grep -r x", cwd=".ssh")[0] is None
    assert plan_of(world, "ls", cwd=".ssh/")[0] is None


# These also passed before the first fix round; they stay as guards.


def test_a_recursive_search_from_the_project_never_prints_a_private_key_still(world):
    plan, _ = plan_of(world, "grep -r PRIVATE-KEY-MATERIAL .")
    done = subprocess.run(list(plan.stages[0].run), cwd=plan.stages[0].cwd, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=20)
    assert "PRIVATE-KEY-MATERIAL" not in done.stdout


def test_paths_count_from_the_cwd_and_granted_folders_are_approved(world):
    _, _, project, other, _ = world
    plan, reason = plan_of(world, "cat a.py", cwd="src")
    assert plan is not None and plan.cwd == str(project / "src"), reason
    plan, reason = plan_of(world, f"cat {other}/file.txt")
    assert plan is not None, reason
    plan, reason = plan_of(world, "cat file.txt", cwd=str(other))
    assert plan is not None and plan.cwd == str(other), reason
    plan, reason = plan_of(world, f"cd {other} && cat file.txt")
    assert plan is not None and plan.cwd == str(other), reason
    assert "outside" in plan_of(world, "cat ../outside.txt", cwd=str(other))[1]
    assert plan_of(world, "ls", cwd="..")[0] is None


def test_globs_expand_in_the_cwd_sorted_without_dotfiles(world):
    plan, _ = plan_of(world, "ls *")
    assert plan.stages[0].argv[1:] == ("README.md", "bin", "data", "keys", "notes", "src")
    plan, _ = plan_of(world, "cat '*.md' src/[ab].py")
    assert plan.stages[0].argv[1:] == ("*.md", "src/a.py", "src/b.py")  # A quoted glob stays literal.
    assert "credential" in plan_of(world, "ls .e*")[1]
    assert "outside the project" in plan_of(world, "echo ../*")[1]
    assert "excluded" in plan_of(world, "echo ../state/*")[1]


def test_recursive_searches_skip_credential_names(world):
    plan, _ = plan_of(world, "grep -rn TOKEN")
    run = plan.stages[0].run
    # grep compares --exclude names case-sensitively: each glob names every case (.ENV, SERVER.PEM, .SSH).
    assert "--exclude=.[eE][nN][vV]" in run and "--exclude=*.[pP][eE][mM]" in run and "--exclude-dir=.[sS][sS][hH]" in run
    assert "--exclude=.[eE][nN][vV].*" in run and "--exclude=.[eE][nN][vV][rR][cC]" in run
    assert "--exclude=*[tT][oO][kK][eE][nN]" in run
    assert "--exclude=.[eE][nN][vV]*" not in run  # An .envs folder or .environment file is searched.
    assert run[-2:] == ("TOKEN", ".")
    plan, _ = plan_of(world, "grep -n TOKEN README.md")
    assert not any(word.startswith("--exclude") for word in plan.stages[0].run)


def test_grep_exclusions_name_every_case_or_the_search_is_not_vetted(world):
    assert shell_vet.any_case_glob("prod-[ab].yaml") == "[pP][rR][oO][dD]-[abAB].[yY][aA][mM][lL]"
    assert shell_vet.any_case_glob("[!a-c]*.key") == "[!a-cA-C]*.[kK][eE][yY]"
    assert shell_vet.any_case_glob("x[0-9]_?.tfvars") == "[xX][0-9]_?.[tT][fF][vV][aA][rR][sS]"
    for pattern in ("[a-Z].key", "[^a].key", "[[:alpha:]].key", "a\\b", "é.key", "[a", "*straße*"):
        assert shell_vet.any_case_glob(pattern) is None, pattern
    manager = world[0]
    try:
        manager.settings.values["secret_file_patterns"] = ["prod\\-*.tfvars"]
        plan, reason = plan_of(world, "grep -rn TOKEN")
        assert plan is None and "cannot skip a secret file name pattern in every case; use rg" in reason
        assert plan_of(world, "grep -n TOKEN README.md")[0] is not None  # Not recursive: no exclusions needed.
        # A letter outside ASCII: rg's case folding is ASCII only too. ß is kept as typed, not casefolded to ss,
        # which would be written in every ASCII case and miss Straße.
        for pattern in ("é.key", "*straße*"):
            manager.settings.values["secret_file_patterns"] = [pattern]
            for command in ("grep -rn TOKEN", *(("rg -n TOKEN src",) if shutil.which("rg") else ())):
                plan, reason = plan_of(world, command)
                assert plan is None and "secret file name pattern with a letter outside ASCII" in reason, command
            assert plan_of(world, "grep -n TOKEN README.md")[0] is not None
    finally:
        manager.settings.values.pop("secret_file_patterns")


@needs_rg
def test_rg_never_reads_its_config_and_skips_credential_names(world):
    plan, _ = plan_of(world, "rg -n TOKEN src")
    run = plan.stages[0].run
    assert run[1] == "--no-config" and "--iglob=!.env" in run and run[-2:] == ("TOKEN", "src")


def test_options_run_before_operands_and_dashed_operands_after_a_double_dash(world):
    plan, _ = plan_of(world, "grep hello README.md -n")  # GNU reads options anywhere; BSD would read a file -n.
    assert plan.stages[0].run[1:] == ("-n", "hello", "README.md")
    plan, _ = plan_of(world, "grep -- -x README.md")
    assert plan.stages[0].run[1:] == ("--", "-x", "README.md")
    plan, _ = plan_of(world, "head -3 README.md")
    assert plan.stages[0].run[1:] == ("-n3", "README.md")
    plan, _ = plan_of(world, "grep -C 2 /etc/hosts README.md")  # BSD grep -C takes only an attached value.
    assert plan.stages[0].run[1:] == ("-C2", "/etc/hosts", "README.md")
    plan, _ = plan_of(world, "grep -e '' README.md")
    assert plan.stages[0].run[1:] == ("-e", "", "README.md")
    plan, _ = plan_of(world, "sed --expression=1p -n -e 2p README.md")
    assert plan.stages[0].run[1:] == ("-e1p", "-n", "-e2p", "README.md")
    assert "ls -I is not an option" in plan_of(world, "ls -I /etc")[1]


@needs_git
def test_git_stages_keep_their_folder_and_pathspecs(world):
    _, _, project, other, _ = world
    plan, reason = plan_of(world, "git status --short && git log --oneline -n 3 | cat")
    assert plan is not None, reason
    assert [stage.kind for stage in plan.stages] == ["git", "git", "read"]
    assert plan.stages[1].run == ("log", "--oneline", "-n3")
    plan, _ = plan_of(world, "git diff HEAD -- src")
    assert plan.stages[0].run == ("diff", "HEAD", "--", "src")
    plan, _ = plan_of(world, f"git -C {other} status")
    assert plan.stages[0].cwd == str(other)
    plan, _ = plan_of(world, "git branch --list 'feat*'")
    assert plan.stages[0].run == ("branch", "--list", "feat*")
    assert "credential" in plan_of(world, "git diff -- .env")[1]


def test_a_rule_stage_runs_in_the_project_and_is_never_fed_by_a_pipe(world):
    _, _, project, _, _ = world
    rule = {"behavior": "allow", "tool": "run_command", "argv": ["make", "test"]}

    def allow(argv):
        return (rule, "project") if list(argv) == ["make", "test"] else None
    if not shutil.which("make"):
        pytest.skip("make is required")
    plan, reason = plan_of(world, "make test 2>&1 | tail -20", allow=allow)
    assert plan is not None, reason
    assert [stage.kind for stage in plan.stages] == ["rule", "read"] and plan.rules == ((rule, "project"),)
    assert not plan.read_only
    assert "reads the output of a pipe" in plan_of(world, "cat README.md | make test", allow=allow)[1]
    assert "only in the project folder" in plan_of(world, "make test", cwd="src", allow=allow)[1]
    assert "make is not a read-only program" in plan_of(world, "make test")[1]


def test_scripts_quote_every_word_and_run_git_through_env(world):
    plan = shell_vet.Plan("/srv/project", (
        shell_vet.Stage(("grep", "a b"), ("/usr/bin/grep", "a b", "x'y"), "read", "/srv/project", ("2>&1",)),
        shell_vet.Stage(("git", "status"), ("status", "--", "src"), "git", "/srv/project"),
        shell_vet.Stage(("head",), ("/usr/bin/head",), "read", "/srv/project")), ("|", "&&"))
    calls = []

    async def git_command(cwd, args, paths):
        calls.append((cwd, args, paths))
        return ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "status", "--", *paths], {"GIT_X": "a b", "HOME": "/h"}
    text = asyncio.run(shell_vet.script(plan, find_program=lambda name: f"/usr/bin/{name}", git_command=git_command))
    assert text == ("/usr/bin/grep 'a b' 'x'\"'\"'y' 2>&1 | /usr/bin/env -i 'GIT_X=a b' HOME=/h /usr/bin/git -c "
                    "core.hooksPath=/dev/null status -- src && /usr/bin/head")
    assert calls == [("/srv/project", ["status"], ["src"])]


def test_evaluate_matches_rules_against_each_stage(world):
    manager, session, *_ = world
    tools = manager.session_tools(session)

    def decide(text, mode="auto", **rules):
        return evaluate(mode, "run_command", {"command": text}, resolve_path=tools.resolve,
                        vet=lambda values, allow: manager.vet_command(tools, values, allow),
                        chat_rules=rules.get("chat", []), project_rules=rules.get("project", []))
    verdict = decide("cat README.md | head -1")
    assert (verdict.decision, verdict.source) == ("allow", "read_only_pipeline") and verdict.script is not None
    assert decide("cat README.md | head -1", "plan").decision == "allow"
    for mode in ("manual", "acceptEdits"):
        assert decide("cat README.md | head -1", mode).decision == "ask"
    assert decide("cat README.md | head -1", "dontAsk").decision == "deny"
    deny = [{"behavior": "deny", "tool": "run_command", "prefix": ["head"]}]
    verdict = decide("cat README.md | head -1", project=deny)
    assert (verdict.decision, verdict.source) == ("deny", "rule")
    ask = [{"behavior": "ask", "tool": "run_command", "prefix": ["cat"]}]
    verdict = decide("cat README.md | head -1", chat=ask)
    assert (verdict.decision, verdict.source, verdict.uncertain) == ("ask", "rule", False)
    # A deny rule for a program no stage runs leaves a vetted command alone (it is matched exactly, not "uncertain").
    assert decide("cat README.md | head -1", project=[{"behavior": "deny", "tool": "run_command",
                                                       "prefix": ["git", "push"]}]).decision == "allow"
    verdict = decide("cat README.md > out", "plan")
    assert verdict.decision == "deny" and "redirects output to a file" in verdict.message


def test_plain_commands_beyond_the_fixed_list_are_vetted_too(world):
    manager, session, *_ = world
    tools = manager.session_tools(session)
    vet = lambda values, allow: manager.vet_command(tools, values, allow)  # noqa: E731
    verdict = evaluate("auto", "run_command", {"command": "cat README.md"}, resolve_path=tools.resolve, vet=vet)
    assert verdict.source == "read_only_pipeline"
    verdict = evaluate("auto", "run_command", {"command": "ls"}, resolve_path=tools.resolve, vet=vet)
    assert verdict.source == "read_only_command"  # The fixed list keeps its hardened argv form.
    verdict = evaluate("auto", "run_command", {"command": "cat README.md", "background": True},
                       resolve_path=tools.resolve, vet=vet)
    assert verdict.decision == "ask"  # Vetted commands run in the foreground only.
    rule = {"behavior": "allow", "tool": "run_command", "argv": ["cat", "README.md"]}
    verdict = evaluate("manual", "run_command", {"command": "cat README.md"}, resolve_path=tools.resolve, vet=vet,
                       project_rules=[rule])
    assert (verdict.source, verdict.argv) == ("rule", ("cat", "README.md"))


# Runs ---------------------------------------------------------------------------------------------------------


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project, other = build_project(tmp_path)
    if shutil.which("git"):
        for command in (["git", "init", "-q"], ["git", "add", "README.md", "src"],
                        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "init"]):
            subprocess.run(command, cwd=project, check=True, capture_output=True)
    settings = Settings(tmp_path / "state")
    settings.values.update(workspace=str(project), env_file="")
    store = Store(settings.state_dir / "runs.sqlite3")
    manager = AgentManager(store, settings)
    session = store.create(settings.values)
    session["permission_mode"] = "auto"
    tools = manager.session_tools(session)
    guidance = load_project_instructions(tools)
    tools.instruction_signature = (guidance["text"], guidance["warnings"])
    tools.skill_signature = ""

    def no_shell(*args, **kwargs):
        raise AssertionError("The agent's text must never run through a shell.")
    monkeypatch.setattr("local_agent.jobs.shell_argv", no_shell)
    yield manager, session, project, tools
    store.db.close()


async def card(manager):
    for _ in range(300):
        if manager.pending:
            return next(iter(manager.pending))
        await asyncio.sleep(0.01)
    raise AssertionError("Expected an approval card")


async def run(manager, session, tools, command, call_id="c", **arguments):
    return await manager.execute_tool(session, tools, "run_command", {"command": command, **arguments}, call_id)


def event_of(session, call_id):
    return next(item for item in session["events"] if item.get("call_id") == call_id)


async def test_auto_runs_a_vetted_pipeline_as_the_apps_script(runtime):
    manager, session, project, tools = runtime
    result = json.loads(await run(manager, session, tools, "cat README.md | head -1 && echo done", "p"))
    assert result["output"] == "hello world\ndone\n" and result["exit_code"] == 0
    event = event_of(session, "p")
    assert event["read_only_pipeline"] is True and "without_shell" not in event and event["state"] == "completed"
    job = manager.jobs.get(result["job_id"])
    assert job["checked_script"] is True and job["command"] == "cat README.md | head -1 && echo done"


async def test_plan_runs_vetted_commands_and_manual_still_asks(runtime):
    manager, session, project, tools = runtime
    session["permission_mode"] = "plan"
    result = json.loads(await run(manager, session, tools, "grep -n second README.md | cut -d: -f1", "plan"))
    assert result["output"] == "2\n"
    output = await run(manager, session, tools, "grep -n second README.md > out.txt", "denied")
    assert output.startswith("Plan mode is read-only.") and "redirects output to a file" in output
    assert not (project / "out.txt").exists()
    session["permission_mode"] = "manual"
    task = asyncio.create_task(run(manager, session, tools, "grep -n second README.md | cut -d: -f1", "manual"))
    await card(manager)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_recursive_grep_never_reads_credential_files(runtime):
    manager, session, project, tools = runtime
    (project / ".envrc").write_text("export TOKEN=envrc-secret\n")
    (project / "notes" / "db_token").write_text("TOKEN=db-token-secret\n")
    result = json.loads(await run(manager, session, tools, "grep -rn = . | sort", "g"))
    assert "src/a.py:1:alpha = 1" in result["output"]
    assert "project-env-secret" not in result["output"] and "secrets.yaml" not in result["output"]
    assert "envrc-secret" not in result["output"] and "db-token-secret" not in result["output"]


@pytest.mark.parametrize("mode", ["auto", "plan"])
async def test_recursive_grep_skips_credential_files_whatever_the_case_of_their_names(runtime, mode):
    """The file tools and the command check compare names casefolded; grep's --exclude does not."""
    manager, session, project, tools = runtime
    session["permission_mode"] = mode
    manager.settings.values["secret_file_patterns"] = ["*.tfvars"]
    (project / "infra").mkdir()
    (project / "keys" / ".SSH").mkdir()
    for path, text in ((".ENV", "env-upper"), (".Env.Local", "envlocal-mixed"), ("keys/SERVER.PEM", "pem-upper"),
                       ("notes/API_TOKEN.TXT", "token-upper"), ("infra/PROD.TFVARS", "tfvars-upper"),
                       ("keys/.SSH/id_test", "ssh-upper"), ("infra/prod.tfvars", "tfvars-lower")):
        (project / path).write_text(f"value = {text}\n")
    output = json.loads(await run(manager, session, tools, "grep -rn = .", "g"))["output"]
    assert event_of(session, "g")["state"] == "completed" and "src/a.py:1:alpha = 1" in output
    leaked = [text for text in ("env-upper", "envlocal-mixed", "pem-upper", "token-upper", "tfvars-upper", "ssh-upper",
                                "tfvars-lower") if text in output]
    assert leaked == []


@pytest.mark.parametrize("mode", ["auto", "plan"])
def test_a_secret_name_pattern_with_a_letter_outside_ascii_leaves_recursive_searches_unchecked(runtime, mode):
    """Settings → Secret file names compare casefolded (ß as ss) in the command check; a recursive search's
    exclusions cannot: Auto asks for it and Plan refuses it, while a search of named files still runs."""
    manager, session, project, tools = runtime
    session["permission_mode"] = mode
    manager.settings.values["secret_file_patterns"] = ["*straße*"]
    (project / "Straße-plan.txt").write_text("value = sharp-s-secret\n")
    expected = "ask" if mode == "auto" else "deny"
    assert manager.permission(session, tools, "run_command", {"command": "cat Straße-plan.txt"}).decision == expected
    assert manager.permission(session, tools, "run_command", {"command": "grep -rn = ."}).decision == expected
    assert manager.permission(session, tools, "run_command", {"command": "grep -n = README.md"}).decision == "allow"


@pytest.mark.parametrize("name", ["state", "Données"])
def test_a_state_folder_name_outside_ascii_is_skipped_by_its_name(tmp_path, monkeypatch, name):
    """The app's state folder is no credential name: grep -r skips it as named on disk when its name cannot be
    written in every case, and stays checked."""
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project, _ = build_project(tmp_path)
    settings = Settings(tmp_path / name)
    settings.values.update(workspace=str(project), env_file="")
    store = Store(settings.state_dir / "runs.sqlite3")
    try:
        manager = AgentManager(store, settings)
        session = store.create(settings.values)
        session["permission_mode"] = "auto"
        tools = manager.session_tools(session)
        plan, reason = manager.vet_command(tools, {"command": "grep -rn TOKEN ."}, None)
        assert reason is None and f"--exclude-dir={shell_vet.any_case_glob(name) or name}" in plan.stages[0].run
        assert manager.permission(session, tools, "run_command", {"command": "grep -rn TOKEN ."}).decision == "allow"
    finally:
        store.db.close()


async def test_awk_sed_and_sort_run(runtime):
    manager, session, project, tools = runtime
    command = "sort -n data/values.txt | uniq | awk '{s += $1} END {print s}' && sed -n '2p' README.md"
    result = json.loads(await run(manager, session, tools, command, "a"))
    assert result["output"] == "6\nsecond line\n"


async def test_a_leading_cd_into_a_granted_folder_runs_there(runtime):
    manager, session, project, tools = runtime
    other = project.parent / "other"
    session["allowed_directories"] = [str(other)]
    tools = manager.session_tools(session)
    guidance = load_project_instructions(tools)
    tools.instruction_signature, tools.skill_signature = (guidance["text"], guidance["warnings"]), ""
    result = json.loads(await run(manager, session, tools, f"cd {other} && cat file.txt && pwd", "cd"))
    assert result["output"] == f"other file\n{other}\n"
    job = manager.jobs.get(result["job_id"])
    assert job["cwd"] == str(other) and job["workspace"] == str(project)


@needs_git
async def test_vetted_git_runs_hardened_in_its_repository(runtime):
    manager, session, project, tools = runtime
    (project / "README.md").write_text("changed\n")
    result = json.loads(await run(manager, session, tools, "git status --short -- README.md && git log --oneline | wc -l",
                                  "git"))
    assert result["output"].split() == ["M", "README.md", "1"]
    result = json.loads(await run(manager, session, tools, "git diff --stat -- README.md | tail -1", "diff"))
    assert "1 file changed" in result["output"]


async def test_a_planted_program_on_path_never_runs(runtime, monkeypatch):
    manager, session, project, tools = runtime
    planted = project / "bin" / "cat"
    planted.write_text("#!/bin/sh\necho planted > planted.txt\n")
    planted.chmod(0o755)
    monkeypatch.setenv("PATH", f"{project / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}")
    task = asyncio.create_task(run(manager, session, tools, "cat README.md | head -1", "planted"))
    await card(manager)  # Not vetted: cat resolves inside the project, so the command asks.
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not (project / "planted.txt").exists()


async def test_a_waiting_card_resolved_by_auto_runs_the_script(runtime):
    manager, session, project, tools = runtime
    session["permission_mode"] = "manual"
    task = asyncio.create_task(run(manager, session, tools, "head -1 README.md | wc -c", "switch"))
    await card(manager)
    await manager.change_mode(session, "auto")
    result = json.loads(await asyncio.wait_for(task, 10))
    assert result["output"].strip() == "12"
    assert event_of(session, "switch")["approved_by_mode"] == "auto"


# A2: tracked files inside credential folders at any depth never print through a vetted Git command -----------------

KEY_FILES = {"sub/.ssh/deploy_key": "KEYMARK-sub-ssh", ".aws/cli/cache/abc.json": "KEYMARK-aws-cache",
             ".ssh/keys.pub/deploy": "KEYMARK-ssh-pub-folder"}


def repository_with_keys(project):
    """The runtime project plus a second commit that tracks the key files, then a change to every one of them
    and to src/app.py: any content of a key file in a diff is then a leak."""
    def commit(message):
        subprocess.run(["git", "add", "-f", "-A", "sub", ".aws", ".ssh/keys.pub", ".docker", "src"], cwd=project,
                       check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", message],
                       cwd=project, check=True, capture_output=True)
    files = {**{path: f"{marker}-base\n" for path, marker in KEY_FILES.items()}, ".docker/compose.yml": "a: 1\n",
             "src/app.py": "APPBASE = 1\n"}
    for path, text in files.items():
        (project / path).parent.mkdir(parents=True, exist_ok=True)
        (project / path).write_text(text)
    commit("track keys")
    for path, marker in KEY_FILES.items():
        (project / path).write_text(f"{marker}-changed\n")
    (project / ".docker/compose.yml").write_text("a: 2\n")
    (project / "src/app.py").write_text("APPCHANGE = 2\n")
    commit("change keys")  # So that HEAD, HEAD~1 and the working tree all differ in the key files.
    for path, marker in KEY_FILES.items():
        (project / path).write_text(f"{marker}-working\n")
    (project / "src/app.py").write_text("APPCHANGE = 3\n")


# (command, whether src/app.py shows in its output)
GIT_VIEWS = [
    ("git diff", True), ("git diff HEAD", True), ("git show", True), ("git show HEAD", True),
    ("git diff HEAD~1", True), ("git diff -- sub", False), ("git diff -- .", True), ("git diff -- '*.json'", False),
    ("git -C sub diff", False), ("cd sub && git diff", False), ("git diff -w", True), ("git diff -U0", True),
    ("git diff --stat", True), ("git diff --cached", False), ("git show --stat", True), ("git show -p HEAD~1", True),
    ("git diff HEAD HEAD~1", True), ("git diff HEAD~1..HEAD", True), ("git diff HEAD~1...", True),
    ("git status -v", True), ("git diff -- .aws", False), ("git diff -- .ssh", False), ("git diff -- sub/.ssh", False),
    ("git diff -- .aws/cli", False), ("git diff -- ./.aws/cli/cache/abc.json", False),
    ("git diff --relative=sub", False), ("git diff HEAD~1 -- sub .aws", False),
]
LEAK_COMMANDS = [
    "git log -p", "git log -p -- sub", "git log --patch", "git show HEAD:sub/.ssh/deploy_key", "git show HEAD:.ssh/keys.pub/deploy",
    "git show :sub/.ssh/deploy_key", "git grep KEYMARK", "git grep -n KEYMARK HEAD", "git stash show -p", "git stash show -p stash@{0}",
    "git cat-file -p HEAD:sub/.ssh/deploy_key", "git cat-file blob HEAD:sub/.ssh/deploy_key", "git blame sub/.ssh/deploy_key",
    "git format-patch -1 --stdout", "git range-diff HEAD~1 HEAD", "git diff --no-index sub/.ssh/deploy_key README.md",
    "git ls-tree -r HEAD", "git archive HEAD", "git diff-tree -p HEAD", "git show-ref", "git reflog -p",
    "git whatchanged -p", "git diff -- ..", "git -C sub diff -- ..", "cd sub && git diff -- ..", "cd sub && git diff -- ../.aws",
    "git -C sub show -- ..", "git log --stat -p", "git diff -c HEAD", "git diff --cc",
]


def vetted(manager, tools, command):
    plan, reason = manager.vet_command(tools, {"command": command}, None)
    return plan, reason


async def output_of(manager, session, tools, command, call_id):
    """What the command printed when it ran (or the error text when running it failed)."""
    try:
        return json.loads(await run(manager, session, tools, command, call_id))["output"]
    except (ValueError, KeyError, TypeError) as exc:
        return f"error: {exc}"


@needs_git
@pytest.mark.parametrize("command, shows_app", GIT_VIEWS)
async def test_a_vetted_git_view_never_prints_a_key_file_in_a_credential_folder(runtime, command, shows_app):
    manager, session, project, tools = runtime
    session["permission_mode"] = "plan"  # What is not vetted is refused at once, nothing waits for a card.
    repository_with_keys(project)
    plan, reason = vetted(manager, tools, command)
    if plan is None:
        return  # Asking is always safe.
    output = await output_of(manager, session, tools, command, "view")
    assert "KEYMARK" not in output, (command, output)
    if shows_app:
        assert "app.py" in output, (command, output)


@needs_git
@pytest.mark.parametrize("command", [
    "git diff", "git diff HEAD", "git show", "git show HEAD", "git diff HEAD~1", "git diff -- .", "git diff -w", "git diff -U0",
    "git -C sub diff", "cd sub && git diff", "git diff -- sub",
])
def test_the_git_views_of_a_project_are_vetted_not_just_asked(runtime, command):
    manager, session, project, tools = runtime
    repository_with_keys(project)
    assert vetted(manager, tools, command)[0] is not None, command


@needs_git
@pytest.mark.parametrize("command", LEAK_COMMANDS)
async def test_git_commands_that_could_print_a_key_file_are_not_vetted_or_print_none(runtime, command):
    manager, session, project, tools = runtime
    session["permission_mode"] = "plan"
    repository_with_keys(project)
    plan, reason = vetted(manager, tools, command)
    if plan is None:
        assert reason, command
        return
    output = await output_of(manager, session, tools, command, "leak")
    assert "KEYMARK" not in output, (command, output)


@needs_git
@pytest.mark.parametrize("revisions", ["{blob}", "{blob} {old}", "{old} {blob}", "{blob_short}", "HEAD {blob}", "{old}..{blob}",
                                       "{blob}^{{blob}}", "{tag}"])
@pytest.mark.parametrize("command", ["git show", "git diff", "git show --stat", "git diff --stat"])
async def test_a_blob_named_by_its_hash_never_prints_through_git_show_or_diff(runtime, command, revisions):
    manager, session, project, tools = runtime
    session["permission_mode"] = "plan"
    repository_with_keys(project)
    blob = subprocess.run(["git", "rev-parse", "HEAD:sub/.ssh/deploy_key"], cwd=project, check=True,
                          capture_output=True, text=True).stdout.strip()
    old = subprocess.run(["git", "rev-parse", "HEAD~1:sub/.ssh/deploy_key"], cwd=project, check=True,
                         capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "tag", "blobtag", blob], cwd=project, check=True, capture_output=True)
    text = f"{command} " + revisions.format(blob=blob, old=old, blob_short=blob[:10], tag="blobtag")
    plan, reason = vetted(manager, tools, text)
    if plan is None:
        return
    output = await output_of(manager, session, tools, text, "blob")
    assert "KEYMARK" not in output, (text, output)


@needs_git
def test_git_pathspecs_cannot_climb_out_of_the_folder_the_excludes_are_written_for(runtime):
    manager, session, project, tools = runtime
    repository_with_keys(project)
    for command in ("cd sub && git diff -- ..", "git -C sub diff -- ../.aws", "git diff -- sub/..", "git diff -- ./../x"):
        plan, reason = vetted(manager, tools, command)
        assert plan is None and ".." in reason, (command, reason)
    assert vetted(manager, tools, "git diff -- src/.")[0] is not None


@needs_git
@pytest.mark.parametrize("command", [
    # A3: recursion and working-folder changes inside a credential-named folder always ask, even for .docker.
    "cd .docker && ls", "cd .docker/ && ls", "grep -r a .docker", "grep -r a .docker/", "rg a .docker",
    "find .docker -type f", "find .docker", "ls -R .docker", "ls --recursive .docker", "du -a .docker",
    "git -C .docker status", "git -C .docker log", "cd .docker && git status",
    "cd .aws && ls", "grep -r a .kube", "ls -R .kube", "git -C .kube status",
])
def test_a_command_that_goes_into_a_credential_named_folder_asks(world, credential_folders, command):
    # The rule: recursion (grep -r, find, ls -R, du -a) and a change of working folder (cd, git -C) inside a
    # folder named .ssh, .gnupg, .aws, .kube or .docker always ask. That also costs the harmless top-level .docker.
    assert plan_of(world, command)[0] is None, command


@needs_git
@pytest.mark.parametrize("command", [
    # A3, the other side of the same rule: a file named directly inside .aws, .kube or .docker follows the name
    # rules (the credential names are refused, ordinary names are read), as it did before the first fix round.
    "cat .docker/compose.yml", "cat .aws/plain.txt", "cat .kube/notes.txt", "head -1 .docker/compose.yml",
    "wc -l .docker/compose.yml", "ls .docker", "ls -la .docker", "ls .docker/compose.yml",
])
def test_an_ordinary_file_directly_in_a_credential_named_folder_is_still_read_unasked(world, credential_folders, command):
    assert plan_of(world, command)[0] is not None, command


@pytest.mark.parametrize("command", ["cat .docker/config.json", "cat .aws/credentials", "cat .aws/config",
                                     "cat .kube/config", "cat .DOCKER/CONFIG.JSON"])
def test_the_credential_names_directly_in_a_credential_named_folder_still_ask(world, credential_folders, command):
    assert plan_of(world, command)[0] is None, command
