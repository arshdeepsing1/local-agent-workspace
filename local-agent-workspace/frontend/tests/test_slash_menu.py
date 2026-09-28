"""Skill commands typed in the message box: "/" lists skills, like Claude Desktop."""
from playwright.sync_api import expect


def start_app(ui, page, session=None, setup=""):
    ui.app(session=session, scenario="workspace", setup=setup)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")


def message(page):
    return page.get_by_role("textbox", name="Message")


def skills(page):
    return page.get_by_role("listbox", name="Skills")


def options(page):
    return skills(page).get_by_role("option")


def sent(ui, session="created"):
    return [body["text"] for body in ui.bodies(f"/api/sessions/{session}/messages", "POST")]


def test_slash_lists_skills_for_a_new_conversation_and_filters(ui, page):
    start_app(ui, page)
    message(page).press_sequentially("/")
    expect(options(page)).to_have_count(2)
    expect(options(page).nth(0)).to_contain_text("/review")
    expect(options(page).nth(0)).to_contain_text("Code review")
    expect(options(page).nth(0)).to_contain_text("Review the current diff for correctness bugs.")
    expect(options(page).nth(1)).to_contain_text("/handoff")
    expect(options(page).nth(1)).to_contain_text("Write a detailed cold-start handoff.")
    # A new conversation uses the default workspace's skills.
    assert len(ui.requests("/api/skills")) == 1
    message(page).press_sequentially("han")
    expect(options(page)).to_have_count(1)
    expect(options(page)).to_contain_text("/handoff")
    message(page).press_sequentially("x")
    expect(skills(page)).to_have_count(0)
    expect(page.get_by_role("status").filter(has_text="No skill matches /hanx.")).to_be_visible()
    assert len(ui.requests("/api/skills")) == 1


def test_arrows_choose_and_enter_inserts_before_a_second_enter_sends(ui, page):
    start_app(ui, page)
    box = message(page)
    box.press_sequentially("/")
    expect(options(page).nth(0)).to_have_attribute("aria-selected", "true")
    expect(box).to_have_attribute("aria-activedescendant", options(page).nth(0).get_attribute("id"))
    expect(box).to_have_attribute("aria-controls", skills(page).get_attribute("id"))
    box.press("ArrowDown")
    expect(options(page).nth(1)).to_have_attribute("aria-selected", "true")
    expect(options(page).nth(0)).to_have_attribute("aria-selected", "false")
    expect(box).to_have_attribute("aria-activedescendant", options(page).nth(1).get_attribute("id"))
    box.press("ArrowDown")
    expect(options(page).nth(0)).to_have_attribute("aria-selected", "true")
    box.press("ArrowUp")
    expect(options(page).nth(1)).to_have_attribute("aria-selected", "true")
    box.press("Enter")
    expect(box).to_have_value("/handoff ")
    expect(skills(page)).to_have_count(0)
    assert ui.js("['aria-activedescendant', 'aria-controls'].filter(name => document.activeElement.hasAttribute(name))") == []
    expect(box).to_be_focused()
    assert not ui.requests("/api/sessions/created/messages")
    box.press_sequentially("Create a handoff")
    box.press("Enter")
    ui.wait("fake.requests('/api/sessions/created/messages').length === 1")
    assert sent(ui) == ["/handoff Create a handoff"]


def test_tab_and_click_insert_and_escape_closes_until_the_text_changes(ui, page):
    start_app(ui, page)
    box = message(page)
    box.press_sequentially("/han")
    expect(options(page)).to_have_count(1)
    box.press("Tab")
    expect(box).to_have_value("/handoff ")
    expect(box).to_be_focused()
    box.fill("")
    box.press_sequentially("/")
    options(page).filter(has_text="/review").click()
    expect(box).to_have_value("/review ")
    expect(box).to_be_focused()
    box.fill("")
    box.press_sequentially("/re")
    expect(options(page)).to_have_count(1)
    box.press("Escape")
    expect(page.locator(".slash-menu")).to_have_count(0)
    expect(box).to_have_value("/re")
    box.press_sequentially("v")
    expect(options(page)).to_have_count(1)
    box.press("Escape")
    # With the menu closed, Enter sends the text as typed.
    box.press("Enter")
    ui.wait("fake.requests('/api/sessions/created/messages').length === 1")
    assert sent(ui) == ["/rev"]


def test_paths_and_text_after_a_command_do_not_open_the_menu(ui, page):
    start_app(ui, page)
    box = message(page)
    box.press_sequentially("/tmp/")
    expect(page.locator(".slash-menu")).to_have_count(0)
    box.press_sequentially("output.log is empty")
    expect(page.locator(".slash-menu")).to_have_count(0)
    box.fill("/review the parser")
    expect(page.locator(".slash-menu")).to_have_count(0)
    box.fill("Please /review")
    expect(page.locator(".slash-menu")).to_have_count(0)
    box.fill("/tmp/output.log is empty")
    box.press("Enter")
    ui.wait("fake.requests('/api/sessions/created/messages').length === 1")
    assert sent(ui) == ["/tmp/output.log is empty"]


