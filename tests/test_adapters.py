import json
import unittest
from pathlib import Path

from llmux_core.adapters import AntseedAdapter, SurplusAdapter, CatalogSchemaError
from llmux_core.catalog import Catalog
from llmux_core.models import ModelRecord, Offer

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class SurplusAdapterTests(unittest.TestCase):
    def test_live_marketplace_fields_become_normalized_offer(self):
        models = {
            "data": [
                {
                    "id": "kimi-k3.1-pro",
                    "name": "Kimi K3.1 Pro",
                    "context_length": 1048576,
                    "architecture": {"output_modalities": ["text"]},
                    "pricing": {"prompt": "0.000002", "completion": "0.00001"},
                }
            ]
        }
        records = SurplusAdapter.parse_catalog(models, fixture("surplus_marketplace_kimi.json"))
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.id, "kimi-k3.1-pro")
        offer = record.offers[0]
        self.assertEqual(offer.source, "surplus")
        self.assertAlmostEqual(offer.input_per_m, 0.196926)
        self.assertAlmostEqual(offer.output_per_m, 0.984629)
        self.assertAlmostEqual(offer.cached_input_per_m, 0.019693)
        self.assertEqual(offer.tps_p50, 54.3)
        self.assertEqual(offer.ttft_p50_ms, 7487)
        self.assertEqual(offer.samples, 1025)
        self.assertTrue(offer.healthy)


class AntseedAdapterTests(unittest.TestCase):
    def test_live_peer_fields_and_kimi_alias_become_normalized_offers(self):
        records = AntseedAdapter.parse_catalog(
            fixture("antseed_models_kimi.json"), min_reputation=50
        )
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.id, "kimi-k3.1-pro")
        self.assertIn("kimi-k3-1-pro", record.aliases)
        self.assertEqual(len(record.offers), 2)
        self.assertEqual(record.offers[0].source, "antseed")
        self.assertEqual(
            record.offers[0].upstream_model,
            "d19ffae0e85b5230422019926b2f37ec0f1f41f3@kimi-k3-1-pro",
        )
        self.assertEqual(record.offers[0].tps_p50, 93)
        self.assertEqual(record.offers[0].samples, 160)

    def test_reputation_threshold_controls_eligible_peers(self):
        records = AntseedAdapter.parse_catalog(
            fixture("antseed_models_kimi.json"), min_reputation=0
        )
        self.assertEqual(len(records[0].offers), 4)

    def test_malformed_payload_fails_closed(self):
        with self.assertRaises(CatalogSchemaError):
            AntseedAdapter.parse_catalog({"unexpected": []}, min_reputation=0)
        with self.assertRaises(CatalogSchemaError):
            SurplusAdapter.parse_catalog({"unexpected": []}, {"unexpected": []})


class UnionCatalogTests(unittest.TestCase):
    def test_union_deduplicates_model_and_resolves_marketplace_alias(self):
        models = {
            "data": [
                {
                    "id": "kimi-k3.1-pro",
                    "name": "Kimi K3.1 Pro",
                    "architecture": {"output_modalities": ["text"]},
                }
            ]
        }
        surplus = SurplusAdapter.parse_catalog(models, fixture("surplus_marketplace_kimi.json"))
        antseed = AntseedAdapter.parse_catalog(
            fixture("antseed_models_kimi.json"), min_reputation=50
        )
        catalog = Catalog.merge([surplus, antseed])
        self.assertEqual(len(catalog.records), 1)
        self.assertEqual(len(catalog.get("kimi-k3-1-pro").offers), 3)
        self.assertEqual(catalog.get("kimi-k3.1-pro").name, "Kimi K3.1 Pro")

    def test_models_without_routable_offers_are_excluded(self):
        empty = ModelRecord(id="catalog-only", name="Catalog Only")
        market = ModelRecord(
            id="kimi-k3.1-pro",
            name="Kimi K3.1 Pro",
            offers=(Offer(source="surplus", upstream_model="kimi-k3.1-pro", input_per_m=1, output_per_m=2),),
        )
        catalog = Catalog.merge([[empty], [market]])
        self.assertEqual([record.id for record in catalog.records], ["kimi-k3.1-pro"])

    def test_models_with_only_unhealthy_offers_are_excluded(self):
        market = ModelRecord(
            id="dead-model",
            name="Dead Model",
            offers=(
                Offer(
                    source="surplus",
                    upstream_model="dead-model",
                    input_per_m=1,
                    output_per_m=2,
                    healthy=False,
                ),
            ),
        )
        catalog = Catalog.merge([[market]])
        self.assertEqual(catalog.records, ())


if __name__ == "__main__":
    unittest.main()
