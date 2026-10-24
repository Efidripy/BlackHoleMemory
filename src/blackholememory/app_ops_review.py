"""Review queue, conflict detection, redaction, and policy enforcement for app_ops."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone

from fastapi import HTTPException

from . import app_core
from . import app_core_hit
from . import app_core_hook
from . import app_runtime as rt
from .app_runtime import _safe_exception_text
from .api_models import (
    MemoryDetectRequest,
    MemoryRedactRequest,
    MemoryReviewQueueRequest,
    PolicyEnforceMemoryRequest,
    ReviewQueueApplyRequest,
)
from .lifecycle_suggestions import build_lifecycle_suggestions
from .observation_security import contains_secret_like, redact_secret_text
from .security_boundaries import SecurityBoundaryError, compile_bounded_regex

_CUSTOM_REDACTION_MAX_PATTERN_LENGTH = 120
_CUSTOM_REDACTION_MAX_INPUT_CHARS = 64 * 1024


def _serialize_conflict_candidate(item: dict) -> dict:
    return {
        "queue_id": item.get("queue_id"),
        "left_id": item.get("left_id"),
        "right_id": item.get("right_id"),
        "project": item.get("project"),
        "score": item.get("score"),
        "reason": item.get("reason"),
        "left_title": item.get("left_title"),
        "right_title": item.get("right_title"),
        "shared_tags": item.get("shared_tags") or [],
        "left_revision_id": item.get("left_revision_id"),
        "right_revision_id": item.get("right_revision_id"),
        "left_content_sha256": item.get("left_content_sha256"),
        "right_content_sha256": item.get("right_content_sha256"),
    }

def _memory_content_sha256(record: dict) -> str:
    metadata = record.get("metadata") or {}
    return str(metadata.get("content_sha256") or hashlib.sha256(str(record.get("content") or "").encode("utf-8")).hexdigest())

def _review_status(record: dict) -> str:
    value = str((record.get("metadata") or {}).get("review_status") or "open").strip().lower()
    return value if value in {"open", "needs_review", "resolved", "dismissed"} else "open"

def _combined_review_status(records: list[dict]) -> str:
    statuses = {_review_status(record) for record in records if record}
    if "needs_review" in statuses:
        return "needs_review"
    if "open" in statuses or not statuses:
        return "open"
    if statuses == {"resolved"}:
        return "resolved"
    if statuses == {"dismissed"}:
        return "dismissed"
    return "open"

def _review_queue_id(kind: str, project: str | None, memory_ids: list[str], fingerprint: str = "") -> str:
    payload = {
        "kind": kind,
        "project": rt._canonical_project(project) if project else None,
        "memory_ids": sorted(str(item) for item in memory_ids),
        "fingerprint": fingerprint,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"review_{digest[:24]}"

def _detect_conflicts(request: MemoryDetectRequest) -> list[dict]:
    records = [
        item for item in rt._load_live_memories()
        if app_core_hit._memory_matches_filters(item, project=request.project, include_archived=request.include_archived)
    ]
    groups: dict[tuple[str, str], list[dict]] = {}
    for item in records:
        groups.setdefault((str(item.get("project") or ""), str(item.get("memory_type") or "")), []).append(item)

    candidates: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for (project, _memory_type), group in sorted(groups.items()):
        ordered = sorted(group, key=lambda item: str(item.get("source_id") or ""))
        title_buckets: dict[str, list[dict]] = {}
        prefix_tag_buckets: dict[tuple[str, str], list[dict]] = {}
        for item in ordered:
            metadata = item.get("metadata") or {}
            title = str(metadata.get("raw_title") or "").strip().lower()
            text = app_core_hit._normalized_text(item.get("content"))
            if title:
                title_buckets.setdefault(title, []).append(item)
            prefix = text[:80]
            if prefix:
                for tag in set(str(value) for value in (item.get("tags") or []) if str(value).strip()):
                    prefix_tag_buckets.setdefault((prefix, tag), []).append(item)

        candidate_pairs: set[tuple[str, str]] = set()
        for bucket in title_buckets.values():
            for left_index, left in enumerate(bucket):
                for right in bucket[left_index + 1:]:
                    candidate_pairs.add(tuple(sorted((str(left.get("source_id")), str(right.get("source_id"))))))
        for bucket in prefix_tag_buckets.values():
            for left_index, left in enumerate(bucket):
                for right in bucket[left_index + 1:]:
                    candidate_pairs.add(tuple(sorted((str(left.get("source_id")), str(right.get("source_id"))))))

        by_id = {str(item.get("source_id")): item for item in ordered}
        for left_id, right_id in sorted(candidate_pairs):
            if (left_id, right_id) in seen_pairs:
                continue
            seen_pairs.add((left_id, right_id))
            left = by_id[left_id]
            right = by_id[right_id]
            left_metadata = left.get("metadata") or {}
            right_metadata = right.get("metadata") or {}
            left_title = str(left_metadata.get("raw_title") or "").strip()
            right_title = str(right_metadata.get("raw_title") or "").strip()
            left_text = app_core_hit._normalized_text(left.get("content"))
            right_text = app_core_hit._normalized_text(right.get("content"))
            shared_tags = sorted(set(str(value) for value in (left.get("tags") or [])) & set(str(value) for value in (right.get("tags") or [])))
            same_title = bool(left_title and right_title and left_title.lower() == right_title.lower())
            same_prefix = bool(left_text[:80] and left_text[:80] == right_text[:80])
            materially_different = bool(left_text and right_text and left_text != right_text)

            score = 0.0
            reason = ""
            if same_title and materially_different:
                score = 0.9
                reason = "same_title_different_content"
            elif shared_tags and materially_different and same_prefix:
                score = 0.82
                reason = "shared_tags_divergent_content"
            if score <= 0:
                continue
            fingerprint = "|".join(
                [
                    reason,
                    _memory_content_sha256(left),
                    _memory_content_sha256(right),
                    ",".join(shared_tags),
                ]
            )
            candidates.append(
                {
                    "queue_id": _review_queue_id("contradiction", project, [left_id, right_id], fingerprint),
                    "left_id": left_id,
                    "right_id": right_id,
                    "project": project,
                    "score": score,
                    "reason": reason,
                    "left_title": left_title or app_core._build_memory_title(left.get("content") or ""),
                    "right_title": right_title or app_core._build_memory_title(right.get("content") or ""),
                    "shared_tags": shared_tags,
                    "left_revision_id": left_metadata.get("revision_id"),
                    "right_revision_id": right_metadata.get("revision_id"),
                    "left_content_sha256": _memory_content_sha256(left),
                    "right_content_sha256": _memory_content_sha256(right),
                }
            )
    candidates.sort(key=lambda item: (item.get("score") or 0.0, item.get("queue_id") or ""), reverse=True)
    return candidates[: max(min(request.limit, 200), 1)]

def _lint_memory_record(record: dict) -> list[dict]:
    issues: list[dict] = []
    content = record.get("content") or ""
    metadata = record.get("metadata") or {}
    if len(content.strip()) < 20:
        issues.append({"severity": "warning", "code": "too_short", "message": "memory content is very short"})
    if len(content) > 4000:
        issues.append({"severity": "warning", "code": "too_long", "message": "memory content is unusually long"})
    if not record.get("project"):
        issues.append({"severity": "error", "code": "missing_project", "message": "memory has no project scope"})
    if not record.get("memory_type"):
        issues.append({"severity": "error", "code": "missing_type", "message": "memory has no type"})
    if not (record.get("tags") or []):
        issues.append({"severity": "warning", "code": "missing_concepts", "message": "memory has no concepts/tags"})
    if not (metadata.get("files") or []):
        issues.append({"severity": "info", "code": "missing_files", "message": "memory has no linked files"})
    if contains_secret_like(content):
        issues.append({"severity": "error", "code": "possible_secret", "message": "memory may contain a secret-like token"})
    if content.count("\n") > 80:
        issues.append({"severity": "warning", "code": "log_like_blob", "message": "memory looks more like a raw log than compact durable knowledge"})
    return issues

def _memory_review_queue(request: MemoryReviewQueueRequest) -> dict:
    project = rt._canonical_project(request.project) if request.project else None
    records = [
        item for item in rt._load_live_memories()
        if app_core_hit._memory_matches_filters(item, project=project, include_archived=False)
    ]
    by_id = {str(item.get("source_id")): item for item in records}
    queue: list[dict] = []
    for item in records:
        issues = _lint_memory_record(item)
        confidence = (item.get("metadata") or {}).get("confidence")
        reasons = [issue["code"] for issue in issues if issue["severity"] in {"error", "warning"}]
        if confidence is not None and confidence < 0.5:
            reasons.append("low_confidence")
        if not ((item.get("metadata") or {}).get("source_refs") or []):
            reasons.append("missing_source_refs")
        if not reasons:
            continue
        status = _review_status(item)
        if status in {"resolved", "dismissed"} and not request.include_closed:
            continue
        queue.append(
            {
                "queue_id": _review_queue_id("quality", item.get("project"), [item["source_id"]], "|".join(sorted(set(reasons))) + "|" + _memory_content_sha256(item)),
                "kind": "quality",
                "status": status,
                "score": min(1.0, 0.35 + 0.15 * len(set(reasons))),
                "reasons": sorted(set(reasons)),
                "memory_ids": [item["source_id"]],
                "memory": app_core._serialize_memory_record(item),
            }
        )

    if request.include_conflicts:
        from . import app_ops as _owner
        detect_conflicts_fn = getattr(_owner, "_detect_conflicts", _detect_conflicts)
        conflicts = detect_conflicts_fn(
            MemoryDetectRequest(project=project, limit=200, include_archived=False)
        )
        for conflict in conflicts:
            left = by_id.get(conflict["left_id"])
            right = by_id.get(conflict["right_id"])
            status = _combined_review_status([left, right])
            if status in {"resolved", "dismissed"} and not request.include_closed:
                continue
            queue.append(
                {
                    "queue_id": conflict["queue_id"],
                    "kind": "contradiction",
                    "status": status,
                    "score": conflict["score"],
                    "reasons": [conflict["reason"]],
                    "memory_ids": [conflict["left_id"], conflict["right_id"]],
                    "payload": _serialize_conflict_candidate(conflict),
                }
            )
    queue.sort(key=lambda item: (item.get("score") or 0.0, item.get("queue_id") or ""), reverse=True)
    bounded_queue = queue[: max(min(request.limit, 200), 1)]
    return {
        "project": project,
        "items": bounded_queue,
        "lifecycle_suggestions": build_lifecycle_suggestions(bounded_queue),
    }

def _redact_memory_record(record: dict, request: MemoryRedactRequest) -> tuple[dict, int, bool]:
    original = record.get("content") or ""
    redaction_kinds: list[str] = []
    if request.patterns:
        if len(original) > _CUSTOM_REDACTION_MAX_INPUT_CHARS:
            raise HTTPException(status_code=413, detail="custom redaction input exceeds the bounded limit")
        redacted = original
        replacements = 0
        for pattern in request.patterns:
            try:
                compiled = compile_bounded_regex(
                    pattern,
                    field="redaction pattern",
                    max_length=_CUSTOM_REDACTION_MAX_PATTERN_LENGTH,
                )
            except SecurityBoundaryError as exc:
                raise HTTPException(status_code=422, detail=_safe_exception_text(exc)) from exc
            redacted, count = compiled.subn(
                lambda match: ((match.group(1) if match.lastindex else "") + request.replacement),
                redacted,
            )
            replacements += count
        if replacements:
            redaction_kinds.append("custom")
    else:
        redaction = redact_secret_text(original)
        redacted = re.sub(r"\[REDACTED:[^\]]+\]", request.replacement, redaction.value)
        replacements = redaction.replacements
        redaction_kinds.extend(sorted(set(redaction.kinds)))
    metadata = record.setdefault("metadata", {})
    removed_legacy_plaintext = metadata.pop("content_before_redaction", None) is not None
    changed = bool(replacements or removed_legacy_plaintext)
    if changed:
        metadata["redacted_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        metadata["redaction_count"] = replacements
        metadata["redaction_kinds"] = redaction_kinds
        metadata["content_before_redaction_sha256"] = hashlib.sha256(original.encode("utf-8")).hexdigest()
        metadata["content_before_redaction_chars"] = len(original)
        record["content"] = redacted
        record["updated_at"] = metadata["redacted_at"]
        app_core._append_memory_changelog(
            record,
            "redact",
            {
                "replacements": replacements,
                "kinds": redaction_kinds,
                "removed_legacy_plaintext": removed_legacy_plaintext,
            },
        )
    return record, replacements, changed

def _memory_redact(request: MemoryRedactRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    record, replacements, changed = _redact_memory_record(copy.deepcopy(record), request)
    if changed:
        app_core._replace_live_memory(record)
    return {"memory": app_core._serialize_memory_record(record), "replacements": replacements}

def _review_queue_apply(request: ReviewQueueApplyRequest) -> dict:
    queue = _memory_review_queue(
        MemoryReviewQueueRequest(
            project=request.project,
            limit=max(request.limit, len(request.queue_ids), 1),
            include_conflicts=True,
            include_closed=bool(request.queue_ids),
        )
    ).get("items", [])
    if request.queue_ids:
        requested_ids = {str(value).strip() for value in request.queue_ids if str(value).strip()}
        queue = [item for item in queue if item.get("queue_id") in requested_ids]
    applied: list[dict] = []
    effective_status = request.status if request.mark_needs_review else None
    missing_queue_ids = sorted(
        {str(value).strip() for value in request.queue_ids if str(value).strip()}
        - {str(item.get("queue_id")) for item in queue}
    )
    selected_memory_ids = list(
        dict.fromkeys(
            str(value)
            for item in queue
            for value in item.get("memory_ids") or []
            if str(value)
        )
    )
    canonical_project = rt._canonical_project(request.project) if request.project else None
    authoritative = rt._memory_store_is_authoritative()
    if authoritative:
        baseline_records = rt._memory_service().get_records(
            selected_memory_ids,
            project=canonical_project,
        )
    else:
        baseline_records = [
            item
            for item in rt._load_live_memories()
            if item.get("source_id") in selected_memory_ids
            and app_core_hook._project_matches(item.get("project"), canonical_project)
        ]
    records_by_id = {
        str(record.get("source_id")): copy.deepcopy(record)
        for record in baseline_records
        if record.get("source_id")
    }
    changed_ids: set[str] = set()
    for item in queue:
        memory_ids = [str(value) for value in item.get("memory_ids") or []]
        records = [records_by_id[memory_id] for memory_id in memory_ids if memory_id in records_by_id]
        if not records:
            continue
        before_statuses = [_review_status(record) for record in records]
        redacted_ids: list[str] = []
        if request.auto_redact_secrets:
            for record in records:
                _record, replacements, changed = _redact_memory_record(
                    record,
                    MemoryRedactRequest(id=record["source_id"], project=record.get("project")),
                )
                if changed:
                    changed_ids.add(str(record["source_id"]))
                if replacements:
                    redacted_ids.append(record["source_id"])
        updated_records: list[dict] = []
        for memory_id in memory_ids:
            record = records_by_id.get(memory_id)
            if record is None:
                continue
            metadata = record.setdefault("metadata", {})
            status_changed = bool(effective_status and _review_status(record) != effective_status)
            was_redacted = memory_id in redacted_ids
            if not status_changed and not was_redacted:
                updated_records.append(record)
                continue
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            if effective_status:
                metadata["review_status"] = effective_status
                metadata["review_updated_at"] = now
                metadata["review_reason"] = ",".join(item.get("reasons") or [])
                metadata["review_queue_id"] = item.get("queue_id")
            app_core._append_memory_changelog(
                record,
                "review_queue_apply",
                {
                    "queue_id": item.get("queue_id"),
                    "kind": item.get("kind"),
                    "status": effective_status,
                    "reasons": item.get("reasons") or [],
                    "redacted": record["source_id"] in redacted_ids,
                },
            )
            record["updated_at"] = now
            changed_ids.add(memory_id)
            updated_records.append(record)
        if effective_status and all(status == effective_status for status in before_statuses) and not redacted_ids:
            action = "already_" + effective_status
        elif not effective_status and not redacted_ids:
            action = "already_noop"
        else:
            action = "updated"
        applied.append(
            {
                "queue_id": item.get("queue_id"),
                "kind": item.get("kind"),
                "action": action,
                "status": effective_status or _combined_review_status(updated_records),
                "memory_ids": [record["source_id"] for record in updated_records],
                "memories": [app_core._serialize_memory_record(record) for record in updated_records],
            }
        )
    if changed_ids:
        changed_records = [records_by_id[memory_id] for memory_id in selected_memory_ids if memory_id in changed_ids]
        if authoritative:
            rt._memory_service().upsert_records(changed_records)
        else:
            replacements = {record["source_id"]: record for record in changed_records}
            merged_records = [
                replacements.get(str(record.get("source_id")), record)
                for record in rt._load_live_memories()
            ]
            app_core._save_live_memories(merged_records)
    return {
        "project": request.project,
        "status": effective_status,
        "count": len(applied),
        "items": applied,
        "missing_queue_ids": missing_queue_ids,
    }

def _policy_enforce_memory(request: PolicyEnforceMemoryRequest) -> dict:
    record = rt._find_live_memory(request.id, request.project)
    if record is None:
        raise HTTPException(status_code=404, detail="memory not found")
    profile = app_core_hook._load_policy_profile()
    content = record.get("content") or ""
    issues = []
    if profile.get("require_project") and not record.get("project"):
        issues.append("missing_project")
    if profile.get("require_memory_type") and not record.get("memory_type"):
        issues.append("missing_memory_type")
    if len(content) > int(profile.get("max_content_chars") or 8000):
        issues.append("content_too_large")
    if content.count("\n") > int(profile.get("max_lines") or 120):
        issues.append("too_many_lines")
    secret_like = contains_secret_like(content)
    if profile.get("block_secret_like") and secret_like:
        issues.append("secret_like")
        if request.auto_redact:
            from . import app_ops as _owner
            redact_fn = getattr(_owner, "_memory_redact", _memory_redact)
            redact_fn(MemoryRedactRequest(id=request.id, project=request.project))
            record = rt._find_live_memory(request.id, request.project) or record
    raw_log_like = content.count("\n") > int(profile.get("max_lines") or 120)
    if profile.get("block_raw_logs") and raw_log_like:
        issues.append("raw_log_like")
    return {"memory": app_core._serialize_memory_record(record), "profile": profile, "issues": issues, "ok": not issues}
