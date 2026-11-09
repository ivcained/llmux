/**
 * LLMUX desktop seller card.
 *
 * One composer-adjacent button (composer.actions, before the model pill) opens
 * the seller dialog for the focused chat: up to four measured routes for the
 * current model with median TTFT/TPS, an estimated reply time for a 1000
 * output token answer, and a cache warning computed from the focused session's
 * live `focusedUsage.context_used` (never an invented token count).
 *
 * Switching is bound to the chat the user picked the route in: the focused
 * runtime session id, its connection-qualified owner, and the model are
 * captured at pick time and re-checked before anything is applied. A focus or
 * owner change rejects the switch instead of pinning a seller on the wrong
 * chat. The candidate route is validated through the LLMUX backend
 * (POST /validate-route) BEFORE any in-flight turn is interrupted, and the
 * gateway's `config.set` answer is honored: `confirm_required` is routed to
 * the shared human confirm flow (never auto-confirmed), `deferred` is
 * reported as queued for the next turn, and only a clean response is reported
 * as pinned. Nothing replays automatically after a switch.
 *
 * Recovery: if the session model update fails, stop any in-flight turn and
 * offer a clean session; the pinned route is named so the user can pick it in
 * the model picker. Never leave a half-broken runtime.
 */

import {
  Badge,
  Button,
  COMPOSER_AREAS,
  ConfirmDialog,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  host,
  surfaceModelSwitchConfirm,
  useQuery,
  useValue,
} from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'llmux'
const MAX_OPTIONS = 4
const MAX_SIMILAR = 3
const LIVE_REFRESH_MS = 30_000

const SORTS = [
  { id: 'recommended', label: 'Recommended' },
  { id: 'speed', label: 'Speed' },
  { id: 'price', label: 'Price' },
  { id: 'firstToken', label: 'First token' },
  { id: 'discount', label: 'Discount' },
]

/** Finite number, or null so rows without a measurement sort last. */
function finiteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** Blended 4:1 input/output price; the server sends it, this is only a fallback. */
function blendedPerM(row) {
  const sent = finiteNumber(row?.blended_per_m)
  if (sent !== null) return sent
  const input = finiteNumber(row?.input_per_m)
  const output = finiteNumber(row?.output_per_m)
  return input === null || output === null ? null : 0.8 * input + 0.2 * output
}

/** Estimated seconds for a 1000-output-token answer, or null when unmeasured. */
function replySeconds(row) {
  const sent = finiteNumber(row?.estimated_reply_seconds)
  if (sent !== null) return sent
  const tps = finiteNumber(row?.tps_p50)
  const ttft = finiteNumber(row?.ttft_p50_ms)
  if (tps === null) return null
  return (ttft === null ? 0 : ttft / 1000) + 1000 / tps
}

/**
 * Sort rows for the card. 'recommended' keeps the proxy's ranking; the rest
 * order by one measured value, and rows missing that value sink to the end
 * instead of being ranked as if they measured zero.
 */
export function sortRows(rows, sortId) {
  const list = Array.isArray(rows) ? [...rows] : []
  const byNumber = (getValue, { descending = false } = {}) => (a, b) => {
    const av = getValue(a)
    const bv = getValue(b)
    if (av === null && bv === null) return 0
    if (av === null) return 1
    if (bv === null) return -1
    return descending ? bv - av : av - bv
  }
  switch (sortId) {
    case 'price':
      return list.sort(byNumber(row => blendedPerM(row)))
    case 'speed':
      return list.sort(byNumber(row => replySeconds(row)))
    case 'firstToken':
      return list.sort(byNumber(row => finiteNumber(row?.ttft_p50_ms)))
    case 'discount':
      return list.sort(byNumber(row => finiteNumber(row?.discount_pct), { descending: true }))
    default:
      return list
  }
}

let restFn = null

/** The model slug without any route suffix (`model@source@peer:x` -> `model`). */
export function bareModel(model) {
  return String(model || '').split('@')[0].trim()
}

/**
 * Canonical route target for a chosen offer. A server-provided `canonical`
 * field wins verbatim (that is how a model switch names its target); otherwise
 * the bare model is re-pinned to the offer's source and, when the offer names
 * one, its Surplus provider or Antseed peer.
 */
