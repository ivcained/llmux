/**
 * LLMUX desktop controls.
 *
 * composer.actions contributes source, policy and floor selectors immediately
 * before the model pill through the public SDK. The seller card lists ranked
 * offers for the active model with TTFT, TPS and estimated reply time. Switching
 * re-sends the whole chat, so it always confirms through the app's ConfirmDialog
 * before pinning a seller mid-session.
 *
 * Recovery: if the gateway refuses the switch, stop any in-flight turn and offer
 * a clean session on the pinned route. Never leave a half-broken runtime.
 */

import {
  COMPOSER_AREAS,
  Badge,
  Button,
  ConfirmDialog,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  STATUSBAR_AREAS,
  atom,
  host,
  useValue,
} from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'llmux'
const $sourceByModel = atom({})
const $policy = atom('balanced')
const $floor = atom('')
let storage = null
let restFn = null

function setModelSource(model, source) {
  if (!model) {
    host.notify({ kind: 'warning', message: 'Select a model before choosing an LLMUX source.' })
    return
  }
  const next = { ...$sourceByModel.get() }
  if (source === 'auto') delete next[model]
  else next[model] = source
  $sourceByModel.set(next)
  storage?.set('sourceByModel', next)
}

function setPolicy(policy) {
  $policy.set(policy)
  storage?.set('policy', policy)
}

function setFloor(floor) {
  $floor.set(floor)
  storage?.set('floor', floor)
}

function fmtUsd(value) {
  if (value === null || value === undefined) return 'n/a'
  return value < 0.001 ? `$${value.toFixed(5)}` : `$${value.toFixed(4)}`
}

function fmtMs(ms) {
  if (ms === null || ms === undefined) return 'n/a'
  return `${(ms / 1000).toFixed(1)}s`
}

async function fetchOptions(model) {
  if (!restFn) {
    throw new Error('LLMUX backend is not enabled. Enable it in Settings → Plugins → LLMUX.')
  }
  const body = await restFn(`/seller-options?model=${encodeURIComponent(model)}`)
  const rows = Array.isArray(body?.data) ? body.data : []
  if (rows[0]?.error) throw new Error(rows[0].error)
  return rows
}

function Controls() {
  const model = useValue(host.state.model)
  const sourceByModel = useValue($sourceByModel)
  const policy = useValue($policy)
  const floor = useValue($floor)
  const source = sourceByModel[model] || 'auto'

  return jsxs('div', {
    className: 'flex items-center gap-1',
    children: [
      jsxs(Select, {
        value: source,
        onValueChange: value => setModelSource(model, value),
        children: [
          jsx(SelectTrigger, {
            className: 'h-7 w-[112px] text-xs',
            'aria-label': `LLMUX source for ${model || 'the current model'}`,
            children: jsx(SelectValue, {}),
          }),
          jsxs(SelectContent, {
            children: [
              jsx(SelectItem, { value: 'auto', children: 'Auto source' }),
              jsx(SelectItem, { value: 'surplus', children: 'Surplus' }),
              jsx(SelectItem, { value: 'antseed', children: 'Antseed' }),
            ],
          }),
        ],
      }),
      jsxs(Select, {
        value: policy,
        onValueChange: setPolicy,
        children: [
          jsx(SelectTrigger, {
            className: 'h-7 w-[102px] text-xs',
            'aria-label': 'LLMUX routing policy',
            children: jsx(SelectValue, {}),
          }),
          jsxs(SelectContent, {
            children: [
              jsx(SelectItem, { value: 'cheapest', children: 'Cheapest' }),
              jsx(SelectItem, { value: 'fastest', children: 'Fastest' }),
              jsx(SelectItem, { value: 'balanced', children: 'Balanced' }),
            ],
          }),
        ],
      }),
      jsxs(Select, {
        value: floor || 'off',
        onValueChange: value => setFloor(value === 'off' ? '' : value),
        children: [
          jsx(SelectTrigger, {
            className: 'h-7 w-[104px] text-xs',
            'aria-label': 'LLMUX TPS floor',
            children: jsx(SelectValue, {}),
          }),
          jsxs(SelectContent, {
            children: [
              jsx(SelectItem, { value: 'off', children: 'No TPS floor' }),
              jsx(SelectItem, { value: 'low', children: 'TPS ≥10' }),
              jsx(SelectItem, { value: 'mid', children: 'TPS ≥25' }),
              jsx(SelectItem, { value: 'high', children: 'TPS ≥60' }),
            ],
          }),
        ],
      }),
    ],
  })
}

