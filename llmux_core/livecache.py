"""On-disk snapshot of the live market for a warm restart.

The proxy keeps the market in memory and refreshes it in the background
(stale-while-revalidate, see ``ProxyServer``). This snapshot lets a restarted proxy answer from the last known market
immediately instead of starting cold. The snapshot holds public market data
only (prices, speeds, seller names), never keys or usage.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import ModelRecord, Offer

LOGGER = logging.getLogger("llmux.livecache")
DEFAULT_SNAPSHOT = Path.home() / ".hermes" / "llmux" / "market-cache.json"
SNAPSHOT_VERSION = 1


def offer_to_dict(offer: Offer) -> dict[str, Any]:
    return asdict(offer)


def offer_from_dict(raw: dict[str, Any]) -> Offer | None:
    try:
        return Offer(**{k: raw[k] for k in Offer.__dataclass_fields__ if k in raw})
    except TypeError:
        return None


def record_to_dict(record: ModelRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "offers": [offer_to_dict(o) for o in record.offers],
        "aliases": list(record.aliases),
        "metadata": record.metadata,
    }


def record_from_dict(raw: dict[str, Any]) -> ModelRecord | None:
    try:
        offers = tuple(o for o in (offer_from_dict(x) for x in raw.get("offers", [])) if o)
        return ModelRecord(
            id=str(raw["id"]),
            name=str(raw.get("name") or raw["id"]),
            offers=offers,
            aliases=tuple(raw.get("aliases") or ()),
            metadata=dict(raw.get("metadata") or {}),
        )
    except (KeyError, TypeError):
        return None


class Snapshot:
    """Atomic JSON snapshot of public market data for a warm restart."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or os.environ.get("LLMUX_MARKET_CACHE") or DEFAULT_SNAPSHOT)
        # Background refreshes save concurrently; serialize so writes never interleave.
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("version") != SNAPSHOT_VERSION:
            return {}
        return data

    def save(self, data: dict[str, Any]) -> None:
        payload = json.dumps({"version": SNAPSHOT_VERSION, **data}, separators=(",", ":"))
        with self._lock:
            tmp_name = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # Unique temp file in the same directory, then an atomic rename:
                # a reader never sees a half-written snapshot.
                fd, tmp_name = tempfile.mkstemp(
                    prefix=".market-cache-", suffix=".tmp", dir=self.path.parent
                )
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                os.replace(tmp_name, self.path)
                tmp_name = None
            except OSError as exc:
                LOGGER.debug("market snapshot not saved: %s", exc)
            finally:
                if tmp_name is not None:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
