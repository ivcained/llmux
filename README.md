# LLMUX

**Many markets. One model picker.**

LLMUX is a Hermes Agent plugin that aggregates LLM inference marketplaces: [Surplus Intelligence](https://www.surplusintelligence.ai/), [Antseed](https://antseed.com/), and [Solid](https://solid.xyz/). You pick a model once. LLMUX compares the offers for that model across sources and routes each request to the source you prefer: cheapest, fastest, or balanced. Per-model source overrides live in the desktop composer, next to the model pill.

## Status

Under active development for the [Hermes Agent Plugin Hackathon](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins). The routing proxy, all three marketplace adapters, the desktop seller card, the live market cache, and the local usage ledger are implemented.

Release 0.1.1 verification passes 194 Python tests (including the Desktop wrapper), with 62 Desktop card scenarios also run directly. The validator and runtime doctor pass. Eleven isolated-browser states at 1280x800 and 390x844 verify corrected narrow layouts, trust-tab contrast and unsupported-direct-provider wording against live read-only catalog data. These are component/harness checks, not installed-Desktop proof. The full design review and build plan live in [`concepts_brief.md`](concepts_brief.md); the first live price and performance comparison, with captured fixtures, is in [`docs/delta-spike-2026-11-06.md`](docs/delta-spike-2026-11-06.md).

Not yet demonstrated: a mid-chat seller switch in the installed desktop app, a paid end-to-end reply through the proxy, and a ledger row from a real reply. The latest card fixes are not yet verified in the installed desktop. Catalog admission, demo publication and the hackathon form are separate submission steps, not test results. The working checklist is [`docs/submission-readiness.md`](docs/submission-readiness.md); desktop verification status is [`docs/desktop-live-test-status.md`](docs/desktop-live-test-status.md).

## What it does

- **One model catalog.** The union of the Surplus, Antseed, and Solid chat-model catalogs, deduplicated, with aliases reconciled across sources.
- **Policy routing.** `cheapest`, `fastest`, or `balanced`, with an optional tokens-per-second floor (low 10, mid 25, high 60). Balanced picks the cheapest offer within 25% of the fastest measured TPS.
- **Per-model source choice.** Pin the selected model to Surplus, Antseed, or Solid, or leave it on Auto and let the policy decide.
- **Mid-chat seller switching.** A "Switch seller" card lists up to four ranked routes with median TTFT, median TPS, an estimated reply time for a 1000-output-token answer, price, and discount versus list. Confirming validates the route through the backend first, stops any in-flight turn, and pins that seller for the next turn. It does not migrate or regenerate an interrupted response. The next request sends the current conversation context to the new route, so cached-prefix discounts can be lost and the interrupted request may still be charged. If the session update fails, the card offers a clean session on the pinned route instead of leaving a broken runtime.
- **Honest trust semantics.** Antseed peers carry an effective reputation score; peers below the bar (default 50, `LLMUX_ANTSEED_MIN_REPUTATION`) are labelled Untrusted and are never auto-routed, though you can still pin one explicitly from the "All sellers" view. Solid publishes no trust signal, so its rows carry no badge. Unknown trust is never presented as verified.
- **Live routing receipts.** Every routed response carries `X-LLMUX-*` headers naming the source, requested and applied policy, prices, measured TPS, floor-relaxed flag, and failover origin.
- **Live market cache.** A background loop (45 second default) refreshes the catalog and the seller detail of watched models; reads are stale-while-revalidate, and an atomic snapshot at `$HERMES_HOME/llmux/market-cache.json` lets a restarted proxy serve the last known market immediately. The snapshot holds public market data only.
- **Usage ledger and savings (opt-in).** With `LLMUX_TRACK_USAGE=1`, each completed proxy-routed reply appends one JSONL row locally; the card shows 30-day totals and up to three ranked tips.
- **Failover (opt-in).** Disabled by default: a failed request may still have been billed. Setting `LLMUX_ALLOW_FAILOVER=1` permits an unpinned request to try another source after retryable upstream errors (429, 5xx and some 402 credit errors). There is no hard spending ceiling; enabling this can produce multiple billable attempts. Explicit seller pins and budget-exceeded errors never fail over.

## What it does not do

- **No balance watchdog and no fiat onramp.** Both were cut from scope. Funding happens on each marketplace.
- **No custody.** LLMUX holds no wallet, funds, or private keys and performs no on-chain action. Antseed payments stay inside the Antseed buyer proxy.
- **No privacy layer.** When inference runs, the full conversation context is sent to the selected seller or marketplace. LLMUX only chooses where it goes.
- **Solid prices are a manually entered table dated 2026-11-09, not a live feed.** Override it with `LLMUX_SOLID_PRICES_JSON` when it changes. Solid publishes no speed or trust metrics: speed reads "not published" and no trust verdict is invented. Surplus and Antseed price and speed figures are live upstream measurements (medians, trailing 24h) that move with seller liquidity.

## Architecture

LLMUX registers as a single Hermes model provider whose base URL points at a local OpenAI-compatible routing proxy. The proxy owns the marketplace adapters (the only modules that speak upstream wire formats), policy evaluation, streaming passthrough, cost readback, failover, the live market cache, and the usage ledger. The desktop plugin contributes the seller card through the public Desktop Plugin SDK; the dashboard backend exposes read-only passthrough routes to the proxy.

## Requirements

- Hermes Agent 0.22.5 or newer on Linux or macOS (the detached proxy uses a Unix process lock)
- A Surplus Intelligence buyer key (`inf_...`) with funded credit, as `SURPLUS_API_KEY`
- Optional: an Antseed buyer proxy on `http://127.0.0.1:8377/v1` with a funded payment channel
- Optional: a Solid API key as `LLMUX_SOLID_API_KEY`, `SOLID_API_KEY`, or `HERMES_CUSTOM_SOLID_API_KEY` through Hermes' profile-scoped credential API. Solid's `/models` needs the key, so without one the Solid source is left out entirely rather than reporting a catalog error on every refresh.

Antseed and Solid are optional. LLMUX works with Surplus alone and reports per-source catalog errors in `/health`.

## Install

The repository is public. Until catalog admission, install directly from GitHub:

```bash
hermes plugins install ivcained/llmux
# or pin one immutable commit:
hermes plugins install ivcained/llmux --ref <40-character-commit-sha>
```

For a development checkout, validate instead of installing into an active profile:

```bash
hermes plugins validate /path/to/llmux --install-deps
hermes plugins doctor /path/to/llmux --ci
```

Do not install a development copy into an active Hermes profile while building. Use an isolated `HERMES_HOME`.

## Running the proxy

Loading the plugin starts the proxy automatically: a TCP probe on `127.0.0.1:8378` (or `LLMUX_PORT`) decides whether a child process is spawned, and a lock file serializes concurrent spawners so only one child binds the port. The child is detached into its own session, so it survives Hermes exit and keeps running until the user or the machine stops it. Child output goes to `$HERMES_HOME/logs/llmux-proxy.log`. The child receives only an allowlisted runtime environment plus marketplace keys resolved by `agent.secret_scope.get_secret` for the active profile. It does not inherit unrelated gateway tokens or approval flags. No credential files are read by LLMUX.

Set `LLMUX_AUTOSTART=0` to keep plugin loading side-effect free and run the proxy yourself:

```bash
export SURPLUS_API_KEY="inf_..."
export LLMUX_POLICY="balanced"          # cheapest | fastest | balanced
export LLMUX_TPS_FLOOR="mid"            # optional: low | mid | high
python -m llmux_core --port 8378
```

The proxy binds to `127.0.0.1` only by default. Non-local binding requires `LLMUX_ALLOW_REMOTE=1`.

Every endpoint except `/health` requires the current profile's local bearer. Hermes and the SDK backend supply it automatically. An old unauthenticated proxy or another profile's listener is rejected before inference or private usage reads. One port serves one profile and configuration snapshot. Use separate ports for concurrent profiles; restart that proxy safely after keys or routing settings change. Antseed-only operation without any marketplace key is not supported by this authentication scheme.

Endpoints:

- `GET /health` returns policy, floor, and per-source catalog errors.
- `GET /llmux/authcheck` verifies the expected authenticated proxy protocol without inference.
- `GET /v1/models` returns the deduplicated union catalog with `sources` on every model.
- `GET /v1/models/<id>` returns normalized offers, prices, TPS, TTFT, sample counts, and health.
- `POST /v1/chat/completions` routes one request and passes JSON or SSE through.
- `GET /llmux/plan?model=<id>` returns the routing decision for a model string without calling upstream.
- `GET /llmux/seller-options?model=<id>` returns up to eight ranked seller rows (the card displays the top four) plus the untrusted-inclusive list, comparable models in the same price tier, the current route, a freshness stamp, and the savings summary.
- `GET /llmux/similar-models?model=<id>` returns comparable models in the same price tier, ranked by measured speed.
- `GET /llmux/savings?days=30` returns spend, savings and ranked advice from the local usage ledger.

## Usage, savings and rewards

Usage recording is disabled by default. Enable it with `LLMUX_TRACK_USAGE=1`; then every completed reply that went through the proxy appends one row to `$HERMES_HOME/llmux/usage.jsonl`: model, source, seller, token counts, cost, what the same tokens would have cost at list price and on the cheapest and priciest live offer, and any estimated cashback. Replies that bypass the proxy (for example a chat on a direct Solid provider) are not routed by LLMUX and are not recorded. The card shows the 30-day totals and up to three ranked tips.

- **Profile-scoped keys.** Hermes owns credential storage and supplies keys through `agent.secret_scope.get_secret`. LLMUX passes those keys only to its detached proxy and the matching marketplace. A profile/configuration-bound loopback bearer, not the upstream key, authenticates Hermes and the card to that proxy. The local bearer is derived in memory, never written to a second credential file. The ledger never stores keys, prompts, completions, wallet addresses, or peer secrets; only token counts and dollar figures.
- **Honest costs.** When Surplus reports what it charged (`x-si-buyer-cost-micro`), that figure is used and marked authoritative. Otherwise cost is estimated from published per-token prices. A reply with no usage block is recorded with nulls, not zeros, and the card warns when reported-usage coverage drops below 80%.
- **Streaming is covered.** Hermes requests `stream_options.include_usage`, so the final SSE event carries token counts; LLMUX reads it without buffering the reply.
- **Turn it off** with `LLMUX_TRACK_USAGE=0` (the default). Delete the file at any time to reset.

Cashback and free-model programs change often, so LLMUX ships with none hard-coded. Describe the ones you have in `$HERMES_HOME/llmux/rewards.json` (or point `LLMUX_REWARDS_JSON` at another file); see `examples/rewards.example.json`. Terms are treated as estimates unless you mark them `"confirmed": true`, and the card labels them accordingly.

Model ids accept routing directives:

```text
kimi-k3.1-pro                          policy decides the source
kimi-k3.1-pro@surplus                  pin Surplus
kimi-k3.1-pro@antseed                  pin Antseed
kimi-k3.1-pro@solid                    pin Solid
kimi-k3.1-pro@fastest                  pin the policy for this request
kimi-k3.1-pro@antseed@cheap
kimi-k3.1-pro@peer:<peer-id>           pin one Antseed seller
kimi-k3.1-pro@provider:<provider-id>   pin one Surplus provider
```

`peer:` and `provider:` pins are marketplace-specific and mutually exclusive.

## Testing

The suite runs against recorded fixtures and local synthetic marketplaces; it makes no paid inference calls. Profile-isolation regressions additionally exercise Hermes' actual credential-resolution seam and a real detached child on an ephemeral loopback port. Final full-suite results are recorded in `docs/submission-readiness.md`. A separate read-only live catalog smoke test is the only network-dependent check.

## License

MIT. See [LICENSE](LICENSE).
