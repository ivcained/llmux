import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from llmux_core.catalog import Catalog
from llmux_core.models import ModelRecord, Offer
from llmux_core.proxy import ProxyConfig, ProxyServer


def record(source_price_in=0.2, source_price_out=1.0, source_tps=54):
    return ModelRecord(
        id="kimi-k3.1-pro",
        name="Kimi K3.1 Pro",
        metadata={"list_input_per_m": 2.0, "list_output_per_m": 10.0},
        offers=(
            Offer(
                source="surplus",
                upstream_model="kimi-k3.1-pro",
                input_per_m=source_price_in,
                output_per_m=source_price_out,
                cached_input_per_m=0.02,
                tps_p50=source_tps,
                ttft_p50_ms=7487,
                samples=100,
            ),
            Offer(
                source="antseed",
                upstream_model="peer-1@kimi-k3-1-pro",
                input_per_m=0.12,
                output_per_m=0.585,
                tps_p50=76,
                ttft_p50_ms=17488,
                samples=30,
                reputation=77.1,
                peer_id="peer-1",
            ),
        ),
    )


class MockUpstream:
    def __init__(self, mode="ok", source="surplus"):
        self.mode = mode
        self.source = source
        self.requests = []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length)
                upstream.requests.append(
                    {"path": self.path, "body": json.loads(body), "headers": dict(self.headers)}
                )
                if upstream.mode in {"fail", "budget", "credit"}:
                    if upstream.mode == "fail":
                        status, error = 503, "upstream_unavailable"
                    elif upstream.mode == "budget":
                        status, error = 402, "budget_exceeded"
                    else:
                        status, error = 402, "insufficient_credit"
                    payload = json.dumps({"error": {"code": error, "message": error}}).encode()
                    self.send_response(status)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if upstream.mode in {"sse", "partial"}:
                    done = b"data: [DONE]\n\n" if upstream.mode == "sse" else b""
                    payload = (
                        b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n' + done
                    )
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("x-request-id", f"{upstream.source}-stream")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                model = upstream.requests[-1]["body"].get("model")
                payload = json.dumps(
                    {
                        "id": f"{upstream.source}-completion",
                        "object": "chat.completion",
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": upstream.source},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("x-request-id", f"{upstream.source}-request")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class FakeAdapter:
    api_key = None

    def __init__(self, source, base_url, records):
        self.source = source
        self.base_url = base_url
        self._records = records

    def fetch_catalog(self):
        return self._records


class FakeDetailAdapter(FakeAdapter):
    """Surplus adapter double that also serves named per-provider offers."""

    def __init__(self, source, base_url, records, detail=None, detail_error=None):
        super().__init__(source, base_url, records)
        self._detail = list(detail or [])
        self._detail_error = detail_error
        self.detail_calls = []

    def provider_offers(self, model_id):
        self.detail_calls.append(model_id)
        if self._detail_error is not None:
            raise self._detail_error
        return list(self._detail)

    @staticmethod
    def build_extra_body(decision):
        provider_id = getattr(decision.offer, "provider_id", None)
        return {"provider_order": [provider_id]} if provider_id else {}


def detail_offers():
    """Two named Surplus providers, shaped like the live join produces."""

    return [
        Offer(
            source="surplus",
            upstream_model="kimi-k3.1-pro",
            input_per_m=0.3,
            output_per_m=1.2,
            tps_p50=73.3,
            ttft_p50_ms=4108,
            samples=736,
            seller="Alibaba DashScope \u2014 China",
            provider_id="dashscope-cn",
        ),
        Offer(
            source="surplus",
            upstream_model="kimi-k3.1-pro",
            input_per_m=0.25,
            output_per_m=1.1,
            tps_p50=None,
            ttft_p50_ms=33570,
            samples=2,
            seller="InferHub",
            provider_id="inferhub",
        ),
    ]


class ProxyCase(unittest.TestCase):
    surplus_mode = "ok"
    antseed_mode = "ok"
    surplus_cheap = False
    config = ProxyConfig(policy="cheapest")

    def setUp(self):
        self.surplus = MockUpstream(self.surplus_mode, "surplus")
        self.antseed = MockUpstream(self.antseed_mode, "antseed")
        catalog_record = (
            record(0.01, 0.02, 54) if self.surplus_cheap else record()
        )
        surplus_record = replace(
            catalog_record,
            offers=tuple(
                offer for offer in catalog_record.offers if offer.source == "surplus"
            ),
        )
        antseed_record = replace(
            catalog_record,
            offers=tuple(
                offer for offer in catalog_record.offers if offer.source == "antseed"
            ),
        )
        self.surplus_records = [surplus_record]
        self.antseed_records = [antseed_record]
        self.proxy = ProxyServer(
            adapters={
                "surplus": FakeAdapter("surplus", self.surplus.base_url, self.surplus_records),
                "antseed": FakeAdapter("antseed", self.antseed.base_url, self.antseed_records),
            },
            config=self.config,
        )
        self.port = self.proxy.start(port=0)

    def tearDown(self):
        self.proxy.stop()
        self.surplus.close()
        self.antseed.close()

    def request_json(self, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers={"content-type": "application/json", **(headers or {})},
            method="POST" if body is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), json.loads(response.read())

    def completions(self, model="kimi-k3.1-pro", stream=False, headers=None):
        return self.request_json(
            "/v1/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": stream,
            },
            headers,
        )

    def completions_raw(self, model="kimi-k3.1-pro", stream=True, headers=None):
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": stream,
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json", **(headers or {})},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read().decode()


class CatalogEndpointTests(ProxyCase):
    def test_models_endpoint_returns_union(self):
        status, _, body = self.request_json("/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual([model["id"] for model in body["data"]], ["kimi-k3.1-pro"])
        self.assertEqual(body["data"][0]["sources"], ["antseed", "surplus"])

    def test_remote_bind_is_rejected_by_default(self):
        with self.assertRaises(ValueError):
            ProxyServer(adapters={}).start(host="0.0.0.0", port=0)


class NonStreamingRoutingTests(ProxyCase):
    def test_cheapest_routes_to_antseed_and_rewrites_pinned_model(self):
        status, headers, body = self.completions()
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-LLMUX-Source"], "antseed")
        self.assertEqual(headers["X-LLMUX-Input-Per-M"], "0.12")
        self.assertEqual(headers["X-LLMUX-Output-Per-M"], "0.585")
        self.assertEqual(headers["X-LLMUX-TPS"], "76")
        self.assertEqual(body["model"], "peer-1@kimi-k3-1-pro")
        self.assertEqual(len(self.surplus.requests), 0)
        self.assertEqual(len(self.antseed.requests), 1)

    def test_peer_directive_pins_exact_seller(self):
        status, headers, body = self.completions(model="kimi-k3.1-pro@antseed@peer:peer-1")
        self.assertEqual(status, 200)
        self.assertEqual(body["model"], "peer-1@kimi-k3-1-pro")
        self.assertEqual(headers["X-LLMUX-Source"], "antseed")

    def test_peer_directive_rejects_unknown_seller(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.completions(model="kimi-k3.1-pro@antseed@peer:does-not-exist")
        self.assertEqual(caught.exception.code, 404)

    def test_source_suffix_pins_surplus(self):
        status, headers, _ = self.completions(model="kimi-k3.1-pro@surplus")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-LLMUX-Source"], "surplus")
        self.assertEqual(len(self.surplus.requests), 1)
        self.assertEqual(len(self.antseed.requests), 0)


class FloorHeaderTests(ProxyCase):
    surplus_cheap = True
    config = ProxyConfig(policy="cheapest", tps_floor="high")

    def test_floor_header_excludes_slow_cheaper_offer(self):
        status, headers, _ = self.completions(model="kimi-k3.1-pro")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-LLMUX-Source"], "antseed")


class StreamingTests(ProxyCase):
    antseed_mode = "sse"

    def test_stream_is_passed_through_with_receipt(self):
        headers = {"accept": "text/event-stream"}
        status, response_headers, body = self.completions_raw(stream=True, headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(response_headers["X-LLMUX-Source"], "antseed")
        self.assertIn("hello", body)
        self.assertIn("[DONE]", body)


class FailoverTests(ProxyCase):
    surplus_mode = "fail"
    surplus_cheap = True

    def test_retryable_failure_before_first_byte_fails_over_once(self):
        status, headers, body = self.completions()
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-LLMUX-Source"], "antseed")
        self.assertEqual(headers["X-LLMUX-Failover"], "surplus")
        self.assertEqual(len(self.surplus.requests), 1)
        self.assertEqual(len(self.antseed.requests), 1)

    def test_budget_cap_never_fails_over(self):
        self.surplus.mode = "budget"
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.completions()
        self.assertEqual(caught.exception.code, 402)
        self.assertEqual(len(self.antseed.requests), 0)

    def test_insufficient_credit_can_fail_over(self):
        self.surplus.mode = "credit"
        status, headers, _ = self.completions()
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-LLMUX-Failover"], "surplus")

    def test_explicit_source_pin_never_fails_over(self):
        self.surplus.mode = "credit"
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.completions(model="kimi-k3.1-pro@surplus")
        self.assertEqual(caught.exception.code, 402)
        self.assertEqual(len(self.antseed.requests), 0)


class PartialStreamTests(ProxyCase):
    surplus_mode = "partial"
    surplus_cheap = True

    def test_partial_stream_is_never_replayed(self):
        headers = {"accept": "text/event-stream"}
        status, response_headers, body = self.completions_raw(stream=True, headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(response_headers["X-LLMUX-Source"], "surplus")
        self.assertNotIn("[DONE]", body)
        self.assertIn("hello", body)
        self.assertEqual(len(self.antseed.requests), 0)


class SellerOptionsTests(ProxyCase):
    def test_options_rank_sellers_by_estimated_reply_time(self):
        options = self.proxy.seller_options("kimi-k3.1-pro", limit=4)
        self.assertEqual(len(options), 2)
        self.assertEqual(options[0]["source"], "surplus")
        self.assertEqual(options[0]["ttft_p50_ms"], 7487)
        self.assertEqual(options[0]["estimated_reply_seconds"], 26.0)
        self.assertEqual(options[0]["upstream_model"], "kimi-k3.1-pro")
        self.assertIsNone(options[0]["cache_warning"]["prompt_tokens"])
        self.assertIsNone(options[0]["cache_warning"]["uncached_input_cost_usd"])

    def test_options_carry_discount_against_list_price(self):
        options = self.proxy.seller_options("kimi-k3.1-pro", limit=4)
        by_source = {option["source"]: option for option in options}
        # Surplus blended 0.36 vs list blended 3.6 -> 90% off.
        self.assertEqual(by_source["surplus"]["discount_pct"], 90)
        # Antseed blended 0.213 vs list blended 3.6 -> 94% off.
        self.assertEqual(by_source["antseed"]["discount_pct"], 94)

    def test_options_without_list_price_report_no_discount(self):
        self.proxy._catalog = None
        self.proxy._catalog_time = 0.0
        bare = ModelRecord(
            id="kimi-k3.1-pro",
            name="Kimi K3.1 Pro",
            offers=tuple(
                Offer(
                    source=o.source,
                    upstream_model=o.upstream_model,
                    input_per_m=o.input_per_m,
                    output_per_m=o.output_per_m,
                    tps_p50=o.tps_p50,
                    ttft_p50_ms=o.ttft_p50_ms,
                    samples=o.samples,
                    peer_id=o.peer_id,
                )
                for o in record().offers
            ),
        )
        self.proxy._catalog = Catalog.merge([[bare]])
        self.proxy._catalog_time = time.monotonic() + 999
        options = self.proxy.seller_options("kimi-k3.1-pro")
        self.assertTrue(options)
        self.assertTrue(all(option["discount_pct"] is None for option in options))

    def test_plan_endpoint_validates_exact_pin_without_inference(self):
        status, _, body = self.request_json('/llmux/plan?model=kimi-k3.1-pro@antseed@peer:peer-1')
        self.assertEqual(status, 200)
        self.assertEqual(body['upstream_model'], 'peer-1@kimi-k3-1-pro')
        self.assertFalse(body['failover_allowed'])
        self.assertEqual(self.antseed.requests, [])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request_json('/llmux/plan?model=kimi-k3.1-pro@antseed@peer:missing')
        self.assertEqual(caught.exception.code, 404)

    def test_options_unknown_model_is_empty(self):
        self.assertEqual(self.proxy.seller_options("missing"), [])

    def test_seller_options_http_endpoint(self):
        status, _, body = self.request_json("/llmux/seller-options?model=kimi-k3.1-pro")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["data"]), 2)
        self.assertEqual(body["data"][0]["source"], "surplus")


def similar_record(model_id, name, price_in, price_out, tps, ttft_ms, *, source="surplus",
                   list_in=None, list_out=None, samples=100, seller="marketplace"):
    metadata = {}
    if list_in is not None and list_out is not None:
        metadata = {"list_input_per_m": list_in, "list_output_per_m": list_out}
    return ModelRecord(
        id=model_id,
        name=name,
        metadata=metadata,
        offers=(
            Offer(
                source=source,
                upstream_model=model_id,
                input_per_m=price_in,
                output_per_m=price_out,
                tps_p50=tps,
                ttft_p50_ms=ttft_ms,
                samples=samples,
                seller=seller,
            ),
        ),
    )


class SimilarModelsTests(unittest.TestCase):
    """Mid-chat model switching: comparable price, measured speed."""

    def setUp(self):
        self.records = [
            # current model: list blend 3.6, routed $0.20/$1.00, 54 tok/s
            similar_record("kimi-k3.1-pro", "Kimi K3.1 Pro", 0.2, 1.0, 54, 7487,
                           list_in=2.0, list_out=10.0),
            # same tier (list blend 2.48 -> 0.69x) and faster -> suggested first
            similar_record("qwen3.8-flash", "Qwen3.8 Flash", 0.12, 0.5, 73, 4108,
                           list_in=1.6, list_out=6.0, seller="Alibaba DashScope"),
            # same tier (list blend 3.72 -> 1.03x), moderately faster -> second
            similar_record("glm-5.3-flash", "GLM 5.3 Flash", 0.18, 0.9, 61, 2300,
                           list_in=2.4, list_out=9.0, seller="Zhipu"),
            # 4x the tier price -> outside the band
            similar_record("gpt-5.6-luna", "GPT 5.6 Luna", 4.0, 20.0, 90, 900,
                           list_in=8.0, list_out=40.0),
            # tiny tier, absurd measured speed -> neither comparable nor credible
            similar_record("tiny-7b", "Tiny 7B", 0.001, 0.004, 900, 200,
                           list_in=0.01, list_out=0.03),
            # same tier but an implausible 6000 tok/s artifact -> excluded
            similar_record("artifact-model", "Artifact", 0.2, 1.0, 6000, 300,
                           list_in=2.0, list_out=10.0),
            # no published list price -> not comparable against a list-priced model
            similar_record("no-list-model", "No List", 0.2, 1.0, 80, 500),
            # no speed measurements -> cannot be compared honestly, excluded
            similar_record("mystery-model", "Mystery", 0.2, 1.0, None, None),
        ]
        adapter = FakeAdapter("surplus", "http://127.0.0.1:1/v1", self.records)
        self.proxy = ProxyServer(adapters={"surplus": adapter}, config=ProxyConfig(policy="balanced"))

    def test_suggests_comparable_models_ranked_by_speed(self):
        rows = self.proxy.similar_models("kimi-k3.1-pro")
        self.assertEqual([row["model"] for row in rows], ["qwen3.8-flash", "glm-5.3-flash"])
        fastest = rows[0]
        self.assertEqual(fastest["tps_p50"], 73)
        self.assertEqual(fastest["ttft_p50_ms"], 4108)
        self.assertEqual(fastest["seller"], "Alibaba DashScope")
        self.assertEqual(fastest["switch_model"], "qwen3.8-flash")
        self.assertEqual(fastest["input_per_m"], 0.12)
        self.assertEqual(fastest["price_basis"], "list")

    def test_rows_carry_discount_and_price_ratio(self):
        rows = {row["model"]: row for row in self.proxy.similar_models("kimi-k3.1-pro")}
        qwen = rows["qwen3.8-flash"]
        # offer blend 0.196 against its own list blend 2.48 -> 92% off
        self.assertEqual(qwen["discount_pct"], 92)
        # tier ratio: list blend 2.48 vs current list blend 3.6 -> 0.69
        self.assertEqual(qwen["price_ratio"], 0.69)

    def test_far_cheaper_small_models_are_not_comparable(self):
        models = [row["model"] for row in self.proxy.similar_models("kimi-k3.1-pro")]
        self.assertNotIn("tiny-7b", models)
        self.assertNotIn("gpt-5.6-luna", models)
        self.assertNotIn("mystery-model", models)

    def test_implausible_speed_is_not_a_performance_claim(self):
        models = [row["model"] for row in self.proxy.similar_models("kimi-k3.1-pro")]
        self.assertNotIn("artifact-model", models)

    def test_different_price_basis_is_not_compared(self):
        models = [row["model"] for row in self.proxy.similar_models("kimi-k3.1-pro")]
        self.assertNotIn("no-list-model", models)

    def test_free_current_route_still_gets_suggestions_via_list_tier(self):
        records = [
            similar_record("free-route", "Free Route", 0.0, 0.0, 50, 800,
                           list_in=2.0, list_out=10.0, seller="Open Forge"),
            similar_record("qwen3.8-flash", "Qwen3.8 Flash", 0.12, 0.5, 73, 4108,
                           list_in=1.6, list_out=6.0),
        ]
        proxy = ProxyServer(
            adapters={"surplus": FakeAdapter("surplus", "http://127.0.0.1:1/v1", records)},
            config=ProxyConfig(policy="balanced"),
        )
        rows = proxy.similar_models("free-route")
        self.assertEqual([row["model"] for row in rows], ["qwen3.8-flash"])
        self.assertEqual(rows[0]["price_basis"], "list")

    def test_current_model_is_never_suggested_against_itself(self):
        self.assertNotIn("kimi-k3.1-pro", [r["model"] for r in self.proxy.similar_models("kimi-k3.1-pro")])

    def test_limit_is_respected(self):
        self.assertEqual(len(self.proxy.similar_models("kimi-k3.1-pro", limit=1)), 1)

    def test_unknown_model_returns_empty(self):
        self.assertEqual(self.proxy.similar_models("does-not-exist"), [])

    def test_http_endpoint_serves_similar_models(self):
        port = self.proxy.start(port=0)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/llmux/similar-models?model=kimi-k3.1-pro", timeout=10
            ) as response:
                body = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(body["data"][0]["model"], "qwen3.8-flash")
        finally:
            self.proxy.stop()


class SurplusProviderDetailTests(ProxyCase):
    """Named Surplus providers replace the anonymous aggregate route."""

    def setUp(self):
        super().setUp()
        self.detail = FakeDetailAdapter(
            "surplus", self.surplus.base_url, self.surplus_records, detail=detail_offers()
        )
        self.proxy.adapters["surplus"] = self.detail
        self.proxy._detail_cache.clear()

    def test_options_name_real_surplus_providers(self):
        options = self.proxy.seller_options("kimi-k3.1-pro", limit=4)
        sellers = {option["seller"] for option in options}
        self.assertIn("Alibaba DashScope \u2014 China", sellers)
        self.assertIn("InferHub", sellers)
        self.assertNotIn("marketplace", sellers)
        dashscope = next(o for o in options if o["provider_id"] == "dashscope-cn")
        self.assertEqual(dashscope["tps_p50"], 73.3)
        self.assertEqual(dashscope["ttft_p50_ms"], 4108)
        self.assertEqual(dashscope["samples"], 736)
        self.assertEqual(dashscope["source"], "surplus")

    def test_detail_is_cached_per_model(self):
        self.proxy.seller_options("kimi-k3.1-pro")
        self.proxy.seller_options("kimi-k3.1-pro")
        self.assertEqual(self.detail.detail_calls, ["kimi-k3.1-pro"])

    def test_detail_failure_falls_back_to_labelled_aggregate(self):
        self.proxy.adapters["surplus"] = FakeDetailAdapter(
            "surplus", self.surplus.base_url, self.surplus_records, detail_error=OSError("detail down")
        )
        self.proxy._detail_cache.clear()
        options = self.proxy.seller_options("kimi-k3.1-pro")
        surplus_rows = [option for option in options if option["source"] == "surplus"]
        self.assertEqual(len(surplus_rows), 1)
        self.assertEqual(surplus_rows[0]["seller"], "Surplus auto")
        self.assertIsNone(surplus_rows[0]["provider_id"])

    def test_pinned_provider_is_sent_upstream_with_provider_order(self):
        status, headers, _ = self.completions(model="kimi-k3.1-pro@surplus@provider:dashscope-cn")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-LLMUX-Source"], "surplus")
        self.assertEqual(len(self.surplus.requests), 1)
        sent = self.surplus.requests[0]["body"]
        self.assertEqual(sent["provider_order"], ["dashscope-cn"])
        self.assertEqual(sent["model"], "kimi-k3.1-pro")
        self.assertEqual(self.antseed.requests, [])

    def test_unpinned_request_carries_no_provider_order(self):
        status, _, _ = self.completions(model="kimi-k3.1-pro@surplus")
        self.assertEqual(status, 200)
        self.assertNotIn("provider_order", self.surplus.requests[0]["body"])

    def test_unknown_provider_pin_is_refused_without_inference(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request_json("/llmux/plan?model=kimi-k3.1-pro@surplus@provider:nope")
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(self.surplus.requests, [])

    def test_paid_route_never_claims_a_full_discount(self):
        cheap = Offer(
            source="surplus",
            upstream_model="kimi-k3.1-pro",
            input_per_m=0.0001,
            output_per_m=0.0002,
            tps_p50=90.0,
            ttft_p50_ms=500,
            samples=50,
            seller="Too Good",
            provider_id="too-good",
        )
        self.proxy.adapters["surplus"] = FakeDetailAdapter(
            "surplus", self.surplus.base_url, self.surplus_records, detail=[cheap]
        )
        self.proxy._detail_cache.clear()
        options = self.proxy.seller_options("kimi-k3.1-pro")
        row = next(o for o in options if o["provider_id"] == "too-good")
        self.assertEqual(row["discount_pct"], 99)


if __name__ == "__main__":
    unittest.main()
