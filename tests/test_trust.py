"""Trusted / all sellers: labels, the card switch payload, and routing safety."""

import unittest

from llmux_core.catalog import Catalog
from llmux_core.models import ModelRecord, Offer
from llmux_core.proxy import ProxyServer
from llmux_core.routing import NoRouteError, choose_offer
from llmux_core.surplus_detail import join_offers


def offer(seller, *, trusted, price=1.0, tps=50.0, source="antseed", peer=None):
    return Offer(
        source=source,
        upstream_model=f"{peer or seller}@m",
        input_per_m=price,
        output_per_m=price * 4,
        tps_p50=tps,
        ttft_p50_ms=800,
        samples=40,
        seller=seller,
        peer_id=peer or seller,
        trusted=trusted,
        trust_basis="Antseed reputation 80" if trusted else "Antseed reputation 10 (below 50)",
    )


class CatalogAdapter:
    source = "antseed"

    def __init__(self, records):
        self.records = records

    def fetch_catalog(self):
        return self.records


def proxy_with(*offers):
    record = ModelRecord(id="m", name="M", offers=tuple(offers), aliases=("m",))
    return ProxyServer(adapters={"antseed": CatalogAdapter([record])})


class RoutingSafetyTests(unittest.TestCase):
    def test_auto_routing_never_picks_untrusted_even_if_cheaper_and_faster(self):
        record = ModelRecord(
            id="m",
            name="M",
            offers=(
                offer("Shady", trusted=False, price=0.01, tps=500),
                offer("Solid Co", trusted=True, price=1.0, tps=50),
            ),
        )
        for policy in ("cheapest", "fastest", "balanced"):
            self.assertEqual(choose_offer(record, policy=policy).offer.seller, "Solid Co")

    def test_unlabelled_offers_stay_routable(self):
        record = ModelRecord(id="m", name="M", offers=(offer("Fixed API", trusted=None),))
        self.assertEqual(choose_offer(record).offer.seller, "Fixed API")

    def test_only_untrusted_means_no_auto_route(self):
        record = ModelRecord(id="m", name="M", offers=(offer("Shady", trusted=False),))
        with self.assertRaises(NoRouteError):
            choose_offer(record)

    def test_explicit_pin_to_untrusted_seller_still_works(self):
        proxy = proxy_with(offer("Shady", trusted=False), offer("Good", trusted=True))
        decision, _, auto = proxy._route("m@peer:Shady")
        self.assertEqual(decision.offer.seller, "Shady")
        self.assertFalse(auto)

    def test_pin_works_when_the_only_seller_is_untrusted(self):
        # The auto plan has no candidate; an explicit pin must not depend on it.
        proxy = proxy_with(offer("Shady", trusted=False), offer("Good", trusted=True))
        proxy._catalog = Catalog(records=(ModelRecord(
            id="m", name="M", offers=(offer("Shady", trusted=False),), aliases=("m",)),))
        proxy._catalog_time = 1e12
        decision, _, _ = proxy._route("m@peer:Shady")
        self.assertEqual(decision.offer.seller, "Shady")

    def test_model_served_only_by_untrusted_sellers_is_not_advertised(self):
        catalog = Catalog.merge(
            [[ModelRecord(id="m", name="M", offers=(offer("Shady", trusted=False),))]]
        )
        self.assertEqual(catalog.records, ())


class SellerViewTests(unittest.TestCase):
    def setUp(self):
        self.proxy = proxy_with(
            offer("Shady", trusted=False, tps=90),
            offer("Good", trusted=True, tps=60),
            offer("Fine", trusted=True, tps=40),
        )

    def test_default_view_hides_untrusted(self):
        rows = self.proxy.seller_options("m")
        self.assertEqual([r["seller"] for r in rows], ["Good", "Fine"])
        self.assertTrue(all(r["trusted"] is True for r in rows))

    def test_all_view_lists_everything_labelled(self):
        rows = self.proxy.seller_options("m", trust="all")
        self.assertEqual({r["seller"]: r["trusted"] for r in rows}, {"Shady": False, "Good": True, "Fine": True})
        shady = next(r for r in rows if r["seller"] == "Shady")
        self.assertIn("below 50", shady["trust_basis"])

    def test_payload_carries_both_views_and_count(self):
        payload = self.proxy.seller_options_payload("m")
        self.assertEqual(len(payload["data"]), 2)
        self.assertEqual(len(payload["all_sellers"]), 3)
        self.assertEqual(payload["untrusted_count"], 1)
        self.assertTrue(payload["current"]["trusted"])


class SurplusTrustFieldTests(unittest.TestCase):
    def test_trusted_flag_parsed_from_markets(self):
        markets = {
            "offers": [
                {"provider_id": "a", "route_label": "A", "healthy": True, "available": True,
                 "price_input_per_1m": 1000, "price_output_per_1m": 4000, "trusted": True},
                {"provider_id": "b", "route_label": "B", "healthy": True, "available": True,
                 "price_input_per_1m": 500, "price_output_per_1m": 2000, "trusted": False},
                {"provider_id": "c", "route_label": "C", "healthy": True, "available": True,
                 "price_input_per_1m": 500, "price_output_per_1m": 2000},
            ]
        }
        offers = {o.provider_id: o for o in join_offers(markets, None, model_id="m")}
        self.assertIs(offers["a"].trusted, True)
        self.assertIs(offers["b"].trusted, False)
        self.assertEqual(offers["b"].trust_basis, "not trusted by Surplus")
        self.assertIsNone(offers["c"].trusted, "no signal published: no verdict invented")


if __name__ == "__main__":
    unittest.main()
