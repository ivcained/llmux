# LLMUX

* **Author:** lc + Ora (GitHub: ivcained)
* **Date:** 2026-11-05
* **Submission deadline:** 2026-11-11 at 5PM EST
* **Demo requirement:** public demo video on X, under 90 seconds, tagging @NousResearch
* **Status:** Goldfish-reviewed; name locked as LLMUX; implementation starting

## Overview

LLMUX is a Hermes plugin that gives one chat endpoint access to two AI inference marketplaces at once: Surplus Intelligence and Antseed. The user picks a model the normal way; behind it a local routing proxy picks which marketplace serves each request, following a policy the user controls: prefer cheapest, prefer fastest, or balanced, with an optional speed floor. A live scoreboard of real prices and measured tokens-per-second feeds those decisions, a status control in the desktop app switches policy instantly, and a balance watchdog with a Peer.xyz onramp button (CashApp/Venmo/PayPal to USDC on Base) keeps the accounts funded. The plugin never holds private keys and never takes custody of funds.

Target user, honestly stated: the founder's own 24/7 Hermes agent fleet (cost-sensitive background workers) plus interactive chat (speed-sensitive), and secondarily any Hermes user who wants marketplace pricing without managing two providers by hand. The submission goal is the Nous hackathon and the human-reviewed Hermes plugin catalog.

## Analysis

Six Goldfish lenses reviewed the concept (zoom-out, design, implementation-readiness, codebase-details, structural, comprehension). Findings below are deduplicated and merged; severity reflects the consensus.

### The architecture must be a local proxy, not a provider override

The original concept assumed the plugin could route per request from inside a ProviderProfile. The codebase review verified this is not possible for a catalog plugin:

- ProviderProfile is declarative. It does not own client construction, credentials, or streaming; those stay on Hermes' agent runtime.
- The one escape hatch, `create_client()`, is hard-rejected for third-party plugins: the plugin host raises `PluginHostUnsupported` when a captured profile overrides it, and the isolation audit flags any override.
- There is no per-request hook that can pick between two upstream base URLs.

