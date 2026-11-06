"""Exercise the backend as Hermes loads it: standalone, with actual HTTP routing."""
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

class DashboardRuntimeTests(unittest.TestCase):
    def test_standalone_backend_serves_options(self):
        path = Path(__file__).resolve().parents[1] / 'dashboard' / 'plugin_api.py'
        spec = importlib.util.spec_from_file_location('llmux_runtime_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        app = FastAPI()
        app.include_router(module.router, prefix='/api/plugins/llmux')
        with patch('urllib.request.urlopen') as request:
            request.return_value.__enter__.return_value.read.return_value = json.dumps({'data': [{'source': 'surplus', 'seller': 'marketplace'}]}).encode()
            response = TestClient(app).get('/api/plugins/llmux/seller-options?model=kimi-k3.1-pro')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data'][0]['source'], 'surplus')

if __name__ == '__main__':
    unittest.main()
