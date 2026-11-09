"""Per-provider Surplus offers: named sellers, prices and measured speed.

The catalog payload carries one aggregated best offer per model, which a seller
card can only label "marketplace". Surplus also publishes the full offer book
(``/api/markets/<model>``) and trailing-24h per-provider performance
(``/api/analytics/performance/<model>``). Joining them yields one named route
per provider, each pinnable with the documented ``provider_order`` request
field, so a user can choose a specific Surplus seller by measured speed instead
of accepting whatever the marketplace router picks.

Speed figures with too few samples are dropped to ``None`` rather than shown:
a provider with two requests must not win a latency ranking on noise.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Callable
from urllib.parse import quote

from . import __version__
from .models import Offer

MICRO = 1_000_000
DEFAULT_BASE_URL = "https://api.surplusintelligence.ai"
DEFAULT_MIN_TPS_SAMPLES = 10

Opener = Callable[[str, float], Any]


def default_opener(url: str, timeout: float) -> Any:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"llmux/{__version__}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https host
        return json.loads(response.read())


def _dedupe_by_provider(offers: list[dict]) -> dict[str, dict]:
    """Keep one offer per provider: servable first, then cheapest blended price."""

    def sort_key(offer: dict) -> tuple[bool, float]:
        servable = bool(offer.get("healthy") and offer.get("available"))
        blended = 0.8 * float(offer.get("price_input_per_1m") or 0) + 0.2 * float(
            offer.get("price_output_per_1m") or 0
        )
        return (not servable, blended)

    best: dict[str, dict] = {}
    for offer in offers:
        provider_id = offer.get("provider_id")
        if not provider_id:
            continue
        current = best.get(provider_id)
        if current is None or sort_key(offer) < sort_key(current):
            best[provider_id] = offer
    return best


def join_offers(
    markets_payload: dict | None,
    performance_payload: dict | None,
    *,
    model_id: str,
    min_tps_samples: int = DEFAULT_MIN_TPS_SAMPLES,
) -> list[Offer]:
    """Join the offer book with per-provider performance into routable offers."""

    performance = {
        row.get("provider"): row
        for row in (performance_payload or {}).get("providers") or []
        if isinstance(row, dict) and row.get("provider")
    }
    offers: list[Offer] = []
    for provider_id, offer in _dedupe_by_provider(
        list((markets_payload or {}).get("offers") or [])
    ).items():
        if not (offer.get("healthy") and offer.get("available")):
            continue
        stats = performance.get(provider_id) or {}
        samples = int(stats.get("tps_samples") or 0)
        tps = stats.get("tps_p50")
        if tps is not None and samples < min_tps_samples:
            tps = None
        ttft = stats.get("ttft_p50_ms")
        if ttft is not None and int(stats.get("ttft_samples") or 0) < min_tps_samples:
            ttft = None
        offers.append(
            Offer(
                source="surplus",
                upstream_model=model_id,
                input_per_m=float(offer.get("price_input_per_1m") or 0) / MICRO,
                output_per_m=float(offer.get("price_output_per_1m") or 0) / MICRO,
                tps_p50=float(tps) if tps is not None else None,
                ttft_p50_ms=float(ttft) if ttft is not None else None,
                samples=samples,
                healthy=True,
                seller=str(offer.get("route_label") or offer.get("provider") or provider_id),
                provider_id=str(provider_id),
                trusted=offer.get("trusted") if isinstance(offer.get("trusted"), bool) else None,
                trust_basis=(
                    "Surplus trusted seller"
                    if offer.get("trusted") is True
                    else "not trusted by Surplus"
                    if offer.get("trusted") is False
                    else ""
                ),
            )
        )
    return offers


def fetch_provider_offers(
    model_id: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 20.0,
    opener: Opener = default_opener,
    min_tps_samples: int = DEFAULT_MIN_TPS_SAMPLES,
) -> list[Offer]:
    """Fetch and join both Surplus detail endpoints for one model.

    A missing performance payload still yields priced, named routes: speed is
    reported unknown instead of dropping the provider entirely.
    """

    root = base_url.rstrip("/")
    markets = opener(f"{root}/api/markets/{quote(model_id)}", timeout)
    try:
        performance = opener(f"{root}/api/analytics/performance/{quote(model_id)}", timeout)
    except Exception:
        performance = None
    return join_offers(
        markets if isinstance(markets, dict) else None,
        performance if isinstance(performance, dict) else None,
        model_id=model_id,
        min_tps_samples=min_tps_samples,
    )
