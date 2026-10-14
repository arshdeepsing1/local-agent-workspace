"""Readable colours, one focus ring, an 11 px type floor and a compact model picker (styles.css).

Contrast is the WCAG 2.x ratio of an element's text colour, blended over its effective background (the
non-transparent background colours of it and its ancestors, down to the first opaque one). Text needs 4.5:1;
icons and focus indicators need 3:1 (an icon's colour is also faded by the opacity of it and its ancestors)."""
import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

CONTRAST_JS = """
window.visual = (() => {
  const parse = value => {
    const match = String(value).match(/rgba?\\(([^)]*)\\)/)
    if (!match) throw new Error(`Unexpected colour ${value}`)
    const parts = match[1].split(/[\\s,/]+/).filter(Boolean).map(parseFloat)
    return [parts[0], parts[1], parts[2], parts.length > 3 ? parts[3] : 1]
  }
  // Source-over compositing of one colour on another.
  const over = (top, bottom) => {
    const alpha = top[3] + bottom[3] * (1 - top[3])
    if (!alpha) return [0, 0, 0, 0]
    return [0, 1, 2].map(i => (top[i] * top[3] + bottom[i] * bottom[3] * (1 - top[3])) / alpha).concat(alpha)
  }
  const background = el => {
    const layers = []
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const colour = parse(getComputedStyle(node).backgroundColor)
      if (colour[3] > 0) layers.push(colour)
      if (colour[3] >= 1) break
    }
    let base = [255, 255, 255, 1]
    for (const layer of layers.reverse()) base = over(layer, base)
    return base
  }
  const opacity = el => {
    let value = 1
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) value *= parseFloat(getComputedStyle(node).opacity)
    return value
  }
  const luminance = rgb => {
    const [r, g, b] = rgb.slice(0, 3).map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4 })
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
  }
  const ratioOf = (fg, bg) => {
    const [a, b] = [luminance(fg), luminance(bg)].sort((x, y) => y - x)
    return (a + 0.05) / (b + 0.05)
  }
  // Text (or icon) contrast of an element, optionally of a pseudo-element such as '::placeholder'.
  const settle = () => document.getAnimations().forEach(animation => animation.finish())
  const ratio = (el, pseudo = null) => {
    settle()
    const bg = background(el)
    const fg = parse(getComputedStyle(el, pseudo).color)
    fg[3] *= opacity(el)
    return Math.round(ratioOf(over(fg, bg), bg) * 100) / 100
  }
  const ownText = el => [...el.childNodes].some(node => node.nodeType === 3 && node.textContent.trim())
  const visible = el => el.checkVisibility({ visibilityProperty: true, opacityProperty: true })
    && el.getClientRects().length > 0 && el.getBoundingClientRect().width > 0
  // Every visible element with its own text under root whose contrast is under the minimum (disabled controls are exempt).
  const failures = (root = document.body, minimum = 4.5) => settle() || [...root.querySelectorAll('*')]
    .filter(el => ownText(el) && visible(el) && !el.closest('svg, .sr-only, :disabled, [aria-disabled=true]'))
    .map(el => ({ el, value: ratio(el) }))
    .filter(item => item.value < minimum)
    .map(({ el, value }) => `${el.tagName.toLowerCase()}.${[...el.classList].join('.')} "${el.textContent.trim().slice(0, 40)}" ${value}`)
  // Visible icons (Lucide SVGs) under 3:1, outside disabled controls.
  const iconFailures = (root = document.body, minimum = 3) => settle() || [...root.querySelectorAll('svg.lucide')]
    .filter(el => visible(el) && !el.closest(':disabled, [aria-disabled=true]'))
    .map(el => ({ el, value: ratio(el) }))
    .filter(item => item.value < minimum)
    .map(({ el, value }) => `${el.getAttribute('class')} in ${el.parentElement.tagName.toLowerCase()}.${[...el.parentElement.classList].join('.')} ${value}`)
  // Visible elements with their own text under 11 px (svg text and .sr-only are left out).
  // An element at font-size 0 shows its text through ::before or ::after (a short label), so those are measured.
  const sizeOf = el => {
    const own = parseFloat(getComputedStyle(el).fontSize)
    if (own) return own
    const shown = ['::before', '::after'].map(pseudo => getComputedStyle(el, pseudo))
      .filter(style => !['none', 'normal'].includes(style.content)).map(style => parseFloat(style.fontSize))
    return shown.length ? Math.min(...shown) : Infinity
  }
  const smallText = (root = document.body, minimum = 11) => [...root.querySelectorAll('*')]
    .filter(el => ownText(el) && visible(el) && !el.closest('svg, .sr-only'))
    .filter(el => sizeOf(el) < minimum)
    .map(el => `${el.tagName.toLowerCase()}.${[...el.classList].join('.')} "${el.textContent.trim().slice(0, 30)}" ${sizeOf(el)}px`)
  return { ratio, failures, iconFailures, smallText, background, parse }
})()
"""