export function routeTarget(option, base) {
  const canonical = typeof option?.canonical === 'string' ? option.canonical.trim() : ''
  if (canonical) return canonical
  const source = String(option?.source || '').trim()
  const pin = option?.provider_id
    ? `@provider:${option.provider_id}`
    : option?.peer_id
      ? `@peer:${option.peer_id}`
      : ''
  return `${bareModel(base)}@${source}${pin}`
}

/** Uncached re-send cost in USD, or null when either input is unreported. */
export function uncachedInputCostUsd(contextTokens, inputPerM) {
  if (typeof contextTokens !== 'number' || !(contextTokens > 0)) return null
  if (typeof inputPerM !== 'number' || !(inputPerM >= 0)) return null
  return (contextTokens * inputPerM) / 1_000_000
}

function ownerKey(owner, profile) {
  return owner ? `${owner.connectionId}::${owner.profile}` : `profile:${profile}`
}

/**
 * Snapshot of the chat a switch applies to: focused runtime session, its
 * connection-qualified owner, and the model at pick time. Re-checked before
 * applying so a focus/profile/model move mid-dialog rejects the switch.
 */
function captureBinding() {
  const owner = host.state.focusedSessionOwner.get()
  const profile = host.state.focusedSessionProfile.get()
  return {
    sessionId: host.state.focusedSessionId.get(),
    profile,
    model: host.state.model.get(),
    key: `${host.state.focusedSessionId.get() || ''}|${ownerKey(owner, profile)}|${host.state.model.get() || ''}`,
  }
}

function bindingCurrent(binding) {
  const owner = host.state.focusedSessionOwner.get()
  const key = `${host.state.focusedSessionId.get() || ''}|${ownerKey(owner, host.state.focusedSessionProfile.get())}|${host.state.model.get() || ''}`
  return key === binding.key
}

function sessionBusy(sessionId) {
  const map = host.state.busyBySession.get() || {}
  return Boolean(map[sessionId] || host.state.busy.get())
}

function fmtUsd(value) {
  if (typeof value !== 'number' || Number.isNaN(value)) return 'n/a'
  return value < 0.001 ? `$${value.toFixed(5)}` : `$${value.toFixed(4)}`
}

function fmtMs(ms) {
  if (typeof ms !== 'number' || Number.isNaN(ms)) return 'unknown'
  return `${(ms / 1000).toFixed(1)}s`
}

/** AntSeed-style price text: dollar amount, trailing zeros trimmed. */
function fmtPrice(value) {
  if (typeof value !== 'number' || Number.isNaN(value)) return 'unknown'
  return `$${value.toFixed(4).replace(/\.?0+$/, '')}`
}

/**
 * The Hermes provider the focused chat really uses. The SDK exposes the model
 * slug but not the provider, and a chat on Solid directly must not be shown as
 * an LLMUX-planned Surplus route. `model.options` answers from the session's
 * live agent; failure leaves the provider unknown (null), never guessed.
 */
async function focusedProvider() {
  const sessionId = host.state.focusedSessionId.get()
  try {
    const res = await host.request('model.options', sessionId ? { session_id: sessionId } : {})
    return typeof res?.provider === 'string' && res.provider ? res.provider : null
  } catch {
    return null
  }
}

/** True when the chat goes through LLMUX (or the provider is unknown). */
export function routedByLlmux(provider) {
  const value = String(provider || '').trim().toLowerCase()
  return !value || value === 'llmux' || value.endsWith(':llmux')
}

/** Client-side "you are on a direct provider" panel data, for backends that predate `direct`. */
export function directCurrent(model, provider) {
  const label = String(provider).split(':').pop()
  return {
    model,
    name: model,
    source: label,
    seller: label,
    input_per_m: null,
    output_per_m: null,
    tps_p50: null,
    ttft_p50_ms: null,
    pinned: true,
    direct: true,
    reason: `direct Hermes provider ${label}; LLMUX is not routing this chat`,
  }
}

