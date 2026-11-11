"""Profile-scoped credentials must never fall back to a launch-profile key."""

import os
import unittest
from unittest import mock

from agent.secret_scope import reset_multiplex_context, reset_secret_scope, set_multiplex_context, set_secret_scope
from llmux_core.config import load_settings


class ScopedCredentialTests(unittest.TestCase):
    def setUp(self):
        self.multiplex_token = set_multiplex_context(True)

    def tearDown(self):
        reset_multiplex_context(self.multiplex_token)

    def test_production_resolution_uses_active_scope_not_process_environment(self):
        token = set_secret_scope({"SURPLUS_API_KEY": "profile-b", "SOLID_API_KEY": "solid-b"})
        try:
            with mock.patch.dict(os.environ, {"SURPLUS_API_KEY": "profile-a", "SOLID_API_KEY": "solid-a"}):
                settings = load_settings()
            self.assertEqual(settings.surplus_api_key, "profile-b")
            self.assertEqual(settings.solid_api_key, "solid-b")
        finally:
            reset_secret_scope(token)

    def test_empty_active_scope_does_not_use_launch_keys_or_read_dotenv(self):
        token = set_secret_scope({})
        try:
            with mock.patch.dict(os.environ, {"SURPLUS_API_KEY": "profile-a", "SOLID_API_KEY": "solid-a"}), mock.patch(
                "pathlib.Path.read_text", side_effect=AssertionError("plugin must not read credential files")
            ):
                settings = load_settings()
            self.assertIsNone(settings.surplus_api_key)
            self.assertIsNone(settings.solid_api_key)
        finally:
            reset_secret_scope(token)


class LoopbackTokenTests(unittest.TestCase):
    def test_profile_home_and_source_keys_are_both_bound_to_local_auth(self):
        from llmux_core.config import Settings
        from llmux_core.credentials import proxy_token
        a = Settings(surplus_api_key="source-a")
        b = Settings(surplus_api_key="source-b")
        self.assertEqual(proxy_token(a, home="/profiles/a"), proxy_token(a, home="/profiles/a"))
        self.assertNotEqual(proxy_token(a, home="/profiles/a"), proxy_token(a, home="/profiles/b"))
        self.assertNotEqual(proxy_token(a, home="/profiles/a"), proxy_token(b, home="/profiles/a"))
        self.assertNotIn("source-a", proxy_token(a, home="/profiles/a"))

    def test_no_secret_never_yields_a_usable_proxy_token(self):
        from llmux_core.config import Settings
        from llmux_core.credentials import proxy_token
        with self.assertRaisesRegex(ValueError, "credential"):
            proxy_token(Settings(), home="/profiles/a")


class ProfileDataPathsTests(unittest.TestCase):
    def test_default_data_paths_follow_current_hermes_home(self):
        from pathlib import Path
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from llmux_core.ledger import Ledger
        from llmux_core.livecache import Snapshot
        token = set_hermes_home_override(Path("/scoped/profile-b"))
        try:
            with mock.patch.dict(os.environ, {"LLMUX_LEDGER_PATH": "", "LLMUX_MARKET_CACHE": ""}):
                self.assertEqual(Ledger().path, Path("/scoped/profile-b/llmux/usage.jsonl"))
                self.assertEqual(Snapshot().path, Path("/scoped/profile-b/llmux/market-cache.json"))
        finally:
            reset_hermes_home_override(token)


if __name__ == "__main__":
    unittest.main()