STOPPED = "Stopped. You can continue this conversation."
BAD_REQUEST = ('Databricks returned HTTP 400: {"error_code": "BAD_REQUEST", "message": "Invalid request: the parameter '
               'max_tokens is out of range for this endpoint."}')
MARKDOWN = ("See src/app.py for the change.\n\n> Quoted note with `inline code`.\n\n"
            "| File | Test | Note |\n|---|---|---|\n| `src/utils/helpers.py` | `tests/test_app.py` | Keeps the order. |\n\n"
            "```python\nprint('hello')\n```")
BACKGROUND_JOB = json.dumps({"job_id": "0123456789abcdef", "state": "running", "exit_code": None, "output": "", "offset": 0,
                             "next_offset": None, "total_chars": 0, "truncated": False, "background": True})
READ_PAGE = json.dumps({"path": "README.md", "start_line": 1, "end_line": 40, "content": "# Readme"})
CHAT = [
    {"id": "u1", "type": "user", "text": "Review the exporter"},
    {"id": "t1", "type": "tool", "name": "read_file", "state": "completed", "input": {"path": "README.md"}, "output": READ_PAGE},
    {"id": "t2", "type": "tool", "name": "run_command", "state": "completed", "input": {"command": "serve"},
     "output": BACKGROUND_JOB},
    {"id": "r1", "type": "assistant", "text": MARKDOWN, "request_info": {
        "model": "m", "status": "completed", "finish_reason": "stop",
        "usage": {"input_tokens": 9000, "output_tokens": 50, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200}}},
    {"id": "n1", "type": "notice", "text": "Databricks rate limited this chat. Waiting 12 s before trying again."},
    {"id": "e1", "type": "error", "text": BAD_REQUEST, "error_kind": "invalid_request", "http_status": 400},
    {"id": "s1", "type": "notice", "text": STOPPED, "stop_phase": "streaming"},
]
SIDEBAR = """
fake.sessions = new Map([
  ['a', { ...makeSession('a', 'Exporter work'), events: %s, tags: ['bug'] }],
  ['b', { ...makeSession('b', 'Notes'), tags: ['docs'] }],
  ['c', { ...makeSession('c', 'Notes'), updated: 2 }],
  ['k', { ...makeSession('k', 'Child task'), parent_session_id: 'a', is_subagent: true }],
])
window.noteDiff.lines.unshift({ kind: 'hunk', text: '@@ -1,3 +1,4 @@' })
window.gitStatus = { repository: true, branch: 'main', truncated: false, files: [
  { path: 'app.py', area: 'unstaged', status: 'modified', added: 1, removed: 1, hidden: null },
  { path: 'old.py', area: 'unstaged', status: 'deleted', added: 0, removed: 3, hidden: null },
  { path: 'new.py', area: 'untracked', status: 'untracked', added: 2, removed: 0, hidden: null },
  { path: '.env', area: 'unstaged', status: 'modified', added: 0, removed: 0, hidden: 'credential' }] }
window.gitDiff = { path: 'app.py', area: 'unstaged', binary: false, truncated: false, added: 1, removed: 1, lines: [
  { kind: 'hunk', text: '@@ -1 +1 @@' }, { kind: 'removed', text: "print('old')", old: 1 }, { kind: 'added', text: "print('new')", new: 1 }] }
// Extra answers for a test (window.answer), then Git and jobs; everything else goes to the workspace scenario.
fake.override = (path, options) => window.answer?.(path, options)
  || (path.startsWith('/api/git/status') ? fake.json(gitStatus) : path.startsWith('/api/git/diff') ? fake.json(gitDiff)
    : path.startsWith('/api/jobs') ? fake.json([]) : undefined)
"""


