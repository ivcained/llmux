"""Marketplace adapters: the only modules that speak upstream wire formats."""

from __future__ import annotations

import json
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from . import __version__, surplus_detail
from .catalog import canonical_model_id
from .models import ModelRecord, Offer

SURPLUS_DEFAULT_BASE_URL = "https://api.surplusintelligence.ai/v1"
ANTSEED_DEFAULT_BASE_URL = "http://127.0.0.1:8377/v1"
SOLID_DEFAULT_BASE_URL = "https://accounts.solid.xyz/accounts/v1/ai/v1"

# Solid publishes no pricing or speed API: its /models lists ids only. These
# per-million prices and "less than official" percentages are copied from the
# Solid dashboard (Models & prices) and are dated so a stale table is visible.
# Override any row with LLMUX_SOLID_PRICES_JSON (same shape) when they change.
SOLID_PRICES_AS_OF = "2026-11-09"
SOLID_PRICES = {
    "deepseek/deepseek-v4.1-flash": {"in": 0.0, "cached": 0.0, "out": 0.0, "off_pct": None},
    "claude-opus-5-5": {"in": 0.38, "cached": 0.27, "out": 1.20, "off_pct": 92},
    "gpt-6.1-sol": {"in": 0.14, "cached": 0.007, "out": 0.66, "off_pct": 93},
    "kimi-k3": {"in": 0.16, "cached": 0.080, "out": 0.60, "off_pct": 95},
    "claude-sonnet-5-5": {"in": 0.34, "cached": 0.24, "out": 1.02, "off_pct": 87},
    "glm-5.3": {"in": 0.008, "cached": 0.007, "out": 0.44, "off_pct": 94},
}


class CatalogSchemaError(ValueError):
    """The marketplace returned a shape LLMUX does not understand."""


