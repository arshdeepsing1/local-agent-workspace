// Short visible forms of absolute paths in the changes bar, tool cards and the Files roots. The full path
// always stays in titles and accessible names.

// True when an absolute path lies inside the absolute folder `root`.
export const inside = (path, root) => path.startsWith(root.endsWith('/') ? root : `${root}/`)

// Visible names for absolute folders: the last folder name, with as many parent names as it takes to
// tell equal names apart (the full path stays in the title and the accessible name).
export function shortNames(paths) {
  const parts = paths.map(path => path.split('/').filter(Boolean))
  const depth = parts.map(() => 1)
  const name = index => parts[index].slice(-depth[index]).join('/')
  for (let longer = true; longer;) {
    longer = false
    const names = parts.map((_, index) => name(index))
    names.forEach((value, index) => {
      if (depth[index] < parts[index].length && names.some((other, at) => at !== index && other === value)) { depth[index]++; longer = true }
    })
  }
  return new Map(paths.map((path, index) => [path, depth[index] >= parts[index].length ? path : name(index)]))
}

const trimSlash = path => path.length > 1 && path.endsWith('/') ? path.replace(/\/+$/, '') || '/' : path

// Shortens absolute paths inside the project (`workspace`, shown relative to it) and inside other absolute folders
// (`roots`: granted folders, the default code folder; shown as "<folder name>/rest", the deepest folder that holds
// the path). shorten(path) returns the path unchanged when it is relative, home-relative or under none of them.
export function pathShortener(workspace, roots = []) {
  const project = typeof workspace === 'string' && workspace.startsWith('/') ? trimSlash(workspace) : null
  const folders = [...new Set(roots.filter(root => typeof root === 'string' && root.startsWith('/')).map(trimSlash))]
    .filter(root => root !== '/' && root !== project)
  const names = shortNames(folders)
  const deepest = path => folders.filter(root => path === root || inside(path, root)).sort((a, b) => b.length - a.length)[0]
  const shorten = path => {
    if (typeof path !== 'string' || !path.startsWith('/')) return path
    const clean = trimSlash(path)
    const root = deepest(clean)
    // A folder inside the project is shown relative to the project; the project inside a folder, as the project.
    if (project && (clean === project || inside(clean, project)) && !(root && inside(root, project))) {
      return clean === project ? '.' : clean.slice(project.length + 1)
    }
    if (!root) return path
    return clean === root ? names.get(root) : `${names.get(root)}/${clean.slice(root.length + 1)}`
  }
  // True when shorten names an absolute path by the project or one of the folders. Its text can still equal the path:
  // a top-level folder ("/data") is named by its full path.
  shorten.known = path => {
    if (typeof path !== 'string' || !path.startsWith('/')) return false
    const clean = trimSlash(path)
    return Boolean(deepest(clean) || (project && (clean === project || inside(clean, project))))
  }
  return shorten
}

// The folder a changes-bar row names for a file: relative paths keep their folder, a file in one of `roots` is named
// by that folder ("other/pkg"), and any other absolute path by its last two folders ("…/a/b"). '' at the top level.
export function changedFileFolder(path, shorten) {
  const slash = path.lastIndexOf('/')
  if (slash <= 0) return ''
  const folder = path.slice(0, slash)
  if (!path.startsWith('/')) return folder
  if (shorten.known ? shorten.known(folder) : shorten(folder) !== folder) return shorten(folder)
  const parts = folder.split('/').filter(Boolean)
  return parts.length > 2 ? `…/${parts.slice(-2).join('/')}` : folder
}
