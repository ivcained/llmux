# LLMUX

**Many markets. One model picker.**

LLMUX is a Hermes Agent plugin that aggregates LLM inference marketplaces, starting with [Surplus Intelligence](https://www.surplusintelligence.ai/) and [Antseed](https://antseed.com/). You pick a model once. LLMUX compares the live offers for that model across marketplaces and routes each request to the source you prefer: cheapest, fastest, or balanced. Per-model source overrides live in the desktop composer, next to the model pill.

## Status

Under active development for the [Hermes Agent Plugin Hackathon](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins). The repository currently contains the desktop control surface implementation and the provider registration skeleton. The routing proxy and marketplace adapters land next.

## What it will do

- **One model catalog.** The union of the Surplus and Antseed chat-model catalogs, deduplicated, with the same model available from either marketplace.
- **Policy routing.** `cheapest`, `fastest`, or `balanced`, with a tokens-per-second floor (low, mid, high).
- **Per-model source choice.** Pin the selected model to Surplus or Antseed, or leave it on Auto and let the policy decide. Example: Kimi 3.1 Pro is cheaper on Surplus today, so choose Surplus for that model once.
- **Mid-chat seller switching.** A "Switch seller" card lists up to four ranked offers with TTFT, TPS, estimated reply time and price. Confirming the switch pins that seller for the next turn, stops any in-flight turn first, and warns that the whole chat re-sends and the cached-prefix discount is lost. If the gateway refuses the switch, the card offers a clean session on the pinned route instead of leaving a broken runtime.
- **Live routing receipts.** See which marketplace served the last request, what it cost, and its measured speed.
- **Balance watchdog.** Track both marketplace balances separately and warn before either runs dry.
- **Fiat refill.** Open a Peer.xyz onramp to fund the wallet with USDC on Base using CashApp, Venmo, or PayPal rails, then follow the guided deposit step for the marketplace you need. LLMUX never holds keys or funds.
- **Cache dashboard.** Cached-token and spend reporting where the marketplaces expose it.

## Architecture

LLMUX registers as a single Hermes model provider whose base URL points at a local OpenAI-compatible routing proxy. The proxy owns marketplace adapters, policy evaluation, streaming passthrough, cost readback, failover, and the measurement scoreboard. The desktop plugin contributes a source selector and policy selector to the composer through the public Desktop Plugin SDK.

The full concept, decisions, risk review, and build plan live in [`concepts_brief.md`](concepts_brief.md). The first live price and performance comparison, with captured fixtures, is in [`docs/delta-spike-2026-11-06.md`](docs/delta-spike-2026-11-06.md).

## Requirements

- Hermes Agent 0.22.5 or newer
- A Surplus Intelligence buyer key (`inf_...`) with funded credit
- Optional: an Antseed buyer proxy on `http://127.0.0.1:8377/v1` with a funded payment channel

Antseed is optional. LLMUX works with Surplus alone and marks Antseed unavailable when its buyer proxy is not running.

## Running the proxy

```bash
export SURPLUS_API_KEY="inf_..."
export LLMUX_POLICY="balanced"          # cheapest | fastest | balanced
export LLMUX_TPS_FLOOR="mid"            # optional: low | mid | high
python -m llmux_core --port 8378
```

The proxy binds to `127.0.0.1` only by default. Non-local binding requires `LLMUX_ALLOW_REMOTE=1`.

Endpoints:

- `GET /health` returns policy, floor, and per-marketplace catalog errors.
- `GET /v1/models` returns the deduplicated union catalog with `sources` on every model.
- `GET /v1/models/<id>` returns normalized offers, prices, TPS, TTFT, sample counts, and health.
- `POST /v1/chat/completions` routes one request and passes JSON or SSE through.
- `GET /llmux/seller-options?model=<id>` returns up to four ranked seller offers with TTFT, TPS, estimated reply time, price and a cache-cost warning.

Model ids accept routing directives:

```text
kimi-k3.1-pro              policy decides the marketplace
kimi-k3.1-pro@surplus      pin Surplus
kimi-k3.1-pro@antseed      pin Antseed
kimi-k3.1-pro@fastest      pin the policy for this request
kimi-k3.1-pro@antseed@cheap
```

Every routed response carries `X-LLMUX-*` receipt headers: source, requested policy, applied policy, input/output price per million, measured TPS, floor-relaxed flag, and failover origin when one occurred.

The routing proxy is tested against recorded live fixtures and local mock marketplaces. It does not make paid inference calls in its test suite, except for the separate read-only live catalog smoke test.

The plugin is currently validated with:

```bash
hermes plugins validate /path/to/llmux --install-deps
hermes plugins doctor /path/to/llmux --ci
```

Do not install a development copy into an active Hermes profile while building. Use an isolated `HERMES_HOME`.

## License

MIT. See [LICENSE](LICENSE).