async function fetchOptions(model) {
  if (!restFn) {
    throw new Error('LLMUX backend is not enabled. Enable it in Settings > Plugins > LLMUX.')
  }
  const provider = await focusedProvider()
  const query = new URLSearchParams({ model })
  if (provider && !routedByLlmux(provider)) query.set('provider', provider)
  const body = await restFn(`/seller-options?${query.toString()}`)
  const options = Array.isArray(body?.data) ? body.data : []
  if (options[0]?.error) throw new Error(options[0].error)
  let current = body?.current && typeof body.current === 'object' ? body.current : null
  if (provider && !routedByLlmux(provider) && !current?.direct) {
    // Older backend ignored the provider and planned an LLMUX route; that would
    // name the wrong seller, so say what the chat is actually on.
    current = directCurrent(model, provider)
  }
  let similar = Array.isArray(body?.similar) ? body.similar : null
  let similarNote = null
  if (similar === null) {
    // Older gateway backend: no embedded list. Try the standalone route, and
    // say plainly when the gateway has not mounted it yet instead of showing
    // an empty section that looks like "no comparable models exist".
    try {
      const extra = await restFn(`/similar-models?model=${encodeURIComponent(model)}`)
      similar = Array.isArray(extra?.data) ? extra.data : []
    } catch {
      similar = []
      similarNote =
        'Comparable models are not served by this gateway yet. Restart Hermes to mount the newer LLMUX backend routes.'
    }
  }
  const savings = body?.savings && typeof body.savings === 'object' ? body.savings : null
  return { options, similar, current, similarNote, savings }
}

async function validateRoute(model) {
  if (!restFn) {
    throw new Error('LLMUX backend is not enabled. Enable it in Settings > Plugins > LLMUX.')
  }
  await restFn('/validate-route', { method: 'POST', body: { model } })
}

/** Normalize a similar-model row into the shape OfferRow renders. */
function modelRow(row) {
  return {
    ...row,
    kind: 'model',
    label: row.name || row.model,
    canonical: row.switch_model || row.model,
    note:
      typeof row.price_ratio === 'number'
        ? `${row.price_ratio}\u00d7 the current ${row.price_basis === 'list' ? 'list price' : 'price'} \u00b7 via ${row.seller || row.source}`
        : `via ${row.seller || row.source}`,
    cache_warning: row.cache_warning || { text: 'model switch' },
  }
}

/** One big-number stat: value on top, small uppercase caption below. */
function StatBlock({ value, label, accent = false }) {
  return jsxs('div', {
    className: 'min-w-0',
    children: [
      jsx('div', {
        className: `truncate text-lg font-semibold tabular-nums leading-tight${accent ? ' text-(--ui-accent)' : ''}`,
        children: value,
      }),
      jsx('div', {
        className: 'mt-0.5 text-[10px] uppercase tracking-wide text-(--ui-text-secondary)',
        children: label,
      }),
    ],
  })
}

function metaLine(option) {
  const reply =
    typeof option.estimated_reply_seconds === 'number'
      ? `~${option.estimated_reply_seconds}s estimated for 1000 output tokens`
      : 'reply estimate unknown'
  const samples = typeof option.samples === 'number' ? `${option.samples} samples` : 'samples unknown'
  return `${reply} · ${samples}`
}

function cacheLine(option, contextTokens) {
  if (contextTokens === null) {
    return 'Switching re-sends the full chat; context size is not reported yet, so the uncached input cost is unknown.'
  }
  const cost = uncachedInputCostUsd(contextTokens, option.input_per_m)
  if (cost === null) {
    return `Switching re-sends ~${contextTokens.toLocaleString()} context tokens; this route does not report an input price.`
  }
  return `Switching re-sends ~${contextTokens.toLocaleString()} context tokens · uncached input ${fmtUsd(cost)} on this route`
}

