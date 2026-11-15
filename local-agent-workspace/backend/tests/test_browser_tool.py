"""The integration layer of the browser tool (browser_tool.py): the 32-tool fit, the BrowserManager (sessions per chat,
the idle close, argument checks, results, screenshots, errors, Stop) and the status.

Most tests use a fake session, so no browser is needed. The tests marked with the engine run against the loopback pages of
the browser_pages fixture and are skipped, with the reason, when Chromium cannot start (require_browser)."""
import asyncio
import itertools
import json
import logging
import os
import stat
from types import SimpleNamespace

import pytest

from local_agent import browser_tool
from local_agent.browser import BrowserResult, BrowserUnavailable, browser_status
from local_agent.browser_tool import HELPER_PRIORITY, BrowserManager, fit_tools, screenshot_message, status
from local_agent.result_store import ResultStore
from local_agent.uploads import Uploads
from local_agent.web_fetch import UNTRUSTED
from tests.test_browser_engine import make, require_browser  # noqa: F401  (make is used by the engine tests below)

PIXEL = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63fcffff3f"
                      "0005fe02fea7d6a2f30000000049454e44ae426082")
VISION = "databricks-claude-opus-5-5"
NO_VISION = "databricks-gpt-oss-120b"


def tool(name):
    return {"type": "function", "function": {"name": name}}


def tools(*names):
    return [tool(name) for name in names]


def names(items):
    return [item["function"]["name"] for item in items]


# fit_tools --------------------------------------------------------------------------------------------------------

CORE = [f"core{n:02d}" for n in range(17)]
FEATURE = [f"feature{n:02d}" for n in range(11)]
HELPERS = ["web_search", "get_tool_output", "get_subagent_result", "report_findings"]
MCP = [f"mcp__server__tool{n:02d}" for n in range(10)]


def fit(core=CORE, feature=FEATURE, helpers=(), browser=None, mcp=(), **kwargs):
    offered, left_out = fit_tools(tools(*core), tools(*feature), tools(*helpers), tool("browser") if browser else None,
                                  tools(*mcp), **kwargs)
    return names(offered), left_out


def test_without_browsing_the_order_is_core_feature_helpers_then_mcp():
    offered, left_out = fit(helpers=["get_tool_output"], mcp=MCP[:3])
    assert offered == [*CORE, *FEATURE, "get_tool_output", *MCP[:3]] and left_out == []


def test_the_usual_chat_keeps_one_slot_for_mcp_and_names_the_rest():
    offered, left_out = fit(mcp=MCP)
    assert offered == [*CORE, *FEATURE, *MCP[:4]] and left_out == MCP[4:]


def test_the_largest_set_without_browsing_is_exactly_32_and_cuts_every_mcp_tool():
    offered, left_out = fit(helpers=HELPERS, mcp=MCP)
    assert len(offered) == 32 and left_out == MCP
    assert offered == [*CORE, *FEATURE, *HELPERS]


def test_the_worst_case_with_browsing_keeps_the_browser_and_get_tool_output_and_cuts_mcp_first():
    offered, left_out = fit(helpers=HELPERS, browser=True, mcp=MCP)
    assert len(offered) == 32
    assert offered[:28] == [*CORE, *FEATURE] and offered[28] == "browser"
    assert "get_tool_output" in offered
    assert not any(name.startswith("mcp__") for name in offered)
    # One helper does not fit: the lowest in HELPER_PRIORITY. The notice names it before the MCP tools.
    assert left_out == ["report_findings", *MCP]
    assert HELPER_PRIORITY == ("get_tool_output", "get_subagent_result", "web_search", "report_findings")


def test_the_browser_comes_before_the_optional_helpers_but_get_tool_output_is_never_cut_for_it():
    offered, left_out = fit(core=CORE + ["c17", "c18", "c19"], helpers=HELPERS, browser=True)  # 31 before the browser
    assert "browser" not in offered and "browser" in left_out
    assert offered == [*CORE, "c17", "c18", "c19", *FEATURE, "get_tool_output"]  # 32 with get_tool_output first
    assert left_out[0] == "browser"


def test_when_get_tool_output_is_not_offered_yet_the_browser_holds_a_place_for_it():
    # The browser's long pages create the first kept result, which adds get_tool_output mid-turn.
    offered, left_out = fit(browser=True, mcp=MCP)
    assert offered == [*CORE, *FEATURE, "browser", *MCP[:2]] and left_out == MCP[2:]
    assert 32 - len(offered) == 1  # the place get_tool_output takes when it joins
    offered, left_out = fit(helpers=["web_search", "get_subagent_result", "report_findings"], browser=True)
    assert "get_subagent_result" in offered and "web_search" in offered and "report_findings" not in offered
    assert left_out == ["report_findings"] and len(offered) == 31


def test_the_browser_is_left_out_when_it_cannot_have_get_tool_output_beside_it():
    offered, left_out = fit(core=CORE + ["c17", "c18", "c19"], browser=True)  # 31 core and feature tools
    assert "browser" not in offered and left_out == ["browser"] and len(offered) == 31
    offered, left_out = fit(core=CORE + ["c17", "c18"], browser=True)  # 30: the browser and one place fit
    assert offered[-1] == "browser" and left_out == []


def test_reserved_helpers_hold_places_against_the_mcp_tools_only():
    offered, left_out = fit(mcp=MCP, reserve=HELPERS[1:])  # three helpers may still join
    assert offered == [*CORE, *FEATURE, MCP[0]] and left_out == MCP[1:]
    offered, _ = fit(helpers=HELPERS, mcp=MCP, reserve=HELPERS)  # already offered: they hold nothing twice
    assert len(offered) == 32
    # get_tool_output is held by the browser itself, so naming it in reserve does not count it twice.
    assert fit(browser=True, mcp=MCP, reserve=["get_tool_output"]) == fit(browser=True, mcp=MCP)


def test_core_and_feature_tools_over_the_limit_are_cut_from_the_end_by_name():
    core = [f"core{n:02d}" for n in range(30)]
    offered, left_out = fit(core=core, browser=True, mcp=MCP[:2])
    assert offered == [*core, *FEATURE[:2]]
    assert left_out == [*FEATURE[2:], "browser", *MCP[:2]]


