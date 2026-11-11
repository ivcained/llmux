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

    def test_provider_resolves_only_profile_local_loopback_capability(self):
        from agent.secret_scope import reset_secret_scope, set_secret_scope
        from llmux_core.config import load_settings
        from llmux_core.credentials import proxy_token
        token = set_secret_scope({"SURPLUS_API_KEY": "test-profile-source-key"})
        try:
            expected = proxy_token(load_settings())
            with mock.patch.object(llmux_module, "ensure_proxy_running"), mock.patch(
                "llmux_core.credentials.verify_proxy_identity"
            ):
                resolved = llmux.resolve_api_key_secret()
            self.assertEqual(resolved[0], expected)
            self.assertNotIn("test-profile-source-key", resolved[0])
        finally:
            reset_secret_scope(token)

    def test_missing_profile_credentials_fail_before_spawn_or_generic_fallback(self):
        from agent.secret_scope import reset_multiplex_context, reset_secret_scope, set_multiplex_context, set_secret_scope
        multiplex = set_multiplex_context(True)
        scope = set_secret_scope({})
        try:
            with mock.patch.dict(os.environ, {"SURPLUS_API_KEY": "foreign-launch-key"}), mock.patch.object(
                llmux_module, "ensure_proxy_running"
            ) as spawn:
                with self.assertRaisesRegex(ValueError, "marketplace credential"):
                    llmux.resolve_api_key_secret()
                spawn.assert_not_called()
        finally:
            reset_secret_scope(scope)
            reset_multiplex_context(multiplex)

    def test_unscoped_multiplex_autostart_does_not_spawn_with_launch_keys(self):
        from agent.secret_scope import reset_multiplex_context, reset_secret_scope, set_multiplex_context, set_secret_scope
        multiplex = set_multiplex_context(True)
        scope = set_secret_scope(None)
        try:
            with mock.patch.dict(os.environ, {"SURPLUS_API_KEY": "foreign-launch-key", "LLMUX_AUTOSTART": "1"}), mock.patch(
                "llmux_core.autostart.port_listening", return_value=False
            ), mock.patch("llmux_core.autostart.subprocess.Popen") as spawn:
                llmux_module.ensure_proxy_autostart()
                spawn.assert_not_called()
        finally:
            reset_secret_scope(scope)
            reset_multiplex_context(multiplex)

    def test_actual_hermes_auth_resolution_returns_profile_capability(self):
        import tempfile
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as home:
            plugins = Path(home) / "plugins/model-providers"
            plugins.mkdir(parents=True)
            (plugins / "llmux").symlink_to(repo, target_is_directory=True)
            env = {**os.environ, "HERMES_HOME": home, "LLMUX_AUTOSTART": "0",
                   "SURPLUS_API_KEY": "test-profile-source-key"}
            code = '''
from unittest.mock import patch
from providers import get_provider_profile
p = get_provider_profile("llmux")
from llmux_core.config import load_settings
from llmux_core.credentials import proxy_token
from hermes_cli.auth import resolve_api_key_provider_credentials
with patch("llmux_core.credentials.verify_proxy_identity"):
    resolved = resolve_api_key_provider_credentials("llmux")
assert resolved["api_key"] == proxy_token(load_settings())
assert resolved["base_url"] == "http://127.0.0.1:8378/v1"
assert p.seed_pool_credentials(suppressed_sources=frozenset(), explicitly_configured=True) == []
print("profile credential resolution verified")
'''
            result = subprocess.run([sys.executable, "-c", code], env=env,
                                    capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("profile credential resolution verified", result.stdout)

    def test_provider_runtime_url_honors_profile_port(self):
        with mock.patch.dict(os.environ, {"LLMUX_PORT": "9999"}):
            self.assertEqual(llmux.runtime_base_url(api_key="local-capability", default="", env_url=""),
                             "http://127.0.0.1:9999/v1")

    def test_registration_declares_credential_without_auto_selecting_router(self):
        self.assertEqual(llmux.env_vars, ("SURPLUS_API_KEY",))
        self.assertTrue(llmux.ambient_credentials)
        self.assertEqual(llmux.seed_pool_credentials(suppressed_sources=frozenset(), explicitly_configured=True), [])


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
