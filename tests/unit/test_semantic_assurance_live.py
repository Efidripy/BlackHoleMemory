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
    expected = dict(case["expected"])
    return {
        **expected,
        "support_relevance": {
            f"support_{index}": expected["support_relevance"][support["support_id"]]
            for index, support in enumerate(case["supports"], start=1)
        },
    }


def test_live_classifier_prompt_requires_quoted_json_labels() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    case = raw["cases"][0]

    payload = live._chat_payload(case, live.DEFAULT_MODEL_ID)

    assert payload["model"] == live.DEFAULT_MODEL_ID
    assert 'Every classifier label is a JSON string in double quotes' in payload["input"]
    assert 'Never emit a bare label token, Python-style dictionary, code fence' in payload["input"]
    assert '"no_op"' in payload["input"]
    assert "support_ref=support_1" in payload["input"]
    assert "support_id=" not in payload["input"]
    assert "response_format" not in payload


def test_live_classifier_parser_requires_exact_ephemeral_support_aliases() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    case = raw["cases"][0]
    expected = dict(case["expected"])
    aliased = _valid_result(str(case["case_id"]))

    parsed = live._parse_classification(json.dumps(aliased), case)
    assert parsed["support_relevance"] == expected["support_relevance"]

    with pytest.raises(live._LiveFailure):
        live._parse_classification(json.dumps(expected), case)


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
    chain = report["execution"]["event_chain"]
    assert set(chain) == {
        "events", "event_count", "first_event_digest", "last_event_digest", "chain_root",
        "inventory_before_digest", "inventory_after_digest",
    }
    assert chain["event_count"] == report["execution"]["chat_call_count"]
    assert len(chain["events"]) == report["execution"]["chat_call_count"]
    assert all(set(event) == {"sequence", "repeat_number", "case_id", "request_digest", "response_digest", "event_digest"} for event in chain["events"])
    assert live._event_chain_is_valid(chain, report["execution"]["chat_call_count"]) is True
    assert [entry["repeat_number"] for entry in report["execution"]["repeat_executions"]] == [1, 2, 3]
    assert all(entry["chat_call_count"] == len(_holdout_ids()) for entry in report["execution"]["repeat_executions"])
    assert all(entry["case_id_set_digest"] == _sealed_case_binding_digest(live.load_semantic_assurance_dataset(FIXTURE)) for entry in report["execution"]["repeat_executions"])
    assert len({entry["execution_digest"] for entry in report["execution"]["repeat_executions"]}) == 3
    assert len({item["report_digest"] for item in report["offline_reports"]}) == 3
    assert len({item["prediction_digest"] for item in report["offline_reports"]}) == 1
    assert len({item["results_digest"] for item in report["offline_reports"]}) == 1
    assert "Synthetic projection work" not in rendered
    assert "support_id=s1" not in rendered


def test_v6_public_live_receipt_separates_private_per_case_evaluation_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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

    monkeypatch.setattr(live.httpx, "Client", FakeClient)
    legacy = live.run_live_semantic_assurance(FIXTURE, repeats=3)

    public, private = live.build_semantic_assurance_live_v6_evidence(legacy)
    verified = live.verify_semantic_assurance_live_v6_evidence(public, private)
    public_rendered = json.dumps(public, ensure_ascii=False, sort_keys=True)

    assert public["schema_version"] == live.LIVE_PUBLIC_RECEIPT_SCHEMA_VERSION_V6
    assert private["schema_version"] == live.LIVE_PRIVATE_EVALUATION_BUNDLE_SCHEMA_VERSION_V6
    assert public["private_evaluation"]["offline_reports_commitment"] == private["offline_reports_commitment"]
    assert private["public_anchor"]["public_receipt_digest"] == public["public_receipt_digest"]
    assert private["offline_reports"] == legacy["offline_reports"]
    assert verified["offline_report_count"] == 3
    assert not (live._mapping_keys(public) & {
        "offline_reports", "results", "case_id", "case_ids", "predictions",
        "support_relevance", "support_ids", "labels",
    })
    assert all(case_id not in public_rendered for case_id in _holdout_ids())
    assert "support_ref=" not in public_rendered

    public_path = tmp_path / "public-live-v6.json"
    private_path = tmp_path / "private-evaluation-v6.json"
    live.write_semantic_assurance_public_live_receipt_v6(public, public_path)
    live.write_semantic_assurance_private_evaluation_bundle_v6(private, public, private_path)
    assert json.loads(public_path.read_text(encoding="utf-8")) == public
    assert json.loads(private_path.read_text(encoding="utf-8")) == private

    tampered = json.loads(json.dumps(private))
    tampered["offline_reports"][0]["results"] = []
    tampered_core = {key: value for key, value in tampered.items() if key != "private_bundle_digest"}
    tampered["private_bundle_digest"] = live._digest(tampered_core)
    with pytest.raises(live.SemanticAssuranceLiveError, match="binding"):
        live.verify_semantic_assurance_live_v6_evidence(public, tampered)


