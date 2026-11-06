"""Provider registration contract tests for LLMUX."""

import os
import subprocess
import sys
import unittest
from pathlib import Path
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


class LoaderImportTests(unittest.TestCase):
    """Regression: the plugin loader does not put the plugin dir on sys.path.

    A top-level `import llmux_core` at module scope broke provider registration
    (doctor: "No module named 'llmux_core'"). Load the module the way the loader
    does: by file path, with only the Hermes root importable.
    """

    def test_module_imports_without_plugin_dir_on_sys_path(self):
        repo = Path(__file__).resolve().parents[1]
        hermes_root = Path.home() / ".hermes" / "hermes-agent"
        env = dict(os.environ)
        env["LLMUX_AUTOSTART"] = "0"
        env["PYTHONPATH"] = str(hermes_root)
        code = (
            "import importlib.util;"
            f"spec = importlib.util.spec_from_file_location('llmux_probe', r'{repo}/__init__.py');"
            "m = importlib.util.module_from_spec(spec);"
            "spec.loader.exec_module(m);"
            "print(m.llmux.name)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("llmux", result.stdout)


if __name__ == "__main__":
    unittest.main()
