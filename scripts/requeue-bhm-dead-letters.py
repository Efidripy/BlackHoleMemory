"""Preview or safely requeue dead-lettered BHM projection events.

Dead letters are terminal by design, so recovery is explicit and backup-first.
The default command is read-only. ``--apply`` requires ``--backup-root`` and
uses SQLite's online backup API before moving dead letters back to ``pending``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from blackholememory.filesystem_boundaries import assert_safe_path
from blackholememory.filesystem_boundaries import replace_bytes_safely


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = REPO_ROOT / ".runtime" / "live-memory" / "memories.sqlite3"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _event_digest(event_ids: list[str]) -> str:
    payload = "\n".join(sorted(event_ids)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_dead_letters(database: Path) -> tuple[list[str], str | None]:
    database = assert_safe_path(database)
    if not database.exists():
        return [], "database does not exist"
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT event_id FROM memory_outbox WHERE status = 'dead_letter' ORDER BY event_id"
        ).fetchall()
        return [str(row[0]) for row in rows], None
    except sqlite3.DatabaseError as exc:
        return [], str(exc)
    finally:
        connection.close()


def _online_backup(database: Path, backup: Path) -> None:
    database = assert_safe_path(database)
    backup = assert_safe_path(backup)
    if backup.exists():
        raise FileExistsError(f"backup already exists: {backup}")
    backup.parent.mkdir(parents=True, exist_ok=True)
    assert_safe_path(backup.parent, reject_hardlink_target=False)
    # Re-check after directory creation and immediately before SQLite opens the
    # destination so a raced link/reparse target fails closed.
    assert_safe_path(backup)
    source = sqlite3.connect(database)
    target = sqlite3.connect(backup)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _apply(database: Path, backup_root: Path, event_digest: str) -> dict[str, Any]:
    database = assert_safe_path(database)
    current_ids, error = _read_dead_letters(database)
    if error:
        raise RuntimeError(f"cannot revalidate dead-letter preview: {error}")
    current_digest = _event_digest(current_ids)
    if current_digest != event_digest:
        raise RuntimeError("dead-letter preview is stale; re-run the read-only preview")
    backup_root = assert_safe_path(backup_root, reject_hardlink_target=False)
    backup_root.mkdir(parents=True, exist_ok=True)
    assert_safe_path(backup_root, reject_hardlink_target=False)
    backup = backup_root / database.name
    manifest_path = assert_safe_path(backup_root / "requeue-manifest.json")
    _online_backup(database, backup)
    now = _utc_now()
    connection = sqlite3.connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            UPDATE memory_outbox
            SET status = 'pending', attempts = 0, available_at = ?,
                claimed_at = NULL, claim_token = NULL,
                last_error = 'explicit dead-letter requeue', updated_at = ?
            WHERE status = 'dead_letter'
            """,
            (now, now),
        )
        changed = int(cursor.rowcount)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    manifest = {
        "database": str(database),
        "backup": str(backup),
        "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
        "event_digest": event_digest,
        "revalidated_event_digest": current_digest,
        "changed": changed,
        "timestamp": now,
    }
    replace_bytes_safely(
        manifest_path,
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-root", type=Path)
    args = parser.parse_args()

    database = args.database.expanduser().resolve()
    event_ids, error = _read_dead_letters(database)
    if error:
        print(json.dumps({"ok": False, "database": str(database), "error": error}, indent=2))
        return 1
    digest = _event_digest(event_ids)
    report: dict[str, Any] = {
        "ok": True,
        "apply": bool(args.apply),
        "database": str(database),
        "dead_letter_count": len(event_ids),
        "event_digest": digest,
        "writes_live_state": False,
    }
    if not args.apply:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.backup_root is None:
        parser.error("--apply requires --backup-root")
    manifest = _apply(database, args.backup_root.expanduser().resolve(), digest)
    report.update({"writes_live_state": True, "manifest": manifest})
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
