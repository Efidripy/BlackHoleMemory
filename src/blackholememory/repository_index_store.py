"""SQLite-backed index store with job lifecycle, snapshot management, and stale job disposition."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

from .filesystem_boundaries import assert_safe_path
from .observation_security import redact_secret_text
from .repository_index_store_base import _RepositoryIndexStoreBase
from .repository_index_types import (
    _REPOSITORY_INDEX_TABLES,
    STALE_JOB_DISPOSITION_APPLY_SCHEMA_VERSION,
    STALE_JOB_DISPOSITION_ERROR_CODE,
    STALE_JOB_DISPOSITION_PLAN_SCHEMA_VERSION,
    STALE_JOB_DISPOSITION_REASON,
    RepositoryIndexError,
    RepositoryState,
    _canonical_json,
    _clip,
    _sha256_bytes,
    _sha256_file,
    _sha256_json,
    _utc_now,
)

_LOGGER = logging.getLogger(__name__)


class SQLiteRepositoryIndexStore(_RepositoryIndexStoreBase):
    """Repository snapshot tables inside the canonical BHM SQLite database."""
    @staticmethod
    def _stale_disposition_job_binding(row: Mapping[str, Any]) -> dict[str, Any]:
        """Return a content-free immutable binding for one lifecycle row."""

        return {
            "job_id": str(row["job_id"]),
            "project": str(row["project"]),
            "root_id": str(row["root_id"]),
            "state_digest": str(row["state_digest"]),
            "config_digest": str(row["config_digest"]),
            "candidate_digest": str(row["candidate_digest"]),
            "git_status_sha256": str(row["git_status_sha256"]),
            "git_head": str(row["git_head"] or ""),
            "git_branch": str(row["git_branch"] or ""),
            "dirty": bool(row["dirty"]),
            "source_digest": _sha256_bytes(str(row["source_json"]).encode("utf-8")),
            "total_candidates": int(row["total_candidates"]),
            "prefiltered_skip_count": int(row["prefiltered_skip_count"]),
            "processed_count": int(row["processed_count"]),
            "indexed_count": int(row["indexed_count"]),
            "skipped_count": int(row["skipped_count"]),
            "reused_count": int(row["reused_count"]),
            "cursor_present": row["cursor_path"] is not None,
            "snapshot_present": row["snapshot_id"] is not None,
            "status": str(row["status"]),
        }

    @staticmethod
    def _stale_disposition_job_view(row: Mapping[str, Any]) -> dict[str, Any]:
        binding = SQLiteRepositoryIndexStore._stale_disposition_job_binding(row)
        return {
            "job_id": binding["job_id"],
            "status": binding["status"],
            "binding_digest": _sha256_json(binding),
            "total_candidates": binding["total_candidates"],
            "processed_count": binding["processed_count"],
            "indexed_count": binding["indexed_count"],
            "skipped_count": binding["skipped_count"],
            "reused_count": binding["reused_count"],
            "cursor_present": binding["cursor_present"],
            "snapshot_present": binding["snapshot_present"],
            "started_at": str(row["started_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _stale_disposition_pointer_state(
        connection: sqlite3.Connection,
        *,
        project: str,
        root_id: str,
    ) -> dict[str, dict[str, str] | None]:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        specifications = (
            ("repository_index_current", "snapshot_id"),
            ("repository_code_graph_current", "graph_snapshot_id"),
        )
        pointers: dict[str, dict[str, str] | None] = {}
        for table, identifier in specifications:
            if table not in tables:
                raise RepositoryIndexError(f"required current-pointer table is absent: {table}")
            row = connection.execute(
                f"SELECT {identifier}, updated_at FROM {table} WHERE project = ? AND root_id = ?",
                (project, root_id),
            ).fetchone()
            if row is None:
                raise RepositoryIndexError(f"required current pointer is absent: {table}")
            pointers[table] = {
                "snapshot_id": str(row[identifier]),
                "updated_at": str(row["updated_at"]),
            }
        return pointers

    @staticmethod
    def _stale_disposition_checkpoint_is_active(
        connection: sqlite3.Connection,
        *,
        project: str,
        root_id: str,
    ) -> bool:
        row = connection.execute(
            "SELECT value FROM repository_index_meta WHERE key = ?",
            (SQLiteRepositoryIndexStore._watch_checkpoint_key(project, root_id),),
        ).fetchone()
        if row is None:
            return False
        try:
            checkpoint = json.loads(str(row["value"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise RepositoryIndexError("repository-index watcher checkpoint is unreadable") from exc
        if not isinstance(checkpoint, dict):
            raise RepositoryIndexError("repository-index watcher checkpoint has an invalid shape")
        return str(checkpoint.get("status") or "").casefold() == "running"

    @staticmethod
    def _validate_stale_disposition_inputs(
        *,
        state: RepositoryState,
        job_ids: Sequence[str],
        operator: str,
        reason: str,
    ) -> tuple[list[str], str]:
        normalized_ids = [str(job_id).strip() for job_id in job_ids]
        if not 1 <= len(normalized_ids) <= 16 or any(not job_id for job_id in normalized_ids):
            raise ValueError("stale disposition requires between one and sixteen exact job IDs")
        if len(set(normalized_ids)) != len(normalized_ids):
            raise ValueError("stale disposition job IDs must be unique")
        normalized_operator = _clip(operator, 120).strip()
        if not normalized_operator:
            raise ValueError("stale disposition requires a bounded operator identity")
        operator_redaction = redact_secret_text(normalized_operator)
        if any(kind != "path-secret" for kind in operator_redaction.kinds):
            raise ValueError("stale disposition operator identity must be non-secret")
        if reason != STALE_JOB_DISPOSITION_REASON:
            raise ValueError(f"unsupported stale disposition reason: {reason}")
        if not state.git_head:
            raise RepositoryIndexError("current repository state has no Git HEAD binding")
        return sorted(normalized_ids), normalized_operator

    def _stale_disposition_plan_from_connection(
        self,
        connection: sqlite3.Connection,
        *,
        state: RepositoryState,
        job_ids: Sequence[str],
        operator: str,
        reason: str,
    ) -> dict[str, Any]:
        ids, normalized_operator = self._validate_stale_disposition_inputs(
            state=state,
            job_ids=job_ids,
            operator=operator,
            reason=reason,
        )
        placeholders = ", ".join("?" for _ in ids)
        rows = connection.execute(
            f"SELECT * FROM repository_index_jobs WHERE job_id IN ({placeholders}) ORDER BY job_id",
            ids,
        ).fetchall()
        if len(rows) != len(ids):
            found = {str(row["job_id"]) for row in rows}
            missing = sorted(set(ids) - found)
            raise RepositoryIndexError(f"repository-index job not found: {', '.join(missing)}")

        project = state.project
        root_id = state.root_id
        expected_root = str(state.root)
        for row in rows:
            if str(row["project"]) != project or str(row["root_id"]) != root_id:
                raise RepositoryIndexError("stale disposition job scope differs from current repository state")
            if str(row["root_path"]) != expected_root:
                raise RepositoryIndexError("stale disposition job root path differs from current repository state")
            if str(row["status"]) != "running":
                raise RepositoryIndexError(f"stale disposition job is not running: {row['job_id']}")
            if any(
                int(row[key]) != 0
                for key in ("processed_count", "indexed_count", "skipped_count", "reused_count")
            ):
                raise RepositoryIndexError(f"stale disposition job has recorded progress: {row['job_id']}")
            if row["cursor_path"] is not None or row["snapshot_id"] is not None:
                raise RepositoryIndexError(f"stale disposition job has a cursor or snapshot: {row['job_id']}")
            if str(row["state_digest"]) == state.state_digest:
                raise RepositoryIndexError(f"stale disposition job state has not drifted: {row['job_id']}")
            if str(row["git_head"] or "") == str(state.git_head):
                raise RepositoryIndexError(f"stale disposition job Git binding has not drifted: {row['job_id']}")

        active_rows = connection.execute(
            """
            SELECT job_id FROM repository_index_jobs
            WHERE project = ? AND root_id = ? AND status = 'running'
            ORDER BY job_id
            """,
            (project, root_id),
        ).fetchall()
        active_ids = [str(row["job_id"]) for row in active_rows]
        if active_ids != ids:
            raise RepositoryIndexError("stale disposition must account for every running job in its exact scope")
        if self._stale_disposition_checkpoint_is_active(
            connection,
            project=project,
            root_id=root_id,
        ):
            raise RepositoryIndexError("repository-index watcher checkpoint reports an active worker")

        pointers = self._stale_disposition_pointer_state(connection, project=project, root_id=root_id)
        pointer_fingerprint = _sha256_json(pointers)
        jobs = [self._stale_disposition_job_view(row) for row in rows]
        payload = {
            "schema_version": STALE_JOB_DISPOSITION_PLAN_SCHEMA_VERSION,
            "project": project,
            "root_id": root_id,
            "current_state_digest": state.state_digest,
            "current_git_head": str(state.git_head),
            "operator": normalized_operator,
            "reason": reason,
            "jobs": jobs,
            "current_pointers": pointers,
            "pointer_fingerprint": pointer_fingerprint,
        }
        return {**payload, "plan_digest": _sha256_json(payload)}

    def plan_stale_job_disposition(
        self,
        *,
        state: RepositoryState,
        job_ids: Sequence[str],
        operator: str,
        reason: str = STALE_JOB_DISPOSITION_REASON,
    ) -> dict[str, Any]:
        """Build a deterministic, content-free stale-job disposition plan.

        Planning is read-only.  It admits only exact zero-progress running jobs
        whose state and Git bindings have drifted, with a stable index and code
        graph current pointer for the same scope.
        """

        schema = self.inspect_schema()
        if not schema["ready"]:
            raise RepositoryIndexError("repository-index schema is not ready")
        connection = self._connect(read_only=True)
        try:
            return self._stale_disposition_plan_from_connection(
                connection,
                state=state,
                job_ids=job_ids,
                operator=operator,
                reason=reason,
            )
        finally:
            connection.close()

    def _verify_stale_disposition_backup(
        self,
        backup_path: str | Path,
        *,
        expected_sha256: str,
    ) -> dict[str, Any]:
        backup = assert_safe_path(Path(backup_path).expanduser())
        if not backup.is_file():
            raise RepositoryIndexError("stale disposition backup is not a regular file")
        if backup.resolve() == self.path.resolve():
            raise RepositoryIndexError("stale disposition backup must not be the live database")
        normalized_expected = str(expected_sha256).strip().casefold()
        if len(normalized_expected) != 64 or any(character not in "0123456789abcdef" for character in normalized_expected):
            raise ValueError("stale disposition backup SHA-256 must be a 64-character hexadecimal digest")
        actual_sha256 = _sha256_file(backup)
        if actual_sha256 != normalized_expected:
            raise RepositoryIndexError("stale disposition backup SHA-256 differs from the confirmed value")
        connection = sqlite3.connect(
            f"file:{backup.as_posix()}?mode=ro",
            uri=True,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        try:
            integrity_check = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            if integrity_check != "ok" or quick_check != "ok" or foreign_key_errors:
                raise RepositoryIndexError("stale disposition backup fails SQLite integrity validation")
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            required_tables = _REPOSITORY_INDEX_TABLES | {
                "repository_code_graph_current",
            }
            if not required_tables.issubset(tables):
                raise RepositoryIndexError("stale disposition backup lacks required index/current-pointer tables")
        finally:
            connection.close()
        return {
            "backup_path": str(backup),
            "backup_sha256": actual_sha256,
            "integrity_check": integrity_check,
            "quick_check": quick_check,
            "foreign_key_error_count": 0,
        }

    def apply_stale_job_disposition(
        self,
        *,
        state: RepositoryState,
        plan: Mapping[str, Any],
        expected_plan_digest: str,
        offline: bool,
        backup_path: str | Path,
        backup_sha256: str,
    ) -> dict[str, Any]:
        """Atomically mark exact stale zero-progress jobs as failed.

        This is intentionally not a generic job-status API.  A caller must
        provide a plan produced by :meth:`plan_stale_job_disposition`, confirm
        its exact digest, attest the offline boundary, and provide a separately
        verified SQLite recovery file.  No candidate, snapshot or pointer row is
        deleted or updated.
        """

        if offline is not True:
            raise ValueError("stale disposition apply requires an explicit offline boundary")
        plan_payload = dict(plan)
        plan_digest = str(plan_payload.get("plan_digest") or "")
        if not plan_digest or plan_digest != str(expected_plan_digest):
            raise RepositoryIndexError("stale disposition plan digest confirmation differs")
        unsigned_plan = {key: value for key, value in plan_payload.items() if key != "plan_digest"}
        if str(unsigned_plan.get("schema_version") or "") != STALE_JOB_DISPOSITION_PLAN_SCHEMA_VERSION:
            raise RepositoryIndexError("stale disposition plan schema is unsupported")
        if _sha256_json(unsigned_plan) != plan_digest:
            raise RepositoryIndexError("stale disposition plan payload does not match its digest")
        jobs = unsigned_plan.get("jobs")
        if not isinstance(jobs, list):
            raise RepositoryIndexError("stale disposition plan has no exact job list")
        job_ids = [str(item.get("job_id") or "") for item in jobs if isinstance(item, Mapping)]
        if len(job_ids) != len(jobs):
            raise RepositoryIndexError("stale disposition plan job list is invalid")
        operator = str(unsigned_plan.get("operator") or "")
        reason = str(unsigned_plan.get("reason") or "")
        backup_evidence = self._verify_stale_disposition_backup(
            backup_path,
            expected_sha256=backup_sha256,
        )

        with self._write_lock:
            connection = self._transaction()
            try:
                refreshed = self._stale_disposition_plan_from_connection(
                    connection,
                    state=state,
                    job_ids=job_ids,
                    operator=operator,
                    reason=reason,
                )
                if refreshed["plan_digest"] != plan_digest:
                    raise RepositoryIndexError("stale disposition plan drifted before apply")
                before_jobs = list(refreshed["jobs"])
                applied_at = _utc_now()
                for item in before_jobs:
                    detail = _canonical_json(
                        {
                            "schema_version": STALE_JOB_DISPOSITION_APPLY_SCHEMA_VERSION,
                            "reason": reason,
                            "operator": operator,
                            "plan_digest": plan_digest,
                            "job_binding_digest": item["binding_digest"],
                            "pointer_fingerprint": refreshed["pointer_fingerprint"],
                            "applied_at": applied_at,
                        }
                    )
                    updated = connection.execute(
                        """
                        UPDATE repository_index_jobs
                        SET status = 'failed', error_code = ?, error_detail = ?, updated_at = ?
                        WHERE job_id = ? AND status = 'running'
                        """,
                        (STALE_JOB_DISPOSITION_ERROR_CODE, detail, applied_at, item["job_id"]),
                    ).rowcount
                    if updated != 1:
                        raise RepositoryIndexError("stale disposition row drifted during apply")
                after_pointers = self._stale_disposition_pointer_state(
                    connection,
                    project=state.project,
                    root_id=state.root_id,
                )
                if _sha256_json(after_pointers) != refreshed["pointer_fingerprint"]:
                    raise RepositoryIndexError("current pointer changed during stale disposition apply")
                foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
                if foreign_key_errors:
                    raise RepositoryIndexError("foreign-key check failed during stale disposition apply")
                connection.commit()
            except Exception:  # broad: preserve rollback for atomic stale-job disposition
                _LOGGER.error("repository index stale-disposition transaction failed; rolling back")
                connection.rollback()
                raise
            finally:
                connection.close()
        after_jobs = [self._stale_disposition_job_view(self.job(str(item["job_id"]))) for item in before_jobs]
        return {
            "schema_version": STALE_JOB_DISPOSITION_APPLY_SCHEMA_VERSION,
            "ok": True,
            "plan_digest": plan_digest,
            "before_jobs": before_jobs,
            "after_jobs": after_jobs,
            "pointer_fingerprint": refreshed["pointer_fingerprint"],
            "backup": backup_evidence,
            "writes_sqlite_state": True,
            "writes_memory_rows": False,
            "writes_qdrant": False,
            "deletes_job_rows": False,
            "deletes_job_candidates": False,
            "updates_current_pointers": False,
        }

