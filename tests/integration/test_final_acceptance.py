from __future__ import annotations

from blackholememory.product_value import build_product_value_benchmark


def test_product_value_contract_has_synthetic_disposition_and_pruning():
    report = build_product_value_benchmark(iterations=4)
    assert report["evidence_class"] == "synthetic-bounded-fixture"
    assert report["real_user_telemetry"] is False
    assert report["checks"]["pruning_recorded"] is True
    assert report["execution"]["apply_performed"] is False
