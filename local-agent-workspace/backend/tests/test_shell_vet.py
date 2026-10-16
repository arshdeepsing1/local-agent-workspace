"""Vetted read-only commands (shell_vet): the accept and refuse table, scripts, and runs in Auto and Plan."""
import asyncio
import json
import os
import shutil
import subprocess
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
    ("cat ../outside.txt", "outside the project"),
    ("cd .. && cat outside.txt", "outside the project"),
    ("cat ~/.ssh/id_rsa", "credential"),
    ("cat .env", "credential"),
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
    assert "--exclude=.env" in run and "--exclude=*.pem" in run and "--exclude-dir=.ssh" in run
    assert run[-2:] == ("TOKEN", ".")
    plan, _ = plan_of(world, "grep -n TOKEN README.md")
    assert not any(word.startswith("--exclude") for word in plan.stages[0].run)


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
    result = json.loads(await run(manager, session, tools, "grep -rn = . | sort", "g"))
    assert "src/a.py:1:alpha = 1" in result["output"]
    assert "project-env-secret" not in result["output"] and "secrets.yaml" not in result["output"]


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
