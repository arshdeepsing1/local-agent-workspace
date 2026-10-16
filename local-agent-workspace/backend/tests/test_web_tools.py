"""The web_fetch and web_search tools in a chat: offering, approval cards and domain rules in every mode,
paging through get_tool_output, Settings → Web access and redaction (agents.py, rules.py, web_fetch.py).

Offline: fetches reach a local http.server through an injected backend; searches use httpx.MockTransport."""
import asyncio
import json

import httpx
import pytest

from local_agent import rules
from local_agent.agents import AgentManager
from local_agent.config import Settings
from local_agent.permissions import denied_tools, evaluate, tool_decision
from local_agent.rules import normalize_rule, rule_label, rule_matches
from local_agent.store import Store
from local_agent.tools import WorkspaceTools
from test_web_fetch import Site, backend_for

TOKEN = "synthetic-workspace-token"
SEARCH_KEY = "synthetic-search-key-123"


@pytest.fixture
def site():
    server = Site()
    server.routes["/page"] = (200, {"Content-Type": "text/html"}, b"<title>Docs</title><p>Hello from the docs.</p>")
    yield server
    server.server.shutdown()
    server.server.server_close()


@pytest.fixture
async def chat(tmp_path, monkeypatch, site):
    monkeypatch.setattr("local_agent.config.APP_ROOT", tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    credentials = tmp_path / "credentials.env"
    credentials.write_text(f"BRAVE_API_KEY={SEARCH_KEY}\n")
    settings = Settings(tmp_path / "state")
    settings.env = {}
    settings.values.update(workspace=str(project), env_file=str(credentials), model="test-model")
    settings.credentials = lambda: ("https://gateway.example", TOKEN)
    store = Store(settings.state_dir / "web.sqlite3")
    manager = AgentManager(store, settings)
    manager.web_options.update(backend=lambda: backend_for(site), ports=(site.port,))
    session = store.create(settings.values)
    session.update(title="Web", title_generated=True, permission_mode="manual")
    store.save(session)
    manager.live[session["id"]] = session
    tools = WorkspaceTools(str(project))
    yield manager, session, tools, site
    for session_id in list(manager.tasks):
        await manager.stop(session_id)
    await manager.jobs.shutdown()
    store.db.close()


async def card(manager, name):
    for _ in range(500):
        for key in list(manager.pending):
            session = manager.get(key[0])
            event = next((item for item in session["events"] if item["id"] == key[1]), None)
            if event is not None and event["name"] == name:
                return event
        await asyncio.sleep(0.01)
    raise AssertionError(f"Expected a {name} card")


def cards_of(session, name):
    return [event for event in session["events"] if event.get("name") == name]


async def answered(manager, session, tools, name, arguments, **answer):
    """Run one call, answer its card with decide(**answer), and return (output, card)."""
    task = asyncio.create_task(manager.execute_tool(session, tools, name, arguments, f"call-{len(session['events'])}"))
    event = await card(manager, name)
    shown = dict(event)
    manager.decide(session["id"], event["id"], **{"allowed": True, **answer})
    return await task, shown


def fetched(output):
    result = json.loads(output)
    assert "Untrusted web content" in result["note"]
    return result


# Rules ---------------------------------------------------------------------------------------------------------


def test_domain_rules_allow_one_host_and_deny_its_subdomains():
    context = {"resolve_path": lambda value: value}
    allow = normalize_rule({"tool": "web_fetch", "behavior": "allow", "domain": "Docs.Python.org"}, **context)
    deny = normalize_rule({"tool": "web_fetch", "behavior": "deny", "domain": "example.com"}, **context)
    assert allow == {"behavior": "allow", "domain": "docs.python.org", "tool": "web_fetch"}
    assert rule_matches(allow, "web_fetch", {"url": "https://docs.python.org/3/"}, **context)
    assert not rule_matches(allow, "web_fetch", {"url": "https://www.docs.python.org/"}, **context)
    assert not rule_matches(allow, "web_fetch", {"url": "not a url"}, **context)
    assert rule_matches(deny, "web_fetch", {"url": "https://api.example.com/x"}, **context)
    assert rule_matches(deny, "web_fetch", {"url": "not a url"}, **context)  # unchecked never escapes a deny
    assert not rule_matches(deny, "web_fetch", {"url": "https://notexample.com/"}, **context)
    assert rule_label(allow) == "fetches from docs.python.org"
    assert rule_label(deny) == "fetches from example.com and its subdomains"
    with pytest.raises(ValueError, match="must name one domain"):
        normalize_rule({"tool": "web_fetch", "behavior": "allow"}, **context)
    with pytest.raises(ValueError, match="only name a domain"):
        normalize_rule({"tool": "web_fetch", "behavior": "deny", "path": "/srv/other"}, **context)
    with pytest.raises(ValueError, match="no path, hook or domain"):
        normalize_rule({"tool": "run_command", "behavior": "deny", "domain": "example.com"}, **context)
    with pytest.raises(ValueError, match="has no other fields"):
        normalize_rule({"tool": "delegate_task", "behavior": "allow", "domain": "example.com"}, **context)
    assert normalize_rule({"tool": "web_search", "behavior": "allow"}, **context) == {"behavior": "allow",
                                                                                       "tool": "web_search"}
    # A deny rule without a domain covers every fetch, so web_fetch is not offered at all.
    every = normalize_rule({"tool": "web_fetch", "behavior": "deny"}, **context)
    assert denied_tools([every], [], [], ["web_fetch", "web_search"]) == {"web_fetch"}
    assert denied_tools([deny], [], [], ["web_fetch"]) == set()
    assert rules.default_rule("web_fetch", {"url": "https://Docs.Python.org/"}) == {
        "behavior": "allow", "domain": "docs.python.org", "tool": "web_fetch"}


@pytest.mark.parametrize("mode, decision", [("manual", "ask"), ("auto", "ask"), ("acceptEdits", "ask"), ("plan", "ask"),
                                            ("bypassPermissions", "allow"), ("dontAsk", "deny")])
def test_web_tools_ask_in_every_mode_but_bypass(mode, decision):
    arguments = {"url": "https://docs.python.org/"}
    assert evaluate(mode, "web_fetch", arguments).decision == decision
    assert evaluate(mode, "web_search", {"query": "x"}).decision == decision
    rule = {"tool": "web_fetch", "domain": "docs.python.org", "behavior": "allow"}
    # A domain rule allows it, except in Plan, which always shows the card.
    expected = "ask" if mode == "plan" else "allow"
    assert evaluate(mode, "web_fetch", arguments, project_rules=[rule]).decision == expected
    assert tool_decision(mode, "web_fetch", arguments) in ("ask", "allow")


def test_a_deny_rule_for_a_domain_wins_in_bypass():
    deny = {"tool": "web_fetch", "domain": "example.com", "behavior": "deny"}
    allow = {"tool": "web_fetch", "domain": "api.example.com", "behavior": "allow"}
    verdict = evaluate("bypassPermissions", "web_fetch", {"url": "https://api.example.com/"}, chat_rules=[allow],
                       user_rules=[deny])
    assert verdict.decision == "deny" and "fetches from example.com" in verdict.message


# Fetching in a chat ------------------------------------------------------------------------------------------


async def test_a_fetch_card_names_the_domain_and_allow_once_asks_again(chat):
    manager, session, tools, site = chat
    output, shown = await answered(manager, session, tools, "web_fetch", {"url": site.url("/page")})
    result = fetched(output)
    assert result["title"] == "Docs" and result["output"] == "Hello from the docs."
    assert shown["web_domain"] == "site.example" and shown["preview"].startswith("Fetch from site.example:")
    assert shown["rememberable"] == ["chat", "project", "user"] and shown["rule_options"] == {"domain": "site.example"}
    task = asyncio.create_task(manager.execute_tool(session, tools, "web_fetch", {"url": site.url("/page")}, "again"))
    event = await card(manager, "web_fetch")  # Allow once: the next fetch asks again.
    manager.decide(session["id"], event["id"], False)
    assert "User declined this web fetch" in await task


async def test_allow_the_domain_in_this_chat_then_other_domains_still_ask(chat):
    manager, session, tools, site = chat
    await answered(manager, session, tools, "web_fetch", {"url": site.url("/page")}, remember=True)
    assert session["allow_rules"] == [{"tool": "web_fetch", "domain": "site.example"}]
    output = await manager.execute_tool(session, tools, "web_fetch", {"url": site.url("/page")}, "second")
    assert fetched(output)["output"] == "Hello from the docs."
    assert cards_of(session, "web_fetch")[-1]["allowed_by_rule"] is True
    task = asyncio.create_task(manager.execute_tool(session, tools, "web_fetch", {"url": site.url("/page", "other.example")},
                                                    "third"))
    event = await card(manager, "web_fetch")
    assert event["web_domain"] == "other.example"
    manager.decide(session["id"], event["id"], False)
    await task


async def test_always_allow_saves_a_domain_rule_for_all_projects(chat):
    manager, session, tools, site = chat
    await answered(manager, session, tools, "web_fetch", {"url": site.url("/page")}, scope="user")
    assert manager.rules.rules(session["workspace"])["user"] == [
        {"behavior": "allow", "domain": "site.example", "tool": "web_fetch"}]
    await manager.execute_tool(session, tools, "web_fetch", {"url": site.url("/page")}, "next")
    assert cards_of(session, "web_fetch")[-1]["rule_scope"] == "user"


async def test_plan_mode_asks_even_with_a_domain_rule_and_bypass_never_asks(chat):
    manager, session, tools, site = chat
    session["allow_rules"] = [{"tool": "web_fetch", "domain": "site.example"}]
    session["permission_mode"] = "plan"
    output, _ = await answered(manager, session, tools, "web_fetch", {"url": site.url("/page")})
    assert fetched(output)["status"] == 200
    session.update(permission_mode="bypassPermissions", allow_rules=[])
    output = await manager.execute_tool(session, tools, "web_fetch", {"url": site.url("/page")}, "bypass")
    assert fetched(output)["status"] == 200 and not manager.pending


async def test_dont_ask_fetches_only_what_a_rule_allows(chat):
    manager, session, tools, site = chat
    session["permission_mode"] = "dontAsk"
    output = await manager.execute_tool(session, tools, "web_fetch", {"url": site.url("/page")}, "blocked")
    assert output.startswith("Not run: Don't ask mode") and site.requests == []
    session["allow_rules"] = [{"tool": "web_fetch", "domain": "site.example"}]
    assert fetched(await manager.execute_tool(session, tools, "web_fetch", {"url": site.url("/page")}, "ok"))


async def test_a_refused_url_fails_before_any_card_or_lookup(chat):
    manager, session, tools, site = chat
    looked_up = []

    async def resolver(host, port):
        looked_up.append(host)
        return ["127.0.0.1"]
    manager.web_options["backend"] = lambda: backend_for(site, {"x": resolver})
    for url in ("http://127.0.0.1/", "http://localhost/admin", f"https://example.com/?q={TOKEN}"):
        output = json.loads(await manager.execute_tool(session, tools, "web_fetch", {"url": url}, url))
        assert output["error_kind"] == "web_refused" and "curl" in output["hint"]
    assert not manager.pending and looked_up == [] and site.requests == []
    assert TOKEN not in json.dumps(session["events"][-1].get("output"))


async def test_a_redirect_elsewhere_comes_back_for_a_new_call(chat):
    manager, session, tools, site = chat
    session["permission_mode"] = "bypassPermissions"
    site.routes["/moved"] = (302, {"Location": site.url("/page", "other.example")}, b"")
    result = json.loads(await manager.execute_tool(session, tools, "web_fetch", {"url": site.url("/moved")}, "r"))
    assert result["redirect"] == site.url("/page", "other.example") and len(site.requests) == 1


async def test_a_long_page_is_read_on_with_get_tool_output(chat):
    manager, session, tools, site = chat
    session["permission_mode"] = "bypassPermissions"
    body = "".join(f"<p>Paragraph {number:05d} of the long page.</p>" for number in range(2000)).encode()
    site.routes["/long"] = (200, {"Content-Type": "text/html"}, body)
    assert "get_tool_output" not in [tool["function"]["name"] for tool in manager.feature_definitions(session)]
    result = fetched(await manager.execute_tool(session, tools, "web_fetch", {"url": site.url("/long")}, "long"))
    assert result["result_id"].startswith("out-") and result["next_offset"] == len(result["output"])
    assert session["tool_output_offered"] is True
    assert "get_tool_output" in [tool["function"]["name"] for tool in manager.feature_definitions(session)]
    page = json.loads(await manager.execute_tool(session, tools, "get_tool_output", {
        "result_id": result["result_id"], "offset": result["next_offset"]}, "more"))
    assert page["offset"] == result["next_offset"] and "Untrusted web content" in page["note"]
    whole = result["output"] + page["output"]
    assert whole.startswith("Paragraph 00000 of the long page.\n\nParagraph 00001") and "Paragraph 00300" in whole


async def test_fetched_text_is_redacted_before_the_model_sees_it(chat):
    manager, session, tools, site = chat
    session["permission_mode"] = "bypassPermissions"
    site.routes["/leak"] = (200, {"Content-Type": "text/plain"}, f"token={TOKEN} and {SEARCH_KEY}".encode())
    manager.settings.values["web_search_provider"] = "brave"
    output = await manager.execute_tool(session, tools, "web_fetch", {"url": site.url("/leak")}, "leak")
    assert TOKEN not in output and SEARCH_KEY not in output and "[REDACTED]" in output


async def test_settings_can_turn_web_fetch_off_and_an_offered_tool_is_refused(chat):
    manager, session, tools, site = chat
    names = [tool["function"]["name"] for tool in manager.offered_tools(session, [])[0]]
    assert "web_fetch" in names and "web_search" not in names and session["web_fetch_tool"] is True
    manager.settings.values["web_fetch"] = False
    # Offered before in this chat: it stays offered (a stable tool list), and a call is refused.
    assert "web_fetch" in [tool["function"]["name"] for tool in manager.offered_tools(session, [])[0]]
    output = await manager.execute_tool(session, tools, "web_fetch", {"url": site.url("/page")}, "off")
    assert output.startswith("Not fetched: web fetch is turned off") and site.requests == []
    fresh = manager.store.create(manager.settings.values)
    assert "web_fetch" not in [tool["function"]["name"] for tool in manager.offered_tools(fresh, [])[0]]


# Searching ---------------------------------------------------------------------------------------------------


def brave(seen):
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"web": {"results": [
            {"title": "Docs", "url": "https://docs.example.com/", "description": f"echo {SEARCH_KEY}"}]}})
    return httpx.MockTransport(respond)


