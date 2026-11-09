"""Composition root: settings become a ready routing proxy."""

from __future__ import annotations

import json
import logging
import os

from .adapters import AntseedAdapter, SolidAdapter, SurplusAdapter
from .config import Settings
from .ledger import Ledger
from .livecache import Snapshot
from .proxy import ProxyConfig, ProxyServer
from .rewards import load_table


def build_proxy(settings: Settings) -> ProxyServer:
    surplus = SurplusAdapter(
        base_url=settings.surplus_base_url,
        api_key=settings.surplus_api_key,
        timeout=min(settings.upstream_timeout_seconds, 30.0),
    )
    antseed = AntseedAdapter(
        base_url=settings.antseed_base_url,
        min_reputation=settings.antseed_min_reputation,
        timeout=min(settings.upstream_timeout_seconds, 30.0),
    )
    adapters = {"surplus": surplus, "antseed": antseed}
    if settings.solid_api_key:
        # Solid's /models needs the key, so without one the source is left out
        # entirely instead of reporting a catalog error on every refresh.
        adapters["solid"] = SolidAdapter(
            base_url=settings.solid_base_url,
            api_key=settings.solid_api_key,
            timeout=min(settings.upstream_timeout_seconds, 30.0),
            prices=_solid_price_overrides(),
        )
    return ProxyServer(
        adapters=adapters,
        config=ProxyConfig(
            policy=settings.policy,
            tps_floor=settings.tps_floor,
            balanced_tolerance=settings.balanced_tolerance,
            catalog_ttl_seconds=settings.catalog_ttl_seconds,
            upstream_timeout_seconds=settings.upstream_timeout_seconds,
            allow_remote=settings.allow_remote,
        ),
        ledger=Ledger() if settings.track_usage else None,
        snapshot=Snapshot(),
        rewards=load_table(),
    )


def _solid_price_overrides() -> dict | None:
    """Optional LLMUX_SOLID_PRICES_JSON file overriding the dated price table."""

    path = os.environ.get("LLMUX_SOLID_PRICES_JSON")
    if not path:
        return None
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError) as exc:
        logging.getLogger("llmux").warning("ignoring LLMUX_SOLID_PRICES_JSON: %s", exc)
        return None
    return data if isinstance(data, dict) else None