def start_chat(ui, page, width=1280, height=900, events=None, setup=""):
    """Chat 'a' (tool cards, a reply with Markdown and request details, notices) among repeated titles and tags."""
    page.set_viewport_size({"width": width, "height": height})
    page.add_init_script(script=CONTRAST_JS)
    ui.app(session="a", scenario="workspace", setup=SIDEBAR % json.dumps(CHAT if events is None else events) + setup)
    ui.show_session("a")
    ui.emit("a", "{ type: 'changes', changes: makeChanges(changedFile('note.txt', 2, 1)) }")
    expect(page.locator(".transcript .user-message").first).to_be_visible()


def ratio(page, selector, pseudo=None):
    locator = page.locator(selector).first
    expect(locator).to_be_visible()
    return locator.evaluate("(el, pseudo) => visual.ratio(el, pseudo)", pseudo)


def failures(page, root="body"):
    """Text under 4.5:1 and icons under 3:1 among the visible elements under root."""
    return page.evaluate("root => [...visual.failures(document.querySelector(root)), ...visual.iconFailures(document.querySelector(root))]",
                         root)


def small_text(page, root="body"):
    return page.evaluate("root => visual.smallText(document.querySelector(root))", root)


CONTEXT_INFO = {"estimated_tokens": 60380, "input_budget": 120832, "context_window": 131072, "reply_reserve": 8192,
                "compactions": 1, "summarized_messages": 8, "estimate_method": "weighted_utf8", "instruction_files": ["AGENTS.md"],
                "warnings": ["Project instructions were truncated."], "payload_bytes": 245300, "payload_limit_bytes": 4000000,
                "auto_compact_percent": 90, "auto_compact_threshold": 108748,
                "breakdown": {"system_instructions": 1000, "tool_definitions": 2000, "messages_and_results": 2500, "summary": 400,
                              "request_overhead": 100}}


SPLIT_COMMAND = {"id": "cmd", "type": "tool", "name": "run_command", "state": "pending", "preview": "make test",
                 "input": {"command": "make test"}, "rememberable": ["chat", "project", "user"], "editable": ["command"],
                 "rule_options": {"argv": ["make", "test"], "prefixes": [{"prefix": ["make"], "flags": []}], "reason": None}}
PLAN = {"id": "p", "type": "tool", "name": "submit_plan", "state": "pending",
        "input": {"plan": "# Plan\n\n## Steps\n\n" + "".join(f"{n}. Step {n} of the plan.\n" for n in range(1, 41))}}
SETTINGS_WITH_NOTES = """fake.settings = { ...fake.settings, model: 'databricks-claude-opus-5-5', max_output_tokens: 30000,
  context_window: 300000, model_migrated_from: 'databricks-claude-sonnet-4-5', coding_dir: '/srv/code',
  coding_dir_status: { path: '/srv/code', state: 'missing', message: '/srv/code does not exist yet.', source: 'settings' } }"""


def assert_readable(page, *selectors, minimum=4.5):
    values = {selector: ratio(page, selector) for selector in selectors}
    assert all(value >= minimum for value in values.values()), values


# V1, V10: text contrast (4.5:1) and icon contrast (3:1)

def test_text_in_a_chat_and_the_sidebar_is_readable(ui, page):
    # Mutations: restore --muted #858580 (fails most), or --accent #b66b50 (fails .primary).
    start_chat(ui, page, events=CHAT, setup=f"fake.sessions.get('a').context_info = {json.dumps(CONTEXT_INFO)}")
    page.get_by_text("Activity · 2 actions").click()
    page.get_by_text("Request details · completed").click()
    expect(page.locator(".conversation-row .session-id")).to_have_count(2)  # the two rows titled Notes
    assert_readable(page, ".sidebar .connection-state", ".sidebar .section-label", ".conversation-row .session-id",
                    ".tool-card .tool-subject", ".tool-status.neutral", ".changes-bar .primary",
                    ".request-details dt:text-is('Input incl. cache')", ".conversation-notice:has-text('rate limited')",
                    ".notice-details>summary", ".stop-marker", ".activity-summary li small")
    assert ratio(page, ".composer textarea", "::placeholder") >= 4.5
    assert failures(page) == []
    page.locator(".context-meter > summary").click()
    expect(page.locator(".context-details")).to_be_visible()
    assert failures(page, ".context-meter") == []