So the only compliant shape is: the plugin registers a ProviderProfile whose static base URL points at a **local OpenAI-compatible routing proxy** the plugin runs (same shape as Antseed's own localhost:8377 buyer proxy). The proxy is the product: it holds the adapters for both marketplaces, makes the routing decision, passes streaming through, translates errors, reads back cost, and persists the scoreboard. Hermes' native transport, streaming, tool calling, and picker are reused, not rebuilt.

Consequences the design must own:

- Every interactive turn pays one localhost hop. This is small but not zero, and it means the scoreboard must measure through the proxy (which it does, since real traffic flows through it).
- Once both upstreams hide behind one provider, Hermes' native cross-provider failover can never engage. The proxy owns failover and its bugs.
- Model metadata resolution (context length, vision, pricing display) reverse-maps from hostname; a localhost base URL maps to nothing. The plugin must ship its own normalized catalog (see below).
- Cost readback for streamed Surplus responses (x-request-id header, then GET /v1/requests/{id}) is invisible to the profile seam, but the proxy sees every header. All cost accounting lives in the proxy. This is fine, it is actually the only place it can live.

### Measurement: passive first, probes second

The strongest cluster of objections: active probes are paid requests, can cost more than the savings they enable, perturb the thing being measured, and leave the scoreboard empty exactly at cold start (which is when demos happen).

Resolution: the scoreboard learns primarily from **real traffic**. Every actual user request flows through the proxy, which can time time-to-first-token, measure steady-state tok/s from the SSE stream, and read back actual cost. Active probes exist only to bootstrap or refresh (marketplace, model) pairs that have not been served recently, under a hard daily budget cap (default $0.50/day, configurable, opt-out), excluded from the watchdog's low-balance warnings, and never triggered by a read-only tool call. Price data is free to poll (Surplus GET /v1/prices; Antseed seller prices announced on the DHT and visible to the buyer proxy).

Cold start behavior: with no TPS data, the router falls back to cheapest-by-list-price and the UI shows a "measuring" state. A fresh install is never blind, it is just price-led until the first real requests arrive.

Caveat kept on record: a 10-token probe (or a short chat turn) does not perfectly predict a 50k-context agentic turn. The scoreboard records sample counts and ages; the UI shows measurement confidence rather than hiding it.

### Model identity in the union catalog

The comprehension and codebase lenses converged: the concept never defined what one entry in the union catalog means, and `fetch_models` returns bare ID strings with no per-entry metadata. If every model were prefixed per source, the policy would have nothing left to decide; if silently merged, the picker lies about what serves it.

Resolution:

- The picker shows **bare, deduplicated model IDs** (union of both catalogs, chat-completions models only for v1; non-chat modalities like image/audio/embedding are excluded from routing scope). Bare ID means "route this by policy".
- Source pinning and policy overrides are available via a **suffix grammar on the model string**, typed by power users or set programmatically by headless agents: `model@surplus`, `model@antseed` (pin the marketplace), `model@fast`, `model@cheap`, `model@bal` (pin the policy for that request). The proxy parses and strips the suffix before dispatch.
- The plugin maintains its own **normalized catalog file** (model id, sources offering it, per-source price snapshot, capabilities merged from both sides) because Hermes' metadata reverse-mapping cannot resolve a localhost host. Metadata for bare IDs still resolves through the normal path since the IDs match upstream names.
- Model equivalence risk (an Antseed seller serving a different quant under the same name) is real and unobservable. Guardrail for v1: record per-source provenance in every routing receipt, and document the trust-score setting that gates Antseed sellers. Do not attempt quant verification.

### Policy semantics must be defined, not named

The comprehension lens flagged that "balanced" computed nothing and low/mid/high TPS had no numbers. Definitions for v1:

- **cheapest**: among healthy candidate sources for the model, pick the lowest effective price per 1M tokens (input+output blended at a documented ratio). Surplus-side controls (max_price_per_1m) pass through.
- **fastest**: pick the highest measured TPS score, where the score blends time-to-first-token (weight 0.4) and steady-state tok/s (weight 0.6), over a rolling window with decay.
- **balanced**: the cheapest source whose TPS score is within 25% of the best measured TPS score. (Explainable in one sentence; weights tunable in config.)
- **TPS floor (low/mid/high)**: a constraint filter applied before the policy scores candidates. Concretely: low = 10 tok/s, mid = 25 tok/s, high = 60 tok/s, displayed next to the live measured numbers so the user never sees an unlabeled abstraction. If no source meets the floor, the router serves the fastest available and the receipt says the floor was relaxed. The floor never causes an error.
- **Policy control surfaces** (three, because the primary user is headless): (1) the desktop status control, for interactive sessions; (2) the model-string suffix above, for agents and cron; (3) a config-file default for the whole install. The desktop control is per-session; sessions without an explicit setting inherit the config default.

### Failover and error taxonomy

Multiple lenses converged that undefined failover is a correctness risk, and one failure mode is a safety bug: routing around Surplus' `budget_exceeded` (money exists, user's own cap fired) to the other marketplace would spend against the user's deliberate limit.

Error-to-action table for v1:

