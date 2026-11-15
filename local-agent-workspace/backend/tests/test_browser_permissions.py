"""Permissions of the browser tool: the decision table in every mode, the host rules, the engine's refusals, the
card data and the rule labels. No browser is started: a preview is a plain object with the engine's fields."""
from types import SimpleNamespace

import pytest

from local_agent.approvals import rule_for
from local_agent.browser_tool import decide, make_allowed
from local_agent.feature_tools import BROWSER_TOOL, FEATURE_TOOLS
from local_agent.permissions import MODE_LABELS, TOOL_KINDS, denied_tools, evaluate, tool_decision, tool_kind
from local_agent.rules import card_rule_options, default_rule, normalize_rule, rule_label
from local_agent.rules import rule_matches as _rule_matches
from local_agent.tool_profiles import FILE_TOOLS, READ_TOOLS, tool_allowed

MODES = ("manual", "auto", "acceptEdits", "plan", "bypassPermissions", "dontAsk")
PUBLIC = "docs.example.com"
LOOPBACK = "localhost:3000"


def rule_matches(rule, name, arguments):
    return _rule_matches(rule, name, arguments, resolve_path=lambda value: value)


def allow(host):
    return {"behavior": "allow", "tool": "browser", "domain": host}


def deny(host):
    return {"behavior": "deny", "tool": "browser", "domain": host}


def ask(host):
    return {"behavior": "ask", "tool": "browser", "domain": host}


def preview(host, submits=False, refused=None, text=""):
    return SimpleNamespace(host=host, submits=submits, refused=refused, text=text, tab="t1")


def verdict(mode, action, host, *, rules=(), submits=False, **arguments):
    """The decision string of one browser action."""
    arguments = {"action": action, **arguments}
    return decide({}, arguments, preview(host, submits), mode, {"chat": list(rules)}).decision


def table(host, rules, action, **kwargs):
    return {mode: verdict(mode, action, host, rules=rules, **kwargs) for mode in MODES}


READ_ACTIONS = ("read", "find", "scroll", "tabs", "console", "network", "screenshot", "back", "forward", "close")


# The decision table ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("host", [PUBLIC, LOOPBACK])
@pytest.mark.parametrize("action", READ_ACTIONS)
def test_read_actions_on_an_allowed_host_never_ask(host, action):
    assert set(table(host, [allow(host)], action).values()) == {"allow"}


@pytest.mark.parametrize("host", [PUBLIC, LOOPBACK])
@pytest.mark.parametrize("action", ["click", "type", "select"])
def test_acting_on_an_allowed_host_asks_only_in_manual(host, action):
    assert table(host, [allow(host)], action) == {
        "manual": "ask", "auto": "allow", "acceptEdits": "allow", "plan": "deny", "bypassPermissions": "allow",
        "dontAsk": "deny"}


@pytest.mark.parametrize("action", ["click", "type"])
def test_a_submit_on_a_public_host_asks_in_every_mode_but_bypass(action):
    assert table(PUBLIC, [allow(PUBLIC)], action, submits=True) == {
        "manual": "ask", "auto": "ask", "acceptEdits": "ask", "plan": "deny", "bypassPermissions": "allow",
        "dontAsk": "deny"}


def test_a_host_that_is_not_allowed_yet_asks_except_in_plan_dont_ask_and_bypass():
    assert table(PUBLIC, [], "open", url=f"https://{PUBLIC}/") == {
        "manual": "ask", "auto": "ask", "acceptEdits": "ask", "plan": "ask", "bypassPermissions": "allow",
        "dontAsk": "deny"}
    assert table(PUBLIC, [], "read") == table(PUBLIC, [], "open", url=f"https://{PUBLIC}/")
    assert table(PUBLIC, [], "click", ref="r1") == {
        "manual": "ask", "auto": "ask", "acceptEdits": "ask", "plan": "deny", "bypassPermissions": "allow",
        "dontAsk": "deny"}


