"""Model directive parsing and deterministic route selection."""

from __future__ import annotations

from .models import ModelDirective, ModelRecord, Offer, RouteDecision

SOURCE_ALIASES = {
    "surplus": "surplus",
    "surp": "surplus",
    "antseed": "antseed",
    "ant": "antseed",
}
POLICY_ALIASES = {
    "cheap": "cheapest",
    "cheapest": "cheapest",
    "fast": "fastest",
    "fastest": "fastest",
    "bal": "balanced",
    "balanced": "balanced",
}
TPS_FLOORS = {"low": 10.0, "mid": 25.0, "high": 60.0}


class NoRouteError(LookupError):
    """No healthy offer satisfies the request."""


def parse_model_directive(value: str) -> ModelDirective:
    """Parse ``model[@source][@policy]`` without touching upstream peer pins."""

    parts = value.strip().split("@")
    model = parts[0].strip()
    if not model:
        raise ValueError("model is required")
    source = None
    policy = None
    for raw in parts[1:]:
        suffix = raw.strip().lower()
        if suffix in SOURCE_ALIASES:
            source = SOURCE_ALIASES[suffix]
        elif suffix in POLICY_ALIASES:
            policy = POLICY_ALIASES[suffix]
        else:
            raise ValueError(f"unknown LLMUX model directive: @{raw}")
    return ModelDirective(model=model, source=source, policy=policy)


def blended_price(offer: Offer) -> float:
    """Price score using a 4:1 input-to-output token mix."""

    return 0.8 * offer.input_per_m + 0.2 * offer.output_per_m


def _cheapest(candidates: list[Offer]) -> Offer:
    return min(candidates, key=lambda offer: (blended_price(offer), offer.source))


def _fastest(candidates: list[Offer]) -> Offer:
    measured = [
        offer
        for offer in candidates
        if offer.tps_p50 is not None or offer.ttft_p50_ms is not None
    ]
    if not measured:
        return _cheapest(candidates)
    max_tps = max((offer.tps_p50 or 0.0) for offer in measured) or 1.0
    min_ttft = min(
        (offer.ttft_p50_ms for offer in measured if offer.ttft_p50_ms is not None),
        default=None,
    )

    def speed_score(offer: Offer) -> float:
        tps_component = (offer.tps_p50 or 0.0) / max_tps
        if offer.ttft_p50_ms is None or min_ttft is None:
            ttft_component = 0.0
        else:
            ttft_component = min_ttft / max(offer.ttft_p50_ms, 1.0)
        return 0.6 * tps_component + 0.4 * ttft_component

    return max(
        measured,
        key=lambda offer: (speed_score(offer), -blended_price(offer)),
    )


def choose_offer(
    record: ModelRecord,
    *,
    policy: str = "balanced",
    source: str | None = None,
    tps_floor: str | None = None,
    balanced_tolerance: float = 0.25,
) -> RouteDecision:
    """Choose a healthy offer and return an explainable routing receipt."""

    policy = POLICY_ALIASES.get(policy, policy)
    if policy not in {"cheapest", "fastest", "balanced"}:
        raise ValueError(f"unsupported policy: {policy}")
    if tps_floor is not None and tps_floor not in TPS_FLOORS:
        raise ValueError(f"unsupported TPS floor: {tps_floor}")

    candidates = [offer for offer in record.offers if offer.healthy]
    if source:
        candidates = [offer for offer in candidates if offer.source == source]
    if not candidates:
        raise NoRouteError(f"no healthy offers for {record.id}")

    floor_relaxed = False
    applied_policy = policy
    if tps_floor:
        floor = TPS_FLOORS[tps_floor]
        qualifying = [
            offer
            for offer in candidates
            if offer.tps_p50 is not None and offer.tps_p50 >= floor
        ]
        if qualifying:
            candidates = qualifying
        else:
            floor_relaxed = True
            applied_policy = "fastest"

    if applied_policy == "cheapest":
        selected = _cheapest(candidates)
        reason = "lowest blended input/output price"
    elif applied_policy == "fastest":
        selected = _fastest(candidates)
        reason = "highest measured TPS"
    else:
        measured = [offer for offer in candidates if offer.tps_p50 is not None]
        if measured:
            fastest_tps = max(offer.tps_p50 or 0.0 for offer in measured)
            near_fastest = [
                offer
                for offer in measured
                if (offer.tps_p50 or 0.0)
                >= fastest_tps * (1.0 - balanced_tolerance)
            ]
            selected = _cheapest(near_fastest)
            reason = (
                f"cheapest offer within {balanced_tolerance:.0%} of the fastest "
                "measured TPS"
            )
        else:
            selected = _cheapest(candidates)
            reason = "no TPS measurements; lowest blended price"

    if floor_relaxed:
        reason = "no offer met the TPS floor; fastest available"
    return RouteDecision(
        requested_model=record.id,
        upstream_model=selected.upstream_model,
        offer=selected,
        policy=policy,
        applied_policy=applied_policy,
        floor_relaxed=floor_relaxed,
        reason=reason,
    )
