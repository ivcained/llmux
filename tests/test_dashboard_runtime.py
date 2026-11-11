"""Exercise the backend as Hermes loads it: standalone, with actual HTTP routing."""
import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("LLMUX_AUTOSTART", "0")

class DashboardRuntimeTests(unittest.TestCase):
    def test_standalone_backend_serves_options(self):
        path = Path(__file__).resolve().parents[1] / 'dashboard' / 'plugin_api.py'
        spec = importlib.util.spec_from_file_location('llmux_runtime_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        app = FastAPI()
        app.include_router(module.router, prefix='/api/plugins/llmux')
        with patch.object(module, 'verify_proxy_identity'), patch.dict(os.environ, {'SURPLUS_API_KEY': 'test-source-key'}), patch('urllib.request.urlopen') as request:
            request.return_value.__enter__.return_value.read.return_value = json.dumps({'data': [{'source': 'surplus', 'seller': 'marketplace'}]}).encode()
            response = TestClient(app).get('/api/plugins/llmux/seller-options?model=kimi-k3.1-pro')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data'][0]['source'], 'surplus')

    def test_backend_sends_profile_capability_instead_of_upstream_key(self):
        from agent.secret_scope import reset_secret_scope, set_secret_scope
        from llmux_core.config import load_settings
        from llmux_core.credentials import local_headers
        path = Path(__file__).resolve().parents[1] / 'dashboard' / 'plugin_api.py'
        spec = importlib.util.spec_from_file_location('llmux_runtime_auth', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        token = set_secret_scope({"SURPLUS_API_KEY": "test-source-private-key"})
        try:
            expected = local_headers(load_settings())["Authorization"]
            with patch.object(module, 'ensure_proxy'), patch.object(module, 'verify_proxy_identity'), patch('urllib.request.urlopen') as request:
                request.return_value.__enter__.return_value.read.return_value = b'{"data": []}'
                module._read('/llmux/savings')
            outgoing = request.call_args.args[0]
            self.assertIsInstance(outgoing, __import__('urllib.request', fromlist=['Request']).Request)
            self.assertEqual(outgoing.get_header('Authorization'), expected)
            self.assertNotIn('test-source-private-key', expected)
        finally:
            reset_secret_scope(token)

    def test_backend_ensures_proxy_before_reading(self):
        path = Path(__file__).resolve().parents[1] / 'dashboard' / 'plugin_api.py'
        spec = importlib.util.spec_from_file_location('llmux_runtime_ensure', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        app = FastAPI()
        app.include_router(module.router, prefix='/api/plugins/llmux')
        calls = []
        with patch.object(module, 'ensure_proxy', side_effect=lambda: calls.append('ensure')), \
                patch.object(module, 'verify_proxy_identity'), patch.dict(os.environ, {'SURPLUS_API_KEY': 'test-source-key'}), \
                patch('urllib.request.urlopen') as request:
            request.return_value.__enter__.return_value.read.return_value = json.dumps({'data': []}).encode()
            TestClient(app).get('/api/plugins/llmux/seller-options?model=x')
            calls.append('read')
        self.assertEqual(calls, ['ensure', 'read'])

if __name__ == '__main__':
    unittest.main()