def test_an_allowed_loopback_port_does_not_allow_another_port():
    rules = [allow("localhost:3000")]
    assert verdict("auto", "read", "localhost:3000", rules=rules) == "allow"
    assert verdict("auto", "read", "localhost:3001", rules=rules) == "ask"
    assert verdict("auto", "read", "127.0.0.1:3000", rules=rules) == "ask"
    assert verdict("auto", "read", "app.localhost:3000", rules=rules) == "ask"


def test_an_allow_rule_names_one_host_exactly():
    rules = [allow(PUBLIC)]
    assert verdict("auto", "read", PUBLIC, rules=rules) == "allow"
    assert verdict("auto", "read", "api." + PUBLIC, rules=rules) == "ask"
    assert verdict("auto", "read", "example.com", rules=rules) == "ask"
    assert verdict("auto", "read", "evil-" + PUBLIC, rules=rules) == "ask"
    assert verdict("auto", "read", "docs.example.com.evil.test", rules=rules) == "ask"
    assert verdict("auto", "read", PUBLIC, rules=[allow("example.com")]) == "ask"


def test_plan_allows_only_read_actions_on_allowed_hosts_and_refuses_the_rest_with_plans_text():
    rules = [allow(PUBLIC)]
    assert verdict("plan", "read", PUBLIC, rules=rules) == "allow"
    assert verdict("plan", "open", PUBLIC, rules=rules, url=f"https://{PUBLIC}/") == "allow"
    done = decide({}, {"action": "click", "ref": "r1"}, preview(PUBLIC), "plan", {"chat": rules})
    assert done.decision == "deny" and done.source == "plan"
    assert done.message.startswith("Plan mode is read-only.")
    for action in ("type", "select"):
        assert verdict("plan", action, PUBLIC, rules=rules) == "deny"


def test_a_subagent_in_plan_mode_gets_its_own_refusal_text():
    done = decide({"is_subagent": True, "turn_delegated": True}, {"action": "click", "ref": "r1"}, preview(PUBLIC), "plan", {"chat": [allow(PUBLIC)]})
    assert done.decision == "deny" and "submit_report" in done.message


def test_dont_ask_refuses_what_would_ask_and_names_the_mode():
    done = decide({}, {"action": "open", "url": f"https://{PUBLIC}/"}, preview(PUBLIC), "dontAsk", {})
    assert done.decision == "deny" and done.source == "dont_ask" and "Don't ask" in done.message
    assert verdict("dontAsk", "read", PUBLIC, rules=[allow(PUBLIC)]) == "allow"


# Deny and ask rules ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
def test_a_deny_rule_wins_in_every_mode_including_bypass(mode):
    for host in (PUBLIC, LOOPBACK):
        assert verdict(mode, "read", host, rules=[allow(host), deny(host)]) == "deny"
        assert verdict(mode, "click", host, rules=[deny(host)]) == "deny"


@pytest.mark.parametrize("mode", MODES)
def test_a_deny_rule_for_a_parent_domain_covers_its_subdomains(mode):
    rules = [allow(PUBLIC), deny("example.com")]
    done = decide({}, {"action": "read"}, preview(PUBLIC), mode, {"chat": rules})
    assert done.decision == "deny" and done.source == "rule"
    assert "Browse example.com" in done.message and "Do not retry" in done.message


def test_a_deny_rule_for_a_loopback_port_covers_only_that_port():
    rules = [deny("localhost:3000")]
    assert verdict("bypassPermissions", "read", "localhost:3000", rules=rules) == "deny"
    assert verdict("bypassPermissions", "read", "localhost:3001", rules=rules) == "allow"
    assert verdict("bypassPermissions", "read", "sub.localhost:3000", rules=rules) == "allow"


def test_an_ask_rule_for_a_domain_asks_for_every_action_on_it_and_its_subdomains():
    rules = [allow("api.example.com"), ask("example.com")]
    assert verdict("auto", "read", "api.example.com", rules=rules) == "ask"
    assert verdict("auto", "read", "example.com", rules=rules) == "ask"
    assert verdict("dontAsk", "read", "api.example.com", rules=rules) == "deny"
    assert verdict("auto", "read", "other.test", rules=rules) == "ask"  # not allowed: the usual card


