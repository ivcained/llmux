"""Read the running LLMUX proxy from the plugin's dashboard backend.

Kept apart from the proxy so the route can be tested without a live server:
a read failure returns an empty list with an error record the card labels
"proxy offline", not a 500 the page has to translate. Before reading, the
proxy is ensured running (spawn-on-demand), so a gateway restart or a crashed
proxy recovers on the next card open instead of showing a manual command.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote

_BACKEND_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _BACKEND_DIR.parent


def proxy_port() -> int:
    try:
        return int(os.environ.get("LLMUX_PORT", "8378"))
    except ValueError:
        return 8378


def ensure_proxy() -> None:
    """Spawn the proxy child when nothing serves the port. Never raises."""
    try:
        if str(_PLUGIN_DIR) not in sys.path:
            sys.path.insert(0, str(_PLUGIN_DIR))
        from llmux_core.autostart import ensure_proxy_running

        ensure_proxy_running(_PLUGIN_DIR, port=proxy_port())
    except Exception:
        pass


def seller_options_for(model: str, *, base_url: str | None = None) -> list[dict[str, Any]]:
    expected_base = f"http://127.0.0.1:{proxy_port()}"
    if base_url is not None and base_url.rstrip("/") != expected_base:
        raise ValueError("LLMUX private data can only be read from its configured loopback proxy")
    base_url = expected_base
    url = f"{base_url}/llmux/seller-options?model={quote(model)}"
    ensure_proxy()
    try:
        from llmux_core.credentials import local_headers, verify_proxy_identity
        verify_proxy_identity(proxy_port())
        request = urllib.request.Request(url, headers=local_headers())
        with urllib.request.urlopen(request, timeout=35) as response:
            body = json.loads(response.read())
        if isinstance(body, dict) and isinstance(body.get("data"), list):
            return body["data"]
    except Exception:
        pass
    return [
        {
            "source": "llmux",
            "seller": "proxy offline",
            "error": (
                "LLMUX proxy did not start. Check ~/.hermes/logs/llmux-proxy.log "
                "and retry; LLMUX_AUTOSTART=0 disables automatic recovery."
            ),
        }
    ]
