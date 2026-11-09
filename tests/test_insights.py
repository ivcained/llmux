"""Reward policies and savings insights."""

import json
import tempfile
import unittest
from pathlib import Path

from llmux_core.insights import MAX_INSIGHTS, build_insights
from llmux_core.rewards import load_table, parse_policies


def summary(**overrides):
    base = {
        "requests": 10,
        "requests_with_usage": 10,
        "usage_reported_pct": 100.0,
        "spend_usd": 2.0,
        "extra_paid_over_cheapest_usd": 0.0,
        "by_source": {"surplus": {"spend_usd": 2.0, "requests": 10}},
        "by_model": {"kimi-k3.1-pro": {"spend_usd": 0.9}, "glm-5.3-flash": {"spend_usd": 1.1}},
    }
    base.update(overrides)
    return base


class RewardPolicyTests(unittest.TestCase):
    def test_parse_and_apply_cashback(self):
        table = parse_policies(
            {"solid": {"cashback_pct": 50, "models": ["kimi-k3.1-pro"], "monthly_cap_usd": 20}}
        )
        policy = table.for_source("solid")
        assert policy is not None
        self.assertEqual(policy.cashback_for("kimi-k3.1-pro", 0.10), 0.05)
        self.assertIsNone(policy.cashback_for("other-model", 0.10))
        self.assertIsNone(policy.cashback_for("kimi-k3.1-pro", None))
        self.assertFalse(policy.confirmed)

    def test_policy_without_model_list_applies_to_all(self):
        policy = parse_policies({"surplus": {"cashback_pct": 10}}).for_source("surplus")
        assert policy is not None
        self.assertEqual(policy.cashback_for("anything", 1.0), 0.1)

    def test_garbage_input_yields_empty_table(self):
        self.assertEqual(parse_policies(["nope"]).sources(), [])
        self.assertEqual(parse_policies({"x": "not a dict"}).sources(), [])

    def test_missing_or_corrupt_file_is_empty_not_fatal(self):
        self.assertEqual(load_table("/nonexistent/rewards.json").sources(), [])
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "rewards.json"
            bad.write_text("{not json")
            self.assertEqual(load_table(bad).sources(), [])
            good = Path(tmp) / "ok.json"
            good.write_text(json.dumps({"solid": {"cashback_pct": 50, "free_models": ["deepseek-v4.1-flash"]}}))
            table = load_table(good)
            self.assertEqual(table.sources(), ["solid"])
            policy = table.for_source("solid")
            assert policy is not None
            self.assertTrue(policy.is_free("deepseek-v4.1-flash"))


class InsightTests(unittest.TestCase):
    def test_empty_ledger_says_so(self):
        insights = build_insights({"requests": 0})
        self.assertEqual([i["kind"] for i in insights], ["empty"])

    def test_cheaper_route_insight_carries_the_dollar_gap(self):
        insights = build_insights(summary(extra_paid_over_cheapest_usd=0.5))
        first = insights[0]
        self.assertEqual(first["kind"], "cheaper_route")
        self.assertEqual(first["usd"], 0.5)
        self.assertIn("25%", first["detail"])
        self.assertEqual(first["action"], {"policy": "cheapest"})

    def test_tiny_gaps_are_not_nagged_about(self):
        kinds = [i["kind"] for i in build_insights(summary(extra_paid_over_cheapest_usd=0.001))]
        self.assertNotIn("cheaper_route", kinds)

    def test_concentration_flags_a_dominant_model(self):
        s = summary(by_model={"kimi-k3.1-pro": {"spend_usd": 1.8}, "x": {"spend_usd": 0.2}})
        insight = next(i for i in build_insights(s) if i["kind"] == "concentration")
        self.assertIn("90%", insight["title"])

    def test_unconfirmed_cashback_is_labelled_an_estimate_and_capped(self):
        rewards = parse_policies({"solid": {"cashback_pct": 50, "monthly_cap_usd": 0.5}})
        insight = next(i for i in build_insights(summary(), rewards=rewards) if i["kind"] == "cashback")
        self.assertEqual(insight["usd"], 0.5)  # 50% of $2 = $1, capped at $0.50
        self.assertIn("estimate", insight["title"])
        self.assertFalse(insight["confirmed"])

    def test_cashback_source_already_used_is_not_re_suggested(self):
        rewards = parse_policies({"surplus": {"cashback_pct": 50}})
        kinds = [i["kind"] for i in build_insights(summary(), rewards=rewards)]
        self.assertNotIn("cashback", kinds)

    def test_free_models_suggested_only_when_unused(self):
        rewards = parse_policies({"solid": {"free_models": ["deepseek-v4.1-flash", "glm-5.3-flash"]}})
        titles = [i["title"] for i in build_insights(summary(), rewards=rewards) if i["kind"] == "free_model"]
        self.assertEqual(titles, ["deepseek-v4.1-flash is free on solid"])

    def test_data_gap_warns_when_usage_is_mostly_unreported(self):
        kinds = [i["kind"] for i in build_insights(summary(usage_reported_pct=40.0))]
        self.assertIn("data_gap", kinds)

    def test_money_insights_rank_first_and_list_is_capped(self):
        rewards = parse_policies(
            {"solid": {"cashback_pct": 10, "free_models": [f"free-{i}" for i in range(8)]}}
        )
        insights = build_insights(
            summary(extra_paid_over_cheapest_usd=0.9, usage_reported_pct=50.0),
            rewards=rewards,
        )
        self.assertEqual(len(insights), MAX_INSIGHTS)
        usd = [i["usd"] for i in insights]
        self.assertEqual(usd, sorted(usd, reverse=True))
        self.assertEqual(insights[0]["kind"], "cheaper_route")


if __name__ == "__main__":
    unittest.main()
