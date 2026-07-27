from __future__ import annotations

from blackholememory.product_value import build_product_value_benchmark
from blackholememory.product_value import verify_product_value_digest


def test_product_value_benchmark_is_positive_and_deterministic():
    report = build_product_value_benchmark(iterations=16)
    assert report["decision"] == "ship-current-scope"
    assert report["utility_score"] > 0
    assert verify_product_value_digest(report)


def test_product_value_benchmark_prunes_unsafe_optional_features():
    report = build_product_value_benchmark(iterations=1)
    decisions = {row["feature"]: row["decision"] for row in report["pruning"]}
    assert decisions["autonomous_apply"] == "prune-disabled"
    assert decisions["training_lora_qlora"] == "prune-disabled"
    assert report["checks"]["single_authority"] is True
