"""Fail-closed audit for a private V6 semantic-assurance curator ledger.

The audit reads explicitly supplied private JSON and a local canonical source
tree.  It validates declared source fragments and structural isolation, then
returns a content-free receipt.  It never derives reviewer cases, calls a
model or network service, opens BHM storage, or authorises automatic apply.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

ATOMIC_LEDGER_SCHEMA_VERSION = "bhm.semantic-assurance-human-curation-ledger.v1"
CONTROL_CELLS_SCHEMA_VERSION = "bhm.semantic-assurance-human-curation-controls.v1"
RECEIPT_SCHEMA_VERSION = "bhm.semantic-assurance-curator-audit-receipt.v1"

_PARTITIONS = frozenset({"development", "validation", "sealed_holdout"})
_RISK_CLASSES = frozenset({
    "factuality", "entailment", "relevance", "direct_conflict",
    "temporal_conflict", "scope_conflict", "implicit_conflict", "injection",
    "cross_project", "retrieval_pressure", "abstention",
})
_CONFLICT_RISKS = frozenset({"direct_conflict", "temporal_conflict", "scope_conflict", "implicit_conflict"})
_MINIMUMS = {"development": 220, "validation": 220, "sealed_holdout": 400}
_SUPPORT_BUCKETS = frozenset({"one", "two", "three_plus"})
_LENGTH_BUCKETS = frozenset({"short", "medium", "long"})
_DOCUMENT_GENRES = frozenset({"architecture", "api_contract", "governance", "runbook", "source_code", "test"})
_RELATION_FORMS = frozenset({"support", "contradiction", "absence", "temporal", "scope", "relevance"})
_ADVERSARIAL_FORMS = frozenset({"none", "ambiguity", "conflict", "cross_project", "injection", "retrieval_pressure"})
_DIMENSION_SURFACES = frozenset({"factuality", "entailment", "relevance", "safe_operation"})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")


class SemanticAssuranceCuratorAuditError(ValueError):
    """Private curator input cannot be structurally admitted."""


def audit_v6_curator_inputs(
    atomic_facts: object,
    control_cells: object,
    *,
    canonical_root: Path,
) -> dict[str, Any]:
    """Audit V6 curator inputs and return a content-free, no-go receipt.

    ``canonical_root`` is an explicit local repository root.  Allowed source
    fragments are only tracked/public ``docs/**/*.md`` and local canonical
    ``.docs/adr/**/*.md``.  The latter is content-bound but intentionally not
    presented as Git-revision evidence because it is local-only by policy.
    """

    root = _canonical_root(canonical_root)
    facts = _atomic_facts(atomic_facts, root)
    controls = _control_cells(control_cells, facts)
    _coverage(facts, controls)

    fact_rows = [
        {
            "atomic_fact_id": fact["atomic_fact_id"],
            "partition": fact["partition"],
        }
        for fact in facts.values()
    ]
    fact_rows.sort(key=lambda row: row["atomic_fact_id"])
    fragment_rows = [
        {
            "atomic_fact_id": fact["atomic_fact_id"],
            "canonical_fragment_sha256": fact["fragment_sha256"],
            "source_kind": fact["source_kind"],
        }
        for fact in facts.values()
    ]
    fragment_rows.sort(key=lambda row: row["atomic_fact_id"])
    control_rows = [
        {
            "atomic_fact_id": control["atomic_fact_id"],
            "risk_class": control["risk_class"],
            "support_count_bucket": control["support_count_bucket"],
            "approximate_length_bucket": control["approximate_length_bucket"],
            "document_genre": control["document_genre"],
            "relation_form": control["relation_form"],
            "adversarial_form": control["adversarial_form"],
            "dimension_surfaces": control["dimension_surfaces"],
        }
        for control in controls.values()
    ]
    control_rows.sort(key=lambda row: row["atomic_fact_id"])
    partition_counts = {partition: sum(fact["partition"] == partition for fact in facts.values()) for partition in sorted(_PARTITIONS)}
    partition_risk_counts = {
        partition: {
            risk: sum(facts[control["atomic_fact_id"]]["partition"] == partition and control["risk_class"] == risk for control in controls.values())
            for risk in sorted(_RISK_CLASSES)
        }
        for partition in sorted(_PARTITIONS)
    }
    sealed_conflict_counts = {risk: partition_risk_counts["sealed_holdout"][risk] for risk in sorted(_CONFLICT_RISKS)}
    local_adr_count = sum(fact["source_kind"] == "local_adr" for fact in facts.values())
    public_docs_count = len(facts) - local_adr_count
    core = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "ok": True,
        "audit": {
            "atomic_fact_count": len(facts),
            "partition_counts": partition_counts,
            "partition_risk_counts": partition_risk_counts,
            "sealed_conflict_counts": sealed_conflict_counts,
            "atomic_fact_binding_digest": _digest(fact_rows),
            "control_binding_digest": _digest(control_rows),
            "canonical_fragment_binding_digest": _digest(fragment_rows),
            "source_kind_counts": {"local_adr": local_adr_count, "public_docs": public_docs_count},
        },
        "assurance": {
            "canonical_fragments_revalidated": True,
            "source_fragment_provenance_proven": False,
            "curator_identity_authenticated": False,
            "semantic_independence_proven": False,
            "semantic_truth_proven": False,
            "reviewer_cases_constructed": False,
            "automatic_apply_ready": False,
            "decision": "no_go",
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
    return {**core, "audit_digest": _digest(core)}


def verify_v6_curator_audit_receipt(value: object) -> dict[str, Any]:
    """Verify a content-free curator-audit receipt before a later join stage."""

    if not isinstance(value, Mapping) or set(value) != {"schema_version", "ok", "audit", "assurance", "execution", "audit_digest"}:
        raise SemanticAssuranceCuratorAuditError("curator audit receipt schema is invalid")
    if value.get("schema_version") != RECEIPT_SCHEMA_VERSION or value.get("ok") is not True:
        raise SemanticAssuranceCuratorAuditError("curator audit receipt version is invalid")
    audit = value.get("audit")
    expected_audit = {
        "atomic_fact_count", "partition_counts", "partition_risk_counts", "sealed_conflict_counts",
        "atomic_fact_binding_digest", "control_binding_digest", "canonical_fragment_binding_digest", "source_kind_counts",
    }
    if not isinstance(audit, Mapping) or set(audit) != expected_audit:
        raise SemanticAssuranceCuratorAuditError("curator audit receipt content is invalid")
    if not _positive_int(audit.get("atomic_fact_count")) or not _partition_counts(audit.get("partition_counts")):
        raise SemanticAssuranceCuratorAuditError("curator audit receipt counts are invalid")
    if audit["atomic_fact_count"] != sum(audit["partition_counts"].values()):
        raise SemanticAssuranceCuratorAuditError("curator audit receipt count total is invalid")
    if any(audit["partition_counts"][partition] < _MINIMUMS[partition] for partition in _PARTITIONS):
        raise SemanticAssuranceCuratorAuditError("curator audit receipt coverage is below V6 minimum")
    if not _partition_risk_counts(audit.get("partition_risk_counts"), audit["partition_counts"]):
        raise SemanticAssuranceCuratorAuditError("curator audit receipt risk distribution is invalid")
    sealed = audit.get("sealed_conflict_counts")
    if not isinstance(sealed, Mapping) or set(sealed) != _CONFLICT_RISKS or any(not _positive_int(sealed.get(risk)) or sealed[risk] < 50 for risk in _CONFLICT_RISKS):
        raise SemanticAssuranceCuratorAuditError("curator audit receipt sealed conflict distribution is invalid")
    if sum(sealed.values()) < 200:
        raise SemanticAssuranceCuratorAuditError("curator audit receipt sealed conflict total is invalid")
    if not isinstance(audit.get("source_kind_counts"), Mapping) or audit["source_kind_counts"] != {
        "local_adr": audit["source_kind_counts"].get("local_adr"), "public_docs": audit["source_kind_counts"].get("public_docs")
    } or any(not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in audit["source_kind_counts"].values()) or sum(audit["source_kind_counts"].values()) != audit["atomic_fact_count"]:
        raise SemanticAssuranceCuratorAuditError("curator audit receipt source kinds are invalid")
    if any(not _is_sha256(audit.get(key)) for key in ("atomic_fact_binding_digest", "control_binding_digest", "canonical_fragment_binding_digest")):
        raise SemanticAssuranceCuratorAuditError("curator audit receipt digest is invalid")
    assurance = {
        "canonical_fragments_revalidated": True,
        "source_fragment_provenance_proven": False,
        "curator_identity_authenticated": False,
        "semantic_independence_proven": False,
        "semantic_truth_proven": False,
        "reviewer_cases_constructed": False,
        "automatic_apply_ready": False,
        "decision": "no_go",
    }
    execution = {"network_used": False, "model_called": False, "sqlite_mutation": False, "mem0_mutation": False, "qdrant_mutation": False, "automatic_apply": False}
    if value.get("assurance") != assurance or value.get("execution") != execution:
        raise SemanticAssuranceCuratorAuditError("curator audit receipt safety boundary is invalid")
    core = {"schema_version": RECEIPT_SCHEMA_VERSION, "ok": True, "audit": dict(audit), "assurance": assurance, "execution": execution}
    if value.get("audit_digest") != _digest(core):
        raise SemanticAssuranceCuratorAuditError("curator audit receipt digest mismatch")
    return {**core, "audit_digest": value["audit_digest"]}


def _atomic_facts(raw: object, root: Path) -> dict[str, dict[str, Any]]:
    expected = {"schema_version", "curator_identity_attested", "atomic_facts"}
    if not isinstance(raw, Mapping) or set(raw) != expected or raw.get("schema_version") != ATOMIC_LEDGER_SCHEMA_VERSION:
        raise SemanticAssuranceCuratorAuditError("atomic fact ledger schema is invalid")
    if raw.get("curator_identity_attested") is not True or not _sequence(raw.get("atomic_facts")):
        raise SemanticAssuranceCuratorAuditError("atomic fact ledger is incomplete")
    facts: dict[str, dict[str, Any]] = {}
    fragments: set[str] = set()
    private_by_partition: dict[str, dict[str, set[str]]] = {partition: {"proposition": set(), "mechanism": set()} for partition in _PARTITIONS}
    for item in raw["atomic_facts"]:
        expected_fields = {
            "atomic_fact_id", "partition", "canonical_path", "canonical_heading", "line_start", "line_end",
            "fragment_sha256", "proposition_private", "inference_mechanism_private", "relation_form_private", "source_verified_by_human",
        }
        if not isinstance(item, Mapping) or set(item) != expected_fields:
            raise SemanticAssuranceCuratorAuditError("atomic fact schema is invalid")
        atomic_fact_id = _opaque_id(item.get("atomic_fact_id"), "atomic fact id")
        if atomic_fact_id in facts:
            raise SemanticAssuranceCuratorAuditError("atomic fact id is duplicated")
        partition = item.get("partition")
        if partition not in _PARTITIONS or item.get("source_verified_by_human") is not True:
            raise SemanticAssuranceCuratorAuditError("atomic fact partition or human source verification is invalid")
        if item.get("relation_form_private") not in _RELATION_FORMS:
            raise SemanticAssuranceCuratorAuditError("atomic fact private relation form is invalid")
        proposition = _private_text(item.get("proposition_private"), "private proposition")
        mechanism = _private_text(item.get("inference_mechanism_private"), "private inference mechanism")
        for name, value in (("proposition", proposition), ("mechanism", mechanism)):
            if value in private_by_partition[partition][name] or any(value in values[name] for other, values in private_by_partition.items() if other != partition):
                raise SemanticAssuranceCuratorAuditError(f"private {name} is reused across curator facts or partitions")
            private_by_partition[partition][name].add(value)
        fragment, source_kind = _source_fragment(item, root)
        if fragment["sha256"] in fragments:
            raise SemanticAssuranceCuratorAuditError("canonical source fragment is reused")
        fragments.add(fragment["sha256"])
        facts[atomic_fact_id] = {
            "atomic_fact_id": atomic_fact_id,
            "partition": partition,
            "fragment_sha256": fragment["sha256"],
            "source_kind": source_kind,
        }
    return facts


def _control_cells(raw: object, facts: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping) or set(raw) != {"schema_version", "control_cells"} or raw.get("schema_version") != CONTROL_CELLS_SCHEMA_VERSION or not _sequence(raw.get("control_cells")):
        raise SemanticAssuranceCuratorAuditError("curation control cells schema is invalid")
    controls: dict[str, dict[str, Any]] = {}
    fields = {"atomic_fact_id", "risk_class", "support_count_bucket", "approximate_length_bucket", "document_genre", "relation_form", "adversarial_form", "dimension_surfaces"}
    for item in raw["control_cells"]:
        if not isinstance(item, Mapping) or set(item) != fields:
            raise SemanticAssuranceCuratorAuditError("curation control cell schema is invalid")
        atomic_fact_id = _opaque_id(item.get("atomic_fact_id"), "control atomic fact id")
        if atomic_fact_id not in facts or atomic_fact_id in controls:
            raise SemanticAssuranceCuratorAuditError("curation control cells do not bind each atomic fact exactly once")
        surfaces = item.get("dimension_surfaces")
        if (
            item.get("risk_class") not in _RISK_CLASSES or item.get("support_count_bucket") not in _SUPPORT_BUCKETS
            or item.get("approximate_length_bucket") not in _LENGTH_BUCKETS or item.get("document_genre") not in _DOCUMENT_GENRES
            or item.get("relation_form") not in _RELATION_FORMS or item.get("adversarial_form") not in _ADVERSARIAL_FORMS
            or not isinstance(surfaces, list) or not surfaces or set(surfaces) - _DIMENSION_SURFACES or len(surfaces) != len(set(surfaces))
        ):
            raise SemanticAssuranceCuratorAuditError("curation control cell has an invalid closed-vocabulary value")
        controls[atomic_fact_id] = {
            "atomic_fact_id": atomic_fact_id,
            "risk_class": item["risk_class"],
            "support_count_bucket": item["support_count_bucket"],
            "approximate_length_bucket": item["approximate_length_bucket"],
            "document_genre": item["document_genre"],
            "relation_form": item["relation_form"],
            "adversarial_form": item["adversarial_form"],
            "dimension_surfaces": sorted(surfaces),
        }
    if set(controls) != set(facts):
        raise SemanticAssuranceCuratorAuditError("curation control cells do not cover every atomic fact")
    return controls


def _coverage(facts: Mapping[str, Mapping[str, Any]], controls: Mapping[str, Mapping[str, Any]]) -> None:
    counts = {partition: sum(fact["partition"] == partition for fact in facts.values()) for partition in _PARTITIONS}
    if any(counts[partition] < _MINIMUMS[partition] for partition in _PARTITIONS):
        raise SemanticAssuranceCuratorAuditError("atomic fact partition coverage is below V6 minimum")
    for partition in _PARTITIONS:
        risks = {control["risk_class"] for fact_id, control in controls.items() if facts[fact_id]["partition"] == partition}
        if risks != _RISK_CLASSES:
            raise SemanticAssuranceCuratorAuditError("curation controls lack one or more risks in a partition")
    sealed_conflicts = sum(
        control["risk_class"] in _CONFLICT_RISKS and facts[fact_id]["partition"] == "sealed_holdout"
        for fact_id, control in controls.items()
    )
    if sealed_conflicts < 200 or any(sum(control["risk_class"] == risk and facts[fact_id]["partition"] == "sealed_holdout" for fact_id, control in controls.items()) < 50 for risk in _CONFLICT_RISKS):
        raise SemanticAssuranceCuratorAuditError("sealed curator conflict coverage is below V6 minimum")


def _source_fragment(item: Mapping[str, object], root: Path) -> tuple[dict[str, str], str]:
    path = _canonical_path(item.get("canonical_path"), root)
    heading = _canonical_heading(item.get("canonical_heading"))
    line_start, line_end = item.get("line_start"), item.get("line_end")
    if not _positive_int(line_start) or not _positive_int(line_end) or line_start > line_end:
        raise SemanticAssuranceCuratorAuditError("canonical fragment line range is invalid")
    if not _is_sha256(item.get("fragment_sha256")):
        raise SemanticAssuranceCuratorAuditError("canonical fragment SHA-256 is invalid")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise SemanticAssuranceCuratorAuditError("canonical source fragment is unreadable") from exc
    if line_end > len(lines):
        raise SemanticAssuranceCuratorAuditError("canonical fragment line range exceeds source")
    _heading_scope(lines, heading, line_start, line_end)
    selected = "\n".join(lines[line_start - 1:line_end])
    digest = hashlib.sha256(selected.encode("utf-8")).hexdigest()
    if digest != item["fragment_sha256"]:
        raise SemanticAssuranceCuratorAuditError("canonical fragment SHA-256 does not match selected lines")
    relative = path.relative_to(root).as_posix()
    return {"sha256": digest}, "local_adr" if relative.startswith(".docs/adr/") else "public_docs"


def _canonical_root(value: Path) -> Path:
    try:
        root = value.resolve(strict=True)
    except OSError as exc:
        raise SemanticAssuranceCuratorAuditError("canonical root is unavailable") from exc
    if not root.is_dir() or root.is_symlink():
        raise SemanticAssuranceCuratorAuditError("canonical root is invalid")
    return root


def _canonical_path(value: object, root: Path) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise SemanticAssuranceCuratorAuditError("canonical path is invalid")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise SemanticAssuranceCuratorAuditError("canonical path escapes its allowed root")
    normalized = relative.as_posix()
    if not (normalized.startswith("docs/") or normalized.startswith(".docs/adr/")) or not normalized.endswith(".md"):
        raise SemanticAssuranceCuratorAuditError("canonical path is outside the source allowlist")
    candidate = root.joinpath(*relative.parts)
    if any(root.joinpath(*relative.parts[:index]).is_symlink() for index in range(1, len(relative.parts) + 1)):
        raise SemanticAssuranceCuratorAuditError("canonical path uses a symlink")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise SemanticAssuranceCuratorAuditError("canonical path is unavailable or escapes its root") from exc
    if not resolved.is_file():
        raise SemanticAssuranceCuratorAuditError("canonical path is not a regular file")
    return resolved


def _heading_scope(lines: list[str], heading: str, start: int, end: int) -> None:
    matches: list[tuple[int, int]] = []
    for number, line in enumerate(lines, start=1):
        match = _HEADING.match(line)
        if match and match.group(2) == heading:
            matches.append((number, len(match.group(1))))
    for heading_line, level in matches:
        boundary = len(lines) + 1
        for number in range(heading_line + 1, len(lines) + 1):
            nested = _HEADING.match(lines[number - 1])
            if nested and len(nested.group(1)) <= level:
                boundary = number
                break
        if start >= heading_line and end < boundary:
            return
    raise SemanticAssuranceCuratorAuditError("canonical heading does not own the selected line range")


def _partition_counts(value: object) -> bool:
    return isinstance(value, Mapping) and set(value) == _PARTITIONS and all(_positive_int(value[partition]) for partition in _PARTITIONS)


def _partition_risk_counts(value: object, counts: Mapping[str, int]) -> bool:
    if not isinstance(value, Mapping) or set(value) != _PARTITIONS:
        return False
    for partition in _PARTITIONS:
        row = value.get(partition)
        if not isinstance(row, Mapping) or set(row) != _RISK_CLASSES or any(not _positive_int(row[risk]) for risk in _RISK_CLASSES) or sum(row.values()) != counts[partition]:
            return False
    return True


def _opaque_id(value: object, field: str) -> str:
    if not _is_sha256(value):
        raise SemanticAssuranceCuratorAuditError(f"{field} must be an opaque SHA-256 id")
    return str(value)


def _private_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4_000:
        raise SemanticAssuranceCuratorAuditError(f"{field} is invalid")
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _canonical_heading(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4_000:
        raise SemanticAssuranceCuratorAuditError("canonical heading is invalid")
    return unicodedata.normalize("NFKC", value).strip()


def _sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) and bool(value)


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


__all__ = [
    "ATOMIC_LEDGER_SCHEMA_VERSION",
    "CONTROL_CELLS_SCHEMA_VERSION",
    "RECEIPT_SCHEMA_VERSION",
    "SemanticAssuranceCuratorAuditError",
    "audit_v6_curator_inputs",
    "verify_v6_curator_audit_receipt",
]