def test_limit_is_a_parameter_and_tools_can_be_given_as_names():
    offered, left_out = fit_tools(["a", "b"], ["c"], ["get_tool_output"], "browser", ["m1", "m2"], limit=5)
    assert offered == ["a", "b", "c", "browser", "get_tool_output"] and left_out == ["m1", "m2"]
    assert fit_tools([], [], [], None, [], limit=3) == ([], [])


def test_fit_tools_does_not_change_its_inputs():
    core, mcp = tools(*CORE), tools(*MCP)
    snapshot = json.dumps([core, mcp])
    fit_tools(core, tools(*FEATURE), tools(*HELPERS), tool("browser"), mcp)
    assert json.dumps([core, mcp]) == snapshot


def test_fit_tools_exhaustively_never_passes_the_limit_and_keeps_the_priority():
    subsets = [list(combo) for size in range(len(HELPERS) + 1) for combo in itertools.combinations(HELPERS, size)]
    checked = 0
    for core_count, feature_count in itertools.product((0, 5, 12, 17, 20, 40), (0, 3, 9, 11, 14)):
        core, feature = [f"c{n}" for n in range(core_count)], [f"f{n}" for n in range(feature_count)]
        base = [*core, *feature]
        for helpers, browser, mcp_count in itertools.product(subsets, (False, True), (0, 1, 4, 10)):
            mcp = MCP[:mcp_count]
            offered, left_out = fit(core=core, feature=feature, helpers=helpers, browser=browser, mcp=mcp)
            checked += 1
            given = [*base, *(["browser"] if browser else []), *helpers, *mcp]
            assert len(offered) <= 32 and len(set(offered)) == len(offered)
            assert sorted(offered + left_out) == sorted(given)  # nothing lost, nothing invented
            assert offered == [name for name in given if name in set(offered)]  # the input order is kept
            if browser and "browser" in offered:
                assert ("get_tool_output" in offered) == ("get_tool_output" in helpers)  # a place is kept, not filled
                assert len(offered) + ("get_tool_output" not in helpers) <= 32  # its place stays free
            if browser and "browser" not in offered:
                assert len(base) >= 31  # only when it cannot have get_tool_output beside it
            offered_mcp = [name for name in offered if name.startswith("mcp__")]
            assert offered_mcp == mcp[:len(offered_mcp)]  # MCP tools are cut from the end
            if offered_mcp:  # MCP tools are the first to go: everything else is already in
                assert not [name for name in left_out if name != "browser" and not name.startswith("mcp__")]
            others = [name for name in offered if name in HELPERS and name != "get_tool_output"]
            if others and browser:
                assert ("browser" in offered) == (len(base) < 31)  # the browser comes before the optional helpers
            if len(base) > 32:
                assert offered == base[:32]
    assert checked > 1_000


# The manager with a fake session --------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


class FakeSession:
    """Stands in for BrowserSession: records calls and returns the planned BrowserResult."""
    made = []

    def __init__(self, profile_dir, **options):
        self.profile_dir, self.options = profile_dir, options
        self.state, self.closed, self.calls, self.previews = "ready", False, [], []
        self.extras = []  # {"submit_ok", "expect"} of each act call
        self.plans = []  # results the next act calls return before self.plan does
        self.plan = BrowserResult(f"{UNTRUSTED.format(host='x.test')}\nOpened.", host="x.test", tab="t1")
        self.label = None
        self.fail = None
        self.gate = None
        FakeSession.made.append(self)

    async def preview(self, action, **arguments):
        self.previews.append((action, arguments))
        if isinstance(self.fail, ValueError):
            raise self.fail
        return BrowserResult("", host="x.test", tab="t1", label=self.label)

    async def act(self, action, *, submit_ok=None, expect=None, **arguments):
        self.calls.append((action, arguments))
        self.extras.append({"submit_ok": submit_ok, "expect": expect})
        if self.gate is not None:
            self.state = "busy"
            await self.gate.wait()
            self.state = "ready"
        if self.fail is not None and not isinstance(self.fail, ValueError):
            raise self.fail
        return self.plans.pop(0) if self.plans else self.plan

    async def close(self):
        self.closed, self.state = True, "closed"


@pytest.fixture
def fake(tmp_path):
    FakeSession.made = []
    clock = Clock()
    manager = BrowserManager(tmp_path / "state", clock=clock, session_factory=FakeSession)
    manager.clock = clock
    return manager


def store_for(chat="chat"):
    results = ResultStore()
    return results, lambda text, note=None: results.add(chat, text, note)


async def run(manager, chat, arguments, **kwargs):
    saved = []

    def keep_image(data, mime, name):
        saved.append((data, mime, name))
        return {"upload": "a" * 64, "name": name, "media_type": mime, "width": 1, "height": 1, "bytes": len(data)}
    options = {"allowed": lambda key: True, "redact": lambda text: text, "store_result": store_for(chat)[1],
               "store_image": keep_image, **kwargs}
    outcome = await manager.run(chat, arguments, **options)
    outcome.saved_images = saved
    return outcome


async def test_one_session_per_chat_is_created_on_first_use_and_reused(fake):
    assert FakeSession.made == []
    first = await run(fake, "a", {"action": "open", "url": "https://x.test/"})
    second = await run(fake, "a", {"action": "read"})
    assert first.kind == second.kind == "result" and len(FakeSession.made) == 1
    assert [call[0] for call in FakeSession.made[0].calls] == ["open", "read"]
    assert fake.chats() == ["a"]


async def test_the_session_gets_the_settings_and_its_own_chats_profile_folder(tmp_path):
    FakeSession.made = []
    manager = BrowserManager(tmp_path / "state", headless=True, action_timeout=45, channel="chrome",
                             session_factory=FakeSession)
    await run(manager, "a", {"action": "read"})
    session = FakeSession.made[0]
    assert session.profile_dir == tmp_path / "state" / "browser-profiles" / "a" == manager.profile_dir_for("a")
    assert session.options["headless"] is True and session.options["action_timeout"] == 45
    assert session.options["channel"] == "chrome"
    assert callable(session.options["allowed"]) and callable(session.options["redact"])