function OfferRow({ option, contextTokens, onChoose, isCurrent = false, tags = [] }) {
  const cache = option.cache_warning
  const tps = typeof option.tps_p50 === 'number' ? String(option.tps_p50) : 'unknown'
  return jsxs('div', {
    className: 'rounded-md border border-(--ui-stroke-secondary) px-3 py-2.5',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx('span', { className: 'truncate text-sm font-medium', children: option.label || option.seller }),
          jsx(Badge, { variant: 'outline', children: option.source }),
          isCurrent ? jsx(Badge, { variant: 'outline', children: 'current' }) : null,
          ...tags.map(tag => jsx(Badge, { variant: 'secondary', children: tag }, `tag:${tag}`)),
          jsxs('div', {
            className: 'ml-auto flex shrink-0 items-center gap-2',
            children: [
              typeof option.discount_pct === 'number'
                ? jsx('span', {
                    className: 'text-sm font-semibold text-(--ui-accent)',
                    children: `${option.discount_pct}% off list`,
                  })
                : null,
              jsx(Button, {
                size: 'sm',
                variant: 'outline',
                onClick: () => onChoose(option),
                children: 'Switch',
              }),
            ],
          }),
        ],
      }),
      jsxs('div', {
        className: 'mt-2 grid grid-cols-4 gap-2',
        children: [
          jsx(StatBlock, { value: fmtPrice(option.input_per_m), label: 'in /M tok', accent: true }),
          jsx(StatBlock, { value: fmtPrice(option.output_per_m), label: 'out /M tok', accent: true }),
          jsx(StatBlock, { value: tps, label: 'median tok/s' }),
          jsx(StatBlock, { value: fmtMs(option.ttft_p50_ms), label: 'median first token' }),
        ],
      }),
      jsx('div', {
        className: 'mt-2 text-[11px] text-(--ui-text-secondary)',
        children: metaLine(option),
      }),
      option.note
        ? jsx('div', {
            className: 'mt-1 text-[11px] text-(--ui-text-secondary)',
            children: option.note,
          })
        : null,
      cache
        ? jsx('div', {
            className: 'mt-1 text-[11px] text-(--ui-text-secondary)',
            children: cacheLine(option, contextTokens),
          })
        : null,
    ],
  })
}

/** Live badges: which row leads on speed, price or first token right now. */
export function rowTags(rows) {
  const tags = new Map()
  const add = (row, tag) => {
    if (!row) return
    const list = tags.get(row) || []
    list.push(tag)
    tags.set(row, list)
  }
  const measured = (key, pick) => {
    let best = null
    for (const row of rows) {
      const value = key(row)
      if (value === null) continue
      if (best === null || pick(value, key(best))) best = row
    }
    return best
  }
  if (rows.length > 1) {
    add(measured(row => finiteNumber(row.tps_p50), (a, b) => a > b), 'Fastest')
    add(measured(row => blendedPerM(row), (a, b) => a < b), 'Cheapest')
    add(measured(row => finiteNumber(row.ttft_p50_ms), (a, b) => a < b), 'Quickest start')
  }
  for (const row of rows) {
    if (finiteNumber(row.input_per_m) === 0 && finiteNumber(row.output_per_m) === 0) add(row, 'Free')
  }
  return tags
}

function fmtDollars(value) {
  if (typeof value !== 'number' || Number.isNaN(value)) return '$0'
  if (Math.abs(value) >= 1) return `$${value.toFixed(2)}`
  if (Math.abs(value) >= 0.01) return `$${value.toFixed(3)}`
  return `$${value.toFixed(4)}`
}

/** Spend, savings and the top advice, from the local usage ledger. */
export function SavingsPanel({ savings }) {
  if (!savings || savings.enabled === false || !savings.summary) return null
  const s = savings.summary
  const insights = Array.isArray(savings.insights) ? savings.insights.slice(0, 3) : []
  return jsxs('div', {
    className: 'rounded-md border border-(--ui-stroke-secondary) px-3 py-2.5',
    'data-testid': 'llmux-savings',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(SectionLabel, { children: `Your savings \u00b7 last ${s.window_days} days` }),
          jsx('span', {
            className: 'ml-auto text-[11px] text-(--ui-text-secondary)',
            children: `${s.requests} replies`,
          }),
        ],
      }),
      s.requests > 0
        ? jsxs('div', {
            className: 'mt-2 grid grid-cols-4 gap-2',
            children: [
              jsx(StatBlock, { value: fmtDollars(s.spend_usd), label: 'spent' }),
              jsx(StatBlock, { value: fmtDollars(s.savings_vs_list_usd), label: 'saved vs list', accent: true }),
              jsx(StatBlock, { value: fmtDollars(s.savings_vs_priciest_usd), label: 'saved vs priciest', accent: true }),
              jsx(StatBlock, { value: fmtDollars(s.cashback_usd), label: 'cashback (est.)' }),
            ],
          })
        : null,
      insights.map((insight, index) =>
        jsxs(
          'div',
          {
            className: 'mt-2 text-[11px]',
            children: [
              jsx('span', { className: 'font-semibold', children: insight.title }),
              insight.detail
                ? jsx('span', { className: 'text-(--ui-text-secondary)', children: ` \u00b7 ${insight.detail}` })
                : null,
            ],
          },
          `${insight.kind}:${index}`,
        ),
      ),
    ],
  })
}