def _get_json(url: str, timeout: float = 15.0, api_key: str | None = None) -> Any:
    headers = {"Accept": "application/json", "User-Agent": f"llmux/{__version__}"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


class SurplusAdapter:
    """Surplus Intelligence catalog and live marketplace offers."""

    source = "surplus"

    def __init__(
        self,
        base_url: str = SURPLUS_DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    @property
    def api_base(self) -> str:
        """Origin of the API host, without the ``/v1`` inference path."""

        parts = urlsplit(self.base_url)
        return f"{parts.scheme}://{parts.netloc}"

    def provider_offers(self, model_id: str) -> list[Offer]:
        """Named per-provider offers for one model, joined with measured speed."""

        return surplus_detail.fetch_provider_offers(
            model_id, base_url=self.api_base, timeout=self.timeout
        )

    @staticmethod
    def build_extra_body(decision: Any) -> dict:
        """Pin the request to a chosen Surplus provider when one was selected."""

        provider_id = getattr(decision.offer, "provider_id", None)
        return {"provider_order": [provider_id]} if provider_id else {}

    @staticmethod
    def parse_catalog(models_payload: dict, marketplace_payload: dict) -> list[ModelRecord]:
        if not isinstance(models_payload, dict) or not isinstance(
            models_payload.get("data"), list
        ):
            raise CatalogSchemaError("Surplus models payload has no data list")
        market_root = marketplace_payload.get("data", marketplace_payload)
        if not isinstance(market_root, dict) or not isinstance(
            market_root.get("markets"), list
        ):
            raise CatalogSchemaError("Surplus marketplace payload has no markets list")
        markets = market_root["markets"]
        by_model = {row.get("model"): row for row in markets if isinstance(row, dict)}
        records: list[ModelRecord] = []
        for model in models_payload.get("data", []):
            model_id = model.get("id")
            if not model_id:
                continue
            architecture = model.get("architecture") or {}
            outputs = architecture.get("output_modalities") or []
            if outputs and "text" not in outputs:
                continue
            pricing = model.get("pricing") or {}
            direct_in = float(pricing.get("prompt") or 0) * 1_000_000
            direct_out = float(pricing.get("completion") or 0) * 1_000_000
            market = by_model.get(model_id)
            offers: list[Offer] = []
            list_input = list_output = None
            if market:
                input_price = (
                    (market.get("best_input_per_1m") or 0) / 1_000_000 or direct_in
                )
                output_price = (
                    (market.get("best_output_per_1m") or 0) / 1_000_000 or direct_out
                )
                cached = market.get("best_cache_read_per_1m")
                direct_in_field = market.get("direct_input_per_1m")
                direct_out_field = market.get("direct_output_per_1m")
                if direct_in_field or direct_out_field:
                    list_input = direct_in_field / 1_000_000 if direct_in_field else None
                    list_output = direct_out_field / 1_000_000 if direct_out_field else None
                offers.append(
                    Offer(
                        source=SurplusAdapter.source,
                        upstream_model=model_id,
                        input_per_m=float(input_price),
                        output_per_m=float(output_price),
                        cached_input_per_m=(float(cached) / 1_000_000 if cached else None),
                        tps_p50=market.get("tps_p50"),
                        ttft_p50_ms=market.get("ttft_p50_ms"),
                        samples=int(market.get("tps_samples") or 0),
                        healthy=int(market.get("healthy_seller_count") or 0) > 0,
                        seller="Surplus auto",
                    )
                )
            if list_input is None and direct_in:
                list_input = direct_in
            if list_output is None and direct_out:
                list_output = direct_out
            metadata: dict = {"source": "surplus"}
            if list_input is not None:
                metadata["list_input_per_m"] = float(list_input)
            if list_output is not None:
                metadata["list_output_per_m"] = float(list_output)
            records.append(
                ModelRecord(
                    id=canonical_model_id(model_id),
                    name=model.get("name") or model_id,
                    offers=tuple(offers),
                    aliases=(model_id,),
                    metadata={
                        **metadata,
                        "context_length": model.get("context_length"),
                        "supported_features": model.get("supported_features", []),
                    },
                )
            )
        return records

    def fetch_catalog(self) -> list[ModelRecord]:
        models = _get_json(f"{self.base_url}/models", self.timeout)
        parts = urlsplit(self.base_url)
        marketplace_url = f"{parts.scheme}://{parts.netloc}/api/marketplace"
        marketplace = _get_json(marketplace_url, self.timeout)
        return self.parse_catalog(models, marketplace)


class SolidAdapter:
    """Solid AI API: one OpenAI-compatible key, a fixed discounted price table.

    Model ids come live from ``/models`` (so a removed model disappears), prices
    from the dated dashboard table above. Solid reports no speed figures, so
    TPS and first-token stay ``None`` and render as unknown, never guessed.
    """

    source = "solid"

    def __init__(
        self,
        base_url: str = SOLID_DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout: float = 15.0,
        prices: dict | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.prices = {**SOLID_PRICES, **(prices or {})}

    @staticmethod
    def parse_catalog(payload: dict, prices: dict | None = None) -> list[ModelRecord]:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise CatalogSchemaError("Solid models payload has no data list")
        table = prices if prices is not None else SOLID_PRICES
        records: list[ModelRecord] = []
        for model in payload["data"]:
            model_id = model.get("id") if isinstance(model, dict) else None
            if not model_id:
                continue
            price = table.get(model_id)
            if price is None:
                # Listed by Solid but not in the price table: routable only
                # once its price is known, so it is skipped rather than shown
                # as free.
                continue
            input_per_m = float(price["in"])
            output_per_m = float(price["out"])
            metadata: dict = {"source": "solid", "prices_as_of": SOLID_PRICES_AS_OF}
            off = price.get("off_pct")
            if isinstance(off, (int, float)) and 0 < off < 100:
                metadata["list_input_per_m"] = input_per_m / (1 - off / 100)
                metadata["list_output_per_m"] = output_per_m / (1 - off / 100)
            records.append(
                ModelRecord(
                    id=canonical_model_id(model_id.rsplit("/", 1)[-1]),
                    name=model.get("display_name") or model_id,
                    offers=(
                        Offer(
                            source=SolidAdapter.source,
                            upstream_model=model_id,
                            input_per_m=input_per_m,
                            output_per_m=output_per_m,
                            cached_input_per_m=price.get("cached"),
                            seller="Solid",
                        ),
                    ),
                    aliases=(model_id,),
                    metadata=metadata,
                )
            )
        return records

    def fetch_catalog(self) -> list[ModelRecord]:
        payload = _get_json(f"{self.base_url}/models", self.timeout, api_key=self.api_key)
        return self.parse_catalog(payload, self.prices)


class AntseedAdapter:
    """Antseed local buyer catalog with per-seller offers."""

    source = "antseed"

    def __init__(
        self,
        base_url: str = ANTSEED_DEFAULT_BASE_URL,
        min_reputation: float = 50.0,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_reputation = min_reputation
        self.timeout = timeout

    @staticmethod
    def parse_catalog(
        payload: dict, *, min_reputation: float = 50.0
    ) -> list[ModelRecord]:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise CatalogSchemaError("Antseed catalog payload has no data list")
        records: list[ModelRecord] = []
        for model in payload["data"]:
            model_id = model.get("id")
            if not model_id or model.get("type") not in (None, "text"):
                continue
            offers: list[Offer] = []
            for peer in model.get("peers", []):
                reputation = float(peer.get("effectiveReputationScore") or 0)
                # Peers under the reputation bar stay listed (labelled
                # untrusted) instead of vanishing; routing skips them.
                trusted = reputation >= min_reputation
                health = peer.get("health") or {}
                status = health.get("status")
                performance = peer.get("performance") or {}
                peer_id = peer.get("peerId")
                service_id = peer.get("serviceId") or model_id
                upstream_model = f"{peer_id}@{service_id}" if peer_id else service_id
                offers.append(
                    Offer(
                        source=AntseedAdapter.source,
                        upstream_model=upstream_model,
                        input_per_m=float(peer.get("inputUsdPerMillion") or 0),
                        output_per_m=float(peer.get("outputUsdPerMillion") or 0),
                        cached_input_per_m=peer.get("cachedInputUsdPerMillion"),
                        tps_p50=performance.get("tokensPerSecondP50"),
                        ttft_p50_ms=performance.get("firstTokenMsP50"),
                        samples=int(performance.get("requests") or 0),
                        healthy=status in (None, "healthy"),
                        reputation=reputation,
                        seller=peer.get("displayName") or "",
                        peer_id=peer_id,
                        trusted=trusted,
                        trust_basis=(
                            f"Antseed reputation {reputation:.0f}"
                            + ("" if trusted else f" (below {min_reputation:.0f})")
                        ),
                    )
                )
            aliases = tuple(dict.fromkeys([model_id, *model.get("aliases", [])]))
            records.append(
                ModelRecord(
                    id=canonical_model_id(model_id),
                    name=model.get("name") or model_id,
                    offers=tuple(offers),
                    aliases=aliases,
                    metadata={"source": "antseed"},
                )
            )
        return records

    def fetch_catalog(self) -> list[ModelRecord]:
        payload = _get_json(f"{self.base_url}/models?type=text", self.timeout)
        return self.parse_catalog(payload, min_reputation=self.min_reputation)

    def build_pin(self, model_id: str, peer_id: str | None) -> str:
        """Return the model id that forces Antseed's buyer to a chosen seller."""

        if not peer_id:
            return model_id
        return f"{peer_id}@{model_id}"
