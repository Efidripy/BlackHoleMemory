"""Read-only live adapter for the local semantic-assurance contract."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from blackholememory.semantic_assurance_evaluation import SemanticAssuranceEvaluationError
from blackholememory.semantic_assurance_evaluation import evaluate_semantic_assurance
from blackholememory.semantic_assurance_evaluation import load_semantic_assurance_dataset
from blackholememory.semantic_assurance_label_admission import SemanticAssuranceLabelAdmissionError
from blackholememory.semantic_assurance_label_admission import verify_semantic_assurance_label_admission
from blackholememory.semantic_assurance_live_base import (
    DEFAULT_BASE_URL, DEFAULT_MODEL_ID, DEFAULT_MODEL_VARIANT, LIVE_PRIVATE_EVALUATION_BUNDLE_SCHEMA_VERSION_V6, LIVE_PUBLIC_RECEIPT_SCHEMA_VERSION_V6,
    LIVE_RECEIPT_SCHEMA_VERSION, LIVE_RECEIPT_SCHEMA_VERSION_V4, LIVE_RECEIPT_SCHEMA_VERSION_V5, LIVE_RECEIPT_SCHEMA_VERSION_V6, PROMPT_CONTRACT,
    MAX_RESPONSE_BYTES, MAX_TIMEOUT_SECONDS, MAX_REPEATS, MAX_CASES_PER_PARTITION, MAX_SUPPORTS_PER_CASE, _CLASSIFIER_KEYS, _FACTUALITY, _ENTAILMENT, _CONFLICT, _OPERATION, _RELEVANCE,
    _LABEL_ADMISSION_RECEIPT_V1, _LABEL_ADMISSION_RECEIPT_V2, _LABEL_ADMISSION_RECEIPT_V3, _LABEL_ADMISSION_RECEIPT_V4, _LABEL_ADMISSION_RECEIPT_V5, _LABEL_ADMISSION_RECEIPT_V6,
    _V2_CORPUS_ADMISSION_KEYS, _V3_CORPUS_ADMISSION_KEYS, _CURATOR_AUDIT_KEYS, _V4_CORPUS_ADMISSION_KEYS, _V6_ADMISSION_PROFILE, _V6_CURATOR_AUDITED_ADMISSION_PROFILE, _V6_RISK_CLASSES, _SYSTEM_PROMPT,
    SemanticAssuranceLiveError, _v6_reports_bind_label, _repeat_reports_bind_ledger, _label, _bounded_text, _sha256_digest, _failure, _LiveFailure,
)
from blackholememory.semantic_assurance_live_base import _mapping_keys  # noqa: F401
from blackholememory.semantic_assurance_live_event import _digest, _event_chain, _event_chain_is_valid, _event_chain_seed, _event_record
from blackholememory.semantic_assurance_live_event import _is_sha256  # noqa: F401
from blackholememory.semantic_assurance_live_evidence import (
    build_semantic_assurance_live_v6_evidence, verify_semantic_assurance_live_v6_evidence,
)
from blackholememory.semantic_assurance_live_evidence import _verified_legacy_live_receipt  # noqa: F401
from blackholememory.semantic_assurance_live_evidence import _public_live_receipt_v6_core  # noqa: F401
from blackholememory.semantic_assurance_live_evidence import _offline_reports_commitment  # noqa: F401
from blackholememory.semantic_assurance_live_evidence import _verify_public_live_receipt_v6  # noqa: F401
from blackholememory.semantic_assurance_live_evidence import _verify_private_evaluation_bundle_v6  # noqa: F401

def run_live_semantic_assurance(
    dataset_path: Path | str,
    *,
    partition: str = "sealed_holdout",
    base_url: str = DEFAULT_BASE_URL,
    timeout_seconds: float = 30.0,
    repeats: int = 1,
    model_id: str = DEFAULT_MODEL_ID,
    model_variant: str = DEFAULT_MODEL_VARIANT,
    label_admission: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a bounded fixture partition against the already-loaded exact model.

    Transport and parser failures become a content-free failed receipt; they
    never become invented predictions and no subsequent case is attempted.
    Invalid caller input or an unreadable fixture remains an exception because
    there is no trustworthy receipt boundary to evaluate.
    """

    endpoint = _loopback_endpoint(base_url)
    timeout = _timeout(timeout_seconds)
    repeat_count = _repeat_count(repeats)
    requested_model = _requested_model(model_id, model_variant)
    dataset, prompt_cases = _load_prompt_cases(dataset_path, partition)
    normalized_label_admission = _validate_label_admission(label_admission, dataset)
    case_binding_digest = _partition_case_binding_digest(dataset, partition)
    started = time.perf_counter()
    failure: dict[str, Any] | None = None
    reports: list[dict[str, Any]] = []
    before_inventory: dict[str, Any] | None = None
    after_inventory: dict[str, Any] | None = None
    model_load_events = 0
    model_load_evidence_available = True
    chat_call_count = 0
    repeat_executions: list[dict[str, Any]] = []
    event_records: list[dict[str, Any]] = []
    previous_event_digest: str | None = None

    try:
        with httpx.Client(base_url=endpoint, timeout=timeout, trust_env=False, follow_redirects=False) as client:
            try:
                before_inventory = _inventory(client, requested_model)
            except _LiveFailure as exc:
                failure = _failure("inventory_before", exc.code)
            if failure is None:
                previous_event_digest = _event_chain_seed(before_inventory)
                for repeat_number in range(1, repeat_count + 1):
                    repeat_started_calls = chat_call_count
                    predictions: list[dict[str, Any]] = []
                    for case in prompt_cases:
                        try:
                            request = _chat_payload(case, requested_model["model_id"])
                            response = _chat(client, request)
                            chat_call_count += 1
                            if response["model_load_event"] is None:
                                model_load_evidence_available = False
                            else:
                                model_load_events += int(response["model_load_event"])
                            predictions.append(_parse_classification(response["content"], case))
                            event = _event_record(
                                sequence=len(event_records) + 1,
                                repeat_number=repeat_number,
                                case_id=str(case["case_id"]),
                                request_digest=_digest(request),
                                response_digest=_digest(response["content"]),
                                previous_event_digest=previous_event_digest,
                            )
                            event_records.append(event)
                            previous_event_digest = event["event_digest"]
                        except _LiveFailure as exc:
                            failure = _failure(exc.stage, exc.code, repeat_number, str(case["case_id"]))
                            break
                    if failure is not None:
                        break
                    try:
                        report = evaluate_semantic_assurance(
                            dataset,
                            predictions,
                            partition=partition,
                            invocation={
                                "model_id": requested_model["model_id"],
                                "model_variant": requested_model["model_variant"],
                                "prompt_digest": _digest(PROMPT_CONTRACT),
                                "endpoint": endpoint,
                                "repeat_number": repeat_number,
                                "case_count": len(prompt_cases),
                                "case_id_set_digest": case_binding_digest,
                            },
                            label_admission=normalized_label_admission,
                        )
                    except SemanticAssuranceEvaluationError:
                        failure = _failure("offline_evaluation", "offline_evaluation_failed", repeat_number)
                        break
                    reports.append(report)
                    if failure is None:
                        repeat_entry = {
                            "repeat_number": repeat_number,
                            "case_count": len(prompt_cases),
                            "case_id_set_digest": case_binding_digest,
                            "chat_call_count": chat_call_count - repeat_started_calls,
                            "prediction_digest": report["prediction_digest"],
                        }
                        repeat_executions.append({**repeat_entry, "execution_digest": _digest(repeat_entry)})
            if failure is None:
                try:
                    after_inventory = _inventory(client, requested_model)
                except _LiveFailure as exc:
                    failure = _failure("inventory_after", exc.code)
    except httpx.HTTPError:
        failure = failure or _failure("transport", "transport_failed")

    return _receipt(
        dataset=dataset,
        partition=str(partition).strip().casefold(),
        endpoint=endpoint,
        requested_model=requested_model,
        repeats=repeat_count,
        reports=reports,
        before_inventory=before_inventory,
        after_inventory=after_inventory,
        failure=failure,
        chat_call_count=chat_call_count,
        model_load_events=model_load_events,
        model_load_evidence_available=model_load_evidence_available,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        repeat_executions=repeat_executions,
        event_records=event_records,
        label_admission=normalized_label_admission,
    )


