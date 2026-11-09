#!/usr/bin/env node
/**
 * Persona-run renderer: renders the REAL desktop/plugin.js in jsdom with LIVE
 * proxy payloads (no synthetic offers) for each UX scenario, and dumps the
 * full visible text of the dialog. Used by the adversarial UX test: the
 * persona sees exactly what a user sees, and every claim can be re-verified
 * against this same run.
 *
 * Usage: node tests/persona_render.mjs [--scenario name]
 * Writes plain-text transcripts to /Users/lc/.hermes/cache/scratch/persona-ux/
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const TESTS_DIR = path.dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = path.dirname(TESTS_DIR)
const PLUGIN_SOURCE = path.join(REPO_ROOT, 'desktop', 'plugin.js')
const TRANSFORMED = path.join(TESTS_DIR, '.persona-plugin.mjs')
const LINK_DIR = path.join(TESTS_DIR, 'node_modules')
const OUT_DIR = '/Users/lc/.hermes/cache/scratch/persona-ux'

const NEEDED = ['react', 'react-dom', 'jsdom', 'nanostores', '@testing-library/react', '@testing-library/dom']
const LINKED = ['react', 'react-dom', 'jsdom', 'nanostores', '@testing-library']
const DEPS_CANDIDATES = [
  process.env.LLMUX_HOST_DEPS,
  '/Users/lc/.hermes/hermes-agent/node_modules',
].filter(Boolean)
function resolveDepsRoot() {
  for (const root of DEPS_CANDIDATES) {
    if (NEEDED.every(name => fs.existsSync(path.join(root, name, 'package.json')))) return root
  }
  throw new Error('no host deps root')
}

const depsRoot = resolveDepsRoot()
fs.rmSync(LINK_DIR, { recursive: true, force: true })
fs.mkdirSync(LINK_DIR, { recursive: true })
for (const name of LINKED) fs.symlinkSync(path.join(depsRoot, name), path.join(LINK_DIR, name))
const transformed = fs.readFileSync(PLUGIN_SOURCE, 'utf8').replaceAll("'@hermes/plugin-sdk'", "'./sdk_shim.mjs'")
fs.writeFileSync(TRANSFORMED, transformed)
process.on('exit', () => {
  fs.rmSync(LINK_DIR, { recursive: true, force: true })
  fs.rmSync(TRANSFORMED, { force: true })
})

const { JSDOM } = await import('jsdom')
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' })
globalThis.window = dom.window
globalThis.document = dom.window.document
Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true })
globalThis.HTMLElement = dom.window.HTMLElement
globalThis.Node = dom.window.Node
globalThis.MutationObserver = dom.window.MutationObserver
globalThis.getComputedStyle = dom.window.getComputedStyle.bind(dom.window)
globalThis.Event = dom.window.Event
globalThis.IS_REACT_ACT_ENVIRONMENT = true

const shim = await import('./sdk_shim.mjs')
const plugin = await import('./.persona-plugin.mjs')
const { act, cleanup: unmount, fireEvent, render, waitFor } = await import('@testing-library/react')

const PROXY = 'http://127.0.0.1:8378'

async function live(pathname) {
  const res = await fetch(`${PROXY}${pathname}`, { signal: AbortSignal.timeout(15000) })
  if (!res.ok) throw new Error(`${pathname} -> HTTP ${res.status}`)
  return res.json()
}

const SCENARIOS = {
  // The core workflow: open the card on a real model mid-chat.
  'open-opus': { model: 'claude-opus-5-5', provider: 'solid' },
  // Same card for a surplus-routed chat.
  'open-kimi': { model: 'kimi-k3', provider: 'llmux' },
  // Empty state: brand-new chat, ledger empty, no rows for a made-up model.
  'cold-start': { model: 'nonexistent-model-xyz', provider: 'llmux' },
  // Error recovery: proxy dead entirely.
  'proxy-down': { model: 'claude-opus-5-5', provider: 'solid', dead: true },
}

const name = process.argv.includes('--scenario')
  ? process.argv[process.argv.indexOf('--scenario') + 1]
  : null
const toRun = name ? [[name, SCENARIOS[name]]] : Object.entries(SCENARIOS)

fs.mkdirSync(OUT_DIR, { recursive: true })
let failures = 0

for (const [scenario, cfg] of toRun) {
  const lines = []
  const emit = s => lines.push(s)
  try {
    if (cfg.dead) {
      // Point rest at a port with nothing listening.
      shim.reset({
        model: cfg.model,
        rest: async p => {
          emit(`[rest call] ${p}`)
          throw new Error('fetch failed: connection refused')
        },
        request: async (method) => {
          if (method === 'model.options') return { model: cfg.model, provider: cfg.provider }
          return {}
        },
      })
    } else {
      const options = await live(`/llmux/seller-options?model=${encodeURIComponent(cfg.model)}&provider=${cfg.provider}`)
      emit(`[live payload keys] ${Object.keys(options).join(', ')}`)
      shim.reset({
        model: cfg.model,
        rest: async p => {
          emit(`[rest call] ${p}`)
          if (p.startsWith('/seller-options')) return options
          if (p.startsWith('/similar-models')) return { data: options.similar || [] }
          if (p === '/validate-route') return { ok: true, plan: { model: 'planned' } }
          throw new Error(`unexpected path ${p}`)
        },
        request: async (method) => {
          if (method === 'model.options') return { model: cfg.model, provider: cfg.provider }
          return {}
        },
      })
    }
    const contributions = []
    plugin.default.register({ rest: shim.rest, register: c => contributions.push(c) })
    const card = contributions.find(c => c.id === 'seller-switch')
    const view = render(card.render())
    // Multiple LLMUX buttons (composer card + dialog card) are legitimate.
    const buttons = view.getAllByText('LLMUX')
    fireEvent.click(buttons[0])
    await waitFor(() => view.getByTestId('dialog'), { timeout: 8000 })
    // Give queries a beat to resolve; live payloads are already in the mock.
    await act(async () => { await Promise.resolve() })
    emit('')
    emit('===== DIALOG VISIBLE TEXT =====')
    const node = view.getByTestId('dialog')
    const raw = node.innerText
    const text = (typeof raw === 'string' ? raw : node.textContent || '').replace(/\n{3,}/g, '\n\n').trim()
    emit(text)
    emit('===== END =====')
    // Count the interactions a user needs for the core task: switch seller.
    const switches = view.getAllByText('Switch').length
    emit(`Switch buttons: ${switches}`)
    const sorts = ['Recommended', 'Speed', 'Price', 'Discount'].filter(s => view.queryByText(s)).length
    emit(`Sort options present: ${sorts}`)
    unmount()
  } catch (err) {
    failures += 1
    emit(`RENDER FAILED: ${err.message}`)
  }
  fs.writeFileSync(path.join(OUT_DIR, `${scenario}.txt`), lines.join('\n'))
  console.log(`${scenario}: ${failures ? 'see file' : 'ok'} (${lines.length} lines)`)
}
process.exit(failures ? 1 : 0)
