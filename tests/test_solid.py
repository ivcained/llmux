"""Solid adapter, provider-accurate current route, key discovery."""

import tempfile
import unittest
from pathlib import Path

from llmux_core import config as config_mod
from llmux_core.adapters import SOLID_PRICES, SolidAdapter
from llmux_core.catalog import Catalog
from llmux_core.proxy import ProxyServer, provider_source
from llmux_core.routing import parse_model_directive

SOLID_MODELS = {
    "data": [
        {"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5"},
        {"id": "deepseek/deepseek-v4.1-flash", "display_name": "DeepSeek V4.1 Flash"},
        {"id": "kimi-k3", "display_name": "Kimi K3"},
        {"id": "brand-new-model", "display_name": "Unpriced"},
    ]
}


class FakeSolid:
    source = "solid"
    base_url = "http://127.0.0.1:9/v1"
    api_key = None

    def fetch_catalog(self):
        return SolidAdapter.parse_catalog(SOLID_MODELS)


class SolidCatalogTests(unittest.TestCase):
    def setUp(self):
        self.records = {r.id: r for r in SolidAdapter.parse_catalog(SOLID_MODELS)}

    def test_priced_models_parse_with_dashboard_prices(self):
        opus = self.records["claude-opus-5.5"]  # dash form joins the dotted catalog id
        offer = opus.offers[0]
        self.assertEqual((offer.input_per_m, offer.output_per_m), (0.38, 1.20))
        self.assertEqual(offer.cached_input_per_m, 0.27)
        self.assertEqual(offer.upstream_model, "claude-opus-5-5")
        self.assertEqual(offer.seller, "Solid")
        self.assertIsNone(offer.tps_p50)  # Solid reports no speed; never guessed

    def test_list_price_derived_from_the_published_discount(self):
        meta = self.records["claude-opus-5.5"].metadata
        self.assertAlmostEqual(meta["list_input_per_m"], 0.38 / 0.08, places=6)
        self.assertEqual(meta["prices_as_of"], "2026-11-09")

    def test_free_model_is_zero_priced_and_reachable_by_its_slash_id(self):
        flash = self.records["deepseek-v4.1-flash"]
        self.assertEqual((flash.offers[0].input_per_m, flash.offers[0].output_per_m), (0.0, 0.0))
        catalog = Catalog.merge([list(self.records.values())])
        self.assertIs(catalog.get("deepseek/deepseek-v4.1-flash"), flash)

    def test_unpriced_models_are_skipped_not_shown_free(self):
        self.assertNotIn("brand-new-model", self.records)

    def test_price_table_covers_the_six_dashboard_models(self):
        self.assertEqual(len(SOLID_PRICES), 6)

    def test_solid_is_a_pinnable_source(self):
        self.assertEqual(parse_model_directive("kimi-k3@solid").source, "solid")


class ProviderSourceTests(unittest.TestCase):
    def test_mapping(self):
        self.assertIsNone(provider_source(None))
        self.assertIsNone(provider_source("llmux"))
        self.assertIsNone(provider_source("custom:llmux"))
        self.assertEqual(provider_source("solid"), "solid")
        self.assertEqual(provider_source("custom:solid"), "solid")
        self.assertEqual(provider_source("openrouter"), "unknown")


class CurrentRouteProviderTests(unittest.TestCase):
    def setUp(self):
        self.proxy = ProxyServer(adapters={"solid": FakeSolid()})

    def test_direct_solid_chat_reports_solid_not_an_llmux_plan(self):
        current = self.proxy.current_route("deepseek/deepseek-v4.1-flash", "solid")
        assert current is not None
        self.assertEqual(current["source"], "solid")
        self.assertEqual(current["seller"], "Solid")
        self.assertTrue(current["direct"])
        self.assertEqual(current["input_per_m"], 0.0)
        self.assertIn("not routing", current["reason"])

    def test_unknown_direct_provider_borrows_no_prices(self):
        current = self.proxy.current_route("kimi-k3", "openrouter")
        assert current is not None
        self.assertEqual(current["source"], "openrouter")
        self.assertIsNone(current["input_per_m"])
        self.assertTrue(current["direct"])

    def test_llmux_provider_uses_the_planner(self):
        current = self.proxy.current_route("kimi-k3", "llmux")
        assert current is not None
        self.assertFalse(current["direct"])
        self.assertEqual(current["source"], "solid")

    def test_unmeasured_solid_offers_still_listed_as_sellers(self):
        rows = self.proxy.seller_options("kimi-k3")
        self.assertEqual([r["source"] for r in rows], ["solid"])
        self.assertIsNone(rows[0]["tps_p50"])

    def test_payload_passes_provider_through(self):
        payload = self.proxy.seller_options_payload("kimi-k3", "solid")
        self.assertTrue(payload["current"]["direct"])


class SolidKeyDiscoveryTests(unittest.TestCase):
    def test_hermes_custom_provider_key_is_found(self):
        settings = config_mod.load_settings({"HERMES_CUSTOM_SOLID_API_KEY": "k1"})
        self.assertEqual(settings.solid_api_key, "k1")

    def test_explicit_llmux_key_wins(self):
        settings = config_mod.load_settings(
            {"LLMUX_SOLID_API_KEY": "mine", "HERMES_CUSTOM_SOLID_API_KEY": "k1"}
        )
        self.assertEqual(settings.solid_api_key, "mine")

    def test_dotenv_fallback_reads_only_named_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text('OTHER=x\nexport HERMES_CUSTOM_SOLID_API_KEY="from-file"\n')
            self.assertEqual(
                config_mod._dotenv_value(config_mod.SOLID_KEY_NAMES, env), "from-file"
            )
            self.assertIsNone(config_mod._dotenv_value(("NOPE",), env))
            self.assertIsNone(config_mod._dotenv_value(("X",), Path(tmp) / "missing"))


if __name__ == "__main__":
    unittest.main()
