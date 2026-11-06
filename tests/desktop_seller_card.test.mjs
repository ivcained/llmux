#!/usr/bin/env node
/**
 * Executable behavior tests for the LLMUX desktop seller card.
 *
 * These drive the REAL desktop/plugin.js (its actual event handlers, rendered
 * by real React into jsdom) against the simulated SDK host in
 * tests/sdk_shim.mjs. No source-string assertions: every check clicks buttons
 * or calls exported functions and inspects recorded host calls or DOM text.
 *
 * Command:
 *   node tests/desktop_seller_card.test.mjs
 *
 * Host dependencies (react, react-dom, jsdom, nanostores, @testing-library)
 * are symlinked from the installed Hermes desktop repo into tests/node_modules
 * at run start and removed at exit. Override the deps root with
 * LLMUX_HOST_DEPS=/path/to/node_modules. Keep the symlink dir out of git.
 *
 * This is a simulated-host gate; the installed-plugin check in Hermes Desktop
 * remains a separate, manual gate.
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const TESTS_DIR = path.dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = path.dirname(TESTS_DIR)
const PLUGIN_SOURCE = path.join(REPO_ROOT, 'desktop', 'plugin.js')
const TRANSFORMED = path.join(TESTS_DIR, '.plugin-under-test.mjs')
const LINK_DIR = path.join(TESTS_DIR, 'node_modules')

const NEEDED = [
  'react',
  'react-dom',
  'jsdom',
  'nanostores',
  '@testing-library/react',
  '@testing-library/dom',
]
const LINKED = ['react', 'react-dom', 'jsdom', 'nanostores', '@testing-library']
const DEPS_CANDIDATES = [
  process.env.LLMUX_HOST_DEPS,
  '/Users/lc/.hermes/hermes-agent/node_modules',
  '/Users/lc/.hermes/hermes-agent/apps/desktop/node_modules',
].filter(Boolean)

function resolveDepsRoot() {
  for (const root of DEPS_CANDIDATES) {
    if (NEEDED.every(name => fs.existsSync(path.join(root, name, 'package.json')))) return root
  }
  throw new Error(
    `No host dependency root with ${NEEDED.join(', ')}. Tried: ${DEPS_CANDIDATES.join(', ')}. ` +
      'Set LLMUX_HOST_DEPS to a node_modules that has them.',
  )
}

function linkDeps(root) {
  fs.rmSync(LINK_DIR, { recursive: true, force: true })
  fs.mkdirSync(LINK_DIR, { recursive: true })
  for (const name of LINKED) {
    fs.symlinkSync(path.join(root, name), path.join(LINK_DIR, name), 'dir')
  }
}

function transformPlugin() {
  const source = fs.readFileSync(PLUGIN_SOURCE, 'utf8')
  assert.ok(source.includes("'@hermes/plugin-sdk'"), 'plugin must import the SDK specifier')
  const transformed = source.replaceAll("'@hermes/plugin-sdk'", "'./sdk_shim.mjs'")
  fs.writeFileSync(TRANSFORMED, transformed)
}

function cleanup() {
  fs.rmSync(LINK_DIR, { recursive: true, force: true })
  fs.rmSync(TRANSFORMED, { force: true })
}

const depsRoot = resolveDepsRoot()
linkDeps(depsRoot)
transformPlugin()
process.on('exit', cleanup)

// jsdom globals must exist before react-dom / @testing-library load.
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
const plugin = await import('./.plugin-under-test.mjs')
const { act, cleanup: unmount, fireEvent, render, waitFor } = await import('@testing-library/react')

const OFFER = {
  source: 'surplus',
  seller: 'Surplus A',
  peer_id: null,
  upstream_model: 'test-model',
  tps_p50: 55,
  ttft_p50_ms: 400,
  samples: 12,
  input_per_m: 0.5,
  output_per_m: 1.5,
  discount_pct: 90,
  estimated_reply_seconds: 18.6,
  cache_warning: {
    text: 'The next request sends the current conversation context to the selected route.',
    prompt_tokens: null,
    uncached_input_cost_usd: null,
  },
}

function offer(overrides = {}) {
  return { ...OFFER, ...overrides }
}

function offersPayload(rows) {
  return async path => {
    if (path.startsWith('/seller-options')) return { data: rows }
    if (path === '/validate-route') return { ok: true, plan: { model: 'planned' } }
    throw new Error(`unexpected rest path: ${path}`)
  }
}

function registerCard() {
  const contributions = []
  plugin.default.register({ rest: shim.rest, register: c => contributions.push(c) })
  const card = contributions.find(c => c.id === 'seller-switch')
  assert.ok(card, 'seller-switch contribution registered')
  return card
}

async function openDialog(overrides = {}) {
  shim.reset(overrides)
  const card = registerCard()
  const view = render(card.render())
  fireEvent.click(view.getByText(overrides.busy ? 'LLMUX: seller…' : 'LLMUX seller'))
  await waitFor(() => assert.ok(view.getByTestId('dialog')))
  return view
}

async function openAndPick(view) {
  await waitFor(() => assert.ok(view.getAllByText('Switch').length > 0))
  fireEvent.click(view.getAllByText('Switch')[0])
  await waitFor(() => assert.ok(view.getByTestId('confirm-dialog')))
}

async function confirm(view) {
  await act(async () => {
    fireEvent.click(view.getByTestId('confirm-accept'))
  })
}

function seqIndex(predicate) {
  return shim.seq.findIndex(predicate)
}

// --- tests -----------------------------------------------------------------

const tests = []
function test(name, fn) {
  tests.push([name, fn])
}

test('registers composer-adjacent (composer.actions), not the statusbar', () => {
  shim.reset({ rest: offersPayload([offer()]) })
  const card = registerCard()
  assert.equal(card.area, 'composer.actions')
})

test('renders at most four offers with median labels and a 1000-output-token estimate', async () => {
  const rows = [
    offer(),
    offer({ seller: 'Antseed B', source: 'antseed', peer_id: 'peer9' }),
    offer({ seller: 'Surplus C', peer_id: 'peerC' }),
    offer({ seller: 'Antseed D', source: 'antseed' }),
    offer({ seller: 'Surplus E', peer_id: 'peerE' }),
    offer({ seller: 'Antseed F', source: 'antseed', peer_id: 'peerF' }),
  ]
  const view = await openDialog({ rest: offersPayload(rows) })
  await waitFor(() => assert.equal(view.getAllByText('Switch').length, 4))
  const text = view.getByTestId('dialog').textContent
  assert.ok(text.includes('median TTFT 0.4s'), 'p50 TTFT labelled median')
  assert.ok(text.includes('median 55 tok/s'), 'p50 TPS labelled median')
  assert.ok(text.includes('~18.6s estimated for 1000 output tokens'), 'estimate names 1000 output tokens')
  assert.ok(text.includes('In $0.5'), 'input price in AntSeed style')
  assert.ok(text.includes('Out $1.5'), 'output price in AntSeed style')
  assert.ok(text.includes('/M tok'), 'per-million unit shown')
  assert.ok(text.includes('90% off list'), 'discount shown against list price')
  assert.ok(text.includes('12 samples'), 'sample count shown')
  assert.ok(!text.includes('Surplus E') && !text.includes('Antseed F'), 'fifth and sixth offers cut')
  unmount()
})

test('unknown measurements are rendered as unknown, not guessed', async () => {
  const view = await openDialog({
    rest: offersPayload([offer({ tps_p50: null, ttft_p50_ms: null, estimated_reply_seconds: null, samples: null, discount_pct: null })]),
  })
  await waitFor(() => assert.ok(view.getAllByText('Switch').length === 1))
  const text = view.getByTestId('dialog').textContent
  assert.ok(text.includes('median TTFT unknown'), 'missing TTFT honest')
  assert.ok(text.includes('tok/s unknown'), 'missing TPS honest')
  assert.ok(text.includes('reply estimate unknown'), 'missing estimate honest')
  assert.ok(text.includes('samples unknown'), 'missing samples honest')
  assert.ok(!text.includes('off list'), 'missing discount honest')
  unmount()
})

test('cache warning uses live focusedUsage context_used and the route price', async () => {
  const view = await openDialog({ usage: { context_used: 12345 }, rest: offersPayload([offer()]) })
  await waitFor(() => assert.ok(view.getAllByText('Switch').length === 1))
  const text = view.getByTestId('dialog').textContent
  assert.ok(text.includes('12,345 context tokens'), 'context token count from focusedUsage')
  assert.ok(text.includes('uncached input $0.0062'), 'cost = 12345 * $0.5/1M on this route')
  unmount()
})

test('cache warning stays honest when context size is not reported', async () => {
  const view = await openDialog({ usage: null, rest: offersPayload([offer()]) })
  await waitFor(() => assert.ok(view.getAllByText('Switch').length === 1))
  assert.ok(view.getByTestId('dialog').textContent.includes('context size is not reported yet'))
  unmount()
})

test('happy path: validate before interrupt before config.set, then verified success', async () => {
  const view = await openDialog({ busy: true, rest: offersPayload([offer({ peer_id: 'peer9' })]) })
  await openAndPick(view)
  assert.equal(view.getByTestId('confirm-title').textContent, 'Switch to Surplus A?')
  await confirm(view)
  await waitFor(() =>
    assert.ok(shim.notifications.some(n => n.kind === 'success' && n.message.includes('Pinned Surplus A'))),
  )
  const iValidate = seqIndex(e => e.type === 'rest' && e.path === '/validate-route')
  const iInterrupt = seqIndex(e => e.type === 'rpc' && e.method === 'session.interrupt')
  const iSet = seqIndex(e => e.type === 'rpc' && e.method === 'config.set')
  assert.ok(iValidate > -1, 'route validated')
  assert.ok(iInterrupt > iValidate, 'interrupt only after validation')
  assert.ok(iSet > iInterrupt, 'config.set only after interrupt')
  const validate = shim.seq[iValidate]
  assert.equal(validate.options.method, 'POST')
  assert.deepEqual(validate.options.body, { model: 'test-model@surplus@peer:peer9' })
  const interrupt = shim.seq[iInterrupt]
  assert.deepEqual(interrupt.params, { session_id: 'sess-1' })
  const setCall = shim.seq[iSet]
  assert.deepEqual(setCall.params, {
    key: 'model',
    session_id: 'sess-1',
    value: 'test-model@surplus@peer:peer9 --provider llmux --session',
  })
  const success = shim.notifications.find(n => n.kind === 'success')
  assert.ok(success.message.includes('nothing replays automatically'), 'no auto-replay promise broken')
  await waitFor(() => assert.equal(view.queryByTestId('dialog'), null))
  unmount()
})

test('no in-flight turn means no interrupt', async () => {
  const view = await openDialog({ rest: offersPayload([offer()]) })
  await openAndPick(view)
  await confirm(view)
  await waitFor(() => assert.ok(shim.notifications.some(n => n.kind === 'success')))
  assert.equal(seqIndex(e => e.type === 'rpc' && e.method === 'session.interrupt'), -1)
  unmount()
})

test('validation failure aborts before any session mutation', async () => {
  const view = await openDialog({
    busy: true,
    rest: async path => {
      if (path.startsWith('/seller-options')) return { data: [offer()] }
      throw new Error('route not planned')
    },
  })
  await openAndPick(view)
  await confirm(view)
  await waitFor(() => assert.ok(view.getByTestId('confirm-error').textContent.includes('route not planned')))
  assert.equal(seqIndex(e => e.type === 'rpc'), -1, 'no interrupt, no config.set')
  assert.equal(shim.notifications.length, 0, 'no success claimed')
  unmount()
})

test('no focused session refuses honestly before validation', async () => {
  const view = await openDialog({ sessionId: null, rest: offersPayload([offer()]) })
  await openAndPick(view)
  await confirm(view)
  await waitFor(() =>
    assert.ok(view.getByTestId('confirm-error').textContent.includes('No focused chat session')),
  )
  assert.equal(seqIndex(e => e.type === 'rest' && e.path === '/validate-route'), -1)
  assert.equal(seqIndex(e => e.type === 'rpc'), -1)
  unmount()
})

test('focus change between pick and confirm rejects the switch', async () => {
  const view = await openDialog({ rest: offersPayload([offer()]) })
  await openAndPick(view)
  act(() => shim.setFocusedSessionId('sess-2'))
  await confirm(view)
  await waitFor(() =>
    assert.ok(view.getByTestId('confirm-error').textContent.includes('focused chat or model changed')),
  )
  assert.equal(seqIndex(e => e.type === 'rpc' && e.method === 'config.set'), -1)
  assert.equal(shim.notifications.filter(n => n.kind === 'success').length, 0)
  unmount()
})

test('owner change between pick and confirm rejects the switch', async () => {
  const view = await openDialog({ rest: offersPayload([offer()]) })
  await openAndPick(view)
  act(() => shim.setFocusedSessionOwner({ connectionId: 'other-box', profile: 'default' }))
  await confirm(view)
  await waitFor(() =>
    assert.ok(view.getByTestId('confirm-error').textContent.includes('focused chat or model changed')),
  )
  assert.equal(seqIndex(e => e.type === 'rpc' && e.method === 'config.set'), -1)
  unmount()
})

test('model change between pick and confirm rejects the switch', async () => {
  const view = await openDialog({ rest: offersPayload([offer()]) })
  await openAndPick(view)
  act(() => shim.setModel('other-model'))
  await confirm(view)
  await waitFor(() =>
    assert.ok(view.getByTestId('confirm-error').textContent.includes('focused chat or model changed')),
  )
  assert.equal(seqIndex(e => e.type === 'rpc' && e.method === 'config.set'), -1)
  unmount()
})

test('confirm_required goes to the shared human flow and resends only on accept', async () => {
  let setCalls = 0
  const view = await openDialog({
    rest: offersPayload([offer()]),
    request: async (method, params) => {
      if (method !== 'config.set') return {}
      setCalls += 1
      return setCalls === 1 ? { confirm_required: true, confirm_message: 'Costs more than usual.' } : {}
    },
    surface: async opts => {
      assert.equal(opts.confirmMessage, 'Costs more than usual.')
      const res = await opts.requestConfirmed()
      opts.finish?.(res)
      return true
    },
  })
  await openAndPick(view)
  await confirm(view)
  await waitFor(() => assert.ok(shim.notifications.some(n => n.kind === 'success')))
  const sets = shim.seq.filter(e => e.type === 'rpc' && e.method === 'config.set')
  assert.equal(sets.length, 2, 'initial set plus confirmed resend')
  assert.equal(sets[0].params.confirm_expensive_model, undefined)
  assert.equal(sets[1].params.confirm_expensive_model, true)
  assert.ok(seqIndex(e => e.type === 'surfaceModelSwitchConfirm') > -1, 'shared confirm flow used')
  unmount()
})

test('confirm_required declined by the human applies nothing further', async () => {
  const view = await openDialog({
    rest: offersPayload([offer()]),
    request: async method => (method === 'config.set' ? { confirm_required: true, confirm_message: 'Costs more.' } : {}),
    surface: async () => false,
  })
  await openAndPick(view)
  await confirm(view)
  await waitFor(() => assert.ok(seqIndex(e => e.type === 'surfaceModelSwitchConfirm') > -1))
  const sets = shim.seq.filter(e => e.type === 'rpc' && e.method === 'config.set')
  assert.equal(sets.length, 1, 'no resend without a human confirm')
  assert.equal(shim.notifications.filter(n => n.kind === 'success').length, 0, 'no success claimed')
  unmount()
})

test('deferred switch is reported as queued, never as pinned', async () => {
  const view = await openDialog({
    rest: offersPayload([offer()]),
    request: async method => (method === 'config.set' ? { deferred: true } : {}),
  })
  await openAndPick(view)
  await confirm(view)
  await waitFor(() => assert.ok(shim.notifications.length === 1))
  const notice = shim.notifications[0]
  assert.equal(notice.kind, 'info')
  assert.ok(notice.message.includes('queued'), 'honest deferred wording')
  assert.ok(notice.message.includes('next turn'), 'names when it applies')
  assert.equal(shim.notifications.filter(n => n.kind === 'success').length, 0)
  unmount()
})

test('config.set failure offers an honestly labelled clean-session recovery', async () => {
  const view = await openDialog({
    rest: offersPayload([offer()]),
    request: async method => {
      if (method === 'config.set') throw new Error('gateway refused the model')
      return {}
    },
  })
  await openAndPick(view)
  await confirm(view)
  await waitFor(() => assert.ok(view.getByText(/Session model update failed: gateway refused the model/)))
  const recovery = view.getByText('Open clean session')
  fireEvent.click(recovery)
  await waitFor(() => assert.equal(shim.newChats.length, 1))
  assert.equal(shim.newChats[0], 'default', 'clean session opened on the focused profile')
  const notice = shim.notifications.find(n => n.kind === 'info')
  assert.ok(
    notice.message.includes('Pick test-model@surplus in the model picker'),
    'recovery names the route without claiming it is pinned',
  )
  assert.equal(shim.notifications.filter(n => n.kind === 'success').length, 0)
  unmount()
})

test('exported helpers: routeTarget, bareModel, uncachedInputCostUsd', () => {
  const { bareModel, routeTarget, uncachedInputCostUsd } = plugin
  assert.equal(bareModel('model@surplus@peer:x'), 'model')
  assert.equal(bareModel('plain'), 'plain')
  assert.equal(
    routeTarget({ source: 'surplus', peer_id: 'p1' }, 'model@old@peer:z'),
    'model@surplus@peer:p1',
    'prior route suffix stripped before re-pinning',
  )
  assert.equal(routeTarget({ source: 'antseed' }, 'model'), 'model@antseed')
  assert.equal(
    routeTarget({ canonical: 'canon@surplus@peer:p1', source: 'surplus' }, 'model'),
    'canon@surplus@peer:p1',
    'server canonical target wins verbatim',
  )
  assert.ok(Math.abs(uncachedInputCostUsd(12000, 0.5) - 0.006) < 1e-12)
  assert.equal(uncachedInputCostUsd(null, 0.5), null)
  assert.equal(uncachedInputCostUsd(12000, null), null)
})

// --- runner ----------------------------------------------------------------

let failures = 0
for (const [name, fn] of tests) {
  try {
    await fn()
    console.log(`PASS ${name}`)
  } catch (err) {
    failures += 1
    console.error(`FAIL ${name}`)
    console.error(err instanceof Error ? err.stack || err.message : err)
  }
}
unmount()
console.log(`\n${tests.length - failures}/${tests.length} passed (deps: ${depsRoot})`)
process.exit(failures === 0 ? 0 : 1)