def test_a_deny_rule_in_the_project_or_user_scope_counts_too():
    for scope in ("project", "user"):
        done = decide({}, {"action": "read"}, preview(PUBLIC), "bypassPermissions", {scope: [deny(PUBLIC)]})
        assert done.decision == "deny"
    done = decide({}, {"action": "read"}, preview(PUBLIC), "auto", {"user": [allow(PUBLIC)]})
    assert done.decision == "allow"


def test_a_tool_rule_without_a_domain_denies_every_host():
    rules = [{"behavior": "deny", "tool": "browser"}]
    assert verdict("bypassPermissions", "read", PUBLIC, rules=rules) == "deny"
    assert denied_tools(rules, [], [], ["browser", "web_fetch"]) == {"browser"}


def test_the_session_can_turn_the_tool_off():
    done = decide({"disabled_tools": ["browser"]}, {"action": "read"}, preview(PUBLIC), "bypassPermissions", {})
    assert done.decision == "deny" and done.source == "disabled"


# The engine's refusals ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("reason", ["private_address", "unsupported_address", "credential_field", "bot_check", "download",
                                    "dialog", "file_chooser", "permission_prompt", "stale_ref", "not_open", "tab_limit"])
@pytest.mark.parametrize("mode", MODES)
def test_the_engines_refusals_hold_in_every_mode_and_with_every_rule(mode, reason):
    rules = [allow(PUBLIC), allow(LOOPBACK)]
    done = decide({}, {"action": "type", "ref": "r1", "text": "x"},
                  preview(PUBLIC, refused=reason, text=f"Not typed: {reason}."), mode, {"chat": rules, "user": rules})
    assert done.decision == "refuse" and done.source == "engine" and done.card is None
    assert done.message == f"Not typed: {reason}."


def test_an_open_refused_by_the_engine_never_asks_for_the_host():
    done = decide({}, {"action": "open", "url": "http://10.0.0.5/"},
                  preview(None, refused="private_address", text="Not opened: private address."), "manual", {})
    assert done.decision == "refuse" and done.card is None


def test_the_hosts_not_allowed_refusal_of_a_preview_is_a_permission_matter_not_a_refusal():
    done = decide({}, {"action": "open", "url": f"https://{PUBLIC}/"},
                  preview(PUBLIC, refused="host_not_allowed", text="x is not allowed yet."), "manual", {})
    assert done.decision == "ask"
    done = decide({}, {"action": "open", "url": f"https://{PUBLIC}/"},
                  preview(PUBLIC, refused="host_not_allowed", text="x"), "manual", {"chat": [allow(PUBLIC)]})
    assert done.decision == "allow"


def test_close_with_no_page_open_needs_nothing():
    for mode in MODES:
        assert decide({}, {"action": "close"}, preview(None), mode, {}).decision == "allow"


# The effective arguments the permission layer sees --------------------------------------------------------------

def test_the_effective_arguments_carry_the_host_key_and_the_submit_flag_and_cannot_be_forged():
    done = decide({}, {"action": "click", "ref": "r2", "host": "evil.test", "submits": False}, preview(PUBLIC, submits=True),
                  "auto", {"chat": [allow(PUBLIC)]})
    assert done.arguments["host"] == PUBLIC and done.arguments["submits"] is True
    assert done.decision == "ask"


def test_the_permission_layer_works_on_the_effective_arguments_through_evaluate():
    done = decide({}, {"action": "read"}, preview(PUBLIC), "manual", {})
    assert evaluate("manual", "browser", done.arguments).decision == "ask"
    assert evaluate("manual", "browser", done.arguments, chat_rules=[allow(PUBLIC)]).decision == "allow"
    assert tool_kind("browser") == "browser" and TOOL_KINDS["browser"] == "browser"
    assert tool_decision("bypassPermissions", "browser", done.arguments) == "allow"
    assert tool_decision("manual", "browser", done.arguments) == "ask"
    assert tool_decision("plan", "browser", {"action": "click", "host": PUBLIC}) == "deny"


