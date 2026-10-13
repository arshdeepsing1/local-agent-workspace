"""The permission decision table: which commands rules allow, ask about or deny, in each mode."""
from pathlib import Path

import pytest

from local_agent.permissions import evaluate
from local_agent.rules import (METACHARACTERS, NEVER_FLAGS, WRAPPERS, normalize_rule, opaque, parse_command,
                               read_only_argv, rule_matches, uncertain_match)

ALLOW = [
    {"behavior": "allow", "tool": "run_command", "argv": ["npm", "test"]},
    {"behavior": "allow", "tool": "run_command", "prefix": ["git", "status"], "flags": ["--short", "-s"]},
    {"behavior": "allow", "tool": "run_command", "prefix": ["rg", "-n"], "flags": ["TODO"]},
    {"behavior": "allow", "tool": "run_command", "argv": ["make", "lint"]},
]
USER = [{"behavior": "deny", "tool": "run_command", "prefix": ["git", "push"]}]
PROJECT = [*ALLOW, {"behavior": "deny", "tool": "run_command", "prefix": ["rm", "-rf"]}]
CHAT = [{"tool": "run_command", "command": "npm test | tee out"}]


def resolve(value):
    return Path("/srv/project") / value


def decide(command, mode="manual", chat=CHAT, project=PROJECT, user=USER, **arguments):
    return evaluate(mode, "run_command", {"command": command, **arguments}, chat_rules=chat, project_rules=project,
                    user_rules=user, resolve_path=resolve, protected=lambda value: None, disabled=set())


@pytest.mark.parametrize("command", ["npm test", "npm  test"])
def test_an_exact_argv_rule_allows_the_same_words_without_a_shell(command):
    verdict = decide(command)
    assert (verdict.decision, verdict.source, verdict.scope) == ("allow", "rule", "project")
    assert verdict.argv == ("npm", "test") and verdict.read_only is False


@pytest.mark.parametrize("command", [
    "npm test; rm -rf ~", "npm test && rm -rf x", "npm test | tee out2", "npm test $(rm -rf ~)", "npm test `id`",
    "npm test > out.txt", "npm test 2>&1", "npm test < in", "npm test src/*.js", "npm test ~/x", "npm test --watch",
    "npm tests", "NODE_OPTIONS=--require=x npm test"])
def test_anything_else_near_an_allowed_command_asks(command):
    assert decide(command).decision == "ask"


@pytest.mark.parametrize("mode", ["manual", "bypassPermissions"])
def test_a_legacy_chat_rule_allows_its_exact_text_through_the_shell(mode):
    verdict = decide("npm test | tee out", mode)
    assert verdict.decision == "allow" and verdict.argv is None
    if mode == "manual":
        assert (verdict.source, verdict.scope) == ("rule", "chat")


def test_prefix_rules_allow_only_listed_options():
    verdict = decide("git status -s")
    assert verdict.decision == "allow" and verdict.argv == ("git", "status", "-s") and verdict.read_only
    assert decide("git status --porcelain").decision == "ask"
    for mode in ("manual", "auto"):
        assert decide("git -c core.pager=less status", mode).decision == "ask"
    assert decide("rg -n TODO").decision == "allow"
    # The prefix must come first, word for word: these contain the same words in another order.
    assert decide("rg TODO -n").decision == "ask"
    assert decide("git -s status").decision == "ask"
    assert decide("rg --pre=x -n TODO").decision == "ask"
    assert decide("rg --pre x TODO").decision == "ask"


def test_test_runners_never_run_by_mode():
    assert decide("pytest", "auto").decision == "ask"
    assert decide("pytest", "acceptEdits").decision == "ask"
    verdict = decide("pytest", "dontAsk")
    assert (verdict.decision, verdict.source) == ("deny", "dont_ask")
    assert verdict.message.startswith("Not run: Don't ask mode blocks")
    assert decide("python -m pytest", "auto").decision == "ask"


def test_auto_runs_only_the_fixed_read_only_list():
    verdict = decide("ls", "auto")
    assert (verdict.decision, verdict.source, verdict.read_only, verdict.argv) == ("allow", "read_only_command", True, ("ls",))
    for command in ("ls -R", "ls ../private", "git remote -v"):
        assert decide(command, "auto").decision == "ask", command
    verdict = decide("git status", "auto")
    assert verdict.decision == "allow" and verdict.read_only
    assert decide("git status", "auto", project=[], background=True).decision == "ask"
    verdict = decide("git status", "auto", background=True)  # A rule still allows it, but not as read-only.
    assert (verdict.source, verdict.read_only) == ("rule", False)


