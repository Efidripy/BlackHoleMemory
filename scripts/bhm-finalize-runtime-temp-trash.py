#!/usr/bin/env python
"""Fail closed finalization for an operator-reviewed BHM runtime trash stage."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME = ROOT / ".runtime"
EXPECTED_EXTERNAL_DATABASES = {"memories.sqlite3", "observations.sqlite3", "hook-jobs.sqlite3"}


class TempTrashFinalizationError(RuntimeError):
    """Raised when a staged runtime deletion does not meet its safety contract."""


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    return bool(getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_regular(path: Path, label: str) -> None:
    if not path.is_file() or _is_reparse_point(path):
        raise TempTrashFinalizationError(f"{label} must be a regular non-reparse file: {path}")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    _assert_regular(path, label)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TempTrashFinalizationError(f"cannot read {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise TempTrashFinalizationError(f"{label} must contain a JSON object: {path}")
    return payload


def _assert_no_reparse_points(root: Path) -> None:
    if _is_reparse_point(root):
        raise TempTrashFinalizationError(f"staged root is a reparse point: {root}")
    for path in root.rglob("*"):
        if _is_reparse_point(path):
            raise TempTrashFinalizationError(f"reparse point inside staged data: {path}")


def _remove_readonly(func: Any, path: str, _exc_info: Any) -> None:
    """Retry deletion of an operator-owned historical artifact with ReadOnly set."""

    candidate = Path(path)
    if _is_reparse_point(candidate):
        raise TempTrashFinalizationError(f"refusing to alter a reparse point: {candidate}")
    candidate.chmod(stat.S_IWRITE)
    func(path)


def build_plan(runtime_root: Path, stage_name: str) -> dict[str, Any]:
    runtime_root = runtime_root.resolve(strict=True)
    trash_root = (runtime_root / "TEMP_TRASH").resolve(strict=True)
    if not stage_name.startswith("historical-prune-") or "/" in stage_name or "\\" in stage_name:
        raise TempTrashFinalizationError("stage name must be a direct historical-prune-* child")
    target = (trash_root / stage_name).resolve(strict=True)
    if target.parent != trash_root:
        raise TempTrashFinalizationError(f"staged target escapes TEMP_TRASH: {target}")
    _assert_no_reparse_points(target)
    manifest_path = target / "manifest.json"
    manifest = _read_json(manifest_path, "staged manifest")
    if manifest.get("schemaVersion") != "bhm.runtime-temp-trash.v1":
        raise TempTrashFinalizationError("unsupported staged manifest schema")
    total_bytes = manifest.get("total_bytes")
    if not isinstance(total_bytes, int) or total_bytes < 1:
        raise TempTrashFinalizationError("staged manifest requires a positive total_bytes")
    external_value = manifest.get("external_live_backup")
    if not isinstance(external_value, str) or not external_value:
        raise TempTrashFinalizationError("staged manifest has no external live backup")
    external_manifest_path = Path(external_value).expanduser().resolve(strict=True)
    external_manifest = _read_json(external_manifest_path, "external backup manifest")
    if external_manifest.get("schemaVersion") != "bhm.external-live-backup.v1":
        raise TempTrashFinalizationError("external backup manifest has an unsupported schema")
    databases = external_manifest.get("sqlite_online_backups")
    if not isinstance(databases, list):
        raise TempTrashFinalizationError("external backup manifest has no SQLite verification")
    by_source = {row.get("source"): row for row in databases if isinstance(row, dict)}
    if set(by_source) != EXPECTED_EXTERNAL_DATABASES:
        raise TempTrashFinalizationError("external backup does not cover every active SQLite database")
    for name, row in by_source.items():
        if row.get("quick_check") != "ok" or row.get("foreign_key_errors") != 0:
            raise TempTrashFinalizationError(f"external SQLite backup is not verified: {name}")
    return {
        "schemaVersion": "bhm.runtime-temp-trash-finalization-plan.v1",
        "stage": stage_name,
        "target": str(target),
        "staged_manifest_sha256": _sha256(manifest_path),
        "staged_bytes": total_bytes,
        "external_backup_manifest": str(external_manifest_path),
        "external_backup_manifest_sha256": _sha256(external_manifest_path),
    }


def finalize(runtime_root: Path, stage_name: str, expected_plan_digest: str) -> dict[str, Any]:
    plan = build_plan(runtime_root, stage_name)
    digest = hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if digest != expected_plan_digest:
        raise TempTrashFinalizationError("plan digest mismatch; rebuild and review before deleting")
    target = Path(plan["target"])
    shutil.rmtree(target, onerror=_remove_readonly)
    if target.exists():
        raise TempTrashFinalizationError(f"staged deletion did not complete: {target}")
    return {**plan, "plan_digest": digest, "deleted": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-plan-digest")
    args = parser.parse_args(argv)
    try:
        if args.apply:
            if not args.confirm_plan_digest:
                raise TempTrashFinalizationError("--apply requires --confirm-plan-digest")
            report = finalize(args.runtime_root, args.stage, args.confirm_plan_digest)
        else:
            report = build_plan(args.runtime_root, args.stage)
            report["plan_digest"] = hashlib.sha256(
                json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        print(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (TempTrashFinalizationError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