function OfferRow({ option, onChoose }) {
  const cache = option.cache_warning
  return jsxs('div', {
    className: 'flex items-center justify-between gap-2 rounded-md border border-(--ui-stroke-secondary) px-3 py-2',
    children: [
      jsxs('div', {
        className: 'min-w-0 flex-1',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('span', { className: 'truncate text-sm font-medium', children: option.seller }),
              jsx(Badge, { variant: 'outline', children: option.source }),
            ],
          }),
          jsx('div', {
            className: 'mt-1 text-[11px] text-(--ui-text-secondary)',
            children: `TTFT ${fmtMs(option.ttft_p50_ms)} · ${option.tps_p50 || '?'} tok/s · ~${option.estimated_reply_seconds}s for 1k tokens · ${fmtUsd(option.input_per_m)}/${fmtUsd(option.output_per_m)} per 1M · ${option.samples} samples`,
          }),
          cache
            ? jsx('div', {
                className: 'mt-1 text-[11px] text-(--ui-text-secondary)',
                children: `Re-sends ~${cache.prompt_tokens.toLocaleString()} prompt tokens · uncached input ${fmtUsd(cache.uncached_input_cost_usd)}`,
              })
            : null,
        ],
      }),
      jsx(Button, {
        size: 'sm',
        variant: 'outline',
        className: 'shrink-0',
        onClick: () => onChoose(option),
        children: 'Switch',
      }),
    ],
  })
}

function SwitchDialog({ model, onClose }) {
  const [options, setOptions] = useState(null)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(null)
  const [switchFailed, setSwitchFailed] = useState(null)

  useEffect(() => {
    let live = true
    setOptions(null)
    setError(null)
    fetchOptions(model).then(
      rows => { if (live) setOptions(rows) },
      err => { if (live) setError(err instanceof Error ? err.message : String(err)) },
    )
    return () => { live = false }
  }, [model])

  async function applySwitch(option) {
    const sessionId = host.state.focusedSessionId.get()
    if (host.state.busy.get() && sessionId) {
      await host.request('session.interrupt', { session_id: sessionId })
    }
    const directive = option.peer_id ? `@peer:${option.peer_id}` : ''
    const target = `${model}@${option.source}${directive}`
    try {
      await host.request('config.set', {
        key: 'model',
        session_id: sessionId,
        value: `${target} --provider llmux --session`,
      })
      host.notify({
        kind: 'success',
        message: `Pinned ${option.seller}. Next turn re-sends the full chat on this seller.`,
      })
      setSelected(null)
      onClose()
    } catch (err) {
      setSelected(null)
      setSwitchFailed({ message: err instanceof Error ? err.message : String(err), target })
    }
  }

  async function recoverSession() {
    const sessionId = host.state.focusedSessionId.get()
    if (host.state.busy.get() && sessionId) {
      await host.request('session.interrupt', { session_id: sessionId }).catch(() => {})
    }
    host.newChat(host.state.focusedSessionProfile.get())
    host.notify({
      kind: 'info',
      message: `Opened a clean session. Pick ${switchFailed.target} in the model picker to use the pinned seller.`,
    })
    setSwitchFailed(null)
    onClose()
  }

  return jsxs(DialogContent, {
    className: 'max-w-xl',
    children: [
      jsxs(DialogHeader, {
        children: [
          jsx(DialogTitle, { children: 'Switch seller mid-chat' }),
          jsx(DialogDescription, {
            children: `Four fastest routes for ${model}. Switching re-sends the whole chat and drops the cached-prefix discount, so confirm before you pin.`,
          }),
        ],
      }),
      error
        ? jsx('div', { className: 'rounded border border-(--ui-stroke-secondary) p-3 text-sm', children: error })
        : null,
      switchFailed
        ? jsxs('div', {
            className: 'rounded border border-(--ui-stroke-secondary) p-3 text-sm',
            children: [
              jsx('p', { children: `Session model update failed: ${switchFailed.message}` }),
              jsx('p', {
                className: 'mt-1 text-(--ui-text-secondary)',
                children: 'Recover with a clean session instead of retrying the same runtime.',
              }),
              jsxs('div', {
                className: 'mt-3 flex gap-2',
                children: [
                  jsx(Button, { size: 'sm', onClick: recoverSession, children: 'New session on pinned seller' }),
                  jsx(Button, { size: 'sm', variant: 'ghost', onClick: onClose, children: 'Cancel' }),
                ],
              }),
            ],
          })
        : null,
      options === null && !error
        ? jsx('div', { className: 'py-6 text-sm text-(--ui-text-secondary)', children: 'Loading live offers…' })
        : options?.length === 0
          ? jsx('div', { className: 'py-6 text-sm text-(--ui-text-secondary)', children: 'No measured offers for this model. Start the proxy or wait for the catalog to refresh.' })
          : jsxs('div', {
              className: 'flex max-h-[50vh] flex-col gap-2 overflow-y-auto',
              children: (options || []).map(option =>
                jsx(OfferRow, { option, onChoose: setSelected, key: `${option.source}:${option.peer_id || 'default'}` }),
              ),
            }),
      jsx(ConfirmDialog, {
        open: selected !== null,
        onClose: () => setSelected(null),
        onConfirm: () => applySwitch(selected),
        title: `Switch to ${selected?.seller || 'seller'}?`,
        description: 'This re-sends the entire conversation to the new seller. Cached-prefix discounts are lost, input is billed again, and any in-flight turn is stopped first. The switch applies to the next turn; nothing replays automatically.',
        confirmLabel: 'Switch seller and re-send chat',
        busyLabel: 'Switching…',
        cancelLabel: 'Keep current seller',
      }),
    ],
  })
}

