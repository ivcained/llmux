"""Local OpenAI-compatible routing proxy for LLMUX."""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .catalog import Catalog
from .insights import build_insights
from .livecache import Snapshot, offer_from_dict, offer_to_dict, record_from_dict, record_to_dict
from .ledger import Ledger, compute_costs, extract_usage
from .rewards import RewardTable
from .models import ModelDirective, ModelRecord, Offer, RouteDecision
from .routing import NoRouteError, blended_price, choose_offer, parse_model_directive

LOGGER = logging.getLogger("llmux.proxy")
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
MAX_ERROR_BODY = 65536
STREAM_CHUNK_SIZE = 8192
# No served LLM sustains generation above this; higher figures come from cached
# or trivially short completions and must not win a speed ranking.
MAX_PLAUSIBLE_TPS = 2000.0
# Sources that sell at a fixed published price and report no speed figures.
UNMEASURED_SOURCES = frozenset({"solid"})


def provider_source(provider: str | None) -> str | None:
    """Map a Hermes provider id to the marketplace it names.

    None means the chat goes through LLMUX (or the provider is not known), so
    the LLMUX planner is authoritative. ``"unknown"`` means some other direct
    provider that LLMUX has no prices for.
    """

    value = (provider or "").strip().lower()
    if not value or value == "llmux" or value.endswith(":llmux"):
        return None
    value = value.split(":", 1)[-1]
    for source in ("solid", "surplus", "antseed"):
        if source in value:
            return source
    return "unknown"


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def plausible_tps(value: float | None) -> float | None:
    """Measured TPS, or None when the figure is not a credible generation rate."""

    if value is None:
        return None
    return value if value <= MAX_PLAUSIBLE_TPS else None


class _UsageScanner:
    """Picks the usage block out of an SSE stream without buffering the reply.

    OpenAI-style streams with ``stream_options.include_usage`` put token counts
    in the final data event. Only the trailing partial line is kept between
    chunks, so memory stays bounded however long the reply runs.
    """

    MAX_PENDING = 65536

    def __init__(self) -> None:
        self._pending = b""
        self.usage: dict[str, Any] = extract_usage(None)

    def feed(self, chunk: bytes) -> None:
        data = self._pending + chunk
        lines = data.split(b"\n")
        self._pending = lines.pop()[-self.MAX_PENDING :]
        for line in lines:
            self._line(line.strip())

    def _line(self, line: bytes) -> None:
        if not line.startswith(b"data:") or b'"usage"' not in line:
            return
        payload = line[5:].strip()
        try:
            found = extract_usage(json.loads(payload))
        except ValueError:
            return
        if found.get("completion_tokens") is not None or found.get("prompt_tokens") is not None:
            self.usage = found


class UpstreamError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: bytes = b"",
        retryable: bool = False,
        budget_exceeded: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.retryable = retryable
        self.budget_exceeded = budget_exceeded


@dataclass(frozen=True)
class ProxyConfig:
    policy: str = "balanced"
    tps_floor: str | None = None
    balanced_tolerance: float = 0.25
    catalog_ttl_seconds: float = 60.0
    detail_ttl_seconds: float = 120.0
    similar_price_band: float = 2.0
    similar_limit: int = 3
    similar_min_samples: int = 10
    # The card sorts client-side, so it needs a wider candidate set than the
    # default API limits: sorting the top four speed-ranked rows by price would
    # only reorder an already-filtered shortlist.
    card_seller_limit: int = 8
    card_similar_limit: int = 6
    upstream_timeout_seconds: float = 300.0
    allow_remote: bool = False
    # Live cache: keep these models' seller detail refreshed in the background
    # so the card opens instantly. Models the user opens join automatically.
    warm_models: tuple[str, ...] = ()
    warm_interval_seconds: float = 45.0
    warm_max_models: int = 24


