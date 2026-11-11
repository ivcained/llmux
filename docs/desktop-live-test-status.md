# Desktop seller-switch verification

## Current evidence

Automated card interactions run the real React component against a simulated Hermes SDK host. Isolated browser captures additionally use the real Desktop UI primitives and styles with live read-only marketplace payloads. Neither establishes native Desktop switching.

The final card suite passes 62 scenarios. It includes same-model cross-chat provider refresh, focus/owner/model binding, full seller-pin queries, a post-interrupt focus change, human approval accept/decline, queued/deferred outcomes and explicit-untrusted confirmation. Eleven rendered states at 1280x800 and 390x844 show no clipped Switch buttons or truncated speed placeholders; active trust tabs measure 5.19:1. The unsupported Bedrock case says pricing is not connected and preserves the direct chat. A scoped lintapple pass exits 0 with documented exclusions for real SDK controls, intentional truncation, segmented-button density and the scroll-mask contrast limitation; it is not a whole-app accessibility certification.

## Separate installation identities

The checkout, installed plugin at `$HERMES_HOME/plugins/llmux/desktop/plugin.js`, and Desktop runtime copy at `$HERMES_HOME/desktop-plugins/llmux/plugin.js` are separate. On 2026-11-11 the installed and runtime copies matched each other but did not match the latest checkout. A validation pass does not install the checkout. A Desktop plugin reload does not replace an already-imported Python backend module.

## Not verified

- Mid-chat seller switching through the installed Desktop UI, including the real shared confirmation and deferred apply timing.
- A real reply after switching and its upstream route receipt.
- Real usage/accounting after a proxy-routed reply.

No paid inference was sent during this release verification. Do not change the active project chat to obtain proof. Use a separate test chat or isolated installation, preserve drafts, and obtain a bounded spend approval before any paid request.

## Intended behavior and limits

Switching validates a route, interrupts a busy turn if needed, rechecks the session binding, and applies the route for the next turn. It does not migrate or replay an interrupted response. A prior request can still be charged, and new context may lose cached-prefix discounts. Queued changes are not described as applied.

Direct-provider chats are not routed or counted by LLMUX. If an exact model has no recognized offer in connected sources, preserve its current provider and say that coverage is unavailable. Do not invent an alias or silently choose another model.