# make_allowed: the engine's host check --------------------------------------------------------------------------

def test_make_allowed_follows_the_rules_the_mode_and_the_hosts_allowed_once():
    rules = {"chat": [allow(PUBLIC), deny("bad.test")]}
    check = make_allowed({}, "manual", rules)
    assert check(PUBLIC) is True and check("other.test") is False and check("bad.test") is False
    assert make_allowed({}, "manual", rules, once={"other.test"})("other.test") is True
    assert make_allowed({}, "manual", rules, once={"bad.test"})("bad.test") is False  # a deny rule still wins
    open_mode = make_allowed({}, "bypassPermissions", rules)
    assert open_mode("other.test") is True and open_mode("bad.test") is False
    assert make_allowed({}, "dontAsk", rules)("other.test") is False
    assert make_allowed({}, "plan", rules)(PUBLIC) is True
    assert make_allowed({"disabled_tools": ["browser"]}, "bypassPermissions", {})("x.test") is False


# The card -------------------------------------------------------------------------------------------------------

def card(arguments, host, mode="manual", rules=(), submits=False, label=None):
    done = decide({}, arguments, preview(host, submits), mode, {"chat": list(rules)}, label=label)
    assert done.decision == "ask", done
    return done.card


def test_a_card_for_a_new_host_offers_once_and_for_this_chat():
    shown = card({"action": "open", "url": f"https://{PUBLIC}/guide?x=1"}, PUBLIC)
    assert shown["title"] == f"Browse {PUBLIC}" and shown["host"] == PUBLIC
    assert shown["action"] == "open" and shown["detail"] == f"Open https://{PUBLIC}/guide?x=1"
    assert shown["choices"] == [{"id": "once", "label": "Allow once"},
                                {"id": "chat", "label": f"Allow {PUBLIC} in this chat"}]
    assert shown["rule_options"] == {"domain": PUBLIC}
    local = card({"action": "open", "url": "http://localhost:3000/"}, LOOPBACK)
    assert local["title"] == "Browse localhost:3000" and local["choices"][1]["label"] == "Allow localhost:3000 in this chat"


def test_a_card_for_a_click_on_an_allowed_host_offers_only_once():
    shown = card({"action": "click", "ref": "r7"}, PUBLIC, rules=[allow(PUBLIC)], label='button "Delete account"')
    assert shown["title"] == f"Click on {PUBLIC}"
    assert shown["detail"] == 'Click r7 (button "Delete account")'
    assert shown["choices"] == [{"id": "once", "label": "Allow once"}]


def test_a_card_for_a_submit_is_titled_for_the_form():
    shown = card({"action": "type", "ref": "r3", "text": "hello world", "submit": True}, PUBLIC, mode="auto",
                 rules=[allow(PUBLIC)], submits=True, label='textbox "Search"')
    assert shown["title"] == f"Submit a form on {PUBLIC}"
    assert shown["detail"] == 'Type "hello world" (11 characters) into r3 (textbox "Search"), then press Enter'
    shown = card({"action": "click", "ref": "r9"}, PUBLIC, mode="auto", rules=[allow(PUBLIC)], submits=True,
                 label='button "Send"')
    assert shown["title"] == f"Submit a form on {PUBLIC}" and shown["detail"] == 'Click r9 (button "Send")'


def test_typed_text_shows_its_length_and_never_text_for_a_credential_like_field():
    shown = card({"action": "type", "ref": "r3", "text": "hunter2"}, PUBLIC, rules=[allow(PUBLIC)],
                 label='textbox "Password"')
    assert "hunter2" not in str(shown) and "7 characters" in shown["detail"]
    long = card({"action": "type", "ref": "r3", "text": "x" * 500}, PUBLIC, rules=[allow(PUBLIC)])
    assert "500 characters" in long["detail"] and len(long["detail"]) < 300


