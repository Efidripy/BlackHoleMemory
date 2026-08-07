from __future__ import annotations

import fnmatch
import hashlib
import json
import shutil
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .hook_queue import HookJobQueue
from .filesystem_boundaries import assert_safe_path
from .filesystem_boundaries import replace_bytes_safely
from .observation_store import ObservationStore


RETENTION_POLICY_SCHEMA_VERSION = "1.0"
RETENTION_BACKUP_SCHEMA_VERSION = "1.0"
SECONDS_PER_DAY = 86_400.0


class RetentionPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class MatchSpec:
    hook_type: tuple[str, ...] = ()
    source: tuple[str, ...] = ()
    project: tuple[str, ...] = ()
    sensitivity: tuple[str, ...] = ()
    lifecycle: tuple[str, ...] = ()
    status: tuple[str, ...] = ()
    kind: tuple[str, ...] = ()

    def matches(self, candidate: dict[str, Any]) -> bool:
        fields = (
            (self.hook_type, candidate.get("hookType")),
            (self.source, candidate.get("source")),
            (self.project, candidate.get("project")),
            (self.sensitivity, candidate.get("sensitivity")),
            (self.lifecycle, candidate.get("lifecycle")),
            (self.status, candidate.get("status")),
            (self.kind, candidate.get("kind")),
        )
        for patterns, value in fields:
            if patterns and not _matches_patterns(value, patterns):
                return False
        return True


@dataclass(frozen=True)
class ObservationRetentionRule:
    name: str
    match: MatchSpec
    hot_days: float
    sample_rate: float
    max_days: float
    min_per_bucket: int


@dataclass(frozen=True)
class HookJobRetentionRule:
    name: str
    match: MatchSpec
    retain_days: float


@dataclass(frozen=True)
class RetentionPolicy:
    path: Path
    sha256: str
    schema_version: str
    observation_rules: tuple[ObservationRetentionRule, ...]
    hook_job_rules: tuple[HookJobRetentionRule, ...]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


def parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _patterns(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RetentionPolicyError("retention match patterns must be arrays")
    return tuple(str(item).strip().casefold() for item in value if str(item).strip())


def _match_spec(value: Any) -> MatchSpec:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise RetentionPolicyError("retention match must be an object")
    supported = {"hookType", "source", "project", "sensitivity", "lifecycle", "status", "kind"}
    unknown = sorted(set(value) - supported)
    if unknown:
        raise RetentionPolicyError(f"unsupported retention match fields: {', '.join(unknown)}")
    return MatchSpec(
        hook_type=_patterns(value.get("hookType")),
        source=_patterns(value.get("source")),
        project=_patterns(value.get("project")),
        sensitivity=_patterns(value.get("sensitivity")),
        lifecycle=_patterns(value.get("lifecycle")),
        status=_patterns(value.get("status")),
        kind=_patterns(value.get("kind")),
    )


def _matches_patterns(value: Any, patterns: Sequence[str]) -> bool:
    normalized = str(value or "").casefold()
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in patterns)


