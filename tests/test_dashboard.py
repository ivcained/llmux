import importlib.util
import sys
import unittest
from pathlib import Path


def _load_api():
    path = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("llmux_dashboard_api", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["llmux_dashboard_api"] = module
    spec.loader.exec_module(module)
    return module


class DashboardRouteTests(unittest.TestCase):
    def test_route_table_exposes_seller_options(self):
        api = _load_api()
        self.assertIn("/seller-options", api.routes())


if __name__ == "__main__":
    unittest.main()
