# Live marketplace delta spike: Kimi K3.1 Pro

* **Captured:** 2026-11-06T05:54:45Z
* **Surplus model id:** `kimi-k3.1-pro`
* **Antseed model id:** `kimi-k3-1-pro`
* **Method:** read-only GET requests. No inference calls and no spend.

This is a point-in-time snapshot, not a standing price claim. Seller offers, health, discounts, and performance move.

## Sources

- Surplus live marketplace: `GET https://api.surplusintelligence.ai/api/marketplace`
- Surplus public price matrix: `GET https://api.surplusintelligence.ai/v1/prices`
- Antseed local buyer catalog: `GET http://127.0.0.1:8377/v1/models?type=text`

## Price snapshot

Surplus direct list price is $2.00 input and $10.00 output per million tokens. Its best live marketplace offer in the response was:

| Source | Eligibility | Input / 1M | Output / 1M | Cached input / 1M |
|---|---|---:|---:|---:|
| Surplus best offer | Marketplace best | $0.196926 | $0.984629 | $0.019693 |
| Antseed attacompute.ai | Healthy, effective reputation 53.18 | $0.12 | $0.585 | $0.12 |
| Antseed Vector Studios | Healthy, effective reputation 0 | $0.0603 | $0.494 | $0.018 |

The trusted healthy Antseed offer was 0.609x the Surplus best-offer input price and 0.594x the output price in this snapshot. The gap is large enough to justify cross-market routing for this model at this moment.

Do not compare only the absolute cheapest row. Trust, health, success rate, offer caps, and routability change which offer a buyer can actually use.

## Performance and cache telemetry found

Surplus returned aggregate performance and cache metrics for this model:

- TPS p50: 54.3 across 1,025 samples
- TTFT p50: 7,487 ms across 21,937 samples
- Uptime: 77.9%
- Cache hit rate: 92.8%
- Healthy seller count: 86

Antseed returned per-seller price, reputation, health, and in several cases performance:

| Seller | Effective reputation | Requests | Success rate | TTFT p50 | TPS p50 |
|---|---:|---:|---:|---:|---:|
| Super Seeder | 77.11 | 160 | 100% | 3,460 ms | 93 |
| attacompute.ai | 53.18 | 30 | 73.33% | 17,488 ms | 76 |
| Vector Studios | 0 | 23 | 100% | 17,488 ms | 76 |
| Hana Gateway | 0 | 1 | 100% | 37,488 ms | 290 |

Hana Gateway illustrates why sample count must be visible: 290 TPS from one request is not comparable with 93 TPS from 160 requests.

## Design corrections from the spike

1. **Upstream telemetry exists.** The earlier assumption that neither marketplace exposes TPS was wrong for the live endpoints inspected. Seed the scoreboard from Surplus aggregate metrics and Antseed per-seller metrics, then refine with LLMUX's own real traffic.
2. **Paid probes move to the edge.** Use them only for missing or stale measurements, under the existing budget cap. They are not the primary measurement system.
3. **Model id normalization is required immediately.** Surplus calls this model `kimi-k3.1-pro`; Antseed calls it `kimi-k3-1-pro`. The normalized catalog needs explicit aliases, not string equality.
4. **Price comparison needs an eligibility policy.** Compare healthy and routable offers, with trust requirements visible. An untrusted $0.06 seller and a trusted $0.99 seller are not interchangeable.
5. **Cache monitoring has real upstream data.** Surplus exposes a model-level cache hit rate and Antseed exposes cached-input prices. This supports the passive cache dashboard. It still does not expose which seller holds a warm conversation prefix, so cache steering remains cut.

Selected live response subsets are preserved under `tests/fixtures/` for adapter contract tests.
