import json
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from llmux_core.models import ModelRecord, Offer
from llmux_core.proxy import ProxyConfig, ProxyServer


def record(source_price_in=0.2, source_price_out=1.0, source_tps=54):
    return ModelRecord(
        id="kimi-k3.1-pro",
        name="Kimi K3.1 Pro",
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
        self.proxy = ProxyServer(
            adapters={
                "surplus": FakeAdapter("surplus", self.surplus.base_url, [surplus_record]),
                "antseed": FakeAdapter("antseed", self.antseed.base_url, [antseed_record]),
            },
            config=self.config,
        )
        self.port = self.proxy.start()

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
        self.assertEqual(options[0]["cache_warning"]["prompt_tokens"], 8000)
        self.assertAlmostEqual(
            options[0]["cache_warning"]["uncached_input_cost_usd"], 0.0016
        )

    def test_options_unknown_model_is_empty(self):
        self.assertEqual(self.proxy.seller_options("missing"), [])

    def test_seller_options_http_endpoint(self):
        status, _, body = self.request_json("/llmux/seller-options?model=kimi-k3.1-pro")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["data"]), 2)
        self.assertEqual(body["data"][0]["source"], "surplus")


if __name__ == "__main__":
    unittest.main()
