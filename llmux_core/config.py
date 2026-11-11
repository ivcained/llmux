"""Runtime settings with profile-scoped Hermes credential resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from .adapters import ANTSEED_DEFAULT_BASE_URL, SOLID_DEFAULT_BASE_URL, SURPLUS_DEFAULT_BASE_URL

# Key names checked in order. Hermes stores a custom provider's key as
# HERMES_CUSTOM_<NAME>_API_KEY, so a user who already added Solid as a Hermes
# provider needs no second copy of the key.
SOLID_KEY_NAMES = ("LLMUX_SOLID_API_KEY", "SOLID_API_KEY", "HERMES_CUSTOM_SOLID_API_KEY")
SURPLUS_KEY_NAMES = ("LLMUX_SURPLUS_API_KEY", "SURPLUS_API_KEY")


def _scoped_credentials() -> dict[str, str]:
    """Hermes owns secret resolution. Missing/unbound scopes never use files."""
    from agent.secret_scope import get_secret

    return {name: value for name in (*SURPLUS_KEY_NAMES, *SOLID_KEY_NAMES)
            if (value := get_secret(name))}


def _first(values: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        if values.get(name):
            return values[name]
    return None
from .routing import POLICY_ALIASES, TPS_FLOORS


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8378
    policy: str = "balanced"
    tps_floor: str | None = None
    balanced_tolerance: float = 0.25
    surplus_base_url: str = SURPLUS_DEFAULT_BASE_URL
    surplus_api_key: str | None = None
    antseed_base_url: str = ANTSEED_DEFAULT_BASE_URL
    antseed_min_reputation: float = 50.0
    catalog_ttl_seconds: float = 60.0
    upstream_timeout_seconds: float = 300.0
    allow_remote: bool = False
    track_usage: bool = False
    allow_failover: bool = False
    solid_base_url: str = SOLID_DEFAULT_BASE_URL
    solid_api_key: str | None = None


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Parse LLMUX_* environment variables, rejecting invalid policy values."""

    values = os.environ if env is None else env
    secrets = _scoped_credentials() if env is None else env
    raw_policy = (values.get("LLMUX_POLICY") or "balanced").strip().lower()
    policy = POLICY_ALIASES.get(raw_policy, raw_policy)
    if policy not in {"cheapest", "fastest", "balanced"}:
        raise ValueError(f"LLMUX_POLICY must be cheapest, fastest or balanced, got {policy}")
    floor = (values.get("LLMUX_TPS_FLOOR") or "").strip().lower() or None
    if floor is not None and floor not in TPS_FLOORS:
        raise ValueError(f"LLMUX_TPS_FLOOR must be low, mid or high, got {floor}")
    host = (values.get("LLMUX_HOST") or "127.0.0.1").strip()
    if host not in {"127.0.0.1", "localhost", "::1"} and values.get(
        "LLMUX_ALLOW_REMOTE"
    ) != "1":
        raise ValueError("non-local LLMUX_HOST requires LLMUX_ALLOW_REMOTE=1")
    try:
        port = int(values.get("LLMUX_PORT") or 8378)
        min_reputation = float(values.get("LLMUX_ANTSEED_MIN_REPUTATION") or 50.0)
        balanced_tolerance = float(values.get("LLMUX_BALANCED_TOLERANCE") or 0.25)
        catalog_ttl = float(values.get("LLMUX_CATALOG_TTL_SECONDS") or 60.0)
        timeout = float(values.get("LLMUX_UPSTREAM_TIMEOUT_SECONDS") or 300.0)
    except ValueError as exc:
        raise ValueError(f"invalid numeric LLMUX setting: {exc}") from exc
    if not 0.0 <= balanced_tolerance <= 1.0:
        raise ValueError("LLMUX_BALANCED_TOLERANCE must be between 0 and 1")
    return Settings(
        host=host,
        port=port,
        policy=policy,
        tps_floor=floor,
        balanced_tolerance=balanced_tolerance,
        allow_remote=values.get("LLMUX_ALLOW_REMOTE") == "1",
        surplus_base_url=(values.get("LLMUX_SURPLUS_BASE_URL") or SURPLUS_DEFAULT_BASE_URL).rstrip("/"),
        surplus_api_key=_first(secrets, SURPLUS_KEY_NAMES),
        antseed_base_url=(values.get("LLMUX_ANTSEED_BASE_URL") or ANTSEED_DEFAULT_BASE_URL).rstrip("/"),
        antseed_min_reputation=min_reputation,
        catalog_ttl_seconds=catalog_ttl,
        upstream_timeout_seconds=timeout,
        track_usage=(values.get("LLMUX_TRACK_USAGE") or "0").strip() == "1",
        allow_failover=(values.get("LLMUX_ALLOW_FAILOVER") or "0").strip() == "1",
        solid_base_url=(values.get("LLMUX_SOLID_BASE_URL") or SOLID_DEFAULT_BASE_URL).rstrip("/"),
        solid_api_key=_first(secrets, SOLID_KEY_NAMES),
    )
