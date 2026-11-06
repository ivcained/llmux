"""Provider registration contract tests for LLMUX."""

import unittest

from llmux import LLMUX_PROXY_BASE_URL, llmux


class ProviderRegistrationTests(unittest.TestCase):
    def test_provider_identity(self):
        self.assertEqual(llmux.name, "llmux")
        self.assertEqual(llmux.display_name, "LLMUX")

    def test_provider_targets_local_proxy(self):
        self.assertEqual(llmux.base_url, LLMUX_PROXY_BASE_URL)
        self.assertTrue(LLMUX_PROXY_BASE_URL.startswith("http://127.0.0.1:"))
        self.assertTrue(LLMUX_PROXY_BASE_URL.endswith("/v1"))

    def test_registration_has_no_fixed_credential_requirement(self):
        self.assertEqual(llmux.env_vars, ())


if __name__ == "__main__":
    unittest.main()