async def test_the_first_search_shows_its_query_then_searches_run_for_the_chat(chat):
    manager, session, tools, _ = chat
    seen = []
    manager.web_options["search_transport"] = brave(seen)
    assert "web_search" not in [tool["function"]["name"] for tool in manager.feature_definitions(session)]
    manager.settings.values["web_search_provider"] = "brave"
    assert "web_search" in [tool["function"]["name"] for tool in manager.feature_definitions(session)]
    output, shown = await answered(manager, session, tools, "web_search", {"query": "python   asyncio"})
    assert shown["preview"].startswith("Search the web with Brave Search:\npython asyncio")
    assert shown["web_provider"] == "Brave Search"
    result = json.loads(output)
    assert result["results"][0]["url"] == "https://docs.example.com/" and SEARCH_KEY not in output
    assert {"tool": "web_search"} in session["allow_rules"]
    await manager.execute_tool(session, tools, "web_search", {"query": "second", "max_results": 3}, "s2")
    assert cards_of(session, "web_search")[-1]["allowed_by_rule"] is True and len(seen) == 2
    assert seen[1].url.params["count"] == "3" and seen[1].headers["X-Subscription-Token"] == SEARCH_KEY


async def test_a_search_without_its_key_says_what_to_add_before_any_card(chat):
    manager, session, tools, _ = chat
    manager.settings.values.update(web_search_provider="tavily")
    output = json.loads(await manager.execute_tool(session, tools, "web_search", {"query": "q"}, "s"))
    assert "Add TAVILY_API_KEY=… to your credential file" in output["message"] and not manager.pending