@pytest.mark.parametrize("command", ["git push origin main", "/usr/bin/git -C . push", '"git" push', "git --force push"])
def test_deny_rules_win_in_bypass(command):
    verdict = decide(command, "bypassPermissions")
    assert (verdict.decision, verdict.source, verdict.scope) == ("deny", "rule", "user")
    assert verdict.message == ("Blocked by a deny rule for all projects: commands starting with git push (any options). "
                               "Do not retry it or work around it.")


@pytest.mark.parametrize("command", [
    "env git push", "command git push", "nohup git push", "timeout 5 git push", "xargs git push", 'sh -c "git push"',
    "noglob git push", "- git push", "git -c alias.p=push p", "git status && git push", "/usr/bin/gi? push"])
def test_commands_a_deny_rule_might_cover_ask_even_in_bypass(command):
    verdict = decide(command, "bypassPermissions")
    assert (verdict.decision, verdict.uncertain) == ("ask", True), command


def test_unrelated_shell_syntax_does_not_ask_in_bypass():
    verdict = decide("npm run build | tee out", "bypassPermissions")
    assert verdict.decision == "allow" and not verdict.uncertain


@pytest.mark.parametrize("command", ["rm -r -f build", "rm -fr build", "rm -rf build", "/bin/rm -f -r build"])
def test_deny_rules_expand_short_option_clusters(command):
    verdict = decide(command)
    assert (verdict.decision, verdict.scope) == ("deny", "project")


def test_plan_ignores_allow_rules_and_keeps_ask_rules():
    verdict = decide("npm test", "plan")
    assert (verdict.decision, verdict.source) == ("deny", "plan")
    assert decide("git status", "plan").decision == "allow"
    ask = {"behavior": "ask", "tool": "run_command", "prefix": ["git", "diff"]}
    assert decide("git diff", "plan").decision == "allow"
    assert decide("git diff", "plan", chat=[*CHAT, ask]).decision == "ask"


def test_dont_ask_runs_what_a_rule_allows():
    verdict = decide("npm test", "dontAsk")
    assert (verdict.decision, verdict.source) == ("allow", "rule")
    assert decide("make test", "dontAsk").decision == "deny"


def test_parse_command_rejects_every_shell_metacharacter_and_control_character():
    for character in METACHARACTERS:
        assert parse_command(f"echo a{character}b")[0] is None, character
        assert parse_command(f"echo 'a{character}b'")[0] is None, character
    for text in ("echo\ta", "echo a\nb", "echo \x7f", "", "   ", "A=1 echo", "echo 'open", "x" * 50_001):
        assert parse_command(text)[0] is None, text
    assert parse_command("echo " + " ".join(["a"] * 64))[0] is None
    assert parse_command("echo " + "a" * 1001)[0] is None
    assert parse_command('git commit -m "a message"') == (["git", "commit", "-m", "a message"], None)
    assert parse_command('"git" push')[0] == ["git", "push"]
    assert parse_command("echo a;b")[1] == "uses shell syntax (;)"


def test_opaque_commands_and_never_flags():
    assert all(opaque([wrapper, "git", "push"]) for wrapper in WRAPPERS)
    for argv in (["python3", "-c", "x"], ["python3.12", "-c", "x"], ["node", "-e", "x"], ["perl", "-E", "x"],
                 ["pwsh", "-Command", "x"], ["awk", "{print}"], ["sed", "s/a/b/"], ["git", "-ccore.pager=x", "log"],
                 ["rg", "--pre=x", "a"], ["rg", "-nz", "a"], ["find", ".", "-delete"], ["git", "--git-dir=x", "log"]):
        assert opaque(argv), argv
    for argv in (["python3", "script.py"], ["git", "status"], ["rg", "-n", "x"], ["find", ".", "-name", "x"]):
        assert not opaque(argv), argv
    assert set(NEVER_FLAGS) == {"git", "rg", "find"}


def test_uncertain_matching_of_unparseable_text():
    deny = {"behavior": "deny", "tool": "run_command", "prefix": ["git", "push"]}
    assert not uncertain_match(deny, "npm test | tee out", None)
    assert uncertain_match(deny, "git status && git push", None)
    assert uncertain_match(deny, "echo $HOME", None)
    assert uncertain_match(deny, "npm test | sh", None)
    assert uncertain_match({"behavior": "ask", "tool": "run_command"}, "npm test | tee out", None)
    assert not uncertain_match({"behavior": "allow", "tool": "run_command", "argv": ["git"]}, "git x | y", None)


def test_read_only_list_spellings():
    assert read_only_argv(["/bin/ls", "-la"]) and read_only_argv(["/bin/pwd"])
    assert not read_only_argv(["/usr/bin/ls"]) and not read_only_argv(["git", "remote", "-v"])