def test_the_workspace_review_and_its_active_tab_are_readable(ui, page):
    # Mutation: delete .workspace-tabs .active:hover (the hovered active tab falls to 4.08:1).
    start_chat(ui, page)
    page.get_by_role("button", name=re.compile("^Workspace")).click()
    page.get_by_role("button", name="note.txt Changed by the agent").click()
    expect(page.locator(".diff-review .diff-line.hunk")).to_be_visible()
    assert_readable(page, ".diff-review .line-number", ".diff-review .diff-line.hunk", ".workspace-tabs .active", ".review-bar .primary")
    assert failures(page, ".workspace-panel") == []
    page.locator(".workspace-tabs .active").hover()
    expect(page.locator(".workspace-tabs .active")).to_have_css("background-color", "rgb(232, 229, 222)")
    assert ratio(page, ".workspace-tabs .active") >= 4.5
    page.get_by_role("button", name="Changes", exact=True).click()
    # The Git file list (Mutations: .git-letter #8b6f2e, or .git-letter.deleted #a95640 on a hovered row).
    expect(page.locator(".git-letter.deleted")).to_be_visible()
    assert_readable(page, ".git-letter.modified", ".git-letter.deleted", ".git-letter.untracked")
    assert failures(page, ".workspace-panel") == []
    deleted_row = page.get_by_role("button", name=re.compile("old.py")).first
    deleted_row.hover()
    expect(deleted_row).to_have_css("background-color", "rgb(232, 229, 222)")
    assert ratio(page, ".git-letter.deleted") >= 4.5
    page.get_by_role("button", name=re.compile("app.py")).first.click()
    expect(page.locator(".git-diff-lines .diff-line.hunk")).to_be_visible()
    assert failures(page, ".workspace-panel") == []


def test_settings_and_folder_access_notes_are_readable(ui, page):
    start_chat(ui, page, setup=SETTINGS_WITH_NOTES)
    page.get_by_role("button", name="Settings", exact=True).click()
    dialog = page.get_by_role("dialog", name="Settings")
    expect(dialog.get_by_text("Above 20,000, four requests at once")).to_be_visible()
    assert_readable(page, "dialog[open] .settings-note", "dialog[open] .settings-group>legend")
    assert failures(page, "dialog[open]") == []
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Folder access").click()
    expect(page.locator("dialog[open] .folder-grant")).to_have_count(2)
    assert_readable(page, "dialog[open] .folder-grant small")
    assert failures(page, "dialog[open]") == []


def test_approval_and_plan_cards_are_readable(ui, page):
    start_chat(ui, page, events=CHAT[:1] + [SPLIT_COMMAND], setup="fake.sessions.get('a').status = 'awaiting_approval'")
    expect(page.locator(".split-button")).to_be_visible()
    assert_readable(page, ".split-button>button:first-child", ".composer-hint")
    assert failures(page, ".main-area") == []


def test_the_sticky_plan_actions_are_readable(ui, page):
    start_chat(ui, page, events=CHAT[:1] + [PLAN], setup="fake.sessions.get('a').status = 'awaiting_approval'")
    ui.js("(() => { const list = document.querySelector('.transcript'), card = document.querySelector('.plan-card');"
          " list.scrollTop += card.getBoundingClientRect().top - list.getBoundingClientRect().top; return true })()")
    actions = page.locator(".plan-actions")
    expect(actions).to_have_css("position", "sticky")
    assert actions.evaluate("e => getComputedStyle(e).backgroundColor") == "rgb(250, 243, 233)"  # #faf3e9
    assert_readable(page, ".plan-actions .primary", ".plan-actions .outline", ".plan-card h3")
    assert failures(page, ".plan-card") == []


def test_the_row_menu_is_readable(ui, page):
    start_chat(ui, page)
    page.get_by_role("button", name="More actions for Exporter work", exact=True).click()
    expect(page.locator(".row-menu .danger")).to_be_visible()
    assert_readable(page, ".row-menu .danger")
    assert failures(page, ".sidebar") == []


DECLINED = {"id": "t3", "type": "tool", "name": "run_command", "state": "rejected", "approval": "declined",
            "input": {"command": "make clean"}}
OTHER_PROJECT = ("fake.sessions.set('o', { ...makeSession('o', 'Other waiting'), workspace: '/srv/other', "
                 "status: 'awaiting_approval' })\n")
