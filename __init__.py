"""LLMUX provider registration.

Registers one Hermes provider whose base URL points at the local LLMUX routing
proxy. The proxy owns marketplace adapters and per-request routing; this module
is only the Hermes seam.

The proxy listens on 127.0.0.1:8378 by default (LLMUX_PORT). Loading this
plugin ensures the proxy is running: a TCP probe decides whether a child
process must be spawned, so a gateway restart recovers without a manual
command. Set LLMUX_AUTOSTART=0 to keep registration side-effect free.
"""

import os
import sys
from pathlib import Path

# The plugin loader does not guarantee the plugin directory is importable as a
# top-level path, so make the sibling package resolvable before importing it.
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from providers import register_provider
from providers.base import ProviderProfile

from llmux_core.autostart import DEFAULT_PORT, ensure_proxy_running

LLMUX_PROXY_BASE_URL = "http://127.0.0.1:8378/v1"

class LlmuxProvider(ProviderProfile):
    def resolve_api_key_secret(self):
        from llmux_core.config import load_settings
        from llmux_core.credentials import proxy_token, verify_proxy_identity

        settings = load_settings()
        token = proxy_token(settings)
        ensure_proxy_running(plugin_dir(), port=settings.port)
        verify_proxy_identity(settings.port, settings)
        return token, "llmux:profile-scoped"

    def seed_pool_credentials(self, *, suppressed_sources, explicitly_configured):
        # Source API keys must never become the loopback client's pooled bearer.
        return []

    def runtime_base_url(self, *, api_key, default, env_url):
        # Never send the local capability to a caller-supplied remote endpoint.
        return f"http://127.0.0.1:{proxy_port()}/v1"


llmux = LlmuxProvider(
    name="llmux",
    display_name="LLMUX",
    base_url=LLMUX_PROXY_BASE_URL,
    env_vars=("SURPLUS_API_KEY",),
    ambient_credentials=True,
    supports_vision=True,
)

register_provider(llmux)


def plugin_dir() -> Path:
    return Path(__file__).resolve().parent


def proxy_port() -> int:
    try:
        return int(os.environ.get("LLMUX_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def ensure_proxy_autostart() -> None:
    """Start the routing proxy child when nothing serves the port.

    Never raises: a failed autostart must not break provider registration.
    The dashboard backend re-attempts on demand, so a transient failure here
    is recoverable without a gateway restart.
    """
    try:
        ensure_proxy_running(plugin_dir(), port=proxy_port())
    except Exception:
        pass


ensure_proxy_autostart()