async def test_every_profile_folder_is_private_and_nothing_is_made_before_the_first_use(tmp_path):
    manager = BrowserManager(tmp_path / "state", session_factory=FakeSession)
    assert not manager.profiles_dir.exists()
    await run(manager, "a", {"action": "read"})
    await run(manager, "b", {"action": "read"})
    for folder in (manager.profiles_dir, manager.profile_dir_for("a"), manager.profile_dir_for("b")):
        assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700, folder
    os.chmod(manager.profile_dir_for("a"), 0o755)
    other = BrowserManager(tmp_path / "state", session_factory=FakeSession)
    await run(other, "a", {"action": "read"})
    assert stat.S_IMODE(os.stat(other.profile_dir_for("a")).st_mode) == 0o700


async def test_a_chat_id_cannot_name_a_folder_outside_the_profiles_folder(tmp_path):
    manager = BrowserManager(tmp_path / "state", session_factory=FakeSession)
    for chat in ("../escape", "a/b", "..", "", "x" * 400):
        folder = manager.profile_dir_for(chat)
        assert manager.profiles_dir in folder.parents and folder.parent == manager.profiles_dir, chat


async def test_the_profile_folder_of_an_earlier_build_is_left_alone_and_logged_once(tmp_path, caplog):
    old = tmp_path / "state" / "browser-profile"
    old.mkdir(parents=True)
    (old / "Cookies").write_text("OLD-COOKIE")
    manager = BrowserManager(tmp_path / "state", session_factory=FakeSession)
    with caplog.at_level(logging.INFO, logger="local_agent.browser_tool"):
        await run(manager, "a", {"action": "read"})
        await run(manager, "b", {"action": "read"})
    assert (old / "Cookies").read_text() == "OLD-COOKIE"  # kept: it may hold the user's data
    assert [record for record in caplog.records if "browser-profile" in record.getMessage()
            and "not used" in record.getMessage()].__len__() == 1


async def test_several_chats_have_a_live_browser_at_once_and_the_cap_is_three(fake):
    for chat in ("a", "b", "c"):
        await run(fake, chat, {"action": "read"})
        fake.clock.now += 1
    assert fake.chats() == ["a", "b", "c"] and not any(session.closed for session in FakeSession.made)
    await run(fake, "d", {"action": "read"})  # the least recently used idle session goes first
    assert sorted(fake.chats()) == ["b", "c", "d"] and FakeSession.made[0].closed
    fake.clock.now += 1
    await run(fake, "b", {"action": "read"})  # b was used last, so c is next
    await run(fake, "e", {"action": "read"})
    assert sorted(fake.chats()) == ["b", "d", "e"] and FakeSession.made[2].closed


async def test_a_busy_session_is_never_closed_for_a_new_chat(fake):
    for chat in ("a", "b", "c"):
        await run(fake, chat, {"action": "read"})
    for session in FakeSession.made:
        session.gate = asyncio.Event()
    running = [asyncio.create_task(run(fake, chat, {"action": "read"})) for chat in ("a", "b", "c")]
    await asyncio.sleep(0.05)
    blocked = await run(fake, "d", {"action": "read"})
    assert blocked.kind == "error" and blocked.text == "Too many chats are using the browser. Try again shortly."
    assert not any(session.closed for session in FakeSession.made) and "d" not in fake.chats()
    FakeSession.made[0].gate.set()  # one becomes idle: the new chat takes its place
    await running[0]
    assert (await run(fake, "d", {"action": "read"})).kind == "result"
    for session in FakeSession.made[1:3]:
        session.gate.set()
    await asyncio.gather(*running[1:])
    assert FakeSession.made[0].closed and len(fake.chats()) == 3


async def test_one_chats_call_never_closes_another_chats_session(fake):
    await run(fake, "a", {"action": "read"})
    await run(fake, "b", {"action": "read"})
    assert not FakeSession.made[0].closed and not FakeSession.made[1].closed and fake.chats() == ["a", "b"]
    assert len(FakeSession.made) == 2 and FakeSession.made[0].profile_dir != FakeSession.made[1].profile_dir


async def test_deleting_a_chat_removes_its_profile_folder_and_an_idle_close_keeps_it(fake):
    await run(fake, "a", {"action": "read"})
    await run(fake, "b", {"action": "read"})
    (fake.profile_dir_for("a") / "Cookies").write_text("A-LOGIN")
    fake.clock.now += 700
    assert sorted(await fake.sweep()) == ["a", "b"] and (fake.profile_dir_for("a") / "Cookies").read_text() == "A-LOGIN"
    await run(fake, "a", {"action": "read"})
    assert await fake.close_chat("a") is True and not fake.profile_dir_for("a").exists()
    assert fake.profile_dir_for("b").exists()
    assert await fake.close_chat("b") is False and not fake.profile_dir_for("b").exists()  # no live session; the folder goes too


async def test_the_idle_close_runs_on_a_timer_without_a_new_turn(tmp_path):
    FakeSession.made = []
    clock = Clock()
    manager = BrowserManager(tmp_path / "s", clock=clock, session_factory=FakeSession, idle_limit=60, sweep_interval=0.05)
    await run(manager, "a", {"action": "read"})
    assert manager.chats() == ["a"] and manager.timer_running()
    clock.now += 61
    for _ in range(60):
        if not manager.chats():
            break
        await asyncio.sleep(0.05)
    assert manager.chats() == [] and FakeSession.made[0].closed
    for _ in range(40):
        if not manager.timer_running():
            break
        await asyncio.sleep(0.05)
    assert not manager.timer_running()  # it stops while no session is open
    await run(manager, "a", {"action": "read"})
    assert manager.timer_running()  # and starts again with the next one
    await manager.close_all()
    assert not manager.timer_running()


