// Renders one UI module in isolation for a test:
//   await mount('components/Conversation.js', () => ({ session, onError: fake.spy('onError') }))
//   rerender(() => ({ ...newProps }))   unmount()
//   await probe('useWorkspace.js', 'useWorkspace')  then read window.hook
import { h, options, render } from 'preact'
import { useState } from 'preact/hooks'

// Run effects right after each render instead of after the next animation
// frame, so tests do not depend on frame timing or on a faked clock.
options.requestAnimationFrame = callback => queueMicrotask(callback)

const root = document.getElementById('root')
let setProps = null

async function load(module, name = 'default') {
  const loaded = await import(`/static/js/${module}`)
  if (!loaded[name]) throw new Error(`${module} has no export ${name}`)
  return loaded[name]
}

function Host({ component, initial }) {
  const [props, update] = useState(initial)
  setProps = update
  return h(component, props)
}

// Rendering is synchronous, so each helper returns once the component is on the page.
window.mount = async (module, makeProps = () => ({}), name = 'default') => {
  const component = await load(module, name)
  render(h(Host, { component, initial: makeProps() }), root)
}
window.rerender = makeProps => setProps(makeProps())
window.unmount = () => render(null, root)

// Mounts a component that calls a hook and publishes its latest result.
window.probe = async (module, name) => {
  const hook = await load(module, name)
  function Probe() { window.hook = hook(); return null }
  render(h(Probe), root)
}
window.harnessReady = true
