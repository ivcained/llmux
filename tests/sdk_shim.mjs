/**
 * Simulated @hermes/plugin-sdk for the LLMUX desktop seller-card tests.
 *
 * This is a SIMULATED HOST, not the installed desktop app: it runs the real
 * plugin file against real React and real nanostores atoms, with every host
 * door (request, notify, newChat, rest, surfaceModelSwitchConfirm) recorded
 * in `seq` so tests assert call order and payloads. The installed-plugin
 * check in Hermes Desktop is a separate gate.
 *
 * Only the SDK surface desktop/plugin.js imports is implemented.
 */

import { atom } from 'nanostores'
import { useEffect, useState, useSyncExternalStore } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

export { atom }

export const COMPOSER_AREAS = {
  top: 'composer.top',
  bottom: 'composer.bottom',
  underside: 'composer.underside',
  leading: 'composer.leading',
  actions: 'composer.actions',
  middleware: 'composer.middleware',
  attachments: 'composer.attachments',
  microActions: 'composer.microActions',
  atCompletions: 'composer.atCompletions',
  modelPill: 'composer.modelPill',
}
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }

/** Ordered event log across every host door: { type: 'rest' | 'rpc' | 'notify' | 'newChat' | 'surfaceModelSwitchConfirm', ... }. */
export const seq = []
export const notifications = []
export const newChats = []

export const state = {
  model: atom('test-model'),
  focusedSessionId: atom('sess-1'),
  focusedSessionOwner: atom({ connectionId: 'local', profile: 'default' }),
  focusedSessionProfile: atom('default'),
  focusedUsage: atom({ context_used: 12000 }),
  busy: atom(false),
  busyBySession: atom({}),
}

let restImpl = async () => ({ data: [] })
let requestImpl = async () => ({})
let surfaceImpl = async () => false

export function reset(overrides = {}) {
  state.model.set(overrides.model ?? 'test-model')
  state.focusedSessionId.set(overrides.sessionId === undefined ? 'sess-1' : overrides.sessionId)
  state.focusedSessionOwner.set(
    overrides.owner === undefined ? { connectionId: 'local', profile: 'default' } : overrides.owner,
  )
  state.focusedSessionProfile.set(overrides.profile ?? 'default')
  state.focusedUsage.set(overrides.usage === undefined ? { context_used: 12000 } : overrides.usage)
  state.busy.set(Boolean(overrides.busy))
  state.busyBySession.set(overrides.busyBySession || {})
  seq.length = 0
  notifications.length = 0
  newChats.length = 0
  restImpl = overrides.rest || (async () => ({ data: [] }))
  requestImpl = overrides.request || (async () => ({}))
  surfaceImpl = overrides.surface || (async () => false)
}

export function setModel(model) {
  state.model.set(model)
}
export function setFocusedSessionId(id) {
  state.focusedSessionId.set(id)
}
export function setFocusedSessionOwner(owner) {
  state.focusedSessionOwner.set(owner)
}

export const host = {
  state,
  request: async (method, params) => {
    seq.push({ type: 'rpc', method, params })
    return requestImpl(method, params)
  },
  notify: notice => {
    notifications.push(notice)
    seq.push({ type: 'notify', kind: notice.kind, message: notice.message })
  },
  newChat: profile => {
    newChats.push(profile)
    seq.push({ type: 'newChat', profile })
  },
}

/** ctx.rest stand-in: records the call, then defers to the test's impl. */
export function rest(path, options) {
  seq.push({ type: 'rest', path, options })
  return restImpl(path, options)
}

/** Shared guarded-switch applier stand-in. Never auto-confirms: the test's
 *  impl decides what the human answered. */
export function surfaceModelSwitchConfirm(options) {
  seq.push({ type: 'surfaceModelSwitchConfirm', confirmMessage: options.confirmMessage || null, model: options.model })
  return surfaceImpl(options)
}

export function useValue(store) {
  return useSyncExternalStore(
    cb => store.listen(cb),
    () => store.get(),
  )
}

/** Minimal useQuery: runs queryFn when enabled, keyed by queryKey. */
export function useQuery({ queryKey, queryFn, enabled = true }) {
  const [state_, setState] = useState({ data: undefined, error: null, isPending: Boolean(enabled) })
  const key = JSON.stringify(queryKey)
  useEffect(() => {
    if (!enabled) {
      setState({ data: undefined, error: null, isPending: false })
      return undefined
    }
    let live = true
    setState({ data: undefined, error: null, isPending: true })
    Promise.resolve()
      .then(queryFn)
      .then(
        data => {
          if (live) setState({ data, error: null, isPending: false })
        },
        error => {
          if (live) setState({ data: undefined, error, isPending: false })
        },
      )
    return () => {
      live = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled])
  return state_
}

export function Button({ children, onClick }) {
  return jsx('button', { onClick, children })
}

export function Badge({ children }) {
  return jsx('span', { children })
}

export function Dialog({ open, children }) {
  return open ? jsx('div', { 'data-testid': 'dialog', children }) : null
}

export function DialogContent({ children }) {
  return jsx('div', { children })
}

export function DialogHeader({ children }) {
  return jsx('div', { children })
}

export function DialogTitle({ children }) {
  return jsx('h2', { children })
}

export function DialogDescription({ children }) {
  return jsx('p', { children })
}

/** ConfirmDialog stand-in: renders the real props and wires confirm/cancel to
 *  buttons. onConfirm errors surface inline, like the app's dialog. */
export function ConfirmDialog(props) {
  const [error, setError] = useState(null)
  if (!props.open) return null
  return jsxs('div', {
    'data-testid': 'confirm-dialog',
    children: [
      jsx('div', { 'data-testid': 'confirm-title', children: props.title }),
      jsx('div', { 'data-testid': 'confirm-description', children: props.description }),
      error ? jsx('div', { 'data-testid': 'confirm-error', children: error }) : null,
      jsx('button', {
        'data-testid': 'confirm-accept',
        onClick: async () => {
          try {
            await props.onConfirm()
          } catch (err) {
            setError(err instanceof Error ? err.message : String(err))
          }
        },
        children: props.confirmLabel || 'Confirm',
      }),
      jsx('button', {
        'data-testid': 'confirm-cancel',
        onClick: props.onClose,
        children: props.cancelLabel || 'Cancel',
      }),
    ],
  })
}
