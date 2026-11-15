"""The app has ONE light style (direction B, "Studio"; FR-049, FR-052). Old Style and Theme choices saved in a
browser are ignored without an error, the system's dark mode changes nothing, and Settings has no Style or Theme
control. Text size stays."""
import re
from pathlib import Path

from playwright.sync_api import expect

from conftest import ORIGIN

FRONTEND = Path(__file__).resolve().parents[1]
# B's light tokens as the browser computes them.
CANVAS, INK, SURFACE = "rgb(244, 245, 248)", "rgb(23, 26, 33)", "rgb(255, 255, 255)"
OLD_CHOICES = "localStorage.setItem('local.style', 'vscode'); localStorage.setItem('local.theme', 'dark')"
READ = """() => ({
  canvas: getComputedStyle(document.body).backgroundColor, ink: getComputedStyle(document.body).color,
  scheme: getComputedStyle(document.documentElement).colorScheme,
  attributes: [document.documentElement.dataset.theme ?? null, document.documentElement.dataset.style ?? null],
  token: getComputedStyle(document.documentElement).getPropertyValue('--canvas').trim() })"""


def watch(page):
    """Console errors and warnings, and every script the page asks for."""
    log = {"errors": [], "scripts": []}
    page.on("console", lambda message: message.type in ("error", "warning") and log["errors"].append(message.text))
    page.on("request", lambda request: request.resource_type == "script" and log["scripts"].append(request.url))
    return log


def test_saved_vscode_and_dark_choices_and_a_dark_system_change_nothing(ui, page):
    # Mutation: bring back any [data-theme=dark] or [data-style=vscode] token block, or a prefers-color-scheme block.
    page.emulate_media(color_scheme="dark")
    log = watch(page)
    ui.app(scenario="workspace", setup=OLD_CHOICES)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    look = page.evaluate(READ)
    assert look["canvas"] == CANVAS and look["ink"] == INK and look["scheme"] == "light", look
    assert look["token"] == "#f4f5f8"
    assert look["attributes"] == [None, None], look
    assert page.evaluate("getComputedStyle(document.querySelector('.sidebar')).backgroundColor") == "rgb(236, 238, 243)"
    assert log["errors"] == []
    # theme.js is gone: nothing asks for it, and the saved choices stay where they are, unread and unchanged.
    assert [url for url in log["scripts"] if "theme.js" in url] == []
    assert page.evaluate("[localStorage.getItem('local.style'), localStorage.getItem('local.theme')]") == ["vscode", "dark"]
    assert not (FRONTEND / "static" / "js" / "theme.js").exists()


def test_the_app_never_writes_a_style_or_a_theme(ui, page):
    ui.app(scenario="workspace", setup="""window.writes = []; const set = Storage.prototype.setItem
      Storage.prototype.setItem = function (key, value) { window.writes.push(key); return set.call(this, key, value) }""")
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    page.get_by_role("button", name="Settings", exact=True).click()
    page.get_by_role("dialog").get_by_role("combobox", name="Text size").select_option("125")
    writes = page.evaluate("window.writes")
    assert "local.textSize" in writes and [key for key in writes if key in ("local.style", "local.theme")] == [], writes
    assert page.evaluate("[localStorage.getItem('local.style'), localStorage.getItem('local.theme')]") == [None, None]


def test_settings_appearance_has_no_style_or_theme_but_text_size_still_scales(ui, page):
    ui.app(scenario="workspace", setup=OLD_CHOICES)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    page.get_by_role("button", name="Settings", exact=True).click()
    appearance = page.get_by_role("dialog").get_by_role("group", name="Appearance")
    expect(appearance).to_be_visible()
    for name in ("Style", "Theme"):
        expect(appearance.get_by_role("combobox", name=name, exact=True)).to_have_count(0)
    expect(appearance.get_by_text(re.compile(r"^(Style|Theme)$"))).to_have_count(0)
    expect(appearance.get_by_text(re.compile("Match system|VS Code|Claude|Dark"))).to_have_count(0)
    size = appearance.get_by_role("combobox", name="Text size")
    expect(size).to_have_value("100")
    assert ui.js("getComputedStyle(document.documentElement).getPropertyValue('--text-scale').trim()") == "1"
    size.select_option("125")
    assert ui.js("getComputedStyle(document.documentElement).getPropertyValue('--text-scale').trim()") == "1.25"
    # The other controls of the section still work.
    expect(appearance.get_by_role("checkbox", name=re.compile("Group tool calls"))).to_be_visible()
    assert page.evaluate(READ)["canvas"] == CANVAS


def test_index_html_is_light_and_has_no_boot_script():
    html = (FRONTEND / "index.html").read_text()
    assert re.search(r'<meta\s+name="color-scheme"\s+content="light"\s*/?>', html)
    assert re.search(r'<meta\s+name="theme-color"\s+content="#f4f5f8"\s*/?>', html)
    assert "data-style" not in html and "data-theme" not in html and "dataset" not in html
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.S)
    assert [script for script in scripts if re.search(r"localStorage|matchMedia|local\.(style|theme)", script)] == []
    assert "theme.js" not in html


def test_the_page_alone_is_light_with_saved_choices_and_a_dark_system(page):
    # index.html with the app's script left out: no flash of another look, and the meta tags say light.
    page.emulate_media(color_scheme="dark")
    page.route(f"{ORIGIN}/static/js/main.js", lambda route: route.fulfill(status=200, body="", content_type="text/javascript"))
    page.add_init_script(script=OLD_CHOICES)
    page.goto(f"{ORIGIN}/index.html")
    look = page.evaluate(READ)
    assert look["canvas"] == CANVAS and look["scheme"] == "light" and look["attributes"] == [None, None], look
    assert page.evaluate("document.querySelector('meta[name=theme-color]').content") == "#f4f5f8"
    assert page.evaluate("document.querySelector('meta[name=color-scheme]').content") == "light"
    assert page.evaluate("document.querySelector('html').getAttribute('data-theme')") is None


def test_the_conversation_and_the_composer_are_the_same_light_cards_in_a_dark_system(ui, page):
    page.emulate_media(color_scheme="dark")
    ui.app(scenario="workspace", setup=OLD_CHOICES)
    expect(page.get_by_role("combobox", name="Model")).to_have_value("test-model")
    composer = page.locator(".app-shell .composer").first
    expect(composer).to_have_css("background-color", SURFACE)
    # Native form controls follow color-scheme: a light select and a light scrollbar.
    assert page.evaluate("getComputedStyle(document.querySelector('select')).colorScheme") == "light"
