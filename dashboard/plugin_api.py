"""Dashboard plugin manifest: registers LLMUX's seller-options backend.

The desktop half calls ``ctx.rest('/seller-options?model=...')`` which Hermes
routes to this file's FastAPI router at ``/api/plugins/llmux``. The router is
a thin wrapper over the same proxy method the /v1 endpoint uses, so a card and
an agent tool can never disagree about which seller a route uses.
"""

from __future__ import annotations

from typing import Any

try:
    from fastapi import APIRouter
except ImportError:  # the host's own tests import without FastAPI
    APIRouter = None

router = APIRouter() if APIRouter is not None else None


def routes() -> list[str]:
    return ["/seller-options"]


if router is not None:

    @router.get("/seller-options")
    async def seller_options(model: str) -> dict[str, Any]:
        import proxy_reader

        rows = proxy_reader.seller_options_for(model)
        return {"object": "list", "data": rows}
