"""LLMUX provider registration.

Registers one Hermes provider whose base URL points at the local LLMUX routing
proxy. The proxy owns marketplace adapters and per-request routing; this module
is only the Hermes seam.

The proxy listens on 127.0.0.1:8378 by default. It is not started by this
registration and does not perform network I/O at import time.
"""

from providers import register_provider
from providers.base import ProviderProfile

LLMUX_PROXY_BASE_URL = "http://127.0.0.1:8378/v1"

llmux = ProviderProfile(
    name="llmux",
    display_name="LLMUX",
    base_url=LLMUX_PROXY_BASE_URL,
    env_vars=(),
    supports_vision=True,
)

register_provider(llmux)