async def test_the_allowed_check_and_redactor_of_the_latest_call_reach_the_session(fake):
    await run(fake, "a", {"action": "read"}, allowed=lambda key: key == "one.test", redact=lambda text: text.upper())
    options = FakeSession.made[0].options
    assert options["allowed"]("one.test") is True and options["allowed"]("two.test") is False
    assert options["redact"]("secret") == "SECRET"
    await run(fake, "a", {"action": "read"}, allowed=lambda key: key == "two.test")
    assert options["allowed"]("two.test") is True and options["allowed"]("one.test") is False


async def test_sweep_closes_a_session_after_ten_idle_minutes(fake):
    await run(fake, "a", {"action": "read"})
    fake.clock.now += 599
    assert await fake.sweep() == [] and not FakeSession.made[0].closed
    fake.clock.now += 2
    assert await fake.sweep() == ["a"] and FakeSession.made[0].closed and fake.chats() == []
    assert await fake.sweep() == []
    await run(fake, "a", {"action": "read"})  # a later call starts a new session
    assert len(FakeSession.made) == 2 and not FakeSession.made[1].closed


async def test_a_call_resets_the_idle_time_and_a_busy_session_is_not_swept(fake):
    await run(fake, "a", {"action": "read"})
    fake.clock.now += 500
    await run(fake, "a", {"action": "read"})
    fake.clock.now += 500
    assert await fake.sweep() == []
    session = FakeSession.made[0]
    session.gate = asyncio.Event()
    running = asyncio.create_task(run(fake, "a", {"action": "read"}))
    await asyncio.sleep(0.05)
    fake.clock.now += 5_000
    assert await fake.sweep() == [] and not session.closed
    session.gate.set()
    await running


async def test_the_idle_limit_can_be_set(tmp_path):
    clock = Clock()
    manager = BrowserManager(tmp_path / "s", clock=clock, session_factory=FakeSession, idle_limit=60)
    await run(manager, "a", {"action": "read"})
    clock.now += 61
    assert await manager.sweep() == ["a"]


async def test_sweep_closes_every_session_while_browsing_is_off(tmp_path):
    FakeSession.made = []
    enabled = {"on": True}
    manager = BrowserManager(tmp_path / "s", session_factory=FakeSession, enabled=lambda: enabled["on"])
    await run(manager, "a", {"action": "read"})
    assert await manager.sweep() == []
    enabled["on"] = False
    assert await manager.sweep() == ["a"] and FakeSession.made[0].closed


async def test_close_chat_and_close_all(tmp_path):
    FakeSession.made = []
    manager = BrowserManager(tmp_path / "s", session_factory=FakeSession)
    await run(manager, "a", {"action": "read"})
    assert await manager.close_chat("a") is True and FakeSession.made[0].closed
    assert await manager.close_chat("a") is False and await manager.close_chat("never") is False
    await run(manager, "a", {"action": "read"})
    await manager.close_all()
    assert FakeSession.made[1].closed and manager.chats() == []
    await manager.close_all()  # safe to repeat


async def test_a_session_that_will_not_close_does_not_stop_the_others_or_raise(tmp_path):
    FakeSession.made = []
    manager = BrowserManager(tmp_path / "s", session_factory=FakeSession)
    await run(manager, "a", {"action": "read"})

    async def broken():
        raise RuntimeError("stuck")
    FakeSession.made[0].close = broken
    await manager.close_all()
    assert manager.chats() == []


async def test_a_call_while_browsing_is_off_says_so_and_starts_nothing(tmp_path):
    FakeSession.made = []
    manager = BrowserManager(tmp_path / "s", session_factory=FakeSession, enabled=lambda: False)
    outcome = await run(manager, "a", {"action": "read"})
    assert outcome.kind == "error" and outcome.text == "Not available: browsing is off (Settings → Browsing)."
    assert FakeSession.made == []


@pytest.mark.parametrize("arguments", [
    {}, {"action": "teleport"}, {"action": "open", "url": 5}, {"action": "read", "extra": 1},
    {"action": "scroll", "direction": "sideways"}, {"action": "click", "ref": ["r1"]}, {"action": "type", "submit": "yes"},
    {"action": None}])
async def test_arguments_are_checked_against_the_schema_before_anything_starts(fake, arguments):
    outcome = await run(fake, "a", arguments)
    assert outcome.kind == "error" and outcome.text.startswith("Invalid tool arguments")
    assert FakeSession.made == []


async def test_non_object_arguments_are_an_error_too(fake):
    outcome = await run(fake, "a", "open https://x.test")
    assert outcome.kind == "error" and "JSON object" in outcome.text


async def test_the_engines_argument_errors_become_tool_errors(fake):
    await run(fake, "a", {"action": "read"})
    FakeSession.made[0].fail = ValueError("The click action needs the 'ref' argument.")
    outcome = await run(fake, "a", {"action": "click"})
    assert outcome.kind == "error" and outcome.text == "The click action needs the 'ref' argument."
    assert FakeSession.made[0].calls == [("read", {})]


async def test_browser_unavailable_and_crashes_become_tool_errors(fake, caplog):
    await run(fake, "a", {"action": "read"})
    session = FakeSession.made[0]
    session.fail = BrowserUnavailable("Browsing needs a browser. Install one with: python -m playwright install chromium")
    outcome = await run(fake, "a", {"action": "read"})
    assert outcome.kind == "error" and "python -m playwright install chromium" in outcome.text
    assert not isinstance(outcome.result, BaseException)
    session.fail = RuntimeError("Target closed: the page crashed")
    with caplog.at_level(logging.ERROR, logger="local_agent.browser_tool"):
        outcome = await run(fake, "a", {"action": "read"})
    assert outcome.kind == "error" and outcome.text.startswith("The browser failed")
    assert "the page crashed" in outcome.text and "Traceback" not in outcome.text
    assert any("page crashed" in record.getMessage() or record.exc_info for record in caplog.records)
    session.fail = None
    assert (await run(fake, "a", {"action": "read"})).kind == "result"  # the next call still works


async def test_a_session_that_failed_is_replaced_on_the_next_call(fake):
    await run(fake, "a", {"action": "read"})
    first = FakeSession.made[0]
    first.state = "failed"
    await run(fake, "a", {"action": "read"})
    assert len(FakeSession.made) == 2 and first.closed


