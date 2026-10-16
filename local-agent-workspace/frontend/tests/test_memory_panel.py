"""Agent tools → Memory (MemoryPanel.js): the project's notes, added, edited and deleted by the user, and the
remember tool's approval card."""
import json
import re

from playwright.sync_api import expect

from test_agent_tools import open_dialog, overview_row

NOTE = {"id": "a1b2c3d4e5f6", "date": "2026-10-16", "text": "Tests run with pytest -q from the backend folder."}
MEMORY = """window.memoryState = { ...fake.memory, project: '/project', notes: [NOTE], bytes: 62 }
  fake.override = (path, options) => {
    if (!path.startsWith('/api/memory')) return undefined
    const method = options.method || 'GET'
    const body = options.body ? JSON.parse(options.body) : {}
    if (method === 'POST') memoryState.notes = [...memoryState.notes, { id: 'f6e5d4c3b2a1', date: '2026-10-16', text: body.text }]
    if (method === 'PATCH') memoryState.notes = memoryState.notes.map(note => path.includes(note.id) ? { ...note, text: body.text } : note)
    if (method === 'DELETE') memoryState.notes = path.startsWith('/api/memory?') ? [] : memoryState.notes.filter(note => !path.includes(note.id))
    return fake.json({ ...memoryState, bytes: memoryState.notes.length * 62 })
  }""".replace("NOTE", json.dumps(NOTE))


def open_memory(ui, page, setup=MEMORY):
    open_dialog(ui, setup)
    page.get_by_role("button", name="Memory", exact=True).click()
    region = page.get_by_role("region", name="Memory")
    expect(region).to_be_visible()
    return region


def test_the_overview_counts_the_notes_and_opens_memory(ui, page):
    open_dialog(ui, MEMORY)
    expect(overview_row(page, "Memory").locator(".overview-value")).to_have_text("1 note")
    page.get_by_role("button", name="Open Memory", exact=True).click()
    expect(page.get_by_role("region", name="Memory")).to_contain_text(NOTE["text"])


def test_the_notes_list_with_their_project_and_limits(ui, page):
    region = open_memory(ui, page)
    expect(region.get_by_role("list", name="Memory notes").get_by_role("listitem")).to_have_count(1)
    expect(region.get_by_text(NOTE["text"])).to_be_visible()
    expect(region.get_by_text("2026-10-16")).to_be_visible()
    expect(region.get_by_text("Project: /project")).to_be_visible()
    expect(region.get_by_text("1 note of 50 · 62 of 4,000 bytes")).to_be_visible()
    expect(region.get_by_text(re.compile("never in the repository itself, and not included in conversation exports"))).to_be_visible()
    assert ui.js("fake.calls.some(call => call.path === '/api/memory?session_id=a')")


def test_add_edit_and_delete_a_note(ui, page):
    region = open_memory(ui, page)
    add = region.get_by_role("button", name="Add note")
    expect(add).to_be_disabled()
    region.get_by_role("textbox", name="New note").fill("Use tabs in Makefiles.")
    add.click()
    expect(region.get_by_role("status")).to_have_text("Note added.")
    expect(region.get_by_text("Use tabs in Makefiles.")).to_be_visible()
    expect(region.get_by_role("textbox", name="New note")).to_have_value("")
    assert ui.bodies("/api/memory?session_id=a", "POST") == [{"text": "Use tabs in Makefiles."}]
    region.get_by_role("button", name="Edit note: Tests run with pytest -q from the backe…").click()
    editor = region.get_by_role("textbox", name="Edit note")
    expect(editor).to_have_value(NOTE["text"])
    editor.fill("Tests run with pytest -q.")
    region.get_by_role("button", name="Save note").click()
    expect(region.get_by_role("status")).to_have_text("Note saved.")
    assert ui.bodies(f"/api/memory/{NOTE['id']}?session_id=a", "PATCH") == [{"text": "Tests run with pytest -q."}]
    region.get_by_role("button", name="Delete note: Tests run with pytest -q.").click()
    expect(region.get_by_role("status")).to_have_text("Note deleted.")
    expect(region.get_by_text("Tests run with pytest -q.")).to_have_count(0)
    assert ui.js(f"fake.requests('/api/memory/{NOTE['id']}?session_id=a', 'DELETE').length") == 1


def test_clear_asks_first(ui, page):
    region = open_memory(ui, page)
    ui.run("fake.confirmAnswer = false")
    region.get_by_role("button", name="Clear memory").click()
    assert ui.js("fake.confirms") == ["Delete all 1 note of this project's memory? This cannot be undone."]
    assert ui.js("fake.requests('/api/memory?session_id=a', 'DELETE').length") == 0
    ui.run("fake.confirmAnswer = true")
    region.get_by_role("button", name="Clear memory").click()
    expect(region.get_by_role("status")).to_have_text("Memory cleared.")
    expect(region.get_by_text(re.compile("^No notes yet"))).to_be_visible()


def test_memory_off_and_an_unreadable_file_are_explained(ui, page):
    region = open_memory(ui, page, """fake.memory = { ...fake.memory, enabled: false,
      problem: "This project's memory file could not be read. Clear the memory to start again; until then nothing is loaded or saved." }""")
    expect(region.get_by_role("status")).to_have_text(re.compile("^Project memory is off in Settings → Agent"))
    expect(region.get_by_role("alert")).to_have_text(re.compile("memory file could not be read"))


def test_a_rejected_note_shows_the_server_reason(ui, page):
    region = open_memory(ui, page, """fake.override = (path, options) => path === '/api/memory?session_id=a' && options.method === 'POST'
      ? fake.json({ detail: 'This note looks like it holds a secret (a token, password or key).' }, 400) : undefined""")
    region.get_by_role("textbox", name="New note").fill("password=fake")
    region.get_by_role("button", name="Add note").click()
    expect(region.get_by_role("alert")).to_have_text("This note looks like it holds a secret (a token, password or key).")
    expect(region.get_by_role("textbox", name="New note")).to_have_value("password=fake")


def test_the_panel_fits_a_phone(ui, page):
    page.set_viewport_size({"width": 360, "height": 740})
    region = open_memory(ui, page)
    expect(region.get_by_role("button", name=re.compile("^Edit note"))).to_be_in_viewport()
    assert ui.js("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    assert ui.js("(b => b.scrollWidth <= b.clientWidth + 1)(document.querySelector('.agent-tools-body'))")
