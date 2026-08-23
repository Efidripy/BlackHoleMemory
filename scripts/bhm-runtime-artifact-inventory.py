"""Read-only governance inventory for local BHM runtime artifacts.

The report assigns every policy root a deliberate disposition.  It never moves,
archives, compresses, or deletes data; an ``archive-review`` result is only a
prompt for a separately approved, receipt-preserving archival decision.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import stat
import sys
from pathlib import Path
from typing import Any


POLICY_SCHEMA = "bhm.runtime-artifact-governance.v1"
DEFAULT_POLICY = Path(__file__).resolve().parents[1] / "config" / "runtime-artifact-governance.json"


class RuntimeArtifactInventoryError(RuntimeError):
    """Raised for unsafe or invalid governance policy inputs."""


def _parse_as_of(value: str | None) -> dt.datetime:
    if value is None:
        return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeArtifactInventoryError("--as-of must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise RuntimeArtifactInventoryError("--as-of must include a UTC offset or Z")
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0)


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise RuntimeArtifactInventoryError(f"path escapes repository root: {path}") from exc


def _reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    return bool(getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _safe_path(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
        raise RuntimeArtifactInventoryError(f"unsafe policy path: {value!r}")
    path = (root / value).resolve(strict=False)
    _relative(root, path)
    return path


def _load_policy(path: Path) -> dict[str, Any]:
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeArtifactInventoryError(f"cannot read policy: {path}") from exc
    if policy.get("schemaVersion") != POLICY_SCHEMA or not isinstance(policy.get("rules"), list):
        raise RuntimeArtifactInventoryError("unsupported runtime artifact governance policy")
    return policy


def _measure(path: Path) -> tuple[int, dt.datetime]:
    if _reparse(path):
        raise RuntimeArtifactInventoryError(f"reparse-point root rejected: {path}")
    total = 0
    latest = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    if path.is_file():
        return path.stat().st_size, latest
    for child in path.rglob("*"):
        if _reparse(child):
            raise RuntimeArtifactInventoryError(f"reparse-point below governed root: {child}")
        modified = dt.datetime.fromtimestamp(child.stat().st_mtime, tz=dt.timezone.utc)
        latest = max(latest, modified)
        if child.is_file():
            total += child.stat().st_size
    return total, latest


def build_inventory(root: Path, policy_path: Path = DEFAULT_POLICY, *, as_of: str | None = None) -> dict[str, object]:
    root = root.resolve(strict=True)
    policy = _load_policy(policy_path)
    now = _parse_as_of(as_of)
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    items: list[dict[str, object]] = []

    for rule in policy["rules"]:
        if not isinstance(rule, dict):
            raise RuntimeArtifactInventoryError("governance rules must be objects")
        rule_id = rule.get("id")
        disposition = rule.get("disposition")
        if not isinstance(rule_id, str) or not rule_id or rule_id in seen_ids:
            raise RuntimeArtifactInventoryError("governance rule ids must be unique")
        if disposition not in {"protected", "manual-classification", "archive-review"}:
            raise RuntimeArtifactInventoryError(f"unsupported disposition for {rule_id}")
        path = _safe_path(root, rule.get("path"))
        relative = _relative(root, path)
        if relative in seen_paths:
            raise RuntimeArtifactInventoryError(f"duplicate governed path: {relative}")
        seen_ids.add(rule_id)
        seen_paths.add(relative)
        row: dict[str, object] = {
            "id": rule_id,
            "path": relative,
            "owner": rule.get("owner"),
            "class": rule.get("class"),
            "disposition": disposition,
            "reason": rule.get("reason"),
        }
        if not path.exists():
            row["state"] = "missing"
            items.append(row)
            continue
        try:
            size, latest = _measure(path)
        except OSError as exc:
            row.update({"state": "blocked", "reason": str(exc)})
            items.append(row)
            continue
        row.update({"bytes": size, "latest_modified_at": latest.replace(microsecond=0).isoformat().replace("+00:00", "Z")})
        if disposition != "archive-review":
            row["state"] = disposition
        else:
            days = rule.get("minimumRetentionDays")
            if not isinstance(days, int) or days < 1:
                raise RuntimeArtifactInventoryError(f"archive-review rule requires positive minimumRetentionDays: {rule_id}")
            review_after = latest + dt.timedelta(days=days)
            row["review_after"] = review_after.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            row["state"] = "archive-review-due" if now >= review_after else "retain-until-review"
        items.append(row)

    payload = {
        "schemaVersion": POLICY_SCHEMA,
        "as_of": now.isoformat().replace("+00:00", "Z"),
        "policy_sha256": hashlib.sha256(policy_path.read_bytes()).hexdigest(),
        "items": items,
    }
    payload["inventory_digest"] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    states: dict[str, int] = {}
    for item in items:
        state = str(item["state"])
        states[state] = states.get(state, 0) + 1
    payload["summary"] = {
        "items": len(items),
        "reported_item_bytes": sum(int(item.get("bytes", 0)) for item in items),
        "reported_item_bytes_may_overlap": True,
        "states": states,
    }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--as-of", help="UTC ISO-8601 timestamp for deterministic review state")
    args = parser.parse_args()
    try:
        report = build_inventory(args.root, args.policy, as_of=args.as_of)
    except RuntimeArtifactInventoryError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