def test_v3_event_chain_has_exact_1200_ordered_content_free_events_and_binds_inventories() -> None:
    before = {"exact_requested_model_loaded": True, "only_requested_llm_loaded": True, "loaded_llm_count": 1}
    after = {"exact_requested_model_loaded": True, "only_requested_llm_loaded": True, "loaded_llm_count": 1}
    previous = live._event_chain_seed(before)
    events: list[dict[str, object]] = []
    for sequence in range(1, 1201):
        event = live._event_record(
            sequence=sequence,
            repeat_number=((sequence - 1) // 400) + 1,
            case_id=f"sealed-{((sequence - 1) % 400) + 1:03d}",
            request_digest=_digest({"request": sequence}),
            response_digest=_digest({"response": sequence}),
            previous_event_digest=previous,
        )
        events.append(event)
        previous = event["event_digest"]

    chain = live._event_chain(events, before, after)

    assert chain["event_count"] == 1200
    assert chain["first_event_digest"] == events[0]["event_digest"]
    assert chain["last_event_digest"] == events[-1]["event_digest"]
    assert live._event_chain_is_valid(chain, 1200) is True
    assert '"request":' not in json.dumps(chain)
    assert '"response":' not in json.dumps(chain)

    changed_inventory = dict(chain)
    changed_inventory["inventory_after_digest"] = "0" * 64
    assert live._event_chain_is_valid(changed_inventory, 1200) is False
    changed_event = {**chain, "events": [dict(event) for event in chain["events"]]}
    changed_event["events"][400]["case_id"] = "substituted-case"
    assert live._event_chain_is_valid(changed_event, 1200) is False


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


def test_live_adapter_emits_forward_only_v4_receipt_with_exact_v6_label_binding() -> None:
    dataset = _v6_dataset()
    label_admission = _v3_label_admission(dataset)
    receipt = live._receipt(
        dataset=dataset,
        partition="sealed_holdout",
        endpoint=live.DEFAULT_BASE_URL,
        requested_model={"model_id": live.DEFAULT_MODEL_ID, "model_variant": live.DEFAULT_MODEL_VARIANT},
        repeats=1,
        reports=[],
        before_inventory=None,
        after_inventory=None,
        failure={"stage": "unit", "code": "not_run"},
        chat_call_count=0,
        model_load_events=0,
        model_load_evidence_available=True,
        elapsed_ms=0.0,
        repeat_executions=[],
        event_records=[],
        label_admission=label_admission,
    )

    assert receipt["schema_version"] == live.LIVE_RECEIPT_SCHEMA_VERSION_V4
    assert receipt["label_admission"] == {
        "receipt_schema_version": "bhm.semantic-assurance-human-label-admission-receipt.v3",
        "admission_digest": "c" * 64,
        "corpus_pre_admission_digest": "a" * 64,
        "corpus_admission_digest": "a" * 64,
        "admission_profile": "v6_independent_human_curation",
        "partition_risk_counts": live._partition_risk_counts(dataset),
        "v6_control_binding_digest": "b" * 64,
    }
    assert receipt["report_digest"] == _digest({key: value for key, value in receipt.items() if key != "report_digest"})


def test_live_adapter_v4_rejects_offline_report_with_substituted_v6_binding() -> None:
    dataset = _v6_dataset()
    binding = live._v6_label_admission_binding(_v3_label_admission(dataset), dataset)
    report = {
        "dataset": {
            "label_admission_version": "v3_v6_corpus_pre_admitted",
            "human_label_admission_digest": binding["admission_digest"],
            "corpus_pre_admission_digest": binding["corpus_pre_admission_digest"],
            "admission_profile": binding["admission_profile"],
            "partition_risk_counts": binding["partition_risk_counts"],
            "v6_control_binding_digest": binding["v6_control_binding_digest"],
        },
    }

    assert live._v6_reports_bind_label([report], binding, expected_count=1) is True
    report["dataset"]["v6_control_binding_digest"] = "f" * 64
    assert live._v6_reports_bind_label([report], binding, expected_count=1) is False


def test_live_adapter_emits_forward_only_v5_receipt_with_exact_curator_audit_and_strata_binding() -> None:
    dataset = _v6_dataset()
    label_admission = _v5_label_admission(dataset)
    receipt = live._receipt(
        dataset=dataset,
        partition="sealed_holdout",
        endpoint=live.DEFAULT_BASE_URL,
        requested_model={"model_id": live.DEFAULT_MODEL_ID, "model_variant": live.DEFAULT_MODEL_VARIANT},
        repeats=1,
        reports=[],
        before_inventory=None,
        after_inventory=None,
        failure={"stage": "unit", "code": "not_run"},
        chat_call_count=0,
        model_load_events=0,
        model_load_evidence_available=True,
        elapsed_ms=0.0,
        repeat_executions=[],
        event_records=[],
        label_admission=label_admission,
    )

    assert receipt["schema_version"] == live.LIVE_RECEIPT_SCHEMA_VERSION_V5
    binding = receipt["label_admission"]
    assert binding["receipt_schema_version"] == "bhm.semantic-assurance-human-label-admission-receipt.v5"
    assert binding["admission_profile"] == "v6_curator_audited_independent_human_curation"
    assert binding["curator_audit"]["canonical_fragment_binding_digest"] == "4" * 64
    assert binding["sealed_readiness_strata_binding_digest"] == "5" * 64
    assert binding["sealed_readiness_strata_minimum_population"] == 80
    assert receipt["gate"]["automatic_apply_ready"] is False
    assert receipt["gate"]["automatic_apply_decision"] == "no_go"


def test_live_adapter_v5_rejects_tampered_curator_audit_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = _v6_dataset()
    receipt = _v5_label_admission(dataset)
    audit = receipt["corpus_admission"]["curator_audit"]
    assert isinstance(audit, dict)
    audit["atomic_fact_binding_digest"] = "not-a-digest"

    class ForbiddenClient:
        def __init__(self, **kwargs: Any) -> None:
            raise AssertionError("curator-audited evidence must be validated before HTTP client creation")

    monkeypatch.setattr(live, "verify_semantic_assurance_label_admission", lambda value: value)
    monkeypatch.setattr(live, "_load_prompt_cases", lambda *_args: (dataset, []))
    monkeypatch.setattr(live.httpx, "Client", ForbiddenClient)
    with pytest.raises(live.SemanticAssuranceLiveError, match="curator audit atomic_fact_binding_digest"):
        live.run_live_semantic_assurance(FIXTURE, label_admission=receipt)


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda receipt: receipt["corpus_admission"].pop("admission_profile"), "corpus pre-admission binding"),
        (lambda receipt: receipt["corpus_admission"].__setitem__("admission_profile", "ordinary_curation"), "V6 label admission binding"),
        (lambda receipt: receipt["corpus_admission"]["partition_risk_counts"]["development"].__setitem__("factuality", 999), "partition risk binding"),
        (lambda receipt: receipt["corpus_admission"].__setitem__("v6_control_binding_digest", "substituted-not-a-digest"), "V6 control binding"),
        (lambda receipt: receipt["corpus"].__setitem__("corpus_pre_admission_digest", "d" * 64), "corpus pre-admission digest"),
    ],
)
def test_live_adapter_rejects_absent_tampered_or_substituted_v6_binding_before_http(
    monkeypatch: pytest.MonkeyPatch,
    mutate: Any,
    error: str,
) -> None:
    dataset = _v6_dataset()
    receipt = _v3_label_admission(dataset)
    mutate(receipt)

    class ForbiddenClient:
        def __init__(self, **kwargs: Any) -> None:
            raise AssertionError("V6 label evidence must be validated before HTTP client creation")

    # The receipt verifier is independently tested in its own module.  This
    # focused test exercises the second, dataset-local live-boundary check.
    monkeypatch.setattr(live, "verify_semantic_assurance_label_admission", lambda value: value)
    monkeypatch.setattr(live, "_load_prompt_cases", lambda *_args: (dataset, []))
    monkeypatch.setattr(live.httpx, "Client", ForbiddenClient)

    with pytest.raises(live.SemanticAssuranceLiveError, match=error):
        live.run_live_semantic_assurance(FIXTURE, label_admission=receipt)


