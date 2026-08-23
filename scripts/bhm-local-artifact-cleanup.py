"""Fail-closed cleanup for explicitly disposable local BHM artifacts.

The command plans by default.  Applying a plan requires its exact digest, so a
changed file, policy, timestamp, or candidate set stops cleanup before writes.
It never traverses roots recursively to discover targets and it refuses
reparse-points, authoritative storage, backups, and rollback evidence.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


POLICY_SCHEMA = "bhm.local-artifact-retention-policy.v1"
DEFAULT_POLICY = Path(__file__).resolve().parents[1] / "config" / "local-artifact-retention-policy.json"


class ArtifactCleanupError(RuntimeError):
    """Raised when a cleanup plan is invalid or unsafe to apply."""


@dataclass(frozen=True)
class ArtifactCandidate:
    rule_id: str
    path: str
    kind: str
    bytes: int
    modified_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "path": self.path,
            "kind": self.kind,
            "bytes": self.bytes,
            "modified_at": self.modified_at,
        }


def _parse_as_of(value: str | None) -> dt.datetime:
    if value is None:
        return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ArtifactCleanupError("--as-of must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ArtifactCleanupError("--as-of must include a UTC offset or Z")
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0)


def _as_posix_relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ArtifactCleanupError(f"candidate escapes repository root: {path}") from exc


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _tree_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if _is_reparse_point(child):
            raise ArtifactCleanupError(f"reparse-point inside candidate: {child}")
        if child.is_file():
            total += child.stat().st_size
    return total


def _load_policy(policy_path: Path) -> dict[str, Any]:
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactCleanupError(f"cannot read policy: {policy_path}") from exc
    if policy.get("schemaVersion") != POLICY_SCHEMA:
        raise ArtifactCleanupError("unsupported local artifact retention policy schema")
    if not isinstance(policy.get("managedRoots"), list) or not isinstance(policy.get("protectedRoots"), list):
        raise ArtifactCleanupError("policy must declare managedRoots and protectedRoots")
    if not isinstance(policy.get("rules"), list):
        raise ArtifactCleanupError("policy must declare rules")
    return policy


def _safe_policy_path(root: Path, value: str) -> Path:
    candidate = (root / value).resolve(strict=False)
    _as_posix_relative(root, candidate)
    if value in {"", "."}:
        return root
    if Path(value).is_absolute() or ".." in Path(value).parts:
        raise ArtifactCleanupError(f"unsafe policy path: {value}")
    return candidate


def _is_protected(relative: str, protected: set[str]) -> bool:
    return any(relative == item or relative.startswith(f"{item}/") for item in protected)


def _candidate_from_path(root: Path, path: Path, rule: dict[str, Any]) -> ArtifactCandidate | None:
    if _is_reparse_point(path):
        raise ArtifactCleanupError(f"reparse-point candidate rejected: {path}")
    kind = str(rule.get("kind"))
    if kind == "file" and not path.is_file():
        return None
    if kind == "directory" and not path.is_dir():
        return None
    if kind not in {"file", "directory"}:
        raise ArtifactCleanupError(f"unsupported rule kind: {kind}")
    if bool(rule.get("emptyOnly")) and any(path.iterdir()):
        return None
    return ArtifactCandidate(
        rule_id=str(rule["id"]),
        path=_as_posix_relative(root, path),
        kind=kind,
        bytes=_tree_bytes(path),
        modified_at=dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )


def build_plan(root: Path, policy_path: Path = DEFAULT_POLICY, *, as_of: str | None = None) -> dict[str, object]:
    root = root.resolve(strict=True)
    policy = _load_policy(policy_path)
    as_of_time = _parse_as_of(as_of)
    protected = {str(Path(item).as_posix()).rstrip("/") for item in policy["protectedRoots"]}
    candidates: list[ArtifactCandidate] = []
    blocked: list[dict[str, str]] = []
    seen_paths: set[str] = set()

    for rule in policy["rules"]:
        if not isinstance(rule, dict) or not isinstance(rule.get("id"), str):
            raise ArtifactCleanupError("every policy rule needs an id")
        parent_value = str(rule.get("parent", ""))
        parent = _safe_policy_path(root, parent_value)
        parent_relative = _as_posix_relative(root, parent)
        managed_roots = {str(Path(item).as_posix()).rstrip("/") for item in policy["managedRoots"]}
        is_managed_parent = parent_relative in managed_roots or any(
            parent_relative == managed_root or parent_relative.startswith(f"{managed_root}/")
            for managed_root in managed_roots
        )
        if not is_managed_parent:
            raise ArtifactCleanupError(f"rule parent is outside managed roots: {parent_value}")
        if not parent.is_dir() or _is_reparse_point(parent):
            continue
        pattern = str(rule.get("glob", ""))
        if not pattern or "/" in pattern or "\\" in pattern:
            raise ArtifactCleanupError(f"rule glob must match direct children only: {rule.get('id')}")
        minimum_age = float(rule.get("minAgeDays", -1))
        if minimum_age < 0:
            raise ArtifactCleanupError(f"rule minAgeDays must be non-negative: {rule.get('id')}")
        for path in sorted(parent.iterdir(), key=lambda item: item.name.casefold()):
            if not fnmatch.fnmatchcase(path.name, pattern):
                continue
            try:
                relative = _as_posix_relative(root, path)
                if _is_protected(relative, protected):
                    continue
                modified = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
                if as_of_time - modified < dt.timedelta(days=minimum_age):
                    continue
                candidate = _candidate_from_path(root, path, rule)
            except OSError as exc:
                blocked.append({"rule_id": str(rule["id"]), "path": _as_posix_relative(root, path), "reason": str(exc)})
                continue
            if candidate and candidate.path not in seen_paths:
                candidates.append(candidate)
                seen_paths.add(candidate.path)

    candidate_rows = [candidate.as_dict() for candidate in candidates]
    digest_payload = {
        "schemaVersion": POLICY_SCHEMA,
        "as_of": as_of_time.isoformat().replace("+00:00", "Z"),
        "policy_sha256": hashlib.sha256(policy_path.read_bytes()).hexdigest(),
        "candidates": candidate_rows,
        "blocked": blocked,
    }
    digest = hashlib.sha256(json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        **digest_payload,
        "plan_digest": digest,
        "summary": {"candidates": len(candidate_rows), "blocked": len(blocked), "bytes": sum(candidate.bytes for candidate in candidates)},
    }


def apply_plan(root: Path, policy_path: Path, *, as_of: str, expected_digest: str) -> dict[str, object]:
    plan = build_plan(root, policy_path, as_of=as_of)
    if plan["plan_digest"] != expected_digest:
        raise ArtifactCleanupError("plan digest mismatch; rebuild and review the current dry-run")
    if plan["blocked"]:
        raise ArtifactCleanupError("plan contains inaccessible candidates; resolve or remove the rule before cleanup")
    removed: list[dict[str, object]] = []
    root = root.resolve(strict=True)
    for row in plan["candidates"]:
        path = root / str(row["path"])
        if not path.exists() or _is_reparse_point(path):
            raise ArtifactCleanupError(f"candidate changed before cleanup: {path}")
        if row["kind"] == "file":
            path.unlink()
        else:
            shutil.rmtree(path)
        removed.append(row)
    return {**plan, "applied": True, "removed": removed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--as-of", help="UTC ISO-8601 timestamp; required for --apply")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-plan-digest")
    args = parser.parse_args()
    try:
        if args.apply:
            if not args.as_of or not args.confirm_plan_digest:
                raise ArtifactCleanupError("--apply requires --as-of and --confirm-plan-digest")
            report = apply_plan(args.root, args.policy, as_of=args.as_of, expected_digest=args.confirm_plan_digest)
        else:
            report = build_plan(args.root, args.policy, as_of=args.as_of)
    except ArtifactCleanupError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