function SectionLabel({ children }) {
  return jsx('div', {
    className: 'text-[10px] uppercase tracking-wide text-(--ui-text-secondary)',
    children,
  })
}

/** True when a listed offer is the route the chat is on right now. */
export function isCurrentRoute(option, current) {
  if (!current) return false
  return (
    option.source === current.source &&
    (option.provider_id || null) === (current.provider_id || null) &&
    (option.peer_id || null) === (current.peer_id || null)
  )
}

/** "You are here": the model, the seller serving it, and its measured numbers. */
function CurrentPanel({ current, base }) {
  if (!current) {
    return jsxs('div', {
      className: 'rounded-md border border-(--ui-stroke-secondary) px-3 py-2 text-[11px] text-(--ui-text-secondary)',
      children: ['Current model: ', base || 'unknown', ' (route details unavailable)'],
    })
  }
  const tps = typeof current.tps_p50 === 'number' ? String(current.tps_p50) : 'unknown'
  return jsxs('div', {
    className: 'rounded-md border border-(--ui-accent) px-3 py-2.5',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(SectionLabel, { children: 'Current route' }),
          jsx('span', { className: 'truncate text-sm font-semibold', children: current.name || current.model }),
          jsx(Badge, { variant: 'outline', children: current.source }),
          current.direct
            ? jsx(Badge, { variant: 'outline', children: 'direct' })
            : current.pinned
              ? jsx(Badge, { variant: 'outline', children: 'pinned' })
              : null,
          jsx('span', {
            className: 'ml-auto shrink-0 text-[11px] text-(--ui-text-secondary)',
            children: current.seller,
          }),
        ],
      }),
      jsxs('div', {
        className: 'mt-2 grid grid-cols-4 gap-2',
        children: [
          jsx(StatBlock, { value: fmtPrice(current.input_per_m), label: 'in /M tok', accent: true }),
          jsx(StatBlock, { value: fmtPrice(current.output_per_m), label: 'out /M tok', accent: true }),
          jsx(StatBlock, { value: tps, label: 'median tok/s' }),
          jsx(StatBlock, { value: fmtMs(current.ttft_p50_ms), label: 'median first token' }),
        ],
      }),
      jsx('div', {
        className: 'mt-2 text-[11px] text-(--ui-text-secondary)',
        children: `${current.model} \u00b7 ${current.reason}`,
      }),
    ],
  })
}

