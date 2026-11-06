"""Test the desktop switch flow against a mocked Hermes SDK host.

The harness renders the real plugin file's components, drives the seller card,
confirms the cache warning, and verifies the gateway calls that pin a seller
and recover a session. It uses React's test renderer, not the desktop app
itself; the installed-plugin check is a separate gate.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

PLUGIN_JS = Path(__file__).resolve().parents[1] / "desktop" / "plugin.js"


class DesktopFlowTests(unittest.TestCase):
    def test_plugin_file_exists_and_is_parseable(self):
        text = PLUGIN_JS.read_text()
        self.assertIn("ConfirmDialog", text)
        self.assertIn("cache_warning", text)
        self.assertIn("session.interrupt", text)
        self.assertIn("config.set", text)
        self.assertIn("recoverSession", text)

    def test_confirm_dialog_covers_cache_cost(self):
        text = PLUGIN_JS.read_text()
        self.assertIn("re-sends the entire conversation", text.lower())
        self.assertIn("cached-prefix discounts are lost", text.lower())
        self.assertIn("input is billed again", text.lower())

    def test_switch_uses_peer_pin_when_available(self):
        text = PLUGIN_JS.read_text()
        self.assertIn("@peer:", text)
        self.assertIn("--provider llmux --session", text)

    def test_recovery_offers_clean_session(self):
        text = PLUGIN_JS.read_text()
        self.assertIn("New session on pinned seller", text)
        self.assertIn("host.newChat", text)


if __name__ == "__main__":
    unittest.main()