async def test_cancellation_propagates_and_leaves_the_manager_usable(fake):
    await run(fake, "a", {"action": "read"})
    FakeSession.made[0].fail = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await run(fake, "a", {"action": "read"})
    FakeSession.made[0].fail = None
    assert (await run(fake, "a", {"action": "read"})).kind == "result"
    # Cancelling the task while the action waits.
    session = FakeSession.made[0]
    session.gate = asyncio.Event()
    task = asyncio.create_task(run(fake, "a", {"action": "read"}))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# The permission step inside run ----------------------------------------------------------------------------------------

def decision(kind, **extra):
    return SimpleNamespace(decision=kind, message=extra.pop("message", ""), card=extra.pop("card", None),
                           arguments={}, source="x", host="x.test", **extra)


async def test_run_returns_before_acting_when_the_permission_layer_wants_a_card(fake):
    seen = []

    def ask(arguments, preview):
        seen.append((arguments, preview.host))
        return decision("ask", card={"title": "Browse x.test"})
    outcome = await run(fake, "a", {"action": "open", "url": "https://x.test/"}, decide=ask)
    assert outcome.kind == "needs_approval" and outcome.card == {"title": "Browse x.test"}
    assert outcome.decision.decision == "ask" and outcome.host == "x.test"
    assert seen == [({"action": "open", "url": "https://x.test/"}, "x.test")]
    assert FakeSession.made[0].calls == []
    approved = await run(fake, "a", {"action": "open", "url": "https://x.test/"}, decide=ask, approved=True)
    assert approved.kind == "result" and FakeSession.made[0].calls == [("open", {"url": "https://x.test/"})]


@pytest.mark.parametrize("kind", ["deny", "refuse"])
@pytest.mark.parametrize("approved", [False, True])
async def test_a_denial_or_refusal_is_final_even_after_an_approval(fake, kind, approved):
    outcome = await run(fake, "a", {"action": "read"}, decide=lambda a, p: decision(kind, message="Blocked by a rule."),
                        approved=approved)
    assert outcome.kind == "refused" and outcome.text == "Blocked by a rule." and outcome.result == "Blocked by a rule."
    assert FakeSession.made[0].calls == []


async def test_an_allow_decision_runs_the_action(fake):
    outcome = await run(fake, "a", {"action": "read"}, decide=lambda a, p: decision("allow"))
    assert outcome.kind == "result" and FakeSession.made[0].calls == [("read", {})]

# What an approval covers, and the submit request ------------------------------------------------------------------------

BANK = {"action": "click", "ref": "r1"}


def ask_card(**extra):
    return decision("ask", card={"title": "Click on x.test", "host": "x.test", "tab": "t1", "ref": "r1",
                                 "ref_label": 'button "OK"', "submits": False, **extra})


async def test_an_approval_is_bound_to_what_the_card_showed_and_the_card_records_it(fake):
    from local_agent.browser_tool import decide
    FakeSession.made = []
    asked = await run(fake, "a", BANK, decide=lambda a, p: ask_card())
    assert asked.kind == "needs_approval" and asked.binding == {"tab": "t1", "host": "x.test", "label": 'button "OK"',
                                                                  "submits": False}
    assert FakeSession.made[0].calls == []
    FakeSession.made[0].label = 'button "OK"'  # the page still shows the control the card named
    approved = await run(fake, "a", BANK, decide=lambda a, p: ask_card(), approved=True, binding=asked.binding)
    assert approved.kind == "result" and FakeSession.made[0].extras[0]["expect"] == asked.binding
    # The manager checks the facts too, before it asks the engine: a control that is not the one the card named stops it.
    FakeSession.made[0].label = 'button "Delete"'
    changed = await run(fake, "a", BANK, decide=lambda a, p: ask_card(), approved=True, binding=asked.binding)
    assert changed.kind == "refused" and changed.refused == "page_changed" and "Delete" in changed.text
    assert len(FakeSession.made[0].calls) == 1
    # The real card carries the same facts: tab, host, ref, the control's label and the submit fact.
    shown = decide({}, BANK, BrowserResult("", host="x.test", tab="t3", label='link "Home"'), "manual", {})
    assert shown.card["tab"] == "t3" and shown.card["host"] == "x.test" and shown.card["ref"] == "r1"
    assert shown.card["ref_label"] == 'link "Home"' and shown.card["submits"] is False


async def test_a_call_that_was_not_asked_about_has_nothing_to_check(fake):
    await run(fake, "a", BANK, decide=lambda a, p: decision("allow"))
    assert FakeSession.made[0].extras[0]["expect"] is None


async def test_a_result_that_says_the_page_changed_is_a_refusal_that_needs_a_new_decision(fake):
    await run(fake, "a", {"action": "read"})
    FakeSession.made[0].label = 'button "OK"'
    FakeSession.made[0].plan = BrowserResult(UNTRUSTED.format(host="x.test") + "\nThe page changed while waiting for "
                                             "approval: the active tab was t2 and is now t3. Nothing was done.",
                                             refused="page_changed", host="x.test", tab="t3")
    outcome = await run(fake, "a", BANK, decide=lambda a, p: ask_card(), approved=True,
                        binding={"tab": "t2", "host": "x.test", "label": 'button "OK"', "submits": False})
    assert outcome.kind == "refused" and "changed while waiting for approval" in outcome.text and outcome.refused == "page_changed"


def submit_blocked(host="x.test"):
    return BrowserResult(UNTRUSTED.format(host=host) + "\nA request that sends data to x.test was stopped.",
                         refused="submit_needs_approval", host=host, tab="t1")


