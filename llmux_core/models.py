"""Normalized marketplace catalog and routing data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Offer:
    """One routable offer for a model."""

    source: str
    upstream_model: str
    input_per_m: float
    output_per_m: float
    cached_input_per_m: float | None = None
    tps_p50: float | None = None
    ttft_p50_ms: float | None = None
    samples: int = 0
    healthy: bool = True
    reputation: float | None = None
    seller: str = ""
    peer_id: str | None = None


@dataclass(frozen=True)
class ModelRecord:
    """A canonical model with offers from every marketplace that serves it."""

    id: str
    name: str
    offers: tuple[Offer, ...] = ()
    aliases: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelDirective:
    """Routing instructions parsed from a user-facing model id."""

    model: str
    source: str | None = None
    policy: str | None = None
    peer_id: str | None = None


@dataclass(frozen=True)
class RouteDecision:
    """The selected upstream and an explainable routing receipt."""

    requested_model: str
    upstream_model: str
    offer: Offer
    policy: str
    applied_policy: str
    floor_relaxed: bool = False
    reason: str = ""
