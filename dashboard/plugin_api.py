"""Hermes-mounted backend for the LLMUX desktop card.

Only read-only requests to the fixed loopback proxy are permitted here. No
credential, wallet, inference, or session mutation is handled by this bridge.
Synchronous routes run in FastAPI's worker pool, not its async event loop.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()
PROXY = "http://127.0.0.1:8378"


def routes() -> list[str]:
    return ["/seller-options", "/validate-route"]


def _read(path: str) -> dict:
    try:
        with urllib.request.urlopen(PROXY + path, timeout=35) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=exc.code, detail="LLMUX rejected the requested model or seller.") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="LLMUX proxy is unavailable. Start python -m llmux_core on port 8378.") from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="Unexpected LLMUX proxy response.")
    return result


@router.get("/seller-options")
def seller_options(model: str) -> dict:
    return _read("/llmux/seller-options?" + urllib.parse.urlencode({"model": model}))


class ValidateRoute(BaseModel):
    model: str = Field(min_length=1, max_length=512)


@router.post("/validate-route")
def validate_route(body: ValidateRoute) -> dict:
    return _read("/llmux/plan?" + urllib.parse.urlencode({"model": body.model}))
