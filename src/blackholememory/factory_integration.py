"""Evidence crosswalk for QA, incident and documentation factories (WI-10)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .documentation_factory import build_documentation_factory_preview
from .qa_incident_factory import build_qa_incident_preview


FACTORY_INTEGRATION_SCHEMA_VERSION = "bhm.factory-integration.v1"
FACTORY_INTEGRATION_MAX_ITEMS = 64


class FactoryIntegrationError(ValueError):
    pass


def build_factory_integration_preview(
    artifacts: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    *,
    project: str = "blackholememory",
    changed_paths: Sequence[str] = (),
    code_items: Sequence[Mapping[str, Any]] = (),
    task_items: Sequence[Mapping[str, Any]] = (),
    risk_class: str = "medium",
    qa_feature_flags: Mapping[str, Any] | None = None,
    documentation_feature_flags: Mapping[str, Any] | None = None,
    max_items: int = 32,
    now: datetime | None = None,
) -> dict[str, Any]:
    if len(artifacts) > FACTORY_INTEGRATION_MAX_ITEMS or len(documents) > FACTORY_INTEGRATION_MAX_ITEMS:
        raise FactoryIntegrationError("factory inputs exceed bounded limit")
    if not 1 <= int(max_items) <= FACTORY_INTEGRATION_MAX_ITEMS:
        raise FactoryIntegrationError("max_items must be between 1 and 64")
    project_name = _clip(project, 120) or "blackholememory"
    normalized_risk = _clip(risk_class, 24).casefold() or "medium"
    if normalized_risk not in {"low", "medium", "high", "critical"}:
        raise FactoryIntegrationError("risk_class must be low/medium/high/critical")
    changed = sorted({_clip(path, 240).replace("\\", "/") for path in changed_paths if str(path or "").strip()})
    code = _normalize_code_items(code_items, project_name)
    tasks = _normalize_task_items(task_items, project_name)
    crosswalk = _build_crosswalk(changed, code, tasks)
    qa = build_qa_incident_preview(
        artifacts,
        project=project_name,
        changed_paths=changed,
        feature_flags=qa_feature_flags,
        max_items=max_items,
        now=now,
    )
    docs = build_documentation_factory_preview(
        documents,
        project=project_name,
        feature_flags=documentation_feature_flags,
        max_patches=max_items,
        now=now,
    )
    review_queue = _review_queue(qa, docs, crosswalk, normalized_risk)
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    core = {
        "project": project_name,
        "risk_class": normalized_risk,
        "changed_paths": changed,
        "code_impact": code[:max_items],
        "task_context": tasks[:max_items],
        "crosswalk": crosswalk[:max_items],
        "qa": qa,
        "documentation": docs,
        "review_queue": review_queue[:max_items],
        "generated_at": clock.isoformat().replace("+00:00", "Z"),
    }
    digest = _sha256(_canonical_json(core))
    return {
        "schema_version": FACTORY_INTEGRATION_SCHEMA_VERSION,
        "preview_digest": digest,
        **core,
        "execution": {
            "tests_started": False,
            "incident_commands_started": False,
            "documents_written": False,
            "model_started": False,
            "vision_started": False,
            "writes_performed": False,
            "auto_apply": False,
            "authority": "proposal",
        },
        "gates": {
            "all_qa_verdicts_have_evidence": bool(qa.get("gates", {}).get("all_verdicts_have_evidence", False)),
            "documentation_review_required": any(bool(item.get("requires_review")) for item in docs.get("patches", [])),
            "code_impact_is_not_coverage": True,
            "secret_output": False,
            "raw_log_output": False,
        },
    }


def verify_factory_integration_digest(preview: Mapping[str, Any]) -> bool:
    expected = str(preview.get("preview_digest") or "")
    if not expected:
        return False
    core = {key: preview.get(key) for key in ("project", "risk_class", "changed_paths", "code_impact", "task_context", "crosswalk", "qa", "documentation", "review_queue", "generated_at")}
    return expected == _sha256(_canonical_json(core))


def _normalize_code_items(items: Sequence[Mapping[str, Any]], project: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in list(items)[:FACTORY_INTEGRATION_MAX_ITEMS]:
        item = dict(raw)
        path = _clip(item.get("path") or item.get("source_ref"), 240).replace("\\", "/")
        if not path:
            continue
        result.append({"path": path, "symbol": _clip(item.get("symbol") or item.get("name"), 180), "kind": _clip(item.get("kind"), 80), "test_paths": _bounded_strings(item.get("test_paths") or item.get("tests"), 240, 12), "impact": _bounded_strings(item.get("impact") or item.get("related_paths"), 240, 12), "source_ref": _clip(item.get("source_ref") or path, 240), "project": project})
    return sorted(result, key=lambda item: (item["path"], item["symbol"]))


def _normalize_task_items(items: Sequence[Mapping[str, Any]], project: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in list(items)[:FACTORY_INTEGRATION_MAX_ITEMS]:
        item = dict(raw)
        task_id = _clip(item.get("task_id") or item.get("id"), 180)
        if not task_id:
            continue
        files = _bounded_strings(item.get("files_touched") or item.get("scope_in") or item.get("files"), 240, 16)
        result.append({"task_id": task_id, "status": _clip(item.get("status"), 40) or "open", "files": files, "evidence_refs": _bounded_strings(item.get("evidence_refs") or item.get("validation"), 240, 12), "source_ref": _clip(item.get("source_ref") or f"task:{task_id}", 240), "project": project})
    return sorted(result, key=lambda item: item["task_id"])


def _build_crosswalk(changed: Sequence[str], code: Sequence[Mapping[str, Any]], tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in changed:
        code_matches = [item for item in code if item["path"] == path or item["path"].startswith(path.rstrip("/") + "/")]
        task_matches = [item for item in tasks if path in item.get("files", [])]
        tests = sorted({test for item in code_matches for test in item.get("test_paths", [])})
        payload = {"path": path, "code_refs": [item["source_ref"] for item in code_matches], "task_refs": [item["source_ref"] for item in task_matches], "test_refs": tests}
        result.append({"crosswalk_id": f"impact_{_sha256(_canonical_json(payload))[:20]}", **payload, "coverage_claim": "selected-evidence-only", "requires_review": True})
    return result


def _review_queue(qa: Mapping[str, Any], docs: Mapping[str, Any], crosswalk: Sequence[Mapping[str, Any]], risk_class: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in list(qa.get("test_drafts") or [])[:FACTORY_INTEGRATION_MAX_ITEMS]:
        result.append({"review_id": f"qa:{item.get('draft_id')}", "kind": "qa", "target": item.get("target"), "risk_class": risk_class, "evidence_refs": item.get("evidence_refs") or [], "requires_human_review": True, "authority": "proposal"})
    for item in list(docs.get("patches") or [])[:FACTORY_INTEGRATION_MAX_ITEMS]:
        result.append({"review_id": f"docs:{item.get('patch_id')}", "kind": "documentation", "target": item.get("target"), "risk_class": risk_class, "evidence_refs": [item.get("target")], "requires_human_review": True, "authority": "proposal"})
    for item in list(crosswalk)[:FACTORY_INTEGRATION_MAX_ITEMS]:
        result.append({"review_id": f"impact:{item.get('crosswalk_id')}", "kind": "impact", "target": item.get("path"), "risk_class": risk_class, "evidence_refs": item.get("code_refs") or [], "requires_human_review": True, "authority": "proposal"})
    return result


def _bounded_strings(values: Any, limit: int, max_items: int) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    for value in values:
        text = _clip(value, limit)
        if text and text not in result:
            result.append(text)
        if len(result) >= max_items:
            break
    return result


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = ["FACTORY_INTEGRATION_SCHEMA_VERSION", "FactoryIntegrationError", "build_factory_integration_preview", "verify_factory_integration_digest"]