| Failure | Action |
|---|---|
| Surplus 402 insufficient_credit / insufficient_balance | Demote Surplus for this request, try the other marketplace, warn + surface the refill action |
| Surplus 402 budget_exceeded | **Stop. Surface to user. Never reroute.** (the cap is the user's intent) |
| Antseed buyer proxy not running / no sellers | Exclude Antseed from candidates; status shows "antseed: down"; chat continues on Surplus alone |
| Upstream 5xx/timeout before first byte | Retry once on the other marketplace |
| Mid-stream death | Surface the error; never replay to the other marketplace (double-billing risk) |
| Adapter sees an unrecognized response schema | Mark that marketplace unroutable (fail-closed) and say so, never route on garbage |

### Balance watchdog and the onramp

Honest gaps the reviews exposed:

- Surplus balance endpoint: to be verified during build (llms.txt documents credits and 402 semantics; the exact balance-read endpoint and cadence need pinning).
- Antseed balance = payment-channel state, not a REST balance. Read via the buyer proxy if it exposes it; direct Base RPC reads are a documented fallback, marked stretch. The two marketplaces' balances are different things and the UI must label them separately, not sum them.
- The Peer onramp delivers USDC to the user's **wallet**. Turning wallet USDC into Surplus credit or a funded Antseed channel is a separate manual step on each marketplace. V1 must present this as a guided multi-step refill (onramp → deposit), not "one-click". The button's value is the CashApp/Venmo/PayPal rails and the low-balance trigger, not magic.
- Geo/platform restrictions: the demo must be pre-checked for the reviewer's region; the button degrades to a documented link where unsupported.

### The strategic objection, kept on record

The zoom-out lens' hardest point: both marketplaces already route internally to their cheapest healthy/trusted seller, so the residual cross-market delta a meta-router captures might be pennies per million tokens, and nobody has measured it. If the delta is noise, the durable value is not arbitrage; it is the union catalog, the policy layer, the measurement, the single UX, and the cash onramp. That is still a legitimate product, but the README and catalog description must pitch it honestly.

Two actions follow: (1) a **day-0 spike** measures today's actual price and TPS delta between the marketplaces for the fleet's model class (GLM/Kimi-class), and the pitch is written from the measured numbers; (2) success criteria are defined as submission accepted + the founder's own fleet measurably cheaper or faster, not a projected user base, since the reviews found the beyond-the-founder audience is unproven.

### Differentiation against existing art

- Against "just configure two native providers": native config gives you two pickers and manual switching, no policy automation, no measurement, no shared bill view. LLMUX is transparent per-request routing.
- Against the installed `surp-hermes-x402-llm` tools plugin (Surplus-only, agent-invoked tools for prices/quotes/TPS): LLMUX is provider-level and automatic, adds the Antseed union, and adds the desktop control. The catalog description must make this argument explicitly or it reads as a re-badge.
- Against LiteLLM-class external routers: those are not Hermes-native, have no desktop policy UX, and are not catalog-installable.

### Maintenance posture (2-year story)

The structural lens' requirement, adopted: a single `MarketplaceAdapter` interface (catalog, price, cost readback, balance, health, error taxonomy) with Surplus and Antseed implementations as the only modules allowed to speak wire protocols. Every feature consumes adapters, never marketplaces. Per-adapter contract tests with recorded response fixtures run in CI, so upstream drift is caught as a failing test, not a live incident. Name the local proxy port and the state files in the docs so they are stable public contracts.

## Goals vs. Non-Goals

**In-scope goals:**

1. Model-provider plugin registering one provider (renamed, see decisions) backed by a local routing proxy: union catalog, policy engine (cheapest/fastest/balanced + TPS floor), failover per the error table.
2. Scoreboard: passive measurement from real traffic, budget-capped active probes, persistent rolling windows with sample age.
3. Routing receipts: per-response attribution (source, policy, cost, TPS) visible in the status surface and queryable via tools.
4. Agent tools: read-only scoreboard/policy queries ("best source for model X right now", with measurement age; never triggers probes).
5. **Desktop routing controls**: a policy switch (Auto/cheapest/fastest/balanced, TPS low/mid/high), a **per-model source override** (Auto / Surplus / Antseed) for the active model, and a live last-route readout. The user's concrete case: when Kimi 3.1 Pro is cheaper on Surplus than Antseed, they can select that model and pin Surplus without editing config or changing marketplaces manually.
6. Balance watchdog + guided Peer onramp (CashApp/Venmo/PayPal → USDC on Base), no keys, no custody.
7. Cache dashboard (passive): cached-token stats and spend per source where upstreams expose them.
8. Catalog submission: repo under ivcained, `hermes plugins validate` clean, entry YAML pinned to a full sha.

**Out-of-scope non-goals:**

1. Cache-aware routing/steering (cut, pending user confirmation, see Decisions).
2. Stake-to-earn / ANTS rewards, any seller-side feature.
3. Non-chat modalities (image, audio, video, embeddings) in routing scope.
4. Key custody, wallet management, or holding user funds.
5. Rebuilding anything Hermes does natively (pickers, streaming, tool calling, per-provider config).
6. A hosted service. Everything runs locally.

## Alternatives Weighed

1. **Per-marketplace routing inside one marketplace (original framing).** Rejected: duplicates what both marketplaces already do internally; no value-add.
2. **`create_client()` override for per-request dispatch.** Rejected: hard-blocked by the plugin host for third-party plugins (verified in-tree).
3. **Tools-only plugin (surp-hermes-x402-llm style).** Rejected: not transparent, agent must remember to ask; no Antseed union; no desktop policy UX.
4. **Two native providers + manual switching (status quo).** Rejected: no automation, no measurement, no shared view; this is the baseline the demo must visibly beat.
5. **Hosted routing service.** Rejected: custody and key handling, catalog review optics, running cost, against the no-custody constraint.
6. **External LiteLLM-class router pointed at both marketplaces.** Rejected: not Hermes-native, no catalog distribution, no desktop control.
7. **Cache-steering in balanced mode.** Cut for v1: requires seller-level cache warmth neither marketplace exposes, fights both marketplaces' internal health-aware routing, and produces plausible-looking wrong answers. Kept as future work conditional on upstreams exposing signals.
8. **Prefixing every model ID per source in the picker.** Rejected: leaves the policy nothing to decide and triples picker noise. Suffix grammar on the model string covers pinning.

## Key Decisions & Staging for Next Steps

**Decisions taken (from the reviews):**

1. **Architecture**: thin ProviderProfile + local OpenAI-compatible routing proxy owned by the plugin. The proxy is the product; the profile is the registration shim.
2. **Name**: locked as **LLMUX** on 2026-11-06. Catalog/plugin id `llmux`, provider id `llmux`, repository `ivcained/llmux`. GitHub and Hermes collision checks passed for the user's repository and the Hermes codebase; unrelated projects use the same name elsewhere, so the README and demo must clearly describe marketplace aggregation.
3. **Model surface**: bare deduplicated IDs for policy routing; `@surplus`/`@antseed`/`@cheap`/`@fast`/`@bal` suffixes for programmatic pinning; chat-completions models only in v1. The desktop adds the same source override as a visible Auto / Surplus / Antseed control for the active model. Source override takes precedence over the global policy; Auto restores policy routing. Placement is verified against the installed Hermes 0.22.5 source: `COMPOSER_AREAS.actions` (`composer.actions`) renders inline before the model pill and will carry the source/policy button; `COMPOSER_AREAS.modelPill` can annotate the model label with the selected source. A status-bar readout remains available as a secondary surface.
4. **Policy definitions** as specified above (cheapest/fastest/balanced formula, TPS floor numbers, relax-with-receipt fallback).
5. **Measurement**: passive-first from real traffic; probes budget-capped (default $0.50/day) and opt-out-able; cold start routes cheapest-by-list-price.
6. **Failover table** as above; `budget_exceeded` never reroutes; mid-stream failures never replay.
7. **Onboarding**: works with a Surplus key alone; Antseed optional, health-checked, guided start, graceful exclusion when down.
8. **Packaging**: one repo, one catalog entry; Python plugin(s) + `desktop/plugin.js` (precedent: meshy/fiddy/hermes-muse ship both surfaces from one package). Day-1 spike verifies whether the tools/watchdog need a second Python package due to kind detection (model-provider sources are excluded from the general plugin manager), and where the shared state rendezvous lives (filesystem state file vs. the proxy's localhost API).
9. **Prior art check**: day-0 measurement spike of the real Surplus-vs-Antseed price/TPS delta on the fleet's model class; README pitched from measured numbers.

**Staging (submission due 2026-11-11 at 5PM EST):**

1. **Day 0, Nov 6**: name and repository locked; delta spike; desktop surface verified as `composer.actions` + `composer.modelPill`; packaging and validation skeleton.
2. **Day 1, Nov 7**: adapters and proxy core: routing decision, streaming passthrough, error table, cost readback, union catalog.
3. **Day 2, Nov 8**: scoreboard persistence, passive measurement, capped probes, read-only agent tools, routing receipts.
4. **Day 3, Nov 9**: desktop source/policy controls and attribution; balance watchdog; guided Peer onramp.
5. **Day 4, Nov 10**: contract tests with recorded fixtures; README with measured delta; screenshots; clean validation and runtime doctor.
6. **Day 5, Nov 11**: final verification buffer; under-90-second demo video; catalog entry pinned to a full sha; PR and submission form before 5PM EST.

**Decisions closed:**

1. **Cache steering**: cut for v1; ship the passive cache dashboard only, with steering as future work. Accepted by the user on 2026-11-06 as part of the reviewed direction.
2. **Name**: LLMUX, selected by the user on 2026-11-06.
3. **Desktop placement**: status bar fallback accepted by the user on 2026-11-06. The per-model source override uses the same placement rule as the policy control.