def test_live_adapter_legacy_v2_path_rejects_v3_binding_shape_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = _v6_dataset()
    receipt = _v3_label_admission(dataset)
    receipt["schema_version"] = "bhm.semantic-assurance-human-label-admission-receipt.v2"

    class ForbiddenClient:
        def __init__(self, **kwargs: Any) -> None:
            raise AssertionError("legacy receipt shape must fail before HTTP client creation")

    monkeypatch.setattr(live, "verify_semantic_assurance_label_admission", lambda value: value)
    monkeypatch.setattr(live, "_load_prompt_cases", lambda *_args: (dataset, []))
    monkeypatch.setattr(live.httpx, "Client", ForbiddenClient)

    with pytest.raises(live.SemanticAssuranceLiveError, match="corpus pre-admission binding"):
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


def _v3_label_admission(dataset: dict[str, object]) -> dict[str, object]:
    corpus_admission_digest = "a" * 64
    return {
        "schema_version": "bhm.semantic-assurance-human-label-admission-receipt.v3",
        "ok": True,
        "corpus": {
            "dataset_id": dataset["dataset_id"],
            "fixture_digest": dataset["fixture_digest"],
            "label_digest": dataset["label_digest"],
            "corpus_pre_admission_digest": corpus_admission_digest,
        },
        "corpus_admission": {
            **_v2_corpus_admission(dataset, corpus_admission_digest),
            "receipt_schema_version": "bhm.semantic-assurance-corpus-admission-receipt.v2",
            "admission_profile": "v6_independent_human_curation",
            "partition_risk_counts": live._partition_risk_counts(dataset),
            "v6_control_binding_digest": "b" * 64,
        },
        "assurance": {"v6_corpus_structurally_bound": True},
        "admission_digest": "c" * 64,
    }