function SwitchCard() {
  const model = useValue(host.state.model)
  const [open, setOpen] = useState(false)
  const busy = useValue(host.state.busy)

  return jsxs('div', {
    className: 'flex items-center gap-1',
    children: [
      jsx(Button, {
        size: 'sm',
        variant: 'outline',
        className: 'h-7 text-xs',
        onClick: () => setOpen(true),
        children: busy ? 'LLMUX: seller…' : 'LLMUX seller',
      }),
      open
        ? jsx(Dialog, {
            open: true,
            onOpenChange: next => { if (!next) setOpen(false) },
            children: jsx(SwitchDialog, { model, onClose: () => setOpen(false) }),
          })
        : null,
    ],
  })
}

export default {
  id: ID,
  name: 'LLMUX',
  defaultEnabled: false,
  register(ctx) {
    storage = ctx.storage
    restFn = ctx.rest ? (path, options) => ctx.rest(path, options) : null
    Promise.all([
      ctx.storage.get('sourceByModel'),
      ctx.storage.get('policy'),
      ctx.storage.get('floor'),
    ]).then(([savedSources, savedPolicy, savedFloor]) => {
      if (savedSources && typeof savedSources === 'object' && !Array.isArray(savedSources)) {
        $sourceByModel.set(savedSources)
      }
      if (['cheapest', 'fastest', 'balanced'].includes(savedPolicy)) $policy.set(savedPolicy)
      if (typeof savedFloor === 'string') $floor.set(savedFloor)
    }).catch(() => {
      host.notify({ kind: 'warning', message: 'LLMUX could not restore saved routing controls.' })
    })

    ctx.register({
      id: 'routing-controls',
      area: COMPOSER_AREAS.actions,
      order: 20,
      render: () => jsx(Controls, {}),
    })
    ctx.register({
      id: 'seller-switch',
      area: STATUSBAR_AREAS.right,
      order: 30,
      render: () => jsx(SwitchCard, {}),
    })
  },
}