STATUSES = ("{ type: 'changes', changes: makeChanges(changedFile('note.txt', 2, 1), changedFile('new.py', 3, 0, 'created'), "
            "changedFile('old.py', 0, 2, 'deleted'), changedFile('gone.bin', 0, 0, 'unavailable')) }")


def test_status_labels_badges_and_outcome_lines_are_readable(ui, page):
    # Mutations: .changes-status #777971, .changes-status.deleted #a95640, .project-badge #706e64, .tool-outcome #956545,
    # .project-group-toggle #706e64 or .diff-stat .removed #a95640 (both 4.1:1 on the hover background).
    start_chat(ui, page, events=CHAT[:1] + [DECLINED] + CHAT[3:4], setup=OTHER_PROJECT)
    ui.emit("a", STATUSES)
    expect(page.locator(".tool-outcome")).to_have_text("Action declined")
    toggle = page.locator(".changes-toggle")
    toggle.click()
    expect(page.locator(".changes-status")).to_have_count(3)
    other = page.locator(".project-group-toggle", has_text="other")
    expect(other.locator(".project-badge")).to_have_text("1 needs approval")
    assert_readable(page, ".tool-outcome", ".changes-status.created", ".changes-status.deleted", ".changes-status.unavailable",
                    ".project-badge")
    assert failures(page) == []
    # Touch keeps :hover on a tapped button, so the hovered toggles stay readable too.
    for hovered, text in ((toggle, ".changes-toggle .diff-stat .removed"), (other, ".project-group-toggle:has-text('other') .project-group-name")):
        hovered.hover()
        expect(hovered).to_have_css("background-color", "rgb(232, 229, 222)")
        assert ratio(page, text) >= 4.5, text


def test_the_permission_menu_is_readable(ui, page):
    # Mutation: .permission-name small back to #777971 (3.87:1 on its #f0f0ed chip).
    start_chat(ui, page)
    page.locator(".permission-trigger").click()
    expect(page.locator(".permissions-menu")).to_be_visible()
    page.locator(".never-asks>summary").click()
    expect(page.locator(".never-asks li").first).to_be_visible()
    assert_readable(page, ".permission-name small", ".never-asks>summary")
    assert failures(page, ".permissions-menu") == []


def test_text_on_a_phone_is_readable(ui, page):
    start_chat(ui, page, 390, 844)
    page.get_by_text("Activity · 2 actions").click()
    assert_readable(page, ".tool-card .tool-subject", ".conversation-notice:has-text('rate limited')", ".stop-marker",
                    ".activity-summary li small")
    assert failures(page, ".main-area") == []
    page.get_by_role("button", name="Open sidebar").click()
    expect(page.locator(".sidebar")).to_be_visible()
    assert_readable(page, ".sidebar .connection-state", ".sidebar .section-label", ".conversation-row .session-id")
    assert failures(page, ".sidebar") == []


def test_line_comment_buttons_have_three_to_one_on_touch_screens(coarse_ui, coarse_page):
    # V10. Without hover the per-line Add comment buttons stay faded. Mutation: opacity .55 (2.1:1).
    ui, page = coarse_ui, coarse_page
    start_chat(ui, page, 390, 844)
    page.get_by_role("button", name=re.compile("^Workspace")).click()
    page.get_by_role("button", name="note.txt Changed by the agent").click()
    icons = page.locator(".diff-review .line-comment svg")
    expect(icons.first).to_be_visible()
    values = icons.evaluate_all("els => els.map(el => visual.ratio(el))")
    assert len(values) >= 3 and min(values) >= 3, values


def test_message_action_icons_have_three_to_one(ui, page):
    # V-T3. Mutation: restore #938d82 at opacity .7 (2.15:1).
    start_chat(ui, page)
    icons = page.locator(".message-actions .message-action svg, .message-actions .copy-button svg")
    expect(icons.first).to_be_visible()
    values = icons.evaluate_all("els => els.map(el => visual.ratio(el))")
    assert len(values) >= 3 and min(values) >= 3, values


# V2, V3: the message box shows focus, and every focusable element gets the same 2 px accent ring

ACCENT = "rgb(163, 90, 64)"


def keyboard_focus(page, locator):
    """Focus an element after a key press, so :focus-visible applies as it does for Tab."""
    page.keyboard.press("Shift")
    locator.focus()
    assert locator.evaluate("e => document.activeElement === e && e.matches(':focus-visible')")