def test_select_and_read_actions_have_a_plain_detail():
    assert card({"action": "select", "ref": "r4", "value": "Blue"}, PUBLIC, rules=[allow(PUBLIC)])["detail"] == (
        'Choose "Blue" in r4')
    assert card({"action": "read"}, PUBLIC)["detail"] == "read"
    assert card({"action": "find", "query": "price"}, PUBLIC)["detail"] == 'find "price"'


def test_a_denied_or_allowed_decision_has_no_card():
    assert decide({}, {"action": "read"}, preview(PUBLIC), "auto", {"chat": [allow(PUBLIC)]}).card is None
    assert decide({}, {"action": "read"}, preview(PUBLIC), "auto", {"chat": [deny(PUBLIC)]}).card is None


# Rules: labels, matching, normalising, remembering -----------------------------------------------------------------

def test_rule_labels():
    assert rule_label(allow(LOOPBACK)) == "Browse localhost:3000"
    assert rule_label(allow(PUBLIC)) == "Browse docs.example.com"
    assert rule_label(deny("example.com")) == "Browse example.com and its subdomains"
    assert rule_label(deny(LOOPBACK)) == "Browse localhost:3000"
    assert rule_label({"behavior": "deny", "tool": "browser"}) == "browsing"


def test_rule_matches_follows_the_web_domain_rules():
    def match(rule, host):
        return rule_matches(rule, "browser", {"action": "read", "host": host})
    assert match(allow(PUBLIC), PUBLIC) and not match(allow(PUBLIC), "a." + PUBLIC)
    assert match(deny("example.com"), "example.com") and match(deny("example.com"), PUBLIC)
    assert not match(deny("example.com"), "badexample.com")
    assert match(ask("example.com"), "x.example.com")
    assert match(deny(LOOPBACK), LOOPBACK) and not match(deny(LOOPBACK), "localhost:3001")
    assert not match(deny("localhost:3000"), "a.localhost:3000")
    # No host to check: an allow rule never matches; a deny or ask rule does.
    assert not rule_matches(allow(PUBLIC), "browser", {"action": "read"})
    assert rule_matches(deny(PUBLIC), "browser", {"action": "read"})
    assert not rule_matches(allow(PUBLIC), "web_fetch", {"url": f"https://{PUBLIC}"})


def test_a_rule_for_the_open_action_reads_the_url_when_no_host_is_set():
    assert rule_matches(allow(PUBLIC), "browser", {"action": "open", "url": f"https://{PUBLIC}/a"})
    assert not rule_matches(allow(PUBLIC), "browser", {"action": "open", "url": f"https://x.{PUBLIC}/a"})


def test_normalize_rule_for_the_browser():
    context = {"resolve_path": lambda value: value}
    assert normalize_rule({"tool": "browser", "domain": "Docs.Example.com."}, **context) == {
        "behavior": "allow", "domain": PUBLIC, "tool": "browser"}
    assert normalize_rule({"tool": "browser", "domain": "http://localhost:3000/x"}, **context)["domain"] == LOOPBACK
    assert normalize_rule({"tool": "browser", "domain": "localhost:3000"}, **context)["domain"] == LOOPBACK
    assert normalize_rule({"tool": "browser", "domain": "[::1]:8080"}, **context)["domain"] == "[::1]:8080"
    assert normalize_rule({"tool": "browser", "behavior": "deny", "domain": "example.com"}, **context)["domain"] == "example.com"
    assert normalize_rule({"tool": "browser", "behavior": "deny"}, **context) == {"behavior": "deny", "tool": "browser"}
    for bad in ({"tool": "browser"}, {"tool": "browser", "behavior": "ask", "path": "/x"},
                {"tool": "browser", "domain": ""}, {"tool": "browser", "domain": "example.com:8443"},
                {"tool": "browser", "domain": "ftp://example.com"}, {"tool": "browser", "domain": 5}):
        with pytest.raises(ValueError):
            normalize_rule(bad, **context)


