"""Fail-closed structural admission for private semantic-assurance review packets.

This is deliberately a pure Mapping validator.  It does not read a corpus from
disk, call a model, authenticate people, or mutate BHM storage.  A successful
receipt says that a *declared* private corpus is structurally fit for blinded
human review; it does not prove the curator's identity, semantic independence,
or semantic truth.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA_VERSION = "bhm.semantic-assurance-corpus-admission.v1"
RECEIPT_SCHEMA_VERSION = "bhm.semantic-assurance-corpus-admission-receipt.v1"
_PARTITIONS = frozenset({"development", "validation", "sealed_holdout"})
_ISOLATION_KEYS = frozenset({
    "family_id", "group_id", "source_group_id", "source_theme",
    "prompt_template_id", "paraphrase_cluster_id",
})
_REQUIRED_RISKS = frozenset({
    "factuality", "entailment", "relevance", "direct_conflict",
    "temporal_conflict", "scope_conflict", "implicit_conflict", "injection",
    "cross_project", "retrieval_pressure", "abstention",
})
_CONFLICT_BUCKETS = frozenset({"direct_conflict", "temporal_conflict", "scope_conflict", "implicit_conflict"})
_ID_DISCLOSURE = re.compile(
    r"(?:development|validation|sealed|holdout|factuality|entailment|relevance|"
    r"conflict|injection|cross[_-]?project|retrieval|abstention)", re.IGNORECASE
)
_LOCATOR = re.compile(
    r"(?:\b(?:source|anchor|locator)[_-]?(?:id|ref)?\b|(?:^|[\s(])(?:[A-Za-z]:[\\/]|(?:\.{0,2}/)?\.?docs/|[^\s]+\.md(?::\d+)?)(?=$|[\s)])|"
    r"\b(?:line|section|heading|строк[аи]?|раздел|якор[ья])\s*(?:\d+|#))",
    re.IGNORECASE,
)


class SemanticAssuranceCorpusAdmissionError(ValueError):
    """The proposed private review corpus is not structurally admissible."""


def admit_semantic_assurance_corpus_for_human_review(raw: object) -> dict[str, Any]:
    """Return a content-free receipt or fail before private packet distribution."""

    if not isinstance(raw, Mapping) or set(raw) != {
        "schema_version", "corpus", "packets", "ledgers", "curator_attestation",
        "curator_ledger", "distribution",
    } or raw.get("schema_version") != SCHEMA_VERSION:
        raise SemanticAssuranceCorpusAdmissionError("corpus admission schema is invalid")
    curator_attestation = _curator_attestation(
        raw.get("curator_attestation"), raw.get("corpus"), raw.get("curator_ledger")
    )
    if raw.get("distribution") != {"permitted": True}:
        raise SemanticAssuranceCorpusAdmissionError("explicit reviewer distribution permission is required")

    cases = _cases(raw.get("corpus"))
    by_id = {case["case_id"]: case for case in cases}
    _coverage(cases)
    _split_isolation(cases)
    _split_text_collisions(cases)
    packet_rows = _packets(raw.get("packets"), by_id)
    _blank_ledgers(raw.get("ledgers"), packet_rows)
    _curator_ledger(raw.get("curator_ledger"), cases)

    packet_digests = {name: [_digest(case) for case in rows] for name, rows in packet_rows.items()}
    dataset_binding = _dataset_binding(cases)
    core = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "ok": True,
        "corpus": {
            "case_count": len(cases),
            "partition_counts": {partition: sum(case["partition"] == partition for case in cases) for partition in sorted(_PARTITIONS)},
            "sealed_support_count": sum(len(case["supports"]) for case in cases if case["partition"] == "sealed_holdout"),
            "dataset_binding_digest": dataset_binding["dataset_binding_digest"],
            "sealed_case_binding_digest": dataset_binding["sealed_case_binding_digest"],
            "sealed_support_binding_digest": dataset_binding["sealed_support_binding_digest"],
            "risk_counts": dataset_binding["risk_counts"],
            "sealed_risk_counts": dataset_binding["sealed_risk_counts"],
            "packet_payload_set_digest": _digest(sorted(packet_digests["reviewer_a"])),
            "curator_ledger_digest": _digest(raw["curator_ledger"]),
            "curator_attestation_digest": _digest(curator_attestation),
        },
        "review": {
            "reviewer_packet_count": 2,
            "packet_case_count": len(cases),
            "same_payload_digest_set": True,
            "same_packet_positions": 0,
            "blank_ledger_coverage": True,
        },
        "assurance": {
            "status": "structurally_admitted_for_human_review_not_semantic_independence_proven",
            "curator_claimed_attestation": True,
            "curator_attestation_bound_to_corpus": True,
            "curator_identity_externally_authenticated": False,
            "semantic_independence_proven": False,
            "semantic_truth_proven": False,
            "automatic_apply_ready": False,
            "automatic_apply_decision": "no_go",
        },
        "execution": {
            "network_used": False,
            "model_called": False,
            "sqlite_mutation": False,
            "mem0_mutation": False,
            "qdrant_mutation": False,
            "automatic_apply": False,
        },
    }
    return {**core, "admission_digest": _digest(core)}


def admit_semantic_assurance_corpus(raw: object) -> dict[str, Any]:
    """Short alias for callers that use the admission stage by itself."""

    return admit_semantic_assurance_corpus_for_human_review(raw)


def verify_semantic_assurance_corpus_admission(value: object) -> dict[str, Any]:
    """Verify a content-free receipt before it is referenced by later stages."""

    expected = {"schema_version", "ok", "corpus", "review", "assurance", "execution", "admission_digest"}
    if not isinstance(value, Mapping) or set(value) != expected or value.get("schema_version") != RECEIPT_SCHEMA_VERSION or value.get("ok") is not True:
        raise SemanticAssuranceCorpusAdmissionError("corpus admission receipt schema is invalid")
    corpus = value.get("corpus")
    review = value.get("review")
    if not isinstance(corpus, Mapping) or set(corpus) != {
        "case_count", "partition_counts", "sealed_support_count", "dataset_binding_digest",
        "sealed_case_binding_digest", "sealed_support_binding_digest",
        "risk_counts", "sealed_risk_counts", "packet_payload_set_digest",
        "curator_ledger_digest", "curator_attestation_digest",
    }:
        raise SemanticAssuranceCorpusAdmissionError("corpus admission receipt corpus is invalid")
    if not isinstance(review, Mapping) or review != {"reviewer_packet_count": 2, "packet_case_count": corpus.get("case_count"), "same_payload_digest_set": True, "same_packet_positions": 0, "blank_ledger_coverage": True}:
        raise SemanticAssuranceCorpusAdmissionError("corpus admission receipt review is invalid")
    counts = corpus.get("partition_counts")
    if not isinstance(counts, Mapping) or set(counts) != _PARTITIONS or any(not _positive_int(counts[key]) for key in counts):
        raise SemanticAssuranceCorpusAdmissionError("corpus admission receipt partition counts are invalid")
    if (
        counts["development"] < 80 or counts["validation"] < 80 or counts["sealed_holdout"] < 400
        or corpus.get("case_count") != sum(counts.values()) or corpus.get("sealed_support_count", 0) < 800
    ):
        raise SemanticAssuranceCorpusAdmissionError("corpus admission receipt coverage is below the admission minimum")
    if not _positive_int(corpus.get("case_count")) or not _positive_int(corpus.get("sealed_support_count")):
        raise SemanticAssuranceCorpusAdmissionError("corpus admission receipt counts are invalid")
    risk_counts = corpus.get("risk_counts")
    sealed_risk_counts = corpus.get("sealed_risk_counts")
    if not _risk_count_map(risk_counts, "corpus admission receipt risk counts"):
        raise SemanticAssuranceCorpusAdmissionError("corpus admission receipt risk counts are invalid")
    if not _risk_count_map(sealed_risk_counts, "corpus admission receipt sealed risk counts"):
        raise SemanticAssuranceCorpusAdmissionError("corpus admission receipt sealed risk counts are invalid")
    if sum(risk_counts.values()) != corpus["case_count"]:
        raise SemanticAssuranceCorpusAdmissionError("corpus admission receipt risk counts do not cover cases")
    if sum(sealed_risk_counts.values()) != counts["sealed_holdout"]:
        raise SemanticAssuranceCorpusAdmissionError("corpus admission receipt sealed risk counts do not cover holdout")
    if any(sealed_risk_counts[risk] > risk_counts[risk] for risk in _REQUIRED_RISKS):
        raise SemanticAssuranceCorpusAdmissionError("corpus admission receipt sealed risk counts exceed corpus counts")
    for key in ("dataset_binding_digest", "sealed_case_binding_digest", "sealed_support_binding_digest"):
        if not _sha256(corpus.get(key)):
            raise SemanticAssuranceCorpusAdmissionError(f"corpus admission receipt {key.replace('_', ' ')} is invalid")
    for key in ("packet_payload_set_digest", "curator_ledger_digest", "curator_attestation_digest"):
        if not _sha256(corpus.get(key)):
            raise SemanticAssuranceCorpusAdmissionError("corpus admission receipt digest is invalid")
    assurance = {
        "status": "structurally_admitted_for_human_review_not_semantic_independence_proven",
        "curator_claimed_attestation": True,
        "curator_attestation_bound_to_corpus": True,
        "curator_identity_externally_authenticated": False,
        "semantic_independence_proven": False,
        "semantic_truth_proven": False,
        "automatic_apply_ready": False,
        "automatic_apply_decision": "no_go",
    }
    execution = {"network_used": False, "model_called": False, "sqlite_mutation": False, "mem0_mutation": False, "qdrant_mutation": False, "automatic_apply": False}
    if value.get("assurance") != assurance or value.get("execution") != execution:
        raise SemanticAssuranceCorpusAdmissionError("corpus admission receipt safety boundary is invalid")
    core = {"schema_version": RECEIPT_SCHEMA_VERSION, "ok": True, "corpus": dict(corpus), "review": dict(review), "assurance": assurance, "execution": execution}
    if value.get("admission_digest") != _digest(core):
        raise SemanticAssuranceCorpusAdmissionError("corpus admission receipt digest mismatch")
    return {**core, "admission_digest": value["admission_digest"]}


def _cases(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, Mapping) or set(raw) != {"cases"} or not _sequence(raw.get("cases")):
        raise SemanticAssuranceCorpusAdmissionError("corpus cases are invalid")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_case in raw["cases"]:
        if not isinstance(raw_case, Mapping) or set(raw_case) != {"case_id", "partition", "risk_class", "isolation", "prompt", "candidate", "supports"}:
            raise SemanticAssuranceCorpusAdmissionError("corpus case schema is invalid")
        case_id = _text(raw_case.get("case_id"), "case id", 160)
        if case_id in seen or _ID_DISCLOSURE.search(case_id):
            raise SemanticAssuranceCorpusAdmissionError("case ids must be unique opaque non-risk identifiers")
        partition = raw_case.get("partition")
        risk = raw_case.get("risk_class")
        if partition not in _PARTITIONS or risk not in _REQUIRED_RISKS:
            raise SemanticAssuranceCorpusAdmissionError("corpus partition or risk class is invalid")
        isolation = raw_case.get("isolation")
        if not isinstance(isolation, Mapping) or set(isolation) != _ISOLATION_KEYS:
            raise SemanticAssuranceCorpusAdmissionError("corpus split isolation declaration is invalid")
        normal_isolation = {key: _text(isolation.get(key), "isolation identifier", 160) for key in sorted(_ISOLATION_KEYS)}
        supports = _supports(raw_case.get("supports"))
        result.append({"case_id": case_id, "partition": partition, "risk_class": risk, "isolation": normal_isolation, "prompt": _text(raw_case.get("prompt"), "prompt", 10000), "candidate": _text(raw_case.get("candidate"), "candidate", 10000), "supports": supports})
        seen.add(case_id)
    return result


def _supports(raw: object) -> list[dict[str, str]]:
    if not _sequence(raw):
        raise SemanticAssuranceCorpusAdmissionError("case supports are invalid")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {"support_id", "evidence"}:
            raise SemanticAssuranceCorpusAdmissionError("support schema is invalid")
        support_id = _text(item.get("support_id"), "support id", 160)
        if support_id in seen or _ID_DISCLOSURE.search(support_id):
            raise SemanticAssuranceCorpusAdmissionError("support ids must be unique opaque non-risk identifiers")
        result.append({"support_id": support_id, "evidence": _text(item.get("evidence"), "support evidence", 10000)})
        seen.add(support_id)
    return result


def _dataset_binding(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return the content-free identity of the admitted final dataset.

    Only opaque case/support identities and declared partition/risk metadata
    are included.  Prompt, candidate and evidence text never enters this
    binding, so the receipt remains safe to expose to later stages.  Rows are
    sorted by case id and support identities are sorted to make the digest
    independent of JSON/input ordering while still binding the exact ID set.
    """

    rows = [
        {
            "case_id": str(case["case_id"]),
            "partition": str(case["partition"]),
            "risk_class": str(case["risk_class"]),
            "support_ids": sorted(str(item["support_id"]) for item in case["supports"]),
        }
        for case in cases
    ]
    rows.sort(key=lambda row: row["case_id"])
    sealed_rows = [row for row in rows if row["partition"] == "sealed_holdout"]
    sealed_support_rows = [
        {"case_id": row["case_id"], "support_ids": row["support_ids"]}
        for row in sealed_rows
    ]
    risk_counts = {risk: sum(case["risk_class"] == risk for case in cases) for risk in sorted(_REQUIRED_RISKS)}
    sealed_risk_counts = {
        risk: sum(case["partition"] == "sealed_holdout" and case["risk_class"] == risk for case in cases)
        for risk in sorted(_REQUIRED_RISKS)
    }
    return {
        "dataset_binding_digest": _digest(rows),
        "sealed_case_binding_digest": _digest(sealed_rows),
        "sealed_support_binding_digest": _digest(sealed_support_rows),
        "risk_counts": risk_counts,
        "sealed_risk_counts": sealed_risk_counts,
    }


