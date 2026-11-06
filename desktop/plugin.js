/**
 * LLMUX desktop controls.
 *
 * Contributes source and policy selectors immediately before the model pill
 * through the public composer.actions surface. Source overrides are stored per
 * model; Auto removes the override and returns the model to policy routing.
 *
 * This is the desktop half of the unified LLMUX package. Routing is supplied by
 * the local LLMUX proxy in the next milestone.
 */

import {
  COMPOSER_AREAS,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  atom,
  host,
  useValue,
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'llmux'
const $sourceByModel = atom({})
const $policy = atom('balanced')
let storage = null

function setModelSource(model, source) {
  if (!model) {
    host.notify({ kind: 'warning', message: 'Select a model before choosing an LLMUX source.' })
    return
  }
  const next = { ...$sourceByModel.get() }
  if (source === 'auto') {
    delete next[model]
  } else {
    next[model] = source
  }
  $sourceByModel.set(next)
  storage?.set('sourceByModel', next)
}

function setPolicy(policy) {
  $policy.set(policy)
  storage?.set('policy', policy)
}

function Controls() {
  const model = useValue(host.state.model)
  const sourceByModel = useValue($sourceByModel)
  const policy = useValue($policy)
  const source = sourceByModel[model] || 'auto'

  return jsxs('div', {
    className: 'flex items-center gap-1',
    children: [
      jsxs(Select, {
        value: source,
        onValueChange: value => setModelSource(model, value),
        children: [
          jsx(SelectTrigger, {
            className: 'h-7 w-[104px] text-xs',
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
            className: 'h-7 w-[100px] text-xs',
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
    ],
  })
}

export default {
  id: ID,
  name: 'LLMUX',
  defaultEnabled: false,
  register(ctx) {
    storage = ctx.storage
    Promise.all([
      ctx.storage.get('sourceByModel'),
      ctx.storage.get('policy'),
    ]).then(([savedSources, savedPolicy]) => {
      if (savedSources && typeof savedSources === 'object' && !Array.isArray(savedSources)) {
        $sourceByModel.set(savedSources)
      }
      if (['cheapest', 'fastest', 'balanced'].includes(savedPolicy)) {
        $policy.set(savedPolicy)
      }
    }).catch(() => {
      host.notify({ kind: 'warning', message: 'LLMUX could not restore its saved routing controls.' })
    })

    ctx.register({
      id: 'routing-controls',
      area: COMPOSER_AREAS.actions,
      order: 20,
      render: () => jsx(Controls, {}),
    })
  },
}
