"""Bounded documentation/ops/vision patch proposals for BHM."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .llm_safety import sanitize_llm_value


DOCUMENTATION_FACTORY_SCHEMA_VERSION = "bhm.llm.documentation-factory.v1"
DOCUMENTATION_FACTORY_MAX_DOCUMENTS = 64
DOCUMENTATION_FACTORY_MAX_ASSETS = 32
DOCUMENTATION_FACTORY_MAX_PATCHES = 96
DOCUMENTATION_FACTORY_FEATURES = (
    "readme",
    "adr",
    "changelog",
    "release",
    "runbook",
    "migration",
    "localization",
    "vision",
)


class DocumentationFactoryError(ValueError):
    """Raised when documentation factory input exceeds safe bounds."""


def build_documentation_factory_preview(
    documents: Sequence[Mapping[str, Any]],
    *,
    project: str = "blackholememory",
    locale: str = "ru-RU",
    vision_confirmed: bool = False,
    vision_assets: Sequence[Mapping[str, Any]] = (),
    feature_flags: Mapping[str, Any] | None = None,
    max_patches: int = 32,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create consistency findings and patch proposals without writing documents."""

    if len(documents) > DOCUMENTATION_FACTORY_MAX_DOCUMENTS:
        raise DocumentationFactoryError(f"documents exceed limit {DOCUMENTATION_FACTORY_MAX_DOCUMENTS}")
    if len(vision_assets) > DOCUMENTATION_FACTORY_MAX_ASSETS:
        raise DocumentationFactoryError(f"vision_assets exceed limit {DOCUMENTATION_FACTORY_MAX_ASSETS}")
    if not 1 <= int(max_patches) <= DOCUMENTATION_FACTORY_MAX_PATCHES:
        raise DocumentationFactoryError(f"max_patches must be between 1 and {DOCUMENTATION_FACTORY_MAX_PATCHES}")
    safe_project = _safe_text(project, "blackholememory", 120) or "blackholememory"
    safe_locale = _safe_text(locale, safe_project, 32) or "ru-RU"
    flags = _normalize_flags(feature_flags)
    normalized = _normalize_documents(documents, safe_project)
    findings = _consistency_findings(normalized, safe_locale, flags)
    patches = _patch_proposals(findings, normalized, safe_project, flags, int(max_patches))
    vision = _vision_preview(vision_assets, safe_project, bool(vision_confirmed), flags)
    localization = _localization_preview(normalized, safe_locale, flags)
    gates = _gates(normalized, findings, vision, localization)
    summary = {
        "document_count": len(normalized),
        "finding_count": len(findings),
        "patch_count": len(patches),
        "vision_asset_count": len(vision_assets),
        "localization_count": len(localization),
        "broken_link_count": sum(item["code"] == "broken_link" for item in findings),
    }
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    core = {
        "project": safe_project,
        "locale": safe_locale,
        "feature_flags": flags,
        "summary": summary,
        "documents": normalized,
        "findings": findings,
        "patches": patches,
        "vision": vision,
        "localization": localization,
        "gates": gates,
        "generated_at": clock.isoformat().replace("+00:00", "Z"),
    }
    digest = _sha256(_canonical_json(core))
    return {
        "schema_version": DOCUMENTATION_FACTORY_SCHEMA_VERSION,
        "preview_digest": digest,
        **core,
        "execution": {
            "documents_written": False,
            "vision_started": False,
            "ocr_started": False,
            "git_started": False,
            "auto_apply": False,
            "authority": "proposal",
        },
    }


def verify_documentation_factory_digest(preview: Mapping[str, Any]) -> bool:
    """Verify a documentation factory preview digest."""

    expected = str(preview.get("preview_digest") or "")
    if not expected:
        return False
    core = {
        key: preview.get(key)
        for key in (
            "project",
            "locale",
            "feature_flags",
            "summary",
            "documents",
            "findings",
            "patches",
            "vision",
            "localization",
            "gates",
            "generated_at",
        )
    }
    return expected == _sha256(_canonical_json(core))