async def test_a_stopped_submit_asks_about_a_submit_on_that_host_and_runs_again_once_approved(fake):
    await run(fake, "a", {"action": "read"})
    session = FakeSession.made[0]
    session.plans = [submit_blocked()]
    seen = []

    def ask_for_submits(arguments, preview):
        seen.append((preview.host, preview.submits))
        if preview.submits:
            return decision("ask", card={"title": "Submit a form on x.test", "host": "x.test", "tab": "t1", "ref": None,
                                         "ref_label": None, "submits": True})
        return decision("allow")
    first = await run(fake, "a", BANK, decide=ask_for_submits)
    assert first.kind == "needs_approval" and first.card["title"] == "Submit a form on x.test"
    assert seen == [("x.test", False), ("x.test", True)] and first.host == "x.test"
    # After the card is approved the action runs again, and a submit to that host (only) is approved for it.
    again = await run(fake, "a", BANK, decide=ask_for_submits, approved=True, submit_hosts={"x.test"})
    ok = session.extras[-1]["submit_ok"]
    assert again.kind == "result" and ok("x.test") is True and ok("other.test") is False


async def test_where_the_mode_allows_submits_the_engine_is_told_so_and_nothing_asks(fake):
    await run(fake, "a", {"action": "read"})
    allow_all = lambda arguments, preview: decision("allow")  # noqa: E731  (Bypass: submits need no card)
    await run(fake, "a", BANK, decide=allow_all)
    ok = FakeSession.made[0].extras[-1]["submit_ok"]
    assert ok("x.test") is True and ok("anything.test") is True


async def test_a_submit_the_mode_refuses_is_a_refusal_and_without_a_permission_layer_it_runs(fake):
    await run(fake, "a", {"action": "read"})
    FakeSession.made[0].plans = [submit_blocked()]

    def deny_submits(arguments, preview):
        return decision("deny", message="Blocked by a deny rule.") if preview.submits else decision("allow")
    outcome = await run(fake, "a", BANK, decide=deny_submits)
    assert outcome.kind == "refused" and outcome.text == "Blocked by a deny rule."
    await run(fake, "a", BANK)  # no decide: the engine is told every submit is approved
    assert FakeSession.made[0].extras[-1]["submit_ok"]("any.test") is True


async def test_the_card_never_says_none_for_a_page_with_no_address():
    from local_agent.browser_tool import decide
    blank = BrowserResult("", host=None, tab="t2", label='button "Go"')
    card = decide({}, {"action": "click", "ref": "r1"}, blank, "manual", {}).card
    assert "None" not in card["title"] and card["title"] == "Click on this page"
    submits = BrowserResult("", host=None, tab="t2", label='button "Go"', submits=True)
    assert decide({}, {"action": "click", "ref": "r1"}, submits, "auto", {}).card["title"] == "Submit a form on this page"



async def test_a_decide_function_that_raises_is_a_tool_error_not_an_exception(fake):
    def broken(arguments, preview):
        raise KeyError("boom")
    outcome = await run(fake, "a", {"action": "read"}, decide=broken)
    assert outcome.kind == "error" and FakeSession.made[0].calls == []


# Results -------------------------------------------------------------------------------------------------------------

async def test_a_short_result_is_the_engines_text_with_its_banner(fake):
    await run(fake, "a", {"action": "read"})
    FakeSession.made[0].plan = BrowserResult(UNTRUSTED.format(host="x.test") + "\nEarlier: A dialog was dismissed.\nPage: Hi",
                                             host="x.test", tab="t2")
    outcome = await run(fake, "a", {"action": "read"})
    assert outcome.kind == "result" and outcome.ok and outcome.host == "x.test" and outcome.tab == "t2"
    assert outcome.result == {"output": outcome.text} and outcome.text.startswith("Untrusted web content from x.test")
    assert "Earlier: A dialog was dismissed." in outcome.text and outcome.image is None and outcome.result_id is None


async def test_the_redactor_runs_on_the_final_text(fake):
    await run(fake, "a", {"action": "read"}, redact=lambda text: text)
    FakeSession.made[0].plan = BrowserResult("token=abc123 shown", host="x.test")
    outcome = await run(fake, "a", {"action": "read"}, redact=lambda text: text.replace("abc123", "[redacted]"))
    assert outcome.text == "token=[redacted] shown"


async def test_a_long_result_keeps_the_first_page_and_stores_the_rest_for_get_tool_output(fake):
    await run(fake, "a", {"action": "read"})
    head = UNTRUSTED.format(host="x.test") + "\nPage: Long\n"
    rest = "\n".join(f"Paragraph {n}: " + "lorem ipsum " * 20 for n in range(2, 300))
    FakeSession.made[0].plan = BrowserResult(head + "Paragraph 1\n[The text continues: 60,000 more characters. Read them with "
                                             "get_tool_output.]", more=rest, host="x.test")
    results, keep = store_for("a")
    outcome = await run(fake, "a", {"action": "read"}, store_result=keep)
    assert outcome.result_id and outcome.result["result_id"] == outcome.result_id
    assert outcome.result["next_offset"] == 0 and outcome.result["total_chars"] == len(rest)
    assert outcome.result["output"] == outcome.text and "Paragraph 1" in outcome.text
    page = results.page("a", outcome.result_id, 0, 8_000)
    assert page["output"].startswith("Paragraph 2:") and page["total_chars"] == len(rest)
    assert page["note"].startswith("Untrusted web content from x.test")
    assert page["next_offset"] is not None


async def test_screenshots_become_uploads_and_the_text_names_them_for_a_vision_model(fake):
    await run(fake, "a", {"action": "read"})
    FakeSession.made[0].plan = BrowserResult(UNTRUSTED.format(host="x.test") + "\nScreenshot: 1x1.", host="x.test",
                                             screenshot=PIXEL, screenshot_size=(1, 1), screenshot_mime="image/png")
    outcome = await run(fake, "a", {"action": "screenshot"}, model=VISION)
    assert outcome.image["media_type"] == "image/png" and outcome.image["name"].startswith("Screenshot of x.test")
    assert outcome.saved_images[0][0] == PIXEL and outcome.saved_images[0][1] == "image/png"
    assert outcome.text.endswith("Screenshot saved and attached for the model.")
    assert outcome.result["output"] == outcome.text and "screenshot" not in outcome.result
    plain = await run(fake, "a", {"action": "screenshot"}, model=NO_VISION)
    assert plain.text.endswith("Screenshot shown to the user only; this model does not take images.")
    assert plain.image is not None  # still stored: the chat card shows it
    unknown = await run(fake, "a", {"action": "screenshot"}, model="some-other-endpoint")
    assert "shown to the user only" in unknown.text
    unset = await run(fake, "a", {"action": "screenshot"})
    assert "shown to the user only" in unset.text


