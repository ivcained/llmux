"""Live market cache: instant stale reads, background refresh, warm restart."""

import tempfile
import threading
import time
import unittest
from pathlib import Path

from llmux_core.livecache import Snapshot, record_from_dict, record_to_dict
from llmux_core.models import ModelRecord, Offer
from llmux_core.proxy import ProxyConfig, ProxyServer


def make_record(price=0.2, model="kimi-k3"):
    return ModelRecord(
        id=model,
        name=model.upper(),
        offers=(
            Offer(
                source="surplus",
                upstream_model=model,
                input_per_m=price,
                output_per_m=1.0,
                tps_p50=50,
                ttft_p50_ms=900,
                samples=40,
            ),
        ),
        aliases=(model,),
        metadata={"list_input_per_m": 2.0, "list_output_per_m": 10.0},
    )


class SlowAdapter:
    """Catalog + detail double whose fetches can be made slow or failing."""

    source = "surplus"
    base_url = "http://127.0.0.1:9/v1"
    api_key: str | None = None

    def __init__(self):
        self.price = 0.2
        self.delay = 0.0
        self.fail = False
        self.catalog_calls = 0
        self.detail_calls = 0
        self.gate = threading.Event()
        self.gate.set()

    def fetch_catalog(self):
        self.catalog_calls += 1
        self.gate.wait(5)
        time.sleep(self.delay)
        if self.fail:
            raise OSError("marketplace down")
        return [make_record(self.price)]

    def provider_offers(self, model_id):
        self.detail_calls += 1
        self.gate.wait(5)
        time.sleep(self.delay)
        if self.fail:
            raise OSError("detail down")
        return [
            Offer(
                source="surplus",
                upstream_model=model_id,
                input_per_m=self.price,
                output_per_m=1.0,
                tps_p50=60,
                ttft_p50_ms=800,
                samples=99,
                seller="InferHub",
                provider_id="inferhub",
            )
        ]


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class StaleWhileRevalidateTests(unittest.TestCase):
    def setUp(self):
        self.adapter = SlowAdapter()
        self.proxy = ProxyServer(
            adapters={"surplus": self.adapter},
            config=ProxyConfig(catalog_ttl_seconds=60, detail_ttl_seconds=60),
        )

    def expire(self):
        self.proxy._catalog_time = -1e9
        with self.proxy._detail_lock:
            for key, entry in list(self.proxy._detail_cache.items()):
                self.proxy._detail_cache[key] = (-1e9, entry[1], entry[2])

    def test_stale_read_returns_immediately_and_refreshes_behind(self):
        first = self.proxy.seller_options("kimi-k3")
        self.assertEqual(first[0]["input_per_m"], 0.2)
        self.expire()
        self.adapter.price = 0.1
        self.adapter.gate.clear()  # upstream now hangs until released
        started = time.monotonic()
        stale = self.proxy.seller_options("kimi-k3")
        self.assertLess(time.monotonic() - started, 0.5, "stale read must not wait on the network")
        self.assertEqual(stale[0]["input_per_m"], 0.2, "old price served while refreshing")
        self.adapter.gate.set()
        self.assertTrue(
            wait_until(lambda: self.proxy.seller_options("kimi-k3")[0]["input_per_m"] == 0.1),
            "background refresh lands the new price",
        )

    def test_concurrent_stale_reads_start_one_refresh_per_key(self):
        self.proxy.seller_options("kimi-k3")
        self.expire()
        before = (self.adapter.catalog_calls, self.adapter.detail_calls)
        self.adapter.gate.clear()
        for _ in range(10):
            self.proxy.seller_options("kimi-k3")
        self.adapter.gate.set()
        wait_until(lambda: not self.proxy._refreshing)
        self.assertEqual(self.adapter.catalog_calls - before[0], 1)
        self.assertEqual(self.adapter.detail_calls - before[1], 1)

    def test_failed_refresh_keeps_last_good_market(self):
        self.proxy.seller_options("kimi-k3")
        self.expire()
        self.adapter.fail = True
        self.proxy.seller_options("kimi-k3")
        wait_until(lambda: not self.proxy._refreshing)
        rows = self.proxy.seller_options("kimi-k3")
        self.assertEqual(rows[0]["seller"], "InferHub", "last good sellers survive an outage")

    def test_freshness_reports_age_and_payload_carries_it(self):
        payload = self.proxy.seller_options_payload("kimi-k3")
        fresh = payload["freshness"]
        self.assertIsNotNone(fresh["updated_at"])
        self.assertLess(fresh["age_seconds"], 5)
        self.assertIn("refreshing", fresh)


