"""Local usage and savings ledger.

Every routed request appends one JSON line: what was asked for, which seller
served it, how many tokens came back, what it cost, and what it would have cost
on the alternatives that were on offer. From that the card can answer the two
questions that actually matter: "what did I spend" and "what did routing save
me".

Privacy rules this module enforces:

* No prompt or completion text is ever stored, only token counts.
* No credential, API key, wallet address or peer secret is stored.
* The ledger is a plain append-only file under ``~/.hermes/llmux/`` that the
  user can read, truncate or delete at any time.

Cost honesty rules:

* When the marketplace reports what it charged (Surplus sends
  ``x-si-buyer-cost-micro``), that figure wins and is flagged authoritative.
* Otherwise cost is estimated from the offer's published per-token prices and
  flagged as an estimate.
* When a response carries no usage figures, the row records nulls and
  ``usage_reported: false`` instead of guessing a token count.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .paths import data_dir
ENV_PATH = "LLMUX_LEDGER_PATH"
MICRO = 1_000_000


def token_cost_usd(
    prompt_tokens: int | None,
    completion_tokens: int | None,
    input_per_m: float | None,
    output_per_m: float | None,
    cached_tokens: int | None = None,
    cached_input_per_m: float | None = None,
) -> float | None:
    """Cost in USD from token counts and per-million prices, or None."""

    if prompt_tokens is None or completion_tokens is None:
        return None
    if input_per_m is None or output_per_m is None:
        return None
    fresh_input = prompt_tokens
    cached_cost = 0.0
    if cached_tokens and cached_input_per_m is not None:
        cached = min(cached_tokens, prompt_tokens)
        fresh_input = prompt_tokens - cached
        cached_cost = cached * cached_input_per_m
    return round(
        (fresh_input * input_per_m + cached_cost + completion_tokens * output_per_m) / MICRO,
        8,
    )


def extract_usage(payload: Any) -> dict[str, Any]:
    """Token counts out of an OpenAI-shaped completion body.

    Returns None values rather than zeros when a figure is absent, so an
    unreported count is never mistaken for a free request.
    """

    if not isinstance(payload, dict):
        return {"prompt_tokens": None, "completion_tokens": None, "cached_tokens": None}
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {"prompt_tokens": None, "completion_tokens": None, "cached_tokens": None}
    details = usage.get("prompt_tokens_details")
    cached = None
    if isinstance(details, dict) and isinstance(details.get("cached_tokens"), (int, float)):
        cached = int(details["cached_tokens"])
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    return {
        "prompt_tokens": int(prompt) if isinstance(prompt, (int, float)) else None,
        "completion_tokens": int(completion) if isinstance(completion, (int, float)) else None,
        "cached_tokens": cached,
    }


def cost_from_micro_header(value: str | None) -> float | None:
    """Surplus reports what it charged in integer microdollars; trust it."""

    if not value:
        return None
    try:
        return round(int(value) / MICRO, 8)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class RouteCosts:
    """What one request cost, and what the alternatives on offer would have cost."""

    actual_usd: float | None
    authoritative: bool
    list_usd: float | None
    cheapest_usd: float | None
    priciest_usd: float | None

    @property
    def savings_vs_list(self) -> float | None:
        if self.list_usd is None or self.actual_usd is None:
            return None
        return round(self.list_usd - self.actual_usd, 8)

    @property
    def savings_vs_priciest(self) -> float | None:
        if self.priciest_usd is None or self.actual_usd is None:
            return None
        return round(self.priciest_usd - self.actual_usd, 8)

    @property
    def missed_saving_vs_cheapest(self) -> float | None:
        """Positive when a cheaper route existed than the one taken."""

        if self.cheapest_usd is None or self.actual_usd is None:
            return None
        return round(self.actual_usd - self.cheapest_usd, 8)


def compute_costs(
    *,
    usage: dict[str, Any],
    input_per_m: float,
    output_per_m: float,
    cached_input_per_m: float | None,
    charged_micro_header: str | None,
    list_input_per_m: float | None,
    list_output_per_m: float | None,
    alternative_offers: Iterable[tuple[float, float]] = (),
) -> RouteCosts:
    """Actual, list-price and alternative costs for one request."""

    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    cached = usage.get("cached_tokens")
    charged = cost_from_micro_header(charged_micro_header)
    estimated = token_cost_usd(
        prompt, completion, input_per_m, output_per_m, cached, cached_input_per_m
    )
    actual = charged if charged is not None else estimated
    list_usd = token_cost_usd(prompt, completion, list_input_per_m, list_output_per_m)
    alternatives = [
        cost
        for cost in (
            token_cost_usd(prompt, completion, alt_in, alt_out, cached, cached_input_per_m)
            for alt_in, alt_out in alternative_offers
        )
        if cost is not None
    ]
    return RouteCosts(
        actual_usd=actual,
        authoritative=charged is not None,
        list_usd=list_usd,
        cheapest_usd=min(alternatives) if alternatives else None,
        priciest_usd=max(alternatives) if alternatives else None,
    )


class Ledger:
    """Append-only JSONL usage log with rollup summaries."""

    def __init__(self, path: Path | str | None = None) -> None:
        if path is None:
            path = os.environ.get(ENV_PATH) or (data_dir() / "usage.jsonl")
        self.path = Path(path)
        self._lock = threading.Lock()
        self._entries: list[dict[str, Any]] = []
        self._loaded = False

    def record(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Append one usage row. Never raises: tracking must not break a reply."""

        row = {"ts": time.time(), **entry}
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, separators=(",", ":")) + "\n")
                self._entries.append(row)
                self._loaded = True
        except OSError:
            return row
        return row

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self._loaded:
                self._entries = list(self._read())
                self._loaded = True
            return list(self._entries)

    def _read(self) -> Iterable[dict[str, Any]]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        yield row
        except OSError:
            return

    def summary(self, *, days: float = 30.0) -> dict[str, Any]:
        """Totals over the window: spend, savings, cashback, per source and model."""

        cutoff = time.time() - days * 86400.0
        rows = [row for row in self.entries() if float(row.get("ts") or 0) >= cutoff]
        spend = sum(float(r["actual_usd"]) for r in rows if isinstance(r.get("actual_usd"), (int, float)))
        list_spend = sum(float(r["list_cost_usd"]) for r in rows if isinstance(r.get("list_cost_usd"), (int, float)))
        priciest_spend = sum(
            float(r["priciest_cost_usd"]) for r in rows if isinstance(r.get("priciest_cost_usd"), (int, float))
        )
        cheapest_spend = sum(
            float(r["cheapest_cost_usd"]) for r in rows if isinstance(r.get("cheapest_cost_usd"), (int, float))
        )
        cashback = sum(float(r["cashback_usd"]) for r in rows if isinstance(r.get("cashback_usd"), (int, float)))
        measured = [r for r in rows if r.get("usage_reported")]
        by_source: dict[str, dict[str, Any]] = {}
        by_model: dict[str, dict[str, Any]] = {}
        for row in rows:
            source = str(row.get("source") or "unknown")
            model = str(row.get("model") or "unknown")
            for key, bucket in ((source, by_source), (model, by_model)):
                slot = bucket.setdefault(
                    key,
                    {"requests": 0, "spend_usd": 0.0, "savings_vs_list_usd": 0.0, "cashback_usd": 0.0},
                )
                slot["requests"] += 1
                slot["spend_usd"] = round(slot["spend_usd"] + float(row.get("actual_usd") or 0.0), 8)
                slot["savings_vs_list_usd"] = round(
                    slot["savings_vs_list_usd"] + float(row.get("savings_vs_list_usd") or 0.0), 8
                )
                slot["cashback_usd"] = round(
                    slot["cashback_usd"] + float(row.get("cashback_usd") or 0.0), 8
                )
        return {
            "window_days": days,
            "requests": len(rows),
            "requests_with_usage": len(measured),
            "usage_reported_pct": round(100.0 * len(measured) / len(rows), 1) if rows else None,
            "spend_usd": round(spend, 6),
            "list_spend_usd": round(list_spend, 6),
            "priciest_alternative_spend_usd": round(priciest_spend, 6),
            "cheapest_alternative_spend_usd": round(cheapest_spend, 6),
            "savings_vs_list_usd": round(list_spend - spend, 6),
            "savings_vs_priciest_usd": round(priciest_spend - spend, 6),
            "extra_paid_over_cheapest_usd": round(spend - cheapest_spend, 6),
            "cashback_usd": round(cashback, 6),
            "effective_spend_after_cashback_usd": round(spend - cashback, 6),
            "by_source": by_source,
            "by_model": by_model,
        }

    def recent(self, *, limit: int = 25, model: str | None = None) -> list[dict[str, Any]]:
        rows = self.entries()
        if model:
            rows = [row for row in rows if row.get("model") == model]
        return list(reversed(rows[-limit:]))