def load_retention_policy(path: Path | str) -> RetentionPolicy:
    policy_path = Path(path).resolve()
    raw = policy_path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetentionPolicyError(f"invalid retention policy {policy_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RetentionPolicyError("retention policy root must be an object")
    schema_version = str(payload.get("schemaVersion") or "")
    if schema_version != RETENTION_POLICY_SCHEMA_VERSION:
        raise RetentionPolicyError(
            f"unsupported retention policy schema {schema_version}; expected {RETENTION_POLICY_SCHEMA_VERSION}"
        )

    observation_rules: list[ObservationRetentionRule] = []
    names: set[str] = set()
    for item in payload.get("observationRules") or []:
        if not isinstance(item, dict):
            raise RetentionPolicyError("observation retention rule must be an object")
        name = str(item.get("name") or "").strip()
        if not name or name in names:
            raise RetentionPolicyError(f"observation retention rule name is missing or duplicated: {name!r}")
        names.add(name)
        hot_days = float(item.get("hotDays", 0))
        sample_rate = float(item.get("sampleRate", 0))
        max_days = float(item.get("maxDays", hot_days))
        min_per_bucket = int(item.get("minPerBucket", 0))
        if hot_days < 0 or max_days < hot_days:
            raise RetentionPolicyError(f"invalid TTL window for observation rule {name}")
        if not 0.0 <= sample_rate <= 1.0:
            raise RetentionPolicyError(f"sampleRate outside 0..1 for observation rule {name}")
        if min_per_bucket < 0:
            raise RetentionPolicyError(f"minPerBucket must be non-negative for observation rule {name}")
        observation_rules.append(
            ObservationRetentionRule(
                name=name,
                match=_match_spec(item.get("match")),
                hot_days=hot_days,
                sample_rate=sample_rate,
                max_days=max_days,
                min_per_bucket=min_per_bucket,
            )
        )

    hook_rules: list[HookJobRetentionRule] = []
    for item in payload.get("hookJobRules") or []:
        if not isinstance(item, dict):
            raise RetentionPolicyError("hook job retention rule must be an object")
        name = str(item.get("name") or "").strip()
        if not name or name in names:
            raise RetentionPolicyError(f"hook job retention rule name is missing or duplicated: {name!r}")
        names.add(name)
        retain_days = float(item.get("retainDays", 0))
        if retain_days < 0:
            raise RetentionPolicyError(f"retainDays must be non-negative for hook job rule {name}")
        hook_rules.append(
            HookJobRetentionRule(
                name=name,
                match=_match_spec(item.get("match")),
                retain_days=retain_days,
            )
        )

    if not observation_rules or not hook_rules:
        raise RetentionPolicyError("retention policy must define observationRules and hookJobRules")
    return RetentionPolicy(
        path=policy_path,
        sha256=hashlib.sha256(raw).hexdigest(),
        schema_version=schema_version,
        observation_rules=tuple(observation_rules),
        hook_job_rules=tuple(hook_rules),
    )


def _first_matching_rule(candidate: dict[str, Any], rules: Iterable[Any]) -> Any | None:
    for rule in rules:
        if rule.match.matches(candidate):
            return rule
    return None


def _age_days(candidate: dict[str, Any], keys: Sequence[str], as_of: datetime) -> float | None:
    timestamp = None
    for key in keys:
        timestamp = parse_timestamp(candidate.get(key))
        if timestamp is not None:
            break
    if timestamp is None:
        return None
    return max((as_of - timestamp).total_seconds() / SECONDS_PER_DAY, 0.0)


def _stable_rank(identifier: str, policy_sha256: str, rule_name: str) -> str:
    return hashlib.sha256(f"{policy_sha256}:{rule_name}:{identifier}".encode("utf-8")).hexdigest()


def _stable_sample(identifier: str, policy_sha256: str, rule_name: str, rate: float) -> bool:
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    digest = _stable_rank(identifier, policy_sha256, rule_name)
    value = int(digest[:16], 16) / float(0xFFFFFFFFFFFFFFFF)
    return value < rate


def _bucket_key(candidate: dict[str, Any], rule_name: str) -> str:
    timestamp = str(candidate.get("storedAt") or candidate.get("occurredAt") or "")
    day = timestamp[:10] if len(timestamp) >= 10 else "unknown-day"
    return "|".join(
        (
            rule_name,
            str(candidate.get("project") or ""),
            str(candidate.get("hookType") or ""),
            day,
        )
    )


def build_retention_plan(
    observation_candidates: Sequence[dict[str, Any]],
    hook_job_candidates: Sequence[dict[str, Any]],
    policy: RetentionPolicy,
    *,
    as_of: datetime | None = None,
    selected_rules: set[str] | None = None,
) -> dict[str, Any]:
    effective_as_of = (as_of or _utc_now()).astimezone(timezone.utc)
    observation_evaluations: list[dict[str, Any]] = []
    sample_windows: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for candidate in observation_candidates:
        event_id = str(candidate.get("eventId") or "")
        lifecycle = str(candidate.get("lifecycle") or "active")
        if lifecycle == "purged":
            evaluation = {
                "scope": "observation",
                "id": event_id,
                "rule": "explicit-purge",
                "outcome": "expire",
                "ageDays": _age_days(candidate, ("storedAt", "occurredAt"), effective_as_of),
                "payloadBytes": int(candidate.get("payloadBytes") or 0),
            }
            if selected_rules is None or "explicit-purge" in selected_rules:
                observation_evaluations.append(evaluation)
            continue

        rule = _first_matching_rule(candidate, policy.observation_rules)
        if rule is None:
            observation_evaluations.append(
                {"scope": "observation", "id": event_id, "rule": "unmatched", "outcome": "keep-unmatched"}
            )
            continue
        if selected_rules is not None and rule.name not in selected_rules:
            continue
        age_days = _age_days(candidate, ("storedAt", "occurredAt"), effective_as_of)
        evaluation = {
            "scope": "observation",
            "id": event_id,
            "rule": rule.name,
            "ageDays": age_days,
            "payloadBytes": int(candidate.get("payloadBytes") or 0),
            "lifecycle": lifecycle,
            "bucket": _bucket_key(candidate, rule.name),
            "sampleRate": rule.sample_rate,
            "minPerBucket": rule.min_per_bucket,
        }
        if age_days is None:
            evaluation["outcome"] = "keep-invalid-time"
        elif age_days >= rule.max_days:
            evaluation["outcome"] = "expire"
        elif age_days < rule.hot_days:
            evaluation["outcome"] = "keep-hot"
        else:
            evaluation["outcome"] = "sample-window"
            sample_windows[evaluation["bucket"]].append(evaluation)
        observation_evaluations.append(evaluation)

    forced_sample_ids: set[str] = set()
    for evaluations in sample_windows.values():
        if not evaluations:
            continue
        minimum = max(int(evaluations[0].get("minPerBucket") or 0), 0)
        ranked = sorted(
            evaluations,
            key=lambda item: _stable_rank(str(item["id"]), policy.sha256, str(item["rule"])),
        )
        forced_sample_ids.update(str(item["id"]) for item in ranked[:minimum])

    for evaluation in observation_evaluations:
        if evaluation.get("outcome") != "sample-window":
            continue
        keep = str(evaluation["id"]) in forced_sample_ids or _stable_sample(
            str(evaluation["id"]),
            policy.sha256,
            str(evaluation["rule"]),
            float(evaluation.get("sampleRate") or 0),
        )
        if keep:
            evaluation["outcome"] = "keep-sample" if evaluation.get("lifecycle") == "archived" else "archive-sample"
        else:
            evaluation["outcome"] = "expire"

    hook_evaluations: list[dict[str, Any]] = []
    for candidate in hook_job_candidates:
        job_id = str(candidate.get("jobId") or "")
        rule = _first_matching_rule(candidate, policy.hook_job_rules)
        if rule is None:
            hook_evaluations.append(
                {"scope": "hook-job", "id": job_id, "rule": "unmatched", "outcome": "keep-unmatched"}
            )
            continue
        if selected_rules is not None and rule.name not in selected_rules:
            continue
        age_days = _age_days(candidate, ("completedAt", "updatedAt", "createdAt"), effective_as_of)
        hook_evaluations.append(
            {
                "scope": "hook-job",
                "id": job_id,
                "eventId": str(candidate.get("eventId") or ""),
                "rule": rule.name,
                "outcome": "expire" if age_days is not None and age_days >= rule.retain_days else (
                    "keep-invalid-time" if age_days is None else "keep-hot"
                ),
                "ageDays": age_days,
                "payloadBytes": int(candidate.get("payloadBytes") or 0),
                "resultBytes": int(candidate.get("resultBytes") or 0),
            }
        )

    digest_rows = sorted(
        (
            str(item.get("scope") or ""),
            str(item.get("id") or ""),
            str(item.get("rule") or ""),
            str(item.get("outcome") or ""),
        )
        for item in [*observation_evaluations, *hook_evaluations]
    )
    digest_payload = {
        "asOf": utc_iso(effective_as_of),
        "policySha256": policy.sha256,
        "selectedRules": sorted(selected_rules) if selected_rules is not None else [],
        "outcomes": digest_rows,
    }
    plan_digest = hashlib.sha256(_canonical_json(digest_payload).encode("utf-8")).hexdigest()
    return {
        "schemaVersion": "1.0",
        "asOf": utc_iso(effective_as_of),
        "policyPath": str(policy.path),
        "policySha256": policy.sha256,
        "selectedRules": sorted(selected_rules) if selected_rules is not None else [],
        "planDigest": plan_digest,
        "observations": observation_evaluations,
        "hookJobs": hook_evaluations,
    }


def summarize_retention_plan(plan: dict[str, Any], *, sample_limit: int = 20) -> dict[str, Any]:
    observations = list(plan.get("observations") or [])
    hook_jobs = list(plan.get("hookJobs") or [])

    def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
        outcomes = Counter(str(item.get("outcome") or "unknown") for item in items)
        rules = Counter(str(item.get("rule") or "unknown") for item in items)
        expiring = [item for item in items if item.get("outcome") == "expire"]
        archiving = [item for item in items if item.get("outcome") == "archive-sample"]
        return {
            "evaluated": len(items),
            "outcomes": dict(sorted(outcomes.items())),
            "rules": dict(sorted(rules.items())),
            "expireCount": len(expiring),
            "archiveCount": len(archiving),
            "expirePayloadBytes": sum(int(item.get("payloadBytes") or 0) for item in expiring),
            "expireResultBytes": sum(int(item.get("resultBytes") or 0) for item in expiring),
            "expireSampleIds": [str(item.get("id") or "") for item in expiring[:sample_limit]],
            "archiveSampleIds": [str(item.get("id") or "") for item in archiving[:sample_limit]],
            "idsTruncated": len(expiring) > sample_limit or len(archiving) > sample_limit,
        }

    return {
        "schemaVersion": str(plan.get("schemaVersion") or "1.0"),
        "asOf": str(plan.get("asOf") or ""),
        "policyPath": str(plan.get("policyPath") or ""),
        "policySha256": str(plan.get("policySha256") or ""),
        "selectedRules": list(plan.get("selectedRules") or []),
        "planDigest": str(plan.get("planDigest") or ""),
        "observations": summarize(observations),
        "hookJobs": summarize(hook_jobs),
    }


def apply_retention_plan(
    plan: dict[str, Any],
    observation_store: ObservationStore,
    hook_queue: HookJobQueue,
    *,
    max_expire: int,
) -> dict[str, Any]:
    observation_items = list(plan.get("observations") or [])
    hook_items = list(plan.get("hookJobs") or [])
    expire_count = sum(1 for item in [*observation_items, *hook_items] if item.get("outcome") == "expire")
    if expire_count > max(int(max_expire), 0):
        raise RetentionPolicyError(f"retention plan expires {expire_count} records; max_expire={max_expire}")

    archived = 0
    observation_expired = 0
    observation_payload_bytes = 0
    hook_expired = 0
    hook_payload_bytes = 0
    hook_result_bytes = 0
    checkpoints: list[str] = []

    archive_groups: dict[str, list[str]] = defaultdict(list)
    observation_expire_groups: dict[str, list[str]] = defaultdict(list)
    hook_expire_groups: dict[str, list[str]] = defaultdict(list)
    for item in observation_items:
        if item.get("outcome") == "archive-sample":
            archive_groups[str(item.get("rule") or "retention")].append(str(item.get("id") or ""))
        elif item.get("outcome") == "expire":
            observation_expire_groups[str(item.get("rule") or "retention")].append(str(item.get("id") or ""))
    for item in hook_items:
        if item.get("outcome") == "expire":
            hook_expire_groups[str(item.get("rule") or "retention")].append(str(item.get("id") or ""))

    for rule_name, event_ids in sorted(archive_groups.items()):
        archived += observation_store.archive(
            event_ids,
            archived_at=str(plan.get("asOf") or ""),
            archive_reason=f"retention sample preserved by policy {rule_name}",
            archived_by="bhm-retention",
            scale_tier="retention-sample",
        )
    for rule_name, event_ids in sorted(observation_expire_groups.items()):
        result = observation_store.expire_payloads(
            event_ids,
            reason=f"retention TTL expired by policy {rule_name}",
            policy_name=rule_name,
            purged_at=str(plan.get("asOf") or ""),
        )
        observation_expired += result.expired
        observation_payload_bytes += result.payload_bytes
        checkpoints.append(f"observations:{result.checkpoint}")
    for rule_name, job_ids in sorted(hook_expire_groups.items()):
        result = hook_queue.expire_terminal(
            job_ids,
            reason=f"retention TTL expired by policy {rule_name}",
            policy_name=rule_name,
            purged_at=str(plan.get("asOf") or ""),
        )
        hook_expired += result.expired
        hook_payload_bytes += result.payload_bytes
        hook_result_bytes += result.result_bytes
        checkpoints.append(f"hook-jobs:{result.checkpoint}")

    return {
        "archivedObservations": archived,
        "expiredObservations": observation_expired,
        "expiredObservationPayloadBytes": observation_payload_bytes,
        "expiredHookJobs": hook_expired,
        "expiredHookPayloadBytes": hook_payload_bytes,
        "expiredHookResultBytes": hook_result_bytes,
        "checkpoints": checkpoints,
    }


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_quick_check(path: Path | str) -> str:
    connection = sqlite3.connect(str(path))
    try:
        return str(connection.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        connection.close()


def create_retention_backup(
    observation_store: ObservationStore,
    hook_queue: HookJobQueue,
    backup_dir: Path | str,
    *,
    plan_summary: dict[str, Any],
) -> Path:
    target_dir = assert_safe_path(backup_dir, reject_hardlink_target=False).resolve()
    if target_dir.exists() and any(target_dir.iterdir()):
        raise FileExistsError(f"retention backup directory is not empty: {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)
    assert_safe_path(target_dir, reject_hardlink_target=False)
    entries: list[dict[str, Any]] = []
    for kind, source_path, backup_name, backup_func in (
        ("observations", observation_store.path, "observations.sqlite3", observation_store.backup_to),
        ("hook-jobs", hook_queue.path, "hook-jobs.sqlite3", hook_queue.backup_to),
    ):
        backup_path = target_dir / backup_name
        backup_func(backup_path)
        quick_check = sqlite_quick_check(backup_path)
        if quick_check != "ok":
            raise RuntimeError(f"retention backup quick_check failed for {backup_path}: {quick_check}")
        entries.append(
            {
                "kind": kind,
                "sourcePath": str(Path(source_path).resolve()),
                "backupPath": str(backup_path),
                "bytes": backup_path.stat().st_size,
                "sha256": sha256_file(backup_path),
                "quickCheck": quick_check,
            }
        )
    manifest = {
        "schemaVersion": RETENTION_BACKUP_SCHEMA_VERSION,
        "createdAt": utc_iso(_utc_now()),
        "plan": plan_summary,
        "entries": entries,
    }
    manifest_path = assert_safe_path(target_dir / "retention-backup-manifest.json")
    replace_bytes_safely(
        manifest_path,
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return manifest_path


def restore_retention_backup(manifest_path: Path | str, restore_dir: Path | str) -> dict[str, Any]:
    source_manifest = assert_safe_path(manifest_path).resolve()
    payload = json.loads(source_manifest.read_text(encoding="utf-8"))
    if str(payload.get("schemaVersion") or "") != RETENTION_BACKUP_SCHEMA_VERSION:
        raise RetentionPolicyError("unsupported retention backup manifest schema")
    target_dir = assert_safe_path(restore_dir, reject_hardlink_target=False).resolve()
    if target_dir.exists() and any(target_dir.iterdir()):
        raise FileExistsError(f"retention restore directory is not empty: {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)
    assert_safe_path(target_dir, reject_hardlink_target=False)
    restored: list[dict[str, Any]] = []
    for entry in payload.get("entries") or []:
        backup_path = assert_safe_path(str(entry.get("backupPath") or "")).resolve()
        expected_hash = str(entry.get("sha256") or "")
        actual_hash = sha256_file(backup_path)
        if actual_hash != expected_hash:
            raise RuntimeError(f"retention backup hash mismatch: {backup_path}")
        target_path = assert_safe_path(target_dir / backup_path.name)
        shutil.copy2(backup_path, target_path)
        restored_hash = sha256_file(target_path)
        quick_check = sqlite_quick_check(target_path)
        if restored_hash != expected_hash or quick_check != "ok":
            raise RuntimeError(f"retention restore verification failed: {target_path}")
        restored.append(
            {
                "kind": str(entry.get("kind") or ""),
                "path": str(target_path),
                "bytes": target_path.stat().st_size,
                "sha256": restored_hash,
                "quickCheck": quick_check,
            }
        )
    return {
        "success": True,
        "manifestPath": str(source_manifest),
        "restoreDir": str(target_dir),
        "restored": restored,
    }
