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
// The proxy serves from its live cache and refreshes behind it, so polling is
// cheap: every read is answered from memory.
const LIVE_REFRESH_MS = 15_000

const TRUST_VIEWS = [
  { id: 'trusted', label: 'Trusted sellers' },
  { id: 'all', label: 'All sellers' },
]

/**
 * Trust badge text for a row in the "All sellers" view. Marketplaces that
 * publish a signal get Trusted / Untrusted; a source with no signal (a
 * fixed-price API) gets no badge rather than an invented verdict.
 */
export function trustLabel(row) {
  if (row?.trusted === true) return 'Trusted'
  if (row?.trusted === false) return 'Untrusted'
  return null
}

/** Rows for the chosen trust view. Older backends send only `data`. */
export function rowsForTrustView(data, view) {
  const trustedRows = Array.isArray(data?.options) ? data.options : []
  if (view !== 'all') return trustedRows.filter(row => row?.trusted !== false)
  return Array.isArray(data?.allOptions) ? data.allOptions : trustedRows
}

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

// Sources that sell at a fixed price and never publish speed figures. Mirrors
// the proxy's UNMEASURED_SOURCES for backends that predate `speed_published`.
const UNPUBLISHED_SPEED_SOURCES = new Set(['solid'])

/** False when the row's source never publishes speed (vs. simply lacking data). */
export function speedPublished(row) {
  if (typeof row?.speed_published === 'boolean') return row.speed_published
  return !UNPUBLISHED_SPEED_SOURCES.has(String(row?.source || '').toLowerCase())
}

/** Placeholder for a missing speed figure: why it is missing, in plain words. */
function missingSpeed(row) {
  return speedPublished(row) ? 'unknown' : 'not published'
}

function fmtMs(ms, missing = 'unknown') {
  if (typeof ms !== 'number' || Number.isNaN(ms)) return missing
  return `${(ms / 1000).toFixed(1)}s`
}

/**
 * Per-million price text with FIXED decimals per tier, so a column of prices
 * lines up for comparison: $1.20 / $0.38 / $0.029 / $0.0045. Variable
 * trailing-zero trimming made "$1.2" sit next to "$7.448" and forced the
 * reader to do alignment math.
 */
