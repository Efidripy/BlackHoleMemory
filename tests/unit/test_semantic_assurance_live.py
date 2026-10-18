from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import blackholememory.semantic_assurance_live as live


FIXTURE = Path(__file__).parents[1] / "fixtures" / "semantic-assurance-v1.json"


def _response(payload: Any) -> httpx.Response:
    return httpx.Response(200, request=httpx.Request("GET", "http://127.0.0.1:13666"), json=payload)


def _inventory() -> dict[str, object]:
    return {
        "models": [{
            "type": "llm",
            "key": live.DEFAULT_MODEL_ID,
            "selected_variant": live.DEFAULT_MODEL_VARIANT,
            "loaded_instances": [{"config": {"context_length": 8192, "parallel": 1}}],
        }]
    }


def _valid_result(case_id: str) -> dict[str, object]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    case = next(item for item in raw["cases"] if item["case_id"] == case_id)
    return dict(case["expected"])


def test_live_adapter_uses_only_inventory_and_chat_and_emits_content_free_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, path: str) -> httpx.Response:
            calls.append(("GET", path))
            return _response(_inventory())

        def post(self, path: str, *, json: dict[str, Any]) -> httpx.Response:
            calls.append(("POST", path))
            assert json["model"] == live.DEFAULT_MODEL_ID
            assert json["temperature"] == 0
            assert json["reasoning"] == "off"
            assert json["store"] is False
            assert json["max_output_tokens"] == 400
            case_id = next(case_id for case_id in _holdout_ids() if f"Task: { _prompt_for(case_id) }" in json["input"])
            return _response({
                "output": [{"content": json_module_dumps(_valid_result(case_id))}],
                "stats": {"model_load_time_seconds": 0.0},
            })

    monkeypatch.setattr(live.httpx, "Client", FakeClient)
    report = live.run_live_semantic_assurance(FIXTURE, repeats=3)
    rendered = json.dumps(report, ensure_ascii=False)

    assert report["gate"]["mechanical_valid"] is True
    assert report["gate"]["automatic_apply_decision"] == "no_go"
    assert report["execution"]["partition"] == "sealed_holdout"
    assert calls[:1] == [("GET", "/api/v1/models")]
    assert calls[-1:] == [("GET", "/api/v1/models")]
    assert set(calls[1:-1]) == {("POST", "/api/v1/chat")}
    assert report["execution"]["chat_call_count"] == len(_holdout_ids()) * 3
    assert [entry["repeat_number"] for entry in report["execution"]["repeat_executions"]] == [1, 2, 3]
    assert all(entry["chat_call_count"] == len(_holdout_ids()) for entry in report["execution"]["repeat_executions"])
    assert all(entry["case_id_set_digest"] == _sealed_case_binding_digest(live.load_semantic_assurance_dataset(FIXTURE)) for entry in report["execution"]["repeat_executions"])
    assert len({entry["execution_digest"] for entry in report["execution"]["repeat_executions"]}) == 3
    assert len({item["report_digest"] for item in report["offline_reports"]}) == 3
    assert len({item["prediction_digest"] for item in report["offline_reports"]}) == 1
    assert len({item["results_digest"] for item in report["offline_reports"]}) == 1
    assert "Synthetic projection work" not in rendered
    assert "support_id=s1" not in rendered


def test_live_adapter_fails_closed_on_malformed_model_classification(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, path: str) -> httpx.Response:
            return _response(_inventory())

        def post(self, path: str, *, json: dict[str, Any]) -> httpx.Response:
            return _response({"output": [{"content": "not json"}], "stats": {"model_load_time_seconds": 0.0}})

    monkeypatch.setattr(live.httpx, "Client", FakeClient)
    report = live.run_live_semantic_assurance(FIXTURE)

    assert report["gate"]["mechanical_valid"] is False
    assert report["failure"] == {"stage": "parser", "code": "malformed_classification", "repeat": 1, "case_id": "holdout-supported-separation"}
    assert report["execution"]["chat_call_count"] == 1


def test_live_adapter_reports_missing_model_load_telemetry_without_inventing_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, path: str) -> httpx.Response:
            return _response(_inventory())

        def post(self, path: str, *, json: dict[str, Any]) -> httpx.Response:
            case_id = next(case_id for case_id in _holdout_ids() if f"Task: { _prompt_for(case_id) }" in json["input"])
            return _response({"output": [{"content": json_module_dumps(_valid_result(case_id))}]})

    monkeypatch.setattr(live.httpx, "Client", FakeClient)
    report = live.run_live_semantic_assurance(FIXTURE)

    assert report["gate"]["mechanical_valid"] is True
    assert report["model_load_events"] is None
    assert report["model_load_telemetry"] == "unavailable"
    assert report["execution"]["zero_model_load_events_when_observed"] is None