def _v5_label_admission(dataset: dict[str, object]) -> dict[str, object]:
    receipt = _v3_label_admission(dataset)
    corpus_admission = receipt["corpus_admission"]
    assert isinstance(corpus_admission, dict)
    corpus_admission["receipt_schema_version"] = "bhm.semantic-assurance-corpus-admission-receipt.v3"
    corpus_admission["admission_profile"] = "v6_curator_audited_independent_human_curation"
    corpus_admission["curator_audit"] = {
        "receipt_schema_version": "bhm.semantic-assurance-curator-audit-receipt.v1",
        "audit_digest": "1" * 64,
        "atomic_fact_binding_digest": "2" * 64,
        "control_binding_digest": "3" * 64,
        "canonical_fragment_binding_digest": "4" * 64,
    }
    receipt["schema_version"] = "bhm.semantic-assurance-human-label-admission-receipt.v5"
    assurance = receipt["assurance"]
    assert isinstance(assurance, dict)
    assurance["v6_curator_audit_exactly_bound"] = True
    assurance["v6_sealed_exact_label_strata_bound"] = True
    assurance["sealed_readiness_strata_binding_digest"] = "5" * 64
    assurance["sealed_readiness_strata_minimum_population"] = 80
    return receipt


def _v6_dataset() -> dict[str, object]:
    risks = (
        "factuality", "entailment", "relevance", "direct_conflict", "temporal_conflict",
        "scope_conflict", "implicit_conflict", "injection", "cross_project",
        "retrieval_pressure", "abstention",
    )
    cases: list[dict[str, object]] = []
    sealed_conflicts = [
        risk
        for risk in ("direct_conflict", "temporal_conflict", "scope_conflict", "implicit_conflict")
        for _ in range(50)
    ]
    for partition, count in (("development", 220), ("validation", 220), ("sealed_holdout", 400)):
        for index in range(count):
            risk = sealed_conflicts[index] if partition == "sealed_holdout" and index < len(sealed_conflicts) else risks[index % len(risks)]
            case_id = f"{partition}-{index:03d}"
            cases.append({
                "case_id": case_id,
                "partition": partition,
                "risk_class": risk,
                "support_ids": [f"support-{case_id}"],
                "expected": {"support_relevance": {f"support-{case_id}": "relevant"}},
            })
    return {
        "dataset_id": "v6-live-unit-contract",
        "fixture_digest": "d" * 64,
        "label_digest": "e" * 64,
        "cases": cases,
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
