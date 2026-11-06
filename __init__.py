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
from pathlib import Path

from providers import register_provider
from providers.base import ProviderProfile

from llmux_core.autostart import DEFAULT_PORT, ensure_proxy_running

LLMUX_PROXY_BASE_URL = "http://127.0.0.1:8378/v1"

llmux = ProviderProfile(
    name="llmux",
    display_name="LLMUX",
    base_url=LLMUX_PROXY_BASE_URL,
    env_vars=(),
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
