"""Hermes-mounted backend for the LLMUX desktop card.

Only read-only requests to the fixed loopback proxy are permitted here. No
credential, wallet, inference, or session mutation is handled by this bridge.
Synchronous routes run in FastAPI's worker pool, not its async event loop.

Before each read the proxy is ensured running (spawn-on-demand via
llmux_core.autostart), so a gateway restart or crashed proxy recovers on the
next card open instead of surfacing a manual start command.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()
_PLUGIN_DIR = Path(__file__).resolve().parents[1]


def proxy_port() -> int:
    try:
        return int(os.environ.get("LLMUX_PORT", "8378"))
    except ValueError:
        return 8378


def proxy_base() -> str:
    return f"http://127.0.0.1:{proxy_port()}"


def ensure_proxy() -> None:
    """Spawn the proxy child when nothing serves the port. Never raises."""
    try:
        if str(_PLUGIN_DIR) not in sys.path:
            sys.path.insert(0, str(_PLUGIN_DIR))
        from llmux_core.autostart import ensure_proxy_running

        ensure_proxy_running(_PLUGIN_DIR, port=proxy_port())
    except Exception:
        pass


def routes() -> list[str]:
    return ["/seller-options", "/similar-models", "/validate-route"]


def _read(path: str) -> dict:
    ensure_proxy()
    try:
        with urllib.request.urlopen(proxy_base() + path, timeout=35) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=exc.code, detail="LLMUX rejected the requested model or seller.") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "LLMUX proxy did not start. Check ~/.hermes/logs/llmux-proxy.log and retry; "
                "LLMUX_AUTOSTART=0 disables automatic recovery."
            ),
        ) from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="Unexpected LLMUX proxy response.")
    return result


@router.get("/seller-options")
def seller_options(model: str) -> dict:
    return _read("/llmux/seller-options?" + urllib.parse.urlencode({"model": model}))


@router.get("/similar-models")
def similar_models(model: str) -> dict:
    return _read("/llmux/similar-models?" + urllib.parse.urlencode({"model": model}))


class ValidateRoute(BaseModel):
    model: str = Field(min_length=1, max_length=512)


@router.post("/validate-route")
def validate_route(body: ValidateRoute) -> dict:
    return _read("/llmux/plan?" + urllib.parse.urlencode({"model": body.model}))