async def test_a_screenshot_that_cannot_be_saved_is_reported_in_the_text(fake):
    await run(fake, "a", {"action": "read"})
    FakeSession.made[0].plan = BrowserResult(UNTRUSTED.format(host="x.test") + "\nScreenshot: 1x1.", host="x.test",
                                             screenshot=PIXEL, screenshot_size=(1, 1), screenshot_mime="image/png")

    def refuse(data, mime, name):
        raise ValueError("A conversation keeps at most 100 attached files.")
    outcome = await run(fake, "a", {"action": "screenshot"}, store_image=refuse, model=VISION)
    assert outcome.kind == "result" and outcome.image is None
    assert "Screenshot not saved: A conversation keeps at most 100 attached files." in outcome.text


def test_the_next_request_carries_a_screenshot_as_an_image_of_a_user_message():
    ref = {"upload": "b" * 64, "name": "Screenshot of x.test.png", "media_type": "image/png", "width": 1, "height": 1,
           "bytes": 10}
    message = screenshot_message(ref, "x.test")
    assert message["role"] == "user" and message["images"] == [ref]
    assert "x.test" in message["content"] and "browser tool" in message["content"] and "data" in message["content"]


async def test_a_refused_action_returns_the_engines_text(fake):
    await run(fake, "a", {"action": "read"})
    FakeSession.made[0].plan = BrowserResult(UNTRUSTED.format(host="x.test") + "\nStopped: bot check. Ask the user.",
                                             refused="bot_check", host="x.test")
    outcome = await run(fake, "a", {"action": "read"})
    assert outcome.kind == "result" and outcome.refused == "bot_check" and "Stopped: bot check" in outcome.text


# Status -----------------------------------------------------------------------------------------------------------------

def test_status_is_the_engines_status_and_the_setting():
    engine = browser_status()
    assert status(True) == {**engine, "enabled": True}
    assert status(False) == {**engine, "enabled": False}


def test_status_names_the_fix_when_the_package_is_missing(monkeypatch):
    monkeypatch.setattr(browser_tool, "browser_status", lambda channel=None: {"ready": False, "problem": "install it"})
    assert status(True) == {"ready": False, "problem": "install it", "enabled": True}


def test_status_passes_the_channel_on(monkeypatch):
    seen = []
    monkeypatch.setattr(browser_tool, "browser_status", lambda channel=None: seen.append(channel) or {"ready": True})
    assert status(True, channel="chrome")["ready"] is True and seen == ["chrome"]


def test_importing_the_module_does_not_import_playwright():
    import subprocess
    import sys
    from pathlib import Path
    code = "import sys\nsys.modules['playwright'] = None\nfrom local_agent import browser_tool\nprint('ok')\n"
    done = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parent.parent, capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0 and "ok" in done.stdout, done.stderr


# Uploads: a tool-made image ------------------------------------------------------------------------------------------------

def test_an_image_made_by_a_tool_is_saved_as_a_chat_upload_and_returns_a_reference(tmp_path):
    uploads = Uploads(tmp_path / "uploads")
    ref = uploads.save_tool_image("chat-1", "Screenshot of x.test.png", PIXEL)
    assert set(ref) == {"upload", "name", "media_type", "width", "height", "bytes"}
    assert ref["media_type"] == "image/png" and (ref["width"], ref["height"]) == (1, 1)
    record = uploads.get("chat-1", ref["upload"])
    assert record["kind"] == "image" and uploads.file("chat-1", record).read_bytes() == PIXEL
    with pytest.raises(ValueError):
        uploads.save_tool_image("chat-1", "notes.png", b"not an image")
    with pytest.raises(ValueError):
        uploads.save_tool_image("chat-1", "empty.png", b"")


def test_a_saved_screenshot_reaches_the_model_only_with_vision(tmp_path):
    from local_agent.uploads import expand_images
    uploads = Uploads(tmp_path / "uploads")
    ref = uploads.save_tool_image("chat-1", "Screenshot of x.test.png", PIXEL)
    payload = {"messages": [screenshot_message(ref, "x.test")]}
    with_vision = expand_images(payload, uploads.folder("chat-1"), True, VISION)
    assert any(part.get("type") == "image_url" for part in with_vision["messages"][0]["content"])
    without = expand_images(payload, uploads.folder("chat-1"), False, NO_VISION)
    assert "does not take images" in without["messages"][0]["content"]


# Exports: nothing of the browser enters a bundle ---------------------------------------------------------------------------

def test_an_export_holds_no_browser_state_and_a_screenshot_is_an_ordinary_upload(tmp_path):
    from local_agent.config import Settings
    from local_agent.portability import export_bundle
    from local_agent.store import Store
    settings = Settings(tmp_path / "state")
    manager = BrowserManager(settings.state_dir, session_factory=FakeSession)
    manager.profile_dir_for("chat-1").mkdir(parents=True)
    (manager.profile_dir_for("chat-1") / "Cookies").write_text("SESSION-COOKIE-VALUE")
    uploads = Uploads(settings.state_dir / "uploads")
    store = Store(settings.state_dir / "conversations.sqlite3")
    session = store.create(settings.values)
    ref = uploads.save_tool_image(session["id"], "Screenshot of x.test.png", PIXEL)
    session["messages"] = [{"role": "user", "content": "look", "images": [ref]}, {"role": "assistant", "content": "ok"}]
    session["events"] = [{"id": "e1", "type": "tool", "name": "browser", "call_id": "c1", "state": "completed",
                          "input": {"action": "open", "url": "http://x.test/"}, "output": "Opened.", "created": 1.0}]
    text = json.dumps(export_bundle([session], session["id"]))
    assert "SESSION-COOKIE-VALUE" not in text and "browser-profile" not in text and str(tmp_path) not in text
    assert "iVBOR" not in text  # no image bytes: the reference only, and by default a note instead of it
    assert ref["upload"] not in text or "not in exported" in text
    store.db.close()


# With the engine -------------------------------------------------------------------------------------------------------------

