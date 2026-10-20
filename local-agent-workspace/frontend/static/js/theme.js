// Settings → Appearance → Style and Theme. Style: Claude (the default) or VS Code; Theme: Light (the default), Dark, or
// Match system (the operating system's light or dark mode). Any style goes with any theme. Both are kept in this
// browser's localStorage like Text size, and applied as data-style and data-theme on <html>: styles.css swaps its colour
// tokens (and, for VS Code, its fonts and corners) for them. index.html applies stored choices before the stylesheet
// loads, so a dark page never flashes light; keep its copy of CANVAS and the keys in step with this file.
export const STYLES = [['claude', 'Claude'], ['vscode', 'VS Code']]
export const THEMES = [['light', 'Light'], ['dark', 'Dark'], ['system', 'Match system']]
const STYLE_KEY = 'local.style'
const THEME_KEY = 'local.theme'
// <meta name="theme-color">: the page's canvas, for the browser's title bar on phones and installed apps.
const CANVAS = { claude: { light: '#faf9f5', dark: '#262624' }, vscode: { light: '#ffffff', dark: '#1f1f1f' } }
const known = (list, value) => list.some(([name]) => name === value)
const darkQuery = () => window.matchMedia?.('(prefers-color-scheme: dark)')

function stored(key, list) {
  try {
    const value = window.localStorage.getItem(key)
    return known(list, value) ? value : list[0][0]
  } catch { return list[0][0] }  // Storage blocked (private windows): the default.
}

function store(key, list, value) {
  try {
    if (value === list[0][0]) window.localStorage.removeItem(key)
    else window.localStorage.setItem(key, value)
  } catch { /* Kept for this page only. */ }
}

export const theme = () => stored(THEME_KEY, THEMES)
export const style = () => stored(STYLE_KEY, STYLES)

function updateThemeColor() {
  const root = document.documentElement.dataset
  const dark = root.theme === 'dark' || (root.theme === 'system' && !!darkQuery()?.matches)
  const colours = CANVAS[root.style] || CANVAS.claude
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', dark ? colours.dark : colours.light)
}

let watching = false
function watchSystem() {  // Match system follows the operating system while the page is open.
  if (watching) return
  watching = true
  darkQuery()?.addEventListener?.('change', updateThemeColor)
}

export function applyTheme(value) {
  const chosen = known(THEMES, value) ? value : THEMES[0][0]
  document.documentElement.dataset.theme = chosen
  updateThemeColor()
  watchSystem()
  store(THEME_KEY, THEMES, chosen)
  return chosen
}

export function applyStyle(value) {
  const chosen = known(STYLES, value) ? value : STYLES[0][0]
  document.documentElement.dataset.style = chosen
  updateThemeColor()
  store(STYLE_KEY, STYLES, chosen)
  return chosen
}