def write_live_semantic_assurance_receipt(report: Mapping[str, Any], path: Path | str) -> None:
    """Persist an explicit content-free JSON receipt without creating a directory tree implicitly outside its parent."""

    target = Path(path)
    if target.suffix.casefold() != ".json":
        raise SemanticAssuranceLiveError("live semantic assurance receipt path must end in .json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(dict(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_semantic_assurance_public_live_receipt_v6(receipt: Mapping[str, Any], path: Path | str) -> None:
    """Write a verified V6 public projection without private evaluator rows."""

    _verify_public_live_receipt_v6(receipt)
    _write_semantic_assurance_json(receipt, path, "public live receipt")


def write_semantic_assurance_private_evaluation_bundle_v6(
    bundle: Mapping[str, Any],
    public_receipt: Mapping[str, Any],
    path: Path | str,
) -> None:
    """Write a verified owner-only V6 evaluator bundle.

    The caller remains responsible for selecting an owner-controlled directory
    and its ACLs.  This helper verifies the cryptographic public anchor before
    it serializes any per-case evaluator report.
    """

    _verify_private_evaluation_bundle_v6(bundle, public_receipt=public_receipt)
    _write_semantic_assurance_json(bundle, path, "private evaluation bundle")


def _write_semantic_assurance_json(value: Mapping[str, Any], path: Path | str, artifact: str) -> None:
    target = Path(path)
    if target.suffix.casefold() != ".json":
        raise SemanticAssuranceLiveError(f"semantic assurance {artifact} path must end in .json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_prompt_cases(dataset_path: Path | str, partition: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        raw = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SemanticAssuranceLiveError("semantic assurance live fixture is unavailable") from exc
    try:
        dataset = load_semantic_assurance_dataset(dataset_path)
    except SemanticAssuranceEvaluationError as exc:
        raise SemanticAssuranceLiveError("semantic assurance live fixture is invalid") from exc
    selected = str(partition).strip().casefold()
    expected_cases = {str(case["case_id"]): case for case in dataset["cases"] if case["partition"] == selected}
    if not expected_cases or len(expected_cases) > MAX_CASES_PER_PARTITION:
        raise SemanticAssuranceLiveError("semantic assurance live partition is invalid")
    if not isinstance(raw, Mapping) or not isinstance(raw.get("cases"), list):
        raise SemanticAssuranceLiveError("semantic assurance live fixture cases are invalid")
    cases: list[dict[str, Any]] = []
    for raw_case in raw["cases"]:
        if not isinstance(raw_case, Mapping) or str(raw_case.get("partition") or "").casefold() != selected:
            continue
        case_id = _bounded_text(raw_case.get("case_id"), "case id", 120)
        normalized = expected_cases.get(case_id)
        if normalized is None:
            raise SemanticAssuranceLiveError("semantic assurance live case identity is invalid")
        supports = raw_case.get("supports")
        if not isinstance(supports, list) or not 1 <= len(supports) <= MAX_SUPPORTS_PER_CASE:
            raise SemanticAssuranceLiveError("semantic assurance live supports are invalid")
        prompt_supports = []
        for support in supports:
            if not isinstance(support, Mapping):
                raise SemanticAssuranceLiveError("semantic assurance live support is invalid")
            prompt_supports.append({
                "support_id": _bounded_text(support.get("support_id"), "support id", 120),
                "evidence": _bounded_text(support.get("evidence"), "support evidence", 12_000),
            })
        expected_supports = set(normalized["expected"]["support_relevance"])
        if {item["support_id"] for item in prompt_supports} != expected_supports:
            raise SemanticAssuranceLiveError("semantic assurance live supports do not match the evaluator contract")
        cases.append({
            "case_id": case_id,
            "prompt": _bounded_text(raw_case.get("prompt"), "prompt", 12_000),
            "candidate": _bounded_text(raw_case.get("candidate"), "candidate", 12_000),
            "supports": prompt_supports,
        })
    if {case["case_id"] for case in cases} != set(expected_cases):
        raise SemanticAssuranceLiveError("semantic assurance live fixture coverage is invalid")
    return dataset, cases


def _validate_label_admission(
    raw: Mapping[str, Any] | None,
    dataset: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Reject a mismatched human-label receipt before creating an HTTP client."""

    if raw is None:
        return None
    try:
        receipt = verify_semantic_assurance_label_admission(raw)
    except SemanticAssuranceLabelAdmissionError as exc:
        raise SemanticAssuranceLiveError("semantic assurance human-label admission is invalid") from exc
    expected = {
        "dataset_id": dataset["dataset_id"],
        "fixture_digest": dataset["fixture_digest"],
        "label_digest": dataset["label_digest"],
    }
    corpus = receipt.get("corpus")
    if not isinstance(corpus, Mapping) or any(corpus.get(key) != value for key, value in expected.items()):
        raise SemanticAssuranceLiveError("semantic assurance human-label admission does not bind this exact dataset")
    schema_version = receipt.get("schema_version")
    if schema_version == _LABEL_ADMISSION_RECEIPT_V1:
        if set(corpus) != set(expected):
            raise SemanticAssuranceLiveError("semantic assurance v1 human-label admission corpus is invalid")
        return receipt
    if schema_version not in {
        _LABEL_ADMISSION_RECEIPT_V2,
        _LABEL_ADMISSION_RECEIPT_V3,
        _LABEL_ADMISSION_RECEIPT_V4,
        _LABEL_ADMISSION_RECEIPT_V5,
        _LABEL_ADMISSION_RECEIPT_V6,
    }:
        raise SemanticAssuranceLiveError("semantic assurance human-label admission receipt version is unsupported")
    corpus_pre_admission_digest = _sha256_digest(
        corpus.get("corpus_pre_admission_digest"),
        "corpus pre-admission digest",
    )
    corpus_admission = receipt.get("corpus_admission")
    expected_corpus_admission_keys = (
        _V4_CORPUS_ADMISSION_KEYS
        if schema_version in {_LABEL_ADMISSION_RECEIPT_V4, _LABEL_ADMISSION_RECEIPT_V5, _LABEL_ADMISSION_RECEIPT_V6}
        else _V3_CORPUS_ADMISSION_KEYS
        if schema_version == _LABEL_ADMISSION_RECEIPT_V3
        else _V2_CORPUS_ADMISSION_KEYS
    )
    if not isinstance(corpus_admission, Mapping) or set(corpus_admission) != expected_corpus_admission_keys:
        raise SemanticAssuranceLiveError("semantic assurance corpus pre-admission binding is invalid")
    _sha256_digest(
        corpus_admission.get("curator_attestation_digest"),
        "curator attestation binding",
    )
    for key in ("dataset_binding_digest", "sealed_case_binding_digest", "sealed_support_binding_digest"):
        _sha256_digest(corpus_admission.get(key), f"{key} binding")
    if not isinstance(corpus_admission.get("risk_counts"), Mapping) or not isinstance(corpus_admission.get("sealed_risk_counts"), Mapping):
        raise SemanticAssuranceLiveError("semantic assurance v2 corpus risk binding is invalid")
    rows = _canonical_dataset_rows(dataset)
    sealed_rows = [row for row in rows if row["partition"] == "sealed_holdout"]
    partition_counts = {
        name: sum(row["partition"] == name for row in rows)
        for name in ("development", "validation", "sealed_holdout")
    }
    risk_counts = {
        risk: sum(row["risk_class"] == risk for row in rows)
        for risk in sorted(str(item) for item in corpus_admission["risk_counts"])
    }
    sealed_risk_counts = {
        risk: sum(row["risk_class"] == risk for row in sealed_rows)
        for risk in sorted(str(item) for item in corpus_admission["sealed_risk_counts"])
    }
    if (
        corpus_admission.get("case_count") != len(rows)
        or corpus_admission.get("partition_counts") != partition_counts
        or corpus_admission.get("sealed_support_count") != sum(len(row["support_ids"]) for row in sealed_rows)
        or corpus_admission.get("risk_counts") != risk_counts
        or corpus_admission.get("sealed_risk_counts") != sealed_risk_counts
        or corpus_admission.get("dataset_binding_digest") != _digest(rows)
        or corpus_admission.get("sealed_case_binding_digest") != _digest(sealed_rows)
        or corpus_admission.get("sealed_support_binding_digest") != _digest([{"case_id": row["case_id"], "support_ids": row["support_ids"]} for row in sealed_rows])
    ):
        raise SemanticAssuranceLiveError("semantic assurance v2 corpus dataset binding does not match this dataset")
    if _sha256_digest(corpus_admission.get("admission_digest"), "corpus admission binding") != corpus_pre_admission_digest:
        raise SemanticAssuranceLiveError("semantic assurance v2 corpus pre-admission digest does not bind the receipt")
    if schema_version == _LABEL_ADMISSION_RECEIPT_V3:
        _v6_label_admission_binding(receipt, dataset)
    if schema_version in {_LABEL_ADMISSION_RECEIPT_V5, _LABEL_ADMISSION_RECEIPT_V6}:
        _v6_curator_audited_label_admission_binding(receipt, dataset)
    return receipt


def _v6_label_admission_binding(
    receipt: Mapping[str, Any],
    dataset: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the V6-only content-free label binding for live receipt v4.

    The upstream label verifier has already checked its own receipt digest.
    This second, dataset-local validation prevents an accepted V6 label receipt
    from being silently weakened, substituted, or transplanted at the HTTP
    boundary.  It runs before an HTTP client is created.
    """

    if receipt.get("schema_version") != _LABEL_ADMISSION_RECEIPT_V3:
        raise SemanticAssuranceLiveError("semantic assurance V6 label admission receipt version is invalid")
    corpus = receipt.get("corpus")
    corpus_admission = receipt.get("corpus_admission")
    assurance = receipt.get("assurance")
    if (
        not isinstance(corpus, Mapping)
        or not isinstance(corpus_admission, Mapping)
        or not isinstance(assurance, Mapping)
        or set(corpus) != {"dataset_id", "fixture_digest", "label_digest", "corpus_pre_admission_digest"}
        or assurance.get("v6_corpus_structurally_bound") is not True
        or set(corpus_admission) != _V3_CORPUS_ADMISSION_KEYS
        or corpus_admission.get("receipt_schema_version") != "bhm.semantic-assurance-corpus-admission-receipt.v2"
        or corpus_admission.get("admission_profile") != _V6_ADMISSION_PROFILE
    ):
        raise SemanticAssuranceLiveError("semantic assurance V6 label admission binding is invalid")
    corpus_pre_admission_digest = _sha256_digest(
        corpus.get("corpus_pre_admission_digest"),
        "V6 corpus pre-admission digest",
    )
    corpus_admission_digest = _sha256_digest(
        corpus_admission.get("admission_digest"),
        "V6 corpus admission digest",
    )
    if corpus_pre_admission_digest != corpus_admission_digest:
        raise SemanticAssuranceLiveError("semantic assurance V6 corpus pre-admission link is invalid")
    partition_risk_counts = corpus_admission.get("partition_risk_counts")
    expected_partition_risk_counts = _partition_risk_counts(dataset)
    if partition_risk_counts != expected_partition_risk_counts:
        raise SemanticAssuranceLiveError("semantic assurance V6 partition risk binding does not match this dataset")
    return {
        "receipt_schema_version": _LABEL_ADMISSION_RECEIPT_V3,
        "admission_digest": _sha256_digest(receipt.get("admission_digest"), "V6 label admission digest"),
        "corpus_pre_admission_digest": corpus_pre_admission_digest,
        "corpus_admission_digest": corpus_admission_digest,
        "admission_profile": _V6_ADMISSION_PROFILE,
        "partition_risk_counts": expected_partition_risk_counts,
        "v6_control_binding_digest": _sha256_digest(
            corpus_admission.get("v6_control_binding_digest"),
            "V6 control binding digest",
        ),
    }


def _v6_curator_audited_label_admission_binding(
    receipt: Mapping[str, Any],
    dataset: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the V5 strata-bound curator-audited binding for live receipt v5."""

    schema_version = receipt.get("schema_version")
    if schema_version not in {_LABEL_ADMISSION_RECEIPT_V5, _LABEL_ADMISSION_RECEIPT_V6}:
        raise SemanticAssuranceLiveError("semantic assurance curator-audited label receipt version is invalid")
    corpus = receipt.get("corpus")
    corpus_admission = receipt.get("corpus_admission")
    assurance = receipt.get("assurance")
    if (
        not isinstance(corpus, Mapping)
        or not isinstance(corpus_admission, Mapping)
        or not isinstance(assurance, Mapping)
        or set(corpus) != {"dataset_id", "fixture_digest", "label_digest", "corpus_pre_admission_digest"}
        or assurance.get("v6_corpus_structurally_bound") is not True
        or assurance.get("v6_curator_audit_exactly_bound") is not True
        or assurance.get("v6_sealed_exact_label_strata_bound") is not True
        or assurance.get("sealed_readiness_strata_minimum_population") != 80
        or set(corpus_admission) != _V4_CORPUS_ADMISSION_KEYS
        or corpus_admission.get("receipt_schema_version") != "bhm.semantic-assurance-corpus-admission-receipt.v3"
        or corpus_admission.get("admission_profile") != _V6_CURATOR_AUDITED_ADMISSION_PROFILE
    ):
        raise SemanticAssuranceLiveError("semantic assurance curator-audited label admission binding is invalid")
    corpus_pre_admission_digest = _sha256_digest(corpus.get("corpus_pre_admission_digest"), "curator-audited corpus pre-admission digest")
    corpus_admission_digest = _sha256_digest(corpus_admission.get("admission_digest"), "curator-audited corpus admission digest")
    if corpus_pre_admission_digest != corpus_admission_digest:
        raise SemanticAssuranceLiveError("semantic assurance curator-audited corpus pre-admission link is invalid")
    partition_risk_counts = corpus_admission.get("partition_risk_counts")
    expected_partition_risk_counts = _partition_risk_counts(dataset)
    if partition_risk_counts != expected_partition_risk_counts:
        raise SemanticAssuranceLiveError("semantic assurance curator-audited partition risk binding does not match this dataset")
    audit = corpus_admission.get("curator_audit")
    if not isinstance(audit, Mapping) or set(audit) != _CURATOR_AUDIT_KEYS or audit.get("receipt_schema_version") != "bhm.semantic-assurance-curator-audit-receipt.v1":
        raise SemanticAssuranceLiveError("semantic assurance curator audit binding is invalid")
    normalized_audit = {
        key: _sha256_digest(audit.get(key), f"curator audit {key}")
        for key in sorted(_CURATOR_AUDIT_KEYS - {"receipt_schema_version"})
    }
    result = {
        "receipt_schema_version": schema_version,
        "admission_digest": _sha256_digest(receipt.get("admission_digest"), "curator-audited label admission digest"),
        "corpus_pre_admission_digest": corpus_pre_admission_digest,
        "corpus_admission_digest": corpus_admission_digest,
        "admission_profile": _V6_CURATOR_AUDITED_ADMISSION_PROFILE,
        "partition_risk_counts": expected_partition_risk_counts,
        "v6_control_binding_digest": _sha256_digest(corpus_admission.get("v6_control_binding_digest"), "curator-audited V6 control binding digest"),
        "curator_audit": {"receipt_schema_version": audit["receipt_schema_version"], **normalized_audit},
        "sealed_readiness_strata_binding_digest": _sha256_digest(
            assurance.get("sealed_readiness_strata_binding_digest"),
            "curator-audited V5 strata binding digest",
        ),
        "sealed_readiness_strata_minimum_population": 80,
    }
    if schema_version == _LABEL_ADMISSION_RECEIPT_V6:
        from .semantic_assurance_label_admission import _v6_sealed_risk_label_matrix_binding

        expected_matrix = _v6_sealed_risk_label_matrix_binding(
            dataset["cases"],
            {str(case["case_id"]): {"labels": case["expected"]} for case in dataset["cases"]},
        )
        if (
            assurance.get("v6_sealed_risk_label_matrix_bound") is not True
            or assurance.get("sealed_risk_label_matrix_binding_digest") != expected_matrix["sealed_risk_label_matrix_binding_digest"]
            or assurance.get("sealed_risk_label_matrix_minimum_risk_classes") != 2
            or assurance.get("sealed_aligned_conflict_label_minimum_population") != 50
        ):
            raise SemanticAssuranceLiveError("semantic assurance risk-label matrix binding is invalid")
        result.update(expected_matrix)
    return result


def _canonical_dataset_rows(dataset: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build the exact content-free corpus rows used by admission and readiness."""

    rows = [
        {
            "case_id": str(case["case_id"]),
            "partition": str(case["partition"]),
            "risk_class": str(case["risk_class"]),
            "support_ids": sorted(str(item) for item in case["support_ids"]),
        }
        for case in dataset["cases"]
    ]
    rows.sort(key=lambda row: row["case_id"])
    return rows


def _partition_risk_counts(dataset: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """Return the exact, content-free V6 partition-risk distribution."""

    rows = _canonical_dataset_rows(dataset)
    partitions = ("development", "validation", "sealed_holdout")
    minimums = {"development": 220, "validation": 220, "sealed_holdout": 400}
    risks = sorted(_V6_RISK_CLASSES)
    if any(sum(row["partition"] == partition for row in rows) < minimums[partition] for partition in partitions):
        raise SemanticAssuranceLiveError("semantic assurance V6 partition coverage is invalid")
    result = {
        partition: {
            risk: sum(row["partition"] == partition and row["risk_class"] == risk for row in rows)
            for risk in risks
        }
        for partition in partitions
    }
    if any(count < 1 for partition in result.values() for count in partition.values()):
        raise SemanticAssuranceLiveError("semantic assurance V6 partition risk coverage is invalid")
    return result


def _partition_case_binding_digest(dataset: Mapping[str, Any], partition: str) -> str:
    selected = str(partition).strip().casefold()
    rows = [row for row in _canonical_dataset_rows(dataset) if row["partition"] == selected]
    if not rows:
        raise SemanticAssuranceLiveError("semantic assurance live partition is invalid")
    return _digest(rows)


def _inventory(client: httpx.Client, requested_model: Mapping[str, str]) -> dict[str, Any]:
    try:
        payload = _response_json(client.get("/api/v1/models"))
    except (httpx.HTTPError, ValueError) as exc:
        raise _LiveFailure("inventory", "inventory_unavailable") from exc
    models = payload.get("models") if isinstance(payload, Mapping) else None
    if not isinstance(models, list):
        raise _LiveFailure("inventory", "inventory_invalid")
    llms = [item for item in models if isinstance(item, Mapping) and item.get("type") == "llm"]
    loaded_llms = [item for item in llms if isinstance(item.get("loaded_instances"), list) and item["loaded_instances"]]
    requested = next((item for item in llms if item.get("key") == requested_model["model_id"]), None)
    if not isinstance(requested, Mapping):
        raise _LiveFailure("inventory", "requested_model_missing")
    variant = str(requested.get("selected_variant") or "").strip().casefold()
    instances = requested.get("loaded_instances")
    if variant != requested_model["model_variant"].casefold() or not isinstance(instances, list) or len(instances) != 1:
        raise _LiveFailure("inventory", "requested_model_not_exactly_loaded")
    if len(loaded_llms) != 1 or loaded_llms[0].get("key") != requested_model["model_id"]:
        raise _LiveFailure("inventory", "additional_loaded_llm")
    return {
        "exact_requested_model_loaded": True,
        "only_requested_llm_loaded": True,
        "loaded_llm_count": 1,
    }


def _chat(client: httpx.Client, request: Mapping[str, Any]) -> dict[str, Any]:
    try:
        payload = _response_json(client.post("/api/v1/chat", json=dict(request)))
    except (httpx.HTTPError, ValueError) as exc:
        raise _LiveFailure("chat", "transport_or_response_failed") from exc
    if not isinstance(payload, Mapping):
        raise _LiveFailure("chat", "response_invalid")
    output = payload.get("output")
    if not isinstance(output, list) or len(output) != 1 or not isinstance(output[0], Mapping):
        raise _LiveFailure("chat", "response_invalid")
    content = output[0].get("content")
    if not isinstance(content, str) or not content or len(content) > MAX_RESPONSE_BYTES:
        raise _LiveFailure("parser", "malformed_classification")
    stats = payload.get("stats")
    load_time = stats.get("model_load_time_seconds") if isinstance(stats, Mapping) else None
    if load_time is None:
        # LM Studio does not expose this optional metric on every compatible
        # endpoint.  The client still proves the strict no-load boundary by
        # inventory before/after and by never calling a model-load route; the
        # receipt labels the metric as unavailable instead of inventing zero.
        return {"content": content, "model_load_event": None}
    if not isinstance(load_time, (int, float)) or float(load_time) < 0:
        raise _LiveFailure("chat", "model_load_evidence_invalid")
    return {"content": content, "model_load_event": float(load_time) > 0}


def _chat_payload(case: Mapping[str, Any], model_id: str) -> dict[str, Any]:
    supports = "\n".join(
        f"- support_ref=support_{index}: {item['evidence']}"
        for index, item in enumerate(case["supports"], start=1)
    )
    return {
        "model": model_id,
        "input": (
            f"{_SYSTEM_PROMPT}\n<UNTRUSTED_CASE>\nTask: {case['prompt']}\n"
            f"Candidate: {case['candidate']}\nSupports:\n{supports}\n</UNTRUSTED_CASE>\n"
            "Return the exact JSON classifier now."
        ),
        "temperature": 0,
        "reasoning": "off",
        "store": False,
        "max_output_tokens": 400,
    }


def _parse_classification(content: str, case: Mapping[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise _LiveFailure("parser", "malformed_classification") from exc
    if not isinstance(parsed, Mapping) or set(parsed) != _CLASSIFIER_KEYS:
        raise _LiveFailure("parser", "malformed_classification")
    try:
        factuality = _label(parsed.get("factuality"), _FACTUALITY)
        entailment = _label(parsed.get("entailment"), _ENTAILMENT)
        conflict = _label(parsed.get("conflict"), _CONFLICT)
        operation = _label(parsed.get("operation"), _OPERATION)
        raw_relevance = parsed.get("support_relevance")
        if not isinstance(raw_relevance, Mapping):
            raise ValueError
        support_aliases = {
            f"support_{index}": str(item["support_id"])
            for index, item in enumerate(case["supports"], start=1)
        }
        if set(raw_relevance) != set(support_aliases):
            raise ValueError
        relevance = {
            support_aliases[str(alias)]: _label(value, _RELEVANCE)
            for alias, value in raw_relevance.items()
        }
    except (TypeError, ValueError):
        raise _LiveFailure("parser", "malformed_classification") from None
    return {
        "case_id": str(case["case_id"]),
        "factuality": factuality,
        "entailment": entailment,
        "conflict": conflict,
        "operation": operation,
        "support_relevance": dict(sorted(relevance.items())),
    }


def _receipt(
    *,
    dataset: Mapping[str, Any],
    partition: str,
    endpoint: str,
    requested_model: Mapping[str, str],
    repeats: int,
    reports: Sequence[Mapping[str, Any]],
    before_inventory: Mapping[str, Any] | None,
    after_inventory: Mapping[str, Any] | None,
    failure: Mapping[str, Any] | None,
    chat_call_count: int,
    model_load_events: int,
    model_load_evidence_available: bool,
    elapsed_ms: float,
    repeat_executions: Sequence[Mapping[str, Any]],
    event_records: Sequence[Mapping[str, Any]],
    label_admission: Mapping[str, Any] | None,
) -> dict[str, Any]:
    all_reports_valid = len(reports) == repeats and all(bool(report["gate"]["mechanical_valid"]) for report in reports)
    deterministic = len(reports) == repeats and len({str(report.get("prediction_digest")) for report in reports}) == 1 and len({str(report.get("results_digest")) for report in reports}) == 1
    expected_repeat_numbers = list(range(1, repeats + 1))
    repeat_ledger_valid = [entry.get("repeat_number") for entry in repeat_executions] == expected_repeat_numbers and all(
        isinstance(entry, Mapping)
        and set(entry) == {"repeat_number", "case_count", "case_id_set_digest", "chat_call_count", "prediction_digest", "execution_digest"}
        and _digest({key: entry[key] for key in entry if key != "execution_digest"}) == entry["execution_digest"]
        for entry in repeat_executions
    )
    repeat_reports_bound = _repeat_reports_bind_ledger(reports, repeat_executions)
    event_chain = _event_chain(event_records, before_inventory, after_inventory)
    event_chain_valid = _event_chain_is_valid(event_chain, chat_call_count)
    v6_label_binding = (
        _v6_label_admission_binding(label_admission, dataset)
        if label_admission is not None and label_admission.get("schema_version") == _LABEL_ADMISSION_RECEIPT_V3
        else None
    )
    curator_audited_label_binding = (
        _v6_curator_audited_label_admission_binding(label_admission, dataset)
        if label_admission is not None and label_admission.get("schema_version") in {_LABEL_ADMISSION_RECEIPT_V5, _LABEL_ADMISSION_RECEIPT_V6}
        else None
    )
    forward_label_binding = curator_audited_label_binding or v6_label_binding
    model_boundary = (
        before_inventory is not None
        and after_inventory is not None
        and (not model_load_evidence_available or model_load_events == 0)
    )
    checks = {
        "exact_requested_model_loaded_before": before_inventory is not None,
        "exact_requested_model_loaded_after": after_inventory is not None,
        "only_requested_llm_loaded_before": before_inventory is not None,
        "only_requested_llm_loaded_after": after_inventory is not None,
        "all_classifications_parsed": failure is None,
        "offline_mechanical_valid": all_reports_valid,
        "deterministic_repeat_agreement": deterministic,
        "exact_repeat_execution_coverage": repeat_ledger_valid and repeat_reports_bound and sum(int(entry["chat_call_count"]) for entry in repeat_executions) == chat_call_count,
        "content_free_event_chain_integrity": event_chain_valid,
        "model_boundaries_intact": model_boundary,
    }
    if forward_label_binding is not None:
        checks["v6_label_admission_bound"] = _v6_reports_bind_label(
            reports,
            forward_label_binding,
            expected_count=repeats,
        )
    mechanical_valid = all(checks.values())
    core = {
        "schema_version": (
            LIVE_RECEIPT_SCHEMA_VERSION_V6
            if curator_audited_label_binding is not None and curator_audited_label_binding["receipt_schema_version"] == _LABEL_ADMISSION_RECEIPT_V6
            else LIVE_RECEIPT_SCHEMA_VERSION_V5
            if curator_audited_label_binding is not None
            else LIVE_RECEIPT_SCHEMA_VERSION_V4 if v6_label_binding is not None else LIVE_RECEIPT_SCHEMA_VERSION
        ),
        "mode": "live-read-only-local-classifier",
        "dataset": {
            "dataset_id": str(dataset["dataset_id"]),
            "fixture_digest": str(dataset["fixture_digest"]),
            "label_digest": str(dataset["label_digest"]),
        },
        "model": {
            "model_id": requested_model["model_id"],
            "model_variant": requested_model["model_variant"],
            "endpoint": endpoint,
        },
        "prompt_contract": {"name": PROMPT_CONTRACT, "digest": _digest(PROMPT_CONTRACT)},
        "execution": {
            "partition": partition,
            "repeats_requested": repeats,
            "repeats_completed": len(reports),
            "chat_call_count": chat_call_count,
            "repeat_executions": list(repeat_executions),
            "event_chain": event_chain,
            "temperature": 0,
            "reasoning": "off",
            "store": False,
            "allowed_http_routes": ["GET /api/v1/models", "POST /api/v1/chat"],
            "model_load_requested": False,
            "model_unload_requested": False,
            "model_download_requested": False,
            "model_configuration_requested": False,
            "model_load_telemetry": "available" if model_load_evidence_available else "unavailable",
            "zero_model_load_events_when_observed": model_load_events == 0 if model_load_evidence_available else None,
            "sqlite_mutation": False,
            "mem0_mutation": False,
            "qdrant_mutation": False,
            "mcp_or_tools_used": False,
            "queue_persistence": False,
            "automatic_apply": False,
            "elapsed_ms": round(elapsed_ms, 3),
        },
        "inventory": {"before": before_inventory, "after": after_inventory},
        "model_load_events": model_load_events if model_load_evidence_available else None,
        "model_load_telemetry": "available" if model_load_evidence_available else "unavailable",
        "offline_reports": list(reports),
        "failure": dict(failure) if failure is not None else None,
        "gate": {
            "checks": checks,
            "mechanical_valid": mechanical_valid,
            "accepted": mechanical_valid,
            "acceptance_scope": "proposal_only_fixture_measurement",
            "automatic_apply_ready": False,
            "automatic_apply_decision": "no_go",
        },
        "limitations": [
            "the live receipt excludes prompts, evidence and generated model text",
            "a passing fixture measurement is not universal factuality, entailment, relevance, or hidden-conflict proof",
            "model-load telemetry may be unavailable; the client never calls a model-load route and checks exact inventory before and after",
            "automatic semantic apply remains no_go",
        ],
    }
    if forward_label_binding is not None:
        core["label_admission"] = forward_label_binding
    return {**core, "report_digest": _digest(core)}



def _loopback_endpoint(value: str) -> str:
    parsed = urlparse(str(value))
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"} or not parsed.port:
        raise SemanticAssuranceLiveError("live semantic assurance requires an exact loopback HTTP endpoint")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise SemanticAssuranceLiveError("live semantic assurance endpoint must not include a path")
    host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
    return f"http://{host}:{parsed.port}"
def _timeout(value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise SemanticAssuranceLiveError("live semantic assurance timeout is invalid") from exc
    if not 0 < parsed <= MAX_TIMEOUT_SECONDS:
        raise SemanticAssuranceLiveError("live semantic assurance timeout is outside bounds")
    return parsed
def _repeat_count(value: int) -> int:
    if not isinstance(value, int) or not 1 <= value <= MAX_REPEATS:
        raise SemanticAssuranceLiveError("live semantic assurance repeats are outside bounds")
    return value
def _requested_model(model_id: str, model_variant: str) -> dict[str, str]:
    requested_id = _bounded_text(model_id, "model id", 200)
    requested_variant = _bounded_text(model_variant, "model variant", 240)
    if requested_id != DEFAULT_MODEL_ID or requested_variant.casefold() != DEFAULT_MODEL_VARIANT:
        raise SemanticAssuranceLiveError("live semantic assurance accepts only the frozen preloaded model contract")
    return {"model_id": requested_id, "model_variant": requested_variant.casefold()}
def _response_json(response: httpx.Response) -> Any:
    response.raise_for_status()
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ValueError("response exceeds bound")
    return response.json()



__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL_ID",
    "DEFAULT_MODEL_VARIANT",
    "LIVE_PRIVATE_EVALUATION_BUNDLE_SCHEMA_VERSION_V6",
    "LIVE_PUBLIC_RECEIPT_SCHEMA_VERSION_V6",
    "LIVE_RECEIPT_SCHEMA_VERSION",
    "LIVE_RECEIPT_SCHEMA_VERSION_V4",
    "LIVE_RECEIPT_SCHEMA_VERSION_V5",
    "LIVE_RECEIPT_SCHEMA_VERSION_V6",
    "PROMPT_CONTRACT",
    "SemanticAssuranceLiveError",
    "build_semantic_assurance_live_v6_evidence",
    "run_live_semantic_assurance",
    "verify_semantic_assurance_live_v6_evidence",
    "write_live_semantic_assurance_receipt",
    "write_semantic_assurance_private_evaluation_bundle_v6",
    "write_semantic_assurance_public_live_receipt_v6",
]
