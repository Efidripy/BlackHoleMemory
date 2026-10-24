"""Fail-closed aggregation of bounded semantic-assurance evidence.

This module is deliberately a *reader* of local, content-free receipts.  It
does not contact a model, start a runtime, open BHM storage, or make a
lifecycle decision.  A mechanically complete result proves only that the
declared finite evidence package is structurally bound.  It is never an
authorisation to enable automatic semantic apply.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .semantic_assurance_label_admission import (
    verify_semantic_assurance_label_admission as verify_semantic_assurance_label_admission,  # noqa: F401
)
from .semantic_assurance_readiness_attestation import (
    assess_semantic_assurance_readiness_v3 as assess_semantic_assurance_readiness_v3,  # noqa: F401
    assess_semantic_assurance_readiness_v4 as assess_semantic_assurance_readiness_v4,  # noqa: F401
    assess_semantic_assurance_readiness_v5 as assess_semantic_assurance_readiness_v5,  # noqa: F401
)
from .semantic_assurance_readiness_base import (
    LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V2 as LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V2,  # noqa: F401
    RECEIPT_SCHEMA_VERSION as RECEIPT_SCHEMA_VERSION,  # noqa: F401
    RECEIPT_SCHEMA_VERSION_V3 as RECEIPT_SCHEMA_VERSION_V3,  # noqa: F401
    RECEIPT_SCHEMA_VERSION_V4 as RECEIPT_SCHEMA_VERSION_V4,  # noqa: F401
    RECEIPT_SCHEMA_VERSION_V5 as RECEIPT_SCHEMA_VERSION_V5,  # noqa: F401
    RECEIPT_SCHEMA_VERSION_V6 as RECEIPT_SCHEMA_VERSION_V6,  # noqa: F401
    RECEIPT_SCHEMA_VERSION_V7 as RECEIPT_SCHEMA_VERSION_V7,  # noqa: F401
    SCHEMA_VERSION as SCHEMA_VERSION,  # noqa: F401
    SCHEMA_VERSION_V3 as SCHEMA_VERSION_V3,  # noqa: F401
    SCHEMA_VERSION_V4 as SCHEMA_VERSION_V4,  # noqa: F401
    SCHEMA_VERSION_V5 as SCHEMA_VERSION_V5,  # noqa: F401
    SCHEMA_VERSION_V6 as SCHEMA_VERSION_V6,  # noqa: F401
    SCHEMA_VERSION_V7 as SCHEMA_VERSION_V7,  # noqa: F401
    _admission as _admission,  # noqa: F401
    assess_semantic_assurance_readiness as assess_semantic_assurance_readiness,  # noqa: F401
    write_semantic_assurance_readiness_receipt as write_semantic_assurance_readiness_receipt,  # noqa: F401
)
from .semantic_assurance_readiness_evidence import (
    LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V3 as LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V3,  # noqa: F401
    LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V4 as LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V4,  # noqa: F401
    LIVE_RECEIPT_SCHEMA_VERSION as LIVE_RECEIPT_SCHEMA_VERSION,  # noqa: F401
    _V6_ADMISSION_PROFILE as _V6_ADMISSION_PROFILE,  # noqa: F401
    _V6_CURATOR_AUDITED_ADMISSION_PROFILE as _V6_CURATOR_AUDITED_ADMISSION_PROFILE,  # noqa: F401
    _event_chain_for_v3 as _event_chain_for_v3,  # noqa: F401
    _live_execution as _live_execution,  # noqa: F401
    _live_inventory as _live_inventory,  # noqa: F401
    _live_receipt as _live_receipt,  # noqa: F401
    _source_identity as _source_identity,  # noqa: F401
    _v6_curator_audited_label_admission_binding as _v6_curator_audited_label_admission_binding,  # noqa: F401
    _v6_curator_audited_live_label_admission as _v6_curator_audited_live_label_admission,  # noqa: F401
    _v6_label_admission_binding as _v6_label_admission_binding,  # noqa: F401
    _v6_live_label_admission as _v6_live_label_admission,  # noqa: F401
    _v6_private_public_live as _v6_private_public_live,  # noqa: F401
)
from .semantic_assurance_readiness_metrics import (
    LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V5 as LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V5,  # noqa: F401
    LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V6 as LABEL_ADMISSION_RECEIPT_SCHEMA_VERSION_V6,  # noqa: F401
    SemanticAssuranceReadinessError as SemanticAssuranceReadinessError,  # noqa: F401
    _DIGEST_LENGTH as _DIGEST_LENGTH,  # noqa: F401
    _MAX_INPUT_BYTES as _MAX_INPUT_BYTES,  # noqa: F401
    _MINIMUM_SEALED_CASE_COUNT as _MINIMUM_SEALED_CASE_COUNT,  # noqa: F401
    _MINIMUM_SEALED_SUPPORT_ROW_COUNT as _MINIMUM_SEALED_SUPPORT_ROW_COUNT,  # noqa: F401
    _MINIMUM_STRATUM_SUPPORT as _MINIMUM_STRATUM_SUPPORT,  # noqa: F401
    _MINIMUM_WILSON_LOWER_95 as _MINIMUM_WILSON_LOWER_95,  # noqa: F401
    _REQUIRED_RISK_CLASSES as _REQUIRED_RISK_CLASSES,  # noqa: F401
    _REQUIRED_SEALED_REPEATS as _REQUIRED_SEALED_REPEATS,  # noqa: F401
    _REQUIRED_STRATIFIED_LABELS as _REQUIRED_STRATIFIED_LABELS,  # noqa: F401
    _SEALED_CRITICAL_RISK_CLASSES as _SEALED_CRITICAL_RISK_CLASSES,  # noqa: F401
    _V6_SEALED_CONFLICT_CLASSES as _V6_SEALED_CONFLICT_CLASSES,  # noqa: F401
    _architecture_invariants as _architecture_invariants,  # noqa: F401
    _digest as _digest,  # noqa: F401
    _digest_text as _digest_text,  # noqa: F401
    _metric_gate as _metric_gate,  # noqa: F401
    _metrics as _metrics,  # noqa: F401
    _nonnegative_int as _nonnegative_int,  # noqa: F401
    _offline_report as _offline_report,  # noqa: F401
    _owner_charter as _owner_charter,  # noqa: F401
    _policy as _policy,  # noqa: F401
    _positive_int as _positive_int,  # noqa: F401
    _rate as _rate,  # noqa: F401
    _risk_coverage as _risk_coverage,  # noqa: F401
    _risk_list as _risk_list,  # noqa: F401
    _risk_stratified_metrics as _risk_stratified_metrics,  # noqa: F401
    _shadow_human_oracle as _shadow_human_oracle,  # noqa: F401
    _sqlite_rehearsal as _sqlite_rehearsal,  # noqa: F401
    _stratified_metrics as _stratified_metrics,  # noqa: F401
    _stratified_policy_map as _stratified_policy_map,  # noqa: F401
    _text as _text,  # noqa: F401
    _timestamp as _timestamp,  # noqa: F401
)
from .semantic_assurance_readiness_privacy import (
    _assess_privacy_split_readiness as _assess_privacy_split_readiness,  # noqa: F401
    assess_semantic_assurance_readiness_v6 as assess_semantic_assurance_readiness_v6,  # noqa: F401
    assess_semantic_assurance_readiness_v7 as assess_semantic_assurance_readiness_v7,  # noqa: F401
)


def load_semantic_assurance_readiness(path: Path | str) -> dict[str, Any]:
    """Read one bounded local JSON package and emit its public-safe receipt."""

    source = Path(path)
    try:
        if source.stat().st_size > _MAX_INPUT_BYTES:
            raise SemanticAssuranceReadinessError("readiness evidence package exceeds the bounded size")
        raw = json.loads(source.read_text(encoding="utf-8"))
    except SemanticAssuranceReadinessError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticAssuranceReadinessError("readiness evidence package is unreadable") from exc
    if isinstance(raw, Mapping) and raw.get("schema_version") == SCHEMA_VERSION_V3:
        return assess_semantic_assurance_readiness_v3(raw)
    if isinstance(raw, Mapping) and raw.get("schema_version") == SCHEMA_VERSION_V4:
        return assess_semantic_assurance_readiness_v4(raw)
    if isinstance(raw, Mapping) and raw.get("schema_version") == SCHEMA_VERSION_V5:
        return assess_semantic_assurance_readiness_v5(raw)
    if isinstance(raw, Mapping) and raw.get("schema_version") == SCHEMA_VERSION_V6:
        return assess_semantic_assurance_readiness_v6(raw)
    if isinstance(raw, Mapping) and raw.get("schema_version") == SCHEMA_VERSION_V7:
        return assess_semantic_assurance_readiness_v7(raw)
    return assess_semantic_assurance_readiness(raw)


__all__ = [
    "RECEIPT_SCHEMA_VERSION",
    "RECEIPT_SCHEMA_VERSION_V3",
    "RECEIPT_SCHEMA_VERSION_V4",
    "RECEIPT_SCHEMA_VERSION_V5",
    "RECEIPT_SCHEMA_VERSION_V6",
    "RECEIPT_SCHEMA_VERSION_V7",
    "SCHEMA_VERSION",
    "SCHEMA_VERSION_V3",
    "SCHEMA_VERSION_V4",
    "SCHEMA_VERSION_V5",
    "SCHEMA_VERSION_V6",
    "SCHEMA_VERSION_V7",
    "SemanticAssuranceReadinessError",
    "assess_semantic_assurance_readiness",
    "assess_semantic_assurance_readiness_v3",
    "assess_semantic_assurance_readiness_v4",
    "assess_semantic_assurance_readiness_v5",
    "assess_semantic_assurance_readiness_v6",
    "assess_semantic_assurance_readiness_v7",
    "load_semantic_assurance_readiness",
    "write_semantic_assurance_readiness_receipt",
]
