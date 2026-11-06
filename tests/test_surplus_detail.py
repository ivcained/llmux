"""Per-provider Surplus detail: join, dedupe, health filter, sample honesty."""

import json
import unittest
from pathlib import Path

from llmux_core import surplus_detail

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MODEL = "qwen3.8-flash"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class JoinOffersTests(unittest.TestCase):
    def setUp(self):
        self.markets = _load(f"surplus_markets_{MODEL}.json")
        self.performance = _load(f"surplus_performance_{MODEL}.json")

    def test_named_providers_with_prices_and_measured_speed(self):
        offers = surplus_detail.join_offers(self.markets, self.performance, model_id=MODEL)
        by_id = {offer.provider_id: offer for offer in offers}
        # The unhealthy/unavailable InferHub offer is dropped; its servable twin wins.
        self.assertEqual(set(by_id), {"inferhub", "dashscope-cn"})
        dashscope = by_id["dashscope-cn"]
        self.assertEqual(dashscope.seller, "Alibaba DashScope \u2014 China")
        self.assertEqual(dashscope.source, "surplus")
        self.assertEqual(dashscope.upstream_model, MODEL)
        self.assertAlmostEqual(dashscope.input_per_m, 0.00147)
        self.assertAlmostEqual(dashscope.output_per_m, 0.004606)
        self.assertEqual(dashscope.tps_p50, 73.3)
        self.assertEqual(dashscope.ttft_p50_ms, 4108.0)
        self.assertEqual(dashscope.samples, 736)

    def test_thin_sample_counts_do_not_fabricate_speed(self):
        # InferHub reports tps_samples=2 and ttft_samples=72 in the fixture.
        offers = surplus_detail.join_offers(self.markets, self.performance, model_id=MODEL)
        inferhub = next(o for o in offers if o.provider_id == "inferhub")
        self.assertIsNone(inferhub.tps_p50)
        self.assertEqual(inferhub.samples, 2)

    def test_missing_performance_still_yields_priced_routes(self):
        offers = surplus_detail.join_offers(self.markets, None, model_id=MODEL)
        self.assertEqual(len(offers), 2)
        self.assertTrue(all(o.tps_p50 is None and o.ttft_p50_ms is None for o in offers))
        self.assertTrue(all(o.input_per_m > 0 for o in offers))

    def test_dedupe_prefers_servable_then_cheapest(self):
        payload = {
            "offers": [
                {"provider_id": "a", "price_input_per_1m": 900, "price_output_per_1m": 900,
                 "healthy": True, "available": True, "route_label": "A cheap servable"},
                {"provider_id": "a", "price_input_per_1m": 100, "price_output_per_1m": 100,
                 "healthy": False, "available": False, "route_label": "A cheaper but down"},
                {"provider_id": "b", "price_input_per_1m": 2000, "price_output_per_1m": 2000,
                 "healthy": True, "available": True, "route_label": "B"},
            ]
        }
        offers = surplus_detail.join_offers(payload, None, model_id="m")
        a = next(o for o in offers if o.provider_id == "a")
        self.assertEqual(a.seller, "A cheap servable")
        self.assertAlmostEqual(a.input_per_m, 0.0009)


class FetchProviderOffersTests(unittest.TestCase):
    def test_fetch_joins_both_endpoints(self):
        markets = _load(f"surplus_markets_{MODEL}.json")
        performance = _load(f"surplus_performance_{MODEL}.json")
        seen = []

        def opener(url, timeout):
            seen.append(url)
            if "/api/markets/" in url:
                return markets
            if "/api/analytics/performance/" in url:
                return performance
            raise AssertionError(url)

        offers = surplus_detail.fetch_provider_offers(
            MODEL, base_url="https://example.test", timeout=1.0, opener=opener
        )
        self.assertEqual(len(seen), 2)
        self.assertTrue(seen[0].endswith(f"/api/markets/{MODEL}"))
        self.assertEqual({o.provider_id for o in offers}, {"inferhub", "dashscope-cn"})

    def test_performance_failure_degrades_to_prices_only(self):
        markets = _load(f"surplus_markets_{MODEL}.json")

        def opener(url, timeout):
            if "/api/markets/" in url:
                return markets
            raise OSError("analytics down")

        offers = surplus_detail.fetch_provider_offers(
            MODEL, base_url="https://example.test", timeout=1.0, opener=opener
        )
        self.assertEqual(len(offers), 2)
        self.assertTrue(all(o.tps_p50 is None for o in offers))


if __name__ == "__main__":
    unittest.main()
