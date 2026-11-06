import unittest

from llmux_core.app import build_proxy
from llmux_core.adapters import AntseedAdapter, SurplusAdapter
from llmux_core.config import Settings, load_settings


class SettingsTests(unittest.TestCase):
    def test_defaults_are_local_and_safe(self):
        settings = load_settings({})
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 8378)
        self.assertEqual(settings.policy, "balanced")
        self.assertIsNone(settings.tps_floor)
        self.assertEqual(settings.antseed_min_reputation, 50.0)

    def test_environment_overrides(self):
        settings = load_settings(
            {
                "LLMUX_POLICY": "fastest",
                "LLMUX_TPS_FLOOR": "high",
                "LLMUX_PORT": "9001",
                "LLMUX_ANTSEED_MIN_REPUTATION": "42.5",
                "LLMUX_BALANCED_TOLERANCE": "0.4",
            }
        )
        self.assertEqual(settings.policy, "fastest")
        self.assertEqual(settings.tps_floor, "high")
        self.assertEqual(settings.port, 9001)
        self.assertEqual(settings.antseed_min_reputation, 42.5)
        self.assertEqual(settings.balanced_tolerance, 0.4)

    def test_invalid_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            load_settings({"LLMUX_POLICY": "turbo"})


class AppWiringTests(unittest.TestCase):
    def test_build_proxy_uses_both_marketplace_adapters(self):
        settings = Settings(
            surplus_api_key="inf_test",
            antseed_base_url="http://127.0.0.1:9999/v1",
            policy="fastest",
            tps_floor="mid",
            balanced_tolerance=0.4,
        )
        proxy = build_proxy(settings)
        self.assertIsInstance(proxy.adapters["surplus"], SurplusAdapter)
        self.assertIsInstance(proxy.adapters["antseed"], AntseedAdapter)
        self.assertEqual(proxy.adapters["surplus"].api_key, "inf_test")
        self.assertEqual(proxy.adapters["antseed"].min_reputation, 50.0)
        self.assertEqual(proxy.config.policy, "fastest")
        self.assertEqual(proxy.config.tps_floor, "mid")
        self.assertEqual(proxy.config.balanced_tolerance, 0.4)


if __name__ == "__main__":
    unittest.main()