def test_the_message_box_shows_keyboard_focus(ui, page):
    # V-T4. Mutation: delete the .app-shell .composer:focus-within rule.
    start_chat(ui, page)
    composer = page.locator(".composer").first
    textbox = page.get_by_role("textbox", name="Message")
    textbox.evaluate("e => e.blur()")
    blurred = composer.evaluate("e => [getComputedStyle(e).borderTopColor, getComputedStyle(e).boxShadow]")
    keyboard_focus(page, textbox)
    focused = composer.evaluate("e => [getComputedStyle(e).borderTopColor, getComputedStyle(e).boxShadow]")
    assert focused[0] != blurred[0] and focused[0] == ACCENT, (blurred, focused)
    assert ACCENT in focused[1], focused


def outline(locator):
    return locator.evaluate("e => [getComputedStyle(e).outlineStyle, getComputedStyle(e).outlineWidth, getComputedStyle(e).outlineColor]")


def test_one_focus_ring_for_summaries_links_scrollers_and_the_review(ui, page):
    # V-T5. Mutation: remove the :focus-visible default (these fall back to the browser's 1px rgb(16, 16, 16) ring).
    long_error = dict(CHAT[5], text='Databricks returned HTTP 400: {"message": "' + "The request was rejected. " * 300 + '"}')
    start_chat(ui, page, events=CHAT[:5] + [long_error])
    page.get_by_text("Details", exact=True).click()
    details = page.locator(".notice-details pre")
    expect(details).to_be_visible()
    assert details.evaluate("e => e.scrollHeight > e.clientHeight")  # a scroller, so it takes keyboard focus
    targets = [page.locator(".activity-summary>summary"), page.locator(".markdown a.path-link").first, details]
    page.get_by_role("button", name=re.compile("^Workspace")).click()
    page.get_by_role("button", name="note.txt Changed by the agent").click()
    targets.append(page.locator(".diff-review .diff-review-lines"))
    for target in targets:
        expect(target).to_be_visible()
        keyboard_focus(page, target)
        assert outline(target) == ["solid", "2px", ACCENT], target
    page.get_by_role("button", name="Changes", exact=True).click()
    page.get_by_role("button", name=re.compile("app.py")).first.click()
    for target in [page.get_by_role("link", name="Open in VS Code"), page.locator(".git-diff-lines")]:
        expect(target).to_be_visible()
        keyboard_focus(page, target)
        assert outline(target) == ["solid", "2px", ACCENT], target


# The ring's rectangle (outline-offset plus outline-width around the border box) against the padding box of the
# nearest ancestor that clips its overflow: a ring outside that box is cut off and shows nothing.
RING_INSIDE_CLIP = """e => {
  const style = getComputedStyle(e), grow = parseFloat(style.outlineOffset) + parseFloat(style.outlineWidth)
  const box = e.getBoundingClientRect()
  let clip = e.parentElement
  while (clip && getComputedStyle(clip).overflow === 'visible') clip = clip.parentElement
  const outer = clip.getBoundingClientRect(), cs = getComputedStyle(clip)
  const inner = { left: outer.left + parseFloat(cs.borderLeftWidth), right: outer.right - parseFloat(cs.borderRightWidth),
                  top: outer.top + parseFloat(cs.borderTopWidth), bottom: outer.bottom - parseFloat(cs.borderBottomWidth) }
  return box.left - grow >= inner.left - 0.5 && box.right + grow <= inner.right + 0.5
    && box.top - grow >= inner.top - 0.5 && box.bottom + grow <= inner.bottom + 0.5
}"""