def test_remembering_a_card_stores_the_host_key():
    assert rule_for("browser", {"action": "open", "url": f"https://{PUBLIC}/x", "host": PUBLIC}) == {
        "tool": "browser", "domain": PUBLIC}
    assert rule_for("browser", {"action": "open", "url": "http://localhost:3000/x"}) == {
        "tool": "browser", "domain": LOOPBACK}
    assert rule_for("browser", {"action": "read", "host": "[::1]:8080"}) == {"tool": "browser", "domain": "[::1]:8080"}
    assert rule_for("browser", {"action": "read"}) is None
    assert default_rule("browser", {"action": "read", "host": PUBLIC}) == {
        "behavior": "allow", "domain": PUBLIC, "tool": "browser"}
    assert default_rule("browser", {"action": "click", "ref": "r1"}) is None
    assert card_rule_options("browser", {"action": "read", "host": LOOPBACK}) == {"domain": LOOPBACK}


# The tool in profiles, agent files and the tool lists ----------------------------------------------------------------

def test_the_definition_matches_the_contract():
    function = BROWSER_TOOL["function"]
    assert BROWSER_TOOL["type"] == "function" and function["name"] == "browser"
    parameters = function["parameters"]
    assert parameters["required"] == ["action"] and parameters["additionalProperties"] is False
    assert parameters["properties"]["action"]["enum"] == [
        "open", "read", "find", "click", "type", "select", "scroll", "back", "forward", "tabs", "screenshot", "console",
        "network", "close"]
    assert parameters["properties"]["direction"]["enum"] == ["up", "down", "top", "bottom"]
    kinds = {"url": "string", "ref": "string", "text": "string", "value": "string", "query": "string", "tab": "string",
             "new_tab": "boolean", "submit": "boolean", "full_page": "boolean", "errors_only": "boolean"}
    for name, kind in kinds.items():
        assert parameters["properties"][name]["type"] == kind
    assert set(parameters["properties"]) == {"action", "direction", *kinds}
    assert "untrusted" in function["description"].lower() and len(function["description"]) < 160


def test_the_tool_is_not_in_any_list_offered_today():
    assert "browser" not in {item["function"]["name"] for item in FEATURE_TOOLS}


def test_restricted_profiles_leave_the_browser_out_like_web_fetch():
    # A read-only profile cannot split the tool by action, and web_fetch is not in these profiles either.
    assert "browser" not in READ_TOOLS and "browser" not in FILE_TOOLS and "web_fetch" not in READ_TOOLS
    assert tool_allowed({"tool_profile": "inherit"}, "browser") is True
    assert tool_allowed({"tool_profile": "read_only"}, "browser") is False
    assert tool_allowed({"tool_profile": "file_editor"}, "browser") is False
    assert tool_allowed({"tool_profile": "inherit", "disabled_tools": ["browser"]}, "browser") is False
    assert tool_allowed({"tool_profile": "inherit", "agent_tools": ["read_file"]}, "browser") is False
    assert tool_allowed({"tool_profile": "inherit", "agent_tools": ["browser"]}, "browser") is True
    assert tool_allowed({"tool_profile": "inherit", "agent_denied_tools": ["browser"]}, "browser") is False


def test_agent_files_can_allow_or_deny_the_browser():
    from local_agent.custom_agents import TOOL_ALIASES, known_tools, parse_agent
    assert "browser" in known_tools() and TOOL_ALIASES["Browser"] == "browser"
    head = "---\nname: surfer\ndescription: Browses.\nprofile: {profile}\ntools: [read_file, Browser]\n{extra}---\nBody.\n"
    allowed = parse_agent(head.format(profile="inherit", extra=""), "surfer")
    assert allowed["tools"] == ["read_file", "browser"]
    denied = parse_agent(head.format(profile="inherit", extra="disallowed_tools: [browser]\n"), "surfer")
    assert denied["tools"] == ["read_file"] and denied["disallowed_tools"] == ["browser"]
    narrow = parse_agent(head.format(profile="read_only", extra=""), "surfer")
    assert narrow["tools"] == ["read_file"] and any("browser" in note for note in narrow["notes"])


def test_every_mode_label_is_covered_by_the_table():
    assert set(MODES) == set(MODE_LABELS)