def test_an_allow_rule_never_matches_an_opaque_or_credential_command():
    rule = {"behavior": "allow", "tool": "run_command", "prefix": ["cat"], "flags": [".env", "a.txt"]}
    assert rule_matches(rule, "run_command", {"command": "cat a.txt"}, resolve_path=resolve)
    assert not rule_matches(rule, "run_command", {"command": "cat .env"}, resolve_path=resolve)
    subset = {"behavior": "allow", "tool": "run_command", "prefix": ["git", "status"], "flags": []}
    assert not rule_matches(subset, "run_command", {"command": "git -C . status"}, resolve_path=resolve)
    option = {"behavior": "allow", "tool": "run_command", "prefix": ["npm", "test"], "flags": ["--color="]}
    assert rule_matches(option, "run_command", {"command": "npm test --color=always"}, resolve_path=resolve)
    assert not rule_matches(option, "run_command", {"command": "npm test --colour=always"}, resolve_path=resolve)


CONTEXT = {"resolve_path": resolve, "credential_file": "/srv/secrets/databricks.env", "credential_tokens": ["dapi-secret-1"]}


@pytest.mark.parametrize("draft, message", [
    ({"tool": "run_command", "prefix": ["bash"]}, "runs other programs"),
    ({"tool": "run_command", "prefix": ["python3", "-c"]}, "can run code"),
    ({"tool": "run_command", "prefix": ["rg"], "flags": ["--pre="]}, "'--pre=' can run other programs; a rule cannot allow it."),
    ({"tool": "run_command", "prefix": ["git", "-c"]}, "'-c' can run other programs; a rule cannot allow it."),
    ({"tool": "run_command", "argv": ["cat", ".env"]}, "Rules cannot allow commands that name credential files."),
    ({"tool": "run_command", "argv": ["docker", "run", "--env-file=.env"]}, "name credential files"),
    ({"tool": "run_command", "text": "cat config/credentials.json/x", "match": "exact"}, "name credential files"),
    ({"tool": "run_command", "argv": ["cat", "/srv/secrets/databricks.env"]}, "name credential files"),
    ({"tool": "run_command", "argv": ["curl", "-H", "dapi-secret-1"]}, "A rule cannot contain your Databricks token."),
    ({"tool": "run_command", "behavior": "allow"}, "a rule cannot allow every command"),
    ({"tool": "edits", "path": "relative/folder"}, "Enter an absolute folder path."),
    ({"tool": "read_file", "behavior": "ask"}, "Only deny rules apply to read_file."),
    ({"tool": "read_file", "behavior": "allow"}, "Only deny rules apply to read_file."),
    ({"tool": "run_command", "behavior": "deny", "prefix": [f"w{index}" for index in range(17)]},
     "A deny or ask rule can have at most 16 words."),
    ({"tool": "run_command", "behavior": "allow", "prefix": ["a", "b", "c", "d"]}, "at most 3 words"),
    ({"tool": "run_command", "text": "npm test; id", "match": "exact"}, "uses shell syntax (;)"),
    ({"tool": "mcp__docs__*", "behavior": "allow"}, "can only deny or ask"),
    ({"tool": "run_command", "command": "npm test"}, "approval card"),
])
def test_normalize_rule_rejections(draft, message):
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace(")", r"\)").replace(".", r"\.")):
        normalize_rule(draft, **CONTEXT)


def test_normalize_rule_canonical_forms():
    deny = normalize_rule({"tool": "run_command", "behavior": "deny", "text": "git push origin main", "match": "exact"}, **CONTEXT)
    assert deny == {"behavior": "deny", "prefix": ["git", "push", "origin", "main"], "tool": "run_command"}
    assert list(deny) == sorted(deny)
    allow = normalize_rule({"tool": "run_command", "behavior": "allow", "text": "git status", "match": "prefix",
                            "options": "-s --short"}, **CONTEXT)
    assert allow == {"behavior": "allow", "flags": ["-s", "--short"], "prefix": ["git", "status"], "tool": "run_command"}
    assert normalize_rule({"tool": "edits"}, **CONTEXT) == {"behavior": "allow", "tool": "edits"}
    assert normalize_rule({"tool": "edits", "path": "/srv/project/src"}, **CONTEXT)["path"] == "/srv/project/src"
    assert normalize_rule({"tool": "mcp__docs__*", "behavior": "deny"}, **CONTEXT)["tool"] == "mcp__docs__*"
    assert normalize_rule({"tool": "read_file", "behavior": "deny"}, **CONTEXT) == {"behavior": "deny", "tool": "read_file"}
    assert normalize_rule({"tool": "run_command", "behavior": "deny"}, **CONTEXT) == {"behavior": "deny", "tool": "run_command"}
    # Deny rules may name credential files.
    assert normalize_rule({"tool": "run_command", "behavior": "deny", "argv": ["cat", ".env"]}, **CONTEXT)["prefix"] == ["cat", ".env"]
