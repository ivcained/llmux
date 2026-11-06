"""Local OpenAI-compatible routing proxy for LLMUX."""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .catalog import Catalog
from .models import ModelDirective, ModelRecord, RouteDecision
from .routing import NoRouteError, choose_offer, parse_model_directive

LOGGER = logging.getLogger("llmux.proxy")
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
MAX_ERROR_BODY = 65536
STREAM_CHUNK_SIZE = 8192


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
    upstream_timeout_seconds: float = 300.0
    allow_remote: bool = False


class ProxyServer:
    """HTTP proxy that routes OpenAI chat requests across marketplace adapters."""

    def __init__(self, adapters: dict[str, Any], config: ProxyConfig | None = None) -> None:
        self.adapters = adapters
        self.config = config or ProxyConfig()
        self._catalog: Catalog | None = None
        self._catalog_time = 0.0
        self._catalog_lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.source_errors: dict[str, str] = {}

    def _get_catalog(self) -> Catalog:
        with self._catalog_lock:
            age = time.monotonic() - self._catalog_time
            if self._catalog is not None and age < self.config.catalog_ttl_seconds:
                return self._catalog
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
            if self._catalog is None:
                raise UpstreamError("no marketplace catalog is available", retryable=True)
            return self._catalog

    def _route(self, model: str) -> tuple[RouteDecision, ModelDirective, bool]:
        directive = parse_model_directive(model)
        catalog = self._get_catalog()
        record = catalog.get(directive.model)
        if record is None:
            raise NoRouteError(f"unknown model: {directive.model}")
        policy = directive.policy or self.config.policy
        decision = choose_offer(
            record,
            policy=policy,
            source=directive.source,
            tps_floor=self.config.tps_floor,
            balanced_tolerance=self.config.balanced_tolerance,
        )
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
        return decision, directive, directive.source is None and directive.peer_id is None

    def _alternate(
        self, record: ModelRecord, decision: RouteDecision, policy: str
    ) -> RouteDecision | None:
        candidates = tuple(
            offer
            for offer in record.offers
            if offer.source != decision.offer.source
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
        body = json.dumps({**payload, "model": decision.upstream_model}).encode()
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

    def seller_options(self, model: str, *, limit: int = 4) -> list[dict]:
        """Return ranked seller options with timings and an honest cache warning."""

        record = self._get_catalog().get(model)
        if record is None:
            return []
        offers = [
            offer for offer in record.offers
            if offer.healthy and (offer.tps_p50 or offer.ttft_p50_ms)
        ]
        if not offers:
            return []
        max_tps = max((offer.tps_p50 or 0.0) for offer in offers) or 1.0
        min_ttft = min(
            (offer.ttft_p50_ms for offer in offers if offer.ttft_p50_ms is not None),
            default=1.0,
        )

        def score(offer) -> float:
            tps_component = (offer.tps_p50 or 0.0) / max_tps
            ttft_component = (
                min_ttft / max(offer.ttft_p50_ms, 1.0)
                if offer.ttft_p50_ms is not None
                else 0.0
            )
            return 0.6 * tps_component + 0.4 * ttft_component

        ranked = sorted(offers, key=score, reverse=True)[:limit]
        result = []
        for offer in ranked:
            ttft_s = (offer.ttft_p50_ms or 0) / 1000.0
            estimated_reply_seconds = (
                ttft_s + 1000.0 / offer.tps_p50 if offer.tps_p50 else ttft_s
            )
            result.append(
                {
                    "source": offer.source,
                    "seller": offer.seller or offer.source,
                    "peer_id": offer.peer_id,
                    "upstream_model": offer.upstream_model,
                    "tps_p50": offer.tps_p50,
                    "ttft_p50_ms": offer.ttft_p50_ms,
                    "samples": offer.samples,
                    "input_per_m": offer.input_per_m,
                    "output_per_m": offer.output_per_m,
                    "estimated_reply_seconds": round(estimated_reply_seconds, 1),
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
                        raw_query = urlsplit(self.path).query
                        model = parse_qs(raw_query).get("model", [""])[0]
                        self._json(
                            200,
                            {"object": "list", "data": proxy.seller_options(model)},
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
                        alternate = (
                            proxy._alternate(record, decision, decision.policy)
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
                try:
                    if stream:
                        self._relay_stream(response, receipt)
                    else:
                        self._relay_json(response, receipt)
                finally:
                    response.close()
                    connection.close()

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
                while True:
                    chunk = response.read1(STREAM_CHUNK_SIZE)
                    if not chunk:
                        break
                    self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self._server.server_port

    def stop(self) -> None:
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