class ProxyServer:
    """HTTP proxy that routes OpenAI chat requests across marketplace adapters."""

    def __init__(
        self,
        adapters: dict[str, Any],
        config: ProxyConfig | None = None,
        *,
        ledger: Ledger | None = None,
        rewards: RewardTable | None = None,
        snapshot: Snapshot | None = None,
    ) -> None:
        self.adapters = adapters
        self.config = config or ProxyConfig()
        # Usage tracking is opt-in at construction: the composition root passes
        # a ledger, tests and embedders that pass none write nothing to disk.
        self.ledger = ledger
        self.rewards = rewards or RewardTable()
        self._catalog: Catalog | None = None
        self._catalog_time = 0.0
        self._catalog_lock = threading.Lock()
        # Each cache entry: (monotonic fetch time, wall-clock fetch time, offers).
        self._detail_cache: dict[str, tuple[float, float, list[Offer]]] = {}
        self._detail_lock = threading.Lock()
        self._refreshing: set[str] = set()
        self._catalog_wall: float | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._warm_stop = threading.Event()
        self._warm_thread: threading.Thread | None = None
        # Models opened in the card, most recent last; the warm loop keeps them live.
        self._watched: list[str] = list(self.config.warm_models)
        self.source_errors: dict[str, str] = {}
        # Persistence is opt-in like the ledger: tests construct without one.
        self.snapshot = snapshot
        if snapshot is not None:
            self._load_snapshot()

    def _get_catalog(self) -> Catalog:
        """The market catalog, served from memory whenever one exists.

        A stale catalog is returned immediately and refreshed on a background
        thread (stale-while-revalidate), so opening the card never waits on
        three marketplaces. Only a cold start with no snapshot blocks.
        """

        if self._catalog is not None:
            age = time.monotonic() - self._catalog_time
            if age >= self.config.catalog_ttl_seconds:
                self._spawn_refresh("__catalog__", self._refresh_catalog)
            return self._catalog
        return self._refresh_catalog()

    def _spawn_refresh(self, key: str, target) -> None:
        """Run one background refresh per key; concurrent callers do not pile up."""

        with self._detail_lock:
            if key in self._refreshing:
                return
            self._refreshing.add(key)

        def run() -> None:
            try:
                target()
            except Exception as exc:  # noqa: BLE001 - the old value keeps serving
                LOGGER.debug("background refresh %s failed: %s", key, exc)
            finally:
                with self._detail_lock:
                    self._refreshing.discard(key)

        threading.Thread(target=run, daemon=True, name=f"llmux-refresh-{key}").start()

    def _refresh_catalog(self) -> Catalog:
        with self._catalog_lock:
            groups: list[list[ModelRecord]] = []
            errors: dict[str, str] = {}
            for name, adapter in self.adapters.items():
                try:
                    records = adapter.fetch_catalog()
                    groups.append(records)
                    LOGGER.info("catalog %s: %d records", name, len(records))
                except Exception as exc:
                    LOGGER.warning("catalog fetch failed for %s: %s", name, exc)
                    errors[name] = str(exc)
            self.source_errors = errors
            if groups:
                self._catalog = Catalog.merge(groups)
                self._catalog_time = time.monotonic()
                self._catalog_wall = time.time()
                self._save_snapshot()
            elif self._catalog is not None:
                # Every source failed: keep serving the last market, retry soon.
                self._catalog_time = time.monotonic() - self.config.catalog_ttl_seconds + 15.0
            if self._catalog is None:
                raise UpstreamError("no marketplace catalog is available", retryable=True)
            return self._catalog

    def _surplus_detail_offers(self, upstream_model: str) -> list[Offer]:
        """Named per-provider Surplus offers, cached; empty when unavailable.

        A detail fetch failure must never break routing: the aggregated
        marketplace offer remains a valid route, it just cannot be pinned to a
        specific provider.
        """

        adapter = self.adapters.get("surplus")
        fetch = getattr(adapter, "provider_offers", None)
        if not callable(fetch):
            return []
        with self._detail_lock:
            cached = self._detail_cache.get(upstream_model)
        if cached is not None:
            if time.monotonic() - cached[0] >= self.config.detail_ttl_seconds:
                self._spawn_refresh(
                    f"detail:{upstream_model}",
                    lambda: self._fetch_detail(fetch, upstream_model),
                )
            return cached[2]
        return self._fetch_detail(fetch, upstream_model)

    def _fetch_detail(self, fetch, upstream_model: str) -> list[Offer]:
        try:
            raw = fetch(upstream_model)
        except Exception as exc:  # noqa: BLE001 - detail is best-effort enrichment
            LOGGER.debug("surplus detail unavailable for %s: %s", upstream_model, exc)
            with self._detail_lock:
                cached = self._detail_cache.get(upstream_model)
                if cached is not None:
                    # Keep the last good offers; retry after a short backoff.
                    self._detail_cache[upstream_model] = (
                        time.monotonic() - self.config.detail_ttl_seconds + 15.0,
                        cached[1],
                        cached[2],
                    )
                    return cached[2]
            return []
        offers = [offer for offer in raw if isinstance(offer, Offer)] if isinstance(raw, (list, tuple)) else []
        with self._detail_lock:
            self._detail_cache[upstream_model] = (time.monotonic(), time.time(), offers)
        self._save_snapshot()
        return offers

    def freshness(self, model: str | None = None) -> dict[str, Any]:
        """Wall-clock age of what the card is showing, so stale data says so."""

        now = time.time()
        detail_at = None
        bare = self._bare_model(model) if model else None
        if bare and self._catalog is not None:
            record = self._catalog.get(bare)
            aggregate = next(
                (o for o in (record.offers if record else ()) if o.source == "surplus"), None
            )
            if aggregate is not None:
                with self._detail_lock:
                    cached = self._detail_cache.get(aggregate.upstream_model)
                detail_at = cached[1] if cached else None
        stamps = [t for t in (self._catalog_wall, detail_at) if t is not None]
        oldest = min(stamps) if stamps else None
        with self._detail_lock:
            refreshing = bool(self._refreshing)
        return {
            "updated_at": oldest,
            "age_seconds": round(now - oldest, 1) if oldest is not None else None,
            "refreshing": refreshing,
            "live_interval_seconds": self.config.warm_interval_seconds,
        }

    # -- live cache: watch list, warm loop, snapshot ---------------------------

    def watch(self, model: str) -> None:
        """Keep a model's sellers refreshed in the background from now on."""

        bare = self._bare_model(model)
        if not bare:
            return
        with self._detail_lock:
            if bare in self._watched:
                self._watched.remove(bare)
            self._watched.append(bare)
            del self._watched[: max(0, len(self._watched) - self.config.warm_max_models)]

    def warm_once(self) -> None:
        """Refresh the catalog and every watched model's seller detail now."""

        try:
            catalog = self._refresh_catalog()
        except UpstreamError:
            return
        adapter = self.adapters.get("surplus")
        fetch = getattr(adapter, "provider_offers", None)
        if not callable(fetch):
            return
        with self._detail_lock:
            watched = list(self._watched)
        for model in watched:
            if self._warm_stop.is_set():
                return
            record = catalog.get(model)
            aggregate = next(
                (o for o in (record.offers if record else ()) if o.source == "surplus"), None
            )
            if aggregate is not None:
                self._fetch_detail(fetch, aggregate.upstream_model)

    def start_warming(self) -> None:
        """Background loop that keeps the market cache live. Idempotent."""

        if self._warm_thread is not None:
            return
        self._warm_stop.clear()

        def loop() -> None:
            while not self._warm_stop.is_set():
                try:
                    self.warm_once()
                except Exception as exc:  # noqa: BLE001 - the loop must survive any upstream fault
                    LOGGER.warning("market warm cycle failed: %s", exc)
                self._warm_stop.wait(self.config.warm_interval_seconds)

        self._warm_thread = threading.Thread(target=loop, daemon=True, name="llmux-warm")
        self._warm_thread.start()

    def _save_snapshot(self) -> None:
        if self.snapshot is None or self._catalog is None:
            return
        with self._detail_lock:
            detail = {
                model: {"fetched_at": entry[1], "offers": [offer_to_dict(o) for o in entry[2]]}
                for model, entry in self._detail_cache.items()
            }
            watched = list(self._watched)
        self.snapshot.save(
            {
                "catalog_fetched_at": self._catalog_wall,
                "catalog": [record_to_dict(r) for r in self._catalog.records],
                "detail": detail,
                "watched": watched,
            }
        )

    def _load_snapshot(self) -> None:
        """Seed memory from disk. Seeded data is served but immediately stale."""

        assert self.snapshot is not None
        data = self.snapshot.load()
        records = [r for r in (record_from_dict(x) for x in data.get("catalog") or []) if r]
        if records:
            self._catalog = Catalog(records=tuple(records))
            self._catalog_time = -1e9  # stale: first read triggers a background refresh
            wall = data.get("catalog_fetched_at")
            self._catalog_wall = float(wall) if isinstance(wall, (int, float)) else None
        for model, entry in (data.get("detail") or {}).items():
            if not isinstance(entry, dict):
                continue
            offers = [o for o in (offer_from_dict(x) for x in entry.get("offers") or []) if o]
            wall = entry.get("fetched_at")
            self._detail_cache[model] = (
                -1e9,
                float(wall) if isinstance(wall, (int, float)) else 0.0,
                offers,
            )
        for model in data.get("watched") or []:
            if isinstance(model, str) and model not in self._watched:
                self._watched.append(model)

    def _routable_offers(self, record: ModelRecord) -> tuple[Offer, ...]:
        """Offers a user may choose: Antseed peers plus named Surplus providers.

        The aggregated Surplus offer is only kept as a fallback when the detail
        endpoints yield nothing, and is labelled so the card never shows a
        nameless "marketplace" seller next to real provider names.
        """

        others = [offer for offer in record.offers if offer.source != "surplus"]
        aggregate = next((offer for offer in record.offers if offer.source == "surplus"), None)
        if aggregate is None:
            return tuple(others)
        detail = self._surplus_detail_offers(aggregate.upstream_model)
        if detail:
            return tuple(others + detail)
        return tuple(others + [replace(aggregate, seller=aggregate.seller or "Surplus auto")])

    def _route(self, model: str) -> tuple[RouteDecision, ModelDirective, bool]:
        directive = parse_model_directive(model)
        catalog = self._get_catalog()
        record = catalog.get(directive.model)
        if record is None:
            raise NoRouteError(f"unknown model: {directive.model}")
        policy = directive.policy or self.config.policy
        decision: RouteDecision | None
        try:
            decision = choose_offer(
                record,
                policy=policy,
                source=directive.source,
                tps_floor=self.config.tps_floor,
                balanced_tolerance=self.config.balanced_tolerance,
            )
        except NoRouteError:
            # No auto-routable (trusted) offer. An explicit seller pin below
            # can still resolve; without one, the error stands.
            if not (directive.peer_id or directive.provider_id):
                raise
            decision = None
        if directive.peer_id:
            pinned = [
                offer
                for offer in record.offers
                if offer.peer_id == directive.peer_id
                and offer.healthy
                and (directive.source is None or offer.source == directive.source)
            ]
            if not pinned:
                raise NoRouteError(f"seller {directive.peer_id} is not available")
            decision = RouteDecision(
                requested_model=record.id,
                upstream_model=pinned[0].upstream_model,
                offer=pinned[0],
                policy=policy,
                applied_policy=policy,
                reason="seller pinned by user",
            )
        if directive.provider_id:
            pinned = [
                offer
                for offer in self._routable_offers(record)
                if offer.provider_id == directive.provider_id
                and offer.healthy
                and (directive.source is None or offer.source == directive.source)
            ]
            if not pinned:
                raise NoRouteError(f"provider {directive.provider_id} is not available")
            decision = RouteDecision(
                requested_model=record.id,
                upstream_model=pinned[0].upstream_model,
                offer=pinned[0],
                policy=policy,
                applied_policy=policy,
                reason="Surplus provider pinned by user",
            )
        if decision is None:  # unreachable: each pin branch assigns or raises
            raise NoRouteError(f"no route for {directive.model}")
        return (
            decision,
            directive,
            directive.source is None and directive.peer_id is None and directive.provider_id is None,
        )

    def _alternate(
        self,
        record: ModelRecord,
        decision: RouteDecision,
        policy: str,
        tried: set[str] | None = None,
    ) -> RouteDecision | None:
        # Exclude every source already attempted, not just the current one:
        # with two sources both failing, excluding only the current source
        # bounces between them forever.
        excluded = {decision.offer.source} | (tried or set())
        candidates = tuple(
            offer
            for offer in record.offers
            if offer.source not in excluded
        )
        if not candidates:
            return None
        alternate_record = ModelRecord(
            id=record.id, name=record.name, offers=candidates, aliases=record.aliases
        )
        try:
            return choose_offer(
                alternate_record,
                policy=policy,
                tps_floor=self.config.tps_floor,
                balanced_tolerance=self.config.balanced_tolerance,
            )
        except NoRouteError:
            return None

    def _open_upstream(
        self, adapter: Any, decision: RouteDecision, payload: dict, stream: bool
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        parts = urlsplit(adapter.base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise UpstreamError(f"invalid upstream URL for {decision.offer.source}")
        connection_class = (
            http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_class(
            parts.hostname, parts.port, timeout=self.config.upstream_timeout_seconds
        )
        path = f"{parts.path.rstrip('/')}/chat/completions"
        extras: dict = {}
        build_extra_body = getattr(adapter, "build_extra_body", None)
        if callable(build_extra_body):
            try:
                produced = build_extra_body(decision)
            except Exception as exc:  # noqa: BLE001 - never lose a request over metadata
                LOGGER.debug("build_extra_body failed for %s: %s", decision.offer.source, exc)
                produced = None
            if isinstance(produced, dict):
                extras = produced
        body = json.dumps({**payload, **extras, "model": decision.upstream_model}).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": f"llmux/{__version__}",
        }
        api_key = getattr(adapter, "api_key", None)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
        except Exception as exc:
            connection.close()
            raise UpstreamError(f"upstream connection failed: {exc}", retryable=True) from exc
        return connection, response

    @staticmethod
    def _error_code(body: bytes) -> str:
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            return ""
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            inner = error.get("error")
            if isinstance(inner, dict):
                return str(inner.get("code") or inner.get("type") or "")
            return str(error.get("code") or error.get("type") or "")
        return ""

    def _attempt(
        self, adapter: Any, decision: RouteDecision, payload: dict, stream: bool
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse, bytes]:
        connection, response = self._open_upstream(adapter, decision, payload, stream)
        if response.status < 400:
            return connection, response, b""
        declared = response.getheader("Content-Length")
        chunks: list[bytes] = []
        total = 0
        complete = False
        try:
            while total < MAX_ERROR_BODY:
                chunk = response.read1(min(STREAM_CHUNK_SIZE, MAX_ERROR_BODY - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if declared is None:
                    break
            body = b"".join(chunks)
            if declared is not None:
                complete = total >= int(declared)
            else:
                stripped = body.strip()
                complete = bool(stripped) and stripped.endswith(b"}")
        except http.client.IncompleteRead as exc:
            body = exc.partial
        response.close()
        connection.close()
        code = self._error_code(body)
        if response.status == 402 and code == "budget_exceeded":
            raise UpstreamError(
                "budget cap reached",
                status=402,
                body=body,
                budget_exceeded=True,
                retryable=False,
            )
        raise UpstreamError(
            f"upstream returned {response.status}",
            status=response.status,
            body=body,
            retryable=response.status in RETRYABLE_STATUSES
            or (
                response.status == 402
                and code in {"insufficient_credit", "insufficient_balance"}
                and complete
            ),
        )

    @staticmethod
    def _bare_model(model: str) -> str | None:
        """Catalog key for a model string that may carry routing directives.

        After a pin the session model reads ``id@source@provider:x``; catalog
        lookups must use the bare id or the card would go empty exactly when
        the user most needs to switch away again.
        """

        try:
            return parse_model_directive(model).model
        except ValueError:
            return None

    def seller_options(self, model: str, *, limit: int = 4, trust: str = "trusted") -> list[dict]:
        """Return ranked seller options with timings and an honest cache warning.

        ``trust="trusted"`` (default) hides offers a marketplace flagged as
        untrusted; ``trust="all"`` lists them too, each row labelled.
        """

        bare = self._bare_model(model)
        record = self._get_catalog().get(bare) if bare else None
        if record is None:
            return []
        offers = [
            offer for offer in self._routable_offers(record)
            if offer.healthy
            and (trust == "all" or offer.trusted is not False)
            and (
                plausible_tps(offer.tps_p50)
                or offer.ttft_p50_ms
                # Fixed-price APIs publish no telemetry; list them with speed
                # shown as unknown rather than hiding a real route.
                or offer.source in UNMEASURED_SOURCES
            )
        ]
        if not offers:
            return []
        max_tps = max((plausible_tps(offer.tps_p50) or 0.0) for offer in offers) or 1.0
        min_ttft = min(
            (offer.ttft_p50_ms for offer in offers if offer.ttft_p50_ms is not None),
            default=1.0,
        )

        def score(offer) -> float:
            tps_component = (plausible_tps(offer.tps_p50) or 0.0) / max_tps
            ttft_component = (
                min_ttft / max(offer.ttft_p50_ms, 1.0)
                if offer.ttft_p50_ms is not None
                else 0.0
            )
            return 0.6 * tps_component + 0.4 * ttft_component

        ranked = sorted(offers, key=score, reverse=True)[:limit]
        list_input = record.metadata.get("list_input_per_m")
        list_output = record.metadata.get("list_output_per_m")
        list_blended = None
        if isinstance(list_input, (int, float)) and isinstance(list_output, (int, float)):
            list_blended = 0.8 * list_input + 0.2 * list_output
        result = []
        for offer in ranked:
            discount_pct = None
            if list_blended and list_blended > 0:
                discount_pct = max(0, min(100, round((1 - blended_price(offer) / list_blended) * 100)))
                if discount_pct >= 100 and blended_price(offer) > 0:
                    # A non-zero price is never 100% off; keep the badge honest.
                    discount_pct = 99
            row_tps = plausible_tps(offer.tps_p50)
            # No measurement means no estimate: 0s would read as "instant".
            if row_tps is None and offer.ttft_p50_ms is None:
                estimated_reply_seconds = None
            else:
                ttft_s = (offer.ttft_p50_ms or 0) / 1000.0
                estimated_reply_seconds = ttft_s + 1000.0 / row_tps if row_tps else ttft_s
            result.append(
                {
                    "source": offer.source,
                    "seller": offer.seller or offer.source,
                    "peer_id": offer.peer_id,
                    "provider_id": offer.provider_id,
                    "upstream_model": offer.upstream_model,
                    "tps_p50": row_tps,
                    "ttft_p50_ms": offer.ttft_p50_ms,
                    "samples": offer.samples,
                    "input_per_m": offer.input_per_m,
                    "output_per_m": offer.output_per_m,
                    "blended_per_m": round(blended_price(offer), 8),
                    "discount_pct": discount_pct,
                    "estimated_reply_seconds": (
                        round(estimated_reply_seconds, 1)
                        if estimated_reply_seconds is not None
                        else None
                    ),
                    # False when the source never publishes speed (Solid), as
                    # opposed to a measured source lacking data for this seller.
                    "speed_published": offer.source not in UNMEASURED_SOURCES,
                    # True/False when the marketplace publishes a trust signal,
                    # None when it does not (e.g. a fixed-price API).
                    "trusted": offer.trusted,
                    "trust_basis": offer.trust_basis or None,
                    "cache_warning": {
                        "text": (
                            "The next request sends the current conversation context to the "
                            "selected route. Cache discounts may be lost; the interrupted "
                            "request may still be charged."
                        ),
                        "prompt_tokens": None,
                        "uncached_input_cost_usd": None,
                    },
                }
            )
        return result

    @staticmethod
    def _comparison_price(record: ModelRecord, offer: Any) -> tuple[float, str]:
        """Price used to judge "same range": list price when published, else the offer.

        List price tracks the capability tier (a flagship's list sits far above a
        7B's), while a routed offer tracks whatever a discount campaign costs
        right now. Suggestions must compare tiers, not campaigns, so a published
        list price wins and the basis is reported alongside the ratio.
        """

        list_input = record.metadata.get("list_input_per_m")
        list_output = record.metadata.get("list_output_per_m")
        if isinstance(list_input, (int, float)) and isinstance(list_output, (int, float)):
            list_blend = 0.8 * list_input + 0.2 * list_output
            if list_blend > 0:
                return list_blend, "list"
        return blended_price(offer), "market"

    def similar_models(
        self, model: str, *, limit: int | None = None, price_band: float | None = None
    ) -> list[dict]:
        """Comparable models for a mid-chat switch, ranked by measured speed.

        Three honesty rules shape the list:

        * "Same price range" is a symmetric band on the comparison price, so a
          model 200x cheaper is not "similar" just because it is fast, and a
          model 20x pricier is not suggested either.
        * Candidates are compared on the same basis as the current model (list
          against list, market against market); mixing a tier price with a
          campaign price would produce a meaningless ratio.
        * A model without a credible TPS measurement is never proposed as
          similar-or-better performance. Figures above `MAX_PLAUSIBLE_TPS` are
          artifacts of cached or trivially short completions.
        """

        limit = self.config.similar_limit if limit is None else limit
        band = self.config.similar_price_band if price_band is None else price_band
        catalog = self._get_catalog()
        bare = self._bare_model(model)
        current = catalog.get(bare) if bare else None
        if current is None:
            return []
        try:
            current_offer = choose_offer(
                current,
                policy=self.config.policy,
                balanced_tolerance=self.config.balanced_tolerance,
            ).offer
        except NoRouteError:
            return []
        current_price, basis = self._comparison_price(current, current_offer)
        if current_price <= 0:
            return []

        rows: list[dict] = []
        for record in catalog.records:
            if record.id == current.id:
                continue
            try:
                offer = choose_offer(
                    record,
                    policy=self.config.policy,
                    balanced_tolerance=self.config.balanced_tolerance,
                ).offer
            except NoRouteError:
                continue
            if plausible_tps(offer.tps_p50) is None:
                continue
            if int(offer.samples or 0) < self.config.similar_min_samples:
                # Too few measurements to claim "similar or better" performance.
                continue
            candidate_price, candidate_basis = self._comparison_price(record, offer)
            if candidate_price <= 0 or candidate_basis != basis:
                continue
            ratio = candidate_price / current_price
            if ratio > band or ratio < 1.0 / band:
                continue
            list_input = record.metadata.get("list_input_per_m")
            list_output = record.metadata.get("list_output_per_m")
            discount_pct = None
            if isinstance(list_input, (int, float)) and isinstance(list_output, (int, float)):
                list_blend = 0.8 * list_input + 0.2 * list_output
                offer_blend = blended_price(offer)
                if list_blend > 0:
                    discount_pct = max(0, min(100, round((1 - offer_blend / list_blend) * 100)))
                    if discount_pct >= 100 and offer_blend > 0:
                        discount_pct = 99
            rows.append(
                {
                    "model": record.id,
                    "name": record.name or record.id,
                    "source": offer.source,
                    "seller": offer.seller or offer.source,
                    "provider_id": offer.provider_id,
                    "peer_id": offer.peer_id,
                    "input_per_m": offer.input_per_m,
                    "output_per_m": offer.output_per_m,
                    "blended_per_m": round(blended_price(offer), 8),
                    "tps_p50": offer.tps_p50,
                    "ttft_p50_ms": offer.ttft_p50_ms,
                    "samples": offer.samples,
                    "discount_pct": discount_pct,
                    "price_ratio": round(ratio, 2),
                    "price_basis": candidate_basis,
                    "switch_model": record.id,
                }
            )
        rows.sort(key=lambda row: (-(row["tps_p50"] or 0.0), row["price_ratio"]))
        return rows[:limit]

    def current_route(self, model: str, provider: str | None = None) -> dict | None:
        """The route a chat is on, honouring the Hermes provider it actually uses.

        A chat on a non-LLMUX Hermes provider (for example Solid directly) is
        not routed here, so planning it through LLMUX would name the wrong
        seller. In that case the matching marketplace offer is reported with
        ``direct: true``; an unknown provider is reported by name with no
        prices rather than borrowing another source's numbers.
        """

        source = provider_source(provider)
        if source is None:
            return self._planned_route(model)
        bare = self._bare_model(model)
        record = self._get_catalog().get(bare) if bare else None
        offer = None
        if record is not None and source != "unknown":
            offer = next(
                (o for o in record.offers if o.source == source and o.healthy), None
            )
        label = (provider or "").split(":", 1)[-1] or "unknown"
        return {
            "model": record.id if record else bare,
            "name": record.name if record and record.name else bare,
            "requested": model,
            "source": source if source != "unknown" else label,
            "seller": (offer.seller if offer and offer.seller else label),
            "provider_id": None,
            "peer_id": None,
            "input_per_m": offer.input_per_m if offer else None,
            "output_per_m": offer.output_per_m if offer else None,
            "tps_p50": plausible_tps(offer.tps_p50) if offer else None,
            "ttft_p50_ms": offer.ttft_p50_ms if offer else None,
            "samples": offer.samples if offer else 0,
            "policy": None,
            "applied_policy": None,
            "floor_relaxed": False,
            "reason": f"direct Hermes provider {label}; LLMUX is not routing this chat",
            "pinned": True,
            "direct": True,
            "speed_published": source not in UNMEASURED_SOURCES,
        }

    def _planned_route(self, model: str) -> dict | None:
        """The route this model string resolves to right now, with its measurements.

        Answers "which model and seller am I actually on?" without spending a
        request: the same planner the inference path uses, stopped before any
        upstream call. None when the model cannot be routed at all.
        """

        try:
            decision, directive, _ = self._route(model)
        except (NoRouteError, ValueError, UpstreamError):
            return None
        record = self._get_catalog().get(directive.model)
        offer = decision.offer
        return {
            "model": directive.model,
            "name": record.name if record and record.name else directive.model,
            "requested": model,
            "source": offer.source,
            "seller": offer.seller or offer.source,
            "provider_id": offer.provider_id,
            "peer_id": offer.peer_id,
            "input_per_m": offer.input_per_m,
            "output_per_m": offer.output_per_m,
            "tps_p50": plausible_tps(offer.tps_p50),
            "ttft_p50_ms": offer.ttft_p50_ms,
            "samples": offer.samples,
            "policy": decision.policy,
            "applied_policy": decision.applied_policy,
            "floor_relaxed": decision.floor_relaxed,
            "reason": decision.reason,
            "pinned": bool(directive.source or directive.peer_id or directive.provider_id),
            "direct": False,
            "trusted": offer.trusted,
            "trust_basis": offer.trust_basis or None,
            "speed_published": offer.source not in UNMEASURED_SOURCES,
        }

    def seller_options_payload(self, model: str, provider: str | None = None) -> dict:
        """One response carrying sellers, comparable models and the current route.

        The desktop backend mounts its routes once per gateway run, so a newly
        added endpoint is not reachable until Hermes restarts. Riding along in
        the already-mounted ``/seller-options`` response keeps new data available
        without a restart; the standalone endpoints stay for direct API users.
        """

        self.watch(model)
        sellers = self.seller_options(model, limit=self.config.card_seller_limit)
        # Both views ride in one response so the card's Trusted / All switch
        # flips instantly and works through gateway backends that predate it.
        all_sellers = self.seller_options(
            model, limit=self.config.card_seller_limit, trust="all"
        )
        similar = self.similar_models(model, limit=self.config.card_similar_limit)
        current = self.current_route(model, provider)
        return {
            "object": "list",
            # Read after the sections so a cold first call reports its own fetch.
            "freshness": self.freshness(model),
            "data": sellers,
            "all_sellers": all_sellers,
            "untrusted_count": sum(1 for row in all_sellers if row.get("trusted") is False),
            "similar": similar,
            "current": current,
            "savings": self.savings_payload(),
        }

    def record_usage(
        self,
        *,
        record: ModelRecord | None,
        decision: RouteDecision,
        usage: dict[str, Any],
        charged_micro: str | None,
        stream: bool,
        status: int,
        failover_from: str | None,
    ) -> dict[str, Any] | None:
        """Append one ledger row for a completed reply. Never raises."""

        if self.ledger is None:
            return None
        try:
            offer = decision.offer
            metadata = record.metadata if record is not None else {}
            alternatives = []
            if record is not None:
                alternatives = [
                    (alt.input_per_m, alt.output_per_m)
                    for alt in record.offers
                    # Savings are measured against routes LLMUX would use.
                    if alt.healthy and alt.trusted is not False
                ]
                alternatives += [
                    (alt.input_per_m, alt.output_per_m)
                    for alt in self._surplus_detail_offers(offer.upstream_model)
                    if alt.trusted is not False
                ] if offer.source == "surplus" else []
            costs = compute_costs(
                usage=usage,
                input_per_m=offer.input_per_m,
                output_per_m=offer.output_per_m,
                cached_input_per_m=offer.cached_input_per_m,
                charged_micro_header=charged_micro,
                list_input_per_m=_number(metadata.get("list_input_per_m")),
                list_output_per_m=_number(metadata.get("list_output_per_m")),
                alternative_offers=alternatives,
            )
            policy = self.rewards.for_source(offer.source)
            model = record.id if record is not None else decision.requested_model
            cashback = policy.cashback_for(model, costs.actual_usd) if policy else None
            return self.ledger.record(
                {
                    "model": model,
                    "source": offer.source,
                    "seller": offer.seller or offer.source,
                    "provider_id": offer.provider_id,
                    "policy": decision.applied_policy,
                    "stream": stream,
                    "status": status,
                    "failover_from": failover_from,
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "cached_tokens": usage.get("cached_tokens"),
                    "usage_reported": usage.get("completion_tokens") is not None,
                    "actual_usd": costs.actual_usd,
                    "cost_authoritative": costs.authoritative,
                    "list_cost_usd": costs.list_usd,
                    "cheapest_cost_usd": costs.cheapest_usd,
                    "priciest_cost_usd": costs.priciest_usd,
                    "savings_vs_list_usd": costs.savings_vs_list,
                    "cashback_usd": cashback,
                    "cashback_confirmed": bool(policy and policy.confirmed),
                }
            )
        except Exception as exc:  # noqa: BLE001 - tracking must never break a reply
            LOGGER.warning("usage ledger write failed: %s", exc)
            return None

    def savings_payload(self, *, days: float = 30.0) -> dict[str, Any]:
        """Spend, savings and ranked advice for the card and the API."""

        if self.ledger is None:
            return {"enabled": False, "summary": None, "insights": []}
        summary = self.ledger.summary(days=days)
        free_live: list[str] = []
        try:
            free_live = [
                rec.id
                for rec in self._get_catalog().records
                if any(
                    o.healthy and o.trusted is not False and o.input_per_m == 0 and o.output_per_m == 0
                    for o in rec.offers
                )
            ][:3]
        except Exception:  # noqa: BLE001 - advice without the catalog is still advice
            free_live = []
        return {
            "enabled": True,
            "summary": summary,
            "insights": build_insights(summary, rewards=self.rewards, free_models_live=free_live),
            "ledger_path": str(self.ledger.path),
        }

    def plan_route(self, body: dict) -> dict:
        """Return the routing decision for a request without calling upstream."""

        model = body.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError("model is required")
        decision, directive, can_failover = self._route(model)
        return {
            "source": decision.offer.source,
            "upstream_model": decision.upstream_model,
            "policy": decision.policy,
            "applied_policy": decision.applied_policy,
            "floor_relaxed": decision.floor_relaxed,
            "reason": decision.reason,
            "failover_allowed": can_failover,
        }

    def start(self, host: str = "127.0.0.1", port: int = 8378) -> int:
        if host not in {"127.0.0.1", "localhost", "::1"} and not self.config.allow_remote:
            raise ValueError(
                "LLMUX binds locally only; set LLMUX_ALLOW_REMOTE=1 to acknowledge "
                "remote exposure"
            )
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):  # noqa: A002 - base class signature
                LOGGER.debug("proxy request: " + format, *args)

            def _json(self, status: int, payload: dict) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _error(self, status: int, code: str, message: str) -> None:
                self._json(
                    status,
                    {"error": {"message": message, "type": "llmux_error", "code": code}},
                )

            def do_GET(self):
                path = urlsplit(self.path).path
                try:
                    if path == "/health":
                        self._json(
                            200,
                            {
                                "ok": True,
                                "policy": proxy.config.policy,
                                "tps_floor": proxy.config.tps_floor,
                                "source_errors": proxy.source_errors,
                            },
                        )
                        return
                    if path == "/v1/models":
                        self._json(200, proxy._get_catalog().to_openai())
                        return
                    if path.startswith("/v1/models/"):
                        model_id = path.rsplit("/", 1)[-1]
                        record = proxy._get_catalog().get(model_id)
                        if record is None:
                            self._error(404, "model_not_found", f"unknown model: {model_id}")
                            return
                        self._json(
                            200,
                            {
                                "id": record.id,
                                "object": "model",
                                "owned_by": "llmux",
                                "name": record.name,
                                "offers": [
                                    {
                                        "source": offer.source,
                                        "seller": offer.seller,
                                        "input_per_m": offer.input_per_m,
                                        "output_per_m": offer.output_per_m,
                                        "tps_p50": offer.tps_p50,
                                        "ttft_p50_ms": offer.ttft_p50_ms,
                                        "samples": offer.samples,
                                        "healthy": offer.healthy,
                                    }
                                    for offer in record.offers
                                ],
                            },
                        )
                        return
                    if path == "/llmux/plan":
                        model = parse_qs(urlsplit(self.path).query).get("model", [""])[0]
                        self._json(200, proxy.plan_route({"model": model}))
                        return
                    if path == "/llmux/seller-options":
                        query = parse_qs(urlsplit(self.path).query)
                        model = query.get("model", [""])[0]
                        provider = query.get("provider", [""])[0] or None
                        self._json(200, proxy.seller_options_payload(model, provider))
                        return
                    if path == "/llmux/savings":
                        raw_days = parse_qs(urlsplit(self.path).query).get("days", ["30"])[0]
                        try:
                            days = max(0.01, min(365.0, float(raw_days)))
                        except ValueError:
                            days = 30.0
                        self._json(200, proxy.savings_payload(days=days))
                        return
                    if path == "/llmux/similar-models":
                        raw_query = urlsplit(self.path).query
                        model = parse_qs(raw_query).get("model", [""])[0]
                        self._json(
                            200,
                            {"object": "list", "data": proxy.similar_models(model)},
                        )
                        return
                    self._error(404, "not_found", "not found")
                except NoRouteError as exc:
                    self._error(404, "model_not_found", str(exc))
                except ValueError as exc:
                    self._error(400, "invalid_model", str(exc))
                except UpstreamError as exc:
                    self._error(502, "catalog_unavailable", str(exc))

            def do_POST(self):
                path = urlsplit(self.path).path
                if path != "/v1/chat/completions":
                    self._error(404, "not_found", "only /v1/chat/completions is routed")
                    return
                length = int(self.headers.get("content-length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    self._error(400, "invalid_json", "request body is not JSON")
                    return
                if not isinstance(body, dict):
                    self._error(400, "invalid_request", "request body must be an object")
                    return
                try:
                    decision, directive, can_failover = proxy._route(
                        str(body.get("model") or "")
                    )
                except ValueError as exc:
                    self._error(400, "invalid_model", str(exc))
                    return
                except NoRouteError as exc:
                    self._error(404, "model_not_found", str(exc))
                    return
                except UpstreamError as exc:
                    self._error(502, "catalog_unavailable", str(exc))
                    return

                stream = bool(body.get("stream"))
                record = proxy._get_catalog().get(directive.model)
                failover_from = None
                tried: set[str] = set()
                while True:
                    adapter = proxy.adapters[decision.offer.source]
                    try:
                        connection, response, error_body = proxy._attempt(
                            adapter, decision, body, stream
                        )
                        break
                    except UpstreamError as exc:
                        if exc.budget_exceeded:
                            self.send_response(402)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("X-LLMUX-Source", decision.offer.source)
                            self.send_header("Content-Length", str(len(exc.body)))
                            self.end_headers()
                            self.wfile.write(exc.body)
                            return
                        tried.add(decision.offer.source)
                        alternate = (
                            proxy._alternate(record, decision, decision.policy, tried)
                            if exc.retryable and can_failover and record is not None
                            else None
                        )
                        if alternate is None:
                            if exc.body:
                                self.send_response(exc.status or 502)
                                self.send_header("Content-Type", "application/json")
                                self.send_header("Content-Length", str(len(exc.body)))
                                self.end_headers()
                                self.wfile.write(exc.body)
                            else:
                                self._error(502, "upstream_unavailable", str(exc))
                            return
                        failover_from = decision.offer.source
                        decision = alternate

                receipt = {
                    "X-LLMUX-Source": decision.offer.source,
                    "X-LLMUX-Policy": decision.policy,
                    "X-LLMUX-Applied-Policy": decision.applied_policy,
                    "X-LLMUX-Input-Per-M": str(decision.offer.input_per_m),
                    "X-LLMUX-Output-Per-M": str(decision.offer.output_per_m),
                    "X-LLMUX-TPS": str(decision.offer.tps_p50 or ""),
                    "X-LLMUX-Floor-Relaxed": "true" if decision.floor_relaxed else "false",
                }
                if failover_from:
                    receipt["X-LLMUX-Failover"] = failover_from
                usage: dict[str, Any] = {}
                try:
                    if stream:
                        usage = self._relay_stream(response, receipt)
                    else:
                        usage = self._relay_json(response, receipt)
                finally:
                    response.close()
                    connection.close()
                    if response.status < 400:
                        proxy.record_usage(
                            record=record,
                            decision=decision,
                            usage=usage or extract_usage(None),
                            charged_micro=response.getheader("x-si-buyer-cost-micro"),
                            stream=stream,
                            status=response.status,
                            failover_from=failover_from,
                        )

            def _relay_json(self, response, receipt):
                data = response.read()
                self.send_response(response.status)
                content_type = response.getheader("Content-Type") or "application/json"
                self.send_header("Content-Type", content_type)
                for name in ("x-request-id", "x-si-buyer-cost-micro"):
                    value = response.getheader(name)
                    if value:
                        self.send_header(name, value)
                for name, value in receipt.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                try:
                    return extract_usage(json.loads(data))
                except ValueError:
                    return extract_usage(None)

            def _relay_stream(self, response, receipt):
                self.send_response(response.status)
                content_type = response.getheader("Content-Type") or "text/event-stream"
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-cache")
                request_id = response.getheader("x-request-id")
                if request_id:
                    self.send_header("x-request-id", request_id)
                for name, value in receipt.items():
                    self.send_header(name, value)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                scanner = _UsageScanner()
                while True:
                    chunk = response.read1(STREAM_CHUNK_SIZE)
                    if not chunk:
                        break
                    scanner.feed(chunk)
                    self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                return scanner.usage

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self._server.server_port

    def stop(self) -> None:
        self._warm_stop.set()
        if self._warm_thread is not None:
            self._warm_thread.join(timeout=5)
            self._warm_thread = None
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def wait(self) -> None:
        """Block the calling thread until the server stops."""

        if self._thread is not None:
            self._thread.join()
