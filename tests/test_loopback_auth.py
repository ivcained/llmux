"""The local spend-capable proxy rejects callers from other profile scopes."""

import json
import unittest
import urllib.error
import urllib.request

from llmux_core.proxy import ProxyConfig, ProxyServer


class LoopbackAuthTests(unittest.TestCase):
    def setUp(self):
        self.proxy = ProxyServer({}, ProxyConfig())
        # Set after construction so RED exercises the missing HTTP behavior.
        object.__setattr__(self.proxy.config, "auth_token", "profile-a-token")
        self.port = self.proxy.start(port=0)
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.proxy.stop()

    def request(self, path, token=None, body=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        data = json.dumps(body).encode() if body is not None else None
        return urllib.request.urlopen(urllib.request.Request(self.base + path, data=data, headers=headers), timeout=3)

    def test_missing_or_wrong_scope_cannot_read_private_usage(self):
        for token in (None, "profile-b-token"):
            with self.subTest(token=token), self.assertRaises(urllib.error.HTTPError) as caught:
                self.request("/llmux/savings", token)
            self.assertEqual(caught.exception.code, 401)
            caught.exception.close()

    def test_unauthorized_post_is_rejected_before_parsing_or_routing(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/v1/chat/completions", "profile-b-token", {"model": "not-in-catalog"})
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()

    def test_non_ascii_bearer_is_rejected_with_401(self):
        for body in (None, {"model": "not-in-catalog"}):
            with self.subTest(method="GET" if body is None else "POST"):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.request("/v1/chat/completions", "invalid-\u00e9", body)
                self.assertEqual(caught.exception.code, 401)
                caught.exception.close()

    def test_authenticated_reply_uses_upstream_key_not_local_bearer(self):
        from test_proxy import FakeAdapter, MockUpstream, record
        upstream = MockUpstream(source="surplus")
        adapter = FakeAdapter("surplus", upstream.base_url, [record()])
        adapter.api_key = "test-upstream-key"
        self.proxy.adapters = {"surplus": adapter}
        try:
            with self.request("/v1/chat/completions", "profile-a-token", {
                "model": "kimi-k3.1-pro@surplus", "messages": [{"role": "user", "content": "local synthetic test"}]
            }) as response:
                self.assertEqual(json.load(response)["choices"][0]["message"]["content"], "surplus")
            self.assertEqual(upstream.requests[0]["headers"]["Authorization"], "Bearer test-upstream-key")
            self.assertNotIn("profile-a-token", str(upstream.requests))
        finally:
            upstream.close()

    def test_actual_identity_probe_rejects_foreign_profile_without_inference(self):
        from dataclasses import replace
        from llmux_core.config import Settings
        from llmux_core.credentials import proxy_token, verify_proxy_identity
        settings = Settings(surplus_api_key="profile-a-secret")
        self.proxy.config = replace(self.proxy.config, auth_token=proxy_token(settings))
        verify_proxy_identity(self.port, settings)
        with self.assertRaisesRegex(RuntimeError, "authenticate"):
            verify_proxy_identity(self.port, Settings(surplus_api_key="profile-b-secret"))

    def test_authenticated_identity_probe_requires_matching_token(self):
        with self.request("/llmux/authcheck", "profile-a-token") as response:
            self.assertEqual(json.load(response), {"ok": True, "service": "llmux", "auth": "profile-scoped-v1"})


if __name__ == "__main__":
    unittest.main()