def test_rings_inside_clipped_cards_and_the_permission_menu_show(ui, page):
    # V-T5. Mutations: delete :is(.tool-heading,.task-checklist-toggle):focus-visible (offset 3px, clipped by the card's
    # overflow), restore outline:none on .permissions-menu>button:focus-visible, or the never-asks summary ring #347aca.
    start_chat(ui, page)
    ui.emit("a", {"type": "tasks", "tasks": [{"id": "t1", "title": "Read the parser", "description": "", "status": "completed",
                                              "depends_on": []}, {"id": "t2", "title": "Fix empty input", "description": "",
                                                                  "status": "pending", "depends_on": []}]})
    targets = [page.locator(".tool-card .tool-heading").first, page.get_by_role("button", name=re.compile(r"^Tasks 1\/2 done"))]
    for target in targets:
        expect(target).to_be_visible()
        keyboard_focus(page, target)
        assert outline(target) == ["solid", "2px", ACCENT], target
        assert target.evaluate(RING_INSIDE_CLIP), target
    page.locator(".permission-trigger").focus()
    page.keyboard.press("Enter")
    expect(page.locator(".permissions-menu")).to_be_visible()
    page.keyboard.press("ArrowDown")
    item = page.locator(".permissions-menu :focus")
    expect(item).to_have_count(1)
    assert item.evaluate("e => e.matches(':focus-visible')")
    assert outline(item) == ["solid", "2px", ACCENT]
    assert item.evaluate(RING_INSIDE_CLIP)
    for target in (page.locator(".permissions-menu>.add-rule"), page.locator(".never-asks>summary")):
        keyboard_focus(page, target)
        assert outline(target) == ["solid", "2px", ACCENT], target


# V9: an 11 px type floor, 12 px for phone banners

@pytest.mark.parametrize("width,height", [(1280, 900), (390, 844)])
def test_no_text_is_smaller_than_eleven_pixels(ui, page, width, height):
    # V-T6. Mutations: remove font-size:11px from .activity-summary li small (the browser's `smaller`, 10 px), or set
    # .session-id back to 10px.
    start_chat(ui, page, width, height, setup=f"fake.sessions.get('a').context_info = {json.dumps(CONTEXT_INFO)}")
    page.get_by_text("Activity · 2 actions").click()
    page.get_by_text("Request details · completed").click()
    assert small_text(page) == []
    if width <= 600:
        page.get_by_role("button", name="Open sidebar").click()
        expect(page.locator(".sidebar .tag-chip").first).to_be_visible()
        assert small_text(page) == []
        page.locator(".sidebar").get_by_role("button", name="Close sidebar").click()
    page.get_by_role("button", name=re.compile("^Workspace")).click()
    page.get_by_role("button", name="note.txt Changed by the agent").click()
    expect(page.locator(".diff-review .no-newline")).to_be_visible()
    assert small_text(page) == []


def test_the_phone_connection_banner_is_twelve_pixels(ui, page):
    start_chat(ui, page, 390, 844, setup="window.answer = path => path === '/api/connection' ? fake.json({ connected: false, "
                                         "models: [], error: 'All connection attempts failed', error_kind: 'network' }) : undefined")
    banner = page.locator(".connection-banner")
    expect(banner).to_be_visible()
    sizes = banner.evaluate("e => [e, ...e.querySelectorAll('*')].filter(n => n.checkVisibility()).map(n => parseFloat(getComputedStyle(n).fontSize))")
    assert min(sizes) >= 12, sizes


def test_the_stylesheet_has_no_nine_or_ten_pixel_text():
    # V-T8. Mutation: leave .file-roots-root small{font-size:9px}. The usage chart's SVG text is in viewBox units.
    css = (Path(__file__).resolve().parents[1] / "static" / "styles.css").read_text()
    css = css.replace(".usage-chart text{fill:var(--muted);font-size:10px}", "")
    assert re.findall(r"[^{};]*\{[^{}]*(?:font-size:(?:9|10)px|font:[^;}]*\b(?:9|10)px)[^{}]*\}", css) == []


# V5: the model picker is as wide as its label, which sits next to the arrow

MEASURE_PICKER = """select => {
  const style = getComputedStyle(select)
  const context = document.createElement('canvas').getContext('2d')
  context.font = style.font
  const label = context.measureText(select.selectedOptions[0].textContent).width
  const box = select.getBoundingClientRect()
  const chevron = select.parentElement.querySelector('svg').getBoundingClientRect()
  const padding = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight)
  return { width: box.width, limit: label + padding + 4, gap: chevron.left - (box.left + parseFloat(style.paddingLeft) + label) }
}"""


@pytest.mark.parametrize("width,height", [(1280, 900), (390, 844)])
def test_the_model_label_sits_next_to_its_arrow(ui, page, width, height):
    # V-T7. Mutation: remove field-sizing (a wider select, and the label far from the chevron).
    start_chat(ui, page, width, height)
    select = page.get_by_role("combobox", name="Model")
    expect(select).to_have_value("test-model")
    measured = select.evaluate(MEASURE_PICKER)
    assert measured["width"] <= measured["limit"], measured
    assert -2 <= measured["gap"] <= 6, measured


