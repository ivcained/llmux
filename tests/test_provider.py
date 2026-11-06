"""Provider registration contract tests for LLMUX."""

import os
import unittest
from unittest import mock

# Keep test collection hermetic: importing the plugin package must not spawn
# a real proxy child. The autostart decision logic itself is covered by
# tests/test_autostart.py with mocked subprocesses.
os.environ.setdefault("LLMUX_AUTOSTART", "0")

import llmux as llmux_module  # noqa: E402
from llmux import LLMUX_PROXY_BASE_URL, llmux  # noqa: E402


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


class ProxyAutostartTests(unittest.TestCase):
    def test_autostart_uses_plugin_dir_and_port(self):
        with mock.patch.object(llmux_module, "ensure_proxy_running") as ensure, \
                mock.patch.dict(os.environ, {"LLMUX_PORT": "9999"}):
            llmux_module.ensure_proxy_autostart()
        args, kwargs = ensure.call_args
        self.assertEqual(args[0], llmux_module.plugin_dir())
        self.assertEqual(kwargs["port"], 9999)

    def test_invalid_port_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"LLMUX_PORT": "not-a-port"}):
            self.assertEqual(llmux_module.proxy_port(), 8378)

    def test_autostart_failure_never_raises(self):
        with mock.patch.object(llmux_module, "ensure_proxy_running", side_effect=RuntimeError("boom")):
            llmux_module.ensure_proxy_autostart()  # must not raise


if __name__ == "__main__":
    unittest.main()
