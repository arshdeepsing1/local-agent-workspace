# Local workspace design

Original reference: `concept.png`, generated with the built-in Image Gen tool. It is design history. The app
now follows the chosen direction below, not the concept's palette or fonts.

## Chosen direction

On 2026-11-15 the owner chose direction B, "Studio". It is a cool grey-white canvas with white cards that a soft
shadow lifts, compact rows, rounded corners and one indigo for primary actions, selection, focus and the user's
bubble. The app has this one light style and no other style or theme. `design/mockups/b.css` is the mock-up it
came from. `frontend/static/styles.css` is the real source now.

## Style tokens

`styles.css` has one token block, `:root`. It never changes with the system's light or dark mode, and the old
`data-style` and `data-theme` attributes are gone. Old `local.style` and `local.theme` values in a browser are
never read.

- Colour: `--canvas` `#f4f5f8`, `--sidebar` `#eceef3`, `--surface` white, `--surface-2` and `--surface-3` for
  hover and code, `--ink` `#171a21`, `--muted` `#596170`, `--accent` and `--focus` `#4f46e5`. Status colours
  (`danger`, `success`, `warning`, `info`) each have a soft tint. Code, chart and `ctx-*` colours have their own
  tokens. Every colour in the stylesheet comes from a token.
- Spacing: `--space-1` to `--space-8` on a 4 px base (4, 8, 12, 16, 20, 24, 32, 40).
- Corners: `--radius-s` 8 px for controls, `--radius-m` 12 px for cards and menus, `--radius-l` 16 px for
  dialogs, the message box and the user's bubble. Pills and circles use 999px and 50%.
- Type: `--text-xs` 11 px, `--text-s` 12 px, `--text-m` 14 px, `--text-l` 16 px, `--text-xl` 20 px. Each is
  multiplied by `--text-scale` (Settings, Appearance, Text size). The floor is 11 px.
- Controls: `--control-s` 28 px and `--control-m` 34 px heights. Touch targets stay at 24 px or more.
- Elevation: `--shadow-1` for cards, `--shadow-2` for menus, `--shadow-3` for dialogs.
- Fonts: the system UI font for the interface and `--font-mono` for code. No web font loads.

## Shape language

- Cards (tool, plan, question, changes, queue, summaries) are white with a transparent edge and `--shadow-1`.
  A card that waits for you keeps its white fill and gets an indigo edge with a soft halo.
- The user's message is an `--accent-soft` bubble with a 4 px tail corner. Replies have no bubble.
- The selected chat row is a white lifted pill with indigo text. The top bar is white and 48 px high.
- The message box is a lifted white card with a round indigo send button. Focus gives it an indigo edge, a 1 px
  ring and a soft halo. Every other control shows a 2 px indigo outline on keyboard focus.
- Inputs, selects and the chat search keep a `--border-strong` edge (3:1 against the page). Menus and dialogs
  have no edge and a deeper shadow.

## Layout rules

- Compact 270 px sidebar (320 px above 1600 px, 230 px up to 1120 px), 48 px top bar, 924 px content width.
- The Workspace panel sits beside the chat from 1024 px, is a drawer from 601 px to 1023 px and covers the
  screen at 600 px or less. The sidebar becomes a drawer at 600 px or less.
- The message box sits at the bottom with the project chips above it. Attachment, permission mode and model
  choice sit below it. The permission menu opens upward.
- The status row sits above the message box. It shows the errors, then Continue with the queued messages, then one of
  the changes and the tasks (the changes lead). The /btw answer, the commands help and the rewind notice are one line
  each. The item that is not shown is a chip, and a chip carries its main buttons (Keep all and Undo all for the
  changes, Continue, Send for the queue). While focus is in the row, an item that arrives waits as a chip and is
  announced. No control needs more clicks than it did when every bar showed at once.
- Every dialog (Settings, Agent tools, Folder access, Add a permission rule, Project settings files, Delete conversation,
  Fork from here, Rewind and the endpoint checks) uses one frame: a header with the title and Close, an optional
  section list, one scrolling body and a footer button row. Close and Cancel wait while a dialog saves.
- The Workspace panel is closed on a first visit. A choice made in the app is kept.

## Screen inventory

Sidebar: Local / workspace; New conversation; selected project; Conversations;
empty session hint; Settings; real gateway connection status.

Primary screen: New conversation; Workspace; What shall we work on?; Your files,
tools, and ideas. In one place.; composer; live model selector; Explore this
project / Make a change / Run a command; Runs on your machine · Models through
Databricks.

Conversation state extends the same design: user bubbles, Markdown replies,
expandable tool cards, approval actions, pinned composer and stop control.
Workspace drawer contains actual files, a text editor, Git changes, and a command
runner. Settings dialog configures workspace/model/runtime and the external
credential file. No static conversation data or simulated gateway responses.
Hosted-service navigation, account names, fake usage statistics and unsupported
controls from the reference screenshots are not copied into this local application.

## Task click counts

This table is the baseline for the UI revamp. The revamp must not need more clicks for any of these tasks.
The counts come from the components in `frontend/static/js/` and from driving the app in Chromium with the UI
tests' fake backend. They are for the desktop width (1280 px) with a chat open. Typing the text is not counted.

| # | Task | Clicks | Keys | Path today |
|---|---|---|---|---|
| 1 | Send a message in the current chat | 0 | 1 | The message box has focus when a chat opens. Type, then press Enter. The Send button is 1 click instead. |
| 2 | Start a new chat | 1 | 0 | Sidebar: New. Focus moves to the message box. |
| 3 | Switch to another chat | 1 | 0 | Sidebar: the chat's row. |
| 4 | Approve a waiting command | 1 | 0 | Approval card: Approve. If the card is scrolled out of view, Show under the message box comes first (2 clicks). |
| 5 | Allow a command for the whole chat | 1 | 0 | Approval card: Always allow this command in this chat. |
| 6 | Stop a running reply | 0 | 1 | Escape with focus in the message box. The Stop button in the message box is 1 click instead. |
| 7 | Open the diff of a changed file | 2 | 0 | Changes bar above the message box: the "N files changed" toggle, then the file name. The Workspace panel opens on the diff. |
| 8 | Change the permission mode | 2 | 0 | Mode button under the message box, then the mode in the menu. |
| 9 | Open Settings and change the text size | 4 | 0 | Sidebar: Settings. Section list: Appearance. Text size: open the list, choose a size. The size applies at once, without Save settings. Closing the dialog is 1 more click or Escape. |
| 10 | See what fills the context and compact now | 2 | 0 | Context line under the message box opens the usage card. Then Compact now. |

At phone width (390 px) the sidebar is a drawer. Tasks 2, 3 and 9 need 1 more click there (Open sidebar).
At phone width the Settings section list is a row above the body that scrolls sideways, so Appearance is one click
(a swipe along the row first when it is out of view).

`frontend/tests/test_reachability.py` measures these tasks, the features and every status row control on the base
commit (`e59f328`) and keeps those numbers as its limits. Its counter adds one for every real click, key press or
choice in a list, so choosing a size from the Text size list counts once there (3 clicks for task 9 against the 4
above).

## Mock-up directions

`design/mockups/b.css` is the Studio mock-up the owner chose and approved on 2026-11-15. It is a full set of
colour tokens plus a short block of shape rules. The two directions that were not chosen (Paper and Console) are
removed. `scripts/ui_screenshots.py --tokens design/mockups/b.css --out <folder>` renders it on the real frontend
with the UI tests' fake backend. `design/mockups/README.md` describes it and lists its contrast ratios. The file
is a mock-up. The app does not load it.