def test_the_default_model_fits_the_toolbar_at_360(ui, page):
    start_chat(ui, page, 360, 740, setup="fake.sessions.get('a').model = 'databricks-claude-opus-5-5'")
    select = page.get_by_role("combobox", name="Model")
    expect(select).to_have_value("databricks-claude-opus-5-5")
    measured = select.evaluate(MEASURE_PICKER)
    assert measured["width"] <= measured["limit"] and -2 <= measured["gap"] <= 6, measured
    tops = page.locator(".composer-toolbar > :not(.composer-spacer)").evaluate_all(
        "els => els.map(el => Math.round(el.getBoundingClientRect().top))")
    assert max(tops) - min(tops) <= 4, tops  # one row: the picker does not wrap below the permission trigger
    assert ui.js("document.documentElement.scrollWidth <= innerWidth")


# V-X1: a long error keeps its Dismiss button and the message box on screen

@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_a_long_error_stays_inside_the_viewport(ui, page, width, height):
    detail = ("The server rejected the model change: " + "quoted input " * 1600)[:20000]
    start_chat(ui, page, width, height, setup=("window.answer = (path, options) => path === '/api/sessions/a/model' && options.method === 'PUT' "
                                               f"? fake.json({{ detail: {json.dumps(detail)} }}, 400) : undefined"))
    page.get_by_role("combobox", name="Model").select_option("other-model")
    error = page.locator(".app-error")
    expect(error).to_contain_text("The server rejected the model change")
    dismiss = page.get_by_role("button", name="Dismiss error")
    for locator in (dismiss, page.get_by_role("textbox", name="Message")):
        box = locator.bounding_box()
        assert box["y"] >= 0 and box["y"] + box["height"] <= height and box["x"] + box["width"] <= width, box
    assert error.bounding_box()["height"] <= height * 0.3 + 40
    dismiss.click()
    expect(error).to_have_count(0)


@pytest.mark.parametrize("width,height", [(820, 1180), (390, 844)])
def test_a_long_error_in_the_workspace_drawer_stays_inside_the_viewport(ui, page, width, height):
    # Below 1024 px the open drawer shows the error in .drawer-alert. Mutation: drop its max-height and the span's
    # max-height/overflow (the alert grows to 4500 px and Dismiss sits above the screen).
    detail = ("The server rejected the model change: " + "quoted input " * 1600)[:20000]
    start_chat(ui, page, width, height, setup=("window.answer = (path, options) => path === '/api/sessions/a/model' && options.method === 'PUT' "
                                               f"? fake.json({{ detail: {json.dumps(detail)} }}, 400) : undefined"))
    page.get_by_role("combobox", name="Model").select_option("other-model")
    expect(page.locator(".app-error")).to_contain_text("The server rejected the model change")
    page.get_by_role("button", name=re.compile("^Workspace")).click()
    alert = page.locator(".drawer-alert")
    expect(alert).to_contain_text("The server rejected the model change")
    box = alert.bounding_box()
    assert box["y"] >= 0 and box["y"] + box["height"] <= height, box
    assert box["height"] <= height * 0.3 + 40, box
    dismiss = alert.get_by_role("button", name="Dismiss error")
    box = dismiss.bounding_box()
    assert box["y"] >= 0 and box["y"] + box["height"] <= height and box["x"] + box["width"] <= width, box
    dismiss.click(timeout=3000)
    expect(alert).to_have_count(0)
    expect(page.locator(".app-error")).to_have_count(0)


def test_a_two_digit_change_count_fits_its_badge(ui, page):
    # The 17 px badge holds 11 px digits without clipping.
    start_chat(ui, page)
    ui.emit("a", "{ type: 'changes', changes: makeChanges(...Array.from({ length: 12 }, (_, n) => changedFile(`src/file_${n}.py`))) }")
    badge = page.locator(".workspace-toggle .change-count")
    expect(badge).to_contain_text("12")
    assert badge.evaluate("e => getComputedStyle(e).fontSize") == "11px"
    assert badge.evaluate("e => e.scrollWidth <= e.clientWidth && e.scrollHeight <= e.clientHeight")
