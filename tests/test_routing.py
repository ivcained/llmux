import unittest

from llmux_core.models import ModelRecord, Offer
from llmux_core.routing import choose_offer, parse_model_directive


def offer(source, upstream, input_price, output_price, **kwargs):
    return Offer(
        source=source,
        upstream_model=upstream,
        input_per_m=input_price,
        output_per_m=output_price,
        **kwargs,
    )


class ModelDirectiveTests(unittest.TestCase):
    def test_bare_model_uses_defaults(self):
        directive = parse_model_directive("kimi-k3.1-pro")
        self.assertEqual(directive.model, "kimi-k3.1-pro")
        self.assertIsNone(directive.source)
        self.assertIsNone(directive.policy)
        self.assertIsNone(directive.peer_id)

    def test_suffixes_pin_source_and_policy(self):
        directive = parse_model_directive("kimi-k3.1-pro@antseed@fast")
        self.assertEqual(directive.model, "kimi-k3.1-pro")
        self.assertEqual(directive.source, "antseed")
        self.assertEqual(directive.policy, "fastest")

    def test_peer_suffix_pins_one_seller(self):
        directive = parse_model_directive("kimi-k3.1-pro@antseed@peer:d19ffae0")
        self.assertEqual(directive.peer_id, "d19ffae0")
        self.assertEqual(directive.source, "antseed")

    def test_provider_suffix_pins_one_surplus_provider(self):
        directive = parse_model_directive("kimi-k3.1-pro@surplus@provider:dashscope-cn")
        self.assertEqual(directive.provider_id, "dashscope-cn")
        self.assertEqual(directive.source, "surplus")
        self.assertIsNone(directive.peer_id)

    def test_provider_suffix_requires_an_id(self):
        with self.assertRaises(ValueError):
            parse_model_directive("kimi-k3.1-pro@provider:")

    def test_peer_and_provider_pins_are_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            parse_model_directive("kimi-k3.1-pro@peer:d19ffae0@provider:dashscope-cn")


class CheapestPolicyTests(unittest.TestCase):
    def test_cheapest_uses_blended_token_price(self):
        record = ModelRecord(
            id="kimi-k3.1-pro",
            name="Kimi K3.1 Pro",
            offers=(
                offer("surplus", "kimi-k3.1-pro", 2.0, 10.0),
                offer("antseed", "peer@kimi-k3-1-pro", 0.12, 0.585),
            ),
        )
        decision = choose_offer(record, policy="cheapest")
        self.assertEqual(decision.offer.source, "antseed")
        self.assertEqual(decision.upstream_model, "peer@kimi-k3-1-pro")


class SpeedAndConstraintTests(unittest.TestCase):
    def record(self):
        return ModelRecord(
            id="kimi-k3.1-pro",
            name="Kimi K3.1 Pro",
            offers=(
                offer("surplus", "kimi-k3.1-pro", 0.2, 1.0, tps_p50=54, samples=100),
                offer("antseed", "peer@kimi-k3-1-pro", 0.12, 0.585, tps_p50=76, samples=30),
            ),
        )

    def test_fastest_prefers_measured_speed_over_price(self):
        decision = choose_offer(self.record(), policy="fastest")
        self.assertEqual(decision.offer.source, "antseed")
        self.assertEqual(decision.offer.tps_p50, 76)

    def test_fastest_blends_ttft_40_percent_and_tps_60_percent(self):
        record = ModelRecord(
            id="m",
            name="M",
            offers=(
                # 300 TPS but a 20 second wait before the first token
                offer("antseed", "a@m", 1, 2, tps_p50=300, ttft_p50_ms=20000),
                # 120 TPS but answers almost immediately
                offer("surplus", "m", 1, 2, tps_p50=120, ttft_p50_ms=400),
            ),
        )
        decision = choose_offer(record, policy="fastest")
        self.assertEqual(decision.offer.source, "surplus")

    def test_source_override_wins_over_cheapest_policy(self):
        decision = choose_offer(self.record(), policy="cheapest", source="surplus")
        self.assertEqual(decision.offer.source, "surplus")

    def test_tps_floor_filters_slower_offers(self):
        record = ModelRecord(
            id="m",
            name="M",
            offers=(
                offer("surplus", "m", 0.01, 0.02, tps_p50=20),
                offer("antseed", "a@m", 5, 10, tps_p50=76),
            ),
        )
        decision = choose_offer(record, policy="cheapest", tps_floor="high")
        self.assertEqual(decision.offer.source, "antseed")
        self.assertEqual(decision.offer.tps_p50, 76)
        self.assertFalse(decision.floor_relaxed)

    def test_unsatisfiable_floor_relaxes_to_fastest_with_receipt(self):
        record = self.record()
        decision = choose_offer(record, policy="cheapest", tps_floor="high")
        self.assertEqual(decision.offer.tps_p50, 76)
        constrained = ModelRecord(
            id=record.id,
            name=record.name,
            offers=tuple(
                offer(o.source, o.upstream_model, o.input_per_m, o.output_per_m, tps_p50=20, samples=10)
                for o in record.offers
            ),
        )
        decision = choose_offer(constrained, policy="cheapest", tps_floor="high")
        self.assertTrue(decision.floor_relaxed)
        self.assertEqual(decision.offer.source, "antseed")
        self.assertEqual(decision.policy, "cheapest")
        self.assertEqual(decision.applied_policy, "fastest")

    def test_balanced_chooses_cheapest_within_25_percent_of_fastest(self):
        record = ModelRecord(
            id="m",
            name="M",
            offers=(
                offer("surplus", "m", 5, 10, tps_p50=100),
                offer("antseed", "a@m", 2, 4, tps_p50=80),
                offer("other", "b@m", 0.1, 0.2, tps_p50=50),
            ),
        )
        decision = choose_offer(record, policy="balanced")
        self.assertEqual(decision.offer.source, "antseed")

    def test_balanced_tolerance_is_configurable(self):
        record = ModelRecord(
            id="m",
            name="M",
            offers=(
                offer("surplus", "m", 5, 10, tps_p50=100),
                offer("antseed", "a@m", 2, 4, tps_p50=80),
            ),
        )
        decision = choose_offer(record, policy="balanced", balanced_tolerance=0.1)
        self.assertEqual(decision.offer.source, "surplus")


if __name__ == "__main__":
    unittest.main()
