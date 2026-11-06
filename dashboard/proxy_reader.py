"""Read the running LLMUX proxy from the plugin's dashboard backend.

Kept apart from the proxy so the route can be tested without a live server:
a read failure returns an empty list with an error record the card labels
"proxy offline", not a 500 the page has to translate.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote

_BACKEND_DIR = Path(__file__).resolve().parent


def _reader_module():
    """Import the sibling reader from the plugin's dashboard directory."""
    if str(_BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(_BACKEND_DIR))
    import proxy_reader

    return proxy_reader


def seller_options_for(model: str, *, base_url: str = "http://127.0.0.1:8378") -> list[dict[str, Any]]:
    url = f"{base_url}/llmux/seller-options?model={quote(model)}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
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
                "LLMUX proxy is not running. Start it with: "
                "python -m llmux_core --port 8378"
            ),
        }
    ]
