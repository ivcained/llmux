"""Usage ledger: cost math, honest nulls, rollups, privacy."""

import json
import tempfile
import unittest
from pathlib import Path

from llmux_core import ledger


def close(case: unittest.TestCase, value: float | None, expected: float, places: int = 9) -> None:
    """assertAlmostEqual that also fails loudly on an unexpected None."""

    assert value is not None, f"expected about {expected}, got None"
    case.assertAlmostEqual(value, expected, places=places)


class TokenCostTests(unittest.TestCase):
    def test_basic_token_cost(self):
        # 1000 in at $2/M + 500 out at $10/M = 0.002 + 0.005
        close(self, ledger.token_cost_usd(1000, 500, 2.0, 10.0), 0.007)

    def test_cached_input_billed_at_the_cache_rate(self):
        cost = ledger.token_cost_usd(
            1000, 0, 2.0, 10.0, cached_tokens=800, cached_input_per_m=0.2
        )
        # 200 fresh at $2/M + 800 cached at $0.2/M = 0.0004 + 0.00016
        close(self, cost, 0.00056)

    def test_missing_counts_return_none_not_zero(self):
        self.assertIsNone(ledger.token_cost_usd(None, 500, 2.0, 10.0))
        self.assertIsNone(ledger.token_cost_usd(1000, None, 2.0, 10.0))
        self.assertIsNone(ledger.token_cost_usd(1000, 500, None, 10.0))

    def test_cached_tokens_capped_at_prompt_tokens(self):
        cost = ledger.token_cost_usd(
            100, 0, 2.0, 10.0, cached_tokens=500, cached_input_per_m=0.2
        )
        close(self, cost, 100 * 0.2 / 1_000_000)


class ExtractUsageTests(unittest.TestCase):
    def test_reads_openai_usage_shape(self):
        usage = ledger.extract_usage(
            {"usage": {"prompt_tokens": 12, "completion_tokens": 7}}
        )
        self.assertEqual(usage, {"prompt_tokens": 12, "completion_tokens": 7, "cached_tokens": None})

    def test_reads_cached_token_detail(self):
        usage = ledger.extract_usage(
            {
                "usage": {
                    "prompt_tokens": 1200,
                    "completion_tokens": 30,
                    "prompt_tokens_details": {"cached_tokens": 1000},
                }
            }
        )
        self.assertEqual(usage["cached_tokens"], 1000)

    def test_absent_usage_is_all_none(self):
        self.assertEqual(
            ledger.extract_usage({"choices": []}),
            {"prompt_tokens": None, "completion_tokens": None, "cached_tokens": None},
        )
        self.assertEqual(
            ledger.extract_usage(None),
            {"prompt_tokens": None, "completion_tokens": None, "cached_tokens": None},
        )

    def test_micro_header_conversion(self):
        close(self, ledger.cost_from_micro_header("1234567"), 1.234567, places=6)
        self.assertIsNone(ledger.cost_from_micro_header(None))
        self.assertIsNone(ledger.cost_from_micro_header("not-a-number"))


class ComputeCostsTests(unittest.TestCase):
    def test_authoritative_charge_wins_over_estimate(self):
        costs = ledger.compute_costs(
            usage={"prompt_tokens": 1000, "completion_tokens": 500, "cached_tokens": None},
            input_per_m=2.0,
            output_per_m=10.0,
            cached_input_per_m=None,
            charged_micro_header="5000",  # marketplace says it charged $0.005
            list_input_per_m=4.0,
            list_output_per_m=20.0,
            alternative_offers=[(1.0, 5.0), (8.0, 40.0)],
        )
        close(self, costs.actual_usd, 0.005, places=6)
        self.assertTrue(costs.authoritative)
        close(self, costs.list_usd, 0.014, places=6)
        close(self, costs.savings_vs_list, 0.009, places=6)
        close(self, costs.cheapest_usd, 0.0035, places=6)
        close(self, costs.priciest_usd, 0.028, places=6)
        close(self, costs.savings_vs_priciest, 0.023, places=6)
        close(self, costs.missed_saving_vs_cheapest, 0.0015, places=6)

    def test_estimate_used_when_no_charge_header(self):
        costs = ledger.compute_costs(
            usage={"prompt_tokens": 1000, "completion_tokens": 500, "cached_tokens": None},
            input_per_m=2.0,
            output_per_m=10.0,
            cached_input_per_m=None,
            charged_micro_header=None,
            list_input_per_m=None,
            list_output_per_m=None,
        )
        close(self, costs.actual_usd, 0.007, places=6)
        self.assertFalse(costs.authoritative)
        self.assertIsNone(costs.list_usd)
        self.assertIsNone(costs.savings_vs_list)

    def test_unreported_usage_yields_no_costs(self):
        costs = ledger.compute_costs(
            usage={"prompt_tokens": None, "completion_tokens": None, "cached_tokens": None},
            input_per_m=2.0,
            output_per_m=10.0,
            cached_input_per_m=None,
            charged_micro_header=None,
            list_input_per_m=4.0,
            list_output_per_m=20.0,
            alternative_offers=[(1.0, 5.0)],
        )
        self.assertIsNone(costs.actual_usd)
        self.assertIsNone(costs.list_usd)
        self.assertIsNone(costs.cheapest_usd)


class LedgerIoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "nested" / "usage.jsonl"
        self.ledger = ledger.Ledger(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def _row(self, **overrides):
        row = {
            "model": "kimi-k3.1-pro",
            "source": "surplus",
            "seller": "InferHub",
            "actual_usd": 0.01,
            "list_cost_usd": 0.04,
            "cheapest_cost_usd": 0.008,
            "priciest_cost_usd": 0.09,
            "savings_vs_list_usd": 0.03,
            "cashback_usd": 0.0,
            "usage_reported": True,
        }
        row.update(overrides)
        return row

    def test_record_appends_and_rereads(self):
        self.ledger.record(self._row())
        self.ledger.record(self._row(source="antseed", actual_usd=0.02))
        fresh = ledger.Ledger(self.path)
        rows = fresh.entries()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["source"] for row in rows}, {"surplus", "antseed"})
        self.assertTrue(all(isinstance(row["ts"], float) for row in rows))

    def test_summary_rolls_up_spend_and_savings(self):
        self.ledger.record(self._row())
        self.ledger.record(self._row(source="antseed", actual_usd=0.02, list_cost_usd=0.05,
                                     savings_vs_list_usd=0.03, cashback_usd=0.01))
        summary = self.ledger.summary(days=30)
        self.assertEqual(summary["requests"], 2)
        close(self, summary["spend_usd"], 0.03, places=6)
        close(self, summary["list_spend_usd"], 0.09, places=6)
        close(self, summary["savings_vs_list_usd"], 0.06, places=6)
        close(self, summary["cashback_usd"], 0.01, places=6)
        close(self, summary["effective_spend_after_cashback_usd"], 0.02, places=6)
        self.assertEqual(summary["by_source"]["surplus"]["requests"], 1)
        self.assertEqual(summary["by_model"]["kimi-k3.1-pro"]["requests"], 2)

    def test_rows_without_usage_are_counted_but_flagged(self):
        self.ledger.record(self._row())
        self.ledger.record(self._row(usage_reported=False, actual_usd=None, list_cost_usd=None))
        summary = self.ledger.summary(days=30)
        self.assertEqual(summary["requests"], 2)
        self.assertEqual(summary["requests_with_usage"], 1)
        self.assertEqual(summary["usage_reported_pct"], 50.0)

    def test_window_excludes_old_rows(self):
        self.ledger.record(self._row())
        self.ledger.record(self._row(actual_usd=5.0))
        # Age the second row out of a 1-day window by rewriting the file.
        rows = [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]
        rows[1]["ts"] = rows[1]["ts"] - 10 * 86400
        self.path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        summary = ledger.Ledger(self.path).summary(days=1)
        self.assertEqual(summary["requests"], 1)
        close(self, summary["spend_usd"], 0.01, places=6)

    def test_corrupt_lines_are_skipped(self):
        self.ledger.record(self._row())
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("{not json}\n\n")
        self.ledger.record(self._row(source="antseed"))
        self.assertEqual(len(ledger.Ledger(self.path).entries()), 2)

    def test_recent_filters_by_model_and_caps(self):
        for i in range(5):
            self.ledger.record(self._row(model=f"m{i}"))
        self.assertEqual(len(self.ledger.recent(limit=2)), 2)
        self.assertEqual(self.ledger.recent(model="m3")[0]["model"], "m3")
        self.assertEqual(self.ledger.recent(model="nope"), [])

    def test_stored_rows_carry_no_prompt_or_secret_fields(self):
        self.ledger.record(self._row())
        text = self.path.read_text().lower()
        for banned in ("messages", "prompt_text", "api_key", "authorization", "bearer", "private_key"):
            self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()