export function fmtPrice(value) {
  if (typeof value !== 'number' || Number.isNaN(value)) return 'unknown'
  const abs = Math.abs(value)
  const decimals = abs >= 0.1 ? 2 : abs >= 0.01 ? 3 : 4
  return `$${value.toFixed(decimals)}`
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

/**
 * Client-side "you are on a direct provider" panel, for gateway backends that
 * predate `direct` (the gateway mounts plugin routes once per run, so an old
 * route can outlive a plugin update). Prices come from the matching seller row
 * the proxy already returned, and the display name from the planned route, so
 * the panel is complete without a restart. Missing data stays null.
 */
export function directCurrent(model, provider, options = [], planned = null) {
  const label = String(provider).split(':').pop()
  const source = label.toLowerCase()
  const row = options.find(option => String(option?.source || '').toLowerCase() === source) || null
  return {
    model,
    name: planned?.name || model,
    source: row ? row.source : label,
    seller: row ? row.seller || label : label,
    input_per_m: row ? row.input_per_m : null,
    output_per_m: row ? row.output_per_m : null,
    tps_p50: row ? row.tps_p50 : null,
    ttft_p50_ms: row ? row.ttft_p50_ms : null,
    provider_id: row ? row.provider_id || null : null,
    peer_id: row ? row.peer_id || null : null,
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
    current = directCurrent(model, provider, options, current)
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
  const freshness = body?.freshness && typeof body.freshness === 'object' ? body.freshness : null
  const allOptions = Array.isArray(body?.all_sellers) ? body.all_sellers : null
  const untrustedCount = finiteNumber(body?.untrusted_count)
  return { options, allOptions, untrustedCount, similar, current, similarNote, savings, freshness }
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

/**
 * Estimated seconds for a 1000-token reply. The server computes it for seller
 * rows; similar-model rows arrive without it even though they carry the exact
 * inputs (first-token time and tok/s), so the card derives the same estimate
 * instead of printing "reply estimate unknown" beside the numbers it needs.
 */
export function replyEstimateSeconds(option) {
  if (typeof option?.estimated_reply_seconds === 'number') return option.estimated_reply_seconds
  const tps = finiteNumber(option?.tps_p50)
  const ttft = finiteNumber(option?.ttft_p50_ms)
  if (tps === null || tps <= 0 || ttft === null) return null
  return ttft / 1000 + 1000 / tps
}

/** "63% off list" needs the list price in reach, or it is not a number. */
export function listOutPerM(option) {
  const out = finiteNumber(option?.output_per_m)
  const disc = finiteNumber(option?.discount_pct)
  if (out === null || disc === null || disc >= 99.5 || disc < 0) return null
  return out / (1 - disc / 100)
}

/** How this row's price compares with the current route, as a plain ratio. */
export function vsCurrent(row, current) {
  // Similar-model rows already carry their own price_ratio note ("0.5x the
  // current list price"); a second ratio beside it would read as a conflict.
  if (row?.kind === 'model') return null
  const rowOut = finiteNumber(row?.output_per_m)
  const curOut = finiteNumber(current?.output_per_m)
  if (rowOut === null || curOut === null || curOut <= 0) return null
  if (rowOut === curOut) return null
  const ratio = rowOut / curOut
  if (ratio >= 0.95 && ratio <= 1.05) return null
  return ratio
}

function metaLine(option, current = null) {
  const est = replyEstimateSeconds(option)
  const reply = est !== null
    ? `~${est.toFixed(1)}s estimated for 1000 output tokens`
    : speedPublished(option)
      ? 'reply estimate unknown'
      : `speed not published by ${option.seller || option.source}`
  if (!speedPublished(option)) return reply
  const parts = [reply]
  const list = listOutPerM(option)
  if (list !== null) parts.push(`list ${fmtPrice(list)}/M out`)
  const samples = typeof option.samples === 'number' ? `${option.samples} samples` : 'samples unknown'
  parts.push(samples)
  const ratio = vsCurrent(option, current)
  if (ratio !== null) {
    parts.push(
      ratio > 1
        ? `${ratio.toFixed(1)}\u00d7 the current price`
        : `cheaper than current (${ratio.toFixed(2)}\u00d7)`,
    )
  }
  return parts.join(' \u00b7 ')
}

function cacheLine(option, contextTokens) {
  if (contextTokens === null) {
    return 'Switching re-sends the full chat; context size is not reported yet, so the uncached input cost is unknown.'
  }
  const cost = uncachedInputCostUsd(contextTokens, option.input_per_m)
  if (cost === null) {
    return `Switching re-sends ~${contextTokens.toLocaleString()} context tokens; this route does not report an input price.`
  }
  return `Switching re-sends ~${contextTokens.toLocaleString()} context tokens · next input \u2248 ${fmtUsd(cost)} on this route (no cache discount)`
}

function OfferRow({ option, contextTokens, onChoose, isCurrent = false, tags = [], current = null, showTrust = false }) {
  const trust = showTrust ? trustLabel(option) : null
  const cache = option.cache_warning
  const tps = typeof option.tps_p50 === 'number' ? String(option.tps_p50) : missingSpeed(option)
  return jsxs('div', {
    className: 'rounded-md border border-(--ui-stroke-secondary) px-3 py-2.5',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx('span', { className: 'truncate text-sm font-medium', children: option.label || option.seller }),
          jsx(Badge, { variant: 'outline', children: option.source }),
          isCurrent ? jsx(Badge, { variant: 'outline', children: 'current' }) : null,
          trust
            ? jsx(
                'span',
                {
                  'data-testid': 'llmux-trust',
                  title: option.trust_basis || undefined,
                  className:
                    trust === 'Untrusted'
                      ? 'rounded border border-(--ui-text-warning,#d97706) px-1.5 text-[10px] font-medium text-(--ui-text-warning,#d97706)'
                      : 'rounded border border-(--ui-stroke-secondary) px-1.5 text-[10px] font-medium text-(--ui-text-secondary)',
                  children: trust === 'Untrusted' ? '\u26a0 Untrusted' : '\u2713 Trusted',
                },
              )
            : null,
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
          jsx(StatBlock, { value: fmtMs(option.ttft_p50_ms, missingSpeed(option)), label: 'median first token' }),
        ],
      }),
      jsx('div', {
        className: 'mt-2 text-[11px] text-(--ui-text-secondary)',
        children: metaLine(option, isCurrent ? null : current),
      }),
      option.note
        ? jsx('div', {
            className: 'mt-1 text-[11px] text-(--ui-text-secondary)',
            children: option.note,
          })
        : null,
      trust === 'Untrusted'
        ? jsx('div', {
            className: 'mt-1 text-[11px] text-(--ui-text-warning,#d97706)',
            children: `${option.trust_basis || 'Flagged by its marketplace'}. LLMUX never auto-routes here; switch only if you accept the risk.`,
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

/** "Live · updated 12s ago" — data age in plain words, never implied fresh. */
export function freshnessText(freshness, nowMs = Date.now()) {
  const at = finiteNumber(freshness?.updated_at)
  if (at === null) return null
  const age = Math.max(0, Math.round(nowMs / 1000 - at))
  const ago = age < 5 ? 'just now' : age < 60 ? `${age}s ago` : age < 3600 ? `${Math.round(age / 60)}m ago` : `${Math.round(age / 3600)}h ago`
  return `${freshness.refreshing ? 'Refreshing' : 'Live'} \u00b7 updated ${ago}`
}

function SectionLabel({ children }) {
  return jsx('div', {
    className: 'text-[10px] uppercase tracking-wide text-(--ui-text-secondary)',
    children,
  })
}

/**
 * Top rows for display, always including the route the chat is on: a seller
 * without speed telemetry sinks under the default order, and hiding the
 * current route makes every other row look like the only choice.
 */
export function keepCurrentVisible(sorted, current, limit) {
  const top = sorted.slice(0, limit)
  if (!current || top.some(option => isCurrentRoute(option, current))) return top
  const mine = sorted.find(option => isCurrentRoute(option, current))
  return mine ? [...top.slice(0, limit - 1), mine] : top
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
/**
 * Speed figures for the current-route panel. Marketplace medians win when the
 * source publishes them; otherwise fall back to what Hermes measured on THIS
 * chat (rolling average of the last ~10 calls), labelled as such. Hermes times
 * whole calls, not first tokens, so that figure is shown as call time, never
 * passed off as time-to-first-token.
 */
export function currentSpeed(current, usage) {
  const marketTps = finiteNumber(current?.tps_p50)
  const marketTtft = finiteNumber(current?.ttft_p50_ms)
  const chatTps = finiteNumber(usage?.avg_tps)
  const chatCall = finiteNumber(usage?.avg_latency_s)
  const tps =
    marketTps !== null
      ? { value: String(marketTps), label: 'median tok/s' }
      : chatTps !== null
        ? { value: String(chatTps), label: 'tok/s \u00b7 this chat' }
        : { value: missingSpeed(current), label: 'median tok/s' }
  const timing =
    marketTtft !== null
      ? { value: fmtMs(marketTtft), label: 'median first token' }
      : chatCall !== null
        ? { value: `${chatCall.toFixed(1)}s`, label: 'avg call \u00b7 this chat' }
        : { value: missingSpeed(current), label: 'median first token' }
  return { tps, timing }
}

function CurrentPanel({ current, base, usage = null }) {
  if (!current) {
    return jsxs('div', {
      className: 'rounded-md border border-(--ui-stroke-secondary) px-3 py-2 text-[11px] text-(--ui-text-secondary)',
      children: ['Current model: ', base || 'unknown', ' (route details unavailable)'],
    })
  }
  const speed = currentSpeed(current, usage)
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
          jsx(StatBlock, { value: speed.tps.value, label: speed.tps.label }),
          jsx(StatBlock, { value: speed.timing.value, label: speed.timing.label }),
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
  const [trustView, setTrustView] = useState('trusted')

  const query = useQuery({
    queryKey: ['llmux', 'seller-options', base],
    queryFn: () => fetchOptions(base),
    enabled: Boolean(base),
    // Live pricing: marketplace prices and speeds move with seller liquidity.
    refetchInterval: LIVE_REFRESH_MS,
    // Reopening the card shows the last rows at once and refreshes behind them
    // (TanStack keeps the entry in the app's shared QueryClient).
    staleTime: LIVE_REFRESH_MS,
    gcTime: 30 * 60 * 1000,
    refetchOnMount: 'always',
  })
  const data = query.data || {}
  // No slice here: the proxy serves a wider candidate set so a price sort can
  // surface a cheap route the speed ranking would have cut.
  const options = rowsForTrustView(data, trustView)
  const showTrust = trustView === 'all'
  const untrustedCount =
    typeof data.untrustedCount === 'number'
      ? data.untrustedCount
      : (Array.isArray(data.allOptions) ? data.allOptions : []).filter(row => row?.trusted === false).length
  const similar = Array.isArray(data.similar) ? data.similar : []
  const similarNote = typeof data.similarNote === 'string' ? data.similarNote : null
  const current = data.current || null
  const sortedOptions = keepCurrentVisible(sortRows(options, sort), current, MAX_OPTIONS)
  const sortedSimilar = sortRows(similar, sort).slice(0, MAX_SIMILAR)
  const optionTags = rowTags(options)
  const savings = data.savings || null
  const liveText = freshnessText(data.freshness)
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
      base ? jsx(CurrentPanel, { current, base, usage }) : null,
      base ? jsx(SavingsPanel, { savings }) : null,
      base
        ? jsxs('div', {
            className: 'flex flex-wrap items-center gap-2',
            children: [
              jsx('div', {
                role: 'radiogroup',
                'aria-label': 'Which sellers to show',
                'data-testid': 'llmux-trust-switch',
                className: 'inline-flex rounded-md border border-(--ui-stroke-secondary) p-0.5',
                children: TRUST_VIEWS.map(entry =>
                  jsx(
                    'button',
                    {
                      type: 'button',
                      role: 'radio',
                      'aria-checked': trustView === entry.id,
                      onClick: () => setTrustView(entry.id),
                      className:
                        trustView === entry.id
                          ? 'rounded px-2.5 py-1 text-[11px] font-medium bg-(--ui-accent) text-(--ui-accent-foreground,white)'
                          : 'rounded px-2.5 py-1 text-[11px] text-(--ui-text-secondary)',
                      children:
                        entry.id === 'all' && untrustedCount > 0
                          ? `${entry.label} (+${untrustedCount} untrusted)`
                          : entry.label,
                    },
                    `trust:${entry.id}`,
                  ),
                ),
              }),
              jsx('span', {
                className: 'text-[10px] text-(--ui-text-secondary)',
                children:
                  trustView === 'all'
                    ? 'Showing every seller with its marketplace trust label.'
                    : 'Hiding sellers their marketplace flags as untrusted.',
              }),
            ],
          })
        : null,
      base
        ? jsxs('div', {
            className: 'flex flex-wrap items-center gap-1',
            children: [
              jsx(SectionLabel, { children: 'Sort by' }),
              liveText
                ? jsx('span', {
                    className: 'ml-auto order-last text-[10px] text-(--ui-text-secondary)',
                    'data-testid': 'llmux-live',
                    children: liveText,
                  })
                : null,
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
        ? jsxs('div', {
            className: 'rounded border border-(--ui-stroke-secondary) p-3 text-sm',
            'data-testid': 'llmux-load-error',
            children: [
              jsx('p', { children: 'LLMUX pricing is unreachable right now. Your chat still works on its current route.' }),
              jsx('p', {
                className: 'mt-1 text-(--ui-text-secondary)',
                children: `The local pricing helper did not respond (${loadError}). Reopen this card in a moment; it usually recovers when Hermes restarts the helper.`,
              }),
            ],
          })
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
                'data-testid': 'llmux-empty',
                children:
                  data?.freshness?.updated_at
                    ? 'No sellers for this model on any source right now. Prices were refreshed recently.'
                    : 'No measured offers for this model yet. Live pricing is still loading; reopen this card in a moment.',
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
                            current,
                            showTrust,
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
                                current,
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
          selected?.option?.trusted === false
            ? `${selected.option.trust_basis || 'This seller is flagged untrusted by its marketplace'}. Its replies may be unreliable or its terms unverified. This validates the route, stops any in-flight turn, and re-sends the entire conversation to this seller. Cached-prefix discounts are lost. Nothing replays automatically.`
            : selected?.option?.kind === 'model'
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