class WarmLoopTests(unittest.TestCase):
    def test_opened_models_are_watched_and_warmed(self):
        adapter = SlowAdapter()
        proxy = ProxyServer(adapters={"surplus": adapter})
        proxy.seller_options_payload("kimi-k3@surplus")
        self.assertEqual(proxy._watched, ["kimi-k3"])
        before = adapter.detail_calls
        proxy.warm_once()
        self.assertEqual(adapter.detail_calls, before + 1)

    def test_watch_list_is_bounded_and_most_recent_last(self):
        proxy = ProxyServer(adapters={}, config=ProxyConfig(warm_max_models=3))
        for model in ["a", "b", "c", "d", "b"]:
            proxy.watch(model)
        self.assertEqual(proxy._watched, ["c", "d", "b"])

    def test_warm_thread_starts_and_stops(self):
        adapter = SlowAdapter()
        proxy = ProxyServer(adapters={"surplus": adapter}, config=ProxyConfig(warm_interval_seconds=0.05))
        proxy.start_warming()
        self.assertTrue(wait_until(lambda: adapter.catalog_calls >= 2))
        proxy.stop()
        calls = adapter.catalog_calls
        time.sleep(0.2)
        self.assertEqual(adapter.catalog_calls, calls, "loop stopped")


class SnapshotTests(unittest.TestCase):
    def test_record_round_trip(self):
        record = make_record()
        self.assertEqual(record_from_dict(record_to_dict(record)), record)

    def test_restart_serves_snapshot_instantly_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.json"
            adapter = SlowAdapter()
            first = ProxyServer(adapters={"surplus": adapter}, snapshot=Snapshot(path))
            first.seller_options_payload("kimi-k3")
            self.assertTrue(path.exists())

            cold = SlowAdapter()
            cold.gate.clear()  # network hangs: only the snapshot can answer
            second = ProxyServer(adapters={"surplus": cold}, snapshot=Snapshot(path))
            started = time.monotonic()
            rows = second.seller_options("kimi-k3")
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(rows[0]["seller"], "InferHub")
            self.assertEqual(second._watched, ["kimi-k3"])
            cold.gate.set()
            # Let the background refresh the stale read started finish writing
            # before the temp dir is removed.
            self.assertTrue(wait_until(lambda: not second._refreshing))
            second.stop()
            leftovers = [p.name for p in Path(tmp).iterdir() if p.suffix == ".tmp"]
            self.assertEqual(leftovers, [], "atomic save leaves no temp files")

    def test_snapshot_holds_no_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.json"
            adapter = SlowAdapter()
            adapter.api_key = "sk-should-never-appear"
            proxy = ProxyServer(adapters={"surplus": adapter}, snapshot=Snapshot(path))
            proxy.seller_options_payload("kimi-k3")
            text = path.read_text()
            self.assertNotIn("sk-should-never-appear", text)
            self.assertNotIn("api_key", text)

    def test_corrupt_or_old_snapshot_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.json"
            path.write_text("{nope")
            self.assertEqual(Snapshot(path).load(), {})
            path.write_text('{"version": 999, "catalog": []}')
            self.assertEqual(Snapshot(path).load(), {})


if __name__ == "__main__":
    unittest.main()