function SwitchDialog({ onClose }) {
  const model = useValue(host.state.model)
  const usage = useValue(host.state.focusedUsage)
  const base = bareModel(model)
  const contextTokens = typeof usage?.context_used === 'number' && usage.context_used > 0 ? usage.context_used : null
  const [selected, setSelected] = useState(null)
  const [failed, setFailed] = useState(null)
  const [sort, setSort] = useState('recommended')

  const query = useQuery({
    queryKey: ['llmux', 'seller-options', base],
    queryFn: () => fetchOptions(base),
    enabled: Boolean(base),
    // Live pricing: marketplace prices and speeds move with seller liquidity.
    refetchInterval: LIVE_REFRESH_MS,
  })
  const data = query.data || {}
  // No slice here: the proxy serves a wider candidate set so a price sort can
  // surface a cheap route the speed ranking would have cut.
  const options = Array.isArray(data.options) ? data.options : []
  const similar = Array.isArray(data.similar) ? data.similar : []
  const similarNote = typeof data.similarNote === 'string' ? data.similarNote : null
  const current = data.current || null
  const sortedOptions = sortRows(options, sort).slice(0, MAX_OPTIONS)
  const sortedSimilar = sortRows(similar, sort).slice(0, MAX_SIMILAR)
  const optionTags = rowTags(options)
  const savings = data.savings || null
  const loadError = query.error ? (query.error instanceof Error ? query.error.message : String(query.error)) : null

  function notifyPinned(option) {
    const name = option.label || option.seller
    host.notify({
      kind: 'success',
      message: `Pinned ${name}. The next turn re-sends the full chat on this route; nothing replays automatically.`,
    })
  }

  function notifyDeferred(option) {
    const name = option.label || option.seller
    host.notify({
      kind: 'info',
      message: `Switch to ${name} is queued by the gateway and applies at the next turn start. The current turn is untouched.`,
    })
  }

  async function applySwitch(sel) {
    const { option, binding } = sel
    if (!binding.sessionId) {
      throw new Error('No focused chat session. Send a message in the chat first, then switch sellers.')
    }
    const target = routeTarget(option, binding.model)
    // Validate the candidate route with the backend BEFORE the session is
    // touched; a route the proxy cannot plan never interrupts a turn.
    await validateRoute(target)
    // Refuse to act on a chat the user has since left.
    if (!bindingCurrent(binding)) {
      throw new Error('The focused chat or model changed while this dialog was open. Nothing was applied; reopen the seller card in the chat you want to switch.')
    }
    if (sessionBusy(binding.sessionId)) {
      await host.request('session.interrupt', { session_id: binding.sessionId })
    }
    const params = {
      key: 'model',
      session_id: binding.sessionId,
      value: `${target} --provider llmux --session`,
    }
    let result
    try {
      result = await host.request('config.set', params)
    } catch (err) {
      setSelected(null)
      setFailed({ message: err instanceof Error ? err.message : String(err), target })
      return
    }
    setSelected(null)
    if (result?.confirm_required) {
      // The gateway refused pending a human decision. Route through the shared
      // confirm flow; only a confirmed answer resends with
      // confirm_expensive_model, and a stale binding makes it a no-op.
      onClose()
      void surfaceModelSwitchConfirm({
        confirmMessage: result.confirm_message,
        failureMessage: 'Seller switch failed.',
        model: target,
        isStale: () => !bindingCurrent(binding),
        requestConfirmed: () => host.request('config.set', { ...params, confirm_expensive_model: true }),
        finish: res => {
          if (res?.deferred) notifyDeferred(option)
          else notifyPinned(option)
        },
      })
      return
    }
    if (result?.deferred) {
      notifyDeferred(option)
      onClose()
      return
    }
    notifyPinned(option)
    onClose()
  }

  async function recoverCleanSession() {
    const sessionId = host.state.focusedSessionId.get()
    if (sessionId && sessionBusy(sessionId)) {
      await host.request('session.interrupt', { session_id: sessionId }).catch(() => {})
    }
    host.newChat(host.state.focusedSessionProfile.get())
    host.notify({
      kind: 'info',
      message: `Opened a clean session. Pick ${failed.target} in the model picker to use that route.`,
    })
    setFailed(null)
    onClose()
  }

  return jsxs(DialogContent, {
    className: 'max-w-xl',
    children: [
      jsxs(DialogHeader, {
        children: [
          jsx(DialogTitle, { children: 'Switch model or seller mid-chat' }),
          jsx(DialogDescription, {
            children: current
              ? `Current: ${current.name || current.model} via ${current.seller}. Switching re-sends the whole chat on the new route and drops the cached-prefix discount, so confirm before you pin.`
              : `Current: ${base || 'the selected model'}. Switching re-sends the whole chat on the new route and drops the cached-prefix discount, so confirm before you pin.`,
          }),
        ],
      }),
      base ? jsx(CurrentPanel, { current, base }) : null,
      base ? jsx(SavingsPanel, { savings }) : null,
      base
        ? jsxs('div', {
            className: 'flex flex-wrap items-center gap-1',
            children: [
              jsx(SectionLabel, { children: 'Sort by' }),
              SORTS.map(entry =>
                jsx(
                  Button,
                  {
                    size: 'sm',
                    variant: sort === entry.id ? 'default' : 'outline',
                    className: 'h-6 px-2 text-[11px]',
                    onClick: () => setSort(entry.id),
                    children: entry.label,
                  },
                  entry.id,
                ),
              ),
            ],
          })
        : null,
      loadError
        ? jsx('div', { className: 'rounded border border-(--ui-stroke-secondary) p-3 text-sm', children: loadError })
        : null,
      failed
        ? jsxs('div', {
            className: 'rounded border border-(--ui-stroke-secondary) p-3 text-sm',
            children: [
              jsx('p', { children: `Session model update failed: ${failed.message}` }),
              jsx('p', {
                className: 'mt-1 text-(--ui-text-secondary)',
                children: 'Recover with a clean session instead of retrying the same runtime.',
              }),
              jsxs('div', {
                className: 'mt-3 flex gap-2',
                children: [
                  jsx(Button, { size: 'sm', onClick: recoverCleanSession, children: 'Open clean session' }),
                  jsx(Button, { size: 'sm', variant: 'ghost', onClick: onClose, children: 'Cancel' }),
                ],
              }),
            ],
          })
        : null,
      !base
        ? jsx('div', { className: 'py-6 text-sm text-(--ui-text-secondary)', children: 'Set a model first; seller routes are measured per model.' })
        : query.isPending && !loadError
          ? jsx('div', { className: 'py-6 text-sm text-(--ui-text-secondary)', children: 'Loading live offers…' })
          : !loadError && options.length === 0 && !failed
            ? jsx('div', {
                className: 'py-6 text-sm text-(--ui-text-secondary)',
                children: 'No measured offers for this model. Start the proxy or wait for the catalog to refresh.',
              })
            : jsxs('div', {
                className: 'flex max-h-[50vh] flex-col gap-3 overflow-y-auto',
                children: [
                  jsxs('div', {
                    className: 'flex flex-col gap-2',
                    children: [
                      jsx(SectionLabel, { children: 'Measured routes for this model' }),
                      sortedOptions.map(option =>
                        jsx(
                          OfferRow,
                          {
                            option,
                            contextTokens,
                            isCurrent: isCurrentRoute(option, current),
                            tags: optionTags.get(option) || [],
                            onChoose: option => setSelected({ option, binding: captureBinding() }),
                          },
                          `${option.source}:${option.provider_id || option.peer_id || option.seller}`,
                        ),
                      ),
                    ],
                  }),
                  similar.length > 0
                    ? jsxs('div', {
                        className: 'flex flex-col gap-2',
                        children: [
                          jsx(SectionLabel, {
                            children: 'Similar models in this price range',
                          }),
                          sortedSimilar.map(row => {
                            const option = modelRow(row)
                            return jsx(
                              OfferRow,
                              {
                                option,
                                contextTokens,
                                onChoose: picked => setSelected({ option: picked, binding: captureBinding() }),
                              },
                              `model:${row.model}`,
                            )
                          }),
                        ],
                      })
                    : similarNote
                      ? jsx('div', {
                          className: 'rounded border border-(--ui-stroke-secondary) p-3 text-[11px] text-(--ui-text-secondary)',
                          children: similarNote,
                        })
                      : null,
                ],
              }),
      jsx(ConfirmDialog, {
        open: selected !== null,
        onClose: () => setSelected(null),
        onConfirm: () => applySwitch(selected),
        title: `Switch to ${selected?.option?.label || selected?.option?.seller || 'this route'}?`,
        description:
          selected?.option?.kind === 'model'
            ? 'This validates the route, stops any in-flight turn, and re-sends the entire conversation on the new model. Cached-prefix discounts are lost and input is billed again at the new route price. Nothing replays automatically after the switch.'
            : 'This validates the route, stops any in-flight turn, and re-sends the entire conversation on the new seller. Cached-prefix discounts are lost and input is billed again at the new route price. Nothing replays automatically after the switch.',
        confirmLabel: selected?.option?.kind === 'model' ? 'Switch model' : 'Switch seller',
        busyLabel: 'Switching…',
        cancelLabel: 'Keep current route',
      }),
    ],
  })
}

function SwitchCard() {
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
        children: busy ? 'LLMUX…' : 'LLMUX',
      }),
      open
        ? jsx(Dialog, {
            open: true,
            onOpenChange: next => { if (!next) setOpen(false) },
            children: jsx(SwitchDialog, { onClose: () => setOpen(false) }),
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
    restFn = ctx.rest ? (path, options) => ctx.rest(path, options) : null
    ctx.register({
      id: 'seller-switch',
      area: COMPOSER_AREAS.actions,
      order: 20,
      render: () => jsx(SwitchCard, {}),
    })
  },
}
