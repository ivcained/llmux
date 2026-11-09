"""Reward and cashback policies, kept as data rather than hard-coded claims.

Every marketplace pays differently and promos change, so the effective price a
user actually sees depends on a policy table: a cashback percentage, the models
it applies to, and any monthly cap. LLMUX ships with no vendor promises baked
in. Policies load from a user-editable JSON file, so a new program (or a
corrected one) is a data change, not a code change.

Nothing here holds credentials. A policy describes economics only.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path.home() / ".hermes" / "llmux" / "rewards.json"
ENV_PATH = "LLMUX_REWARDS_JSON"


@dataclass(frozen=True)
class RewardPolicy:
    """Cashback terms for one source.

    `models` empty means the policy applies to every model on that source.
    `monthly_cap_usd` of None means uncapped. `confirmed` records whether the
    terms were verified with the vendor: an unconfirmed policy is still applied
    to estimates but is labelled as an estimate in the UI, never as money owed.
    """

    source: str
    cashback_pct: float = 0.0
    models: tuple[str, ...] = ()
    monthly_cap_usd: float | None = None
    free_models: tuple[str, ...] = ()
    note: str = ""
    confirmed: bool = False

    def applies_to(self, model: str) -> bool:
        if not self.cashback_pct and model not in self.free_models:
            return False
        return not self.models or model in self.models

    def cashback_for(self, model: str, spend_usd: float | None) -> float | None:
        """Cashback in USD for one request, or None when not applicable."""

        if not self.applies_to(model):
            return None
        if not self.cashback_pct or spend_usd is None:
            return None
        return round(spend_usd * self.cashback_pct / 100.0, 8)

    def is_free(self, model: str) -> bool:
        return model in self.free_models


@dataclass(frozen=True)
class RewardTable:
    policies: dict[str, RewardPolicy] = field(default_factory=dict)

    def for_source(self, source: str) -> RewardPolicy | None:
        return self.policies.get(source)

    def sources(self) -> list[str]:
        return sorted(self.policies)


def parse_policies(raw: Any) -> RewardTable:
    """Build a table from parsed JSON: ``{"surplus": {...}, "solid": {...}}``."""

    policies: dict[str, RewardPolicy] = {}
    if not isinstance(raw, dict):
        return RewardTable(policies={})
    for source, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        models = entry.get("models") or ()
        free_models = entry.get("free_models") or ()
        cap = entry.get("monthly_cap_usd")
        policies[str(source)] = RewardPolicy(
            source=str(source),
            cashback_pct=float(entry.get("cashback_pct") or 0.0),
            models=tuple(str(m) for m in models),
            monthly_cap_usd=float(cap) if isinstance(cap, (int, float)) else None,
            free_models=tuple(str(m) for m in free_models),
            note=str(entry.get("note") or ""),
            confirmed=bool(entry.get("confirmed")),
        )
    return RewardTable(policies=policies)


def load_table(path: Path | str | None = None) -> RewardTable:
    """Load policies from LLMUX_REWARDS_JSON, the given path, or the default.

    A missing or unreadable file yields an empty table: rewards are an
    enhancement, never a reason to lose usage tracking.
    """

    candidate = path or os.environ.get(ENV_PATH) or DEFAULT_PATH
    try:
        return parse_policies(json.loads(Path(candidate).read_text()))
    except (OSError, ValueError):
        return RewardTable(policies={})