def test_live_adapter_fails_closed_when_evaluator_copies_a_repeat_report(monkeypatch: pytest.MonkeyPatch) -> None:
    original_evaluator = live.evaluate_semantic_assurance

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, path: str) -> httpx.Response:
            return _response(_inventory())

        def post(self, path: str, *, json: dict[str, Any]) -> httpx.Response:
            case_id = next(case_id for case_id in _holdout_ids() if f"Task: { _prompt_for(case_id) }" in json["input"])
            return _response({
                "output": [{"content": json_module_dumps(_valid_result(case_id))}],
                "stats": {"model_load_time_seconds": 0.0},
            })

    def copied_repeat_report(*args: Any, **kwargs: Any) -> dict[str, Any]:
        report = original_evaluator(*args, **kwargs)
        report["invocation"]["repeat_number"] = "1"
        return report

    monkeypatch.setattr(live.httpx, "Client", FakeClient)
    monkeypatch.setattr(live, "evaluate_semantic_assurance", copied_repeat_report)
    report = live.run_live_semantic_assurance(FIXTURE, repeats=2)

    assert report["gate"]["mechanical_valid"] is False
    assert report["gate"]["checks"]["exact_repeat_execution_coverage"] is False


def test_live_adapter_fails_closed_if_a_second_or_changed_model_is_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    inventory = _inventory()
    inventory["models"].append({
        "type": "llm", "key": "another/local-model", "selected_variant": "another/local-model@q4", "loaded_instances": [{}],
    })

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, path: str) -> httpx.Response:
            return _response(inventory)

        def post(self, path: str, *, json: dict[str, Any]) -> httpx.Response:
            raise AssertionError("chat must not be called when inventory is unsafe")

    monkeypatch.setattr(live.httpx, "Client", FakeClient)
    report = live.run_live_semantic_assurance(FIXTURE)

    assert report["gate"]["mechanical_valid"] is False
    assert report["failure"] == {"stage": "inventory_before", "code": "additional_loaded_llm"}
    assert report["execution"]["chat_call_count"] == 0


def test_live_adapter_rejects_non_loopback_and_other_model_contracts() -> None:
    with pytest.raises(live.SemanticAssuranceLiveError):
        live.run_live_semantic_assurance(FIXTURE, base_url="https://example.test")
    with pytest.raises(live.SemanticAssuranceLiveError):
        live.run_live_semantic_assurance(FIXTURE, model_id="another/local-model")


def test_live_adapter_rejects_tampered_human_label_receipt_before_http(monkeypatch: pytest.MonkeyPatch) -> None:
    class ForbiddenClient:
        def __init__(self, **kwargs: Any) -> None:
            raise AssertionError("human-label receipt must be validated before HTTP client creation")

    monkeypatch.setattr(live.httpx, "Client", ForbiddenClient)
    with pytest.raises(live.SemanticAssuranceLiveError, match="human-label admission"):
        live.run_live_semantic_assurance(FIXTURE, label_admission={"tampered": True})


def test_live_adapter_requires_exact_v2_corpus_pre_admission_binding_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = live.load_semantic_assurance_dataset(FIXTURE)
    corpus = {
        "dataset_id": dataset["dataset_id"],
        "fixture_digest": dataset["fixture_digest"],
        "label_digest": dataset["label_digest"],
        "corpus_pre_admission_digest": "a" * 64,
    }
    receipt = {
        "schema_version": "bhm.semantic-assurance-human-label-admission-receipt.v2",
        "corpus": corpus,
        "corpus_admission": _v2_corpus_admission(dataset, "b" * 64),
        "admission_digest": "c" * 64,
    }

    class ForbiddenClient:
        def __init__(self, **kwargs: Any) -> None:
            raise AssertionError("v2 receipt must be validated before HTTP client creation")

    monkeypatch.setattr(live, "verify_semantic_assurance_label_admission", lambda value: value)
    monkeypatch.setattr(live.httpx, "Client", ForbiddenClient)

    with pytest.raises(live.SemanticAssuranceLiveError, match="corpus pre-admission digest"):
        live.run_live_semantic_assurance(FIXTURE, label_admission=receipt)