def _normalize_flags(raw: Mapping[str, Any] | None) -> dict[str, bool]:
    values = dict(raw or {})
    unknown = sorted(set(values) - set(DOCUMENTATION_FACTORY_FEATURES))
    if unknown:
        raise DocumentationFactoryError(f"unsupported feature flags: {', '.join(unknown)}")
    return {name: bool(values.get(name, True)) for name in DOCUMENTATION_FACTORY_FEATURES}


def _normalize_documents(documents: Sequence[Mapping[str, Any]], project: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(documents):
        item = dict(raw)
        path = _safe_text(item.get("path") or item.get("source_ref") or f"document/{index}.md", project, 240).replace("\\", "/")
        content = _safe_text(item.get("content"), project, 20_000)
        kind = _kind_for_path(path)
        normalized.append(
            {
                "path": path,
                "source_ref": path,
                "kind": kind,
                "sha256": _sha256(content),
                "lines": len(content.splitlines()),
                "bytes": len(content.encode("utf-8")),
                "headings": _headings(content),
                "link_targets": _link_targets(content),
                "status_tokens": sorted(set(re.findall(r"\bP\d+(?:\.\d+)?\b", content))),
                "has_cyrillic": bool(re.search(r"[А-Яа-яЁё]", content)),
                "has_latin": bool(re.search(r"[A-Za-z]", content)),
                "content_digest": _sha256(content),
            }
        )
    return sorted(normalized, key=lambda item: item["path"])


def _consistency_findings(documents: Sequence[Mapping[str, Any]], locale: str, flags: Mapping[str, bool]) -> list[dict[str, Any]]:
    paths = {str(item["path"]) for item in documents}
    findings: list[dict[str, Any]] = []
    for item in documents:
        path = str(item["path"])
        required = _required_sections(str(item["kind"]), flags)
        headings = {str(value).casefold() for value in item.get("headings", [])}
        for section in required:
            if section.casefold() not in headings and not any(section.casefold() in heading for heading in headings):
                findings.append(_finding("missing_section", "medium", path, f"required:{section}"))
        for target in item.get("link_targets", []):
            if target.startswith("http://") or target.startswith("https://") or target.startswith("#"):
                continue
            target_path = target.split("#", 1)[0].replace("\\", "/")
            if target_path and target_path not in paths:
                findings.append(_finding("broken_link", "high", path, f"target:{target_path}"))
        if flags["localization"] and locale.startswith("ru") and item["kind"] in {"readme", "runbook", "ops"} and not item["has_cyrillic"]:
            findings.append(_finding("localization_gap", "low", path, f"locale:{locale}"))
        if flags["readme"] and item["kind"] == "readme" and not item["status_tokens"]:
            findings.append(_finding("status_missing", "medium", path, "expected_phase_status"))
    return findings[:DOCUMENTATION_FACTORY_MAX_PATCHES]


def _patch_proposals(
    findings: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    project: str,
    flags: Mapping[str, bool],
    limit: int,
) -> list[dict[str, Any]]:
    by_path = {str(item["path"]): item for item in documents}
    patches: list[dict[str, Any]] = []
    for finding in findings:
        path = str(finding["source_ref"])
        kind = str(by_path.get(path, {}).get("kind") or "docs")
        if not flags.get(kind, True) and kind in DOCUMENTATION_FACTORY_FEATURES:
            continue
        issue_id = str(finding["finding_id"])
        patches.append(
            {
                "patch_id": f"doc_patch_{_sha256(f'{project}:{issue_id}')[:20]}",
                "target": path,
                "patch_format": "unified-diff",
                "operation": "add_or_correct_documentation",
                "finding_id": issue_id,
                "summary": str(finding["evidence"]),
                "diff_digest": _sha256(f"{path}:{issue_id}:proposal"),
                "requires_review": True,
                "auto_apply": False,
                "authority": "proposal",
            }
        )
        if len(patches) >= limit:
            break
    return patches


def _vision_preview(
    assets: Sequence[Mapping[str, Any]],
    project: str,
    confirmed: bool,
    flags: Mapping[str, bool],
) -> dict[str, Any]:
    if not flags["vision"]:
        return {"status": "disabled_by_feature_flag", "confirmed": False, "critiques": []}
    if not confirmed:
        return {"status": "disabled_unconfirmed_capability", "confirmed": False, "critiques": []}
    critiques: list[dict[str, Any]] = []
    for index, raw in enumerate(list(assets)[:DOCUMENTATION_FACTORY_MAX_ASSETS]):
        path = _safe_text(raw.get("path") or raw.get("id") or f"asset-{index}", project, 240)
        critiques.append(
            {
                "asset_ref": path,
                "critique_id": f"vision_{_sha256(f'{project}:{path}')[:20]}",
                "checks": ["contrast", "legibility", "focus", "responsive_state"],
                "status": "proposal_only",
                "ocr_performed": False,
                "requires_review": True,
            }
        )
    return {"status": "confirmed_preview", "confirmed": True, "asset_count": len(critiques), "critiques": critiques}


def _localization_preview(documents: Sequence[Mapping[str, Any]], locale: str, flags: Mapping[str, bool]) -> list[dict[str, Any]]:
    if not flags["localization"]:
        return []
    return [
        {
            "document": item["path"],
            "locale": locale,
            "suggestion": "review terminology and headings for the target locale",
            "source_ref": item["source_ref"],
            "requires_review": True,
        }
        for item in documents
        if item["has_latin"]
    ][:32]


def _gates(documents: Sequence[Mapping[str, Any]], findings: Sequence[Mapping[str, Any]], vision: Mapping[str, Any], localization: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "link_gate": not any(item["code"] == "broken_link" for item in findings),
        "section_gate": not any(item["code"] == "missing_section" for item in findings),
        "secret_gate": True,
        "vision_gate": vision.get("status") in {"disabled_unconfirmed_capability", "disabled_by_feature_flag", "confirmed_preview"},
        "patch_review_required": bool(findings or localization or vision.get("critiques")),
        "documents_bounded": len(documents) <= DOCUMENTATION_FACTORY_MAX_DOCUMENTS,
    }


def _finding(code: str, severity: str, path: str, evidence: str) -> dict[str, Any]:
    source_ref = path
    return {
        "finding_id": f"doc_{_sha256(f'{code}:{source_ref}:{evidence}')[:20]}",
        "code": code,
        "severity": severity,
        "source_ref": source_ref,
        "evidence": _clip(evidence, 180),
        "requires_review": True,
    }


def _required_sections(kind: str, flags: Mapping[str, bool]) -> tuple[str, ...]:
    required = {
        "readme": ("Status", "Architecture"),
        "adr": ("Status", "Decision", "Rollback"),
        "changelog": ("Release",),
        "release": ("Checks", "Rollback"),
        "runbook": ("Health", "Rollback"),
        "migration": ("Backup", "Rollback"),
        "ops": ("Evidence",),
    }
    return required.get(kind, ())


def _kind_for_path(path: str) -> str:
    lower = path.casefold()
    name = path.rsplit("/", 1)[-1].casefold()
    if name == "readme.md":
        return "readme"
    if "/adr/" in f"/{lower}/" or name.startswith("adr"):
        return "adr"
    if "changelog" in name:
        return "changelog"
    if "release" in name:
        return "release"
    if "runbook" in name:
        return "runbook"
    if "migration" in name:
        return "migration"
    if "/ops/" in f"/{lower}/" or name.startswith("bhm-"):
        return "ops"
    return "docs"


def _headings(content: str) -> list[str]:
    return [line.lstrip("#").strip()[:160] for line in content.splitlines() if line.lstrip().startswith("#")][:32]


def _link_targets(content: str) -> list[str]:
    return [match.strip() for match in re.findall(r"\[[^\]]+\]\(([^)]+)\)", content)][:64]


def _safe_text(value: Any, project: str, limit: int) -> str:
    try:
        transformed = sanitize_llm_value(str(value or ""), source="documentation-factory", project=project, max_input_bytes=32_768, max_sanitized_bytes=32_768)
        return str(transformed.value or "").strip()[:limit]
    except ValueError:
        return ""


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "DOCUMENTATION_FACTORY_FEATURES",
    "DOCUMENTATION_FACTORY_MAX_DOCUMENTS",
    "DOCUMENTATION_FACTORY_SCHEMA_VERSION",
    "DocumentationFactoryError",
    "build_documentation_factory_preview",
    "verify_documentation_factory_digest",
]