async def test_a_key_entry_holding_the_databricks_token_is_never_sent(chat, tmp_path):
    manager, session, tools, _ = chat
    (tmp_path / "credentials.env").write_text(f"BRAVE_API_KEY={TOKEN}\n")
    manager.settings.values["web_search_provider"] = "brave"
    seen = []
    manager.web_options["search_transport"] = brave(seen)
    session["permission_mode"] = "bypassPermissions"
    output = json.loads(await manager.execute_tool(session, tools, "web_search", {"query": "q"}, "s"))
    assert "holds your Databricks token" in output["message"] and seen == []


# Settings → Web access ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("values, message", [
    ({"web_search_provider": "bing"}, "none, brave, tavily or searxng"),
    ({"web_search_provider": "searxng"}, "Enter your SearXNG address"),
    ({"web_search_provider": "searxng", "web_search_url": "ftp://127.0.0.1"}, "http or https URL"),
    ({"web_search_key": "bsa-lowercase-key"}, "name of an entry in your credential file"),
    ({"web_search_key": "DBRICKS_TOKEN"}, "Databricks credentials"),
    ({"web_fetch": "yes"}, "Web fetch must be on or off"),
])
def test_web_access_settings_are_checked(chat, values, message):
    manager = chat[0]
    with pytest.raises(ValueError, match=message):
        manager.settings.update(values)


def test_the_key_setting_is_a_name_never_the_key_itself(chat, tmp_path):
    manager = chat[0]
    (tmp_path / "credentials.env").write_text("BRAVE_API_KEY=ABCDEFGH12345678\n")
    with pytest.raises(ValueError, match="not the key itself"):
        manager.settings.update({"web_search_provider": "brave", "web_search_key": "ABCDEFGH12345678"})
    saved = manager.settings.update({"web_search_provider": "searxng", "web_search_url": " http://127.0.0.1:8888/ ",
                                     "web_search_key": ""})
    assert saved["web_search_url"] == "http://127.0.0.1:8888" and saved["web_search_status"] == {
        "provider": "SearXNG", "ready": True}
    status = manager.settings.update({"web_search_provider": "brave", "web_search_url": "not used"})["web_search_status"]
    assert manager.settings.values["web_search_url"] == ""
    assert status == {"provider": "Brave Search", "ready": True, "key_name": "BRAVE_API_KEY"}
    assert "ABCDEFGH12345678" not in json.dumps(manager.settings.public())