def test_an_open_conversation_lists_its_own_skills_again_on_each_opening(ui, page):
    start_app(ui, page, "a", setup="""fake.override = path => path === '/api/skills?session_id=a'
      ? fake.json([{ id: 'worktree-skill', name: 'Worktree skill', description: '', path: '.agents/skills/worktree-skill/SKILL.md' }]) : undefined""")
    ui.show_session("a")
    box = message(page)
    box.press_sequentially("/")
    expect(options(page)).to_have_count(1)
    expect(options(page)).to_contain_text("/worktree-skill")
    expect(options(page)).to_contain_text("Worktree skill")
    assert len(ui.requests("/api/skills?session_id=a")) == 1 and not ui.requests("/api/skills")
    box.press_sequentially(" ")
    expect(page.locator(".slash-menu")).to_have_count(0)
    box.press("Backspace")
    expect(options(page)).to_have_count(1)
    assert len(ui.requests("/api/skills?session_id=a")) == 2


def test_loading_empty_and_error_states(ui, page):
    start_app(ui, page, setup="""const list = fake.defer('skills')
      fake.override = path => path === '/api/skills' ? list.promise : undefined""")
    box = message(page)
    box.press_sequentially("/")
    expect(page.get_by_role("status").filter(has_text="Loading skills…")).to_be_visible()
    ui.run("fake.deferreds.skills.resolve(fake.json(skills))")
    expect(options(page)).to_have_count(2)
    # A failed reload shows its error instead of the earlier list.
    ui.run("""fake.override = path => path === '/api/skills'
      ? fake.json({ detail: 'Skill discovery scans at most 100 entries; reduce the skills directory.' }, 400) : undefined""")
    box.press_sequentially(" ")
    box.press("Backspace")
    expect(page.locator(".slash-menu").get_by_role("alert")).to_have_text(
        "Skill discovery scans at most 100 entries; reduce the skills directory.")
    expect(skills(page)).to_have_count(0)
    ui.run("fake.override = path => path === '/api/skills' ? fake.json([]) : undefined")
    box.press("Escape")
    box.press_sequentially("x")
    box.press("Backspace")
    expect(page.get_by_role("status").filter(
        has_text="No skills found. Add one at .agents/skills/<skill-id>/SKILL.md in your project.")).to_be_visible()
    # Neither state blocks sending.
    box.press("Enter")
    ui.wait("fake.requests('/api/sessions/created/messages').length === 1")
    assert sent(ui) == ["/"]


def test_menu_closes_when_the_message_box_loses_focus(ui, page):
    start_app(ui, page)
    box = message(page)
    box.press_sequentially("/")
    expect(options(page)).to_have_count(2)
    page.get_by_role("heading", name="What’s up next?").click()
    expect(page.locator(".slash-menu")).to_have_count(0)
    box.click()
    expect(options(page)).to_have_count(2)


def test_matching_ranks_id_prefixes_then_name_prefixes_then_substrings(ui):
    ui.start()
    ranked = ui.js("""import('/static/js/components/SlashMenu.js').then(({ matchingSkills, slashQuery }) => {
      const skills = [{ id: 'preview', name: 'Preview' }, { id: 'code-review', name: 'Review code' }, { id: 'review', name: 'Code review' }]
      return { rev: matchingSkills(skills, 'REV').map(s => s.id), all: matchingSkills(skills, '').map(s => s.id),
        queries: ['/', '/rev', '/rev ', '/tmp/x', 'rev', '/rev\\nmore'].map(slashQuery) }
    })""")
    assert ranked == {"rev": ["review", "code-review", "preview"], "all": ["preview", "code-review", "review"],
                      "queries": ["", "rev", None, None, None, None]}


def test_enter_and_tab_wait_for_the_list_instead_of_sending_or_leaving(ui, page):
    start_app(ui, page, setup="""const list = fake.defer('skills')
      fake.override = path => path === '/api/skills' ? list.promise : undefined""")
    box = message(page)
    box.press_sequentially("/han")
    expect(page.get_by_role("status").filter(has_text="Loading skills…")).to_be_visible()
    box.press("Enter")
    box.press("Tab")
    expect(box).to_be_focused()
    expect(box).to_have_value("/han")
    assert not ui.requests("/api/sessions", "POST") and not ui.requests("/api/sessions/created/messages")
    ui.run("fake.deferreds.skills.resolve(fake.json(skills))")
    expect(options(page)).to_have_count(1)
    box.press("Enter")
    expect(box).to_have_value("/handoff ")