def _risk_count_map(value: object, field: str) -> bool:
    if not isinstance(value, Mapping) or set(value) != _REQUIRED_RISKS:
        return False
    if any(not isinstance(value[risk], int) or isinstance(value[risk], bool) or value[risk] < 0 for risk in _REQUIRED_RISKS):
        return False
    return True


def _coverage(cases: Sequence[Mapping[str, Any]]) -> None:
    counts = {partition: sum(case["partition"] == partition for case in cases) for partition in _PARTITIONS}
    if counts["development"] < 80 or counts["validation"] < 80 or counts["sealed_holdout"] < 400:
        raise SemanticAssuranceCorpusAdmissionError("corpus partition coverage is below the admission minimum")
    if sum(len(case["supports"]) for case in cases if case["partition"] == "sealed_holdout") < 800:
        raise SemanticAssuranceCorpusAdmissionError("sealed holdout support coverage is below the admission minimum")
    risks = {case["risk_class"] for case in cases}
    if risks != _REQUIRED_RISKS:
        raise SemanticAssuranceCorpusAdmissionError("corpus required risk coverage is incomplete")
    sealed = [case for case in cases if case["partition"] == "sealed_holdout"]
    conflict_counts = {risk: sum(case["risk_class"] == risk for case in sealed) for risk in _CONFLICT_BUCKETS}
    if any(count < 50 for count in conflict_counts.values()) or sum(conflict_counts.values()) < 200:
        raise SemanticAssuranceCorpusAdmissionError("sealed conflict bucket coverage is below the admission minimum")