def test_live_adapter_rejects_invalid_v2_curator_attestation_binding_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = live.load_semantic_assurance_dataset(FIXTURE)
    pre_admission_digest = "a" * 64
    receipt = {
        "schema_version": "bhm.semantic-assurance-human-label-admission-receipt.v2",
        "corpus": {
            "dataset_id": dataset["dataset_id"],
            "fixture_digest": dataset["fixture_digest"],
            "label_digest": dataset["label_digest"],
            "corpus_pre_admission_digest": pre_admission_digest,
        },
        "corpus_admission": {
            **_v2_corpus_admission(dataset, pre_admission_digest),
            "curator_attestation_digest": "not-a-digest",
        },
        "admission_digest": "c" * 64,
    }

    class ForbiddenClient:
        def __init__(self, **kwargs: Any) -> None:
            raise AssertionError("v2 receipt must be validated before HTTP client creation")

    monkeypatch.setattr(live, "verify_semantic_assurance_label_admission", lambda value: value)
    monkeypatch.setattr(live.httpx, "Client", ForbiddenClient)

    with pytest.raises(live.SemanticAssuranceLiveError, match="curator attestation"):
        live.run_live_semantic_assurance(FIXTURE, label_admission=receipt)


@pytest.mark.parametrize(("case_count", "accepted"), [(400, True), (513, False)])
def test_live_adapter_partition_case_bound_supports_readiness_scale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_count: int,
    accepted: bool,
) -> None:
    """The live transport bound must admit the 400-case sealed readiness floor."""
    cases = [
        {
            "case_id": f"sealed-{index:03d}",
            "partition": "sealed_holdout",
            "prompt": "Classify the redacted proposal.",
            "candidate": "A redacted candidate.",
            "supports": [{"support_id": "s1", "evidence": "Redacted canonical evidence."}],
        }
        for index in range(case_count)
    ]
    fixture = tmp_path / "bounded-live-fixture.json"
    fixture.write_text(json.dumps({"cases": cases}), encoding="utf-8")
    dataset = {
        "cases": [
            {
                "case_id": case["case_id"],
                "partition": "sealed_holdout",
                "expected": {"support_relevance": {"s1": "relevant"}},
            }
            for case in cases
        ]
    }
    monkeypatch.setattr(live, "load_semantic_assurance_dataset", lambda _path: dataset)

    if accepted:
        _, prompt_cases = live._load_prompt_cases(fixture, "sealed_holdout")
        assert len(prompt_cases) == 400
    else:
        with pytest.raises(live.SemanticAssuranceLiveError, match="partition is invalid"):
            live._load_prompt_cases(fixture, "sealed_holdout")


def _holdout_ids() -> list[str]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [str(case["case_id"]) for case in raw["cases"] if case["partition"] == "sealed_holdout"]


def _prompt_for(case_id: str) -> str:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return str(next(case["prompt"] for case in raw["cases"] if case["case_id"] == case_id))


def json_module_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _v2_corpus_admission(dataset: dict[str, object], admission_digest: str) -> dict[str, object]:
    rows = _canonical_rows(dataset)
    sealed_rows = [row for row in rows if row["partition"] == "sealed_holdout"]
    risk_counts = {risk: sum(row["risk_class"] == risk for row in rows) for risk in sorted({row["risk_class"] for row in rows})}
    sealed_risk_counts = {risk: sum(row["risk_class"] == risk for row in sealed_rows) for risk in sorted({row["risk_class"] for row in sealed_rows})}
    return {
        "receipt_schema_version": "bhm.semantic-assurance-corpus-admission-receipt.v1",
        "admission_digest": admission_digest,
        "case_count": len(rows),
        "partition_counts": {partition: sum(row["partition"] == partition for row in rows) for partition in ("development", "validation", "sealed_holdout")},
        "sealed_support_count": sum(len(row["support_ids"]) for row in sealed_rows),
        "packet_payload_set_digest": "d" * 64,
        "curator_ledger_digest": "e" * 64,
        "curator_attestation_digest": "f" * 64,
        "dataset_binding_digest": _digest(rows),
        "sealed_case_binding_digest": _digest(sealed_rows),
        "sealed_support_binding_digest": _digest([{"case_id": row["case_id"], "support_ids": row["support_ids"]} for row in sealed_rows]),
        "risk_counts": risk_counts,
        "sealed_risk_counts": sealed_risk_counts,
    }


def _canonical_rows(dataset: dict[str, object]) -> list[dict[str, object]]:
    cases = dataset["cases"]
    assert isinstance(cases, list)
    rows = [{
        "case_id": str(case["case_id"]),
        "partition": str(case["partition"]),
        "risk_class": str(case["risk_class"]),
        "support_ids": sorted(str(item) for item in case["support_ids"]),
    } for case in cases]
    return sorted(rows, key=lambda row: str(row["case_id"]))


def _sealed_case_binding_digest(dataset: dict[str, object]) -> str:
    return _digest([row for row in _canonical_rows(dataset) if row["partition"] == "sealed_holdout"])


def _digest(value: object) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
