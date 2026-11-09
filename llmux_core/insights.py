"""Savings advice computed from the local ledger and the live catalog.

Each insight is a concrete, checkable claim with the dollar figure behind it,
not a generic tip. Rules:

* Only advise from measured data. A missing figure produces no advice rather
  than a guess.
* Never call cashback "earned": unconfirmed reward terms are labelled as
  estimates, and the user's own policy file is the only source of terms.
* Rank by money at stake, largest first, and cap the list so it stays readable.
"""

from __future__ import annotations

from typing import Any, Iterable

from .rewards import RewardTable

MAX_INSIGHTS = 5
# Below this, a saving is noise next to rounding and is not worth a nudge.
MIN_DOLLARS = 0.01


def _money(value: float) -> str:
    if value >= 1:
        return f"${value:,.2f}"
    if value >= 0.01:
        return f"${value:.3f}"
    return f"${value:.4f}"


def build_insights(
    summary: dict[str, Any],
    *,
    rewards: RewardTable | None = None,
    free_models_live: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Ranked savings advice for the window covered by ``summary``."""

    out: list[dict[str, Any]] = []
    requests = int(summary.get("requests") or 0)
    if not requests:
        return [
            {
                "kind": "empty",
                "title": "No routed requests yet",
                "detail": "Savings appear here after your first reply through LLMUX.",
                "usd": 0.0,
            }
        ]

    extra = float(summary.get("extra_paid_over_cheapest_usd") or 0.0)
    if extra >= MIN_DOLLARS:
        spend = float(summary.get("spend_usd") or 0.0)
        pct = round(100 * extra / spend) if spend > 0 else None
        out.append(
            {
                "kind": "cheaper_route",
                "title": f"{_money(extra)} more than the cheapest seller",
                "detail": (
                    "Cheaper sellers served the same models in this window"
                    + (f"; that is {pct}% of your spend" if pct is not None else "")
                    + ". Switch the policy to Cheapest when speed is not critical."
                ),
                "usd": round(extra, 6),
                "action": {"policy": "cheapest"},
            }
        )

    by_model = summary.get("by_model") or {}
    if by_model:
        top_model, top = max(by_model.items(), key=lambda item: item[1].get("spend_usd") or 0.0)
        top_spend = float(top.get("spend_usd") or 0.0)
        total = float(summary.get("spend_usd") or 0.0)
        if top_spend >= MIN_DOLLARS and total > 0 and top_spend / total >= 0.5:
            out.append(
                {
                    "kind": "concentration",
                    "title": f"{top_model} is {round(100 * top_spend / total)}% of your spend",
                    "detail": (
                        "Open the LLMUX card on this model and check Similar models: "
                        "a model in the same price tier with equal or better measured speed "
                        "can cut this line directly."
                    ),
                    # Rank by recoverable money only: spend on a model is not a
                    # saving, so it must not outrank a concrete cheaper route.
                    "usd": 0.0,
                    "spend_usd": round(top_spend, 6),
                    "action": {"model": top_model},
                }
            )

    if rewards is not None:
        by_source = summary.get("by_source") or {}
        for source in rewards.sources():
            policy = rewards.for_source(source)
            if policy is None or not policy.cashback_pct:
                continue
            routed_elsewhere = sum(
                float(slot.get("spend_usd") or 0.0)
                for name, slot in by_source.items()
                if name != source
            )
            if routed_elsewhere < MIN_DOLLARS:
                continue
            potential = routed_elsewhere * policy.cashback_pct / 100.0
            if policy.monthly_cap_usd is not None:
                potential = min(potential, policy.monthly_cap_usd)
            if potential < MIN_DOLLARS:
                continue
            label = "" if policy.confirmed else " (estimate: terms not confirmed)"
            out.append(
                {
                    "kind": "cashback",
                    "title": f"Up to {_money(potential)} back via {source}{label}",
                    "detail": (
                        f"{source} returns {policy.cashback_pct:g}% on eligible spend"
                        + (f", capped at {_money(policy.monthly_cap_usd)}/month" if policy.monthly_cap_usd else "")
                        + f". {_money(routed_elsewhere)} of your spend went to other sources."
                        + (f" {policy.note}" if policy.note else "")
                    ),
                    "usd": round(potential, 6),
                    "confirmed": policy.confirmed,
                    "action": {"source": source},
                }
            )

        used_models = set(by_model)
        for source in rewards.sources():
            policy = rewards.for_source(source)
            if policy is None:
                continue
            for model in policy.free_models:
                if model in used_models:
                    continue
                out.append(
                    {
                        "kind": "free_model",
                        "title": f"{model} is free on {source}",
                        "detail": (
                            "Try it for drafts, summaries and tool calls where a frontier "
                            "model is overkill."
                            + ("" if policy.confirmed else " Terms not confirmed; check before relying on it.")
                        ),
                        "usd": 0.0,
                        "action": {"model": model, "source": source},
                    }
                )

    for model in free_models_live:
        if model in by_model:
            continue
        out.append(
            {
                "kind": "free_model",
                "title": f"{model} is listed at $0 right now",
                "detail": "A seller is offering this model free; useful for low-stakes work.",
                "usd": 0.0,
                "action": {"model": model},
            }
        )

    reported = summary.get("usage_reported_pct")
    if isinstance(reported, (int, float)) and reported < 80:
        out.append(
            {
                "kind": "data_gap",
                "title": f"Only {reported:g}% of replies reported token usage",
                "detail": (
                    "Streaming replies without a usage block cannot be costed, so savings are "
                    "understated. Totals above cover measured replies only."
                ),
                "usd": 0.0,
            }
        )

    money = sorted((i for i in out if i["usd"] > 0), key=lambda i: -i["usd"])
    other = [i for i in out if i["usd"] <= 0]
    return (money + other)[:MAX_INSIGHTS]