def _split_isolation(cases: Sequence[Mapping[str, Any]]) -> None:
    for key in _ISOLATION_KEYS:
        values: dict[str, set[str]] = {partition: set() for partition in _PARTITIONS}
        for case in cases:
            value = str(case["isolation"][key])
            if value in values[case["partition"]]:
                raise SemanticAssuranceCorpusAdmissionError("declared split isolation identifier is reused")
            values[case["partition"]].add(value)
        if any(values[first] & values[second] for first in _PARTITIONS for second in _PARTITIONS if first < second):
            raise SemanticAssuranceCorpusAdmissionError("declared split isolation crosses partitions")


def _split_text_collisions(cases: Sequence[Mapping[str, Any]]) -> None:
    seen: dict[str, str] = {}
    for case in cases:
        texts = [case["prompt"], case["candidate"], *(item["evidence"] for item in case["supports"])]
        for value in texts:
            normalized = _normalized_text(value)
            prior = seen.get(normalized)
            if prior is not None and prior != case["partition"]:
                raise SemanticAssuranceCorpusAdmissionError("normalized reviewer text collides across partitions")
            seen[normalized] = case["partition"]


def _packets(raw: object, by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(raw, Mapping) or set(raw) != {"reviewer_a", "reviewer_b"}:
        raise SemanticAssuranceCorpusAdmissionError("reviewer packet set is invalid")
    result: dict[str, list[dict[str, Any]]] = {}
    for name in ("reviewer_a", "reviewer_b"):
        packet = raw.get(name)
        if not isinstance(packet, Mapping) or set(packet) != {"cases"} or not _sequence(packet.get("cases")):
            raise SemanticAssuranceCorpusAdmissionError("reviewer packet schema is invalid")
        rows: list[dict[str, Any]] = []
        for row in packet["cases"]:
            if not isinstance(row, Mapping) or set(row) != {"case_id", "prompt", "candidate", "supports"}:
                raise SemanticAssuranceCorpusAdmissionError("reviewer packet case leaks non-review metadata")
            case_id = _text(row.get("case_id"), "packet case id", 160)
            if case_id not in by_id or _ID_DISCLOSURE.search(case_id):
                raise SemanticAssuranceCorpusAdmissionError("reviewer packet case id is invalid or discloses split/risk")
            candidate = {"case_id": case_id, "prompt": _text(row.get("prompt"), "packet prompt", 10000), "candidate": _text(row.get("candidate"), "packet candidate", 10000), "supports": _supports(row.get("supports"))}
            canonical = by_id[case_id]
            if candidate != {key: canonical[key] for key in ("case_id", "prompt", "candidate", "supports")}:
                raise SemanticAssuranceCorpusAdmissionError("reviewer packet payload does not exactly bind corpus")
            if any(_LOCATOR.search(value) for value in _packet_text(candidate)):
                raise SemanticAssuranceCorpusAdmissionError("reviewer packet leaks a source or anchor locator")
            rows.append(candidate)
        if len(rows) != len(by_id) or len({row["case_id"] for row in rows}) != len(rows):
            raise SemanticAssuranceCorpusAdmissionError("reviewer packet coverage is incomplete or duplicated")
        result[name] = rows
    digests = {name: [_digest(row) for row in rows] for name, rows in result.items()}
    if set(digests["reviewer_a"]) != set(digests["reviewer_b"]):
        raise SemanticAssuranceCorpusAdmissionError("reviewer packets do not have the same payload digest set")
    if any(left == right for left, right in zip(digests["reviewer_a"], digests["reviewer_b"], strict=True)):
        raise SemanticAssuranceCorpusAdmissionError("reviewer packets reuse the same positions")
    return result


def _blank_ledgers(raw: object, packets: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
    if not isinstance(raw, Mapping) or set(raw) != {"reviewer_a", "reviewer_b"}:
        raise SemanticAssuranceCorpusAdmissionError("reviewer ledgers are invalid")
    for name, rows in packets.items():
        ledger = raw.get(name)
        if not isinstance(ledger, Mapping) or set(ledger) != {"rows"} or not _sequence(ledger.get("rows")):
            raise SemanticAssuranceCorpusAdmissionError("blank reviewer ledger schema is invalid")
        expected = [(row["case_id"], tuple(item["support_id"] for item in row["supports"])) for row in rows]
        observed: list[tuple[str, tuple[str, ...]]] = []
        for item in ledger["rows"]:
            if not isinstance(item, Mapping) or set(item) != {"case_id", "support_ids", "labels"} or item.get("labels") is not None or not _sequence(item.get("support_ids")):
                raise SemanticAssuranceCorpusAdmissionError("reviewer ledger must be blank and exact")
            observed.append((_text(item.get("case_id"), "ledger case id", 160), tuple(_text(value, "ledger support id", 160) for value in item["support_ids"])))
        if observed != expected:
            raise SemanticAssuranceCorpusAdmissionError("blank reviewer ledger does not cover packet in exact order")


def _curator_ledger(raw: object, cases: Sequence[Mapping[str, Any]]) -> None:
    if not isinstance(raw, Mapping) or set(raw) != {"rows"} or not _sequence(raw.get("rows")):
        raise SemanticAssuranceCorpusAdmissionError("curator ledger is invalid")
    expected = {str(case["case_id"]): case for case in cases}
    seen: set[str] = set()
    fragments: set[str] = set()
    identities: dict[str, dict[str, set[str]]] = {key: {partition: set() for partition in _PARTITIONS} for key in ("semantic_id", "proposition_id", "mechanism_id")}
    for row in raw["rows"]:
        if not isinstance(row, Mapping) or set(row) != {"case_id", "semantic_id", "proposition_id", "mechanism_id", "source_fragments"}:
            raise SemanticAssuranceCorpusAdmissionError("curator ledger row schema is invalid")
        case_id = _text(row.get("case_id"), "curator ledger case id", 160)
        if case_id not in expected or case_id in seen or not _sequence(row.get("source_fragments")):
            raise SemanticAssuranceCorpusAdmissionError("curator ledger coverage or source fragments are invalid")
        partition = str(expected[case_id]["partition"])
        for key in ("semantic_id", "proposition_id", "mechanism_id"):
            identity = _text(row.get(key), f"curator {key}", 160)
            if identity in identities[key][partition]:
                raise SemanticAssuranceCorpusAdmissionError("curator identity is reused within a split")
            if any(identity in values for other, values in identities[key].items() if other != partition):
                raise SemanticAssuranceCorpusAdmissionError("curator semantic identity crosses splits")
            identities[key][partition].add(identity)
        for fragment in row["source_fragments"]:
            value = _text(fragment, "curator source fragment", 64).lower()
            if not _sha256(value):
                raise SemanticAssuranceCorpusAdmissionError("curator source fragment must be a SHA-256 digest")
            if value in fragments:
                raise SemanticAssuranceCorpusAdmissionError("curator source fragment is reused")
            fragments.add(value)
        seen.add(case_id)
    if seen != set(expected):
        raise SemanticAssuranceCorpusAdmissionError("curator ledger does not cover every corpus case")


def _packet_text(case: Mapping[str, Any]) -> list[str]:
    return [str(case["prompt"]), str(case["candidate"]), *(str(item["evidence"]) for item in case["supports"])]


def _curator_attestation(raw: object, corpus: object, ledger: object) -> dict[str, object]:
    """Validate a claimed, digest-bound curator attestation without authentication.

    The attestation binds an opaque curator identity digest to the exact private
    corpus and curator ledger supplied for admission.  It cannot prove that the
    digest belongs to a particular human or that the claimed curation happened.
    """

    if not isinstance(raw, Mapping) or set(raw) != {"claimed", "curator_identity_sha256", "corpus_digest"}:
        raise SemanticAssuranceCorpusAdmissionError("curator attestation schema is invalid")
    identity = raw.get("curator_identity_sha256")
    claimed_digest = raw.get("corpus_digest")
    if raw.get("claimed") is not True or not _sha256(identity) or not _sha256(claimed_digest):
        raise SemanticAssuranceCorpusAdmissionError("curator digest-bound claimed attestation is required")
    expected_digest = _digest({"corpus": corpus, "curator_ledger": ledger})
    if claimed_digest != expected_digest:
        raise SemanticAssuranceCorpusAdmissionError("curator attestation does not bind the exact corpus")
    return {
        "claimed": True,
        "curator_identity_sha256": identity,
        "corpus_digest": claimed_digest,
    }


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise SemanticAssuranceCorpusAdmissionError(f"{field} is invalid")
    return value


def _sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) and bool(value)


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _sha256(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


__all__ = [
    "RECEIPT_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "SemanticAssuranceCorpusAdmissionError",
    "admit_semantic_assurance_corpus",
    "admit_semantic_assurance_corpus_for_human_review",
    "verify_semantic_assurance_corpus_admission",
]
