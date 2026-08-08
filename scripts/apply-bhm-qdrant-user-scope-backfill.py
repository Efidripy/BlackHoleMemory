"""Plan, apply, or rollback the BHM Qdrant user/data payload backfill."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from blackholememory.config import settings
from blackholememory.filesystem_boundaries import assert_safe_path
from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.mem0_adapter import get_qdrant_client
from blackholememory.mem0_adapter import global_collection_name
from blackholememory.mem0_adapter import local_collection_name


BACKUP_VERSION = 1
PAGE_SIZE = 256
MAX_PAGES = 10000


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _payload_sha256(payload: dict[str, Any]) -> str:
    return _sha256_text(_canonical_json(payload))


def _scroll_points(client: Any, collection_name: str) -> list[Any]:
    points: list[Any] = []
    offset: Any = None
    for _page in range(MAX_PAGES):
        batch, offset = client.scroll(
            collection_name=collection_name,
            offset=offset,
            limit=PAGE_SIZE,
            with_payload=True,
            with_vectors=False,
        )
        points.extend(batch or [])
        if offset is None:
            return points
    raise RuntimeError(f"scroll page limit exceeded for {collection_name}")


def _target_rows(client: Any, collections: Iterable[str], expected_user_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    collection_counts: list[dict[str, Any]] = []
    mismatches = 0
    missing_source_ids = 0
    for collection_name in sorted(set(collections)):
        points = _scroll_points(client, collection_name)
        count = 0
        missing_user = 0
        missing_data = 0
        for point in points:
            payload = dict(getattr(point, "payload", None) or {})
            point_id = str(getattr(point, "id", "") or "")
            source_id = str(payload.get("source_id") or "")
            user_id = payload.get("user_id")
            data = str(payload.get("data") or "").strip()
            content = str(payload.get("content") or payload.get("memory") or "")
            if source_id == "":
                missing_source_ids += 1
            if user_id not in (None, "") and str(user_id) != expected_user_id:
                mismatches += 1
            add_user = user_id in (None, "")
            add_data = not data
            if add_user:
                missing_user += 1
            if add_data:
                missing_data += 1
            if not add_user and not add_data:
                continue
            count += 1
            targets.append(
                {
                    "collection": collection_name,
                    "point_id": point_id,
                    "source_id": source_id,
                    "payload": payload,
                    "payload_sha256": _payload_sha256(payload),
                    "add_user_id": add_user,
                    "add_data": add_data,
                    "data_value": data or content,
                }
            )
        collection_counts.append(
            {
                "collection": collection_name,
                "point_count": len(points),
                "target_count": count,
                "missing_user_scope": missing_user,
                "missing_data_field": missing_data,
            }
        )
    target_rows = sorted(targets, key=lambda item: (item["collection"], item["point_id"]))
    target_digest = _sha256_text(
        "\n".join(
            f"{row['collection']}|{row['point_id']}|{row['source_id']}|{row['payload_sha256']}"
            for row in target_rows
        )
    )
    summary = {
        "collections": collection_counts,
        "point_count": sum(int(item["point_count"]) for item in collection_counts),
        "target_count": len(target_rows),
        "missing_user_scope": sum(int(item["missing_user_scope"]) for item in collection_counts),
        "missing_data_field": sum(int(item["missing_data_field"]) for item in collection_counts),
        "mismatched_user_scope": mismatches,
        "missing_source_id": missing_source_ids,
        "target_digest": target_digest,
    }
    return target_rows, summary


def build_plan(project: str = "blackholememory") -> tuple[Any, dict[str, Any], list[dict[str, Any]]]:
    client = get_qdrant_client()
    collections = [local_collection_name(project), global_collection_name()]
    targets, summary = _target_rows(client, collections, str(settings.mem0_user_id))
    plan = {
        "ok": summary["mismatched_user_scope"] == 0 and summary["missing_source_id"] == 0,
        "mode": "user-data-scope-backfill",
        "mutation": False,
        "expected_user_id": str(settings.mem0_user_id),
        "required_fields": ["user_id", "data"],
        "summary": summary,
        "apply_boundary": {
            "requires_confirm": True,
            "requires_backup_dir": True,
            "backup_version": BACKUP_VERSION,
            "update_method": "Qdrant set_payload for missing fields only",
            "rollback_method": "Qdrant overwrite_payload from hash-verified backup",
        },
    }
    return client, plan, targets


def _backup_lines(targets: list[dict[str, Any]]) -> list[str]:
    return [
        _canonical_json(
            {
                "collection": row["collection"],
                "point_id": row["point_id"],
                "source_id": row["source_id"],
                "payload": row["payload"],
                "payload_sha256": row["payload_sha256"],
            }
        )
        for row in targets
    ]


def _write_backup(backup_dir: Path, targets: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
    safe_backup_dir = assert_safe_path(backup_dir, reject_hardlink_target=False)
    lines = _backup_lines(targets)
    payload_path = safe_backup_dir / "payloads.jsonl"
    payload_text = "\n".join(lines) + ("\n" if lines else "")
    replace_bytes_safely(payload_path, payload_text.encode("utf-8"))
    payload_sha = hashlib.sha256(payload_path.read_bytes()).hexdigest()
    manifest = {
        "backup_version": BACKUP_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "expected_user_id": plan["expected_user_id"],
        "target_count": len(targets),
        "target_digest": plan["summary"]["target_digest"],
        "payload_file": payload_path.name,
        "payload_sha256": payload_sha,
    }
    manifest_path = safe_backup_dir / "manifest.json"
    replace_bytes_safely(
        manifest_path,
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    verify_lines = payload_path.read_text(encoding="utf-8").splitlines()
    if verify_lines != lines:
        raise RuntimeError("backup verification failed: payload lines changed after write")
    return manifest


def _apply(client: Any, targets: list[dict[str, Any]], expected_user_id: str) -> int:
    applied = 0
    for row in targets:
        if not row["data_value"] and row["add_data"]:
            raise RuntimeError(f"cannot derive data for {row['collection']}:{row['point_id']}")
        payload: dict[str, Any] = {}
        if row["add_user_id"]:
            payload["user_id"] = expected_user_id
        if row["add_data"]:
            payload["data"] = row["data_value"]
        if payload:
            client.set_payload(
                collection_name=row["collection"],
                payload=payload,
                points=[row["point_id"]],
                wait=True,
            )
            applied += 1
    return applied


def _load_backup(backup_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    safe_backup_dir = assert_safe_path(backup_dir, reject_hardlink_target=False)
    manifest_path = assert_safe_path(safe_backup_dir / "manifest.json")
    payload_path = assert_safe_path(safe_backup_dir / "payloads.jsonl")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("backup_version") != BACKUP_VERSION or manifest.get("payload_file") != payload_path.name:
        raise RuntimeError("unsupported or inconsistent backup manifest")
    if hashlib.sha256(payload_path.read_bytes()).hexdigest() != manifest.get("payload_sha256"):
        raise RuntimeError("backup payload hash mismatch")
    rows = [json.loads(line) for line in payload_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != int(manifest.get("target_count") or -1):
        raise RuntimeError("backup target count mismatch")
    return manifest, rows


def _rollback(client: Any, rows: list[dict[str, Any]]) -> int:
    restored = 0
    for row in rows:
        payload = row.get("payload")
        if not isinstance(payload, dict) or _payload_sha256(payload) != row.get("payload_sha256"):
            raise RuntimeError(f"backup payload digest mismatch for {row.get('collection')}:{row.get('point_id')}")
        client.overwrite_payload(
            collection_name=row["collection"],
            payload=payload,
            points=[row["point_id"]],
            wait=True,
        )
        restored += 1
    return restored


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("plan", "apply", "rollback"), default="plan")
    parser.add_argument("--project", default="blackholememory")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    if args.action in {"apply", "rollback"} and (not args.confirm or args.backup_dir is None):
        parser.error("apply/rollback requires --confirm and --backup-dir")

    if args.action == "rollback":
        client = get_qdrant_client()
        manifest, rows = _load_backup(args.backup_dir)
        restored = _rollback(client, rows)
        result = {"ok": True, "action": "rollback", "mutation": True, "restored": restored, "manifest": manifest}
    else:
        client, plan, targets = build_plan(args.project)
        if args.action == "plan":
            result = {**plan, "action": "plan", "target_sample": [row["point_id"] for row in targets[:10]]}
        else:
            if plan["summary"]["mismatched_user_scope"] or plan["summary"]["missing_source_id"]:
                raise RuntimeError("refusing apply with mismatched user scopes or missing source ids")
            manifest = _write_backup(args.backup_dir, targets, plan)
            applied = _apply(client, targets, plan["expected_user_id"])
            result = {**plan, "ok": True, "action": "apply", "mutation": True, "applied": applied, "backup": manifest}

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