async def engine_manager(tmp_path, pages, **options):
    require_browser()
    options.setdefault("headless", True)
    allowed = options.pop("allowed", None)
    manager = BrowserManager(tmp_path / "state", **options)
    manager.test_allowed = allowed or (lambda key: True)
    return manager


async def test_the_engine_one_session_is_reused_and_closed_by_sweep(tmp_path, browser_pages):
    clock = Clock()
    manager = await engine_manager(tmp_path, browser_pages, clock=clock)
    try:
        opened = await run(manager, "a", {"action": "open", "url": browser_pages.url("form.html")})
        assert opened.kind == "result" and "Order form" in opened.text and opened.text.startswith("Untrusted web content")
        again = await run(manager, "a", {"action": "read"})
        assert "Order form" in again.text and manager.chats() == ["a"]
        clock.now += 601
        assert await manager.sweep() == ["a"]
        after = await run(manager, "a", {"action": "read"})
        assert "no browser is open" in after.text  # a new, empty session
    finally:
        await manager.close_all()
    assert stat.S_IMODE(os.stat(manager.profile_dir_for("a")).st_mode) == 0o700


async def test_the_engine_a_long_page_pages_through_get_tool_output(tmp_path, browser_pages):
    manager = await engine_manager(tmp_path, browser_pages)
    results, keep = store_for("a")
    try:
        outcome = await run(manager, "a", {"action": "open", "url": browser_pages.url("long.html")}, store_result=keep)
        assert len(outcome.text.encode()) <= 8_000 and outcome.result_id
        page = results.page("a", outcome.result_id, 0, 8_000)
        assert page["output"] and "Paragraph" in page["output"]
        seen, offset = page["output"], page["next_offset"]
        while offset is not None:
            page = results.page("a", outcome.result_id, offset, 8_000)
            seen, offset = seen + page["output"], page["next_offset"]
        assert "Paragraph 300:" in seen
    finally:
        await manager.close_all()


async def test_the_engine_a_screenshot_is_saved_as_an_upload(tmp_path, browser_pages):
    manager = await engine_manager(tmp_path, browser_pages)
    uploads = Uploads(tmp_path / "state" / "uploads")
    try:
        await run(manager, "chat-1", {"action": "open", "url": browser_pages.url("form.html")})
        outcome = await run(manager, "chat-1", {"action": "screenshot"}, model=VISION,
                            store_image=lambda data, mime, name: uploads.save_tool_image("chat-1", name, data))
        assert outcome.image and outcome.text.endswith("Screenshot saved and attached for the model.")
        assert uploads.get("chat-1", outcome.image["upload"])["kind"] == "image"
    finally:
        await manager.close_all()


async def test_the_engine_stop_ends_a_slow_page_and_the_session_stays_usable(tmp_path, browser_pages):
    manager = await engine_manager(tmp_path, browser_pages)
    try:
        await run(manager, "a", {"action": "open", "url": browser_pages.url("form.html")})
        task = asyncio.create_task(run(manager, "a", {"action": "open", "url": browser_pages.url("slow.html")}))
        await asyncio.sleep(1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert (await run(manager, "a", {"action": "read"})).kind == "result"
    finally:
        await manager.close_all()


async def test_the_engine_a_host_that_is_not_allowed_asks_before_any_request_leaves(tmp_path, browser_pages):
    from local_agent.browser_tool import decide
    manager = await engine_manager(tmp_path, browser_pages)
    rules = {"chat": []}

    def ask(arguments, preview):
        return decide({}, arguments, preview, "manual", rules)
    try:
        outcome = await run(manager, "a", {"action": "open", "url": browser_pages.url("form.html")}, decide=ask,
                            allowed=lambda key: False)
        assert outcome.kind == "needs_approval" and outcome.card["title"].startswith("Browse 127.0.0.1:")
        assert browser_pages.count() == 0
        rules["chat"].append({"behavior": "allow", "tool": "browser", "domain": outcome.host})
        done = await run(manager, "a", {"action": "open", "url": browser_pages.url("form.html")}, decide=ask,
                         allowed=lambda key: key == outcome.host)
        assert done.kind == "result" and "Order form" in done.text and browser_pages.count() > 0
    finally:
        await manager.close_all()


async def test_the_engine_a_credential_field_is_refused_in_every_mode_on_a_public_name(tmp_path, browser_pages):
    from local_agent.browser_tool import decide
    require_browser()
    from tests.test_browser_engine import PUBLIC_ADDRESS  # noqa: F401
    manager = BrowserManager(tmp_path / "state", headless=True,
                             host_map={"shop.example.test": ("93.184.216.34", f"127.0.0.1:{browser_pages.port}")})
    rules = {"chat": [{"behavior": "allow", "tool": "browser", "domain": "shop.example.test"}]}
    try:
        url = "http://shop.example.test/login.html"
        await run(manager, "a", {"action": "open", "url": url}, allowed=lambda key: True)
        read = await run(manager, "a", {"action": "read"}, allowed=lambda key: True)
        ref = next(line[1:line.index("]")] for line in read.text.splitlines() if line.startswith("[r") and "assword" in line)
        for mode in ("bypassPermissions", "auto", "manual"):
            outcome = await run(manager, "a", {"action": "type", "ref": ref, "text": "hunter2"},
                                decide=lambda a, p, mode=mode: decide({}, a, p, mode, rules), allowed=lambda key: True)
            assert outcome.kind == "refused" and "credential field" in outcome.text
    finally:
        await manager.close_all()


async def test_the_engine_label_for_names_a_control_for_the_card(tmp_path, browser_pages):
    manager = await engine_manager(tmp_path, browser_pages)
    try:
        await run(manager, "a", {"action": "open", "url": browser_pages.url("form.html")})
        read = await run(manager, "a", {"action": "read"})
        ref = next(line[1:line.index("]")] for line in read.text.splitlines() if line.startswith("[r") and "Send order" in line)
        assert "Send order" in manager.label_for("a", ref)
        assert manager.label_for("a", "r999") is None and manager.label_for("other", ref) is None
    finally:
        await manager.close_all()
